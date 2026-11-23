# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The worker (`tripbox work`): one long-lived process that beats every few seconds. A beat
writes the status record the page reads, takes one waiting upload through the pipeline, runs a
sync pass with the flight log, and starts `poll` and `enrich` as processes of their own when
they are due. Every email, text and PDF is the corpus's own, read at run time; the model answers
from the corpus, the flight log is held in memory, and the clock, the sleep and the processes the
worker starts are stand-ins: nothing here starts a process, sleeps or opens a socket."""

import datetime as dt
import email
import email.message
import email.policy
import email.utils
import functools
import hashlib
import html
import json
import logging
import os
import re
import signal
import socket
import sys
from zoneinfo import ZoneInfo

import pytest
from helpers.flightlog import Scripted, seed
from helpers.web import ORIGIN
from starlette.testclient import TestClient
from test_web_uploads import SAME_SITE, page_for

from tripbox import cli, corpus, status, uploads, util, worker
from tripbox.ledger import Ledger
from tripbox.notify.notifier import Notifier
from tripbox.parse import pipeline as pl
from tripbox.parse.models import to_extraction
from tripbox.parse.provider import ModelError, Usage
from tripbox.pipeline import run
from tripbox.trips.store import TripStore, stamp
from tripbox.validate.refdata import Airports
from tripbox.write.executor import Executor
from tripbox.write.sync import Syncer

NOT_RUNNING = "The worker is not running; this waits until it is."
SOON = "It is read within a few seconds."
# A flight log kept and sent to (one held in memory: these are never reached).
FLIGHT_LOG = {
    "TRIPBOX_AIRTRAIL_URL": "http://flightlog.example:3000",
    "TRIPBOX_AIRTRAIL_API_KEY": "placeholder",
    "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
}


@functools.cache
def the_corpus() -> corpus.Corpus:
    return corpus.load(corpus.default_dir())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def squeezed(text: str) -> str:
    return " ".join(text.split())


def plain_text(raw: bytes) -> str | None:
    """The plain text of an email's body, as a paste of it carries it (each line ended by a line
    feed); None for one with none."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain",))
    return body.get_content().replace("\r\n", "\n") if body is not None else None


def pdfs(raw: bytes) -> list[tuple[str | None, bytes]]:
    """The PDFs an email carries: each one's file name and bytes."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return [
        (part.get_filename(), part.get_payload(decode=True))
        for part in msg.walk()
        if part.get_content_type() == "application/pdf"
    ]


def an_entry(test) -> corpus.Entry:
    """The first corpus entry the model reads (it has an answer) that passes `test`."""
    for entry in the_corpus().entries:
        if entry.answer is not None and test(entry):
            return entry
    raise AssertionError("the corpus holds no such entry")


def filed_flights(entry: corpus.Entry, people) -> bool:
    """The entry files flights alone, with no check, each naming whom `people` accepts."""
    items = entry.outcome["items"]
    return (
        entry.outcome["status"] == "processed"
        and bool(items)
        and all(
            i["kind"] == "flight"
            and i["result"] == "filed"
            and not i["booking"]["checks"]
            and people(i["people"])
            for i in items
        )
    )


def owners_flights() -> corpus.Entry:
    return an_entry(lambda e: filed_flights(e, lambda p: p["me"] >= 1))


def a_companions_flights() -> corpus.Entry:
    return an_entry(
        lambda e: filed_flights(e, lambda p: p["me"] == 0 and p["guests"] >= 1 and not p["others"])
    )


def a_paste() -> tuple[corpus.Entry, str]:
    """An entry whose bookings are all filed into trips, and its plain text, as pasted: its first
    lines run on to a blank line, as an email's headers would."""

    def pasted(e: corpus.Entry) -> bool:
        text = plain_text(e.raw) or ""
        return (
            uploads.sniff(text.encode()) == "text"
            and bool(text.split("\n", 1)[0].strip())
            and "\n\n" in text.strip()[:4000]
        )

    entry = an_entry(
        lambda e: e.outcome["status"] == "processed"
        and all(i["result"] == "filed" and i["booking"]["trip"] for i in e.outcome["items"])
        and pasted(e)
    )
    return entry, plain_text(entry.raw)


class Reads:
    """The model, answered from the corpus. Like the real one it reads a message's text and its
    attachments, never its raw bytes: it answers for the entry whose PDF the message carries, or
    whose plain text the message's text holds whole (its email's body, or a paste of it), and
    has no answer for anything else — so an email read empty is answered by nothing.
    `confidence` replaces the answer's own; `reading` is called while it reads (a reading that
    takes long)."""

    model = "corpus"

    def __init__(self, confidence: float | None = None, reading=None) -> None:
        self.confidence = confidence
        self.reading = reading
        self.pdfs: dict[str, corpus.Entry] = {}
        self.texts: list[tuple[str, corpus.Entry]] = []
        for entry in the_corpus().entries:
            if entry.answer is None:
                continue
            for _, pdf in pdfs(entry.raw):
                self.pdfs[sha(pdf)] = entry
            text = plain_text(entry.raw)
            if text and text.strip():
                self.texts.append((squeezed(text), entry))

    def extract(self, nm, hint=None):
        if self.reading is not None:
            self.reading()
        attached = [self.pdfs[sha(a.data)] for a in nm.attachments if sha(a.data) in self.pdfs]
        found = attached[0] if attached else None
        if found is None:
            read = squeezed(nm.text or "")
            found = next((e for text, e in self.texts if text in read), None)
        if found is None:
            raise ModelError("invalid", "the corpus has no answer for this message")
        ex = to_extraction(found.answer)
        if self.confidence is not None:
            ex.confidence = self.confidence
        return ex, Usage(self.model, 0, 0, 0.0)


def context(tmp_path, *, flight_log=False, model=True, **environ) -> run.Context:
    """A worker's context over the ledger in `tmp_path`, under the corpus's settings and
    `environ`: with `flight_log` one is kept, held in memory (`ctx.airtrail` records each request
    and fails one on cue); with `model` the corpus answers for the model."""
    s = corpus.settings(
        the_corpus(),
        data_dir=tmp_path / "data",
        archive_dir=tmp_path / "archive",
        logs_dir=tmp_path / "logs",
        environ={**(FLIGHT_LOG if flight_log else {}), **environ},
    )
    led = Ledger.open(s.ledger_path)
    trips = TripStore(led, s.home_airports, book=s.travellers)
    log = None
    if flight_log:
        seed(led, the_corpus())
        log = Scripted(led)
    led.kv.set("airtrail", "on" if flight_log else "off")
    return run.Context(
        settings=s,
        ledger=led,
        book=s.travellers,
        refdata=Airports(),
        model=Reads() if model else None,
        airtrail=log,
        trips=trips,
        executor=Executor(ledger=led, store=trips, book=s.travellers),
        notifier=Notifier(),
        syncer=(
            Syncer(store=trips, ledger=led, book=trips.book, client=log, refs=None, dry_run=False)
            if log is not None
            else None
        ),
    )


class Clock:
    """The worker's clock: it moves only when told to, or when the worker sleeps."""

    def __init__(self, at: dt.datetime) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def tick(self, seconds: float) -> None:
        self.at += dt.timedelta(seconds=seconds)

    def to(self, at: dt.datetime) -> None:
        self.at = at


class Child:
    """A process the worker started: it runs until it is ended here, killed or terminated."""

    def __init__(self, argv: list[str], clock: Clock, *, deaf: bool = False) -> None:
        self.argv = argv
        self.clock = clock
        self.deaf = deaf  # killing it does not end it
        self.kills = 0
        self.started = clock()
        self.returncode: int | None = None
        self.ended: dt.datetime | None = None
        self.how: str | None = None

    @property
    def command(self) -> str:
        return self.argv[-1]

    def end(self, code: int, how: str = "exit") -> None:
        if self.returncode is None:
            self.returncode, self.ended, self.how = code, self.clock(), how

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        if not self.deaf:
            self.end(-signal.SIGKILL, "kill")

    def terminate(self) -> None:
        self.end(-signal.SIGTERM, "terminate")

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


