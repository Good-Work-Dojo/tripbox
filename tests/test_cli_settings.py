# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What a command does with what is configured: a core problem stops it, a web problem stops
`web`, and a feature that is off or half configured is said and left off."""

import argparse
import logging
import os
from types import SimpleNamespace

import anthropic
import openai
import pytest
import uvicorn
from helpers.modelserver import FakeModelServer
from helpers.sample import CONFIG_DIR

from tripbox import cli
from tripbox import log as logmod
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.mail import imap as imapmod
from tripbox.parse import provider
from tripbox.pipeline import run as runmod
from tripbox.trips.store import TripStore

CLOUDFLARE_KEYS = ("TRIPBOX_ACCESS_TEAM", "TRIPBOX_ACCESS_AUD", "TRIPBOX_ALLOWED_EMAILS")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Nothing configured but where the files go."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    return tmp_path


def test_a_core_problem_stops_a_command_before_it_starts(env, monkeypatch, capsys):
    monkeypatch.setenv("TZ", "Mars/Base")
    assert cli.main(["trips", "list"]) == 2
    refusals = [line for line in capsys.readouterr().err.splitlines() if "config error" in line]
    assert refusals == [
        "config error: TZ: unknown time zone 'Mars/Base' — use an IANA name such as America/Chicago"
    ]
    assert not (env / "data" / "ledger.db").exists()


def test_web_refuses_to_start_with_web_problems_and_says_each(env, monkeypatch, capsys):
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    assert cli.main(["web"]) == 2
    refusals = [line for line in capsys.readouterr().err.splitlines() if "config error" in line]
    assert refusals == [
        "config error: TRIPBOX_PASSWORD: not set — choose a password of 10 characters or more,"
        ' or set TRIPBOX_PASSWORD_HASH to the line "tripbox passwd" prints'
    ]
    monkeypatch.setenv("TRIPBOX_AUTH", "cloudflare")
    assert cli.main(["web"]) == 2
    refusals = [line for line in capsys.readouterr().err.splitlines() if "config error" in line]
    assert refusals == [
        f"config error: {key}: not set — TRIPBOX_AUTH=cloudflare needs it"
        for key in CLOUDFLARE_KEYS
    ]
    assert served == []
    # nothing serves the page past its login: `web` takes no option but where it listens
    (commands,) = [
        a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    ]
    options = [o for a in commands.choices["web"]._actions for o in a.option_strings]
    assert options == ["-h", "--help", "--host", "--port"]
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    assert cli.main(["web"]) == 0
    assert len(served) == 1 and served[-1]["port"] == 8110
    # no sign-in is the local mode: with a public address it is refused, and the fix says so
    capsys.readouterr()
    monkeypatch.setenv("TRIPBOX_BASE_URL", "https://trips.example.com")
    assert cli.main(["web"]) == 2 and len(served) == 1
    refusals = [line for line in capsys.readouterr().err.splitlines() if "config error" in line]
    assert refusals == [
        "config error: TRIPBOX_AUTH: none with a public TRIPBOX_BASE_URL would publish the"
        " page unauthenticated — use password, proxy or cloudflare, or TRIPBOX_AUTH=none (the"
        " local mode) with TRIPBOX_BASE_URL on localhost"
    ]


def test_web_listens_where_listen_says_unless_told(env, monkeypatch):
    """`tripbox web` listens on TRIPBOX_LISTEN (by default this computer alone); `--host` and
    `--port` win, as the image's services pass all interfaces: a published port reaches nothing
    on a container's own loopback."""
    from tripbox.config import CONTAINER_PORT

    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    assert cli.main(["web"]) == 0
    monkeypatch.setenv("TRIPBOX_LISTEN", "[::1]:9000")
    assert cli.main(["web"]) == 0
    assert cli.main(["web", "--host", "127.0.0.1", "--port", str(CONTAINER_PORT)]) == 0
    assert cli.main(["web", "--port", "9001"]) == 0
    monkeypatch.setenv("TRIPBOX_IN_IMAGE", "1")
    assert cli.main(["web", "--host", "0.0.0.0", "--port", str(CONTAINER_PORT)]) == 0
    assert [(o["host"], o["port"]) for o in served] == [
        ("127.0.0.1", 8110),
        ("::1", 9000),
        ("127.0.0.1", 8110),
        ("::1", 9001),
        ("0.0.0.0", 8110),
    ]
    assert CONTAINER_PORT == 8110


