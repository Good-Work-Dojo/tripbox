# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox doctor`: where the installation stands, from the settings alone, and the check that
the registry, docs/configuration.md and .env.example describe the same settings (the example is
what `config.env_example()` writes)."""

import datetime as dt
import json
import os
import re
from pathlib import Path

import pytest
from helpers.sample import CONFIG_DIR

from tripbox import cli, status, util
from tripbox.config import (
    _CHECKS,
    COMPOSE_ONLY,
    IMAGE_ENV,
    KEY_BY_NAME,
    KEYS,
    PATH_KEYS,
    env_example,
)
from tripbox.evalrun import read_env_file
from tripbox.ledger import Ledger
from tripbox.trips.store import stamp

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "configuration.md"
EXAMPLE = ROOT / ".env.example"
TOKEN = re.compile(r"TRIPBOX_[A-Z0-9_]*[A-Z0-9]")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Nothing configured but where the files would go."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    return tmp_path


NOT_CHECKED = "key: not checked yet (tripbox check asks AirTrail)"


def row(name: str, state: str, hint: str = "") -> str:
    return f"{name:<15}{state:<9}{hint}".rstrip()


# What doctor says of the rest of a new install, each optional feature off with its hint
REST = [
    row(
        "mail",
        "off",
        "set TRIPBOX_MAIL_HOST, TRIPBOX_MAIL_USER, TRIPBOX_MAIL_PASSWORD and"
        " TRIPBOX_MAIL_ALLOWED_SENDERS to read a mailbox",
    ),
    row(
        "model",
        "off",
        "only emails carrying structured data are read; set TRIPBOX_LLM_API_KEY: mail that"
        " needs a model waits and is read once one is set up",
    ),
    row(
        "airtrail",
        "off",
        "flights go to trips only; set TRIPBOX_AIRTRAIL_URL and TRIPBOX_AIRTRAIL_API_KEY"
        " to keep a flight log",
    ),
    row(
        "notifications",
        "off",
        "set TRIPBOX_TELEGRAM_BOT_TOKEN and TRIPBOX_TELEGRAM_CHAT_ID, or TRIPBOX_NTFY_URL,"
        " to get messages",
    ),
    row(
        "enrichment",
        "off",
        "set TRIPBOX_ENRICH=aeroapi and TRIPBOX_AEROAPI_KEY to fill in flight details in AirTrail",
    ),
    row(
        "travellers",
        "none",
        "travellers.yaml is absent: nobody is the owner, so no flight reaches AirTrail",
    ),
    row("lists", "shipped"),
    row("files", "ok", f"uid {os.getuid()}, gid {os.getgid()} · config"),
    row("worker", "none", "no ledger yet: the first start makes it"),
]


def test_doctor_on_a_new_install_says_what_to_do_next(env, monkeypatch, capsys):
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "tripbox doctor",
        row("core", "on"),
        row("web", "on", "login: none (this computer only)"),
        *REST,
        "problems: 0",
    ]
    assert not any(line.startswith("dry run") for line in out)


def test_doctor_on_a_new_install_names_the_password_as_the_one_thing_to_set(
    env, monkeypatch, capsys
):
    """Nothing set at all: the page's password is the one problem, and its sentence says what to
    do; set, the page's row names the login and nothing shows the password."""
    assert cli.main(["doctor"]) == 1
    seen = capsys.readouterr()
    assert seen.out.splitlines() == [
        "tripbox doctor",
        row("core", "on"),
        row("web", "invalid", "see the problems below"),
        *REST,
        "problems: 1",
        "TRIPBOX_PASSWORD: not set — choose a password of 10 characters or more, or set"
        ' TRIPBOX_PASSWORD_HASH to the line "tripbox passwd" prints',
    ]
    monkeypatch.setenv("TRIPBOX_PASSWORD", "S3CRET-PASSWORD-MARK")
    assert cli.main(["doctor"]) == 0
    seen = capsys.readouterr()
    assert row("web", "on", "login: password") in seen.out.splitlines()
    assert "problems: 0" in seen.out.splitlines()
    assert "S3CRET" not in seen.out + seen.err


