# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The worker (`tripbox work`): one long-lived process that beats every BEAT_SECONDS. A beat
writes the status record the page reads (`status`), takes one waiting upload through the
pipeline, runs one sync pass with the flight log, and starts `poll` and `enrich` as processes
of their own when they are due: `poll` at once and then every POLL_SECONDS, `enrich` once a
night at NIGHTLY in the configured zone while enrichment is on. With `TRIPBOX_COVERS=wikimedia`
a beat also looks up the photos of a few places (`covers`). A child is never waited for
inside a beat, never started twice at a time, and killed once it runs past its time. A part of
a beat that breaks is logged and recorded as that part's code, and the beat goes on. One
worker runs on a ledger at a time: `Worker.start` refuses while another's record is live, and a
worker that finds another's live record at a beat stops. Before a part that may run long the
record says `busy`, which keeps it live for `status.BUSY_SECONDS`. SIGTERM ends the worker,
which then gives the ledger up (its record says it stopped)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx2

from tripbox import status, uploads, util
from tripbox.pipeline import run
from tripbox.pipeline.run import Context, MessageResult
from tripbox.trips import coverimage, wikimedia
from tripbox.trips.covers import CoverStore
from tripbox.trips.store import stamp

log = logging.getLogger("tripbox.worker")

# A poll is stopped only after one message's longest reading (the extractor's time, then the
# model's over every retry), so a slow model makes mail wait instead of stopping every poll.
BEAT_SECONDS, POLL_SECONDS, POLL_TIMEOUT, ENRICH_TIMEOUT = 5, 300, 900, 900
NIGHTLY = dt.time(3, 30)  # in the configured zone
TIMEOUTS = {"poll": POLL_TIMEOUT, "enrich": ENRICH_TIMEOUT}
FEATURES = ("mail", "model", "airtrail", "notifications", "enrichment")  # in the record
PARTS = ("poll", "enrich", "sync", "uploads", "covers")  # the record's `last`
# Photo lookups: a batch of at most LOOKUPS_PER_BEAT places, at most once every LOOKUP_SECONDS; a
# place with no photo is asked again after NONE_DAYS; a failed one waits LOOKUP_BACKOFF,
# doubling with each failure up to LOOKUP_BACKOFF_MAX
LOOKUPS_PER_BEAT, LOOKUP_SECONDS, NONE_DAYS = 2, 60, 30
LOOKUP_BACKOFF, LOOKUP_BACKOFF_MAX = dt.timedelta(minutes=5), dt.timedelta(hours=24)
STOP_SECONDS = 5  # how long a child has to end once told to, when the worker stops
SOURCE = "upload"  # the source of a message that came as an upload
# `poll`'s exit when it read the mailbox (or had none) and a message failed, recorded as the code
# MESSAGE_FAILED; 1 is a mailbox that could not be read
POLL_MESSAGE_FAILED, MESSAGE_FAILED = 3, "message_failed"
RUNNING = "another worker is running on this ledger"
TAKEN_OVER = "another worker took over this ledger"
# A mail login the page may name as the address to forward confirmations to: one `@`, something
# before it, a dotted domain, no white space. A login that is no address is not named.
ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class WorkerRunning(RuntimeError):
    """Another worker beats on the ledger: this one does not start."""


class _TakenOver(Exception):
    """Another process's live record is on the ledger: this worker stops, writing nothing."""


# ----------------------------------------------------------------------------- uploads
def take_upload(ctx: Context) -> str | None:
    """Read the oldest waiting upload through the pipeline and finish it: the state it ended
    in, or None when none waits. The owner handed it in, so it is their own input, as a paste to
    the bot is: a text is read as pasted text, an email as an email whoever sent it, a PDF as an
    email carrying it (`uploads.as_email`), dated when it was handed in, so a second reading
    of it is the same message. What it ends as is what `uploads.came_to` says of the reading,
    as on every later reading: `filed` when its message, an email attached to it or the
    earlier delivery it was set aside for filed, updated or matched a booking; `nothing` when
    none did (with the word for why when its message waits for the model: `run.WAIT_REASONS`;
    the resume pass that files it later makes it `filed`); `failed` (`unreadable`) when the
    reading failed."""
    led = ctx.ledger
    row = led.uploads.claim_next()
    if row is None:
        return None
    res: MessageResult | None = None
    try:
        res = _read(ctx, row)
        # a reading that read nothing (its message was finished before) and found no booking
        came = uploads.came_to(led, res) or ("nothing", None, res.same_as or res.message_row_id)
        state, error, message_id = came
    except Exception:  # noqa: BLE001 - one upload that cannot be read never stops the worker
        log.exception("upload #%s could not be read", row["id"])
        state, error = "failed", "unreadable"
        message_id = (res.same_as or res.message_row_id) if res is not None else None
    led.uploads.finish(row["id"], state, message_id=message_id, error=error)
    log.info("upload #%s (%s): %s", row["id"], row["kind"], state)
    return state