def test_web_with_no_login_refuses_a_host_beyond_this_computer_outside_the_image(
    env, monkeypatch, capsys
):
    """Outside the image the address `web` binds is what is exposed, and the page's Host check
    is no wall: with no login, a `--host` that is not a loopback address (a name included) is
    refused before anything binds. Inside the image (`TRIPBOX_IN_IMAGE=1`, which its runtime
    stage sets) the container's every interface is what compose publishes at TRIPBOX_LISTEN,
    which no login keeps on loopback. A login takes any host."""
    from tripbox.config import KEY_BY_NAME

    marker = KEY_BY_NAME["TRIPBOX_IN_IMAGE"]
    assert (marker.feature, marker.web, marker.secret) == ("core", True, False)
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    for host in ("0.0.0.0", "192.0.2.10", "::", "somehost"):
        assert cli.main(["web", "--host", host]) == 2, host
        err = capsys.readouterr().err.splitlines()
        assert err == [
            f"web: --host {host} answers beyond this computer, and TRIPBOX_AUTH=none has no login"
            " — use TRIPBOX_AUTH=password, proxy or cloudflare, or --host 127.0.0.1"
        ], host
    assert served == [] and not (env / "data" / "ledger.db").exists()
    for host in ("127.0.0.1", "::1"):
        assert cli.main(["web", "--host", host]) == 0, host
    monkeypatch.setenv("TRIPBOX_IN_IMAGE", "1")
    assert cli.main(["web", "--host", "0.0.0.0"]) == 0
    monkeypatch.delenv("TRIPBOX_IN_IMAGE")
    monkeypatch.setenv("TRIPBOX_AUTH", "password")
    monkeypatch.setenv("TRIPBOX_PASSWORD", "a-long-enough-password")
    for host in ("0.0.0.0", "192.0.2.10", "::", "somehost"):
        assert cli.main(["web", "--host", host]) == 0, host
    assert [o["host"] for o in served] == [
        "127.0.0.1", "::1", "0.0.0.0", "0.0.0.0", "192.0.2.10", "::", "somehost"
    ]  # fmt: skip


def test_web_reads_no_forwarded_header_masks_secrets_and_logs_a_misspelt_name_once(
    env, monkeypatch, caplog
):
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setattr(logmod, "_SECRETS", set())
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_MAIL_PASSWORD", "S3CRET-MARK-0123456789")
    monkeypatch.setenv("TRIPBOX_TELEGRAM_BOTTOKEN", "S3CRET-MARK-misspelt")
    caplog.set_level(logging.INFO)
    assert cli.main(["web"]) == 0
    (options,) = served
    # the client is the socket's peer: no forwarded header is believed, from anywhere
    assert options["proxy_headers"] is False and "forwarded_allow_ips" not in options
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("config: ")]
    assert said == [
        "config: TRIPBOX_TELEGRAM_BOTTOKEN: not a tripbox setting"
        " — did you mean TRIPBOX_TELEGRAM_BOT_TOKEN?"
    ]
    assert logmod.redact("seen S3CRET-MARK-0123456789") == "seen [REDACTED]"


