# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The reconcile with a flight log (AirTrail). For each flight that is due, what the flight log
should hold of it (`checks.desired`) is compared with what it was last told (`told`) and only
the difference is sent — and only for a flight the owner is on with no check open, which is what
its sync state, worked out by the trip store's `settle`, says.

Before the first flight of a pass the flight log's profile is read: the ledger's flight ids are
one AirTrail user's flights, so a key that answers as another user ends the pass with nothing
claimed or sent (`lifecycle.bind`). A pass claims each flight, at the time it reaches it, before
it asks the flight log anything more, so two passes never send one flight twice. A flight with
no flight id is looked for in the flight log before it is created, so a crash between the create
and the store being told leaves one flight, which the next pass adopts. A flight that is no
longer the owner's there (deleted by hand, or the owner taken off it: not found, a refusal that
asks an own-flights key for an any-flights scope, or a record whose passengers the key's user
is not among) is unlinked and never made again unasked, and so is one whose record there has
become another flight; nothing is written to either. An
outage backs off from BACKOFF_FIRST to BACKOFF_MAX seconds and ends the pass; a flight log down
for ALERT_AFTER seconds, a key it rejects, a scope the key lacks, a role that forbids and a
flight it refuses each raise one alert. Only a refusal of the create or the update itself is a
check on the booking. An error of tripbox's own on one flight backs that flight off; the pass
goes on and raises it at the end."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.log import redact
from tripbox.trips.store import TripItem, TripStore, stamp
from tripbox.validate import checks
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rules import Item
from tripbox.write.airtrail import (
    AirTrailClient,
    AirTrailError,
    AirTrailRefs,
    flight_to_input,
    key_report,
    missing_scope,
    not_listed,
    read_profile,
)
from tripbox.write.lifecycle import (
    ALERTED_KV,
    DOWN_SINCE_KV,
    KEY_PROBLEM_KV,
    KEY_REJECTED_KV,
    OTHER_LOG,
    bind,
)

log = logging.getLogger("tripbox.sync")

OPERATIONS = (
    "create",
    "adopt",
    "update",
    "detach",
    "dry_run",
    "wait",
    "outage",
    "refused",
    "airport",
    "stop",
    "other",
    "fill",
    "nothing",
)
# What `Syncer.fill` may put on a flight's record, each only where the record holds nothing:
# what happened to the flight once it flew, never what its booking decides (its day, times,
# number, airports, airline or passengers).
FILL_FIELDS = (
    "aircraftReg",
    "takeoffActual",
    "landingActual",
    "departureGate",
    "arrivalGate",
    "departureTerminal",
    "arrivalTerminal",
    "aircraftId",
)
BACKOFF_FIRST, BACKOFF_MAX, ALERT_AFTER, CLAIM_SECONDS = 30, 6 * 3600, 3600, 600

# The ledger's `kv` rows about the flight log itself (`lifecycle`): since when it has not
# answered, when that was alerted, and when it rejected the key. Each is empty ("") or absent
# while it is not so. Why a pass last stopped for the key is one word in
# `lifecycle.KEY_PROBLEM_KV` (UNAUTHORIZED, `scope:<name>`, ROLE or `lifecycle.OTHER_LOG`); the
# owner is told when the word changes.
DOWN_SINCE, ALERTED, KEY_REJECTED = DOWN_SINCE_KV, ALERTED_KV, KEY_REJECTED_KV
UNAUTHORIZED, ROLE = "unauthorized", "role"
ENDS_A_PASS = ("stop", "other", "outage")
NOT_THEIRS = "it is no longer the owner's in the flight log"  # why a flight was unlinked
READS_OWN = "flight.read.own"  # the scope, and the role's permission, to read one's own flights
SAID = 300  # how much of the flight log's own words an alert carries
NOTE_MAX = 10000  # AirTrail's limit on a flight's note
INSTANTS = ("departure", "arrival", "departureScheduled", "arrivalScheduled")
# How each field of `checks.desired` is named in the note line an update appends.
NAMES = {
    "date": "date",
    "departure": "departure",
    "arrival": "arrival",
    "flight": "flight number",
    "from": "origin",
    "to": "destination",
    "airline": "airline",
    "people": "travellers",
    "cancelled": "status",
}

# AirTrail's own words for a seat class (a flight the backfill files carries one as its cabin).
FLIGHT_LOG_CLASSES = ("economy", "economy+", "business", "first", "private")
CABIN_MAP = (
    ("first", "first"),
    ("business", "business"),
    ("premium", "economy+"),
    ("comfort", "economy+"),
    ("plus", "economy+"),
    ("economy", "economy"),
    ("main", "economy"),
    ("coach", "economy"),
    ("basic", "economy"),
)


def duration_seconds(payload: dict) -> int | None:
    """A flight's length as the flight log keeps it, in seconds; None when it is not known."""
    minutes = payload.get("duration_min")
    return None if minutes is None else int(minutes) * 60


def _same(key: str, a: object, b: object) -> bool:
    """Whether two values of one input field are the same as the flight log reads them: a time as
    an instant (it may hand one back spelled otherwise) and the date as its day."""
    if a is None or b is None:
        return a is None and b is None
    if key == "date":
        return str(a)[:10] == str(b)[:10]
    if key in INSTANTS:
        try:
            return util.parse_iso(str(a)) == util.parse_iso(str(b))
        except ValueError:
            return a == b
    return a == b