class Spawner:
    """`spawn`: records each child it starts; `exit` ends each one at once with that code,
    `deaf` makes each one go on when it is killed, `broken` makes starting one fail."""

    def __init__(
        self, clock: Clock, *, exit: int | None = None, deaf: bool = False, broken: bool = False
    ) -> None:
        self.clock = clock
        self.exit = exit
        self.deaf = deaf
        self.broken = broken
        self.children: list[Child] = []

    def __call__(self, argv: list[str]) -> Child:
        if self.broken:
            raise OSError("no process can be started")
        child = Child(argv, self.clock, deaf=self.deaf)
        if self.exit is not None:
            child.end(self.exit)
        self.children.append(child)
        return child

    def of(self, command: str) -> list[Child]:
        return [c for c in self.children if c.command == command]


def a_worker(
    ctx: run.Context, at: dt.datetime, *, transport=None, **spawner
) -> tuple[worker.Worker, Clock, Spawner]:
    """A worker over `ctx` whose clock stands at `at`, and the processes it starts; `transport`
    answers the photo lookups."""
    clock = Clock(at)
    spawn = Spawner(clock, **spawner)
    made = worker.Worker(
        ctx, spawn=spawn, clock=clock, sleep=clock.tick, transport=transport
    )
    return made, clock, spawn


def beat(w: worker.Worker) -> None:
    """One beat, with the ledger's clock held at the worker's."""
    with util.clock_at(w.clock()):
        w.beat()


def start(w: worker.Worker) -> None:
    """The worker starts, with the ledger's clock held at the worker's."""
    with util.clock_at(w.clock()):
        w.start()


@pytest.fixture(autouse=True)
def no_extractor(monkeypatch):
    """The structured-data extractor is a process of its own: here it finds nothing, and the
    model reads every message."""
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [])


@pytest.fixture(autouse=True)
def handlers(monkeypatch) -> dict:
    """The signal handlers the worker installs, kept here instead of in the test process."""
    installed: dict = {}

    def install(signum, handler):
        installed[signum] = handler

    monkeypatch.setattr(signal, "signal", install)
    return installed


def submit(ctx: run.Context, body: bytes, name: str | None = None) -> str:
    return uploads.submit(ctx.ledger, body, name=name, submitted_by=None)[0]


def bookings(ctx: run.Context, message_id: int) -> list:
    ids = dict.fromkeys(
        r["trip_item_id"] for r in ctx.ledger.items.by_message(message_id) if r["trip_item_id"]
    )
    return [ctx.trips.item(i) for i in ids]


def values(record) -> list:
    """Every value a record holds, however deep."""
    if isinstance(record, dict):
        return [v for x in record.values() for v in values(x)]
    if isinstance(record, list):
        return [v for x in record for v in values(x)]
    return [record]


def test_a_beat_writes_the_status_record_with_codes_only(tmp_path):
    """The record names the process, its start and its last beat, what is on, and how each part
    last ended, in words: a pass that met a rejected key is `key_rejected`, one that met no
    answer `outage`, one that went through `done`. No value in it holds a space."""
    entry = owners_flights()
    ctx = context(tmp_path, flight_log=True)
    token = submit(ctx, entry.raw, "booking.eml")
    ctx.airtrail.fail_next(401, times=2)  # the pass after the message, and the beat's own
    at = corpus.sent(entry) + dt.timedelta(days=1)
    w, clock, spawn = a_worker(ctx, at)
    start(w)
    first = status.read(ctx.ledger)
    assert first["last"] == {
        "poll": None,
        "enrich": None,
        "sync": None,
        "uploads": None,
        "covers": None,
    }
    assert first["started_at"] == first["beat_at"] == stamp(at)
    clock.tick(5)
    beat(w)
    record = status.read(ctx.ledger)
    assert set(record) == {
        "pid",
        "host",
        "started_at",
        "beat_at",
        "busy",
        "stopped_at",
        "features",
        "last",
        "mailbox",
        "waiting",
        "dry_run",
    }
    assert (record["mailbox"], record["waiting"], record["dry_run"]) == (None, {}, False)
    assert (record["pid"], record["host"]) == (os.getpid(), socket.gethostname())
    assert (record["started_at"], record["beat_at"]) == (stamp(at), stamp(clock()))
    assert (record["busy"], record["stopped_at"]) == (False, None)  # between beats: not busy
    assert record["features"] == {
        "mail": "off",
        "model": "off",
        "airtrail": "on",
        "notifications": "off",
        "enrichment": "off",
    }
    beat_at = stamp(clock())
    assert record["last"] == {
        "uploads": {"at": beat_at, "ok": True, "code": "filed"},
        "sync": {"at": beat_at, "ok": False, "code": "key_rejected"},
        "poll": None,  # started, not ended
        "enrich": None,
        "covers": None,  # no photo waits and none is being looked up
    }
    assert ctx.ledger.uploads.get(token)["state"] == "filed"
    assert ctx.ledger.kv.get("airtrail_key_rejected") and ctx.airtrail.sent == []
    assert [c.argv for c in spawn.children] == [[sys.executable, "-m", "tripbox", "poll"]]
    words = [v for v in values(record) if isinstance(v, str)]
    assert words and not [v for v in words if re.search(r"\s", v)]
    assert {type(v) for v in values(record)} <= {str, int, bool, type(None)}
    # no answer: an outage; then the flights are sent
    ctx.airtrail.fail_next(503)
    clock.tick(5)
    beat(w)
    assert status.read(ctx.ledger)["last"]["sync"] == {
        "at": stamp(clock()),
        "ok": False,
        "code": "outage",
    }
    clock.tick(3600)
    beat(w)
    assert status.read(ctx.ledger)["last"]["sync"]["code"] == "done"
    assert len(ctx.airtrail.flights) == len(entry.outcome["items"])
    # the page reads it as it is stored: compact JSON
    text = ctx.ledger.kv.get(status.KV_WORKER)
    assert " " not in text and json.loads(text) == status.read(ctx.ledger)


class Unreachable:
    """The model, which cannot be reached."""

    model = "corpus"

    def extract(self, nm, hint=None):
        raise ModelError("unavailable", "no connection", word="unreachable")


class OverBudget:
    """The model, whose budget is used."""

    model = "corpus"

    def extract(self, nm, hint=None):
        raise ModelError("over_budget", "this month's model budget is used", word="month")


MAILBOX = "trips@example.com"
MAIL = {
    "TRIPBOX_MAIL_HOST": "imap.trips.example",
    "TRIPBOX_MAIL_USER": MAILBOX,
    "TRIPBOX_MAIL_PASSWORD": "placeholder",
    "TRIPBOX_MAIL_TRUST": "mailbox",  # gmail trust needs Gmail's own mailbox
}


def test_the_record_names_the_mailbox_and_what_waits(tmp_path, caplog):
    """With mail on, the record names the address mail is read from, the one setting it
    carries; it counts the mail that waits for the model, by reason, and says whether the flight
    log is in a dry run. With mail off it names no address. No log line holds the address."""
    caplog.set_level(logging.DEBUG)
    first, *rest = [e for e in the_corpus().entries if e.answer is not None][:3]
    dry = {"TRIPBOX_AIRTRAIL_DRY_RUN": "true"}
    ctx = context(tmp_path, flight_log=True, **dry, **MAIL)
    at = corpus.sent(rest[-1]) + dt.timedelta(days=1)
    with util.clock_at(at):
        ctx.model = None
        run.process_raw(ctx, first.raw, source="upload", trusted=True, notify=False)
        ctx.model = Unreachable()
        for entry in rest:
            run.process_raw(ctx, entry.raw, source="upload", trusted=True, notify=False)
    w, clock, _ = a_worker(ctx, at)
    start(w)
    beat(w)
    record = status.read(ctx.ledger)
    assert record["mailbox"] == MAILBOX
    assert record["waiting"] == {"no_model": 1, "model_down": 2}
    assert record["dry_run"] is True
    assert MAILBOX not in caplog.text
    # once the model answers, what waited is read, and nothing waits
    ctx.model = Reads()
    with util.clock_at(clock()):
        assert run.resume_waiting(ctx) == 3
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    assert status.read(ctx.ledger)["waiting"] == {}
    # mail off, and a flight log that is sent to
    ctx = context(tmp_path / "off", flight_log=True)
    w, clock, _ = a_worker(ctx, at)
    start(w)
    record = status.read(ctx.ledger)
    assert (record["mailbox"], record["waiting"], record["dry_run"]) == (None, {}, False)