def test_poll_without_mail_settings_runs_the_rest_and_says_mail_is_off(env, monkeypatch, capsys):
    def no_inbox(*args, **kwargs):
        raise AssertionError("a mailbox was opened")

    purged = []
    monkeypatch.setattr(imapmod, "ImapInbox", no_inbox)
    monkeypatch.setattr(TripStore, "purge_deleted", lambda store: purged.append(1) or (0, 0))
    assert cli.main(["poll"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert (
        "mail is off: TRIPBOX_MAIL_HOST, TRIPBOX_MAIL_USER, TRIPBOX_MAIL_PASSWORD,"
        " TRIPBOX_MAIL_ALLOWED_SENDERS not set"
    ) in out
    led = Ledger.open(env / "data" / "ledger.db")
    assert led.kv.get("last_poll_attempt") and led.kv.get("last_poll_ok")
    led.close()
    assert purged == [1]  # the rest of the poll ran as with mail
    # half configured: each problem said, nothing run
    monkeypatch.setenv("TRIPBOX_MAIL_HOST", "imap.gmail.com")
    assert cli.main(["poll"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert (
        "TRIPBOX_MAIL_USER: not set — mail is half configured; set it, or remove TRIPBOX_MAIL_HOST"
    ) in err
    assert purged == [1]


def test_a_feature_with_invalid_settings_is_left_off_and_logged(env, monkeypatch, caplog, capsys):
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", "http://flightlog.invalid")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_APIKEY", "S3CRET-MARK")  # the key's name misspelt
    # a key pasted into the wrong line: the problem is logged, the value is not
    monkeypatch.setenv("TRIPBOX_AEROAPI_MONTHLY_BUDGET_USD", "S3CRET-MARK-0123456789abcdef")
    ctx = runmod.build_context(Settings.from_env())
    assert ctx.airtrail is None and ctx.syncer is None
    ctx.ledger.close()
    caplog.set_level(logging.INFO)
    assert cli.main(["upcoming"]) == 0
    said = [r for r in caplog.records if r.getMessage().startswith("config: ")]
    assert [r.levelname for r in said] == ["WARNING", "WARNING", "WARNING"]
    assert said[0].getMessage().startswith(
        "config: TRIPBOX_AIRTRAIL_API_KEY: not set — AirTrail is half configured"
    )
    assert said[1].getMessage() == (
        "config: TRIPBOX_AEROAPI_MONTHLY_BUDGET_USD: not a number of 0 or more"
        " — use a number such as 1.00"
    )
    assert said[2].getMessage() == (
        "config: TRIPBOX_AIRTRAIL_APIKEY: not a tripbox setting"
        " — did you mean TRIPBOX_AIRTRAIL_API_KEY?"
    )
    out = capsys.readouterr()
    for text in [r.getMessage() for r in caplog.records] + [caplog.text, out.out, out.err]:
        assert "S3CRET-MARK" not in text


def test_enrich_and_bot_say_their_feature_is_off(env, monkeypatch, capsys):
    built = []
    monkeypatch.setattr(runmod, "build_context", lambda *args, **kwargs: built.append(args))
    assert cli.main(["enrich"]) == 0
    off = capsys.readouterr().out.splitlines()
    assert off == ["enrichment is off: TRIPBOX_ENRICH, TRIPBOX_AEROAPI_KEY not set"]
    assert cli.main(["bot"]) == 2
    assert (
        "Telegram is off: TRIPBOX_TELEGRAM_BOT_TOKEN, TRIPBOX_TELEGRAM_CHAT_ID not set"
        in capsys.readouterr().err.splitlines()
    )
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "off airtrail",
        "off model",
        "off enrichment",
        "off notifications",
        "dry run: on (flights are filed; nothing is sent to AirTrail)",
    ]
    assert cli.main(["airtrail", "backfill"]) == 2
    assert (
        "AirTrail is off: TRIPBOX_AIRTRAIL_URL, TRIPBOX_AIRTRAIL_API_KEY not set"
        in capsys.readouterr().err.splitlines()
    )
    assert built == []
    # a key alone leaves it off
    monkeypatch.setenv("TRIPBOX_AEROAPI_KEY", "S3CRET-AERO")
    assert cli.main(["enrich"]) == 0
    assert capsys.readouterr().out.splitlines() == ["enrichment is off: TRIPBOX_ENRICH not set"]
    # set but not usable: the problems are said, the command does not run
    monkeypatch.setenv("TRIPBOX_ENRICH", "aeroapi")
    assert cli.main(["enrich"]) == 2
    out = capsys.readouterr()
    assert (
        "TRIPBOX_ENRICH: enrichment fills in AirTrail flights — set up AirTrail first,"
        " or remove TRIPBOX_ENRICH"
    ) in out.err.splitlines()
    assert "S3CRET-AERO" not in out.out + out.err and built == []


def refuse_to_build(*args, **kwargs):
    raise AssertionError("a client of the provider that is not configured was built")


CHECK_REST = (
    "off enrichment",
    "off notifications",
    "dry run: on (flights are filed; nothing is sent to AirTrail)",
)


def test_check_asks_anthropic_for_its_model(env, monkeypatch, capsys):
    asked = []

    def stand_in(**kwargs):
        asked.append(kwargs["api_key"])
        return SimpleNamespace(
            models=SimpleNamespace(
                retrieve=lambda name: SimpleNamespace(id=name, display_name="Stand-in")
            )
        )

    monkeypatch.setattr(anthropic, "Anthropic", stand_in)
    monkeypatch.setattr(openai, "OpenAI", refuse_to_build)
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "placeholder")
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "off airtrail",
        f"ok model: anthropic, {provider.DEFAULT_MODEL}",
        *CHECK_REST,
    ]
    assert asked == ["placeholder"]


def test_check_lists_the_openai_servers_models_and_quotes_nothing(env, monkeypatch, capsys):
    """One request to the configured server, through the provider's own client (its key, its
    address, no retry): ok, or FAIL with the error's kind and word, never the key or the
    address."""
    server = FakeModelServer("http://alex:S3CRET-PASS@models.example/v1")
    real = openai.OpenAI
    monkeypatch.setattr(
        openai, "OpenAI", lambda **kw: real(**kw, http_client=server.http_client())
    )
    monkeypatch.setattr(anthropic, "Anthropic", refuse_to_build)
    monkeypatch.setenv("TRIPBOX_LLM_PROVIDER", "openai")
    monkeypatch.setenv("TRIPBOX_LLM_MODEL", "served-model")
    monkeypatch.setenv("TRIPBOX_LLM_BASE_URL", server.base_url)
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "S3CRET-KEY-MARK")
    unlisted = (
        "ok model: openai, served-model is not in the server's list of 1 (some servers list"
        " only what is loaded)"
    )
    for behaviour, args, code, said in (
        (
            "listing",
            ("another-model", "served-model"),
            0,
            "ok model: openai, served-model is listed",
        ),
        ("listing", ("another-model",), 0, unlisted),
        ("fail", (401,), 1, "FAIL model: openai, unavailable (key_rejected)"),
        ("fail", (500,), 1, "FAIL model: openai, unavailable (server_error)"),
        ("drop", (), 1, "FAIL model: openai, unavailable (unreachable)"),
        ("fail", (404,), 1, "FAIL model: openai, rejected"),
    ):
        getattr(server, behaviour)(*args)
        assert cli.main(["check"]) == code, said
        seen = capsys.readouterr()
        assert seen.out.splitlines() == ["off airtrail", said, *CHECK_REST]
        for part in ("S3CRET", "alex", "models.example"):
            assert part not in seen.out + seen.err, said
    assert [(r["method"], r["path"]) for r in server.requests] == [("GET", "/v1/models")] * 6