def seat_class(cabin: str | None) -> str | None:
    """The flight log's seat class for a printed cabin; a cabin that is already one of the flight
    log's own words is that class."""
    if not cabin:
        return None
    c = cabin.lower()
    if c.strip() in FLIGHT_LOG_CLASSES:
        return c.strip()
    for needle, cls in CABIN_MAP:
        if needle in c:
            return cls
    return None


def seats_by_canonical(item: Item, book: PassengerBook) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw, seat in (item.payload.get("seat_by_passenger") or {}).items():
        m = book.match(raw)
        key = "me" if m.kind == "me" else m.canonical
        out[key] = seat
    return out


def build_note(item: Item, extra: str | None = None) -> str:
    p = item.payload
    parts = ["tripbox"]
    if p.get("pnr"):
        parts.append(f"PNR {p['pnr']}")
    if p.get("message_id"):
        parts.append(f"msg {p['message_id']}")
    if p.get("email_date"):
        parts.append(f"booked {str(p['email_date'])[:10]}")
    if p.get("provider"):
        parts.append(str(p["provider"]))
    note = " · ".join(parts)
    if item.classified.others:
        note += "\nAlso travelling (not logged): " + ", ".join(item.classified.others)
    if extra:
        note += "\n" + extra
    return note[:10000]


def build_flight_input(
    item: Item,
    book: PassengerBook,
    *,
    user_id: str | None,
    from_id: int | None,
    to_id: int | None,
    airline_id: int | None,
) -> dict:
    """The FlightInput for a new flight. AirTrail's ids for the airports and the airline are the
    caller's, looked up when the flight is written (a dry run's preview passes None for one it
    could not find); the payload's own ids are never read."""
    p = item.payload
    seats = seats_by_canonical(item, book)
    cls = seat_class(p.get("cabin"))
    passengers: list[dict] = []
    if item.classified.me_present:
        passengers.append(
            {"userId": user_id, "seatNumber": seats.get("me"), "seatClass": cls}
        )
    for g in item.classified.guests:
        passengers.append({"guestName": g, "seatNumber": seats.get(g), "seatClass": cls})
    return {
        "date": p["date_local"],
        "departure": p.get("dep_utc"),
        "arrival": p.get("arr_utc"),
        "departureScheduled": p.get("dep_utc"),
        "arrivalScheduled": p.get("arr_utc"),
        "duration": duration_seconds(p),
        "flightNumber": p.get("flight_code"),
        "fromId": from_id,
        "toId": to_id,
        "airlineId": airline_id,
        "note": build_note(item),
        "passengers": passengers,
    }


def backoff(attempts: int) -> int:
    """Seconds to wait after the `attempts`-th failed attempt in a row (from 1): BACKOFF_FIRST,
    doubling each time, never more than BACKOFF_MAX."""
    return min(BACKOFF_FIRST * 2 ** (min(max(attempts, 1), 32) - 1), BACKOFF_MAX)


def _note(first: list[str], note: str, last: str) -> str:
    """The flight's note with `first` put before it and `last` after it, clipped to NOTE_MAX by
    cutting the old note's end, so the lines just added always fit."""
    head = "\n".join(first)
    room = NOTE_MAX - len(head) - len(last) - 2
    body = note[: max(room, 0)]
    return "\n".join(part for part in (head, body, last) if part)[:NOTE_MAX]


def _instant(value: object) -> dt.datetime | None:
    try:
        return util.parse_iso(str(value)) if value else None
    except ValueError:
        return None


def _agreeing(flight: dict, known: Mapping) -> int:
    """How many of three parts of the record the flight log holds under the booking's flight id
    agree with `known`: what the flight log was last told of the flight or, with nothing told,
    what the booking itself says (`checks.desired`). The parts are its day, the digits of its
    number and its origin's code. A part either side does not say agrees with nothing, but for
    the number or the origin when neither side says it (a flight the flight log holds with no
    number, or from an airport with no code): the destination's code, then the departure's
    instant (the record's scheduled one when it holds no other, as the backfill reads it),
    stands in for it. A flight log put back from an older copy hands out again the ids
    its newest flights held, which may then name the owner's other flights: with fewer than two
    agreeing the record is another flight (`another_flight`); with one other, the owner changed
    the flight there by hand. (Two flights with one number from one origin on different days
    cannot be told apart by this.)"""

    def digits(number: object) -> str:
        return re.sub(r"\D", "", str(number or "")).lstrip("0")

    def code(end: object) -> str:
        return str(end.get("iata") or "").upper() if isinstance(end, dict) else ""

    pairs: list[tuple[object, object]] = [
        (str(flight.get("date") or "")[:10], str(known.get("date") or "")[:10]),
        (digits(flight.get("flightNumber")), digits(known.get("flight"))),
        (code(flight.get("from")), str(known.get("from") or "").upper()),
    ]
    stand_ins = [
        (code(flight.get("to")), str(known.get("to") or "").upper()),
        (
            _instant(flight.get("departure") or flight.get("departureScheduled")),
            _instant(known.get("departure")),
        ),
    ]
    for i in (1, 2):
        if not pairs[i][0] and not pairs[i][1]:
            pairs[i] = stand_ins.pop(0)
    return sum(bool(there) and there == here for there, here in pairs)


def another_flight(flight: dict, known: Mapping) -> bool:
    """Whether the record is another flight than the booking's: fewer than two of its parts
    agree with `known` (`_agreeing`)."""
    return _agreeing(flight, known) < 2