def _read(ctx: Context, row: Any) -> MessageResult:
    """The upload through the pipeline, by its kind. An email handed in with no name (the paste
    form gives none) that yields no booking is read once more as plain text: a paste whose
    first line only looks like a header is not lost."""
    body = bytes(row["body"])
    if row["kind"] == "text":
        return run.process_text(ctx, body.decode("utf-8"), source=SOURCE)
    if row["kind"] == "pdf":
        # dated when it was handed in, so every reading of it is the same email (one message)
        carried = uploads.as_email(row["name"], body, util.parse_iso(row["created_at"]))
        return run.process_raw(ctx, carried, source=SOURCE, trusted=True)
    # the email from its first header on (a row stored before this rule may hold none)
    mail = uploads.email_of(body) or body
    res = run.process_raw(ctx, mail, source=SOURCE, trusted=True)
    if row["name"] is None and not uploads.booked(ctx.ledger, res):
        # the same bytes, so the same message, read again as text (the model asked again)
        res = run.process_text(
            ctx,
            mail.decode("utf-8", errors="replace"),
            source=SOURCE,
            force=True,
            reread=ctx.model is not None,
        )
    return res


# ----------------------------------------------------------------------------- the worker
@dataclass
class _Child:
    """A process the worker started (a subprocess.Popen, or what `spawn` hands back), since when
    it runs, and whether it was killed for running too long (it is kept until it has ended, so
    none is started beside it)."""

    proc: Any
    started: dt.datetime
    killed: bool = False


def _exit_word(code: int) -> str:
    """How a child ended, in one word: `exit_<n>`, or `signal_<n>` when a signal ended it (a
    poll's POLL_MESSAGE_FAILED is MESSAGE_FAILED: `_reap`)."""
    return f"exit_{code}" if code >= 0 else f"signal_{-code}"