def test_check_with_no_model_provider_says_the_model_is_off(env, monkeypatch, capsys):
    monkeypatch.setattr(anthropic, "Anthropic", refuse_to_build)
    monkeypatch.setattr(openai, "OpenAI", refuse_to_build)
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "placeholder")
    monkeypatch.setenv("TRIPBOX_LLM_PROVIDER", "none")
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines() == ["off airtrail", "off model", *CHECK_REST]


def flight_log_answers(monkeypatch, me):
    """The flight log answers its profile with `me` (a dict, or an AirTrailError to raise);
    every request it is sent is counted by its path."""
    from tripbox.write import airtrail

    asked: list[str] = []

    def answer(self):
        asked.append("/api/v1/me")
        if isinstance(me, Exception):
            raise me
        return me

    monkeypatch.setattr(airtrail.AirTrailClient, "me", answer)
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", "http://flightlog.example:3000")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_API_KEY", "S3CRET-FLIGHT-LOG-KEY")
    return asked


def me(scopes, permissions=(), owner=True):
    """The flight log's answer for the owner's key holding `scopes`."""
    return {
        "id": "sampleowner01",
        "isOwner": owner,
        "permissions": list(permissions),
        "credential": {"kind": "apiKey", "scopes": list(scopes)},
    }


def kept_key(env) -> dict:
    import json

    with Ledger.open(env / "data" / "ledger.db") as led:
        return json.loads(led.kv.get("airtrail_key"))