def test_the_record_names_a_mailbox_only_when_the_login_is_an_address(tmp_path):
    """The page tells the owner to forward confirmations to the mailbox, so the record names the
    mail login only when it is an address: one `@`, something before it, a dotted domain, no
    white space. Any other login is no mailbox to name."""
    logins = {
        MAILBOX: MAILBOX,
        "trips": None,
        "trips@example": None,
        "@example.com": None,
        "trips@@example.com": None,
        "trips sample@example.com": None,
        "trips@example.": None,
    }
    for n, (login, named) in enumerate(logins.items()):
        ctx = context(tmp_path / str(n), **{**MAIL, "TRIPBOX_MAIL_USER": login})
        assert ctx.settings.on("mail"), login
        w, _, _ = a_worker(ctx, corpus.sent(the_corpus().entries[0]))
        start(w)
        assert status.read(ctx.ledger)["mailbox"] == named, login


class CountedUnreachable(Unreachable):
    """The model, which cannot be reached; `calls` counts what it is asked."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, nm, hint=None):
        self.calls += 1
        return super().extract(nm, hint)


def test_when_the_resume_pass_stops_every_message_that_waits_takes_its_reason(tmp_path):
    """Three uploads wait for want of a model. One is set up, but it cannot be reached: the
    resume pass asks it once, for the oldest, which waits again, and stops there. A budget or an
    outage is the whole model's, so every message that waits, and each upload it ended as, now
    waits for that reason, and the card counts them under it."""
    from tripbox.web import card

    entries = [e for e in the_corpus().entries if e.answer is not None][:3]
    ctx = context(tmp_path, model=False)
    tokens = [submit(ctx, e.raw, f"b{n}.eml") for n, e in enumerate(entries)]
    at = corpus.sent(entries[-1]) + dt.timedelta(days=1)
    w, clock, _ = a_worker(ctx, at)
    start(w)
    for _ in tokens:
        clock.tick(worker.BEAT_SECONDS)
        beat(w)
    rows = [ctx.ledger.uploads.get(t) for t in tokens]
    assert [(r["state"], r["error"]) for r in rows] == [("nothing", "no_model")] * 3
    ctx.model = CountedUnreachable()
    with util.clock_at(clock()):
        assert run.resume_waiting(ctx) == 0
    assert ctx.model.calls == 1
    assert ctx.ledger.messages.waiting() == {"model_down": 3}
    rows = [ctx.ledger.uploads.get(t) for t in tokens]
    assert [(r["state"], r["error"]) for r in rows] == [("nothing", "model_down")] * 3
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    lines = [r["text"] for r in card.status_card(ctx.ledger, clock()) if r["label"] == "Model"]
    assert "3 waiting: the model cannot be reached." in lines
    assert not any("no model is set up" in line for line in lines)


def test_mail_that_waited_for_a_model_now_gone_waits_for_one_to_be_set_up(tmp_path):
    """An upload waits while the model's budget is used. Then no model is set up at all: once
    the worker starts again, the message and the upload wait for a model to be set up, as the
    record, the card's count and the status page say, not for a budget that no longer applies."""
    entry = owners_flights()
    ctx = context(tmp_path)
    ctx.model = OverBudget()
    token = submit(ctx, entry.raw, "booking.eml")
    at = corpus.sent(entry) + dt.timedelta(days=1)
    w, clock, _ = a_worker(ctx, at)
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"]) == ("nothing", "over_budget")
    budget = "The model's budget is used, so this waits. It is read when the budget allows."
    app, _ = page_for(
        tmp_path, data_dir=ctx.settings.data_dir, config_dir=ctx.settings.config_dir
    )
    page = TestClient(app, base_url=ORIGIN, headers=SAME_SITE)
    assert budget in html.unescape(page.get(f"/uploads/{token}").text)
    ctx.model = None  # the provider `none`
    w, clock, _ = a_worker(ctx, at + dt.timedelta(hours=1))
    start(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"]) == ("nothing", "no_model")
    assert ctx.ledger.messages.get(row["message_id"])["error"] == "no_model"
    assert status.read(ctx.ledger)["waiting"] == {"no_model": 1}
    shown = html.unescape(page.get(f"/uploads/{token}").text)
    assert budget not in shown
    assert "No model is set up, so only confirmations that carry structured data are read." in shown


def test_a_second_worker_refuses_to_start_while_one_beats(tmp_path):
    """While the record of another process beat less than thirty seconds ago, a second worker
    refuses and writes nothing; once that beat is 31 s old it starts and its record replaces
    the first's. A record of this very process (a restarted container keeps its pid and host)
    never holds it back."""
    ctx = context(tmp_path)
    at = corpus.sent(the_corpus().entries[0])
    first, clock, _ = a_worker(ctx, at)
    first.pid = os.getpid() + 1  # another process
    start(first)
    for _ in range(3):
        clock.tick(5)
        beat(first)
    last = clock()
    second = worker.Worker(context(tmp_path), spawn=Spawner(clock), clock=clock, sleep=clock.tick)
    for late in (0, 1, 29):
        clock.to(last + dt.timedelta(seconds=late))
        with pytest.raises(worker.WorkerRunning):
            start(second)
        assert status.read(ctx.ledger)["pid"] == first.pid
    assert status.alive(ctx.ledger, last + dt.timedelta(seconds=29))
    clock.to(last + dt.timedelta(seconds=31))
    assert not status.alive(ctx.ledger, clock())
    start(second)
    record = status.read(ctx.ledger)
    assert (record["pid"], record["started_at"]) == (os.getpid(), stamp(clock()))
    # the same process again, its last beat a moment ago: it starts
    clock.tick(5)
    start(worker.Worker(ctx, spawn=Spawner(clock), clock=clock, sleep=clock.tick))


def test_the_poll_starts_at_once_and_then_every_five_minutes_and_never_twice_at_a_time(tmp_path):
    """Over thirty-one minutes of beats with a poll that never ends by itself: started at once,
    killed at 900 s, started again at the next beat, and so on; never two at a time. One that
    ends by itself is recorded by its exit, and the next still waits for its five minutes."""
    ctx = context(tmp_path)
    at = corpus.sent(the_corpus().entries[0])
    w, clock, spawn = a_worker(ctx, at)
    start(w)
    while clock() <= at + dt.timedelta(minutes=31):
        beat(w)
        assert sum(c.returncode is None for c in spawn.of("poll")) <= 1
        clock.tick(worker.BEAT_SECONDS)
    polls = spawn.of("poll")
    assert spawn.children == polls  # enrichment is off
    assert [(c.started - at).total_seconds() for c in polls] == [0, 905, 1810]
    assert [(c.ended - at).total_seconds() for c in polls[:2]] == [900, 1805]
    assert [c.how for c in polls] == ["kill", "kill", None]
    assert {tuple(c.argv) for c in polls} == {(sys.executable, "-m", "tripbox", "poll")}
    last = status.read(ctx.ledger)["last"]["poll"]
    assert last == {"at": stamp(at + dt.timedelta(seconds=1805)), "ok": False, "code": "timeout"}
    assert (worker.POLL_SECONDS, worker.POLL_TIMEOUT, worker.BEAT_SECONDS) == (300, 900, 5)
    # a poll that ends by itself
    ctx = context(tmp_path / "ends")
    w, clock, spawn = a_worker(ctx, at, exit=1)
    start(w)
    for _ in range(4):
        beat(w)
        clock.tick(worker.BEAT_SECONDS)
    assert len(spawn.of("poll")) == 1
    assert status.read(ctx.ledger)["last"]["poll"] == {
        "at": stamp(at + dt.timedelta(seconds=5)),
        "ok": False,
        "code": "exit_1",
    }
    clock.to(at + dt.timedelta(seconds=300))
    beat(w)
    assert len(spawn.of("poll")) == 2
    # one that goes on after it is killed: no second one starts beside it until it has ended
    ctx = context(tmp_path / "deaf")
    w, clock, spawn = a_worker(ctx, at, deaf=True)
    start(w)
    while clock() <= at + dt.timedelta(minutes=16):
        beat(w)
        clock.tick(worker.BEAT_SECONDS)
    (poll,) = spawn.of("poll")
    assert poll.kills == 1 and poll.returncode is None
    assert status.read(ctx.ledger)["last"]["poll"]["code"] == "timeout"
    poll.end(-signal.SIGKILL)
    beat(w)
    assert len(spawn.of("poll")) == 2  # its end was said when it was killed
    assert status.read(ctx.ledger)["last"]["poll"]["code"] == "timeout"


def test_a_poll_stopped_twice_running_is_told_once_and_the_next_that_finishes_says_so(tmp_path):
    """The poll is stopped for its time twice running: one `mail_down` notice says since when
    no poll finished and after how long it was stopped; a third stop says nothing more. The next
    poll that finishes says the mailbox is read again, once."""
    from helpers.inbox import FakeInbox

    mail = {
        "TRIPBOX_MAIL_HOST": "imap.gmail.com",
        "TRIPBOX_MAIL_USER": "trips@example.com",
        "TRIPBOX_MAIL_PASSWORD": "placeholder",
    }
    ctx = context(tmp_path, **mail)
    assert ctx.settings.on("mail")
    told: list = []

    class Told:
        name = "told"

        def send(self, notice) -> None:
            told.append((notice.event, notice.body))

    ctx.notifier = Notifier([Told()])
    at = corpus.sent(the_corpus().entries[0])
    w, clock, spawn = a_worker(ctx, at)
    start(w)
    stopped = (
        "tripbox's mail check has not finished since it started: it was stopped after"
        f" {worker.POLL_TIMEOUT // 60} minutes."
    )
    while len([c for c in spawn.of("poll") if c.how == "kill"]) < 3:
        beat(w)
        if len([c for c in spawn.of("poll") if c.how == "kill"]) == 1:
            assert told == []
        clock.tick(worker.BEAT_SECONDS)
    assert told == [("mail_down", stopped)]
    # the next poll the worker starts finishes: it says so, once
    while len(spawn.of("poll")) < 4:
        beat(w)
        clock.tick(worker.BEAT_SECONDS)
    with util.clock_at(clock()):
        run.poll(ctx, FakeInbox(messages={}))  # what that poll does
        run.poll(ctx, FakeInbox(messages={}))
    spawn.of("poll")[-1].end(0)
    up = ("mail_up", "tripbox is reading the mailbox again")
    assert told == [("mail_down", stopped), up]
    # two stops running after it: told again, since when that poll finished
    while len([c for c in spawn.of("poll") if c.how == "kill"]) < 5:
        beat(w)
        assert len(told) == 2 or len([c for c in spawn.of("poll") if c.how == "kill"]) == 5
        clock.tick(worker.BEAT_SECONDS)
    since = stopped.replace("it started", ctx.ledger.kv.get("last_poll_ok"))
    assert told[2:] == [("mail_down", since)]


def test_enrichment_runs_once_a_night_at_half_past_three_in_the_configured_zone_and_only_when_it_is_on(  # noqa: E501
    tmp_path,
):
    zone = ZoneInfo(the_corpus().settings["TZ"])
    day = the_corpus().anchor
    nights = (day, day + dt.timedelta(days=1))

    def local(d: dt.date, h: int, m: int, s: int = 0) -> dt.datetime:
        return dt.datetime.combine(d, dt.time(h, m, s), tzinfo=zone).astimezone(dt.UTC)

    beats = [
        local(day, 2, 0),
        dt.datetime.combine(nights[1], dt.time(3, 30), tzinfo=dt.UTC),  # half past three in UTC
        local(day, 3, 29, 55),
        local(day, 3, 30),
        local(day, 3, 30, 5),
        local(day, 3, 44, 55),
        local(day, 3, 45),
        local(day, 12, 0),
        local(nights[1], 3, 29, 55),
        local(nights[1], 3, 30),
        local(nights[1], 3, 30, 5),
    ]
    beats.sort()

    def enriched(environ: dict) -> tuple[list[Child], worker.Worker]:
        ctx = context(tmp_path / str(len(environ)), flight_log=True, **environ)
        w, clock, spawn = a_worker(ctx, beats[0])
        start(w)
        for at in beats:
            clock.to(at)
            beat(w)
        return spawn.of("enrich"), w

    on, w = enriched({"TRIPBOX_ENRICH": "aeroapi", "TRIPBOX_AEROAPI_KEY": "placeholder"})
    assert w.ctx.settings.on("enrichment")
    assert [c.started for c in on] == [local(n, 3, 30) for n in nights]
    assert {tuple(c.argv) for c in on} == {(sys.executable, "-m", "tripbox", "enrich")}
    assert (on[0].ended, on[0].how) == (local(day, 3, 45), "kill")  # past its fifteen minutes
    assert worker.ENRICH_TIMEOUT == 900 and worker.NIGHTLY == dt.time(3, 30)
    off, w = enriched({})
    assert not w.ctx.settings.on("enrichment") and off == []
    off, w = enriched({"TRIPBOX_AEROAPI_KEY": "placeholder"})  # the key alone: off
    assert not w.ctx.settings.on("enrichment") and off == []
    assert status.read(w.ctx.ledger)["last"]["enrich"] is None


def test_a_failure_in_one_part_of_a_beat_does_not_stop_the_others(tmp_path, monkeypatch):
    entry = owners_flights()
    ctx = context(tmp_path, flight_log=True)
    token = submit(ctx, entry.raw, "booking.eml")
    at = corpus.sent(entry) + dt.timedelta(days=1)
    w, clock, spawn = a_worker(ctx, at, exit=0)
    start(w)

    def breaks(*args, **kwargs):
        raise RuntimeError("this part broke")

    # the waiting list cannot be read: the pass and the poll still run
    with monkeypatch.context() as m:
        m.setattr(ctx.ledger.uploads, "claim_next", breaks)
        beat(w)
    last = status.read(ctx.ledger)["last"]
    assert (last["uploads"]["code"], last["sync"]["code"]) == ("error", "done")
    assert len(spawn.of("poll")) == 1 and ctx.ledger.uploads.get(token)["state"] == "waiting"
    # the pass breaks: the upload is still read (its own pass sends nothing either)
    clock.tick(5)
    with monkeypatch.context() as m:
        m.setattr(ctx.syncer, "run_pass", breaks)
        beat(w)
    last = status.read(ctx.ledger)["last"]
    assert (last["uploads"]["code"], last["sync"]["code"]) == ("filed", "error")
    assert last["poll"]["code"] == "exit_0" and ctx.airtrail.sent == []
    # no child can be started: the pass sends what waited
    clock.tick(worker.POLL_SECONDS)
    spawn.broken = True
    beat(w)
    last = status.read(ctx.ledger)["last"]
    assert (last["poll"]["code"], last["sync"]["code"]) == ("error", "done")
    assert len(ctx.airtrail.flights) == len(entry.outcome["items"])
    # the next start waits its five minutes, and then it starts
    spawn.broken = False
    clock.tick(5)
    beat(w)
    assert len(spawn.of("poll")) == 1
    clock.tick(worker.POLL_SECONDS)
    beat(w)
    assert len(spawn.of("poll")) == 2


def test_an_upload_waits_while_no_worker_runs_and_is_read_when_one_starts(tmp_path):
    entry, text = a_paste()
    at = corpus.sent(entry)
    with util.clock_at(at):
        ctx = context(tmp_path)
        app, _ = page_for(
            tmp_path, data_dir=ctx.settings.data_dir, config_dir=ctx.settings.config_dir
        )
        page = TestClient(app, base_url=ORIGIN, headers=SAME_SITE)
        landed = page.post("/add/paste", data={"text": text}, follow_redirects=False)
        where = landed.headers["location"]
        token = where.removeprefix("/uploads/")
        waiting = page.get(where).text
        assert "Received." in waiting and NOT_RUNNING in waiting and SOON not in waiting
        w, _, _ = a_worker(ctx, at)
        start(w)
        assert SOON in page.get(where).text and NOT_RUNNING not in page.get(where).text
        beat(w)
        row = ctx.ledger.uploads.get(token)
        assert (row["state"], row["body"], row["error"]) == ("filed", None, None)
        assert ctx.ledger.messages.get(row["message_id"])["source"] == "upload"
        filed = bookings(ctx, row["message_id"])
        assert len(filed) == len(entry.outcome["items"])
        assert all(ti.trip_id is not None for ti in filed)
        shown = page.get(where).text
        assert "Filed" in shown and NOT_RUNNING not in shown and SOON not in shown
        links = re.findall(r'<a class="choice" href="([^"]+)"', shown)
        assert links == [f"/trips/{ti.trip_id}#item-{ti.id}" for ti in filed]
        assert all(page.get(link.split("#")[0]).status_code == 200 for link in links)
        # read again from the archive (the model asked again), the paste goes back through the
        # text path: read as an email it would hold no text at all
        again = run.process_raw(
            ctx, text.encode(), source="replay", force=True, notify=False, reread=True
        )
        assert [o.result for o in again.outcomes] == ["duplicate"] * len(filed)
        assert {o.trip_item_id for o in again.outcomes} == {ti.id for ti in filed}


def test_an_uploaded_email_is_the_owners_own_input(tmp_path):
    """No sender is allowed here, so the sender check rejects every email as mail. Handed in on
    the page, an email is the owner's own: read, and filed with no `unverified_sender` — also
    one the check had rejected as mail. That settles who sent it, not who is on each flight: a
    flight naming only a companion stays out of the flight log."""
    mine, theirs = owners_flights(), a_companions_flights()
    ctx = context(tmp_path, flight_log=True, TRIPBOX_MAIL_ALLOWED_SENDERS="")
    assert ctx.settings.allowed_senders == ()
    as_mail = run.process_raw(ctx, mine.raw, source="imap", notify=False)
    assert as_mail.status == "rejected" and as_mail.outcomes == []
    tokens = [submit(ctx, e.raw, "booking.eml") for e in (mine, theirs)]
    w, _, _ = a_worker(ctx, corpus.sent(theirs) + dt.timedelta(days=1))
    start(w)
    for _ in tokens:
        beat(w)
    rows = [ctx.ledger.uploads.get(t) for t in tokens]
    assert [r["state"] for r in rows] == ["filed", "filed"]
    assert rows[0]["message_id"] == as_mail.message_row_id  # the message rejected as mail
    owners, companions = (bookings(ctx, r["message_id"]) for r in rows)
    assert len(owners) == len(mine.outcome["items"])
    assert len(companions) == len(theirs.outcome["items"])
    for ti in owners + companions:
        assert ti.checks == () and "unverified_sender" not in (ti.payload.get("raised") or {})
    assert [ti.sync_state for ti in owners] == ["synced"] * len(owners)
    assert [ti.sync_state for ti in companions] == ["off"] * len(companions)
    sent = sorted(body["flightNumber"] for what, body in ctx.airtrail.sent)
    assert sent == sorted(ti.payload["flight_code"] for ti in owners)
    ctx.airtrail.requests.clear()
    assert run.sync(ctx).done == {} and ctx.airtrail.requests == []
    # the owner vouched for the bytes the check rejected: the message is theirs from now on
    assert ctx.ledger.messages.get(as_mail.message_row_id)["source"] == "upload"


def replayed(ctx: run.Context, raw: bytes) -> run.MessageResult:
    """`tripbox replay --force --reread` of one archived message: read again, the model asked
    again."""
    return run.process_raw(ctx, raw, source="replay", force=True, reread=True, notify=False)


def links(ctx: run.Context, token: str) -> list[str]:
    """The links an upload's status page shows."""
    app, _ = page_for(
        ctx.settings.data_dir.parent,
        data_dir=ctx.settings.data_dir,
        config_dir=ctx.settings.config_dir,
    )
    page = TestClient(app, base_url=ORIGIN, headers=SAME_SITE).get(f"/uploads/{token}")
    assert page.status_code == 200
    return re.findall(r'<a class="choice" href="([^"]+)"', page.text)


def test_a_replay_reads_an_email_the_owner_handed_in_as_their_own(tmp_path):
    """Whether a message goes through the sender check is what the ledger knows of it: one the
    owner handed in is read as theirs on every later reading, so `replay --force --reread` of an
    uploaded email the check would reject reads it whole again, and its status page keeps its
    links. Mail from the mailbox is checked on every reading: one rejected stays rejected."""
    mine, stranger = owners_flights(), a_companions_flights()
    ctx = context(tmp_path, TRIPBOX_MAIL_ALLOWED_SENDERS="")
    token = submit(ctx, mine.raw, "booking.eml")
    w, _, _ = a_worker(ctx, corpus.sent(mine) + dt.timedelta(days=1))
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    filed = bookings(ctx, row["message_id"])
    assert row["state"] == "filed" and filed
    shown = links(ctx, token)
    assert len(shown) == len(filed)
    with util.clock_at(w.clock()):
        again = replayed(ctx, mine.raw)
    assert again.status == "ignored"
    assert [o.result for o in again.outcomes] == ["duplicate"] * len(filed)
    assert links(ctx, token) == shown
    # a message from the mailbox stays behind the check, forced and re-read or not
    with util.clock_at(w.clock()):
        mailed = run.process_raw(ctx, stranger.raw, source="imap", notify=False)
        assert mailed.status == "rejected"
        assert replayed(ctx, stranger.raw).status == "rejected"
        plain = run.process_raw(ctx, stranger.raw, source="replay", notify=False)
    assert plain.status == "skipped"
    assert ctx.ledger.messages.get(mailed.message_row_id)["source"] == "imap"


def test_an_email_uploaded_with_no_model_is_filed_by_a_replay_once_one_is_set_up(tmp_path):
    """With no model, an uploaded email only a model reads waits (`nothing`, `no_model`). Once a
    model is set up, `tripbox replay` reads the message again as the owner's own: its bookings
    are filed, the upload is `filed`, and its status page links them."""
    entry = owners_flights()
    ctx = context(tmp_path, model=False, TRIPBOX_MAIL_ALLOWED_SENDERS="")
    token = submit(ctx, entry.raw, "booking.eml")
    w, _, _ = a_worker(ctx, corpus.sent(entry) + dt.timedelta(days=1))
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"]) == ("nothing", "no_model") and links(ctx, token) == []
    ctx.model = Reads()
    with util.clock_at(w.clock()):
        res = run.process_raw(ctx, entry.raw, source="replay", notify=False)
    assert res.status == "processed" and res.message_row_id == row["message_id"]
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"]) == ("filed", None)
    filed = bookings(ctx, row["message_id"])
    assert len(filed) == len(entry.outcome["items"])
    assert links(ctx, token) == [f"/trips/{ti.trip_id}#item-{ti.id}" for ti in filed]


def test_an_upload_counts_against_the_cap_and_is_filed_when_it_allows(tmp_path):
    """Uploads are read under the model's cap as mail is: with one call a day, the first upload
    is filed and the second waits (`nothing`, `over_budget`), its page saying why. The first poll
    of the next day (UTC) reads it: the upload is `filed`, its page linking its bookings."""
    from test_budget import UNPRICED, next_day

    from tripbox.parse import budget

    first, second = owners_flights(), a_companions_flights()
    ctx = context(
        tmp_path,
        TRIPBOX_LLM_API_KEY="placeholder",
        TRIPBOX_LLM_MODEL=UNPRICED,
        TRIPBOX_LLM_MAX_CALLS_PER_DAY="1",
    )
    ctx.model = budget.Budgeted(ctx.model, budget.Budget(ctx.ledger, ctx.settings))
    tokens = [submit(ctx, e.raw, f"b{n}.eml") for n, e in enumerate((first, second))]
    at = corpus.sent(first) + dt.timedelta(days=1)
    w, _, _ = a_worker(ctx, at)
    start(w)
    beat(w)
    beat(w)
    rows = [ctx.ledger.uploads.get(t) for t in tokens]
    assert [(r["state"], r["error"]) for r in rows] == [("filed", None), ("nothing", "over_budget")]
    assert ctx.ledger.llm.day_count(at.astimezone(dt.UTC).date().isoformat()) == 1
    assert ctx.ledger.messages.get(rows[1]["message_id"])["status"] == "waiting"
    app, _ = page_for(
        tmp_path, data_dir=ctx.settings.data_dir, config_dir=ctx.settings.config_dir
    )
    page = TestClient(app, base_url=ORIGIN, headers=SAME_SITE).get(f"/uploads/{tokens[1]}").text
    said = "The model's budget is used, so this waits. It is read when the budget allows."
    assert said in html.unescape(page)
    assert links(ctx, tokens[1]) == []
    with util.clock_at(next_day(at)):
        run.poll(ctx, None)
    row = ctx.ledger.uploads.get(tokens[1])
    assert (row["state"], row["error"]) == ("filed", None)
    filed = bookings(ctx, row["message_id"])
    assert len(filed) == len(second.outcome["items"])
    assert links(ctx, tokens[1]) == [f"/trips/{ti.trip_id}#item-{ti.id}" for ti in filed]


def test_an_upload_read_later_without_a_booking_says_what_became_of_it(tmp_path):
    """Two uploads wait while the model cannot be reached. Once it answers, the resume pass reads
    them: one holds no booking, and the model refuses the other. Each upload then says what a
    first reading ending so would have said, never that it still waits."""
    nothing, refused = an_entry(lambda e: e.outcome["status"] == "ignored"), owners_flights()
    ctx = context(tmp_path)
    reads = ctx.model

    class Down:
        model = "corpus"

        def extract(self, nm, hint=None):
            raise ModelError("unavailable", "no connection", word="unreachable")

    class Answers:
        """The corpus's answers, but the model refuses the second email."""

        model = "corpus"

        def extract(self, nm, hint=None):
            if nm.raw == refused.raw:
                raise ModelError("refused", "model refused the request")
            return reads.extract(nm, hint)

    ctx.model = Down()
    tokens = [submit(ctx, nothing.raw, "a.eml"), submit(ctx, refused.raw, "b.eml")]
    at = corpus.sent(refused) + dt.timedelta(days=1)
    w, _, _ = a_worker(ctx, at)
    start(w)
    beat(w)
    beat(w)
    rows = [ctx.ledger.uploads.get(t) for t in tokens]
    assert [(r["state"], r["error"]) for r in rows] == [("nothing", "model_down")] * 2
    waits = "The model could not be reached, so this waits."
    app, _ = page_for(
        tmp_path, data_dir=ctx.settings.data_dir, config_dir=ctx.settings.config_dir
    )
    client = TestClient(app, base_url=ORIGIN, headers=SAME_SITE)

    def page(token: str) -> str:
        return html.unescape(client.get(f"/uploads/{token}").text)

    assert all(waits in page(t) for t in tokens)
    ctx.model = Answers()
    with util.clock_at(at + dt.timedelta(minutes=5)):
        assert run.resume_waiting(ctx) == 2
    rows = [ctx.ledger.uploads.get(t) for t in tokens]
    assert [(r["state"], r["error"]) for r in rows] == [("nothing", None), ("failed", "unreadable")]
    assert "No booking found — fill it in by hand" in page(tokens[0])
    assert "This could not be read." in page(tokens[1])
    assert not any(waits in page(t) for t in tokens)


def test_an_upload_of_an_email_filed_before_under_its_message_id_links_those_bookings(tmp_path):
    """The owner saves an email the mailbox already brought (the same Message-ID, other bytes)
    and uploads it: it is set aside for the delivery that was read, and the upload is `filed`
    with that delivery's bookings on its status page."""
    entry = owners_flights()
    ctx = context(tmp_path)
    at = corpus.sent(entry) + dt.timedelta(days=1)
    with util.clock_at(at):
        mailed = run.process_raw(ctx, entry.raw, source="imap", notify=False)
    assert mailed.status == "processed"
    saved = b"X-Saved: by the owner\r\n" + entry.raw
    token = submit(ctx, saved, "saved.eml")
    w, _, _ = a_worker(ctx, at)
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["message_id"]) == ("filed", mailed.message_row_id)
    filed = bookings(ctx, mailed.message_row_id)
    assert filed and links(ctx, token) == [
        f"/trips/{ti.trip_id}#item-{ti.id}" for ti in filed
    ]