def as_told(flight: dict, known: Mapping) -> bool:
    """Whether the record is the flight as it was told, all three parts agreeing
    (`_agreeing`): one the owner corrected there by hand in one part is still linked, but no
    longer the flight the booking describes."""
    return _agreeing(flight, known) == 3


class _UnknownAirport(Exception):
    """The flight log knows no airport by this code."""


class _WriteFailed(Exception):
    """The flight log's error on a create or an update itself: the one a refusal of the booking
    can come from (any other request's error describes the flight log)."""

    def __init__(self, error: AirTrailError) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True)
class PassReport:
    done: dict[str, int]  # operation → how many
    # the key cannot be used (rejected, short of a scope, forbidden by the role, or another
    # user's than the ledger's): the pass ended early
    stopped: bool
    down: bool = False  # the flight log did not answer: the pass ended early


class Syncer:
    """Keeps the flight log in step with the trip store, one flight at a time (`sync_one`) or a
    pass over every flight that is due (`run_pass`). With no client nothing is sent and every
    flight waits. With `dry_run` nothing is sent either: each flight's request is logged once,
    the flight stays pending, and a pass does not take it again until what it would be sent for
    changes. `alert` takes one line for the owner (the log has it too)."""

    def __init__(
        self,
        *,
        store: TripStore,
        ledger: Ledger,
        book: PassengerBook,
        client: AirTrailClient | None,
        refs: AirTrailRefs | None,
        dry_run: bool,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.book = book
        self.client = client
        if refs is None and client is not None:
            refs = AirTrailRefs(client, ledger)
        self.refs = refs
        self.dry_run = dry_run
        self.alert = alert
        self._in_pass = False
        self._bound = False  # this pass began, and the ledger is bound to the key's user
        self._log_answers = False  # the flight log's profile was read in this pass
        self._why = ""  # why the flight in hand was unlinked, for its line in the log

    # ------------------------------------------------------------------ passes
    def run_pass(self, *, now: dt.datetime | None = None, limit: int = 50) -> PassReport:
        """`sync_one` over the first `limit` flights due. Given no `now`, the clock is read for
        each flight as the pass reaches it, so a pass that runs long never claims a flight with
        an old time. In a dry run a flight whose request is already logged is not due until
        what it would be sent for changes (`_logged`): the flight log is not asked about it pass
        after pass, and the limit reaches every flight that waits. With a flight due, the pass
        begins with the flight log's profile (`_begin`), which may end it before any flight is
        claimed. A key that cannot be used or an outage ends the pass (the rows not reached stay
        as they are). An error that is not the flight log's answer, on one flight, is logged,
        that flight waits out a backoff, the pass goes on, and the first such error is raised at
        its end."""
        done: Counter[str] = Counter()
        first: Exception | None = None
        self._in_pass, self._bound, self._log_answers = True, False, False
        try:
            due = self.store.due_for_sync(now=now or util.now())
            if self.dry_run:
                due = [ti for ti in due if not self._logged(ti)]
            due = due[:limit]
            if due and self.client is not None:
                ended = self._begin(now or util.now())
                if ended is not None:
                    done[ended] += 1
                    due = []
            for ti in due:
                try:
                    operation = self.sync_one(ti.id, now=now)
                except Exception as exc:  # noqa: BLE001 - raised once the others had their turn
                    log.exception("sync: trip item %s failed", ti.id)
                    first = first or exc
                    continue
                done[operation] += 1
                if operation in ENDS_A_PASS:
                    break
        finally:
            self._in_pass, self._bound, self._log_answers = False, False, False
        if first is not None:
            raise first
        stopped, down = bool(done["stop"] or done["other"]), bool(done["outage"])
        if not (stopped or down) and any(n for op, n in done.items() if op != "wait"):
            self._went_through()
        return PassReport(done=dict(done), stopped=stopped, down=down)

    def _begin(self, now: dt.datetime, *, note_down: bool = True) -> str | None:
        """Before the first flight of a pass, or of a flight synced on its own: the flight log's
        profile is read (a client asks once and keeps a success) and the ledger is bound to its
        user (`lifecycle.bind`), which also keeps what the key may do. None when the pass may
        go on. A refusal of the profile stops the pass as a refusal of any request does
        (`_key_refused`), and anything else ends it as an outage (noted, `_down`, unless
        `note_down` is false). A key that answers as another user than the one the ledger's
        flights were sent as ends it as `other`, the owner told once: every flight id the ledger
        holds would name somebody else's flight, or none. Nothing is claimed and no flight's row
        is touched."""
        assert self.client is not None
        try:
            profile = self.client.profile()
        except AirTrailError as exc:
            if self._key_refused(exc, now):
                self._answered("stop")
                return "stop"
            if note_down:
                self._down(now)
            return "outage"
        if bind(self.ledger, profile):
            if self.ledger.kv.get(KEY_PROBLEM_KV) == OTHER_LOG:
                self.ledger.kv.set(KEY_PROBLEM_KV, "")
            self._bound = True
            return None
        self._stop(
            OTHER_LOG,
            now,
            "The flight log tripbox reaches now is not the one its flights were sent to (another"
            " AirTrail, or another user's key). Nothing is sent. If this is the flight log to"
            " keep, run: tripbox airtrail forget --apply",
        )
        self._answered("other")
        return "other"

    def sync_one(self, item_id: int, *, now: dt.datetime | None = None) -> str:
        """Bring one flight's record in the flight log in step, if the flight is due; the
        operation it took (one of OPERATIONS). `wait` when there is no client, when another pass
        holds the flight, or when it is not due (re-read once it is claimed). On its own, not in
        a pass, a flight that is due is a pass of one: it begins as a pass does (`_begin`),
        which may end it before the flight is claimed. The claim is released on every path; an
        error that is not the flight log's answer is raised once it is, and the flight then
        waits out a backoff."""
        now = now or util.now()
        if self.client is None:
            return "wait"
        if not self._in_pass:
            self._bound, self._log_answers = False, False
            alone = self.store.item(item_id)
            if alone is not None and self._due(alone, now):
                ended = self._begin(now)
                if ended is not None:
                    return ended
        self._why = ""
        if not self.store.claim_sync(item_id, now=now, seconds=CLAIM_SECONDS):
            return "wait"
        ti: TripItem | None = None
        try:
            ti = self.store.item(item_id)
            operation = self._attempt(ti, now) if ti is not None else "wait"
        except Exception:
            tries = (ti.sync_attempts if ti is not None else 0) + 1
            retry = now + dt.timedelta(seconds=backoff(tries))
            self.store.release_sync(item_id, now=now, retry_at=retry)
            raise
        except BaseException:
            self.store.release_sync(item_id, now=now)
            raise
        self.store.release_sync(item_id, now=now)
        if operation not in ("wait", "outage"):  # the flight log answered this attempt
            self._answered(operation)
            if not self._in_pass and operation != "stop":
                self._went_through()
        if operation != "wait":
            why = f" ({self._why})" if self._why else ""
            log.info("sync: trip item %s: %s%s", item_id, operation, why)
        return operation

    def bound(self, now: dt.datetime | None = None) -> str | None:
        """Whether the flight log may be written now: None when the key answers and the ledger
        is bound to its user (`_begin`, as a pass begins), else the operation that says why not
        (`stop`, `other`, `outage`); `wait` with no client. An outage found here notes nothing:
        the outage clock is the sync's (`_down`)."""
        if self.client is None:
            return "wait"
        self._bound, self._log_answers = False, False
        return self._begin(now or util.now(), note_down=False)

    def fill(
        self, item_id: int, fills: Mapping[str, object], *, now: dt.datetime | None = None
    ) -> tuple[str, dict]:
        """Put what is in `fills` (FILL_FIELDS only, else ValueError) on the record of a flight
        that reads `synced`, each value only where the record holds nothing; the operation and
        what was put. The flight is claimed as a pass claims it (`wait`, nothing asked, while a
        pass holds it, or when it is not synced or there is no client); then it goes as a pass
        of one does: the profile (`_begin`: the ledger bound to the key's user, else `stop`,
        `other` or `outage`), its record read, and that record unlinked (`detach`) when it is
        not the owner's there or has become another flight. `nothing` when no blank is left to
        fill, or when the record's day, number or origin is no longer what it was told (the
        owner corrected it there: the link stays, nothing is put); `dry_run` asks nothing. An
        outage notes nothing (the outage clock is the sync's, and a fill runs when no pass may
        follow to stop it) and nothing of the flight's row changes; a refusal of the write that
        says nothing of the key or the flight is raised (an AirTrailError) for the caller to
        report."""
        bad = sorted(set(fills) - set(FILL_FIELDS))
        if bad:
            raise ValueError(f"not a field enrichment fills: {', '.join(bad)}")
        now = now or util.now()
        ti = self.store.item(item_id)
        if self.client is None or ti is None or not self._fillable(ti):
            return "wait", {}
        if self.dry_run:
            return "dry_run", {}
        self._why = ""
        if not self.store.claim_sync(item_id, now=now, seconds=CLAIM_SECONDS):
            return "wait", {}
        try:
            ended = self.bound(now)
            ti = self.store.item(item_id)
            if ended is not None:
                operation, put = ended, {}
            elif ti is None or not self._fillable(ti):
                operation, put = "wait", {}
            else:
                operation, put = self._fill(ti, fills, now)
        finally:
            self.store.release_sync(item_id, now=now)
        if operation not in ("wait", "outage"):
            self._answered(operation)
        if operation != "wait":
            why = f" ({self._why})" if self._why else ""
            log.info("fill: trip item %s: %s%s", item_id, operation, why)
        return operation, put

    @staticmethod
    def _fillable(ti: TripItem) -> bool:
        """A flight the flight log holds as its booking says: synced, with its record's id."""
        return (
            ti.kind == "flight" and ti.sync_state == "synced" and ti.airtrail_flight_id is not None
        )

    def _fill(
        self, ti: TripItem, fills: Mapping[str, object], now: dt.datetime
    ) -> tuple[str, dict]:
        """One claimed, synced flight: its record read and checked as `_update` checks it, then
        the blanks filled."""
        assert self.client is not None and ti.airtrail_flight_id is not None
        flight_id = ti.airtrail_flight_id
        desired = checks.desired(ti.payload, ti.passengers, ti.status, self.book)
        try:
            try:
                flight = self.client.get_flight(flight_id)
            except AirTrailError as exc:
                if self._no_longer_theirs(exc):
                    return self._detach(ti, NOT_THEIRS), {}
                raise
            if self._off_it(flight):
                return self._detach(ti, NOT_THEIRS), {}
            if self._another_flight(flight, ti.told or desired):
                return self._detach(ti, "the record there is another flight"), {}
            if not as_told(flight, ti.told or desired):
                # corrected there by hand: still the booking's, but what was looked up is the
                # booking's flight, not this record's; the link is the sync's to keep
                self._why = "the record there was corrected by hand"
                return "nothing", {}
            inp = flight_to_input(flight)
            put = {
                k: v for k, v in fills.items() if v not in (None, "") and inp.get(k) in (None, "")
            }
            if not put:
                return "nothing", {}
            inp.update(put)
            try:
                self.client.update_flight(flight_id, inp)
            except AirTrailError as exc:
                if self._no_longer_theirs(exc, read_again=flight_id):
                    return self._detach(ti, NOT_THEIRS), {}
                if 400 <= exc.status < 500 and exc.status not in (401, 403, 429):
                    raise _WriteFailed(exc) from exc
                raise
        except AirTrailError as exc:
            return self._fill_failed(exc, now), {}
        except _WriteFailed as failed:
            raise failed.error from None
        return "fill", put

    def _fill_failed(self, exc: AirTrailError, now: dt.datetime) -> str:
        """A fill's read or write the flight log did not take: a refused key stops it as it
        stops a pass; anything else is an outage, noted nowhere, with nothing of the flight's
        row changed: its sync is not in question."""
        if self._key_refused(exc, now):
            return "stop"
        return "outage"

    def _attempt(self, ti: TripItem, now: dt.datetime) -> str:
        """One claimed flight: sent if it is due, and the flight log's error, if any, read.
        Nothing is asked of the flight log for a flight unless the pass began with the ledger
        bound to the key's user (`_begin`)."""
        if not self._due(ti, now) or not self._bound:
            return "wait"
        try:
            return self._send(ti, now)
        except _UnknownAirport:
            retry = now + dt.timedelta(seconds=backoff(ti.sync_attempts + 1))
            self.store.sync_failed(ti.id, code="airport", now=now, retry_at=retry)
            return "airport"
        except _WriteFailed as failed:
            return self._failed(ti, failed.error, now, writing=True)
        except AirTrailError as exc:
            return self._failed(ti, exc, now, writing=False)

    def _due(self, ti: TripItem, now: dt.datetime) -> bool:
        """Whether the flight may be sent now: the store says it is due (`pending`, or `blocked`
        only by an airport the flight log did not know), it is not removed, not in a deleted trip
        and not waiting out a backoff, the flight log is on, and the owner is on the leg."""
        if ti.kind != "flight" or ti.removed_at is not None or not self.store.airtrail_on():
            return False
        if ti.trip_id is not None and self.store.get(ti.trip_id) is None:
            return False
        if not (
            ti.sync_state == "pending"
            or (ti.sync_state == "blocked" and ti.checks == ("airtrail_airport",))
        ):
            return False
        if ti.sync_after is not None and util.parse_iso(ti.sync_after) > now:
            return False
        return checks.owner(ti.kind, ti.passengers, ti.payload, self.book) == "on"

    # ------------------------------------------------------------------ sending
    def _send(self, ti: TripItem, now: dt.datetime) -> str:
        desired = checks.desired(ti.payload, ti.passengers, ti.status, self.book)
        if ti.airtrail_flight_id is None:
            return self._create(ti, desired)
        return self._update(ti, ti.airtrail_flight_id, desired, now)

    def _create(self, ti: TripItem, desired: dict) -> str:
        """A flight with no flight id: the one the flight log already holds for the key's user
        is adopted as it stands; else it is created. A record whose passengers do not list that
        user (a companion's, or another user's) is not the owner's and is never adopted
        (`not_listed`, the rule `_off_it` and the backfill read too)."""
        assert self.client is not None and self.refs is not None
        p = ti.payload
        found = self.client.find_flight(
            flight_code=str(p.get("flight_code") or ""),
            date_local=str(p.get("date_local") or ""),
            origin_iata=p.get("origin"),
            user_id=self.client.user_id(),
        )
        if found is not None:
            self.store.sync_done(ti.id, flight_id=int(found["id"]), told=desired)
            return "adopt"
        from_id = self._airport_id(p.get("origin"))
        to_id = self._airport_id(p.get("destination"))
        airline_id = self.refs.airline_id(p.get("carrier"))
        body = build_flight_input(
            self._item(ti),
            self.book,
            user_id=self.client.user_id(),
            from_id=from_id,
            to_id=to_id,
            airline_id=airline_id,
        )
        if self.dry_run:
            self._log_once(ti, "POST /api/v1/flights", body)
            return "dry_run"
        try:
            flight_id = self.client.create_flight(body)
        except AirTrailError as exc:
            raise _WriteFailed(exc) from exc
        self.store.sync_done(ti.id, flight_id=flight_id, told=desired)
        return "create"

    def _update(self, ti: TripItem, flight_id: int, desired: dict, now: dt.datetime) -> str:
        """A flight the flight log holds: what changed since it was last told is put on the
        flight as it stands there. No longer the owner's there, by the flight log's answer on
        reading it or on writing it (`_no_longer_theirs`) or by the record's own passengers
        (`_off_it`), it is unlinked; so is one whose record there has become another flight
        (`_another_flight`); both before anything is put on it. Nothing to change, it is
        recorded as told and nothing is sent."""
        assert self.client is not None
        try:
            flight = self.client.get_flight(flight_id)
        except AirTrailError as exc:
            if not self._no_longer_theirs(exc):
                raise
            return self._detach(ti, NOT_THEIRS)
        if self._off_it(flight):
            return self._detach(ti, NOT_THEIRS)
        if self._another_flight(flight, ti.told or desired):
            return self._detach(ti, "the record there is another flight")
        body, named = self._changed(ti, flight, desired, ti.told or {}, now)
        if not named:
            self.store.sync_done(ti.id, flight_id=flight_id, told=desired)
            return "adopt"
        if self.dry_run:
            self._log_once(ti, f"PUT /api/v1/flights/{flight_id}", body)
            return "dry_run"
        try:
            self.client.update_flight(flight_id, body)
        except AirTrailError as exc:
            if self._no_longer_theirs(exc, read_again=flight_id):
                return self._detach(ti, NOT_THEIRS)
            raise _WriteFailed(exc) from exc
        self.store.sync_done(ti.id, flight_id=flight_id, told=desired)
        return "update"

    def _detach(self, ti: TripItem, why: str) -> str:
        """Unlink the flight (`TripStore.sync_detached`): nothing is sent, and nothing makes it
        again unasked. `why` goes into its line in the log."""
        self._why = why
        self.store.sync_detached(ti.id)
        return "detach"

    def _no_longer_theirs(self, exc: AirTrailError, *, read_again: int | None = None) -> bool:
        """Whether the flight log's answer to the read or the write of one flight says the
        flight is no longer the owner's there. AirTrail refuses an own-flights key that reaches
        for a flight its user is not on by asking for the any-flights scope, when the user's
        role could see the flight, and says not found when the role could not, as it does for a
        flight that is gone. A not-found is believed only while the flight log otherwise
        answers (`_confirm_answering`); one that answers a write is believed only when the
        flight, read again (`read_again`, its id), is not there either, since AirTrail also
        says not found to a write its role refuses on a flight that is there."""
        assert self.client is not None
        if exc.status == 403:
            scope = missing_scope(exc)
            return scope is not None and scope.endswith(".any")
        if exc.status != 404:
            return False
        if read_again is None:
            self._confirm_answering()
            return True
        try:
            self.client.get_flight(read_again)
        except AirTrailError as again:
            if self._no_longer_theirs(again):
                return True
            raise
        return False

    def _off_it(self, flight: dict) -> bool:
        """Whether the record lists passengers and the key's user is none of them: the flight
        is no longer the owner's there. The flight log's refusal says so only to a key held to
        its user's own flights; a key that reaches any flight, under a role that may see them
        all, reads and could write this one, so whose flight it is is read from the record
        (`not_listed`)."""
        assert self.client is not None
        return not_listed(flight, self.client.user_id())

    _another_flight = staticmethod(another_flight)

    def _confirm_answering(self) -> None:
        """Before the first unlink of a pass, one read a flight log that is up always answers
        (its profile, read afresh): a not-found from a wrong address is then the read's own
        failure, and nothing is unlinked. Nor is anything unlinked while that profile says the
        user's role may not read the owner's own flights (it names what the role grants, and
        reading them is not among it): AirTrail then says not found of every one of them, and
        the pass stops for the role instead."""
        assert self.client is not None
        if self._log_answers:
            return
        profile = read_profile(self.client.me())
        report = key_report(profile)
        if report is not None and profile.permissions and READS_OWN in report.denied:
            raise AirTrailError(403, "it may not read your own flights", code="forbidden")
        self._log_answers = True

    def _changed(
        self, ti: TripItem, flight: dict, desired: dict, told: dict, now: dt.datetime
    ) -> tuple[dict, list[str]]:
        """The flight's input (`flight_to_input`) with each field of `desired` that differs from
        `told` put on it, and the fields that changed it, in `desired`'s order. Only those are
        touched, so what enrichment or the owner filled in there stays; a value the flight
        already holds is no change. The date, the departure and the arrival are each put on their
        own, an end the booking has no time for is never blanked there, and the length goes with
        a moved end only when the booking knows both instants. A cancellation puts a line first
        in the note, a booking that stands again too, and any change appends a line naming the
        fields."""
        p = ti.payload
        inp = flight_to_input(flight)
        before = copy.deepcopy(inp)

        def moved(*keys: str) -> bool:
            return any(not _same(k, inp.get(k), before.get(k)) for k in keys)

        named: list[str] = []
        ends = (
            ("date", "date_local", ("date",)),
            ("departure", "dep_utc", ("departure", "departureScheduled")),
            ("arrival", "arr_utc", ("arrival", "arrivalScheduled")),
        )
        for field, stored, keys in ends:
            if desired[field] is None or desired[field] == told.get(field):
                continue
            inp.update(dict.fromkeys(keys, p.get(stored)))
            if moved(*keys):
                named.append(field)
        if named and p.get("dep_utc") and p.get("arr_utc"):
            inp["duration"] = duration_seconds(p)
        if desired["flight"] != told.get("flight"):
            inp["flightNumber"] = p.get("flight_code")
            if moved("flightNumber"):
                named.append("flight")
        for end, key in (("from", "fromId"), ("to", "toId")):
            if desired[end] != told.get(end):
                new = self._airport_change(flight.get(end), desired[end])
                if new is not None:
                    inp[key] = new
                if moved(key):
                    named.append(end)
        if desired["airline"] != told.get("airline"):
            new = self._airline_change(flight.get("airline"), desired["airline"])
            if new is not None:
                inp["airlineId"] = new
            if moved("airlineId"):
                named.append("airline")
        if desired["people"] != (told.get("people") or []):
            self._people(inp, desired["people"], told.get("people") or [])
            if moved("passengers"):
                named.append("people")
        day = now.astimezone(dt.UTC).date().isoformat()
        first: list[str] = []
        if desired["cancelled"] and not told.get("cancelled"):
            first.append(f"CANCELLED {day}")
        elif told.get("cancelled") and not desired["cancelled"]:
            first.append(f"reinstated {day}")
        if first and str(inp.get("note") or "").split("\n", 1)[0] == first[0]:
            first = []  # the flight log has it already (sent before the store was told)
        if first:
            named.append("cancelled")
        if named:
            line = f"changed {day}: " + ", ".join(NAMES[field] for field in named)
            inp["note"] = _note(first, str(inp.get("note") or ""), line)
        return inp, named

    def _airport_id(self, code: str | None) -> int:
        """The flight log's id for an airport; _UnknownAirport when it knows none."""
        assert self.refs is not None
        found = self.refs.airport_id(code)
        if found is None:
            raise _UnknownAirport(code)
        return found

    def _airport_change(self, current: dict | None, code: str | None) -> int | None:
        """The id to put on the flight for the airport `code`, or None when the flight's own
        airport already is that one."""
        current = current or {}
        if code and str(current.get("iata") or "").upper() == code.upper():
            return None
        found = self._airport_id(code)
        return None if found == current.get("id") else found

    def _airline_change(self, current: dict | None, code: str | None) -> int | None:
        """The id to put on the flight for the airline `code`, or None when the flight already
        has it, or when the flight log knows no airline by that code (the one there stays)."""
        assert self.refs is not None
        current = current or {}
        if code and str(current.get("iata") or "").upper() == code.upper():
            return None
        found = self.refs.airline_id(code)
        return None if found is None or found == current.get("id") else found

    def _people(self, inp: dict, want: list[dict], was: list[dict]) -> None:
        """Put the people of `want` on the flight's passengers as they changed since `was`: one
        who is new is added (the owner by user id, a companion by name) unless the flight has
        them already; a seat or a class that changed is set; a companion who left is removed.
        The owner's own row is never removed."""
        rows: list[dict] = inp.setdefault("passengers", [])
        told = {person["who"]: person for person in was}
        for person in want:
            who = person["who"]
            before = told.get(who, {})
            if person == before:
                continue
            row = self._row(rows, who)
            cls = seat_class(person.get("class"))
            if row is None:
                assert self.client is not None
                who_is = {"userId": self.client.user_id()} if who == "me" else {"guestName": who}
                rows.append({**who_is, "seatNumber": person.get("seat"), "seatClass": cls})
                continue
            if person.get("seat") != before.get("seat"):
                row["seatNumber"] = person.get("seat")
            # a cabin with no class word never blanks the class there
            if person.get("class") != before.get("class") and cls is not None:
                row["seatClass"] = cls
        staying = {person["who"] for person in want}
        gone = {who.casefold() for who in told if who != "me" and who not in staying}
        inp["passengers"] = [
            r
            for r in rows
            if r.get("userId") or str(r.get("guestName") or "").casefold() not in gone
        ]

    def _row(self, rows: list[dict], who: str) -> dict | None:
        """The flight's passenger row for `who`: the owner's by user id, a companion's by name."""
        if who == "me":
            assert self.client is not None
            uid = self.client.user_id()
            return next((r for r in rows if r.get("userId") == uid), None)
        name = who.casefold()
        return next(
            (
                r
                for r in rows
                if not r.get("userId") and str(r.get("guestName") or "").casefold() == name
            ),
            None,
        )

    def _item(self, ti: TripItem) -> Item:
        """The booking as `build_flight_input` reads one."""
        names = list(ti.passengers)
        return Item(
            kind=ti.kind,
            key=ti.item_key,
            payload=ti.payload,
            pnr=ti.confirmation,
            passengers=names,
            classified=self.book.classify(names),
            status=ti.status,
            confidence=1.0,
        )

    def _sent_for(self, ti: TripItem) -> str:
        """The digest of what the flight would be sent for: the record the flight log holds of
        it, what that record was told, and what it should hold (`checks.desired`). Nothing of
        the flight log's own goes in, so it is known without asking it."""
        state = {
            "flight": ti.airtrail_flight_id,
            "told": ti.told,
            "desired": checks.desired(ti.payload, ti.passengers, ti.status, self.book),
        }
        return hashlib.sha256(json.dumps(state, default=str, sort_keys=True).encode()).hexdigest()

    def _logged(self, ti: TripItem) -> bool:
        """Whether a dry run has logged the request for the flight as it now stands. A flight
        whose digest cannot be worked out is not: its own attempt meets the error and backs it
        off."""
        if ti.sync_logged is None:
            return False
        try:
            return ti.sync_logged == self._sent_for(ti)
        except Exception:  # noqa: BLE001 - the flight's attempt raises it where it is handled
            return False

    def _log_once(self, ti: TripItem, what: str, body: dict) -> None:
        """A dry run's request, logged once for what the flight would be sent for: until that
        changes it is neither logged again nor due (`_logged`)."""
        digest = self._sent_for(ti)
        if digest == ti.sync_logged:
            return
        log.info("dry run: would %s %s", what, json.dumps(body, default=str, sort_keys=True))
        self.store.sync_logged(ti.id, digest)

    # ------------------------------------------------------------------ failures
    def _failed(
        self, ti: TripItem, exc: AirTrailError, now: dt.datetime, *, writing: bool
    ) -> str:
        """What the flight log's error means, by the first rule that holds: a refusal of the key
        (`_key_refused`: rejected, short of a scope, forbidden by the role) stops the pass and
        writes nothing to the row, whatever the request; a refusal of the create or the update
        itself (`writing`) is a check on the booking, told with the details the flight log
        gave; anything else — no answer, too many requests, a server error, or a refusal of a
        read that describes the flight log (the flight list, a look-up, the profile), which one
        cause would make of every booking — is an outage, retried after a backoff. (An answer
        that says one flight is no longer the owner's there never comes here: `_update` unlinks
        that flight.)"""
        if self._key_refused(exc, now):
            return "stop"
        status = exc.status
        if writing and 400 <= status < 500 and status != 429:
            self.store.sync_failed(ti.id, code="refused", now=now, retry_at=None)
            if ti.sync_error != "refused":  # once for the booking, until it is sent
                p = ti.payload
                self._alert(
                    f"Your flight log refused flight {p.get('flight_code')}"
                    f" {p.get('origin')}→{p.get('destination')} on {p.get('date_local')}"
                    f" (HTTP {status}: {redact(exc.message)[:SAID]}); it is marked on the trips"
                    " page."
                )
            return "refused"
        retry = now + dt.timedelta(seconds=backoff(ti.sync_attempts + 1))
        self.store.sync_failed(ti.id, code="outage", now=now, retry_at=retry)
        self._down(now)
        return "outage"

    def _key_refused(self, exc: AirTrailError, now: dt.datetime) -> bool:
        """Whether the flight log's answer refuses the key rather than the request, by the first
        rule that holds: 401 is a key it rejects; 403 that names a scope is a key short of that
        scope; 403 `forbidden` is the user's role refusing what the key allows; any other 403
        is taken as a rejected key. Each is noted and told in its own words (`_stop`)."""
        status = exc.status
        if status not in (401, 403):
            return False
        scope = missing_scope(exc) if status == 403 else None
        if scope is not None:
            self._stop(
                f"scope:{scope}",
                now,
                f"The key tripbox uses for your flight log lacks the scope {scope}; nothing is"
                " sent until a key with all six is set (tripbox check lists what a key lacks).",
            )
        elif status == 403 and exc.code == "forbidden":
            self._stop(
                ROLE,
                now,
                "Your role in your flight log does not permit what tripbox needs"
                f" ({redact(exc.message)[:SAID]}); nothing is sent until it does.",
            )
        else:
            self._stop(
                UNAUTHORIZED,
                now,
                f"Your flight log rejected the key tripbox uses (HTTP {status}); nothing is sent"
                " to it until the key works.",
            )
        return True

    def _stop(self, word: str, now: dt.datetime, text: str) -> None:
        """A pass stops for the key, for the reason `word`: the word is kept, and the owner is
        told `text` unless that word is the one kept already (a ledger that says only when the
        key was rejected counts as UNAUTHORIZED). When the key was rejected is kept while it
        is, and forgotten for any other reason: each of those is an answer to a key the flight
        log accepts."""
        kv = self.ledger.kv
        rejected = kv.get(KEY_REJECTED)
        known = kv.get(KEY_PROBLEM_KV) or (UNAUTHORIZED if rejected else "")
        if word == UNAUTHORIZED:
            if not rejected:
                kv.set(KEY_REJECTED, stamp(now))
        elif rejected:
            kv.set(KEY_REJECTED, "")
        if kv.get(KEY_PROBLEM_KV) != word:
            kv.set(KEY_PROBLEM_KV, word)
        if known != word:
            self._alert(text)

    def _down(self, now: dt.datetime) -> None:
        """The flight log did not answer at `now`: since when is kept, and once that is
        ALERT_AFTER seconds ago the owner is told, once."""
        kv = self.ledger.kv
        since = kv.get(DOWN_SINCE)
        if not since:
            since = stamp(now)
            kv.set(DOWN_SINCE, since)
        if kv.get(ALERTED) or now - util.parse_iso(since) < dt.timedelta(seconds=ALERT_AFTER):
            return
        kv.set(ALERTED, stamp(now))
        self._alert(
            "Your flight log has not answered for over an hour; flights waiting for it are sent"
            " once it answers again."
        )

    def _answered(self, operation: str) -> None:
        """The flight log answered, and `operation` came of it: an outage is over, so the next
        one is timed from its own start; and unless the answer stopped the pass it proves the
        flight log accepts the key, so a rejected key is over too and the next rejection alerts
        again. It says nothing of a scope the key lacked or of what the role forbade: those are
        over when a pass gets through (`_went_through`)."""
        kv = self.ledger.kv
        for key in (DOWN_SINCE, ALERTED):
            if kv.get(key):
                kv.set(key, "")
        if operation in ("stop", "other"):
            return
        if kv.get(KEY_REJECTED):
            kv.set(KEY_REJECTED, "")
        if kv.get(KEY_PROBLEM_KV) == UNAUTHORIZED:
            kv.set(KEY_PROBLEM_KV, "")

    def _went_through(self) -> None:
        """A pass reached a flight and ended neither stopped nor in an outage: the flight a
        scope or the role held up was first in line and is held up no more, so that reason is
        over and the next such refusal alerts again. (An answer about another flight, on the
        way to one that is still refused, proves no such thing, and tells the owner nothing
        again.)"""
        problem = self.ledger.kv.get(KEY_PROBLEM_KV)
        if problem and problem != OTHER_LOG:
            self.ledger.kv.set(KEY_PROBLEM_KV, "")

    def _alert(self, text: str) -> None:
        log.warning("%s", text)
        if self.alert is None:
            return
        try:
            self.alert(text)
        except Exception:  # noqa: BLE001 - an alert that cannot be sent is logged above
            log.exception("the alert could not be sent")