def test_check_names_each_scope_the_key_lacks(env, monkeypatch, capsys):
    from tripbox.write.airtrail import REQUIRED_SCOPES

    six = list(REQUIRED_SCOPES)
    lacking = [s for s in six if s not in ("flight.update.own", "flight.passengers.manage.own")]
    asked = flight_log_answers(monkeypatch, me(lacking))
    assert cli.main(["check"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "FAIL airtrail: the key lacks flight.update.own, flight.passengers.manage.own"
    assert out[1:] == ["off model", *CHECK_REST]
    assert asked == ["/api/v1/me"]
    kept = kept_key(env)
    assert kept["missing"] == ["flight.update.own", "flight.passengers.manage.own"]
    # what it stores binds nothing: the ledger is no user's yet
    with Ledger.open(env / "data" / "ledger.db") as led:
        assert not led.kv.get("airtrail_user")
    # the smallest key AirTrail's picker makes: the six and two more, said and no problem
    flight_log_answers(monkeypatch, me([*six, "flight.export.own", "flight.delete.own"]))
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "ok airtrail: answers; the key holds the six scopes; it can also: flight.delete.own,"
        " flight.export.own (AirTrail's scope picker adds them to the six)"
    )
    # a key that reaches further: a warning, and no failure
    flight_log_answers(monkeypatch, me([*six, "flight.read.any", "tracks.write"]))
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == [
        "ok airtrail: answers; the key holds the six scopes",
        "warn airtrail: the key can also flight.read.any, tracks.write; tripbox uses none of them"
        " — a narrower key is safer",
    ]
    # exactly the six
    flight_log_answers(monkeypatch, me(six))
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "ok airtrail: answers; the key holds the six scopes"
    )
    # an AirTrail that does not say what a key may do
    flight_log_answers(monkeypatch, {"id": "sampleowner01"})
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "ok airtrail: answers; this AirTrail does not say what the key may do"
    )


def test_check_stamps_every_look_and_fails_a_key_of_another_user(env, monkeypatch, capsys):
    import datetime as dt

    from tripbox import util
    from tripbox.write.airtrail import REQUIRED_SCOPES

    six = list(REQUIRED_SCOPES)
    flight_log_answers(monkeypatch, me(six))
    first, later = util.now(), util.now() + dt.timedelta(hours=1)
    with util.clock_at(first):
        assert cli.main(["check"]) == 0
    with util.clock_at(later):
        assert cli.main(["check"]) == 0
    capsys.readouterr()
    # the same answer twice: the time is that of the last look
    assert util.parse_iso(kept_key(env)["at"]) == later.replace(microsecond=0)
    # the ledger's flights were sent as another user: the key fails, and nothing of it is kept
    with Ledger.open(env / "data" / "ledger.db") as led:
        led.kv.set("airtrail_user", "another-user")
    flight_log_answers(monkeypatch, me(six[:2]))
    assert cli.main(["check"]) == 1
    assert capsys.readouterr().out.splitlines()[0] == (
        "FAIL airtrail: this key is another AirTrail user's than the one tripbox's flights were"
        " sent as (tripbox airtrail forget)"
    )
    assert kept_key(env)["missing"] == []
    with Ledger.open(env / "data" / "ledger.db") as led:
        assert led.kv.get("airtrail_user") == "another-user"


def test_check_tells_a_role_that_forbids_from_a_key_that_lacks(env, monkeypatch, capsys):
    import httpx2

    from tripbox.write.airtrail import REQUIRED_SCOPES, AirTrailError

    six = list(REQUIRED_SCOPES)
    reads = ["flight.read.own"]
    flight_log_answers(monkeypatch, me(six, permissions=reads, owner=False))
    assert cli.main(["check"]) == 1
    assert capsys.readouterr().out.splitlines()[0] == (
        "FAIL airtrail: your role there does not permit flight.create.own, flight.update.own,"
        " flight.passengers.manage.own"
    )
    for error, said in (
        (AirTrailError(401, "Unauthorized", code="unauthorized"), "refused the key (HTTP 401)"),
        (
            AirTrailError(
                403, "The credential requires profile.read", code="insufficient_scope"
            ),
            "the key lacks profile.read",
        ),
        (
            AirTrailError(503, "down"),
            "answered HTTP 503, which AirTrail does not for this request — is TRIPBOX_AIRTRAIL_URL"
            " the address of AirTrail itself?",
        ),
    ):
        flight_log_answers(monkeypatch, error)
        assert cli.main(["check"]) == 1
        assert capsys.readouterr().out.splitlines()[0] == f"FAIL airtrail: {said}"
    unreachable = AirTrailError(0, "ConnectError: refused")
    unreachable.__cause__ = httpx2.ConnectError("refused")
    flight_log_answers(monkeypatch, unreachable)
    assert cli.main(["check"]) == 1
    seen = capsys.readouterr()
    assert seen.out.splitlines()[0] == "FAIL airtrail: no answer (ConnectError)"
    assert "S3CRET" not in seen.out + seen.err