def test_doctor_says_what_the_dry_run_holds_back_only_when_airtrail_is_on(
    env, monkeypatch, capsys
):
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", "http://flightlog.invalid")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_API_KEY", "k")
    hint = (
        "dry run: flights are filed but not sent;"
        " set TRIPBOX_AIRTRAIL_DRY_RUN=false to send them; " + NOT_CHECKED
    )
    assert cli.main(["doctor"]) == 0
    assert row("airtrail", "on", hint) in capsys.readouterr().out.splitlines()
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_DRY_RUN", "false")
    assert cli.main(["doctor"]) == 0
    assert row("airtrail", "on", NOT_CHECKED) in capsys.readouterr().out.splitlines()


def test_doctor_shows_a_bad_flight_log_address_while_airtrail_stays_on(env, monkeypatch, capsys):
    """The flight log's address is only a link: a bad one is a problem doctor lists (and exits
    1 for, as for any problem), while AirTrail's row stays on with its usual hint."""
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", "http://flightlog.invalid")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_API_KEY", "k")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_DRY_RUN", "false")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_PUBLIC_URL", "flightlog.example")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert row("airtrail", "on", NOT_CHECKED) in out
    assert out[out.index("problems: 1") + 1 :] == [
        "TRIPBOX_AIRTRAIL_PUBLIC_URL: not an http:// or https:// address with a host"
        " — use the address you open your flight log at, such as https://flightlog.example"
    ]


def test_doctor_lists_every_problem_and_exits_1(env, monkeypatch, capsys):
    for name, value in {
        "TZ": "Mars/Base",
        "TRIPBOX_AIRTRAIL_DRY_RUN": "ture",
        "TRIPBOX_MAIL_HOST": "imap.gmail.com",
        "TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN": "ten",
        "TRIPBOX_AUTH": "none",
    }.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "problems: 6" in out
    fix = "mail is half configured; set it, or remove TRIPBOX_MAIL_HOST"
    assert out[out.index("problems: 6") + 1 :] == [
        "TZ: unknown time zone 'Mars/Base' — use an IANA name such as America/Chicago",
        f"TRIPBOX_MAIL_USER: not set — {fix}",
        f"TRIPBOX_MAIL_PASSWORD: not set — {fix}",
        f"TRIPBOX_MAIL_ALLOWED_SENDERS: not set — {fix}",
        "TRIPBOX_AIRTRAIL_DRY_RUN: 'ture' is not true or false"
        " — use true/false, 1/0, yes/no or on/off",
        "TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN: 'ten' is not a whole number of 1 or more"
        " — use a whole number such as 10",
    ]
    for name in ("core", "mail", "airtrail", "enrichment"):
        assert row(name, "invalid", "see the problems below") in out


def test_doctor_never_prints_a_secret_or_an_unknown_names_value(env, monkeypatch, capsys):
    markers = []
    for n, key in enumerate(k for k in KEYS if k.secret):
        markers.append(f"S3CRET-{n}")
        monkeypatch.setenv(key.name, markers[-1])
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", "")
    monkeypatch.setenv("TRIPBOX_TELEGRAM_BOTTOKEN", "S3CRET-X")
    markers.append("S3CRET-X")
    assert cli.main(["doctor"]) == 1
    seen = capsys.readouterr()
    for marker in markers:
        assert marker not in seen.out and marker not in seen.err
    assert "TRIPBOX_TELEGRAM_BOTTOKEN: not a tripbox setting" in seen.out


def test_doctor_never_prints_the_model_servers_address(env, monkeypatch, capsys):
    """The address of an OpenAI-compatible server may hold a login: doctor shows none of it,
    whether it is taken, not an address, or set for a provider that takes none (its host is not
    the one the settings' own example names)."""
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_LLM_PROVIDER", "openai")
    monkeypatch.setenv("TRIPBOX_LLM_MODEL", "served-model")
    for address, provider, code in (
        ("https://alex:S3CRET-MARK@inference.example/v1", "openai", 0),
        ("ftp://alex:S3CRET-MARK@inference.example", "openai", 1),
        ("https://alex:S3CRET-MARK@inference.example/v1", "anthropic", 1),
    ):
        monkeypatch.setenv("TRIPBOX_LLM_BASE_URL", address)
        monkeypatch.setenv("TRIPBOX_LLM_PROVIDER", provider)
        assert cli.main(["doctor"]) == code, (address, provider)
        seen = capsys.readouterr()
        invalid = row("model", "invalid", "see the problems below")
        on = row("model", "on", "openai · served-model · no known price: at most 100 calls a day")
        model = on if code == 0 else invalid
        assert model in seen.out.splitlines()
        for part in ("S3CRET", "alex", "inference.example"):
            assert part not in seen.out and part not in seen.err