def test_an_upload_set_aside_for_an_earlier_delivery_is_filed_with_its_bookings(tmp_path):
    """The owner uploads a saved confirmation while the model cannot be reached: it waits. The
    mailbox then brings the same email (the same Message-ID, other bytes), which is read once
    the model answers. The resume pass sets the upload's message aside for that delivery, and
    the upload is `filed` with that delivery's bookings: its status page links them and no
    longer says it waits."""
    entry = owners_flights()
    ctx = context(tmp_path)
    ctx.model = Unreachable()
    saved = b"X-Saved: by the owner\r\n" + entry.raw
    token = submit(ctx, saved, "saved.eml")
    at = corpus.sent(entry) + dt.timedelta(days=1)
    w, _, _ = a_worker(ctx, at)
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"]) == ("nothing", "model_down")
    waited = row["message_id"]
    ctx.model = Reads()
    with util.clock_at(at):
        mailed = run.process_raw(ctx, entry.raw, source="imap", notify=False)
        assert mailed.status == "processed"
        assert run.resume_waiting(ctx) == 1
    assert ctx.ledger.messages.get(waited)["status"] == "ignored"
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"], row["message_id"]) == (
        "filed",
        None,
        mailed.message_row_id,
    )
    filed = bookings(ctx, mailed.message_row_id)
    assert filed and links(ctx, token) == [f"/trips/{ti.trip_id}#item-{ti.id}" for ti in filed]


