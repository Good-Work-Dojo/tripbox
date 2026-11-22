# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import dataclasses
import os
import re
from pathlib import Path

import pytest
from helpers.sample import CONFIG_DIR

from tripbox.config import FEATURES, KEY_BY_NAME, Settings

BASE = {
    "TRIPBOX_MAIL_HOST": "imap.gmail.com",
    "TRIPBOX_MAIL_USER": "x@example.com",
    "TRIPBOX_MAIL_PASSWORD": "abcd efgh ijkl mnop",
    "TRIPBOX_MAIL_ALLOWED_SENDERS": "alex@example.com",
    "TRIPBOX_AIRTRAIL_URL": "http://airtrail:3000/",
    "TRIPBOX_AIRTRAIL_API_KEY": "k",
    "TRIPBOX_LLM_API_KEY": "a",
    "TRIPBOX_ENRICH": "aeroapi",
    "TRIPBOX_AEROAPI_KEY": "f",
    "TRIPBOX_TELEGRAM_BOT_TOKEN": "t",
    "TRIPBOX_TELEGRAM_CHAT_ID": "1",
    "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
}
WORKER_FEATURES = ("mail", "model", "airtrail", "notifications", "enrichment")
AIRTRAIL = {"TRIPBOX_AIRTRAIL_URL": "http://flightlog.invalid", "TRIPBOX_AIRTRAIL_API_KEY": "k"}
NO_OWNER = (
    "travellers.yaml: AirTrail is set up but no owner is named — add owner: names: so tripbox"
    " knows which flights are yours"
)
CLOUDFLARE_KEYS = ["TRIPBOX_ACCESS_TEAM", "TRIPBOX_ACCESS_AUD", "TRIPBOX_ALLOWED_EMAILS"]
PASSWORD_NOT_SET = (
    "TRIPBOX_PASSWORD: not set — choose a password of 10 characters or more, or set"
    ' TRIPBOX_PASSWORD_HASH to the line "tripbox passwd" prints'
)


def keys(problems) -> list[str]:
    return [p.key for p in problems]


def test_an_empty_environment_has_every_worker_feature_off():
    s = Settings.from_env({})
    assert [f.name for f in s.features] == list(FEATURES)
    assert s.feature("core").state == "on" and s.on("core")
    for name in WORKER_FEATURES:
        assert s.feature(name).state == "off" and not s.on(name), name
    web = s.feature("web")
    assert web.state == "invalid" and keys(web.problems) == ["TRIPBOX_PASSWORD"]
    assert [str(p) for p in s.problems] == [PASSWORD_NOT_SET]
    s = Settings.from_env({"TRIPBOX_AUTH": "cloudflare"})
    assert keys(s.problems) == CLOUDFLARE_KEYS
    assert str(s.problems[0]) == "TRIPBOX_ACCESS_TEAM: not set — TRIPBOX_AUTH=cloudflare needs it"
    s = Settings.from_env({"TRIPBOX_AUTH": "none"})
    assert s.problems == () and s.on("web")


def test_every_problem_is_collected_not_just_the_first():
    s = Settings.from_env(
        {
            "TZ": "Mars/Base",
            "TRIPBOX_AIRTRAIL_DRY_RUN": "ture",
            "TRIPBOX_MAIL_HOST": "imap.gmail.com",
            "TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN": "ten",
            "TRIPBOX_AUTH": "none",
        }
    )
    assert keys(s.problems) == [
        "TZ",
        "TRIPBOX_MAIL_USER",
        "TRIPBOX_MAIL_PASSWORD",
        "TRIPBOX_MAIL_ALLOWED_SENDERS",
        "TRIPBOX_AIRTRAIL_DRY_RUN",
        "TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN",
    ]
    for p in s.problems:
        assert re.fullmatch(r"[A-Za-z_./]+: .+ — .+", str(p)), str(p)
    assert str(s.problems[0]) == (
        "TZ: unknown time zone 'Mars/Base' — use an IANA name such as America/Chicago"
    )
    assert str(s.problems[1]) == (
        "TRIPBOX_MAIL_USER: not set — mail is half configured; set it, or remove TRIPBOX_MAIL_HOST"
    )
    assert str(s.problems[4]) == (
        "TRIPBOX_AIRTRAIL_DRY_RUN: 'ture' is not true or false"
        " — use true/false, 1/0, yes/no or on/off"
    )
    states = {f.name: f.state for f in s.features}
    assert states == {
        "core": "invalid",
        "web": "on",
        "mail": "invalid",
        "model": "off",
        "airtrail": "invalid",
        "notifications": "off",
        "enrichment": "invalid",
    }
    # a bad value takes the key's default
    assert s.tz == "UTC" and s.airtrail_dry_run is True and s.aeroapi_max_calls_per_run == 10


def test_a_mistyped_setting_name_is_a_problem_that_suggests_the_real_one():
    base = {"TRIPBOX_AUTH": "none"}
    features = Settings.from_env(base).features
    s = Settings.from_env({**base, "TRIPBOX_AIRTAIL_URL": "http://flightlog.invalid"})
    assert [str(p) for p in s.problems] == [
        "TRIPBOX_AIRTAIL_URL: not a tripbox setting — did you mean TRIPBOX_AIRTRAIL_URL?"
    ]
    assert s.problems[0].feature == "names" and s.features == features
    s = Settings.from_env({**base, "TRIPBOX_ZZZ": "1"})
    [p] = s.problems
    assert (p.key, p.what, p.fix) == (
        "TRIPBOX_ZZZ",
        "not a tripbox setting",
        "check the spelling against docs/configuration.md",
    )
    assert s.features == features
    s = Settings.from_env({**base, "TRIPBOXOSS_PORT": "8110"})
    assert s.problems == () and s.features == features
    # unknown names come after every registered key's problem
    s = Settings.from_env({"TRIPBOX_ZZZ": "1", "TRIPBOX_LISTEN": "ten"})
    assert keys(s.problems)[-1] == "TRIPBOX_ZZZ"


def test_notifications_are_on_with_telegram_or_ntfy_and_invalid_when_half_set():
    from tripbox.config import KEYS

    telegram = {"TRIPBOX_TELEGRAM_BOT_TOKEN": "t", "TRIPBOX_TELEGRAM_CHAT_ID": "1"}
    address = {"TRIPBOX_NTFY_URL": "https://ntfy.example/trips/"}
    token = {"TRIPBOX_NTFY_TOKEN": "tk"}

    def read(*parts: dict) -> Settings:
        env = {"TRIPBOX_AUTH": "none"}
        for part in parts:
            env.update(part)
        return Settings.from_env(env)

    def said(s: Settings) -> tuple:
        return s.feature("notifications").state, s.telegram_on, s.ntfy_on

    assert said(read()) == ("off", False, False)
    assert said(read(telegram)) == ("on", True, False)
    assert said(read(address)) == ("on", False, True)
    assert said(read(address, token)) == ("on", False, True)
    assert said(read(telegram, address, token)) == ("on", True, True)
    s = read(address, token)
    assert (s.ntfy_url, s.ntfy_token, s.problems) == ("https://ntfy.example/trips", "tk", ())
    # half set: invalid, and neither channel is on
    for name in telegram:
        s = read({name: telegram[name]}, address)
        assert said(s) == ("invalid", False, False), name
        (other,) = set(telegram) - {name}
        assert [str(p) for p in s.problems] == [
            f"{other}: not set — Telegram is half configured; set it, or remove {name}"
        ]
    for s in (read(token), read(telegram, token)):
        assert said(s) == ("invalid", False, False)
        assert [str(p) for p in s.problems] == [
            "TRIPBOX_NTFY_URL: not set — ntfy is half configured; set it, or remove"
            " TRIPBOX_NTFY_TOKEN"
        ]
    # an address that is none is a problem that never shows it
    for bad in ("ntfy.example/trips", "https://alex:S3CRET-MARK@ntfy.example/a b"):
        s = read(telegram, {"TRIPBOX_NTFY_URL": bad})
        assert said(s) == ("invalid", False, False), bad
        assert [str(p) for p in s.problems] == [
            "TRIPBOX_NTFY_URL: not an http:// or https:// address with a host — use your"
            " topic's address, such as https://ntfy.example/trips"
        ]
    assert {k.name: (k.secret, k.web) for k in KEYS if k.name.startswith("TRIPBOX_NTFY")} == {
        "TRIPBOX_NTFY_URL": (True, False),
        "TRIPBOX_NTFY_TOKEN": (True, False),
    }


ENRICH = {"TRIPBOX_ENRICH": "aeroapi", "TRIPBOX_AEROAPI_KEY": "S3CRET-AERO"}


def test_enrichment_without_airtrail_is_invalid():
    s = Settings.from_env({"TRIPBOX_AUTH": "none", **ENRICH})
    enrichment = s.feature("enrichment")
    assert enrichment.state == "invalid" and enrichment.missing == ()
    assert [str(p) for p in s.problems] == [
        "TRIPBOX_ENRICH: enrichment fills in AirTrail flights — set up AirTrail first,"
        " or remove TRIPBOX_ENRICH"
    ]
    owner = {"TRIPBOX_CONFIG_DIR": str(CONFIG_DIR)}
    s = Settings.from_env({"TRIPBOX_AUTH": "none", **ENRICH, **AIRTRAIL, **owner})
    assert s.on("airtrail") and s.on("enrichment") and s.problems == ()
    assert s.enrich == "aeroapi"