def test_doctor_names_the_model_and_its_cap(env, monkeypatch, capsys):
    """With the model on, its row names the provider, the model and the cap it is asked under:
    the monthly budget of a model whose price is known, else the calls it may make a day."""
    default = KEY_BY_NAME["TRIPBOX_LLM_MODEL"].default

    def model_row() -> str:
        assert cli.main(["doctor"]) == 0
        (line,) = [x for x in capsys.readouterr().out.splitlines() if x.startswith("model ")]
        return line

    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "placeholder")
    assert model_row() == row("model", "on", f"anthropic · {default} · budget $5.00/month")
    monkeypatch.setenv("TRIPBOX_LLM_MONTHLY_BUDGET_USD", "12.5")
    assert model_row() == row("model", "on", f"anthropic · {default} · budget $12.50/month")
    monkeypatch.setenv("TRIPBOX_LLM_MODEL", "served-model")
    monkeypatch.setenv("TRIPBOX_LLM_MAX_CALLS_PER_DAY", "7")
    unpriced = "anthropic · served-model · no known price: at most 7 calls a day"
    assert model_row() == row("model", "on", unpriced)
    monkeypatch.setenv("TRIPBOX_LLM_PRICE_USD_PER_MTOK", "1,2")
    assert model_row() == row("model", "on", "anthropic · served-model · budget $12.50/month")


def undocumented(doc: str) -> list[str]:
    """The registered keys that have no table row of their own in the document."""
    return [k.name for k in KEYS if not re.search(rf"^\| `{k.name}` \|", doc, re.M)]


def test_every_registered_key_is_documented_and_every_documented_key_is_registered():
    """Every key has its row, and every one a settings file holds is in the example; the
    folders are the image's, documented apart. The compose file's own names have their rows and
    example lines too, and are the only names beside the registry's."""
    doc, example = DOC.read_text(), EXAMPLE.read_text()
    assert undocumented(doc) == []
    composed = [name for name, _, _ in COMPOSE_ONLY]
    for name in composed:
        assert re.search(rf"^\| `{name}` \|", doc, re.M), name
        assert re.search(rf"^# {name}=", example, re.M), name
    for key in KEYS:
        if key.feature != "dev" and key.name not in (*PATH_KEYS, IMAGE_ENV):
            assert re.search(rf"^#?\s*{key.name}=", example, re.M), key.name
    known = {k.name for k in KEYS} | set(composed)
    assert set(TOKEN.findall(doc)) <= known
    assert set(TOKEN.findall(example)) <= known


def test_a_key_mentioned_in_prose_but_missing_its_table_row_is_reported():
    doc = DOC.read_text()
    row_start = doc.index("| `TRIPBOX_LOGS_DIR` |")
    row_end = doc.index("\n", row_start) + 1
    without_row = doc[:row_start] + doc[row_end:] + "\nProse naming `TRIPBOX_LOGS_DIR`.\n"
    assert undocumented(without_row) == ["TRIPBOX_LOGS_DIR"]