def carrying(ctx: run.Context, inner: list[corpus.Entry]) -> bytes:
    """The owner's message carrying each email of `inner` as an attachment of its own: it reads
    the first as its own, and hands the others back to be read as emails of their own."""
    msg = email.message.EmailMessage()
    msg["From"] = ctx.settings.allowed_senders[0]
    msg["Subject"] = "Fwd: the emails"
    msg["Date"] = email.utils.format_datetime(corpus.sent(inner[-1]))
    msg["Message-ID"] = "<carrying@trips.example>"
    msg.set_content("The emails are attached.")
    for entry in inner:
        msg.add_attachment(email.message_from_bytes(entry.raw, policy=email.policy.default))
    return msg.as_bytes()


def test_an_uploads_attached_emails_count_on_every_reading(tmp_path):
    """The owner uploads a message carrying two emails: it reads the first, which holds no
    booking, and files the second as an email of its own. On its first reading, and on a later
    one (the first waited while the model could not be reached), the upload is `filed` by the
    bookings of the email attached to it."""
    nothing = an_entry(lambda e: e.outcome["status"] == "ignored")
    flights = owners_flights()
    at = corpus.sent(flights) + dt.timedelta(days=1)
    for model in (Reads(), Unreachable()):
        ctx = context(tmp_path / type(model).__name__)
        ctx.model = model
        raw = carrying(ctx, [nothing, flights])
        token = submit(ctx, raw, "the-emails.eml")
        w, clock, _ = a_worker(ctx, at)
        start(w)
        beat(w)
        row = ctx.ledger.uploads.get(token)
        if isinstance(model, Unreachable):
            assert (row["state"], row["error"]) == ("nothing", "model_down")
            ctx.model = Reads()
            with util.clock_at(clock()):
                assert run.resume_waiting(ctx) == 2  # the message, then its email: read with it
            row = ctx.ledger.uploads.get(token)
        assert (row["state"], row["error"]) == ("filed", None), type(model).__name__
        statuses = [r["status"] for r in ctx.ledger.messages.all()]
        assert statuses == ["ignored", "processed"]
        assert row["message_id"] == ctx.ledger.messages.all()[0]["id"]
        attached = ctx.ledger.messages.all()[1]["id"]
        assert len(bookings(ctx, attached)) == len(flights.outcome["items"])
        # read again by force, it is still filed
        with util.clock_at(clock()):
            replayed(ctx, raw)
        assert ctx.ledger.uploads.get(token)["state"] == "filed"


