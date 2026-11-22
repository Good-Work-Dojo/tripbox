# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox mail backfill`: a mailbox is read from the moment tripbox first reads it, and older
mail only on request. The command counts the messages since a day that arrived before the cursor
and, with `--apply`, reads each as the poll reads mail (the sender check, the model's cap, filing
as set), leaving the cursor where it is. Every email is the corpus's own or a stranger's invented
one; the mailbox is `helpers.inbox.FakeInbox` and the model an OpenAI-compatible server held in
the test process."""

import datetime as dt
import os
from types import SimpleNamespace

import openai
import pytest
from helpers.inbox import FakeInbox, small_message
from helpers.modelserver import FakeModelServer
from test_budget import read_by_the_model, sha, the_corpus

from tripbox import cli, corpus, util
from tripbox.ledger import Ledger
from tripbox.mail import imap as imapmod
from tripbox.parse import pipeline as pl

MAILBOX = {
    "TRIPBOX_MAIL_HOST": "imap.gmail.com",
    "TRIPBOX_MAIL_USER": "trips@example.com",
    "TRIPBOX_MAIL_PASSWORD": "placeholder",
}
DAY = dt.timedelta(days=1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The corpus's own settings, a mailbox, and a model on an OpenAI-compatible server held in
    the test process (`env.server`) that may be asked twice a day; nothing else, its files under
    `env.root`. The structured-data extractor finds nothing, so the model reads every email."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    c = the_corpus()
    server = FakeModelServer()
    real = openai.OpenAI
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: real(**kw, http_client=server.http_client()))
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [])
    for name, value in {
        **c.settings,
        **MAILBOX,
        "TRIPBOX_CONFIG_DIR": str(c.root / corpus.CONFIG_DIR),
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_LLM_PROVIDER": "openai",
        "TRIPBOX_LLM_MODEL": "served-model",
        "TRIPBOX_LLM_BASE_URL": server.base_url,
        "TRIPBOX_LLM_MAX_CALLS_PER_DAY": "2",
    }.items():
        monkeypatch.setenv(name, value)
    return SimpleNamespace(root=tmp_path, server=server)


class Mailbox(FakeInbox):
    """The fake mailbox as the command opens it: closed once read."""

    closed = 0

    def close(self) -> None:
        self.closed += 1


def opened(monkeypatch, inbox: Mailbox) -> Mailbox:
    """From here on every command that opens the mailbox opens `inbox`."""
    monkeypatch.setattr(imapmod, "ImapInbox", lambda host, user, password: inbox)
    return inbox


def older_mail() -> tuple[Mailbox, dt.date, list[corpus.Entry]]:
    """A mailbox holding, oldest first, a stranger's message from before the day the backfill
    starts at (UID 1), three of the corpus's emails the model reads, each on the day it was sent
    (2 to 4), and a stranger's message after them (5); the next message was deleted, so a poll's
    cursor stands at 6. And that day (the first email's)."""
    emails = read_by_the_model()[:3]
    days = [corpus.sent(e).date() for e in emails]
    since = days[0]
    inbox = Mailbox()
    inbox.deliver(small_message(1), since - 3 * DAY)
    for e, day in zip(emails, days, strict=True):
        inbox.deliver(e.raw, day)
    inbox.deliver(small_message(2), days[-1])
    inbox.expunge(inbox.deliver(small_message(3), days[-1]))
    return inbox, since, emails


def cursor(env) -> str | None:
    led = Ledger.open(env.root / "data" / "ledger.db")
    try:
        return led.kv.get(imapmod.KV_LAST_UID)
    finally:
        led.close()


def test_mail_backfill_counts_then_reads_older_mail_and_leaves_the_cursor(
    env, monkeypatch, capsys
):
    """A new install starts at now, so the mailbox's older mail is not read. The backfill counts
    the messages since a day up to the cursor, reading none; with --apply it reads them as mail —
    the corpus's emails filed, the stranger's rejected, each copied into its label — and leaves
    the cursor, so a message that came after the start is left to the poll. Run again, it reads
    nothing twice."""
    inbox, since, emails = older_mail()
    opened(monkeypatch, inbox)
    assert cli.main(["poll"]) == 0
    assert cursor(env) == "6" and inbox.fetched == []
    later = inbox.deliver(small_message(4), since)  # after the start: the poll's
    capsys.readouterr()
    day = since.isoformat()
    assert cli.main(["mail", "backfill", "--since", day]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"backfill: 4 messages since {day} would be read; run again with --apply"
    ]
    assert inbox.fetched == [] and inbox.copies == [] and env.server.requests == []
    for e in emails[:2]:
        env.server.answer(e.answer)
    with util.clock_at(corpus.sent(emails[-1])):
        assert cli.main(["mail", "backfill", "--since", day, "--apply"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"backfill: 4 messages since {day} read: {{'processed': 2, 'waiting': 1, 'rejected': 1}}"
    ]
    assert inbox.fetched == [2, 3, 4, 5] and later not in inbox.fetched
    assert inbox.copies == [
        (2, imapmod.LABEL_PROCESSED),
        (3, imapmod.LABEL_PROCESSED),
        (5, imapmod.LABEL_REJECTED),
    ]
    assert cursor(env) == "6" and inbox.closed == 3
    led = Ledger.open(env.root / "data" / "ledger.db")
    rows = [led.messages.by_sha(sha(e.raw)) for e in emails]
    assert [(r["source"], r["mail_uid"], r["status"]) for r in rows] == [
        ("imap", 2, "processed"),
        ("imap", 3, "processed"),
        ("imap", 4, "waiting"),
    ]
    led.close()
    inbox.fetched.clear()
    with util.clock_at(corpus.sent(emails[-1])):  # the same day: the cap is still reached
        assert cli.main(["mail", "backfill", "--since", day, "--apply"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"backfill: 4 messages since {day} read: {{'skipped': 3, 'waiting': 1}}"
    ]
    assert len(inbox.copies) == 3 and len(env.server.requests) == 2


def test_backfill_reads_inside_the_cap(env, monkeypatch, capsys):
    """The backfill asks the model under the same cap as the poll: two calls a day here, so of
    three emails the third waits, unlabelled, and the first poll of the next day reads it."""
    inbox, since, emails = older_mail()
    opened(monkeypatch, inbox)
    at = corpus.sent(emails[-1])
    with util.clock_at(at):
        assert cli.main(["poll"]) == 0
        for e in emails[:2]:
            env.server.answer(e.answer)
        assert cli.main(["mail", "backfill", "--since", since.isoformat(), "--apply"]) == 0
    assert len(env.server.requests) == 2 and (4, imapmod.LABEL_PROCESSED) not in inbox.copies
    led = Ledger.open(env.root / "data" / "ledger.db")
    row = led.messages.by_sha(sha(emails[2].raw))
    assert (row["status"], row["error"]) == ("waiting", "over_budget")
    led.close()
    env.server.answer(emails[2].answer)
    with util.clock_at(at + DAY):
        assert cli.main(["poll"]) == 0
    assert len(env.server.requests) == 3
    led = Ledger.open(env.root / "data" / "ledger.db")
    assert led.messages.get(row["id"])["status"] == "processed" and led.messages.waiting() == {}
    led.close()
    assert cursor(env) == "6"


def test_backfill_refusals(env, monkeypatch, capsys):
    """With mail off, a --since that is no day, or the network refused, the command says so in
    one line and opens no mailbox (exit 2)."""

    def never(*args, **kwargs):
        raise AssertionError("a mailbox was opened")

    monkeypatch.setattr(imapmod, "ImapInbox", never)
    usage = "usage: tripbox mail backfill --since YYYY-MM-DD [--apply]"
    for argv in (
        ["mail", "backfill"],
        ["mail", "backfill", "--since", "2027-02-30"],
        ["mail", "backfill", "--since", "2027-6-1", "--apply"],
        ["mail", "backfill", "--since", "yesterday"],
    ):
        assert cli.main(argv) == 2, argv
        assert capsys.readouterr().err.splitlines() == [usage], argv
    day = ["mail", "backfill", "--since", "2027-06-01", "--apply"]
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    assert cli.main(day) == 2
    assert capsys.readouterr().err == "TRIPBOX_NO_NETWORK is set: mail backfill needs the network\n"
    monkeypatch.delenv("TRIPBOX_NO_NETWORK")
    for name in MAILBOX:
        monkeypatch.delenv(name)
    assert cli.main(day) == 2
    assert capsys.readouterr().err.splitlines() == [
        "mail is off: TRIPBOX_MAIL_HOST, TRIPBOX_MAIL_USER, TRIPBOX_MAIL_PASSWORD not set"
    ]
    monkeypatch.setenv("TRIPBOX_MAIL_HOST", "imap.gmail.com")
    assert cli.main(day) == 2
    err = capsys.readouterr().err.splitlines()  # the log's lines say them too
    for key in ("TRIPBOX_MAIL_USER", "TRIPBOX_MAIL_PASSWORD"):
        fix = "mail is half configured; set it, or remove TRIPBOX_MAIL_HOST"
        assert f"{key}: not set — {fix}" in err, key