def test_doctor_repeats_what_the_last_check_found(env, monkeypatch, capsys):
    """What `tripbox check` (or a sync pass) last found of the key, read from the ledger without
    writing: the row's hint repeats it, and a key short of a scope is a problem of its own,
    counted (exit 1), while AirTrail stays on. With nothing stored: not checked yet, exit 0."""
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", "http://flightlog.invalid")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_API_KEY", "k")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_DRY_RUN", "false")
    data = env / "data"
    with Ledger.open(data / "ledger.db"):
        pass
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert row("airtrail", "on", NOT_CHECKED) in out and "problems: 0" in out

    # when the key was last checked, as the ledger keeps it and as doctor shows it
    checked = dt.datetime.now(dt.UTC).replace(second=6, microsecond=0)
    shown = checked.strftime("%Y-%m-%d %H:%M")

    def kept(**found) -> None:
        at = checked.strftime("%Y-%m-%dT%H:%M:%SZ")
        note = {"at": at, "kind": "apiKey", "missing": [], "denied": []}
        with Ledger.open(data / "ledger.db") as led:
            led.kv.set("airtrail_key", json.dumps({**note, "more": [], **found}))

    kept(missing=["flight.passengers.manage.own"])
    before = sorted(os.listdir(data))
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    lacks = f"key: lacks flight.passengers.manage.own, checked {shown} UTC"
    assert row("airtrail", "on", lacks) in out
    assert out[out.index("problems: 1") + 1 :] == [
        "TRIPBOX_AIRTRAIL_API_KEY: the key lacks flight.passengers.manage.own (checked"
        f" {shown} UTC) — in AirTrail, Settings → Security, make a key with"
        " profile.read, reference_data.read, flight.read.own, flight.create.own,"
        " flight.update.own and flight.passengers.manage.own, then run tripbox check"
    ]
    assert sorted(os.listdir(data)) == before
    # the two scopes AirTrail's picker adds are no problem; more than that is said, not counted
    kept(more=["flight.delete.own", "flight.export.own"])
    assert cli.main(["doctor"]) == 0
    six = f"key: the six scopes, checked {shown} UTC"
    assert row("airtrail", "on", six) in capsys.readouterr().out.splitlines()
    kept(more=["flight.delete.own", "flight.read.any"])
    assert cli.main(["doctor"]) == 0
    wider = (
        f"{six}; it may also flight.read.any; tripbox uses none of them — a narrower key is safer"
    )
    assert row("airtrail", "on", wider) in capsys.readouterr().out.splitlines()
    # what the role denies is a problem too
    kept(denied=["flight.create.own"])
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("problems: 1") + 1 :] == [
        "TRIPBOX_AIRTRAIL_API_KEY: your role in AirTrail does not permit flight.create.own"
        f" (checked {shown} UTC) — ask the AirTrail instance's owner for a role that"
        " permits it, then run tripbox check"
    ]
    # with AirTrail off, nothing of the key is said
    monkeypatch.delenv("TRIPBOX_AIRTRAIL_URL")
    monkeypatch.delenv("TRIPBOX_AIRTRAIL_API_KEY")
    assert cli.main(["doctor"]) == 0


def test_doctor_touches_no_directory(env, monkeypatch):
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    cli.main(["doctor"])
    assert not (env / "data").exists() and not (env / "logs").exists()
    # a config directory that does not exist is read as one without a travellers file
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(env / "nowhere"))
    assert cli.main(["doctor"]) == 0
    assert not (env / "nowhere").exists()


def test_doctor_lists_the_folders_and_the_workers_record(env, monkeypatch, capsys):
    """The folders that are there and who this process is; the worker's record, read as the
    health check reads it, through a connection that writes nothing."""
    monkeypatch.setenv("TRIPBOX_AUTH", "none")

    def rows() -> list[str]:
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out.splitlines()
        return out[out.index(row("lists", "shipped")) + 1 : out.index("problems: 0")]

    ids = f"uid {os.getuid()}, gid {os.getgid()}"
    data = env / "data"
    data.mkdir()
    (env / "archive").mkdir()
    assert rows() == [
        row("files", "ok", f"{ids} · data, archive, config"),
        row("worker", "none", "no ledger yet: the first start makes it"),
    ]
    with Ledger.open(data / "ledger.db"):
        pass
    before = sorted(os.listdir(data))
    assert rows()[1] == row("worker", "none", "no worker has run on this ledger")
    at = dt.datetime.now(dt.UTC).replace(microsecond=0)

    def record(**fields) -> None:
        with Ledger.open(data / "ledger.db") as led:
            text = {"pid": 1, "host": "h", "beat_at": stamp(at), "stopped_at": None, **fields}
            led.kv.set(status.KV_WORKER, json.dumps(text))

    record(busy=False, last={"poll": None})
    with util.clock_at(at + dt.timedelta(seconds=3)):
        assert rows()[1] == row("worker", "running", "beat 3 s ago")
    record(busy=True, last={"poll": {"at": stamp(at), "ok": False, "code": "message_failed"}})
    with util.clock_at(at + dt.timedelta(seconds=40)):
        running = "beat 40 s ago, busy; last poll: message_failed"
        assert rows()[1] == row("worker", "running", running)
    with util.clock_at(at + dt.timedelta(seconds=status.BUSY_SECONDS)):
        assert rows()[1] == row("worker", "stopped", f"last beat {stamp(at)}")
    later = stamp(at + dt.timedelta(minutes=1))
    record(stopped_at=later)
    with util.clock_at(at + dt.timedelta(minutes=2)):
        assert rows()[1] == row("worker", "stopped", f"since {later}")
    assert sorted(os.listdir(data)) == before  # nothing made beside the ledger
    (data / "ledger.db").write_bytes(b"not a ledger")
    assert rows()[1] == row("worker", "unknown", "the ledger could not be read")
    assert sorted(os.listdir(data)) == before