def test_an_upload_with_no_booking_and_one_with_no_model_end_as_nothing(tmp_path):
    """An email that holds no booking ends `nothing`; with no model, a confirmation only a model
    reads ends `nothing` with `no_model`. Handed in again once a model is set up, the same bytes
    are read again, and what it raises is dated by that reading."""
    none = an_entry(lambda e: e.outcome["status"] == "ignored")
    ctx = context(tmp_path)
    token = submit(ctx, none.raw, "offer.eml")
    w, clock, _ = a_worker(ctx, corpus.sent(none))
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"], row["body"]) == ("nothing", None, None)
    assert ctx.ledger.messages.get(row["message_id"])["status"] == "ignored"
    assert status.read(ctx.ledger)["last"]["uploads"]["code"] == "nothing"
    entry, text = a_paste()
    first, later = corpus.sent(entry), corpus.sent(entry) + dt.timedelta(days=2)
    ctx = context(tmp_path / "no-model", model=False)
    token = submit(ctx, text.encode())
    w, clock, _ = a_worker(ctx, first)
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"]) == ("nothing", "no_model")
    assert bookings(ctx, row["message_id"]) == []
    # a model is set up (one that reads the answer with too little confidence to go unchecked)
    ctx.model = Reads(confidence=0.1)
    assert uploads.submit(ctx.ledger, text.encode(), name=None, submitted_by=None) == (token, True)
    clock.to(later)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert (row["state"], row["error"]) == ("filed", None)
    filed = bookings(ctx, row["message_id"])
    assert filed and all(ti.payload["raised"]["low_confidence"] == stamp(later) for ti in filed)