class Worker:
    """`spawn` starts a child from its argument list, `clock` is the time (aware) and `sleep`
    waits between beats: the process's own, unless a test hands in stand-ins."""

    def __init__(
        self,
        ctx: Context,
        *,
        spawn: Callable[[list[str]], Any] = subprocess.Popen,
        clock: Callable[[], dt.datetime] = util.now,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.ctx = ctx
        self._transport = transport
        self._http: httpx2.Client | None = None  # built when the first lookup is due
        self.spawn = spawn
        self.clock = clock
        self.sleep = sleep
        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.started_at: dt.datetime | None = None
        self.last: dict[str, dict | None] = dict.fromkeys(PARTS)
        self._zone = ZoneInfo(ctx.settings.tz)
        self._children: dict[str, _Child] = {}
        self._polled_at: dt.datetime | None = None
        self._polls_stopped = 0  # polls stopped for their time since one last ended by itself
        self._next_enrich: dt.datetime | None = None
        self._looked_up_at: dt.datetime | None = None  # when the last batch of lookups began
        self._stopping = False
        self.taken_over = False  # another worker's live record was found at a beat

    # ------------------------------------------------------------------ start and stop
    def start(self) -> None:
        """Take the ledger for this process. In one write transaction: WorkerRunning while the
        record of another process (its pid or its host differs) is live (`status.live`); else
        what a worker that stopped left being read waits again (but an upload read
        `uploads.MAX_READINGS` times, which is given up), with no model set up the mail
        that waits for one waits for it to be set up (`run.wait_for_a_model`), and this
        process's record is written. From then on SIGTERM ends the worker (`run` returns 0)."""
        now = self.clock()
        led = self.ctx.ledger
        with led.transaction():
            if self._others(status.read(led), now):
                raise WorkerRunning(RUNNING)
            requeued = led.uploads.requeue()
            gone = run.wait_for_a_model(led, "no_model") if self.ctx.model is None else 0
            self.started_at = now
            self._write(now)
        back, given_up = requeued
        said = [f"{back} {'waits' if back == 1 else 'wait'} again"] if back else []
        if given_up:
            said.append(f"{given_up} given up after {uploads.MAX_READINGS} readings")
        if said:  # a clause whose count is none is left out
            log.info("uploads left being read: %s", ", ".join(said))
        if gone:
            log.info("%d message(s) wait for a model to be set up", gone)
        self._next_enrich = self._nightly(now)
        signal.signal(signal.SIGTERM, self._terminated)
        log.info("the worker started (pid %s)", self.pid)

    def _mine(self, record: dict) -> bool:
        return record.get("pid") == self.pid and record.get("host") == self.host

    def _others(self, record: dict | None, now: dt.datetime) -> bool:
        """Whether `record` is another process's, and live."""
        return record is not None and not self._mine(record) and status.live(record, now)

    def _terminated(self, signum: int, frame: object) -> None:
        """SIGTERM: no child is started again, each one running is told to end, and `run`
        returns once the beat or the sleep under way is over."""
        self._stopping = True
        for child in list(self._children.values()):
            try:
                child.proc.terminate()
            except OSError:  # it ended meanwhile
                pass

    def run(self) -> int:
        """Beat every BEAT_SECONDS until SIGTERM; then end the children, give the ledger up and
        return 0. A worker another took the ledger from ends its children and returns 2,
        leaving the other's record as it is."""
        if self.started_at is None:
            self.start()
        try:
            while not self._stopping:
                began = self.clock()
                self.beat()
                if self._stopping:
                    break
                spent = (self.clock() - began).total_seconds()
                self.sleep(max(0.0, BEAT_SECONDS - spent))
        finally:
            self._end_children()
            if not self.taken_over:
                self._release()
        if self.taken_over:
            return 2
        log.info("the worker stopped")
        return 0

    def _release(self) -> None:
        """Give the ledger up: this process's record says it stopped, so no worker reads as
        running and one started at once (a recreated container, on another host) is not
        refused. Another process's live record is left as it is."""
        if self._http is not None:
            self._http.close()
            self._http = None
        now = self.clock()
        try:
            with self.ctx.ledger.transaction():
                if not self._others(status.read(self.ctx.ledger), now):
                    self._write(now, stopped=True)
        except Exception:  # noqa: BLE001 - stopping goes on; the record goes stale in 30 s
            log.exception("the worker's status record could not be released")

    def _end_children(self) -> None:
        """Tell every child to end, give it STOP_SECONDS, then kill it."""
        procs = [child.proc for child in self._children.values()]
        self._children.clear()
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:  # it ended meanwhile
                pass
        for proc in procs:
            try:
                proc.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                log.warning("a child did not end when told to and was killed")

    # ------------------------------------------------------------------ the beat
    def beat(self) -> None:
        """The status record; one waiting upload; a sync pass; the record again (no longer
        busy), then each child that ended or ran past its time, then each one that is due; and
        the record again, with what the parts came to. Each record is written only while no
        other process's record is live: a worker that finds one stops there (`taken_over`)."""
        try:
            self._record()
            self._part("uploads", self._uploads)
            self._part("covers", self._covers)
            self._part("sync", self._sync)
            self._record()  # before a child starts, so one taken over starts none
            for command in TIMEOUTS:
                self._part(command, lambda c=command: self._reap(c))
                self._part(command, lambda c=command: self._start_if_due(c))
            self._record()
        except _TakenOver:
            self._give_way()

    def _give_way(self) -> None:
        """Another worker took the ledger: stop, telling each child to end, and say so once.
        Nothing is written to the ledger: the other's record stays, and so does every upload."""
        if not self.taken_over:
            log.warning("work: %s", TAKEN_OVER)
        self.taken_over = True
        self._terminated(signal.SIGTERM, None)

    def _part(self, name: str, part: Callable[[], tuple[bool, str] | None]) -> None:
        """Run one part of a beat; what it came to (ok, code), if anything, is its `last`. An
        exception is logged and recorded as the code `error`."""
        try:
            ended = part()
        except _TakenOver:
            raise
        except Exception:  # noqa: BLE001 - one part that breaks never stops the beat
            log.exception("the worker's %s failed", name)
            ended = (False, "error")
        if ended is not None:
            self.last[name] = {"at": stamp(self.clock()), "ok": ended[0], "code": ended[1]}

    def _uploads(self) -> tuple[bool, str] | None:
        if self.ctx.ledger.uploads.waiting():
            self._record(busy=True)  # a model may take a minute to read it
        state = take_upload(self.ctx)
        return None if state is None else (state != "failed", state)

    def _covers(self) -> tuple[bool, str] | None:
        """One waiting owner photo, then (with automatic covers on, and LOOKUP_SECONDS since the
        last batch) up to LOOKUPS_PER_BEAT places looked up. None when nothing was done; else
        `done`, or the code of the last thing that failed."""
        cv = CoverStore(self.ctx.ledger)
        failed: str | None = None
        did = False
        waiting = cv.next_upload()
        if waiting is not None:
            did = True
            self._record(busy=True)  # a large photo takes a moment to decode
            try:
                made = coverimage.process(waiting.data)
            except coverimage.CoverImageError as exc:
                kept = cv.fail_upload(waiting, exc.code)
                failed = exc.code if kept else None
            except Exception:  # noqa: BLE001 - a photo that cannot be read never stops the worker
                log.exception("the cover photo of trip #%s could not be read", waiting.trip_id)
                kept = cv.fail_upload(waiting, "unreadable")
                failed = "unreadable" if kept else None
            else:
                kept = cv.finish_upload(waiting, made.large, made.small, made.digest)
            if not kept:  # another upload, Remove photo or a merge came first: theirs stands
                log.info("the cover photo of trip #%s changed while it was read", waiting.trip_id)
        if self.ctx.settings.covers == "wikimedia":
            now = self.clock()
            waited = self._looked_up_at is not None and (
                (now - self._looked_up_at).total_seconds() < LOOKUP_SECONDS
            )
            trips = [] if waited else [(t.id, t.place) for t in self.ctx.trips.all_trips()]
            trips = [(i, p) for i, p in trips if p]
            due = cv.due_lookups(trips, now, LOOKUPS_PER_BEAT)
            if due:
                self._looked_up_at = now
                self._record(busy=True)  # each lookup may take its HTTP timeouts
            for trip_id, place in due:
                did = True
                failed = self._look_up(cv, trip_id, place, now) or failed
        if not did:
            return None
        return (False, failed) if failed else (True, "done")

    def _look_up(self, cv: CoverStore, trip_id: int, place: str, now: dt.datetime) -> str | None:
        """Give one trip's place a cover: copied from a trip at the same place, else asked of
        Wikimedia. Answers the code of a failure, else None. Any exception from the lookup is a
        failure to try again later, so no escape makes the trip due on the next beat."""
        same = cv.same_place_ready(place, exclude=trip_id)
        if same is not None and same.digest and same.credit is not None:
            large, small = cv.image(same.digest, "large"), cv.image(same.digest, "small")
            if large and small:
                cv.found(trip_id, place, large, small, same.digest, same.credit)
                return None
        try:
            found = wikimedia.find(place, http=self._client())
            made = coverimage.process(found.data) if found is not None else None
        except coverimage.CoverImageError:  # what Wikimedia gave is no usable picture
            found, made = None, None
        except Exception as exc:  # noqa: BLE001 - see above
            code = exc.code if isinstance(exc, wikimedia.Transient) else "error"
            if code == "error":
                log.exception("the photo lookup for trip #%s failed", trip_id)
            row = self.ctx.ledger.covers.get(trip_id)
            tries = row["attempts"] if row is not None and row["source"] == "wikimedia" else 0
            wait = min(LOOKUP_BACKOFF * 2**min(tries, 20), LOOKUP_BACKOFF_MAX)
            cv.lookup_failed(trip_id, place, code, now + wait)
            return code
        if found is None or made is None:
            cv.none_found(trip_id, place, now + dt.timedelta(days=NONE_DAYS))
        else:
            cv.found(trip_id, place, made.large, made.small, made.digest, found.credit)
        return None

    def _client(self) -> httpx2.Client:
        if self._http is None:
            self._http = wikimedia.client(
                self.ctx.settings.source_url, transport=self._transport
            )
        return self._http

    def _sync(self) -> tuple[bool, str] | None:
        """One pass (none when no flight log is kept): `done`, or why it ended early."""
        if self.ctx.syncer is None:
            return None
        self._record(busy=True)  # a pass may send up to fifty flights
        report = run.sync(self.ctx)  # it logs a pass that broke and answers None
        if report is None:
            return False, "error"
        if report.stopped:
            return False, "key_rejected"
        if report.down:
            return False, "outage"
        return True, "done"

    def _reap(self, command: str) -> tuple[bool, str] | None:
        """How the child running `command` ended, if it did; one that ran past its time is
        killed and ends as `timeout`, and is collected once it has ended. A poll killed so
        twice running, with none ending by itself between, is told (`run.poll_stopped`)."""
        child = self._children.get(command)
        if child is None:
            return None
        code = child.proc.poll()
        if code is not None:
            del self._children[command]
            if child.killed:  # its end was recorded when it was killed
                return None
            if command == "poll":
                self._polls_stopped = 0
            word = _exit_word(code)
            if command == "poll" and code == POLL_MESSAGE_FAILED:
                word = MESSAGE_FAILED
            log.info("%s ended: %s", command, word)
            return code == 0, word
        ran = (self.clock() - child.started).total_seconds()
        if child.killed or ran < TIMEOUTS[command]:
            return None
        child.proc.kill()
        child.killed = True
        log.warning("%s ran past %d s and was killed", command, TIMEOUTS[command])
        if command == "poll":
            self._polls_stopped += 1
            if self._polls_stopped >= 2:  # told once (`run.poll_stopped`)
                run.poll_stopped(self.ctx, POLL_TIMEOUT // 60)
        return False, "timeout"

    def _start_if_due(self, command: str) -> None:
        """Start `command` as a process of its own when it is due and none runs. Its next time
        is set before it starts, so one that cannot be started is not tried again every beat."""
        now = self.clock()
        if self._stopping or command in self._children:
            return
        if command == "poll":
            if self._polled_at is not None and (
                (now - self._polled_at).total_seconds() < POLL_SECONDS
            ):
                return
            self._polled_at = now
        else:
            if self._next_enrich is None:
                self._next_enrich = self._nightly(now)
            if now < self._next_enrich:
                return
            self._next_enrich = self._nightly(now + dt.timedelta(seconds=1))
            if not self.ctx.settings.on("enrichment"):
                return
        proc = self.spawn([sys.executable, "-m", "tripbox", command])
        self._children[command] = _Child(proc, now)
        log.info("%s started (pid %s)", command, getattr(proc, "pid", "?"))

    def _nightly(self, after: dt.datetime) -> dt.datetime:
        """The first NIGHTLY in the configured zone at `after` or later."""
        day = after.astimezone(self._zone).date()
        at = dt.datetime.combine(day, NIGHTLY, tzinfo=self._zone)
        if at < after:
            at = dt.datetime.combine(day + dt.timedelta(days=1), NIGHTLY, tzinfo=self._zone)
        return at

    # ------------------------------------------------------------------ the record
    def _record(self, *, busy: bool = False) -> None:
        """Write this process's record (`busy` before a part that may run long), in one write
        transaction with the look at what is there: _TakenOver, writing nothing, when it is
        another process's live record."""
        now = self.clock()
        try:
            with self.ctx.ledger.transaction():
                if self._others(status.read(self.ctx.ledger), now):
                    raise _TakenOver
                self._write(now, busy=busy)
        except _TakenOver:
            raise
        except Exception:  # noqa: BLE001 - the beat goes on; the next one writes it again
            log.exception("the worker's status record could not be written")

    def _write(self, now: dt.datetime, *, busy: bool = False, stopped: bool = False) -> None:
        """This process's record, as `status` describes it."""
        s = self.ctx.settings
        waiting = self.ctx.ledger.messages.waiting()
        record = {
            "pid": self.pid,
            "host": self.host,
            "started_at": stamp(self.started_at) if self.started_at is not None else None,
            "beat_at": stamp(now),
            "busy": busy,
            "stopped_at": stamp(now) if stopped else None,
            "features": {name: s.feature(name).state for name in FEATURES},
            "last": dict(self.last),
            "mailbox": s.mail_user if s.on("mail") and ADDRESS.fullmatch(s.mail_user) else None,
            "waiting": {why: waiting[why] for why in run.WAIT_REASONS if waiting.get(why)},
            "dry_run": bool(s.airtrail_dry_run),
        }
        text = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self.ctx.ledger.kv.set(status.KV_WORKER, text)
