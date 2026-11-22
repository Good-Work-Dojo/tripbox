# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`python -m tripbox trips …` edge cases: a bad invocation prints usage, never a traceback."""

import pytest
from helpers.sample import CONFIG_DIR

from tripbox import cli
from tripbox.trips import backfill as bf

CREDENTIALS = {
    "TRIPBOX_MAIL_HOST": "imap.gmail.com",
    "TRIPBOX_MAIL_USER": "x",
    "TRIPBOX_MAIL_PASSWORD": "x",
    "TRIPBOX_MAIL_ALLOWED_SENDERS": "alex@example.com",
    "TRIPBOX_AIRTRAIL_URL": "http://airtrail:3000",
    "TRIPBOX_AIRTRAIL_API_KEY": "k",
    "TRIPBOX_LLM_API_KEY": "k",
    "TRIPBOX_AEROAPI_KEY": "k",
    "TRIPBOX_TELEGRAM_BOT_TOKEN": "t",
    "TRIPBOX_TELEGRAM_CHAT_ID": "1",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in CREDENTIALS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    return tmp_path


def test_list_on_an_empty_ledger(env, capsys):
    assert cli.main(["trips", "list"]) == 0
    assert capsys.readouterr().out.strip() == "No trips yet."


def test_text_for_a_missing_trip(env, capsys):
    assert cli.main(["trips", "text", "999"]) == 1
    assert "no trip #999" in capsys.readouterr().err


def test_no_subcommand_looks_up_a_picture(env, capsys):
    """A trip shows its coloured tile: there is nothing to look up, so no command does."""
    assert cli.main(["trips", "covers"]) == 2
    usage = capsys.readouterr().err
    assert usage.startswith("usage: tripbox trips list | text ID | feed-token")
    assert "cover" not in usage


def test_the_flight_log_backfill_with_a_bad_since_prints_usage(env, capsys):
    assert cli.main(["airtrail", "backfill", "--since", "yesterday"]) == 2
    assert "usage: tripbox airtrail backfill" in capsys.readouterr().err


def test_the_backfill_command_prints_a_refusal_and_exits_2(env, capsys):
    assert cli.main(["airtrail", "backfill"]) == 2  # no home airports are set
    out = capsys.readouterr()
    assert out.err.strip() == bf.NO_HOME and "\n" not in out.err.strip()
    assert "Traceback" not in out.out + out.err


def test_the_backfill_command_does_not_swallow_other_errors(env, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("chain has no dates")

    monkeypatch.setattr(bf, "backfill", broken)
    with pytest.raises(ValueError, match="chain has no dates"):
        cli.main(["airtrail", "backfill"])
