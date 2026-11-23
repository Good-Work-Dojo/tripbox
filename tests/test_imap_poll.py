# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Reading a mailbox (`mail.imap`, `pipeline.run.poll`): where a new install starts, what a
changed UIDVALIDITY reads again, how handled mail is filed (or not), and the size cap. Every
mailbox is `helpers.inbox.FakeInbox` or, for the IMAP adapter itself, a stand-in for imap-tools'
MailBox: nothing opens a socket."""

import datetime as dt
import logging
from types import SimpleNamespace

import imap_tools
from helpers.eml import make_eml
from helpers.inbox import FakeInbox, small_message
from test_budget import Told, the_corpus

from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.mail.imap import (
    ALL_LABELS,
    KV_LAST_UID,
    KV_UIDVALIDITY,
    LABEL_FAILED,
    LABEL_PROCESSED,
    LABEL_REJECTED,
    MAX_HEADER_BYTES,
    MAX_MESSAGE_BYTES,
    fetch_new,
    label,
    mark_seen_uid,
)
from tripbox.notify.notifier import Notifier
from tripbox.parse import pipeline as pl
from tripbox.pipeline import run as runmod
from tripbox.util import clock_at

OWNER = "alex@example.com"


def reading(tmp_path, monkeypatch, **environ):
    """A pipeline context over a new ledger that reads mail from OWNER under `environ`: no model,
    no flight log, no covers, every notice kept by `ctx.told`. The structured-data extractor
    finds nothing, so the owner's own mail waits for a model."""
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [])
    env = {
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(tmp_path / "config"),
        "TRIPBOX_MAIL_ALLOWED_SENDERS": OWNER,
        **environ,
    }
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    ctx = runmod.build_context(Settings.from_env(env), with_model=False, with_notifier=False)
    ctx.told = Told()
    ctx.notifier = Notifier([ctx.told])
    return ctx


def owners_mail(n: int) -> bytes:
    """The owner's own message number `n`, with no Gmail stamp: read in `mailbox` mode only."""
    return make_eml(from_=OWNER, subject=f"Fwd: booking {n}", text=f"booking {n}").as_bytes()