def test_an_unreadable_upload_ends_as_failed_and_the_worker_goes_on(tmp_path, monkeypatch):
    stranger = next(e for e in the_corpus().entries if e.answer is None and not e.outcome["items"])
    entry, text = a_paste()
    ctx = context(tmp_path)
    tokens = [submit(ctx, stranger.raw, "offer.eml"), submit(ctx, text.encode())]
    w, clock, _ = a_worker(ctx, corpus.sent(entry))
    start(w)
    beat(w)  # nothing can read it
    row = ctx.ledger.uploads.get(tokens[0])
    assert (row["state"], row["error"], row["body"]) == ("failed", "unreadable", None)
    assert ctx.ledger.messages.get(row["message_id"])["status"] == "failed"
    assert status.read(ctx.ledger)["last"]["uploads"] == {
        "at": stamp(clock()),
        "ok": False,
        "code": "failed",
    }

    def breaks(*args, **kwargs):
        raise RuntimeError("the reading broke")

    with monkeypatch.context() as m:
        m.setattr(run, "process_text", breaks)
        clock.tick(5)
        beat(w)  # the reading itself breaks
    row = ctx.ledger.uploads.get(tokens[1])
    assert (row["state"], row["error"], row["message_id"]) == ("failed", "unreadable", None)
    # the worker goes on: handed in again, it is read
    assert submit(ctx, text.encode()) == tokens[1]
    clock.tick(5)
    beat(w)
    assert ctx.ledger.uploads.get(tokens[1])["state"] == "filed"


def test_a_paste_that_begins_like_an_email_is_read_as_text_when_the_email_holds_nothing(
    tmp_path,
):
    """A paste whose first line is a header only mail carries is stored as an email: read as
    one, its first lines are taken for headers and the confirmation is cut short, so it is read
    once more as the text it is. A file handed in under a name is read only as what it is."""
    entry, text = a_paste()
    pasted = "X-Note: " + text
    assert uploads.sniff(pasted.encode()) == "eml"
    ctx = context(tmp_path)
    token = submit(ctx, pasted.encode())
    named = submit(ctx, ("X-Note: " + text + "\n").encode(), "pasted.eml")
    w, clock, _ = a_worker(ctx, corpus.sent(entry))
    start(w)
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert row["state"] == "filed"
    filed = bookings(ctx, row["message_id"])
    assert len(filed) == len(entry.outcome["items"])
    clock.tick(5)
    beat(w)
    assert ctx.ledger.uploads.get(named)["state"] == "failed"
    # read as text, it is replayed as text: its bookings stay its own
    with util.clock_at(clock()):
        again = replayed(ctx, pasted.encode())
    assert [o.result for o in again.outcomes] == ["duplicate"] * len(filed)
    assert bookings(ctx, row["message_id"]) == filed


def test_a_row_left_processing_by_a_dead_worker_is_taken_again(tmp_path, caplog):
    entry, text = a_paste()
    ctx = context(tmp_path)
    token = submit(ctx, text.encode())
    assert ctx.ledger.uploads.claim_next()["token"] == token  # a worker took it, then died
    at = corpus.sent(entry)
    gone = {"pid": os.getpid() + 1, "host": socket.gethostname(), "beat_at": stamp(at)}
    ctx.ledger.kv.set(status.KV_WORKER, json.dumps(gone))
    w, clock, _ = a_worker(ctx, at + dt.timedelta(seconds=status.STALE_SECONDS))
    caplog.set_level(logging.INFO, logger="tripbox.worker")
    start(w)
    assert ctx.ledger.uploads.get(token)["state"] == "waiting"
    # nothing was given up: the start line says only what waits again
    said = [r.getMessage() for r in caplog.records if "upload" in r.getMessage()]
    assert said == ["uploads left being read: 1 waits again"]
    beat(w)
    assert ctx.ledger.uploads.get(token)["state"] == "filed"


def test_a_start_says_what_waits_again_and_what_was_given_up(tmp_path, caplog):
    """A start counts both: the uploads a stopped worker left being read that wait again, and
    those it gives up, read MAX_READINGS times already; the one given up is not read again."""
    entry, text = a_paste()
    ctx = context(tmp_path)
    heavy = submit(ctx, text.encode())
    light = submit(ctx, text.encode() + b"\nPasted again\n")
    for _ in range(uploads.MAX_READINGS - 1):
        assert ctx.ledger.uploads.claim_next()["token"] == heavy
        ctx.ledger.uploads.requeue()
    assert ctx.ledger.uploads.claim_next()["token"] == heavy  # its third reading
    assert ctx.ledger.uploads.claim_next()["token"] == light  # then the worker died
    at = corpus.sent(entry)
    gone = {"pid": os.getpid() + 1, "host": socket.gethostname(), "beat_at": stamp(at)}
    ctx.ledger.kv.set(status.KV_WORKER, json.dumps(gone))
    w, _, _ = a_worker(ctx, at + dt.timedelta(seconds=status.STALE_SECONDS))
    caplog.set_level(logging.INFO, logger="tripbox.worker")
    start(w)
    said = [r.getMessage() for r in caplog.records if "upload" in r.getMessage()]
    assert said == [
        "uploads left being read: 1 waits again, 1 given up after 3 readings"
    ]
    assert ctx.ledger.uploads.get(heavy)["state"] == "failed"
    beat(w)
    assert ctx.ledger.uploads.get(heavy)["state"] == "failed"
    assert ctx.ledger.uploads.get(light)["state"] == "filed"


def test_a_pdf_is_handed_to_the_pipeline_as_an_email_with_one_attachment(tmp_path, monkeypatch):
    entry = an_entry(lambda e: bool(pdfs(e.raw)))
    ((name, pdf),) = pdfs(entry.raw)
    ctx = context(tmp_path)
    handed: list[tuple[bytes, dict]] = []
    real = run.process_raw

    def recorded(ctx, raw, **kw):
        handed.append((raw, kw))
        return real(ctx, raw, **kw)

    monkeypatch.setattr(run, "process_raw", recorded)
    at = corpus.sent(entry) + dt.timedelta(hours=1)  # when it is handed in
    with util.clock_at(at):
        token = submit(ctx, pdf, f"scans/{name}")
    w, _, _ = a_worker(ctx, at + dt.timedelta(minutes=5))  # read a little later
    start(w)
    beat(w)
    ((raw, kw),) = handed
    assert kw == {"source": "upload", "trusted": True}
    # dated when it was handed in, not when it is read: read again, it is the same email
    assert uploads.looks_like_email(raw) and raw == uploads.as_email(name, pdf, at)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    (part,) = [p for p in msg.walk() if not p.is_multipart()]
    assert part.get_content_type() == "application/pdf"
    assert part.get_content_disposition() == "attachment"
    assert part.get_payload(decode=True) == pdf and part.get_filename() == name
    assert msg["Subject"] == name and email.utils.parsedate_to_datetime(msg["Date"]) == at
    assert msg["From"] is None
    row = ctx.ledger.uploads.get(token)
    assert row["state"] == "filed"
    message = ctx.ledger.messages.get(row["message_id"])
    assert (message["source"], message["subject"]) == ("upload", name)
    assert len(bookings(ctx, row["message_id"])) == len(entry.outcome["items"])
    # with no name it is still one attachment, named as an upload
    bare = email.message_from_bytes(uploads.as_email(None, pdf, at), policy=email.policy.default)
    (part,) = [p for p in bare.walk() if not p.is_multipart()]
    assert (bare["Subject"], part.get_filename()) == (uploads.PDF_SUBJECT, uploads.PDF_NAME)
    assert uploads.as_email(None, pdf, at) == uploads.as_email(None, pdf, at)


class AnswersAs:
    """The model, answering every message with `entry`'s answer."""

    model = "corpus"

    def __init__(self, entry: corpus.Entry) -> None:
        self.entry = entry

    def extract(self, nm, hint=None):
        return to_extraction(self.entry.answer), Usage(self.model, 0, 0, 0.0)