def test_check_tells_an_answer_that_is_not_airtrails_from_no_answer(env, monkeypatch, capsys):
    """Something answered, but not as AirTrail does: its status, or a body that is no JSON, says
    to look at the address; nothing answering names the error's class only."""
    import httpx2

    from tripbox.write.airtrail import AirTrailError

    is_it = "is TRIPBOX_AIRTRAIL_URL the address of AirTrail itself?"
    for error, said in (
        (
            AirTrailError(404, "Not found"),
            f"answered HTTP 404, which AirTrail does not for this request — {is_it}",
        ),
        (
            AirTrailError(0, "non-JSON response: '<html>'"),
            f"the address answers, but not as AirTrail does — {is_it}",
        ),
    ):
        flight_log_answers(monkeypatch, error)
        assert cli.main(["check"]) == 1
        assert capsys.readouterr().out.splitlines()[0] == f"FAIL airtrail: {said}"
    unreachable = AirTrailError(0, "ConnectError: refused")
    unreachable.__cause__ = httpx2.ConnectError("refused")
    flight_log_answers(monkeypatch, unreachable)
    assert cli.main(["check"]) == 1
    assert capsys.readouterr().out.splitlines()[0] == "FAIL airtrail: no answer (ConnectError)"


def test_a_refused_key_is_remembered_until_a_check_is_answered(env, monkeypatch, capsys):
    """A check the flight log refuses is kept in the ledger: `airtrail status` and `doctor` say
    so (doctor counts it, exit 1) until a check that is answered replaces it."""
    from tripbox.write.airtrail import REQUIRED_SCOPES, AirTrailError

    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_DRY_RUN", "false")
    flight_log_answers(monkeypatch, AirTrailError(401, "Unauthorized", code="unauthorized"))
    assert cli.main(["check"]) == 1
    capsys.readouterr()
    assert set(kept_key(env)) == {"at", "refused"} and kept_key(env)["refused"] == 401
    assert cli.main(["airtrail", "status"]) == 0
    refused = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("key:")]
    assert len(refused) == 1 and refused[0].startswith("key: refused (HTTP 401), checked ")
    assert refused[0].endswith(" UTC")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert any(refused[0] in line for line in out)
    shown = refused[0].removeprefix("key: refused (HTTP 401), checked ").removesuffix(" UTC")
    assert out[out.index("problems: 1") + 1 :] == [
        f"TRIPBOX_AIRTRAIL_API_KEY: AirTrail refused the key (checked {shown} UTC) — in AirTrail,"
        " Settings → Security, make a new key with the six scopes, then run tripbox check"
    ]
    flight_log_answers(monkeypatch, me(list(REQUIRED_SCOPES)))
    assert cli.main(["check"]) == 0
    capsys.readouterr()
    assert "refused" not in kept_key(env)
    assert cli.main(["airtrail", "status"]) == 0
    assert "key: the six scopes" in capsys.readouterr().out
    assert cli.main(["doctor"]) == 0


def test_check_speaks_one_vocabulary_and_says_invalid_for_an_invalid_feature(
    env, monkeypatch, capsys
):
    monkeypatch.setattr(anthropic, "Anthropic", refuse_to_build)
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "placeholder")
    monkeypatch.setenv("TRIPBOX_LLM_BASE_URL", "http://models.example/v1")  # anthropic takes none
    monkeypatch.setenv("TRIPBOX_TELEGRAM_BOT_TOKEN", "S3CRET-BOT")  # half configured
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_DRY_RUN", "false")
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "off airtrail",
        "invalid model: tripbox doctor says why",
        "off enrichment",
        "invalid notifications: tripbox doctor says why",
        "dry run: off",
    ]
    for line in out:
        assert line.split(" ", 1)[0] in ("ok", "FAIL", "warn", "off", "invalid", "dry"), line