def test_fetch_new_yields_only_uids_after_last(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    inbox = FakeInbox(messages={200: b"a", 201: b"b", 202: b"c"}).new_to(led)
    got = [m.uid for m in fetch_new(inbox, led)]
    assert got == [200, 201, 202]
    mark_seen_uid(led, 202)
    inbox.messages[203] = b"d"
    assert [m.uid for m in fetch_new(inbox, led)] == [203]
    assert led.kv.get(KV_UIDVALIDITY) == "1"


def noon(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(12), tzinfo=dt.UTC)


def test_a_new_install_starts_at_now_and_reads_nothing_older(tmp_path, monkeypatch, caplog):
    """A mailbox read for the first time is read from now on: thousands of older messages are
    neither fetched nor sized, the start is said once, and a message that arrives afterwards is
    read."""
    start = the_corpus().anchor
    inbox = FakeInbox.large(5000, start - dt.timedelta(days=60))
    ctx = reading(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger="tripbox.imap")
    with clock_at(noon(start)):
        summary = runmod.poll(ctx, inbox)
    assert (summary.fetched, summary.counts, inbox.fetched) == (0, {}, [])
    assert ctx.ledger.kv.get(KV_UIDVALIDITY) == "1" and ctx.ledger.kv.get(KV_LAST_UID) == "5000"
    said = [r.getMessage() for r in caplog.records if r.name == "tripbox.imap"]
    assert said == ["mail: starting at now; older mail is read only by tripbox mail backfill"]
    caplog.clear()
    uid = inbox.deliver(small_message(5001), start)
    with clock_at(noon(start) + dt.timedelta(minutes=5)):
        summary = runmod.poll(ctx, inbox)
    assert (summary.fetched, summary.counts, inbox.fetched) == (1, {"rejected": 1}, [uid])
    assert [r for r in caplog.records if r.name == "tripbox.imap"] == []


def test_a_changed_uidvalidity_rereads_only_since_the_last_good_poll(tmp_path, monkeypatch):
    """The server starts the mailbox over, every message under a new UID. Only what arrived
    since the day before the last good poll is fetched again: what the ledger holds of it is
    known by its bytes and filed no second time, what came after that poll is read, and the
    cursor ends at the top. Thousands of older messages are never fetched."""
    start = the_corpus().anchor
    days = [start + dt.timedelta(days=n) for n in range(30)]
    inbox = FakeInbox.large(2000, days[0])  # 100 a day, up to days[19]
    ctx = reading(tmp_path, monkeypatch)
    with clock_at(noon(days[20])):
        runmod.poll(ctx, inbox)  # it starts at now
    n = 2000
    for day in days[20:25]:  # ten a day, each read by that day's poll
        for _ in range(10):
            n += 1
            inbox.deliver(small_message(n), day)
        with clock_at(noon(day)):
            assert runmod.poll(ctx, inbox).counts == {"rejected": 10}
    for _ in range(5):  # after the last good poll, and never read
        n += 1
        inbox.deliver(small_message(n), days[24])
    assert len(inbox.copies) == 50 and len(inbox.fetched) == 50
    inbox.renumber(2)
    inbox.fetched.clear()
    with clock_at(noon(days[26])):
        summary = runmod.poll(ctx, inbox)
    since = inbox.uids_since(days[23])  # the day before the last good poll, on
    assert len(since) == 25 and inbox.fetched == since
    assert (summary.fetched, summary.counts) == (25, {"skipped": 20, "rejected": 5})
    assert len(inbox.copies) == 55 and inbox.copies[50:] == [
        (uid, LABEL_REJECTED) for uid in since[-5:]
    ]
    assert ctx.ledger.kv.get(KV_UIDVALIDITY) == "2"
    assert ctx.ledger.kv.get(KV_LAST_UID) == str(inbox.uidnext() - 1) == "2055"


def test_a_changed_uidvalidity_with_no_good_poll_reads_from_now(tmp_path):
    """With no poll that ever succeeded there is no day to read again from: the mailbox is read
    from now on, as a new install's is."""
    led = Ledger.open(tmp_path / "ledger.db")
    led.kv.set(KV_UIDVALIDITY, "1")
    led.kv.set(KV_LAST_UID, "9000")
    inbox = FakeInbox.large(300, the_corpus().anchor, uidvalidity=2)
    assert list(fetch_new(inbox, led)) == [] and led.kv.get(KV_LAST_UID) == "300"
    uid = inbox.deliver(small_message(301))
    assert [m.uid for m in fetch_new(inbox, led)] == [uid] and led.kv.get(KV_UIDVALIDITY) == "2"


def test_an_oversize_message_is_never_fetched_and_is_said_once(tmp_path, monkeypatch):
    """A message over 20 MB from an allowed sender is never fetched, only its header: it counts
    as oversize, one notice says so, it is filed as failed, and the cursor moves past it, so no
    later poll says it again. One at the limit is read."""
    ctx = reading(
        tmp_path, monkeypatch, TRIPBOX_MAIL_TRUST="mailbox", TRIPBOX_MAIL_FILING="labels"
    )
    inbox = FakeInbox()
    runmod.poll(ctx, inbox)
    big = inbox.deliver(owners_mail(1))
    inbox.sizes[big] = 25 * 1024 * 1024
    edge = inbox.deliver(small_message(2))
    inbox.sizes[edge] = MAX_MESSAGE_BYTES
    summary = runmod.poll(ctx, inbox)
    assert (summary.fetched, summary.counts) == (1, {"oversize": 1, "rejected": 1})
    assert inbox.fetched == [edge] and inbox.headers == [big]
    assert inbox.copies == [(big, LABEL_FAILED), (edge, LABEL_REJECTED)]
    said = ["tripbox left a message of 25 MB unread: it is over the 20 MB limit."]
    assert ctx.told.of("failure") == said
    assert ctx.ledger.kv.get(KV_LAST_UID) == str(edge)
    assert [r["mail_uid"] for r in ctx.ledger.conn.execute("SELECT mail_uid FROM messages")] == [
        edge
    ]
    summary = runmod.poll(ctx, inbox)
    assert (summary.fetched, summary.counts, ctx.told.of("failure")) == (0, {}, said)


def test_a_stranger_s_oversize_message_is_rejected_quietly(tmp_path, monkeypatch):
    """Anyone can send a message over 20 MB: its header alone is fetched, and one from no
    allowed sender is rejected as any stranger's mail is, telling nobody. In `gmail` mode the
    owner's address with no Gmail stamp is no allowed sender either."""
    for trust, sender_mail in (
        ("gmail", small_message(1)),
        ("mailbox", small_message(1)),
        ("gmail", owners_mail(1)),
    ):
        ctx = reading(
            tmp_path / f"{trust}{len(sender_mail)}",
            monkeypatch,
            TRIPBOX_MAIL_TRUST=trust,
            TRIPBOX_MAIL_FILING="labels",
        )
        inbox = FakeInbox()
        runmod.poll(ctx, inbox)
        big = inbox.deliver(sender_mail)
        inbox.sizes[big] = 25 * 1024 * 1024
        summary = runmod.poll(ctx, inbox)
        assert (summary.fetched, summary.counts) == (0, {"rejected": 1}), trust
        assert inbox.fetched == [] and inbox.headers == [big], trust
        assert inbox.copies == [(big, LABEL_REJECTED)], trust
        assert ctx.told.notices == [], trust
        assert ctx.ledger.kv.get(KV_LAST_UID) == str(big), trust


def test_label_never_raises(tmp_ledger_path):
    inbox = FakeInbox()
    inbox.ensure_folders(ALL_LABELS)
    assert "tripbox/processed" in inbox.folders
    label(inbox, 5, "tripbox/processed")
    assert inbox.copies == [(5, "tripbox/processed")]
    inbox.fail_copy = True
    label(inbox, 6, "tripbox/failed")  # swallowed, logged


def test_the_imap_inbox_connects_with_a_socket_timeout(monkeypatch):
    """A mail server that stops answering must not wedge the worker: it starts polls one
    after another, so a poll stuck in a socket read would stop every future poll."""
    calls = {}

    class FakeMailBox:
        def __init__(self, host, **kw):
            calls["init"] = (host, kw)

        def login(self, user, password, initial_folder="INBOX"):
            calls["login"] = (user, initial_folder)
            return self

    monkeypatch.setattr(imap_tools, "MailBox", FakeMailBox)
    from tripbox.mail.imap import IMAP_TIMEOUT, ImapInbox

    ImapInbox("imap.gmail.com", "u", "p")
    assert calls["init"] == ("imap.gmail.com", {"timeout": IMAP_TIMEOUT})
    assert calls["login"] == ("u", "INBOX") and 30 <= IMAP_TIMEOUT <= 120


class StandInMailBox:
    """imap-tools' MailBox as a server answers it: its folders, each listed with the server's
    own hierarchy separator, the folders created and the copies made."""

    def __init__(self, separator: str = "/", folders: list[str] | None = None) -> None:
        self.separator = separator
        self.names = list(folders or ["INBOX"])
        self.created: list[str] = []
        self.copied: list[tuple[str, str]] = []
        self.searched: list[str] = []
        self.folder = SimpleNamespace(list=self._list, create=self._create, status=self._status)
        self.client = SimpleNamespace(uid=self._uid)

    def _status(self, folder, options):
        return {"UIDNEXT": 5001, "UIDVALIDITY": 7}

    def uids(self, criteria):
        self.searched.append(str(criteria))
        return ["12", "4", "9"]

    def _uid(self, command, uid, what):
        if what == f"(BODY.PEEK[HEADER]<0.{MAX_HEADER_BYTES}>)":
            head = b"From: a@example.net\r\nSubject: big\r\n\r\n"
            return "OK", [(f"3 (UID {uid} BODY[HEADER]<0> {{{len(head)}}}".encode(), head), b")"]
        assert (command, what) == ("FETCH", "(RFC822.SIZE)")
        return "OK", [f"3 (UID {uid} RFC822.SIZE 4567)".encode()]

    def login(self, user, password, initial_folder="INBOX"):
        return self

    def _list(self, *args, **kwargs):
        return [imap_tools.FolderInfo(name, self.separator, ()) for name in self.names]

    def _create(self, name):
        self.created.append(name)
        self.names.append(name)

    def copy(self, uid, folder):
        self.copied.append((uid, folder))


def test_labels_use_the_servers_folder_separator(monkeypatch):
    """A label is written with the server's own hierarchy separator: on a server that separates
    with a dot, `tripbox/processed` is the folder `tripbox.processed`, created once."""
    from tripbox.mail.imap import ImapInbox

    for separator, folders in ((".", ["INBOX", "tripbox.processed"]), ("/", ["INBOX"])):
        server = StandInMailBox(separator, folders)
        monkeypatch.setattr(imap_tools, "MailBox", lambda host, server=server, **kw: server)
        inbox = ImapInbox("imap.example.com", "u", "p")
        inbox.ensure_folders(ALL_LABELS)
        inbox.ensure_folders(ALL_LABELS)
        on_server = [name.replace("/", separator) for name in ALL_LABELS]
        assert server.created == [n for n in on_server if n not in folders], separator
        inbox.copy_to(7, LABEL_PROCESSED)
        inbox.copy_to(8, LABEL_FAILED)
        assert server.copied == [("7", on_server[0]), ("8", on_server[2])], separator


def test_filing_none_touches_no_folder(tmp_path, monkeypatch):
    """With filing off, a poll makes no folder and copies nothing: a server that refuses every
    folder call reads its mail all the same. With labels, the same server fails the poll."""
    ctx = reading(tmp_path / "none", monkeypatch, TRIPBOX_MAIL_FILING="none")
    inbox = FakeInbox()
    inbox.fail_folders = True
    runmod.poll(ctx, inbox)
    inbox.deliver(small_message(1))
    inbox.deliver(small_message(2))
    summary = runmod.poll(ctx, inbox)
    assert (summary.fetched, summary.counts, summary.errors) == (2, {"rejected": 2}, 0)
    assert inbox.folder_calls == [] and inbox.folders == set() and inbox.copies == []
    ctx = reading(tmp_path / "labels", monkeypatch, TRIPBOX_MAIL_FILING="labels")
    try:
        runmod.poll(ctx, inbox)
    except OSError:
        pass
    else:
        raise AssertionError("labels filed into a server that refuses folders")
    assert inbox.folder_calls == [("ensure", ALL_LABELS)]


def test_a_skipped_message_is_not_filed_again(tmp_path, monkeypatch):
    """A message whose bytes the ledger already holds is skipped, and its copy in the mailbox is
    not filed again: a label is copied once per message read."""
    ctx = reading(tmp_path, monkeypatch)
    inbox = FakeInbox()
    runmod.poll(ctx, inbox)
    first = inbox.deliver(small_message(1))
    assert runmod.poll(ctx, inbox).counts == {"rejected": 1}
    assert inbox.copies == [(first, LABEL_REJECTED)]
    inbox.deliver(small_message(1))
    summary = runmod.poll(ctx, inbox)
    assert (summary.fetched, summary.counts) == (1, {"skipped": 1})
    assert inbox.copies == [(first, LABEL_REJECTED)]
    assert ctx.settings.mail_filing == "labels"  # unset, with Gmail's own check


def test_the_imap_inbox_reads_uidnext_since_and_sizes(monkeypatch):
    """The adapter asks the server for UIDNEXT, searches with SINCE (ascending UIDs back) and
    reads a message's size from its RFC822.SIZE without fetching it."""
    from tripbox.mail.imap import ImapInbox

    server = StandInMailBox()
    monkeypatch.setattr(imap_tools, "MailBox", lambda host, **kw: server)
    inbox = ImapInbox("imap.example.com", "u", "p")
    assert (inbox.uidnext(), inbox.uidvalidity()) == (5001, 7)
    day = the_corpus().anchor
    assert inbox.uids_since(day) == [4, 9, 12]
    assert server.searched == [f"(SINCE {day.day}-{day:%b}-{day.year})"]
    assert inbox.size(9) == 4567
    assert inbox.fetch_header(9) == b"From: a@example.net\r\nSubject: big\r\n\r\n"