def test_a_pdf_read_twice_is_one_message_and_one_booking(tmp_path):
    """A worker reads a PDF and stops before it finishes the upload; the next worker takes the
    upload again. The PDF is the same email both times, so it is one message, read once, and
    its booking is filed once — also a booking with no date, which a second email would file
    again under a draft key of its own, a twin in Unsorted."""
    entry = an_entry(lambda e: bool(pdfs(e.raw)))
    ((name, pdf),) = pdfs(entry.raw)
    undated = an_entry(lambda e: any(i["booking"]["trip"] is None for i in e.outcome["items"]))
    ctx = context(tmp_path)
    ctx.model = AnswersAs(undated)
    at = corpus.sent(entry) + dt.timedelta(hours=1)
    with util.clock_at(at):
        token = submit(ctx, pdf, name)
        worker._read(ctx, ctx.ledger.uploads.claim_next())  # read, and never finished
    filed = ctx.trips.unsorted()
    assert len(filed) == len(undated.outcome["items"])
    w, _, _ = a_worker(ctx, at + dt.timedelta(minutes=10))
    start(w)
    assert ctx.ledger.uploads.get(token)["state"] == "waiting"
    beat(w)
    row = ctx.ledger.uploads.get(token)
    assert row["state"] == "filed"
    messages = ctx.ledger.conn.execute("SELECT id FROM messages").fetchall()
    assert [m["id"] for m in messages] == [row["message_id"]]
    assert [ti.id for ti in ctx.trips.unsorted()] == [ti.id for ti in filed]


def test_a_long_reading_keeps_the_record_live_and_a_second_worker_out(tmp_path):
    """Reading an upload may take long (a model's first call with a new schema takes about a
    minute): the worker marks its record busy first, and a busy record is live for BUSY_SECONDS,
    so a second worker started meanwhile is refused; the beat's next record clears the mark. A
    busy record with no beat for longer than that is a worker that stopped: a new one starts,
    and the old one, back from its reading, writes nothing over it and starts no child."""
    entry, text = a_paste()
    ctx = context(tmp_path)
    at = corpus.sent(entry)
    first, clock, spawn = a_worker(ctx, at)
    first.pid = os.getpid() + 1  # another process than the second
    start(first)
    second = worker.Worker(context(tmp_path), spawn=Spawner(clock), clock=clock, sleep=clock.tick)
    busy: list[bool] = []

    def a_minute():
        clock.tick(60)
        busy.append(status.read(ctx.ledger)["busy"])
        assert status.alive(ctx.ledger, clock())
        with pytest.raises(worker.WorkerRunning):
            start(second)

    ctx.model = Reads(reading=a_minute)
    token = submit(ctx, text.encode())
    beat(first)
    assert busy == [True] and ctx.ledger.uploads.get(token)["state"] == "filed"
    record = status.read(ctx.ledger)
    assert (record["pid"], record["busy"], record["beat_at"]) == (first.pid, False, stamp(clock()))
    assert len(spawn.of("poll")) == 1 and (status.BUSY_SECONDS, status.STALE_SECONDS) == (900, 30)

    def stuck():
        clock.tick(status.BUSY_SECONDS - 1)
        with pytest.raises(worker.WorkerRunning):
            start(second)
        clock.tick(2)
        start(second)

    ctx.model = Reads(reading=stuck)
    submit(ctx, (text + "\n\n").encode())
    beat(first)
    assert first.taken_over and status.read(ctx.ledger)["pid"] == os.getpid()
    assert len(spawn.of("poll")) == 1 and spawn.of("poll")[0].how == "terminate"


def test_a_worker_whose_record_another_took_stops_at_its_next_beat(tmp_path, handlers, caplog):
    """Another worker took the ledger between two beats (this one had stalled): at its next beat
    this one stops — it ends its children, says so in one line and exits 2 — without writing
    over the other's record or touching an upload, waiting or being read."""
    entry, text = a_paste()
    ctx = context(tmp_path)
    at = corpus.sent(entry)
    clock = Clock(at)
    spawn = Spawner(clock)
    other = {"pid": os.getpid() + 1, "host": socket.gethostname()}
    tokens: list[str] = []

    def sleep(seconds: float) -> None:
        clock.tick(seconds)
        assert clock() - at < dt.timedelta(minutes=1), "the worker did not stop"
        if not tokens:
            ctx.ledger.kv.set(status.KV_WORKER, json.dumps({**other, "beat_at": stamp(clock())}))
            tokens.extend(submit(ctx, body) for body in (text.encode(), (text + "\n\n").encode()))
            assert ctx.ledger.uploads.claim_next()["token"] == tokens[0]  # the other reads it

    w = worker.Worker(ctx, spawn=spawn, clock=clock, sleep=sleep)
    caplog.set_level(logging.WARNING, logger="tripbox.worker")
    with util.clock_at(at):
        assert w.run() == 2
    assert [c.how for c in spawn.children] == ["terminate"]
    assert status.read(ctx.ledger) == {**other, "beat_at": stamp(at + dt.timedelta(seconds=5))}
    rows = [ctx.ledger.uploads.get(t) for t in tokens]
    assert [r["state"] for r in rows] == ["processing", "waiting"]
    assert rows[1]["body"] == (text + "\n\n").encode()
    said = [r.getMessage() for r in caplog.records if r.name == "tripbox.worker"]
    assert said == ["work: another worker took over this ledger"]


def test_sigterm_ends_the_children_and_the_worker_exits_0(tmp_path, handlers):
    ctx = context(tmp_path)
    at = corpus.sent(the_corpus().entries[0])
    clock = Clock(at)
    spawn = Spawner(clock)
    beats: list[dt.datetime] = []

    def sleep(seconds: float) -> None:
        beats.append(clock())
        clock.tick(seconds)
        if len(beats) == 3:
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    w = worker.Worker(ctx, spawn=spawn, clock=clock, sleep=sleep)
    assert w.run() == 0
    assert beats == [at + dt.timedelta(seconds=n * worker.BEAT_SECONDS) for n in range(3)]
    (poll,) = spawn.children
    assert poll.how == "terminate" and poll.ended == at + dt.timedelta(seconds=15)
    # it gave the ledger up: no worker runs, and one started at once on a new host is not refused
    record = status.read(ctx.ledger)
    assert record["stopped_at"] == stamp(clock()) and not status.alive(ctx.ledger, clock())
    recreated = worker.Worker(ctx, spawn=Spawner(clock), clock=clock, sleep=clock.tick)
    recreated.host = "recreated"
    start(recreated)
    assert status.read(ctx.ledger)["host"] == "recreated"


def test_work_refuses_offline_and_says_when_another_worker_runs(tmp_path, monkeypatch, capsys):
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(the_corpus().root / corpus.CONFIG_DIR))
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    ran: list = []
    monkeypatch.setattr(worker.Worker, "run", lambda self: ran.append(self) or 0)
    assert cli.main(["work"]) == 2
    out = capsys.readouterr()
    assert out.err.splitlines() == ["TRIPBOX_NO_NETWORK is set: work needs the network"]
    assert out.out == "" and not (tmp_path / "data").exists()
    monkeypatch.delenv("TRIPBOX_NO_NETWORK")
    at = corpus.sent(the_corpus().entries[0])
    with Ledger.open(tmp_path / "data" / "ledger.db") as led:
        other = {"pid": os.getpid() + 1, "host": socket.gethostname(), "beat_at": stamp(at)}
        led.kv.set(status.KV_WORKER, json.dumps(other))
    with util.clock_at(at + dt.timedelta(seconds=10)):
        assert cli.main(["work"]) == 2
    said = [line for line in capsys.readouterr().err.splitlines() if line.startswith("work:")]
    assert said == ["work: another worker is running on this ledger"] and ran == []
    with Ledger.open(tmp_path / "data" / "ledger.db") as led:
        assert status.read(led) == other


def test_a_poll_that_ends_3_is_recorded_as_message_failed(tmp_path):
    """`poll` ends 3 when it read the mailbox (or had none) and a message failed, 1 when the
    mailbox could not be read: the record tells the two apart."""
    at = corpus.sent(the_corpus().entries[0])
    for code, word in ((3, "message_failed"), (1, "exit_1"), (0, "exit_0")):
        ctx = context(tmp_path / str(code))
        w, clock, _ = a_worker(ctx, at, exit=code)
        start(w)
        beat(w)
        clock.tick(worker.BEAT_SECONDS)
        beat(w)  # the poll that ended is collected
        assert status.read(ctx.ledger)["last"]["poll"] == {
            "at": stamp(clock()),
            "ok": code == 0,
            "code": word,
        }, code