def test_check_with_ntfy_alone_sends_nothing(env, monkeypatch, capsys):
    import httpx2

    def nothing_sent(*args, **kwargs):
        raise AssertionError("a request was made")

    monkeypatch.setattr(httpx2, "get", nothing_sent)
    monkeypatch.setattr(httpx2, "post", nothing_sent)
    monkeypatch.setattr(httpx2.Client, "send", nothing_sent)
    monkeypatch.setenv("TRIPBOX_NTFY_URL", "https://ntfy.example/S3CRET-TOPIC")
    assert cli.main(["check"]) == 0
    seen = capsys.readouterr()
    assert seen.out.splitlines() == [
        "off airtrail",
        "off model",
        "off enrichment",
        "ok notifications: ntfy is set; nothing is sent to check it",
        "dry run: on (flights are filed; nothing is sent to AirTrail)",
    ]
    assert "S3CRET" not in seen.out + seen.err


def test_selfcheck_is_no_command(env, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["selfcheck"])
    assert stopped.value.code == 2
    assert "invalid choice: 'selfcheck'" in capsys.readouterr().err
    assert "selfcheck" not in cli.build_parser().format_help()


def test_fixtures_from_archive_is_no_command(env, capsys):
    """Saved mail is never copied into the tree: what is sent of an email that was read wrongly
    is `tripbox report`'s redacted copy."""
    with pytest.raises(SystemExit) as stopped:
        cli.main(["fixtures-from-archive"])
    assert stopped.value.code == 2
    said = capsys.readouterr().err
    assert said.startswith("usage: tripbox ")
    assert "invalid choice: 'fixtures-from-archive'" in said
    listed = cli.build_parser().format_help()
    assert "fixtures" not in listed and "report" in listed


def test_work_needs_no_mail_and_writes_its_own_log(env, monkeypatch):
    """The worker is a long-lived process: it writes its own log file, as the bot and the page
    do. Mail off stops nothing (the `poll` it starts still runs the rest of a poll)."""
    from tripbox import worker

    names, ran = [], []
    monkeypatch.setattr(cli, "setup_logging", lambda logs_dir, name="tripbox": names.append(name))
    monkeypatch.setattr(worker.Worker, "start", lambda self: ran.append("start"))
    monkeypatch.setattr(worker.Worker, "run", lambda self: ran.append("run") or 0)
    assert not Settings.from_env().on("mail")
    assert cli.main(["work"]) == 0
    assert names == ["tripbox-worker"] and ran == ["start", "run"]


def test_a_message_that_waits_is_printed_with_why_in_the_words_the_owner_is_told(capsys):
    """`ingest` and `replay` print why a message waits for the model as the bot's answer says
    it: for want of one, for its budget, or while it cannot be reached."""
    said = {
        "no_model": "No model is set up, so it waits until one is.",
        "over_budget": "The model's budget is used; it is read when the budget allows.",
        "model_down": "The model cannot be reached; it is tried again every few minutes.",
    }
    assert set(said) == set(runmod.WAIT_REASONS)
    for reason, why in said.items():
        res = runmod.MessageResult(7, "waiting", None, wait_reason=reason)
        cli._print_result(res, dry_run=False)
        assert capsys.readouterr().out == f"message #7: waiting. {why}\n", reason
    cli._print_result(runmod.MessageResult(7, "processed", "tripbox/processed"), dry_run=True)
    assert capsys.readouterr().out == "message #7: processed [dry-run]\n"


def test_there_is_no_review_command(env, capsys):
    """Nothing is held for review: a booking is filed with its checks, and the bot's `/check`
    and the page are where they are settled."""
    with pytest.raises(SystemExit) as stopped:
        cli.main(["review", "list"])
    assert stopped.value.code == 2
    assert "invalid choice: 'review'" in capsys.readouterr().err
    assert "review" not in cli.build_parser().format_help()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a folder whatever its mode")