def test_doctor_prints_the_workers_times_only_as_times(env, monkeypatch, capsys):
    """The worker's record is a row the page container can write: a stop or beat time that reads
    as no time is shown as unknown, never as written, so nothing in it reaches the terminal."""
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    data = env / "data"
    data.mkdir()
    (env / "archive").mkdir()
    for stopped, beat, shown in (
        ("\x1b[2Jgone", "\x1b[2Jgone", "since unknown"),
        ({"at": 1}, None, "since unknown"),
        (None, "\x1b[2Jgone", "last beat unknown"),
        (None, 12, "last beat unknown"),
        (None, None, "last beat unknown"),
    ):
        with Ledger.open(data / "ledger.db") as led:
            text = {"pid": 1, "host": "h", "beat_at": beat, "stopped_at": stopped}
            led.kv.set(status.KV_WORKER, json.dumps(text))
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert row("worker", "stopped", shown) in out.splitlines(), (stopped, beat)
        assert "\x1b" not in out


def test_doctor_shows_the_list_overrides(env, monkeypatch, capsys):
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    (env / "config" / "us_states.txt").write_text("Guam\n")
    assert cli.main(["doctor"]) == 0
    assert row("lists", "shipped + us_states.txt") in capsys.readouterr().out.splitlines()
    (env / "config" / "airlines.yaml").write_text("name: x\n")
    assert cli.main(["doctor"]) == 1
    assert row("lists", "invalid", "see the problems below") in capsys.readouterr().out.splitlines()


def test_the_example_settings_file_is_what_the_registry_writes():
    """.env.example is made from the registry, never by hand. After a change to a key, write it
    again in the test image: python -c 'from tripbox.config import env_example;
    print(env_example(), end="")' > .env.example"""
    assert EXAMPLE.read_bytes() == env_example().encode()


def test_the_example_alone_as_settings_has_one_problem_the_password(env, monkeypatch, capsys):
    """Copied as it is, the example sets one line, the page's empty password, and that is the
    one thing to fix; every example it gives passes its key's own check."""
    settings = read_env_file(EXAMPLE)
    assert settings == {"TRIPBOX_PASSWORD": ""}
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("problems: 1") + 1 :] == [
        "TRIPBOX_PASSWORD: not set — choose a password of 10 characters or more, or set"
        ' TRIPBOX_PASSWORD_HASH to the line "tripbox passwd" prints'
    ]
    for key in KEYS:
        if key.example:
            assert not key.secret or key.name in ("TRIPBOX_LLM_BASE_URL", "TRIPBOX_NTFY_URL")
            if key.field in _CHECKS:  # a key with no check takes any value
                _CHECKS[key.field].check(key.example)  # raises for a value its check refuses
        if key.feature != "dev" and key.name not in (*PATH_KEYS, IMAGE_ENV):
            assert key.note and len(key.note) <= 100 and key.note.endswith("."), key.name


def test_the_example_names_no_folder_and_no_dev_key():
    """The folders are where the image puts them, the image marks itself, and the development
    switches are for tests: none is in the example, commented or not."""
    example = EXAMPLE.read_text()
    assert IMAGE_ENV not in example and KEY_BY_NAME[IMAGE_ENV].feature == "core"
    assert len(PATH_KEYS) == 5
    for name in PATH_KEYS:
        assert KEY_BY_NAME[name].feature == "core" and name.endswith("_DIR"), name
    for key in KEYS:
        if key.feature == "dev" or key.name in PATH_KEYS:
            assert key.name not in example, key.name
    assert example.count("\nTRIPBOX_PASSWORD=\n") == 1
    live = [ln for ln in example.splitlines() if ln and not ln.startswith("#")]
    assert live == ["TRIPBOX_PASSWORD="]