def test_enrichment_is_off_until_its_switch_names_a_service():
    owner = {"TRIPBOX_AUTH": "none", "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR), **AIRTRAIL}
    s = Settings.from_env(owner)
    assert s.feature("enrichment").state == "off" and s.enrich == ""
    assert s.feature("enrichment").missing == ("TRIPBOX_ENRICH", "TRIPBOX_AEROAPI_KEY")
    # the switch alone: half configured
    s = Settings.from_env({**owner, "TRIPBOX_ENRICH": "aeroapi"})
    assert s.feature("enrichment").state == "invalid"
    assert [str(p) for p in s.problems] == [
        "TRIPBOX_AEROAPI_KEY: not set — enrichment is half configured; set it, or remove"
        " TRIPBOX_ENRICH"
    ]
    # a service tripbox does not know: a problem that shows the word, never the key
    for word, shown in (("flightaware", "'flightaware' is not"), ("S3CRET-AERO-0123456789", "not")):
        s = Settings.from_env({**owner, **ENRICH, "TRIPBOX_ENRICH": word})
        assert s.feature("enrichment").state == "invalid" and not s.on("enrichment")
        assert [str(p) for p in s.problems] == [
            f"TRIPBOX_ENRICH: {shown} a flight-data service — use aeroapi, or remove the line"
        ]
    # its word in any case, and the key: on
    s = Settings.from_env({**owner, **ENRICH, "TRIPBOX_ENRICH": " AeroAPI "})
    assert s.on("enrichment") and s.enrich == "aeroapi"


def test_a_flight_data_key_with_no_switch_is_a_hint_and_no_problem():
    from tripbox.config import report

    owner = {"TRIPBOX_AUTH": "none", "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR), **AIRTRAIL}
    s = Settings.from_env({**owner, "TRIPBOX_AEROAPI_KEY": "S3CRET-AERO"})
    assert s.feature("enrichment").state == "off" and s.problems == ()
    lines = report(s, [], [])
    (row,) = [line for line in lines if line.startswith("enrichment")]
    assert row.split(None, 2)[1:] == [
        "off",
        "TRIPBOX_AEROAPI_KEY is set and TRIPBOX_ENRICH is not: enrichment stays off",
    ]
    assert "problems: 0" in lines and not any("S3CRET" in line for line in lines)


def test_airtrail_needs_an_owner_in_the_travellers_file(tmp_path):
    from tripbox.config import report

    def row(state: str, hint: str) -> str:
        return f"{'travellers':<15}{state:<9}{hint}"

    env = {"TRIPBOX_AUTH": "none", "TRIPBOX_CONFIG_DIR": str(tmp_path), **AIRTRAIL}
    s = Settings.from_env(env)
    airtrail = s.feature("airtrail")
    assert airtrail.state == "invalid" and not s.on("airtrail") and s.on("core")
    assert [str(p) for p in s.problems] == [NO_OWNER]
    assert s.problems[0].feature == "airtrail" and s.travellers.me_name == ""
    absent = "travellers.yaml is absent: nobody is the owner, so no flight reaches AirTrail"
    assert row("none", absent) in report(s)
    # companions alone name no owner either
    (tmp_path / "travellers.yaml").write_text("companions:\n  - name: Sam Sample\n")
    s = Settings.from_env(env)
    assert not s.on("airtrail") and [str(p) for p in s.problems] == [NO_OWNER]
    assert row("on", "owner: 0 names, companions: 1") in report(s)
    s = Settings.from_env({**env, "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR)})
    assert s.on("airtrail") and s.problems == ()
    assert s.travellers.me_name == "Alex Sample"
    assert row("on", "owner: 3 names, companions: 3") in report(s)


def test_only_a_file_named_travellers_yaml_is_read(tmp_path):
    """The shipped example and a file of the old name are no configuration: a directory that
    holds only one of them has no owner, and the old shape under the new name is a problem."""
    shipped = Path(__file__).resolve().parent.parent / "config"
    assert (shipped / "travellers.example.yaml").is_file()
    assert not (shipped / "travellers.yaml").exists()
    old_shape = (
        "me:\n  aliases: [Alex Sample]\nguests:\n  - name: Sam Sample\n    aliases: [Sam Sample]\n"
    )
    old = tmp_path / "old"
    old.mkdir()
    # the old file name, written in two parts so that a search of the tree for a reader of it
    # finds none
    (old / ".".join(("passengers", "yaml"))).write_text(old_shape)
    copied = tmp_path / "copied"
    copied.mkdir()
    example = (shipped / "travellers.example.yaml").read_text()
    (copied / "travellers.example.yaml").write_text(example)
    for config in (shipped, old, copied):
        env = {"TRIPBOX_AUTH": "none", "TRIPBOX_CONFIG_DIR": str(config), **AIRTRAIL}
        s = Settings.from_env(env)
        assert s.travellers.me_name == "" and s.travellers.guests == (), config
        assert not s.travellers.classify(["Alex Sample"]).me_present
        assert [str(p) for p in s.problems] == [NO_OWNER] and not s.on("airtrail")
    (old / "travellers.yaml").write_text(old_shape)
    s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_CONFIG_DIR": str(old), **AIRTRAIL})
    assert s.feature("core").state == "invalid" and not s.on("airtrail")
    assert [str(p) for p in s.feature("core").problems] == [
        "travellers.yaml: unknown key 'me' — the file holds owner: and companions:",
        "travellers.yaml: unknown key 'guests' — the file holds owner: and companions:",
    ]
    assert s.travellers.me_name == "" and not s.travellers.classify(["Alex Sample"]).me_present


def test_a_half_configured_feature_is_invalid_and_names_what_is_missing():
    env = {
        "TRIPBOX_AUTH": "none",
        "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
        "TRIPBOX_AIRTRAIL_URL": "http://flightlog.invalid",
    }
    s = Settings.from_env(env)
    airtrail = s.feature("airtrail")
    assert airtrail.state == "invalid" and not s.on("airtrail")
    assert airtrail.missing == ("TRIPBOX_AIRTRAIL_API_KEY",)
    assert keys(s.problems) == ["TRIPBOX_AIRTRAIL_API_KEY"]
    assert str(s.problems[0]) == (
        "TRIPBOX_AIRTRAIL_API_KEY: not set — AirTrail is half configured; set it,"
        " or remove TRIPBOX_AIRTRAIL_URL"
    )
    s = Settings.from_env({**env, "TRIPBOX_AIRTRAIL_API_KEY": "k"})
    assert s.feature("airtrail").state == "on" and s.problems == ()


def test_parses_values_and_defaults():
    s = Settings.from_env(
        {
            **BASE,
            "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
            "TRIPBOX_HOME_AIRPORTS": "msp, rst",
            "TRIPBOX_MAIL_ALLOWED_SENDERS": "Alex@Example.com",
        }
    )
    assert s.airtrail_dry_run is False
    assert s.home_airports == ("MSP", "RST")
    assert s.allowed_senders == ("alex@example.com",)
    assert s.mail_password == "abcdefghijklmnop"  # Gmail app password spaces are cosmetic
    assert s.airtrail_url == "http://airtrail:3000"  # trailing slash stripped
    assert s.llm_model == "claude-haiku-5-5"
    assert str(s.ledger_path) == "/app/data/ledger.db"
    assert str(s.backups_dir) == "/app/backups"
    s = Settings.from_env({**BASE, "TRIPBOX_BACKUPS_DIR": "backups"})
    assert [str(p) for p in s.problems if p.feature == "core"] == [
        "TRIPBOX_BACKUPS_DIR: 'backups' is not an absolute path"
        " — use a path that starts with /, such as /app/backups"
    ]
    assert s.feature("core").state == "invalid" and str(s.backups_dir) == "/app/backups"


def test_tripbox_settings_have_defaults_and_parse():
    s = Settings.from_env(BASE)
    assert s.tripbox_auth == "password" and s.listen == ("127.0.0.1", 8110)
    assert s.tripbox_access_team == "" and s.tripbox_allowed_emails == ()
    assert s.base_url == "http://localhost:8110" and s.link_base is None
    s = Settings.from_env(
        {
            **BASE,
            "TRIPBOX_AUTH": "none",
            "TRIPBOX_ACCESS_TEAM": "sample-family",
            "TRIPBOX_ACCESS_AUD": "abc",
            "TRIPBOX_ALLOWED_EMAILS": "Alex@Example.com, x@y.z",
            "TRIPBOX_BASE_URL": "https://trips.example.com/",
            "TRIPBOX_LISTEN": "127.0.0.1:8111",
        }
    )
    assert s.tripbox_auth == "none" and s.tripbox_access_aud == "abc"
    assert s.tripbox_allowed_emails == ("alex@example.com", "x@y.z")
    assert s.base_url == s.link_base == "https://trips.example.com"  # trailing slash stripped
    assert s.listen == ("127.0.0.1", 8111)


LISTEN_FIX = "use 127.0.0.1:8110 for this computer only, or 0.0.0.0:8110 for every network it is on"
NO_LOGIN_PUBLISHED = (
    "TRIPBOX_LISTEN: publishes the page beyond this computer while TRIPBOX_AUTH=none"
    " — use 127.0.0.1:8110, or choose a login: password, proxy or cloudflare"
)


def test_listen_is_an_address_and_a_port():
    """An IPv4 address, or an IPv6 address in brackets, then a port from 1 to 65535: never a
    port alone, a name or an address with a scheme. A bad value stops the page, whatever the
    login, and the setting keeps its default."""
    login = {"TRIPBOX_PASSWORD": "a-long-enough-password"}
    for value, want in (
        ("127.0.0.1:8110", ("127.0.0.1", 8110)),
        ("0.0.0.0:80", ("0.0.0.0", 80)),
        ("[::1]:8110", ("::1", 8110)),
        ("[2001:db8::1]:8443", ("2001:db8::1", 8443)),
        (" 127.0.0.1:65535 ", ("127.0.0.1", 65535)),
    ):
        s = Settings.from_env({**login, "TRIPBOX_LISTEN": value})
        assert s.problems == () and s.listen == want, value
    for value in (
        "8110",
        "localhost:8110",
        "127.0.0.1",
        "127.0.0.1:0",
        "127.0.0.1:70000",
        "http://127.0.0.1:8110",
        "127.0.0.1:08110",
        "::1:8110",
        "[127.0.0.1]:8110",
        "[::1]",
    ):
        s = Settings.from_env({**login, "TRIPBOX_LISTEN": value})
        assert [(p.key, p.fix, p.feature) for p in s.problems] == [
            ("TRIPBOX_LISTEN", LISTEN_FIX, "web")
        ], value
        assert s.feature("web").state == "invalid" and s.listen == ("127.0.0.1", 8110), value
    assert str(Settings.from_env({**login, "TRIPBOX_LISTEN": "8110"}).problems[0]) == (
        f"TRIPBOX_LISTEN: '8110' is not an address and a port — {LISTEN_FIX}"
    )
    said = Settings.from_env({**login, "TRIPBOX_LISTEN": "[::1]"}).problems[0].what
    assert said == "not an address and a port"  # brackets are not plain: never shown
    from tripbox.config import DISPLAY_ONLY

    assert "TRIPBOX_LISTEN" not in DISPLAY_ONLY


def test_no_login_is_refused_when_the_page_is_published_beyond_this_computer(
    tmp_path, monkeypatch
):
    """No login is for this computer only: a listen address that is not loopback publishes the
    page to every client that reaches it, whatever Host it sends, so it is a web problem beside
    the one for a base URL that is not local; any login takes any address."""
    import uvicorn

    from tripbox import cli

    for value in ("0.0.0.0:8110", "[::]:8110", "198.51.100.7:8110", "[2001:db8::1]:8110"):
        s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_LISTEN": value})
        assert [str(p) for p in s.problems] == [NO_LOGIN_PUBLISHED], value
        assert s.feature("web").state == "invalid", value
        both = Settings.from_env(
            {
                "TRIPBOX_AUTH": "none",
                "TRIPBOX_LISTEN": value,
                "TRIPBOX_BASE_URL": "https://trips.example.com",
            }
        )
        assert keys(both.problems) == ["TRIPBOX_AUTH", "TRIPBOX_LISTEN"], value
        s = Settings.from_env({"TRIPBOX_AUTH": "cloudflare", "TRIPBOX_LISTEN": value})
        assert "TRIPBOX_LISTEN" not in keys(s.problems), value
    for value in ("127.0.0.2:8110", "[::1]:8110"):
        s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_LISTEN": value})
        assert s.problems == () and s.on("web"), value
    # the page refuses to start, and never listens
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_LISTEN", "0.0.0.0:8110")
    assert cli.main(["web"]) == 2 and served == []


def test_the_old_port_name_is_a_problem_that_names_listen(tmp_path, monkeypatch):
    """TRIPBOX_PORT is no setting now: a problem of names that says what replaced it, is never
    read, and stops no command, the page's included."""
    import uvicorn

    from tripbox import cli

    s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_PORT": "8111"})
    assert [(str(p), p.feature) for p in s.problems] == [
        (
            "TRIPBOX_PORT: not a tripbox setting — renamed: use TRIPBOX_LISTEN (an address and a"
            " port, such as 127.0.0.1:8110)",
            "names",
        )
    ]
    assert s.listen == ("127.0.0.1", 8110)
    assert s.features == Settings.from_env({"TRIPBOX_AUTH": "none"}).features
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    for name, value in {
        "TRIPBOX_AUTH": "none",
        "TRIPBOX_PORT": "8111",
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
    }.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["trips", "list"]) == 0
    assert cli.main(["web"]) == 0
    assert [(o["host"], o["port"]) for o in served] == [("127.0.0.1", 8110)]


def test_a_bad_no_network_value_is_a_problem_doctor_lists(tmp_path, monkeypatch, capsys):
    """TRIPBOX_NO_NETWORK is 1, 0 or unset: anything else is a core problem, which doctor lists
    and every other command stops at, and offline() still refuses it."""
    from tripbox import cli
    from tripbox.config import ConfigError, offline

    for value in ("", "0", "1"):
        s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_NO_NETWORK": value})
        assert s.problems == (), value
    for value in ("yes", "true", " 1", "2"):
        s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_NO_NETWORK": value})
        assert [(str(p), p.feature) for p in s.problems] == [
            (
                "TRIPBOX_NO_NETWORK: not 1, 0 or unset — use 1, or remove the line",
                "core",
            )
        ], value
        assert s.feature("core").state == "invalid", value
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "yes")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("problems: 1") + 1 :] == [
        "TRIPBOX_NO_NETWORK: not 1, 0 or unset — use 1, or remove the line"
    ]
    assert cli.main(["trips", "list"]) == 2
    with pytest.raises(ConfigError):
        offline()


def test_compose_only_names_are_no_setting_and_no_problem():
    """The names the compose file reads from .env (the image, the user and group it runs as) are
    in the settings file the worker receives whole: no process reads them, and none is a
    problem."""
    from tripbox.config import COMPOSE_ONLY

    assert [name for name, _, _ in COMPOSE_ONLY] == ["TRIPBOX_IMAGE", "TRIPBOX_UID", "TRIPBOX_GID"]
    assert [default for _, default, _ in COMPOSE_ONLY] == [
        "ghcr.io/good-work-dojo/tripbox:1",
        "1000",
        "1000",
    ]
    local = {"TRIPBOX_AUTH": "none"}
    composed = {**local, "TRIPBOX_IMAGE": "tripbox:local", "TRIPBOX_UID": "1001"}
    s = Settings.from_env({**composed, "TRIPBOX_GID": "1001"})
    assert s.problems == () and s == Settings.from_env(local)
    for name, _, _ in COMPOSE_ONLY:
        assert name not in KEY_BY_NAME, name
    # a near miss of one is still a problem of names
    assert keys(Settings.from_env({**local, "TRIPBOX_IMAGES": "x"}).problems) == ["TRIPBOX_IMAGES"]


def test_a_base_url_loses_its_default_port_so_its_origin_is_what_a_browser_sends():
    """A browser's Origin never names the scheme's own port, and a port written with leading
    zeros is no port a browser writes: it is a problem, as any other unreadable address."""

    def read(value: str) -> Settings:
        return Settings.from_env({"TRIPBOX_AUTH": "cloudflare", "TRIPBOX_BASE_URL": value})

    for value, origin, base in (
        ("https://trips.example.com:443", "https://trips.example.com", ""),
        ("https://trips.example.com:443/tripbox/", "https://trips.example.com", "/tripbox"),
        ("HTTP://127.0.0.1:80/a/b", "http://127.0.0.1", "/a/b"),
        ("https://[2001:db8::1]:443", "https://[2001:db8::1]", ""),
        ("http://trips.example.com:443", "http://trips.example.com:443", ""),
        ("https://trips.example.com:80", "https://trips.example.com:80", ""),
        ("https://trips.example.com:8443", "https://trips.example.com:8443", ""),
    ):
        s = read(value)
        assert [p for p in s.problems if p.key == "TRIPBOX_BASE_URL"] == [], value
        assert (s.origin, s.base_path, s.base_url) == (origin, base, origin + base), value
        assert s.link_base == origin + base, value
    for value in (
        "https://trips.example.com:0443",
        "http://127.0.0.1:080",
        "https://trips.example.com:00",
        "https://trips.example.com:08110/tripbox",
    ):
        s = read(value)
        said = [p.what for p in s.problems if p.key == "TRIPBOX_BASE_URL"]
        assert said == ["not an address the page can live at"], value
        assert s.link_base is None and s.origin == "http://localhost:8110", value


def test_the_airtrail_dry_run_defaults_to_on():
    assert Settings.from_env(BASE).airtrail_dry_run is True
    assert Settings.from_env({}).airtrail_dry_run is True
    off = Settings.from_env({**BASE, "TRIPBOX_AIRTRAIL_DRY_RUN": "false"})
    assert off.airtrail_dry_run is False


def test_the_dry_run_alone_does_not_switch_airtrail_on():
    for value in ("true", "false"):
        s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_AIRTRAIL_DRY_RUN": value})
        assert s.problems == () and s.feature("airtrail").state == "off"
        assert s.feature("airtrail").missing == ("TRIPBOX_AIRTRAIL_URL", "TRIPBOX_AIRTRAIL_API_KEY")


def test_the_old_dry_run_name_is_a_problem_that_names_the_new_one():
    s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_DRY_RUN": "false"})
    assert [str(p) for p in s.problems] == [
        "TRIPBOX_DRY_RUN: not a tripbox setting — renamed: use TRIPBOX_AIRTRAIL_DRY_RUN"
        " (it only stops writes to AirTrail; bookings are always filed)"
    ]
    assert s.airtrail_dry_run is True  # the old name is never read


def test_the_flight_logs_address_is_a_link_and_no_switch():
    """TRIPBOX_AIRTRAIL_PUBLIC_URL is where the page links the flight log: the page receives it,
    it is no secret, and alone it neither turns AirTrail on nor leaves it half configured; a
    value that is no http(s) address with a host is an AirTrail problem that never shows it."""
    key = KEY_BY_NAME["TRIPBOX_AIRTRAIL_PUBLIC_URL"]
    assert (key.feature, key.web, key.secret, key.default) == ("airtrail", True, False, "")
    assert key.field == "airtrail_public_url"
    local = {"TRIPBOX_AUTH": "none"}
    assert Settings.from_env(local).airtrail_public_url == ""
    s = Settings.from_env({**local, "TRIPBOX_AIRTRAIL_PUBLIC_URL": "https://flightlog.example/"})
    assert s.problems == () and s.airtrail_public_url == "https://flightlog.example"
    airtrail = s.feature("airtrail")
    assert (airtrail.state, airtrail.missing) == (
        "off",
        ("TRIPBOX_AIRTRAIL_URL", "TRIPBOX_AIRTRAIL_API_KEY"),
    )
    for bad in ("flightlog.example", "javascript:alert(1)", "ftp://flightlog.example/x"):
        s = Settings.from_env({**local, "TRIPBOX_AIRTRAIL_PUBLIC_URL": bad})
        assert [(str(p), p.feature) for p in s.problems] == [
            (
                "TRIPBOX_AIRTRAIL_PUBLIC_URL: not an http:// or https:// address with a host"
                " — use the address you open your flight log at, such as"
                " https://flightlog.example",
                "airtrail",
            )
        ], bad
        assert s.airtrail_public_url == "" and s.feature("web").state == "on", bad
        assert s.feature("airtrail").state == "off", bad  # a bad link switches nothing


def test_a_bad_flight_log_address_is_a_problem_of_the_link_alone():
    """With the flight log set up, an address that is no web address is still a problem of
    AirTrail's, which doctor shows, and the page links nothing; but it is the link's alone:
    AirTrail stays on, and so does what needs it. Any other bad AirTrail value still leaves
    AirTrail off."""
    from tripbox.config import DISPLAY_ONLY

    assert DISPLAY_ONLY == ("TRIPBOX_AIRTRAIL_PUBLIC_URL", "TRIPBOX_SOURCE_URL")
    bad = {**BASE, "TRIPBOX_AUTH": "none", "TRIPBOX_AIRTRAIL_PUBLIC_URL": "flightlog.example"}
    s = Settings.from_env(bad)
    assert [(p.key, p.feature) for p in s.problems] == [("TRIPBOX_AIRTRAIL_PUBLIC_URL", "airtrail")]
    airtrail = s.feature("airtrail")
    assert airtrail.state == "on" and [p.key for p in airtrail.problems] == [
        "TRIPBOX_AIRTRAIL_PUBLIC_URL"
    ]
    assert s.on("enrichment") and s.airtrail_public_url == ""
    s = Settings.from_env({**bad, "TRIPBOX_AIRTRAIL_DRY_RUN": "maybe"})
    assert s.feature("airtrail").state == "invalid" and not s.on("enrichment")


@pytest.mark.parametrize(
    "name, value",
    [
        ("TRIPBOX_PUBLIC_ORIGIN", "https://trips.example.com"),  # TRIPBOX_BASE_URL took its place
        ("TRIPBOX_COVERS", "true"),  # every trip shows its coloured tile: nothing is looked up
    ],
)
def test_a_retired_name_is_a_problem_of_names_that_stops_nothing(
    tmp_path, monkeypatch, name, value
):
    """A name that is no setting now is, like any name tripbox does not know, a problem of its
    own that leaves every feature as it was, is never read, and stops no command."""
    from tripbox import cli

    retired = {"TRIPBOX_AUTH": "none", name: value}
    s = Settings.from_env(retired)
    assert [(str(p), p.feature) for p in s.problems] == [
        (
            f"{name}: not a tripbox setting — check the spelling against docs/configuration.md",
            "names",
        )
    ]
    assert name not in KEY_BY_NAME
    assert s.features == Settings.from_env({"TRIPBOX_AUTH": "none"}).features
    assert s.base_url == "http://localhost:8110" and s.link_base is None
    assert not any("cover" in field.name for field in dataclasses.fields(Settings))
    for name, value in retired.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    assert cli.main(["trips", "list"]) == 0


def test_the_fixtures_folder_is_no_setting(tmp_path, monkeypatch):
    """The folder saved mail was once copied into is no setting: its name is a problem of names
    like any other tripbox does not know, whatever its value, and stops no command."""
    from tripbox import cli

    name = "TRIPBOX_FIXTURES_DIR"
    s = Settings.from_env({"TRIPBOX_AUTH": "none", name: "not a path"})
    assert [(str(p), p.feature) for p in s.problems] == [
        (
            f"{name}: not a tripbox setting — check the spelling against docs/configuration.md",
            "names",
        )
    ]
    assert name not in KEY_BY_NAME
    assert not any("fixtures" in field.name for field in dataclasses.fields(Settings))
    assert s.features == Settings.from_env({"TRIPBOX_AUTH": "none"}).features
    for key, value in {"TRIPBOX_AUTH": "none", name: "not a path"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    assert cli.main(["trips", "list"]) == 0


def test_empty_sender_allowlist_is_a_config_error():
    # with no allowed sender every email would be rejected unparsed; mail is not on without one
    s = Settings.from_env({**BASE, "TRIPBOX_MAIL_ALLOWED_SENDERS": ""})
    assert s.feature("mail").state == "invalid" and not s.on("mail")
    assert "TRIPBOX_MAIL_ALLOWED_SENDERS" in keys(s.feature("mail").problems)
    s = Settings.from_env({**BASE, "TRIPBOX_MAIL_ALLOWED_SENDERS": " , "})
    assert keys(s.feature("mail").problems) == ["TRIPBOX_MAIL_ALLOWED_SENDERS"]


def test_defaults_name_no_place():
    s = Settings.from_env({})
    assert s.tz == "UTC" and s.home_airports == ()
    s = Settings.from_env({"TZ": "America/Denver", "TRIPBOX_HOME_AIRPORTS": "den"})
    assert s.tz == "America/Denver" and s.home_airports == ("DEN",)


def test_a_home_airport_must_be_in_the_table(monkeypatch):
    from helpers.airports import SAMPLE_AIRPORTS

    from tripbox.validate import refdata

    reads = []

    def table():
        reads.append(1)
        return {"MSP": SAMPLE_AIRPORTS["MSP"]}

    monkeypatch.setattr(refdata, "bundled_table", table)
    s = Settings.from_env({**BASE, "TRIPBOX_HOME_AIRPORTS": "MSP,QQQ"})
    core = s.feature("core")
    assert core.state == "invalid"
    assert [str(p) for p in core.problems] == [
        "TRIPBOX_HOME_AIRPORTS: 'QQQ' is not in the airport table — check the code"
    ]
    assert s.home_airports == ()  # a value that fails its check takes the default
    assert Settings.from_env({**BASE, "TRIPBOX_HOME_AIRPORTS": "msp"}).home_airports == ("MSP",)
    reads.clear()
    unset = Settings.from_env(BASE)
    assert unset.feature("core").problems == () and reads == []  # unset: the table is not read


def test_a_malformed_home_airport_never_reads_the_table(monkeypatch):
    from tripbox.validate import refdata

    reads = []
    monkeypatch.setattr(refdata, "bundled_table", lambda: reads.append(1) or {})
    s = Settings.from_env({**BASE, "TRIPBOX_HOME_AIRPORTS": "MSP,Q1"})
    assert [str(p) for p in s.feature("core").problems] == [
        "TRIPBOX_HOME_AIRPORTS: 'Q1' is not a three-letter airport code"
        " — list IATA codes such as MSP,RST"
    ]
    assert reads == []


def test_an_unreadable_airport_table_is_a_problem_not_a_crash(monkeypatch):
    from tripbox.validate import refdata

    def broken():
        raise OSError("gone")

    monkeypatch.setattr(refdata, "bundled_table", broken)
    s = Settings.from_env({**BASE, "TRIPBOX_HOME_AIRPORTS": "MSP,RST"})
    assert [str(p) for p in s.feature("core").problems] == [
        "TRIPBOX_HOME_AIRPORTS: the airport table could not be read — reinstall tripbox"
    ]
    assert s.home_airports == ()


def test_a_broken_override_file_is_a_problem(tmp_path):
    (tmp_path / "airlines.yaml").write_text("name: Example Air\n")
    s = Settings.from_env({**BASE, "TRIPBOX_CONFIG_DIR": str(tmp_path)})
    core = s.feature("core")
    assert core.state == "invalid" and keys(core.problems) == ["airlines.yaml"]
    assert str(core.problems[0]) == (
        "airlines.yaml: is not a list of entries (each starts with - iata:)"
        " — fix the file or remove it"
    )
    (tmp_path / "airlines.yaml").unlink()
    (tmp_path / "us_states.txt").write_bytes(b"\xff")
    s = Settings.from_env({**BASE, "TRIPBOX_CONFIG_DIR": str(tmp_path)})
    assert keys(s.feature("core").problems) == ["us_states.txt"]
    # a second load of another directory sees that directory's files
    other = Settings.from_env({**BASE, "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR)})
    assert other.feature("core").problems == ()


def test_trusted_sender_domains_default_and_override():
    from tripbox import lists
    from tripbox.config import Settings

    s = Settings.from_env({})
    assert s.mail_trusted_sender_domains == lists.trusted_sender_domains()
    assert (
        "delta.com" in s.mail_trusted_sender_domains
        and "expedia.com" in s.mail_trusted_sender_domains
    )
    s = Settings.from_env({"TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS": "Delta.com, smallhotel.example"})
    assert s.mail_trusted_sender_domains == ("delta.com", "smallhotel.example")


def test_a_bad_trusted_domains_value_trusts_no_forwarded_sender():
    """The default is the whole shipped list, so a value that fails its check must not fall back
    to it: that would trust senders the owner had left out."""
    from helpers.eml import make_eml
    from test_normalize import ALLOWED, CAF, D1

    from tripbox import lists
    from tripbox.mail.normalize import normalize

    name = "TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS"
    bad = Settings.from_env({name: "delta.com, nodot"})
    mail = bad.feature("mail")
    assert mail.state == "invalid" and keys(mail.problems) == [name]
    assert bad.mail_trusted_sender_domains == ()
    assert Settings.from_env({}).mail_trusted_sender_domains == lists.trusted_sender_domains()
    narrowed = Settings.from_env({name: "united.com"})
    assert narrowed.mail_trusted_sender_domains == ("united.com",) and narrowed.problems == (
        Settings.from_env({}).problems
    )
    # a filter-forwarded email from a shipped domain: trusted with the shipped list, and with a
    # bad value not (its flights are filed with the unverified-sender check)
    raw = make_eml(
        from_="Delta <DeltaAirLines@t.delta.com>", subject="x", date=D1, text="x", headers=CAF
    ).as_bytes()

    def trusted(s: Settings) -> bool:
        n = normalize(
            raw,
            ALLOWED,
            require_auth=s.require_google_auth,
            trusted_domains=s.mail_trusted_sender_domains,
        )
        assert n.origin_ok
        return n.sender_trusted

    assert trusted(Settings.from_env({})) is True
    assert trusted(bad) is False and trusted(narrowed) is False


def test_unrecognised_booleans_and_bad_ints_are_config_errors():
    for bad in ("ture", "y", "enabled", '"true"'):
        s = Settings.from_env({**BASE, "TRIPBOX_AIRTRAIL_DRY_RUN": bad})
        assert keys(s.problems)[0] == "TRIPBOX_AIRTRAIL_DRY_RUN"
        assert s.feature("airtrail").state == "invalid" and s.feature("core").state == "on"
        assert s.airtrail_dry_run is True  # the default, never a silent "off"
    for good, want in (("0", False), ("False", False), ("no", False), ("OFF", False), ("1", True)):
        s = Settings.from_env({**BASE, "TRIPBOX_AIRTRAIL_DRY_RUN": good})
        assert s.airtrail_dry_run is want
    for name, bad in (
        ("TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN", "ten"),
        ("TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN", "-1"),
        ("TRIPBOX_AEROAPI_MONTHLY_BUDGET_USD", "lots"),
    ):
        s = Settings.from_env({**BASE, name: bad})
        enrichment = s.feature("enrichment")
        assert enrichment.state == "invalid" and keys(enrichment.problems) == [name], bad
    s = Settings.from_env(
        {"TRIPBOX_AUTH": "none", "TRIPBOX_LISTEN": "ten", "TRIPBOX_TELEGRAM_CHAT_ID": "1"}
    )
    assert [str(p) for p in s.feature("web").problems] == [
        f"TRIPBOX_LISTEN: 'ten' is not an address and a port — {LISTEN_FIX}"
    ]
    assert s.listen == ("127.0.0.1", 8110) and s.feature("web").state == "invalid"
    # in registry order across features: the Telegram key is registered before the address
    assert keys(s.problems) == ["TRIPBOX_TELEGRAM_BOT_TOKEN", "TRIPBOX_LISTEN"]


def test_a_problem_shows_a_value_only_when_it_is_short_and_plain():
    from tripbox.config import _CHECKS, KEYS

    def problems(**env) -> list[str]:
        return [str(p) for p in Settings.from_env({"TRIPBOX_AUTH": "none", **env}).problems]

    model_fix = "use a name with no spaces, such as claude-haiku-5-5"
    assert problems(TRIPBOX_LLM_MODEL="bad model abcdef") == [  # 16 characters: shown whole
        f"TRIPBOX_LLM_MODEL: 'bad model abcdef' is not a model name — {model_fix}"
    ]
    assert problems(TRIPBOX_LLM_MODEL="bad model abcdefg") == [  # 17: not shown, never cut
        f"TRIPBOX_LLM_MODEL: not a model name — {model_fix}"
    ]
    for odd in ("1@2", "a=b", "x'y", '1"2', "1\n2"):
        assert problems(TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN=odd) == [
            "TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN: not a whole number of 1 or more"
            " — use a whole number such as 10"
        ], odd
    # a URL is never shown: it can carry a password; a key pasted into the wrong line is not
    # shown either
    shown = problems(
        TRIPBOX_AIRTRAIL_URL="htp://alex:S3CRET-MARK@flights.example",
        TRIPBOX_BASE_URL="https://alex:S3CRET-MARK@example.com",
        TRIPBOX_AEROAPI_MONTHLY_BUDGET_USD="S3CRET-MARK-0123456789abcdef",
    )
    assert (
        "TRIPBOX_AIRTRAIL_URL: not an http:// or https:// address with a host"
        " — use the address AirTrail answers at, such as http://flightlog.example:3000"
    ) in shown
    assert len(shown) == 4  # the URL, the AirTrail key it now needs, the budget, the address
    assert not any("S3CRET" in line or "alex" in line for line in shown)
    # what is accepted does not change: AirTrail's address may carry its own login
    s = Settings.from_env(
        {
            "TRIPBOX_AUTH": "none",
            "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
            "TRIPBOX_AIRTRAIL_URL": "https://alex:S3CRET-MARK@flights.example",
            "TRIPBOX_AIRTRAIL_API_KEY": "k",
        }
    )
    assert s.problems == () and s.on("airtrail")
    # a secret is never shown, however short and plain its value, checked or not
    marks = {k.name: f"S3CRET-{i}" for i, k in enumerate(KEYS) if k.secret}
    assert len(marks) == 10
    found = problems(**marks)
    assert found and not any("S3CRET" in line for line in found)
    for check in _CHECKS.values():  # every problem has a text without the value
        assert "{}" not in check.hidden and (check.shown is None or "{}" in check.shown)


def test_registry_names_are_unique_and_fill_the_settings():
    from tripbox.config import DERIVED_FIELDS, KEY_BY_NAME, KEYS

    names = [k.name for k in KEYS]
    assert len(names) == len(set(names)) == len(KEY_BY_NAME)
    assert all(KEY_BY_NAME[k.name] is k for k in KEYS)
    assert all(n == "TZ" or n.startswith("TRIPBOX_") for n in names)
    features = {"core", "web", "mail", "model", "airtrail", "notifications", "enrichment", "dev"}
    assert {k.feature for k in KEYS} <= features
    fields = {f.name for f in dataclasses.fields(Settings)}
    filled = [k.field for k in KEYS if k.field is not None]
    assert len(filled) == len(set(filled))  # no field is filled by two keys
    assert set(filled) <= fields
    assert fields <= set(filled) | set(DERIVED_FIELDS)
    assert DERIVED_FIELDS == ("problems", "features", "travellers", "link_base")
    assert not set(filled) & set(DERIVED_FIELDS)


def test_names_written_out_by_hand_are_the_registrys(tmp_path, monkeypatch):
    from tripbox import cli
    from tripbox.config import KEY_BY_NAME, KEYS, MODEL_KEY_ENV, OFFLINE_ENV
    from tripbox.log import redact
    from tripbox.pipeline.run import RECORD_ENV, REPLAY_ENV

    for name in (OFFLINE_ENV, MODEL_KEY_ENV, REPLAY_ENV, RECORD_ENV):
        assert name in KEY_BY_NAME, name
    assert KEY_BY_NAME[MODEL_KEY_ENV].field == "llm_api_key"
    # a command masks every secret key's value in its logs, whichever keys are secret
    marks = {k.name: f"S3CRET-MASK-{i:02d}" for i, k in enumerate(KEYS) if k.secret}
    assert len(marks) == 10
    for name, mark in marks.items():
        monkeypatch.setenv(name, mark)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    assert cli.main(["trips", "list"]) == 0
    for mark in marks.values():
        assert redact(f"a {mark} b") == "a [REDACTED] b", mark


# The names settings had before they all took the TRIPBOX_ prefix, each with a value that is not
# its default. They are written in two parts so that a search of the tree for a reader of an old
# name finds none: this is the one place that spells them, to prove nothing reads them.
OLD_NAMES = {
    "_".join(parts): value
    for parts, value in (
        (("HOME", "AIRPORTS"), "RST"),
        (("IMAP", "HOST"), "imap.example.com"),
        (("IMAP", "USER"), "trips@example.com"),
        (("IMAP", "PASSWORD"), "placeholder"),
        (("MAIL", "ALLOWED_SENDERS"), "alex@example.com"),
        (("MAIL", "REQUIRE_GOOGLE_AUTH"), "false"),
        (("MAIL", "TRUSTED_SENDER_DOMAINS"), "smallhotel.example"),
        (("ANTHROPIC", "API_KEY"), "placeholder"),
        (("ANTHROPIC", "MODEL"), "placeholder-model"),
        (("AIRTRAIL", "URL"), "http://flightlog.invalid"),
        (("AIRTRAIL", "API_KEY"), "placeholder"),
        (("AEROAPI", "KEY"), "placeholder"),
        (("AEROAPI", "MONTHLY_BUDGET_USD"), "2.50"),
        (("AEROAPI", "MAX_CALLS_PER_RUN"), "3"),
        (("TELEGRAM", "BOT_TOKEN"), "placeholder"),
        (("TELEGRAM", "CHAT_ID"), "1"),
    )
}


def test_the_old_unprefixed_names_are_not_read():
    assert len(OLD_NAMES) == 16
    assert Settings.from_env(OLD_NAMES) == Settings.from_env({})


def test_mail_trust_maps_to_the_google_check():
    assert Settings.from_env({}).require_google_auth is True
    for word, trust, want in (
        ("gmail", "gmail", True),
        ("mailbox", "mailbox", False),
        (" MAILBOX ", "mailbox", False),
    ):
        s = Settings.from_env({"TRIPBOX_MAIL_TRUST": word})
        assert s.mail_trust == trust and s.require_google_auth is want, word
    s = Settings.from_env({"TRIPBOX_MAIL_TRUST": "true"})
    assert keys(s.feature("mail").problems) == ["TRIPBOX_MAIL_TRUST"]
    assert s.require_google_auth is True  # the default: the stricter check


def test_gmail_trust_needs_gmails_own_mailbox():
    """`gmail` trust believes the Authentication-Results header that names Gmail, which only
    Gmail's own mailbox guarantees is Gmail's: with it, set or by default, a mail host that is not
    Gmail's is a problem that leaves mail off and says to use `mailbox`. Gmail's two hosts in any
    letter case, and no host at all, are fine; so is another host with `mailbox`."""
    for trust in ({}, {"TRIPBOX_MAIL_TRUST": "gmail"}):
        assert Settings.from_env(trust).feature("mail").problems == (), trust
        for host in ("imap.gmail.com", "IMAP.GoogleMail.com"):
            s = Settings.from_env({**BASE, "TRIPBOX_MAIL_HOST": host, **trust})
            assert s.on("mail") and s.feature("mail").problems == (), (host, trust)
    other = "imap.example.com"
    for trust in ({}, {"TRIPBOX_MAIL_TRUST": "gmail"}, {"TRIPBOX_MAIL_TRUST": " Gmail "}):
        s = Settings.from_env({**BASE, "TRIPBOX_MAIL_HOST": other, **trust})
        assert s.feature("mail").state == "invalid" and not s.on("mail"), trust
        assert [str(p) for p in s.feature("mail").problems] == [
            "TRIPBOX_MAIL_TRUST: gmail trust needs Gmail's own mailbox"
            f" — set TRIPBOX_MAIL_TRUST=mailbox for {other}"
        ], trust
    s = Settings.from_env({**BASE, "TRIPBOX_MAIL_HOST": other, "TRIPBOX_MAIL_TRUST": "mailbox"})
    assert s.on("mail") and s.feature("mail").problems == ()
    # a trust that is no word is its own problem, once
    s = Settings.from_env({**BASE, "TRIPBOX_MAIL_HOST": other, "TRIPBOX_MAIL_TRUST": "true"})
    assert keys(s.feature("mail").problems) == ["TRIPBOX_MAIL_TRUST"]


def test_filing_defaults_by_trust_mode():
    """Unset, handled mail is filed under labels in a Gmail mailbox and any other mailbox is left
    as it is; set, the word holds in either mode. Any other word is a problem that leaves mail
    off."""
    for env, want in (
        ({}, "labels"),
        ({"TRIPBOX_MAIL_TRUST": "gmail"}, "labels"),
        ({"TRIPBOX_MAIL_TRUST": "mailbox"}, "none"),
        ({"TRIPBOX_MAIL_TRUST": "mailbox", "TRIPBOX_MAIL_FILING": ""}, "none"),
    ):
        s = Settings.from_env(env)
        assert s.mail_filing == want and s.feature("mail").problems == (), env
    for trust in ("gmail", "mailbox"):
        for word, want in (("labels", "labels"), (" Labels ", "labels"), ("NONE", "none")):
            s = Settings.from_env({"TRIPBOX_MAIL_TRUST": trust, "TRIPBOX_MAIL_FILING": word})
            assert s.mail_filing == want and s.feature("mail").problems == (), (trust, word)
    s = Settings.from_env({**BASE, "TRIPBOX_MAIL_FILING": "copy"})
    assert s.feature("mail").state == "invalid"
    assert [str(p) for p in s.feature("mail").problems] == [
        "TRIPBOX_MAIL_FILING: 'copy' is not labels or none — use labels or none"
    ]


def test_the_model_provider_is_one_of_three_and_none_switches_the_model_off():
    from tripbox import cli
    from tripbox.config import KEYS, report

    key = {"TRIPBOX_LLM_API_KEY": "a"}
    s = Settings.from_env(key)
    assert s.llm_provider == "anthropic" and s.on("model")  # unset: as before, on with a key
    s = Settings.from_env({**key, "TRIPBOX_LLM_PROVIDER": " Anthropic "})
    assert s.llm_provider == "anthropic" and s.on("model")
    # none: off whatever else is set, with nothing missing and nothing to fix
    for env in ({}, key, {**key, "TRIPBOX_LLM_MODEL": "two words"}):
        s = Settings.from_env({**env, "TRIPBOX_LLM_PROVIDER": "none"})
        model = s.feature("model")
        assert (s.llm_provider, model.state, model.missing, model.problems) == (
            "none",
            "off",
            (),
            (),
        ), env
        assert not s.on("model") and s.problems == s.feature("web").problems, env
    hint = "TRIPBOX_LLM_PROVIDER=none: only emails carrying structured data are read"
    assert f"{'model':<15}{'off':<9}{hint}" in report(s)
    # openai with only a key names no model: a problem, which leaves the model off (see below)
    s = Settings.from_env({**key, "TRIPBOX_LLM_PROVIDER": "openai"})
    assert s.llm_provider == "openai"
    assert s.feature("model").state == "invalid" and not s.on("model")
    # any other word is a problem
    s = Settings.from_env({**key, "TRIPBOX_LLM_PROVIDER": "claude"})
    assert s.feature("model").state == "invalid" and not s.on("model")
    assert [str(p) for p in s.feature("model").problems] == [
        "TRIPBOX_LLM_PROVIDER: 'claude' is not anthropic, openai or none"
        " — use anthropic, openai or none"
    ]
    # eval without recorded answers takes every model setting from the process
    assert cli.EVAL_MODEL_KEYS == tuple(k.name for k in KEYS if k.feature == "model")
    assert "TRIPBOX_LLM_PROVIDER" in cli.EVAL_MODEL_KEYS


def test_a_bad_cap_setting_stops_the_model():
    """The cap's three settings are the model's: one that holds a value it cannot take makes the
    model invalid, so nothing is asked. Unset, the budget is 5.00 a month, a model with no known
    price makes at most 100 calls a day, and the price is the shipped table's."""
    from tripbox import cli
    from tripbox.ledger import Ledger
    from tripbox.parse import provider

    key = {"TRIPBOX_LLM_API_KEY": "a"}
    s = Settings.from_env(key)
    assert (s.llm_monthly_budget_usd, s.llm_max_calls_per_day, s.llm_price) == (5.0, 100, None)
    s = Settings.from_env(
        {
            **key,
            "TRIPBOX_LLM_MONTHLY_BUDGET_USD": "0",
            "TRIPBOX_LLM_MAX_CALLS_PER_DAY": "1",
            "TRIPBOX_LLM_PRICE_USD_PER_MTOK": " 0.10 , 0.5 ",
        }
    )
    assert s.on("model") and s.problems == s.feature("web").problems
    assert (s.llm_monthly_budget_usd, s.llm_max_calls_per_day, s.llm_price) == (0.0, 1, (0.1, 0.5))
    free = Settings.from_env({**key, "TRIPBOX_LLM_PRICE_USD_PER_MTOK": "0,0"})
    assert free.llm_price == (0.0, 0.0)
    two_prices = "use INPUT,OUTPUT in dollars per million tokens, such as 0.10,0.50"
    for name, bad, said in (
        (
            "TRIPBOX_LLM_MONTHLY_BUDGET_USD",
            "-1",
            "'-1' is not a number of 0 or more — use a number such as 5.00",
        ),
        (
            "TRIPBOX_LLM_MONTHLY_BUDGET_USD",
            "lots",
            "'lots' is not a number of 0 or more — use a number such as 5.00",
        ),
        (
            "TRIPBOX_LLM_MAX_CALLS_PER_DAY",
            "0",
            "'0' is not a whole number of 1 or more — use a whole number such as 100",
        ),
        ("TRIPBOX_LLM_PRICE_USD_PER_MTOK", "0.10", f"'0.10' is not two prices — {two_prices}"),
        ("TRIPBOX_LLM_PRICE_USD_PER_MTOK", "0.1,-0.5", f"not two prices — {two_prices}"),
        ("TRIPBOX_LLM_PRICE_USD_PER_MTOK", "1,2,3", f"not two prices — {two_prices}"),
        ("TRIPBOX_LLM_PRICE_USD_PER_MTOK", "a,b", f"not two prices — {two_prices}"),
        ("TRIPBOX_LLM_PRICE_USD_PER_MTOK", "nan,1", f"not two prices — {two_prices}"),
    ):
        s = Settings.from_env({**key, name: bad})
        model = s.feature("model")
        assert model.state == "invalid" and not s.on("model"), bad
        assert [str(p) for p in model.problems] == [f"{name}: {said}"], bad
        assert provider.build(s, Ledger.memory()) is None, bad
    # with the provider none, nothing of the model is read
    s = Settings.from_env({"TRIPBOX_LLM_PROVIDER": "none", "TRIPBOX_LLM_MAX_CALLS_PER_DAY": "0"})
    assert s.feature("model").state == "off" and s.problems == s.feature("web").problems
    for name in (
        "TRIPBOX_LLM_MONTHLY_BUDGET_USD",
        "TRIPBOX_LLM_MAX_CALLS_PER_DAY",
        "TRIPBOX_LLM_PRICE_USD_PER_MTOK",
    ):
        assert name in cli.EVAL_MODEL_KEYS  # eval asks the configured model under its cap


def test_the_openai_provider_needs_a_model_and_a_key_or_an_address():
    from tripbox.config import KEY_BY_NAME, report

    def model(env: dict[str, str]) -> tuple[str, list[str]]:
        s = Settings.from_env(env)
        return s.feature("model").state, [str(p) for p in s.feature("model").problems]

    chosen = {"TRIPBOX_LLM_PROVIDER": "openai"}
    named = {**chosen, "TRIPBOX_LLM_MODEL": "served-model"}
    key = {"TRIPBOX_LLM_API_KEY": "a"}
    address = {"TRIPBOX_LLM_BASE_URL": "http://models.example/v1/"}
    for env in (key, address, {**key, **address}):
        assert model({**named, **env}) == ("on", []), env
    s = Settings.from_env({**named, **address})
    assert (s.llm_model, s.llm_base_url, s.llm_api_key) == (
        "served-model",
        "http://models.example/v1",
        "",
    )
    unpriced = "openai · served-model · no known price: at most 100 calls a day"
    assert f"{'model':<15}{'on':<9}{unpriced}" in report(s)
    # no model named: the Anthropic default is never sent to another server, nor is it when it
    # is written out by hand (an `.env` that set it for the Anthropic provider)
    needs_a_model = "TRIPBOX_LLM_MODEL: not set — name the model your server serves"
    default = KEY_BY_NAME["TRIPBOX_LLM_MODEL"].default
    for env in (key, address):
        assert model({**chosen, **env}) == ("invalid", [needs_a_model]), env
        written = {**chosen, **env, "TRIPBOX_LLM_MODEL": f" {default} "}
        assert model(written) == ("invalid", [needs_a_model]), env
        assert Settings.from_env(written).feature("model").missing[-1] == "TRIPBOX_LLM_MODEL"
    # neither a key nor an address
    needs_a_server = (
        "TRIPBOX_LLM_PROVIDER: openai needs TRIPBOX_LLM_API_KEY or TRIPBOX_LLM_BASE_URL"
        " — set the key of a hosted service, or the address of your own server"
    )
    assert model(named) == ("invalid", [needs_a_server])
    assert model(chosen) == ("invalid", [needs_a_server, needs_a_model])
    # an address that is none: the problem shows nothing of it, which may hold a login
    bad = "ftp://alex:S3CRET-MARK@models.example"
    assert model({**named, "TRIPBOX_LLM_BASE_URL": bad}) == (
        "invalid",
        [
            "TRIPBOX_LLM_BASE_URL: not an http:// or https:// address with a host"
            " — use the server's address as the worker reaches it (a model server in another"
            " container on the same Docker network by its service name, or this machine's"
            " address on your network; not localhost), such as http://models.example:11434/v1"
        ],
    )
    # Anthropic takes no address, a good one or a bad one; none reads none
    only_openai = (
        "TRIPBOX_LLM_BASE_URL: only the openai provider takes an address"
        " — remove it, or set TRIPBOX_LLM_PROVIDER=openai"
    )
    for url in (address["TRIPBOX_LLM_BASE_URL"], bad):
        assert model({**key, "TRIPBOX_LLM_BASE_URL": url}) == ("invalid", [only_openai]), url
        assert model({"TRIPBOX_LLM_PROVIDER": "none", "TRIPBOX_LLM_BASE_URL": url}) == ("off", [])
    # a secret (it may carry a login), which the page never receives
    assert KEY_BY_NAME["TRIPBOX_LLM_BASE_URL"].secret
    assert not KEY_BY_NAME["TRIPBOX_LLM_BASE_URL"].web


def test_the_example_password_from_the_instructions_is_refused():
    """A password written as the instructions' placeholder (`<…>`) is the example, pasted as it
    is: long enough, and printed in every copy of the instructions."""
    example = (
        "TRIPBOX_PASSWORD: still the example from the instructions — choose your own password"
        " of 10 characters or more"
    )

    def web(value: str) -> tuple[str, list[str]]:
        s = Settings.from_env({"TRIPBOX_PASSWORD": value})
        return s.feature("web").state, [str(p) for p in s.problems]

    for value in (
        "<choose a password of 10 characters or more>",
        "<ten characters or more>",
        "<x>",
        " <a placeholder with spaces around it> ",
    ):
        assert web(value) == ("invalid", [example]), value
    for value in ("<starts-only-1234", "ends-only-1234>", "a<middle>-1234", "<>x-ten-chars"):
        assert web(value) == ("on", []), value


def test_password_mode_needs_exactly_one_of_the_pair():
    from test_passwords import FAKE, cheap_hash

    def web(**env) -> tuple[str, list[str]]:
        s = Settings.from_env(env)
        return s.feature("web").state, [str(p) for p in s.problems]

    good = cheap_hash(FAKE)
    shorter = "TRIPBOX_PASSWORD: shorter than 10 characters — use 10 or more"
    not_printed = (
        'TRIPBOX_PASSWORD_HASH: not a line "tripbox passwd" printed — run it again and paste the'
        " whole line"
    )
    together = "TRIPBOX_PASSWORD_HASH: set together with TRIPBOX_PASSWORD — keep one of them"
    assert Settings.from_env({}).tripbox_auth == "password"  # the default login
    assert web() == ("invalid", [PASSWORD_NOT_SET])
    assert web(TRIPBOX_PASSWORD=" ", TRIPBOX_PASSWORD_HASH="") == ("invalid", [PASSWORD_NOT_SET])
    assert web(TRIPBOX_PASSWORD="ten-chars!") == ("on", [])
    assert web(TRIPBOX_PASSWORD="nine-char") == ("invalid", [shorter])
    assert web(TRIPBOX_PASSWORD_HASH=good) == ("on", [])
    assert web(TRIPBOX_PASSWORD_HASH=f"{good}\n") == ("on", [])
    for value in ("S3CRET-not-a-hash", good[:-1], f"TRIPBOX_PASSWORD_HASH={good}"):
        assert web(TRIPBOX_PASSWORD_HASH=value) == ("invalid", [not_printed]), value
    assert web(TRIPBOX_PASSWORD="S3CRET-ten-chars", TRIPBOX_PASSWORD_HASH=good) == (
        "invalid",
        [together],
    )
    assert web(TRIPBOX_PASSWORD="S3CRET-ten-chars", TRIPBOX_PASSWORD_HASH="x") == (
        "invalid",
        [together],
    )
    # a password longer than a browser may send can never be typed; a hash line set as the
    # password would be taken as the password itself
    longer = "TRIPBOX_PASSWORD: longer than 1024 characters — use 1024 or fewer"
    misplaced = (
        'TRIPBOX_PASSWORD: holds a line "tripbox passwd" printed — put it in'
        " TRIPBOX_PASSWORD_HASH instead"
    )
    assert web(TRIPBOX_PASSWORD="x" * 1024) == ("on", [])
    assert web(TRIPBOX_PASSWORD="x" * 1025) == ("invalid", [longer])
    assert web(TRIPBOX_PASSWORD=good) == ("invalid", [misplaced])
    assert web(TRIPBOX_PASSWORD=f" {good}\n") == ("invalid", [misplaced])
    assert web(TRIPBOX_PASSWORD=good[:-1]) == ("on", [])  # no hash line: a password
    # each value is kept as it was given; another login reads neither
    s = Settings.from_env({"TRIPBOX_PASSWORD": " spaced words "})
    assert (s.password, s.password_hash) == (" spaced words ", "")
    assert web(TRIPBOX_AUTH="none", TRIPBOX_PASSWORD="short") == ("on", [])
    assert web(TRIPBOX_AUTH="none", TRIPBOX_PASSWORD_HASH="x", TRIPBOX_PASSWORD="y") == ("on", [])
    # the other modes, and one that is no mode
    assert web(TRIPBOX_AUTH="proxy") == (
        "invalid",
        [
            "TRIPBOX_TRUSTED_PROXIES: not set — TRIPBOX_AUTH=proxy needs it",
            "TRIPBOX_PROXY_USERS: not set — TRIPBOX_AUTH=proxy needs it",
        ],
    )
    assert web(TRIPBOX_AUTH="ldap", TRIPBOX_PASSWORD="ten-chars!") == (
        "invalid",
        [
            "TRIPBOX_AUTH: 'ldap' is not password, proxy, cloudflare or none"
            " — use password, proxy, cloudflare or none"
        ],
    )


def test_proxy_mode_needs_its_proxies_and_users_and_refuses_everyone():
    """A login proxy's header is believed only from the addresses listed and only for the users
    listed: proxy mode needs both. A network that holds every address is refused, as a value that
    is no address or network is, and the setting then trusts nobody. Doctor's web row names
    each login."""
    from ipaddress import ip_network

    from tripbox.config import report

    def web(**env) -> tuple[str, list[str]]:
        s = Settings.from_env({"TRIPBOX_AUTH": "proxy", **env})
        return s.feature("web").state, [str(p) for p in s.problems]

    needs = "TRIPBOX_AUTH=proxy needs it"
    unset = Settings.from_env({"TRIPBOX_AUTH": "proxy"}).feature("web")
    assert unset.missing == ("TRIPBOX_TRUSTED_PROXIES", "TRIPBOX_PROXY_USERS")
    assert web(TRIPBOX_PROXY_USERS="alex") == (
        "invalid",
        [f"TRIPBOX_TRUSTED_PROXIES: not set — {needs}"],
    )
    assert web(TRIPBOX_TRUSTED_PROXIES="192.0.2.10", TRIPBOX_PROXY_USERS=" , ") == (
        "invalid",
        [
            "TRIPBOX_PROXY_USERS: not a list of user names — list the names your proxy signs"
            " people in as, comma-separated, such as alex"
        ],
    )
    good = {
        "TRIPBOX_TRUSTED_PROXIES": " 192.0.2.10, 198.51.100.0/24 ,2001:db8::/32",
        "TRIPBOX_PROXY_USERS": "Alex, sam",
    }
    assert web(**good) == ("on", [])
    s = Settings.from_env({"TRIPBOX_AUTH": "proxy", **good})
    assert s.trusted_proxies == (
        ip_network("192.0.2.10/32"),
        ip_network("198.51.100.0/24"),
        ip_network("2001:db8::/32"),
    )
    assert s.proxy_users == ("alex", "sam") and s.proxy_user_header == "Remote-User"
    # an IPv4-mapped address or network is read as the IPv4 one, as a request's peer is
    mapped = {**good, "TRIPBOX_TRUSTED_PROXIES": "::ffff:192.0.2.10, ::ffff:198.51.100.0/120"}
    s = Settings.from_env({"TRIPBOX_AUTH": "proxy", **mapped})
    assert s.trusted_proxies == (ip_network("192.0.2.10/32"), ip_network("198.51.100.0/24"))
    # a network that holds every address trusts everyone: refused, and nobody is trusted
    fix = "list your proxy's address or network, such as 192.0.2.0/24"
    for every in ("0.0.0.0/0", "::/0", "192.0.2.10, ::/0", "::ffff:0:0/96", "0.0.0.0/0.0.0.0"):
        s = Settings.from_env({"TRIPBOX_AUTH": "proxy", **good, "TRIPBOX_TRUSTED_PROXIES": every})
        assert [str(p) for p in s.problems] == [
            f"TRIPBOX_TRUSTED_PROXIES: trusts every address — {fix}"
        ], every
        assert s.trusted_proxies == () and s.feature("web").state == "invalid", every
    # nor is a value that is no address or network taken (shown when it is short and plain)
    hidden = "not a list of addresses or networks"
    for value, what in (
        ("192.0.2.300", "'192.0.2.300' is not an address or a network"),
        ("proxy.example", "'proxy.example' is not an address or a network"),
        ("192.0.2.10/24", "'192.0.2.10/24' is not an address or a network"),  # host bits set
        ("192.0.2.10 192.0.2.11", hidden),
        (" , ", hidden),
    ):
        s = Settings.from_env({"TRIPBOX_AUTH": "proxy", **good, "TRIPBOX_TRUSTED_PROXIES": value})
        assert [str(p) for p in s.problems] == [f"TRIPBOX_TRUSTED_PROXIES: {what} — {fix}"], value
        assert s.trusted_proxies == (), value
    # the header: letters, digits and -
    header_fix = "use letters, digits and -, such as Remote-User"
    for value in ("Remote_User", "X User", "X-User:"):
        assert web(**good, TRIPBOX_PROXY_USER_HEADER=value) == (
            "invalid",
            [f"TRIPBOX_PROXY_USER_HEADER: {value!r} is not a header name — {header_fix}"],
        ), value
    s = Settings.from_env({"TRIPBOX_AUTH": "proxy", **good, "TRIPBOX_PROXY_USER_HEADER": "X-Who"})
    assert s.proxy_user_header == "X-Who" and s.problems == ()
    assert web(**{**good, "TRIPBOX_PROXY_USERS": "alex, sam sample"}) == (
        "invalid",
        [
            "TRIPBOX_PROXY_USERS: 'sam sample' is not a user name — list the names your proxy"
            " signs people in as, comma-separated, such as alex"
        ],
    )
    # another login reads none of the three
    assert Settings.from_env({"TRIPBOX_AUTH": "none"}).problems == ()

    def web_row(**env) -> str:
        (line,) = [x for x in report(Settings.from_env(env)) if x.startswith("web ")]
        return line

    on = f"{'web':<15}{'on':<9}"
    assert web_row(TRIPBOX_AUTH="proxy", **good) == on + "login: proxy"
    assert web_row(TRIPBOX_PASSWORD="ten-chars!") == on + "login: password"
    access = dict.fromkeys(CLOUDFLARE_KEYS, "x@example.com")
    assert web_row(TRIPBOX_AUTH="cloudflare", **access) == on + "login: cloudflare"
    assert web_row(TRIPBOX_AUTH="none") == on + "login: none (this computer only)"


def test_a_bad_source_address_is_a_problem_of_the_link_alone():
    """TRIPBOX_SOURCE_URL is where the page links its source code: a value that is no web
    address is listed (doctor shows it), the link falls back to the project's own address, and
    the page stays on."""
    from tripbox.config import KEY_BY_NAME, report

    key = KEY_BY_NAME["TRIPBOX_SOURCE_URL"]
    assert key.web and not key.secret and key.feature == "web"
    s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_SOURCE_URL": "code.example/tripbox"})
    assert [(p.key, p.feature) for p in s.problems] == [("TRIPBOX_SOURCE_URL", "web")]
    assert s.feature("web").state == "on" and s.source_url == key.default
    assert (
        "TRIPBOX_SOURCE_URL: not an http:// or https:// address with a host"
        in "\n".join(report(s))
    )