def test_web_work_and_bot_refuse_to_start_on_a_folder_they_cannot_write(env, monkeypatch, capsys):
    """A folder a role needs that it cannot write stops it before it starts, in the words
    `doctor` uses: `web` checks data and config, `work` and `bot` data, archive and config."""
    from tripbox import worker

    served, ran = [], []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setattr(worker.Worker, "start", lambda self: ran.append("start"))
    monkeypatch.setattr(runmod, "build_context", lambda *args, **kwargs: ran.append("context"))
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_TELEGRAM_BOT_TOKEN", "placeholder")
    monkeypatch.setenv("TRIPBOX_TELEGRAM_CHAT_ID", "1")
    data, archive = env / "data", env / "archive"
    data.mkdir(mode=0o555)
    who = f"uid {os.getuid()}, gid {os.getgid()}; it belongs to uid {os.getuid()}"
    line = (
        f"config error: TRIPBOX_DATA_DIR: {data} is not writable by this user ({who},"
        f" gid {os.getgid()}, mode 555) — on the host run: chmod u+rwx {data}"
        " (a read-only mount or an SELinux label also does this)"
    )
    try:
        for command in ("web", "work", "bot"):
            assert cli.main([command]) == 2, command
            assert capsys.readouterr().err.splitlines() == [line], command
        # the archive matters to the two that read mail, never to the page
        data.chmod(0o755)
        archive.write_text("")
        for command in ("work", "bot"):
            assert cli.main([command]) == 2, command
            assert capsys.readouterr().err.splitlines() == [
                f"config error: TRIPBOX_ARCHIVE_DIR: {archive} is not a folder"
                " — move it away and make a folder there"
            ], command
        assert served == [] and ran == []
        assert cli.main(["web"]) == 0 and len(served) == 1
    finally:
        data.chmod(0o755)


def test_a_ledger_that_cannot_be_opened_is_one_line_not_a_traceback(env, capsys):
    """A command that is not checked at start meets the ledger as SQLite does: a file it cannot
    open, or write, is one line that points at `doctor`."""
    said = 'ledger: the ledger cannot be opened or written; "tripbox doctor" says why'
    ledger = env / "data" / "ledger.db"
    ledger.mkdir(parents=True)  # a folder where the file goes: SQLite cannot open it
    assert cli.main(["upcoming"]) == 2
    seen = capsys.readouterr()
    assert seen.err.splitlines() == [said] and seen.out == ""
    if os.geteuid() != 0:
        ledger.rmdir()
        with Ledger.open(ledger):
            pass
        ledger.chmod(0o444)
        try:
            assert cli.main(["upcoming"]) == 2
        finally:
            ledger.chmod(0o644)
        seen = capsys.readouterr()
        assert seen.err.splitlines() == [said] and seen.out == ""


def test_passwd_ended_at_the_prompt_says_so(env, monkeypatch, capsys):
    """Ctrl-D at either prompt ends `passwd` with one line, never a traceback."""
    import getpass
    import sys

    from test_passwords import FAKE, Terminal

    for typed in ([], [FAKE]):
        answers = iter(typed)

        def ended(prompt: str = "", stream=None, answers=answers) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr(sys, "stdin", Terminal())
        monkeypatch.setattr(getpass, "getpass", ended)
        assert cli.main(["passwd"]) == 2, typed
        seen = capsys.readouterr()
        assert (seen.out, seen.err) == ("", "passwd: no password given\n"), typed


def test_poll_tells_a_failed_message_from_a_failed_mailbox(env, monkeypatch, capsys):
    """`poll` ends 3 when the mailbox was read (or there is none) and a message failed, and 1
    when the mailbox could not be read."""
    failing = SimpleNamespace(fetched=1, counts={"failed": 1}, errors=1, seconds=0.0)
    monkeypatch.setattr(runmod, "poll", lambda ctx, inbox: failing)
    assert cli.main(["poll"]) == 3  # no mailbox; a message waiting to be read again failed
    monkeypatch.setenv("TRIPBOX_MAIL_HOST", "imap.trips.example")
    monkeypatch.setenv("TRIPBOX_MAIL_USER", "trips@example.com")
    monkeypatch.setenv("TRIPBOX_MAIL_PASSWORD", "placeholder")
    monkeypatch.setenv("TRIPBOX_MAIL_ALLOWED_SENDERS", "alex@example.com")
    monkeypatch.setenv("TRIPBOX_MAIL_TRUST", "mailbox")

    class Inbox:
        def __init__(self, *args) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(imapmod, "ImapInbox", Inbox)
    assert cli.main(["poll"]) == 3
    clean = SimpleNamespace(fetched=1, counts={"filed": 1}, errors=0, seconds=0.0)
    monkeypatch.setattr(runmod, "poll", lambda ctx, inbox: clean)
    assert cli.main(["poll"]) == 0

    def refused(*args) -> None:
        raise OSError("the mailbox refused the login")

    monkeypatch.setattr(imapmod, "ImapInbox", refused)
    assert cli.main(["poll"]) == 1
    capsys.readouterr()
