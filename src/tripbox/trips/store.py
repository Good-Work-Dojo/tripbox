# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""TripStore: the only writer of the ledger's `trips` and `trip_items` tables. A trip item is
the one current state of a real-world booking — what every email about it has been merged into —
so the booking plan, the UI and the text renderer never reason about the rows of the message log.

Every method that changes a booking ends in `settle`, which makes what is stored on the row
follow from the booking's own fields: its derived times and its key, the checks open on it, the
trip it is in (none while it has no date) and where its sync with a flight log stands.

Deletions on the page are soft: a removed booking keeps its row with `removed_at`
set, a deleted trip keeps its rows with `deleted_at` set; both are hidden from every reader,
listed under *Recently deleted*, restorable, and purged by `purge_deleted` after 30 days."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import SimpleNamespace

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.trips import grouping
from tripbox.trips.covers import CoverStore
from tripbox.util import merge_payload, now_iso, parse_iso
from tripbox.validate import checks
from tripbox.validate.keys import draft_key, is_draft, own_draft_key, real_key
from tripbox.validate.passengers import PassengerBook, people, person
from tripbox.validate.rebuild import rebuild_item
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import (
    CARRIER,
    FLIGHT_NO,
    IATA_AIRPORT,
    PRINTED_ZONES,
    Item,
    _norm,
    carry_printed_zones,
    day_alone,
    drop_printed_zones,
    parse_local,
    without_days_alone,
    without_unreadable,
)

log = logging.getLogger("tripbox.trips")

RETENTION_DAYS = 30
AIRTRAIL_KV = "airtrail"  # the ledger's `kv` row saying whether a flight log is kept: on or off
# The ledger's `kv` row saying when the flight log was last switched off (a UTC time; absent while
# it never was): a booking filed since then was filed while none was kept.
AIRTRAIL_OFF_AT_KV = "airtrail_off_at"
# The ledger's `kv` row saying whether the setting holds everything back from the flight log
# (`on`) or not (`off`): the page says why a flight waits from it.
DRY_RUN_KV = "airtrail_dry_run"
# How long a sync pass's claim on a flight holds; an older one is a dead process's and lapses.
# The syncer's own constant (write.sync.CLAIM_SECONDS) is the same number.
CLAIM_SECONDS = 600
SYNC_ERRORS = ("outage", "refused", "airport")  # why the last sync attempt did not finish
# What a flight's stored fields derive, the way the validator derives them: settle writes these
# back from the booking rebuilt from its stored fields, and touches nothing else of the payload.
FLIGHT_DERIVED = (
    "dep_local",
    "arr_local",
    "dep_utc",
    "arr_utc",
    "date_local",
    "duration_min",
    "flight_code",
    "origin_name",
    "origin_city",
    "origin_tz",
    "destination_name",
    "destination_city",
    "destination_tz",
)
# The kinds whose key moves with the booking's fields (a day or a number in it); a stay or a car
# with a real key keeps it, and a corrected code is an alias (see TripStore.edit_item).
REKEYED = ("flight", "train")
# `payload["source"]` of a flight whose fields the flight log gave (the AirTrail backfill files it,
# a relink carries the flight log's schedule into it). Until an email changes the booking (the
# merge drops this source) or the owner edits one of FLIGHT_FIELDS, settle keeps its times, day
# and key as the flight log said them: the flight log's record may have no time (a date only) or
# no zone, and its airports need not be in the bundled table.
FLIGHT_LOG_SOURCE = "airtrail-backfill"
FLIGHT_FIELDS = ("carrier", "flight_number", "origin", "destination", "dep_local", "arr_local")
BLANK = (None, "", [], {})  # what merge_payload treats as "the email did not print it"
NO_OWNER = "no owner is named in travellers.yaml; add owner: names: first"
# What a draft merged into the booking that holds its key brings beyond the values the booking
# lacks: its travellers and the names it prints, its seats, and the owner's edits of it.
JOINED = ("passengers", "seats")
JOINED_MAPS = ("seat_by_passenger", "owner_edits")
# What such a draft never brings: the owner's look and the travellers the owner or an email took
# off it, which were about the draft and not the booking, and what its own emails left on it (a
# recorded change, a cancellation's date, the bookings its route or its names were taken from). Its
# `raised` stamps and its keys are joined on their own.
LEFT_WITH_THE_DRAFT = (
    "looked_at",
    "taken_off",
    "cancelled_names",
    "change_recorded",
    "cancelled_email_date",
    "route_inherited_from",
    "passengers_inherited_from",
)
# `payload["taken_off"]`: the travellers (as printed) the owner took off the booking on the page.
# No email puts one of them back, whatever its date, until the owner adds them again; anyone else
# an email names still joins.
TAKEN_OFF = "taken_off"
# `payload["cancelled_names"]`: the travellers (as named) a partial cancellation took off the
# booking, each with that email's date. An email older than that puts none of them back (a late
# copy of the first receipt, say); a newer one (a rebooking) does.
CANCELLED_NAMES = "cancelled_names"
LIMITS = {  # server-side caps matching the forms' maxlength
    "title": 80,
    "place": 80,
    "property_name": 120,
    "provider": 120,
    "address": 200,
    "pickup_location": 200,
    "dropoff_location": 200,
    "confirmation_code": 40,
    "carrier": 3,
    "flight_number": 8,
    "train_number": 20,
    "origin": 120,  # a flight's airport code, or a train's station
    "destination": 120,
    "passengers": 80,  # each name
}
MAX_TRAVELLERS = 12  # names a booking's travellers list keeps from the page
# What the page may edit, of each kind of booking.
EDITABLE = {
    "flight": (
        "carrier",
        "flight_number",
        "origin",
        "destination",
        "dep_local",
        "arr_local",
        "confirmation_code",
        "passengers",
    ),
    "train": (
        "provider",
        "train_number",
        "origin",
        "destination",
        "dep_local",
        "arr_local",
        "confirmation_code",
        "passengers",
    ),
    "lodging": ("property_name", "address", "check_in", "check_out", "confirmation_code"),
    "car": (
        "provider",
        "pickup_location",
        "dropoff_location",
        "pickup_time",
        "dropoff_time",
        "confirmation_code",
    ),
}
# The payload field an edited field is kept under, where that is not its own name: a flight
# keeps its confirmation code as `pnr`. Owner edits are stamped under these names, the ones
# settle, the merge and the syncer read.
STORED_AS = {"flight": {"confirmation_code": "pnr"}}
SPAN_FIELDS = {  # (field, label) for the start and the end of the booking
    "flight": (("dep_local", "departure"), ("arr_local", "arrival")),
    "train": (("dep_local", "departure"), ("arr_local", "arrival")),
    "lodging": (("check_in", "check-in"), ("check_out", "check-out")),
    "car": (("pickup_time", "pick-up"), ("dropoff_time", "drop-off")),
}
# What the key an edit would give a booking names, when another booking already holds it.
KEYED_BY = {"flight": "flight", "train": "train"}
HAND = "hand"  # `payload["source"]` of a booking the owner typed in on the page (add_by_hand)
# The field that names a stay or a car: what its email and a booking typed in must agree on
# wherever both say one (typed_in_like).
NAMED_BY = {"lodging": "property_name", "car": "provider"}
# What add_by_hand says when it refuses a booking.
EMPTY_FORM = "fill in at least one field"
ALREADY_THERE = "that booking is already in your trips"
IN_RECENTLY_DELETED = "that booking is in Recently deleted"


def _clean_text(value, field: str) -> str | None:
    """Collapsed whitespace, clipped to the form's limit, None when nothing is left."""
    if value is None:
        return None
    text = " ".join(str(value).split())[: LIMITS.get(field, 200)]
    return text or None


# Two characters before a flight number's digits, after which a space may come: an airline
# designator when one of the two is a letter.
DESIGNATOR = re.compile(r"([A-Z0-9]{2}) ?(?=[0-9])")


def _flight_number(text: str, carrier: str | None) -> str:
    """A flight number typed on the page without the airline designator it may start with — the
    carrier typed beside it, or two characters, either holding a letter (a designator is never
    two digits, which would be the number's own), followed by a space or by the digits — and
    then nothing but digits; EditRefused otherwise."""
    rest, mine = text.upper(), (carrier or "").strip().upper()
    lettered = lambda code: any(c.isalpha() for c in code)  # noqa: E731
    if mine and lettered(mine) and rest.startswith(mine):
        rest = rest[len(mine) :].lstrip()
    elif (m := DESIGNATOR.match(rest)) and lettered(m.group(1)):
        rest = rest[m.end() :]
    if not (rest.isascii() and rest.isdigit()):
        raise EditRefused("Flight number is not a number", "flight_number")
    return rest


def _clean(kind: str, field: str, value, carrier: str | None = None):
    """A value typed on the page as the booking keeps it: text as `_clean_text` leaves it, a
    carrier and a flight's airports in capitals, a flight number its digits alone once the
    designator before them goes (`carrier` the one typed beside it); the travellers one name per
    line (or a list of names), each name clipped, each once, at most MAX_TRAVELLERS of them."""
    if field == "passengers":
        lines = value.splitlines() if isinstance(value, str) else list(value or [])
        names = (_clean_text(n, field) for n in lines)
        return list(dict.fromkeys(n for n in names if n))[:MAX_TRAVELLERS]
    text = _clean_text(value, field)
    if text is None:
        return None
    if field == "carrier" or (kind == "flight" and field in ("origin", "destination")):
        return text.upper()
    if kind == "flight" and field == "flight_number":
        return _flight_number(text, carrier)
    return text


def _refuse_what_the_page_cannot_take(kind: str, fields: Mapping) -> None:
    """ValueError for a kind of booking the page does not edit, and for a field it does not edit
    of that kind (EDITABLE); EditRefused, naming the field, for a time typed with no date or with
    none that reads as one."""
    allowed = EDITABLE.get(kind)
    if allowed is None:
        raise ValueError(f"{kind} bookings are not edited here")
    for k in fields:
        if k not in allowed:
            raise ValueError(f"{k} is not editable")
    for field, label in SPAN_FIELDS[kind]:
        typed = _clean_text(fields.get(field), field)
        if typed and parse_local(typed) is None:
            raise EditRefused(f"{label} is not a date", field)


def _owner_typed(kind: str, payload: dict, changed: Mapping, at: str) -> None:
    """The values the owner typed (`changed`, by the page's field names) go into `payload` under
    the names it keeps them as (STORED_AS), each stamped at `at` in `payload["owner_edits"]`, so a
    later email overrides one only when it is newer than the typing (_reapply_owner_edits).
    EditRefused, naming the field, when a stay's or a car's end is then before its start."""
    stored_as = STORED_AS.get(kind, {})
    payload.update({stored_as.get(k, k): v for k, v in changed.items()})
    if kind not in REKEYED:
        (start_f, start_l), (end_f, end_l) = SPAN_FIELDS[kind]
        start, end = payload.get(start_f), payload.get(end_f)
        if start and end and _before(str(end), str(start)):
            raise EditRefused(f"{end_l} is before {start_l}", end_f)
    if changed:
        payload["owner_edits"] = {
            **(payload.get("owner_edits") or {}),
            **dict.fromkeys((stored_as.get(k, k) for k in changed), at),
        }


def editable_value(ti: TripItem, field: str):
    """What the booking holds for a field the page edits (EDITABLE): its travellers' names, its
    confirmation code (the payload's, else the codes the booking holds), or the payload's
    value under the name it is kept as (STORED_AS)."""
    if field == "passengers":
        return list(ti.passengers)
    value = ti.payload.get(STORED_AS.get(ti.kind, {}).get(field, field))
    if field == "confirmation_code" and value in BLANK:
        return ti.confirmation
    return value


def _before(end: str, start: str) -> bool:
    """`end` earlier than `start` as a person reads them: by date when either is date-only (a
    same-day date-only check-out next to a timed check-in is odd, not wrong), by time otherwise."""
    e, s = parse_local(end), parse_local(start)
    if e is None or s is None:
        return False
    if e.date() != s.date():
        return e.date() < s.date()
    return len(end) > 10 and len(start) > 10 and e < s


def _as_shown(kind: str, field: str, typed, held) -> bool:
    """Whether what was sent for a field is the booking's value `held` as the page shows it, so
    nobody touched it: a time that prints the same instant ('2027-06-22 15:00' stored by a
    parser and '2027-06-22T15:00' from the form), the same travellers once cleaned, the same
    text for anything else. An untouched value is never rewritten by the cleaning (a clip, a
    flight number's designator)."""
    if field in {f for f, _ in SPAN_FIELDS.get(kind, ())}:
        norm = lambda v: grouping.local_str(str(v)) if v else None  # noqa: E731
        return norm(typed) == norm(held)
    if field == "passengers":
        return _clean(kind, field, typed) == _clean(kind, field, held)
    text = lambda v: "" if v is None else " ".join(str(v).split())  # noqa: E731
    return text(typed) == text(held)


def held_edits(payload: Mapping, new: Mapping) -> set[str]:
    """The fields the owner's edits of a booking (`payload["owner_edits"]`, field → time of the
    edit) hold against an email's payload `new`: every edited field, but one `new` prints when
    the email is newer than the edit, which then wins."""
    edits = payload.get("owner_edits")
    if not isinstance(edits, dict) or not edits:
        return set()
    try:
        when = parse_iso(str(new["email_date"])) if new.get("email_date") else None
    except ValueError:
        when = None
    held: set[str] = set()
    for field, at in edits.items():
        try:
            newer = when is not None and when > parse_iso(str(at))
        except ValueError:
            newer = False
        if not (newer and new.get(field) not in BLANK):
            held.add(field)
    return held


def removed_by_email(
    payload: Mapping, email_date: object, book: PassengerBook | None
) -> set[tuple[str, str]]:
    """The people an email took off the booking (`payload[CANCELLED_NAMES]`) whom an email dated
    `email_date` does not put back: each one taken off by an email that is not older than it.
    An email with no date, or one that cannot be read, puts nobody back."""
    removed = payload.get(CANCELLED_NAMES)
    if not isinstance(removed, dict) or not removed:
        return set()
    try:
        when = parse_iso(str(email_date)) if email_date else None
    except ValueError:
        when = None
    kept: list[str] = []
    for name, at in removed.items():
        try:
            older = when is not None and parse_iso(str(at)) < when
        except ValueError:
            older = False
        if not older:
            kept.append(name)
    return people(kept, book)


def _swap_codes(held: str | None, shown: str | None, typed: str | None) -> str | None:
    """The confirmation codes a booking holds (`held`, joined by ', ') with the code or codes
    the page showed (`shown`) replaced by the one the owner typed (`typed`, None to clear)."""
    gone = {c for c in (shown or "").split(", ") if c}
    kept = [c for c in (held or "").split(", ") if c and c not in gone]
    return _join_codes(", ".join(kept) or None, typed)


def _alias_for(kind: str, p: dict) -> str | None:
    """The key an edited stay or car would be filed under, or None when it has no real key."""
    return real_key(kind, p, airports=Airports())  # a stay's or a car's key names no airport


def _day_of(payload: Mapping, field: str) -> dt.date | None:
    """The local day of the time `payload` holds for `field`, when it reads as one."""
    value = payload.get(field)
    when = parse_local(value) if isinstance(value, str) else None
    return when.date() if when is not None else None


def _flight_day(payload: Mapping) -> dt.date | None:
    """The local day a flight departs (`date_local`, else its departure's day), when one reads."""
    value = payload.get("date_local")
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            pass
    return _day_of(payload, "dep_local")


def _names_agree(one: object, other: object) -> bool | None:
    """Whether two names of a stay's property or a car's company agree: compared by their words,
    whatever their case, they do when one's words are all among the other's ("Example Suites"
    and "EXAMPLE SUITES DOWNTOWN"), and two different properties do not. None when either says
    no name."""
    words = [
        frozenset(re.findall(r"\w+", n.casefold())) if isinstance(n, str) else frozenset()
        for n in (one, other)
    ]
    if not (words[0] and words[1]):
        return None
    return words[0] <= words[1] or words[1] <= words[0]


def _alt_keys(payload: dict) -> list[str]:
    """The keys the booking also answers to (`payload["alt_keys"]`), those that are text."""
    earlier = payload.get("alt_keys")
    return [k for k in earlier if isinstance(k, str)] if isinstance(earlier, list) else []


def _code_alias(kind: str, key: str) -> bool:
    """Whether `key` is the key a stay's or a car's confirmation code makes (`lodging|CODE`)."""
    return key.startswith(f"{kind}|") and key.count("|") == 1


def _span(kind: str, payload: dict) -> tuple[str, str | None, str | None, str | None] | None:
    """`grouping.item_span` of the booking, or None when it has no start."""
    try:
        return grouping.item_span(SimpleNamespace(kind=kind, payload=payload))
    except ValueError:
        return None


def stamp(when: dt.datetime) -> str:
    """`when` as the sync columns hold a time: UTC, ISO-8601 to the second, so that two such
    times compare as text in a query. ValueError for a time with no zone."""
    if when.utcoffset() is None:
        raise ValueError("a sync time needs a zone")
    return when.astimezone(dt.UTC).isoformat(timespec="seconds")


def _join_codes(existing: str | None, new: str | None) -> str | None:
    """Two separately booked PNRs on one flight (one traveller under AAA, another under BBB) are
    both worth texting: keep every distinct code, oldest first."""
    codes = [c for c in (existing or "").split(", ") if c]
    if new and new not in codes:
        codes.append(new)
    return ", ".join(codes) or None


@dataclass
class Trip:
    id: int
    title: str
    place: str | None
    start_date: str
    end_date: str
    notes: str | None
    cancelled: bool
    created_at: str
    updated_at: str
    deleted_at: str | None = None


@dataclass
class TripItem:
    id: int
    trip_id: int | None  # None exactly when start_at is: a booking with no date has no trip
    item_key: str
    kind: str
    status: str
    start_at: str | None
    end_at: str | None
    start_tz: str | None
    end_tz: str | None
    confirmation: str | None
    payload: dict
    passengers: list[str]
    airtrail_flight_id: int | None
    source_message_id: int | None  # the last message that filed or changed it
    updated_at: str = ""
    removed_at: str | None = None
    checks: tuple[str, ...] = ()
    sync_state: str = "off"
    sync_hold: str | None = None
    told: dict | None = None
    sync_error: str | None = None
    sync_error_at: str | None = None
    sync_attempts: int = 0
    sync_after: str | None = None  # not tried again before this time
    sync_claimed_at: str | None = None  # a sync pass holds the flight since then
    sync_logged: str | None = None  # a dry run logged its request for what this digest says

    @property
    def page(self) -> str:
        """Where the page shows the booking, under its base: its trip's page at the booking, or
        the list for a booking with no trip."""
        return f"/trips/{self.trip_id}#item-{self.id}" if self.trip_id is not None else "/"


class EditRefused(ValueError):
    """A save the page refuses, with the sentence it shows and the field it is about (`field`,
    one of EDITABLE; None when it is about the booking as a whole)."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class KeyCollision(RuntimeError):
    """The key a booking's fields make is another booking's: settle changed nothing."""

    def __init__(self, item_id: int, other_id: int) -> None:
        super().__init__(f"trip item {item_id} has the key of trip item {other_id}")
        self.item_id = item_id
        self.other_id = other_id


class TripStore:
    """`book` names the travellers (the owner on a leg decides its sync); `airports` is the table
    a booking is validated against, the bundled one when left out."""

    def __init__(
        self,
        ledger: Ledger,
        home_airports: tuple[str, ...],
        *,
        book: PassengerBook | None = None,
        airports: Airports | None = None,
    ) -> None:
        self.led = ledger
        self.home = tuple(a.upper() for a in home_airports)
        self.book = book if book is not None else PassengerBook.empty()
        self.airports = airports if airports is not None else Airports()
        self.covers = CoverStore(ledger)

    def airtrail_on(self) -> bool:
        """Whether a flight log is kept: the ledger's `kv` row `airtrail` says `on`."""
        return self.led.kv.get(AIRTRAIL_KV) == "on"

    # ------------------------------------------------------------------ loading
    def _trip(self, row: sqlite3.Row | None) -> Trip | None:
        if row is None:
            return None
        total, cancelled = self.led.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(status='cancelled'),0) FROM trip_items"
            " WHERE trip_id=? AND removed_at IS NULL",
            (row["id"],),
        ).fetchone()
        return Trip(
            id=row["id"],
            title=row["title"],
            place=row["place"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            notes=row["notes"],
            cancelled=bool(total) and total == cancelled,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    @staticmethod
    def _item(row: sqlite3.Row | None) -> TripItem | None:
        if row is None:
            return None
        return TripItem(
            id=row["id"],
            trip_id=row["trip_id"],
            item_key=row["item_key"],
            kind=row["kind"],
            status=row["status"],
            start_at=row["start_at"],
            end_at=row["end_at"],
            start_tz=row["start_tz"],
            end_tz=row["end_tz"],
            confirmation=row["confirmation"],
            payload=Ledger.trip_items_payload(row),
            passengers=Ledger.trip_items_passengers(row),
            airtrail_flight_id=row["airtrail_flight_id"],
            source_message_id=row["source_message_id"],
            updated_at=row["updated_at"],
            removed_at=row["removed_at"],
            checks=tuple(json.loads(row["checks_json"])),
            sync_state=row["sync_state"],
            sync_hold=row["sync_hold"],
            told=None if row["told_json"] is None else json.loads(row["told_json"]),
            sync_error=row["sync_error"],
            sync_error_at=row["sync_error_at"],
            sync_attempts=row["sync_attempts"],
            sync_after=row["sync_after"],
            sync_claimed_at=row["sync_claimed_at"],
            sync_logged=row["sync_logged"],
        )

    def get(self, trip_id: int) -> Trip | None:
        """A trip that is not deleted (it may still be empty: `find_or_create` and `move_item`
        read a just-inserted trip back through here)."""
        row = self.led.trips.get(trip_id)
        if row is None or row["deleted_at"]:
            return None
        return self._trip(row)

    def _any_trip(self, trip_id: int) -> Trip | None:
        """Deleted or not — for the Recently deleted page's 'from <trip>' label."""
        return self._trip(self.led.trips.get(trip_id))

    def items(self, trip_id: int) -> list[TripItem]:
        rows = [self._item(r) for r in self.led.trip_items.by_trip(trip_id)]
        return sorted(rows, key=grouping.sort_key)  # type: ignore[arg-type]

    def item(self, item_id: int) -> TripItem | None:
        """Any row by id, removed or not: callers look at `removed_at`."""
        return self._item(self.led.trip_items.get(item_id))

    def visible(self, item_id: int) -> TripItem | None:
        """The booking by id if the owner can see it: not removed, and not in a deleted trip."""
        row = self.led.trip_items.get(item_id)
        return None if row is None or self._hidden(row) else self._item(row)

    def _find_by_key(self, key: str) -> sqlite3.Row | None:
        """The row for a booking key, by its key or by a corrected-code alias; hidden or not;
        else a booking filed by tripbox 1.0.0 under its day as printed whose fields now make
        this key (`_keyed_by_a_printed_day`)."""
        return (
            self.led.trip_items.by_key(key)
            or self.led.trip_items.by_alias(key)
            or self._keyed_by_a_printed_day(key)
        )

    def _keyed_by_a_printed_day(self, key: str) -> sqlite3.Row | None:
        """tripbox 1.0.0 keyed a day by the first ten characters of its value as printed
        (`lodging|<name>|Mar 13, 20`); its stays and cars keep that key, and a flight or a train
        keeps it until it is next settled. Such a booking is found by the key its fields make
        now, so a later email for it is not filed again."""
        if is_draft(key) or key.count("|") < 2:
            return None
        kind = key.split("|", 1)[0]
        kind = kind if kind in ("lodging", "car", "train") else "flight"
        for row in self.led.trip_items.keyed_by_a_printed_day(kind):
            payload = json.loads(row["payload_json"])
            if real_key(kind, payload, airports=self.airports) == key:
                return row
        return None

    def _hidden(self, row: sqlite3.Row) -> bool:
        """Removed, or in a deleted trip. A booking with no trip (no date yet) is shown."""
        if row["removed_at"] is not None:
            return True
        return row["trip_id"] is not None and self.get(row["trip_id"]) is None

    def item_by_key(self, key: str) -> TripItem | None:
        """The booking's own trip item, if the owner can see it (not removed, trip not
        deleted)."""
        row = self._find_by_key(key)
        if row is None or self._hidden(row):
            return None
        return self._item(row)

    def find(self, key: str) -> TripItem | None:
        """The booking filed under `key` or holding it as an alias, hidden or not."""
        return self._item(self._find_by_key(key))

    def by_code(self, kind: str, code: str, *, hidden: bool = False) -> list[TripItem]:
        """The bookings of `kind` that hold the confirmation code `code`, oldest first: those the
        owner can see (not removed, not in a deleted trip) or, with `hidden`, those put away."""
        if not code:
            return []
        rows = self.led.trip_items.by_code(kind, code)
        return [self._item(r) for r in rows if self._hidden(r) == hidden]  # type: ignore[misc]

    def unsorted(self) -> list[TripItem]:
        """Bookings with no trip (no date yet) that are not removed, oldest first."""
        return [self._item(r) for r in self.led.trip_items.unsorted()]  # type: ignore[misc]

    def sent_flights(self) -> list[TripItem]:
        """The flights a flight log holds a record of (`airtrail_flight_id`), not removed and not
        in a deleted trip, oldest first, cancelled ones included: the ones enrichment asks about
        and the backfill checks are still there."""
        return [self._item(r) for r in self.led.trip_items.sent_flights()]  # type: ignore[misc]

    def sync_counts(self) -> dict[str, int]:
        """How the flights the owner can see stand with the flight log: `synced`, `pending`,
        `blocked`, `past` and `deleted` (held out of the reconcile as having left before it was
        kept, or as gone from it), and `off` (any other flight it is not kept for: one the owner
        is not on, or a cancelled one never sent). With no flight log kept every flight is
        `off`."""
        counts = dict.fromkeys(("synced", "pending", "blocked", "past", "deleted", "off"), 0)
        for row in self.led.trip_items.flights_shown():
            hold = row["sync_hold"]
            word = hold if hold in ("past", "deleted") else row["sync_state"]
            counts[word if word in counts else "off"] += 1
        return counts

    def still_listed(self) -> int:
        """How many flights the owner can see carry the open mark that the flight log still
        lists them as the owner's (`checks.STILL_LISTS`); one the owner has looked at is not."""
        return sum(
            checks.STILL_LISTS in json.loads(row["checks_json"] or "[]")
            for row in self.led.trip_items.flights_shown()
        )

    def flagged(self, trip_id: int | None = None) -> list[TripItem]:
        """Bookings the owner can see with a check open on them: one trip's, or all of them
        (those in trips first, by start, then the unsorted ones)."""
        return [self._item(r) for r in self.led.trip_items.flagged(trip_id)]  # type: ignore[misc]

    def flag_counts(self) -> dict[int | None, int]:
        """Trip id (None for the unsorted bookings) → how many of its visible bookings have a
        check open."""
        return self.led.trip_items.flag_counts()

    def all_trips(self) -> list[Trip]:
        return [self._trip(r) for r in self.led.trips.all()]  # type: ignore[misc]

    def deleted_trips(self) -> list[Trip]:
        return [self._trip(r) for r in self.led.trips.deleted()]  # type: ignore[misc]

    def removed_items(self) -> list[tuple[TripItem, Trip | None]]:
        """Removed bookings, newest first, each with the trip it came from (deleted or not)."""
        return [
            (self._item(r), self._any_trip(r["trip_id"]))  # type: ignore[misc]
            for r in self.led.trip_items.removed()
        ]

    def deleted_counts(self) -> tuple[int, int]:
        """(deleted trips, removed bookings) — the list page's footer link, two COUNTs."""
        c = self.led.conn
        trips = c.execute("SELECT COUNT(*) FROM trips WHERE deleted_at IS NOT NULL").fetchone()[0]
        items = c.execute(
            "SELECT COUNT(*) FROM trip_items WHERE removed_at IS NOT NULL"
        ).fetchone()[0]
        return int(trips), int(items)

    def trip_for_pnr(self, pnr: str) -> int | None:
        for r in self.led.trip_items.by_pnr(pnr):
            if self.get(r["trip_id"]) is not None:
                return r["trip_id"]
        return None

    def counts(self, trip_id: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.led.trip_items.by_trip(trip_id):
            if r["status"] != "cancelled":
                out[r["kind"]] = out.get(r["kind"], 0) + 1
        return out

    def upcoming(self, today: dt.date) -> list[Trip]:
        t = today.isoformat()
        trips = [x for x in self.all_trips() if x.end_date >= t and not x.cancelled]
        return sorted(trips, key=lambda x: (0 if x.start_date <= t else 1, x.start_date, x.id))

    def past(self, today: dt.date, limit: int | None = 10) -> list[Trip]:
        t = today.isoformat()
        trips = [x for x in self.all_trips() if x.end_date < t or x.cancelled]
        trips.sort(key=lambda x: (x.start_date, x.id), reverse=True)
        return trips if limit is None else trips[:limit]

    # ------------------------------------------------------------------ grouping
    def _trip_dicts(self) -> list[dict]:
        out = []
        for r in self.led.trips.all():
            rows = self.led.trip_items.by_trip(r["id"])
            out.append(
                {
                    "id": r["id"],
                    "title": r["place"] or r["title"],
                    "start_date": r["start_date"],
                    "end_date": r["end_date"],
                    # a trip whose every item is cancelled is over; new bookings start afresh
                    "cancelled": bool(rows) and all(x["status"] == "cancelled" for x in rows),
                }
            )
        return out

    def find_or_create(
        self,
        item: grouping.HasKindAndPayload,
        *,
        span: tuple[dt.date, dt.date] | None = None,
        preferred_id: int | None = None,
    ) -> Trip:
        dates = grouping.item_dates(item)
        if dates is None:
            raise ValueError(f"{item.kind} item has no dates")
        d0, d1 = dates
        if span:
            d0, d1 = min(d0, span[0]), max(d1, span[1])
        place = grouping.trip_place(grouping.trip_title(item, self.home))
        if place.lower() == "trip":
            place = ""
        trips = self._trip_dicts()
        chosen = next((t for t in trips if t["id"] == preferred_id), None) if preferred_id else None
        if chosen is None:
            chosen = grouping.choose_trip(
                trips, d0, d1, place=place
            ) or grouping.nearby_trip_same_place(trips, place, d0, d1)
        if chosen is not None:
            s, e = chosen["start_date"], chosen["end_date"]
            new_s, new_e = min(s, d0.isoformat()), max(e, d1.isoformat())
            if (new_s, new_e) != (s, e):
                self.led.trips.update(chosen["id"], start_date=new_s, end_date=new_e)
            return self.get(chosen["id"])  # type: ignore[return-value]
        tid = self.led.trips.insert(
            title=place or "Trip",
            place=place or None,
            start_date=d0.isoformat(),
            end_date=d1.isoformat(),
        )
        return self.get(tid)  # type: ignore[return-value]

    def same_place_trip(self, place: str, d0: dt.date, d1: dt.date) -> Trip | None:
        """A live trip titled for `place` that covers [d0, d1] (with the usual slack) or sits
        within NEARBY_DAYS of it — the only trips a chain of bookings the caller has already
        grouped (the AirTrail backfill) may join; never a trip for somewhere else."""
        want = grouping.place_key(place or "")
        if not want or want == "trip":
            return None
        trips = [t for t in self._trip_dicts() if grouping.place_key(str(t["title"])) == want]
        # every candidate is the same place already; choose_trip's place rank is moot
        chosen = grouping.choose_trip(trips, d0, d1) or next(
            (
                t
                for t in sorted(
                    trips,
                    key=lambda t: max(
                        (dt.date.fromisoformat(t["start_date"]) - d1).days,
                        (d0 - dt.date.fromisoformat(t["end_date"])).days,
                        0,
                    ),
                )
                if not t.get("cancelled")
                and max(
                    (dt.date.fromisoformat(t["start_date"]) - d1).days,
                    (d0 - dt.date.fromisoformat(t["end_date"])).days,
                    0,
                )
                <= grouping.NEARBY_DAYS
            ),
            None,
        )
        return self.get(chosen["id"]) if chosen else None

    def covering_trip(self, d0: dt.date, d1: dt.date) -> Trip | None:
        """The smallest live trip whose dates (with slack) cover [d0, d1], whatever its place."""
        chosen = grouping.choose_trip(self._trip_dicts(), d0, d1)
        return self.get(chosen["id"]) if chosen else None

    def new_trip(self, place: str | None, d0: dt.date, d1: dt.date) -> Trip:
        """A fresh trip for bookings the caller has already grouped; the store stays the only
        writer of trips. Dates are recomputed from the rows as they are filed."""
        clean = (place or "").strip()
        tid = self.led.trips.insert(
            title=clean or "Trip",
            place=clean or None,
            start_date=d0.isoformat(),
            end_date=d1.isoformat(),
        )
        return self.get(tid)  # type: ignore[return-value]

    def relink_flight(
        self, old_key: str, new_key: str, flight_id: int, fields: dict
    ) -> TripItem | None:
        """The AirTrail flight behind a trip item was replaced (deleted and imported again):
        point the row at the new flight, under the key the planner would now
        compute, carrying the schedule fields the new flight has, and record the flight log as
        told what it should hold of the booking (`checks.desired`): its record is the booking's
        now, so the last attempt's error and its place in the queue are over too. The key it
        leaves is not kept: the flight that had it is gone from the flight log. Refuses to touch
        a row when another row answers to the new key, as its key or an alias — the backfill
        must relink before it files, never after."""
        with self._tx():
            row = self._find_by_key(old_key)
            if row is None:
                return None
            if new_key != row["item_key"]:
                clash = self._find_by_key(new_key)
                if clash is not None and clash["id"] != row["id"]:
                    raise RuntimeError(f"{new_key} already has a trip item: relink before filing")
            payload = {
                **Ledger.trip_items_payload(row),
                **fields,
                "airtrail_flight_id": flight_id,
                "source": FLIGHT_LOG_SOURCE,  # its schedule is the flight log's now
            }
            drop_printed_zones(payload, *PRINTED_ZONES)  # an email's, for times it no longer has
            passengers = Ledger.trip_items_passengers(row)
            self.led.trip_items.update(
                row["id"],
                item_key=new_key,
                airtrail_flight_id=flight_id,
                payload=payload,
                told=checks.desired(payload, passengers, row["status"], self.book),
                sync_error=None,
                sync_error_at=None,
                sync_attempts=0,
                sync_after=None,
            )
            return self.settle(row["id"])

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _reapply_owner_edits(old: dict, new: dict, merged: dict) -> set[str]:
        """A field the owner edited on the page keeps their value unless the incoming email is
        newer than the edit AND prints that field (then the email wins and the edit record goes;
        `held_edits`). The travellers are the exception: an email's still join them, and only
        those the owner took off (TAKEN_OFF) stay off, which the caller sees to. Returns the
        fields kept. `payload["owner_edits"]` maps field → ISO time of the edit."""
        edits = dict(old.get("owner_edits") or {})
        if not edits:
            merged.pop("owner_edits", None)
            return set()
        kept = held_edits(old, new)
        for field in list(edits):
            if field not in kept:
                edits.pop(field)
            elif field != "passengers":
                merged[field] = old.get(field)
        if edits:
            merged["owner_edits"] = edits
        else:
            merged.pop("owner_edits", None)
        return kept

    def upsert_item(
        self,
        trip: Trip | None,
        item,
        *,
        airtrail_flight_id: int | None = None,
        source_message_id: int | None = None,
        status: str = "confirmed",
    ) -> TripItem:
        """File `item` (kind, key, payload, passengers, pnr, status) on the row its key or an
        alias finds, merged into what that row holds, or on a new row. A new row, or one coming
        back from Recently deleted (for an email sent after the owner put it away), goes into
        `trip`; with no trip given, or no date, settle places it (a booking with no date is in
        no trip). `source_message_id` is the message that files or changes it. Ends in
        settle."""
        status = "cancelled" if getattr(item, "status", None) == "cancelled" else status
        names = [n for n in (getattr(item, "passengers", None) or []) if n and n.strip()]
        conf = getattr(item, "pnr", None) or item.payload.get("confirmation_code")
        with self._tx():
            existing = self._find_by_key(item.key)
            if (
                existing is not None
                and existing["status"] == "cancelled"
                and status != "cancelled"
            ):
                # a receipt older than the cancellation (an original forwarded late) does not
                # revive the booking; a genuinely newer email (re-booked) does.
                # Compared on the emails' own dates: both may well be handled in the same poll.
                since = (
                    Ledger.trip_items_payload(existing).get("cancelled_email_date")
                    or existing["updated_at"]
                )
                email_date = item.payload.get("email_date")
                try:
                    newer = bool(email_date) and parse_iso(str(email_date)) > parse_iso(str(since))
                except ValueError:
                    newer = False
                if not newer:
                    status = "cancelled"
            iid = self._write_item(
                trip, item, existing, status, conf, names, airtrail_flight_id, source_message_id
            )
            return self.settle(iid)

    def _write_item(
        self,
        trip: Trip | None,
        item,
        existing: sqlite3.Row | None,
        status: str,
        conf: str | None,
        names: list[str],
        airtrail_flight_id: int | None,
        source_message_id: int | None,
    ) -> int:
        """upsert_item's write half, inside its transaction. A row is put in a trip only with
        its start (the ledger holds a trip exactly when it holds a start); settle derives the
        rest."""
        if existing is None:
            span = _span(item.kind, item.payload)
            placed = trip is not None and span is not None
            start_at, end_at, start_tz, end_tz = span or (None, None, None, None)
            payload = dict(item.payload)
            if status == "cancelled":
                # filed cancelled (a cancellation for a booking never filed): as of its email,
                # so that only a newer email revives it
                payload["cancelled_email_date"] = item.payload.get("email_date") or now_iso()
            iid = self.led.trip_items.insert(
                trip_id=trip.id if placed else None,  # type: ignore[union-attr]
                item_key=item.key,
                kind=item.kind,
                status=status,
                start_at=start_at if placed else None,
                end_at=end_at if placed else None,
                start_tz=start_tz,
                end_tz=end_tz,
                confirmation=conf,
                payload=payload,
                passengers=names,
                airtrail_flight_id=airtrail_flight_id,
                source_message_id=source_message_id,
            )
            if trip is not None:  # redated; one the caller made for an undated booking goes
                self.recompute_range(trip.id)
            return iid
        iid, trip_id = existing["id"], existing["trip_id"]
        old_names = Ledger.trip_items_passengers(existing)
        old_payload = Ledger.trip_items_payload(existing)
        # a day alone keeps a held time, and a value that fails its hard check keeps what the
        # booking holds
        said = without_days_alone(
            old_payload, without_unreadable(item.kind, item.payload, self.airports)
        )
        merged = merge_payload(old_payload, said)
        if item.kind == "flight":
            carry_printed_zones(merged, old_payload, said)
        if (
            merged.get("source") == FLIGHT_LOG_SOURCE
            and item.payload.get("source") != FLIGHT_LOG_SOURCE
        ):
            # a booking read from mail: its fields are no longer the flight log's alone, and
            # settle derives them again from now on
            merged.pop("source")
        if status == "cancelled":
            merged["cancelled_email_date"] = item.payload.get("email_date") or now_iso()
        else:
            merged.pop("cancelled_email_date", None)
        kept = self._reapply_owner_edits(old_payload, said, merged)
        # nobody the owner took off comes back with an email, whatever its date, nor anyone an
        # email newer than this one took off
        off = self._taken_off(old_payload) | self._removed_since(
            old_payload, old_names, item.payload.get("email_date")
        )
        if off:
            merged["passengers"] = self._not_taken_off(merged.get("passengers"), off)
        if merged.get("source") == HAND and merged.get("confirmation_code") not in BLANK:
            # a stay or a car typed in with no code keeps its key and answers to the code an
            # email filled in, so the next email under that code finds it
            self._alias_the_key_it_makes(item.kind, existing["item_key"], merged)
        fields: dict = {
            "status": status,
            # the codes the owner set stay as set, as the payload's do
            "confirmation": (
                existing["confirmation"]
                if kept & {"confirmation_code", "pnr"}
                else _join_codes(existing["confirmation"], conf)
            ),
            "payload": merged,
            "passengers": list(
                dict.fromkeys([*old_names, *self._not_taken_off(names, off)])
            ),
        }
        if airtrail_flight_id is not None:
            fields["airtrail_flight_id"] = airtrail_flight_id
        if source_message_id is not None:
            fields["source_message_id"] = source_message_id
        target = trip_id
        if self._hidden(existing) and status != "cancelled":
            # an email sent after the owner put the booking away (a change, a rebooking) files it
            # again: the same row comes back, in the trip the caller chose — or, with none, out of
            # a deleted trip for settle to place. One sent before (a late copy) is merged in place
            # and the booking stays put away, as a cancellation for a hidden booking does; the
            # empty trip the caller may have made for it is recomputed away below.
            if self._sent_after_put_away(existing, item.payload.get("email_date")):
                fields["removed_at"] = None
                if trip is not None:
                    target = trip.id
                elif trip_id is not None and self.get(trip_id) is None:
                    target = None
        elif trip_id is None and trip is not None:
            target = trip.id  # a booking with no trip yet goes where the caller files it
        if target != trip_id:
            # the span comes from the MERGED payload: a partial receipt (no arrival time) must
            # not erase what an earlier email established
            span = _span(item.kind, merged)
            if target is None:
                fields.update(trip_id=None, start_at=None, end_at=None)
            elif span is not None:
                fields.update(trip_id=target, start_at=span[0], end_at=span[1])
            else:
                target = trip_id  # no start to place it with: settle takes it out of any trip
        self.led.trip_items.update(iid, **fields)
        for tid in dict.fromkeys((trip_id, trip.id if trip is not None else None)):
            self.recompute_range(tid)
        return iid

    def _sent_after_put_away(self, row: sqlite3.Row, email_date: object) -> bool:
        """Whether an email dated `email_date` (its own Date) was sent after the owner put the
        booking `row` away: after its removal (`removed_at`) and after its trip's deletion
        (`deleted_at`), whichever it has. Compared as the cancelled-revival rule compares, on
        the email's date; a missing or unreadable date is not after."""
        put_away = [row["removed_at"]]
        if row["trip_id"] is not None:
            trip = self.led.trips.get(row["trip_id"])
            put_away.append(trip["deleted_at"] if trip is not None else None)
        try:
            sent = parse_iso(str(email_date)) if email_date else None
            return sent is not None and all(sent > parse_iso(str(at)) for at in put_away if at)
        except ValueError:
            return False

    def _taken_off(self, payload: Mapping) -> set[tuple[str, str]]:
        """The people the owner took off the booking on the page (TAKEN_OFF)."""
        names = payload.get(TAKEN_OFF)
        return people(names, self.book) if isinstance(names, list) else set()

    def _removed_since(
        self, payload: Mapping, names: list[str], email_date: object
    ) -> set[tuple[str, str]]:
        """The people an email took off the booking that an email dated `email_date` does not
        put back (`removed_by_email`), but for anyone the booking holds again (its travellers
        `names` and the names its payload prints)."""
        gone = removed_by_email(payload, email_date, self.book)
        if not gone:
            return gone
        return gone - people([*names, *(payload.get("passengers") or [])], self.book)

    def _not_taken_off(self, names, off: set[tuple[str, str]]) -> list[str]:
        """`names` without the people of `off` (`passengers.person`)."""
        return [n for n in (names or []) if not (off and person(n, self.book) in off)]

    @staticmethod
    def _stamp(payload: dict, raised: dict | None) -> None:
        """Join what an email's soft findings stamped (code → time) into the booking's own
        stamps: each takes the later of the two times."""
        for code, at in (raised or {}).items():
            checks.raise_soft(payload, [code], at)

    def cancel(
        self,
        key: str,
        *,
        email_date: str | None = None,
        raised: dict | None = None,
        source_message_id: int | None = None,
    ) -> TripItem | None:
        """The booking is cancelled, as of `email_date` (now when none is given); nothing else of
        it changes. `raised` is what the cancelling email's soft findings stamped, which the
        booking then carries; `source_message_id` the cancelling message."""
        with self._tx():
            row = self._find_by_key(key)
            if row is None:
                return None
            payload = Ledger.trip_items_payload(row)
            payload["cancelled_email_date"] = email_date or now_iso()
            self._stamp(payload, raised)
            source = {} if source_message_id is None else {"source_message_id": source_message_id}
            self.led.trip_items.update(row["id"], status="cancelled", payload=payload, **source)
            return self.settle(row["id"])

    def mark(self, item_id: int, raised: dict) -> TripItem:
        """An email raised soft findings about the booking and changed nothing of it (a
        cancellation that does not say whose seat it cancels): `raised` (code → time) is joined
        into the booking's own stamps, each the later of the two times. Ends in settle."""
        with self._tx():
            payload = Ledger.trip_items_payload(self._row(item_id))
            self._stamp(payload, raised)
            self.led.trip_items.update(item_id, payload=payload)
            return self.settle(item_id)

    def remove_passengers(
        self,
        key: str,
        names: list[str],
        *,
        email_date: str | None = None,
        raised: dict | None = None,
        source_message_id: int | None = None,
    ) -> TripItem | None:
        """A partial cancellation: these travellers leave the booking, the booking stands. Each
        is recorded as taken off as of `email_date` (now when none is given) in
        payload[CANCELLED_NAMES], so no older email puts them back. `raised` and
        `source_message_id` are as for `cancel`."""
        with self._tx():
            row = self._find_by_key(key)
            if row is None:
                return None
            gone = {n.upper() for n in names}
            payload = Ledger.trip_items_payload(row)
            self._stamp(payload, raised)
            removed = payload.get(CANCELLED_NAMES)
            removed = dict(removed) if isinstance(removed, dict) else {}
            at = email_date or now_iso()
            for name in names:
                removed[name] = at
            if removed:
                payload[CANCELLED_NAMES] = removed
            payload["passengers"] = [
                n for n in (payload.get("passengers") or []) if n.upper() not in gone
            ]
            kept = [n for n in Ledger.trip_items_passengers(row) if n.upper() not in gone]
            source = {} if source_message_id is None else {"source_message_id": source_message_id}
            self.led.trip_items.update(row["id"], payload=payload, passengers=kept, **source)
            return self.settle(row["id"])

    def edit_item(
        self, item_id: int, fields: dict, *, at: str | None = None, seen: str | None = None
    ) -> TripItem:
        """The owner corrects a booking of any kind on the page (`fields`, each of EDITABLE for
        its kind; `passengers` a list of names). A field sent as the page shows it is left as
        it is; any other is cleaned as typed (`_clean`). A time that does not read as a date
        (one typed with no date too, whatever the booking holds), a flight number that is not
        one, and a stay's or a car's end before its start raise EditRefused, naming the field,
        with the sentence the form shows. A start may be left out: the booking then waits with
        no trip. Anything else wrong with what is typed is stored, and is a hard check on the
        booking.

        Each changed field is stamped at `at` (now when none is given) in
        payload["owner_edits"], under the name the payload keeps it as (STORED_AS), so a later
        email overrides it only when it is newer than the edit (_reapply_owner_edits). Whoever a
        travellers edit takes off is recorded (TAKEN_OFF) and no email puts them back. A stay's
        or a car's corrected code becomes an alias key, never a new key: emails printing either
        code land on this row. Every save is a look (`checks.looked`), changed or not, at
        `seen`, the time the owner saw the booking as the form showed it (now when none is
        given); a `seen` of '' (a form whose time could not be read) stamps no look. A flight
        whose last sync attempt found an airport the flight log does not know, given another
        origin or destination, forgets that attempt and its backoff: the next pass asks again.

        Ends in settle, which gives a flight or a train the key its fields now make (a draft of
        its own when they make none). When another booking holds that key, a draft is merged
        into that booking, which is returned, unless the merge is one the page does not make
        (`_refuse_a_merge_the_page_cannot_make`); any other booking raises EditRefused. A
        refusal stores nothing."""
        with self._tx():
            return self._edit_item(item_id, fields, at, seen)

    def _edit_item(
        self, item_id: int, fields: dict, at: str | None, seen: str | None
    ) -> TripItem:
        """edit_item inside its transaction: the row is read under the write lock."""
        row = self.led.trip_items.get(item_id)
        if row is None:
            raise KeyError(item_id)
        ti = self._item(row)
        assert ti is not None
        kind = ti.kind
        if self._hidden(row):
            raise ValueError("this booking is in Recently deleted")
        # a time sent with no date (or no readable one) is refused, whatever the booking holds
        _refuse_what_the_page_cannot_take(kind, fields)
        carrier = _clean_text(fields.get("carrier", ti.payload.get("carrier")), "carrier")
        changed: dict = {}
        for k, typed in fields.items():
            held = editable_value(ti, k)
            if _as_shown(kind, k, typed, held):
                continue
            value = _clean(kind, k, typed, carrier)
            if value != held:
                changed[k] = value
        payload = dict(ti.payload)
        _owner_typed(kind, payload, changed, at or now_iso())
        update: dict = {}
        if "passengers" in changed:
            update["passengers"] = changed["passengers"]
            self._record_taken_off(payload, ti.passengers, changed["passengers"])
        if "confirmation_code" in changed:
            shown = editable_value(ti, "confirmation_code")
            update["confirmation"] = _swap_codes(
                ti.confirmation, shown, changed["confirmation_code"]
            )
            self._alias_the_key_it_makes(kind, ti.item_key, payload)
        self._refuse_a_merge_the_page_cannot_make(ti, payload)
        look = now_iso() if seen is None else seen
        if look:
            checks.looked(payload, look)
        if row["sync_error"] == "airport" and changed.keys() & {"origin", "destination"}:
            # the flight log did not know an airport, and another is typed in: the next sync
            # pass asks it again, with no backoff left to wait out
            update.update(sync_error=None, sync_error_at=None, sync_attempts=0, sync_after=None)
        self.led.trip_items.update(item_id, payload=payload, **update)
        try:
            return self.settle(item_id)
        except KeyCollision as clash:
            # the refusal rolls back the transaction the edit was written in
            taken = KEYED_BY.get(kind, "confirmation code")
            raise EditRefused(f"another booking already has this {taken}") from clash

    def _alias_the_key_it_makes(self, kind: str, key: str, payload: dict) -> None:
        """A stay or a car under `key` answers to the key its fields in `payload` now make (its
        code's, when it holds one) as an alias, never as its key, so emails printing either land
        on this row: the latest such alias replaces a superseded code's, the booking's other
        keys stay. Nothing for a flight or a train (settle rekeys those), nor when another
        booking already answers to that key."""
        alias = None if kind in REKEYED else _alias_for(kind, payload)
        if alias and alias != key and self._find_by_key(alias) is None:
            earlier = payload.get("alt_keys") or []
            payload["alt_keys"] = [*(k for k in earlier if not _code_alias(kind, k)), alias]

    def _record_taken_off(self, payload: dict, before: list[str], after: list[str]) -> None:
        """The owner's travellers edit records in `payload[TAKEN_OFF]` who it took off (the
        people of `before` not in `after`), joined to those taken off earlier; anyone `after`
        names again is no longer among them."""
        now = people(after, self.book)
        earlier = payload.get(TAKEN_OFF)
        names = [*(earlier if isinstance(earlier, list) else []), *before]
        seen: set[tuple[str, str]] = set()
        off: list[str] = []
        for name in names:
            who = person(name, self.book) if isinstance(name, str) else None
            if who is not None and who not in now and who not in seen:
                seen.add(who)
                off.append(name)
        payload.pop(TAKEN_OFF, None)
        if off:
            payload[TAKEN_OFF] = off

    def _refuse_a_merge_the_page_cannot_make(self, ti: TripItem, payload: dict) -> None:
        """A draft whose edited fields make the key another booking holds is merged into it by
        settle. On the page that is refused (EditRefused) when the booking is one the owner put
        away, into which the draft would vanish behind a save, and when the draft holds a record
        in the flight log, which a merge would leave behind on a day or number it no longer
        has."""
        if not is_draft(ti.item_key):
            return
        key = real_key(ti.kind, payload, airports=self.airports)
        other = self._find_by_key(key) if key else None
        if other is None or other["id"] == ti.id:
            return
        taken = KEYED_BY.get(ti.kind, "confirmation code")
        if self._hidden(other):
            raise EditRefused(
                f"another booking already has this {taken}; it is in Recently deleted"
                " — restore it first"
            )
        if ti.airtrail_flight_id is not None:
            raise EditRefused(f"another booking already has this {taken}")

    def add_by_hand(
        self, kind: str, fields: dict, *, trip_id: int | None = None, at: str | None = None
    ) -> TripItem:
        """The owner types a booking in on the page: `fields` (each of EDITABLE for `kind`;
        `passengers` a list of names) are cleaned, limited and refused as the edit form's are
        (`edit_item`), and a form with no field filled but the travellers (the owner's name the
        form starts with) is refused (ValueError, EMPTY_FORM). The
        payload holds what was typed, under the names it keeps it as, with `source` HAND and each
        field stamped at `at` (now when none is given) in `owner_edits`: an email older than the
        entry never overwrites what was typed, a newer one wins what it prints.

        Its key is the real one its fields make, else a draft key of its own. A booking that
        already answers to that real key (as its key or an alias) refuses it, saying where that
        booking is (ALREADY_THERE, or IN_RECENTLY_DELETED when the owner put it away), and
        nothing is stored. With a date it goes into the trip `trip_id` when that is a trip that is
        not deleted, else where settle's grouping puts it; with none it waits in no trip. No
        message is written and no soft finding is stamped: its checks are what its fields lack.
        Ends in settle."""
        with self._tx():
            _refuse_what_the_page_cannot_take(kind, fields)
            carrier = _clean_text(fields.get("carrier"), "carrier")
            changed = {
                k: value
                for k, typed in fields.items()
                if (value := _clean(kind, k, typed, carrier)) not in BLANK
            }
            if not changed.keys() - {"passengers"}:  # travellers alone are no booking
                raise ValueError(EMPTY_FORM)
            payload: dict = {"source": HAND}
            _owner_typed(kind, payload, changed, at or now_iso())
            key = real_key(kind, payload, airports=self.airports)
            there = self._find_by_key(key) if key is not None else None
            if there is not None:
                raise EditRefused(IN_RECENTLY_DELETED if self._hidden(there) else ALREADY_THERE)
            # an undated booking is in no trip, whichever was chosen
            trip = self.get(trip_id) if trip_id is not None and _span(kind, payload) else None
            item = SimpleNamespace(
                kind=kind, key=key or draft_key(secrets.token_hex(8), 0), payload=payload
            )
            iid = self._write_item(
                trip,
                item,
                None,
                "confirmed",
                changed.get("confirmation_code"),
                changed.get("passengers", []),
                None,
                None,
            )
            return self.settle(iid)

    def set_status(self, item_id: int, status: str) -> TripItem:
        """The owner's say-so on the page. Cancelled works like a cancellation email dated now (a
        late copy of the original receipt cannot revive it; a genuinely newer email can);
        confirmed clears that. Any kind, flights included. Nothing is sent from here: the next
        sync pass tells the flight log of a flight it holds (a cancelled flight never sent stays
        unsent)."""
        ti = self.item(item_id)
        if ti is None:
            raise KeyError(item_id)
        if status == "cancelled":
            return self.cancel(ti.item_key)  # type: ignore[return-value]
        if status != "confirmed":
            raise ValueError(f"status must be cancelled or confirmed, not {status!r}")
        with self._tx():
            row = self.led.trip_items.get(item_id)
            payload = Ledger.trip_items_payload(row)
            payload.pop("cancelled_email_date", None)
            self.led.trip_items.update(item_id, status="confirmed", payload=payload)
            return self.settle(item_id)

    # ------------------------------------------------------------------ the owner's say
    def _row(self, item_id: int) -> sqlite3.Row:
        row = self.led.trip_items.get(item_id)
        if row is None:
            raise KeyError(item_id)
        return row

    def looks_right(self, item_id: int, *, at: str | None = None) -> TripItem:
        """The owner looked at the booking at `at` (now when none is given) and it is right:
        every soft check raised by then closes (`checks.looked`); one raised after `at` stays
        open, and a look earlier than the one recorded changes nothing. Nobody named and the
        hard checks stay open: an answer or a fixed field closes them. A later email reopens a
        soft check only by raising it with a later time, as it would after an edit. Ends in
        settle."""
        with self._tx():
            payload = Ledger.trip_items_payload(self._row(item_id))
            checks.looked(payload, at or now_iso())
            self.led.trip_items.update(item_id, payload=payload)
            return self.settle(item_id)

    def owner_answer(self, item_id: int, on_it: bool, *, at: str | None = None) -> TripItem:
        """The owner's answer to whether they were on a flight, given at `at` (now when none is
        given). Yes: the owner's name (`book.me_name`) joins the booking's travellers and the
        names it prints, so every reader of either finds them aboard, and an earlier no goes. No:
        `payload["owner_answer"]` is "no", which keeps the flight out of the flight log whoever
        it names. The answer is stamped in `payload["owner_edits"]` like an edit (`passengers`
        for a yes, `owner_answer` for a no), so an older email never undoes it. ValueError, and
        nothing stored, for a booking that is not a flight, and for a yes when the travellers
        file names no owner. Ends in settle."""
        with self._tx():
            row = self._row(item_id)
            if row["kind"] != "flight":
                raise ValueError(f"only a flight is asked who was on it, not a {row['kind']}")
            me = self.book.me_name
            if on_it and not me:
                raise ValueError(NO_OWNER)
            when = at or now_iso()
            payload = Ledger.trip_items_payload(row)
            names = Ledger.trip_items_passengers(row)
            edits = dict(payload.get("owner_edits") or {})
            if on_it:
                printed = list(payload.get("passengers") or [])
                for held in (printed, names):
                    if not self.book.classify(held).me_present:
                        held.append(me)
                payload["passengers"] = printed
                # the owner is back on: no longer one of those taken off it
                left = self._not_taken_off(payload.get(TAKEN_OFF), people([me], self.book))
                payload.pop(TAKEN_OFF, None)
                if left:
                    payload[TAKEN_OFF] = left
                payload.pop("owner_answer", None)
                edits.pop("owner_answer", None)
                edits["passengers"] = when
            else:
                payload["owner_answer"] = "no"
                edits["owner_answer"] = when
            payload["owner_edits"] = edits
            self.led.trip_items.update(item_id, payload=payload, passengers=names)
            return self.settle(item_id)

    def send_again(self, item_id: int) -> TripItem:
        """The owner asks for the flight to be sent to the flight log again. One held out of the
        reconcile because it was deleted there by hand (`sync_hold` `deleted`) comes back into
        it, and the next pass looks for it there before it creates it again; one waiting out a
        backoff is tried at the next pass (`sync_after` and `sync_attempts` start afresh). One
        that had left when the flight log was switched on (`past`) stays out. Ends in settle."""
        with self._tx():
            row = self._row(item_id)
            fields: dict = {"sync_after": None, "sync_attempts": 0}
            if row["sync_hold"] == "deleted":
                fields["sync_hold"] = None
            self.led.trip_items.update(item_id, **fields)
            return self.settle(item_id)

    def drafts_like(self, item) -> list[TripItem]:
        """The drafts `item` (kind, pnr, payload, passengers) may complete, oldest first: the
        bookings of its kind the owner can see, under a draft key, that hold its confirmation code
        and

        - say enough to be one leg (`_leg`): their local day, and their number or their
          departure time; and their departure time when they do not say where they leave from
          (on a through-flight one number flies two legs in one day, and a draft that says
          only its day and number could be either). A draft that says less is completed on the
          page, never by an email;
        - agree with it wherever both say something;
        - share a traveller with it when both name someone (`passengers.people`), so another
          traveller's receipt under a shared code never completes a draft of the owner's.

        A stay or a car that holds a code is keyed by it, so only a flight or a train is ever
        like a draft."""
        if item.kind not in REKEYED or not item.pnr:
            return []
        mine = self._leg(item.kind, item.payload)
        named = people(item.passengers, self.book)
        out = []
        for row in self.by_code(item.kind, item.pnr):
            if not is_draft(row.item_key):
                continue
            theirs = self._leg(row.kind, row.payload)
            if "day" not in theirs or not {"number", "time"} & set(theirs):
                continue
            if "origin" not in theirs and "time" not in theirs:
                continue
            if any(theirs[k] != v for k, v in mine.items() if k in theirs):
                continue
            held = people([*row.passengers, *(row.payload.get("passengers") or [])], self.book)
            if named and held and not named & held:
                continue
            out.append(row)
        return out

    def typed_in_like(self, item) -> list[TripItem]:
        """The bookings typed in by hand that a stay or a car read from an email (`item`: kind,
        payload) may be, oldest first: those of its kind the owner can see and has not cancelled,
        added by hand (HAND) and holding no confirmation code, under any key, whose start day
        is the item's, whose end day is the item's wherever both say one, and whose name (a
        stay's property, a car's company) agrees with the item's wherever both say one
        (`_names_agree`) — and of which something beyond the start day, the end day or the
        name, is said by both and agrees, so a booking that says only its start is no one's.
        The plan takes the one there is when no booking holds the item's key; none or several,
        and the email is filed on its own. A flight or a train is found by its key, by the move
        it made or by the draft it completes, never by its days."""
        if item.kind not in NAMED_BY:
            return []
        (start_f, _), (end_f, _) = SPAN_FIELDS[item.kind]
        name_f = NAMED_BY[item.kind]
        starts, ends = _day_of(item.payload, start_f), _day_of(item.payload, end_f)
        if starts is None:
            return []
        out = []
        for row in self.led.trip_items.without_code(item.kind, HAND):
            if self._hidden(row) or row["status"] == "cancelled":
                continue
            ti = self._item(row)
            assert ti is not None
            if ti.payload.get("confirmation_code") not in BLANK:
                continue
            if _day_of(ti.payload, start_f) != starts:
                continue
            agreed = False
            their_end = _day_of(ti.payload, end_f)
            if ends is not None and their_end is not None:
                if their_end != ends:
                    continue
                agreed = True
            names = _names_agree(item.payload.get(name_f), ti.payload.get(name_f))
            if names is False:
                continue
            if agreed or names:
                out.append(ti)
        return out

    def _leg(self, kind: str, p: dict) -> dict[str, object]:
        """What a flight's or a train's fields say of its leg, each value only when the validator
        takes it (one that fails its hard check says nothing): the local day and the departure
        time it leaves at; for a flight its carrier, its number and its origin and destination
        airports, known to the store's table; for a train its number and its stations."""
        out: dict[str, object] = {}
        leaves = p.get("dep_local")
        when = parse_local(leaves) if isinstance(leaves, str) else None
        if when is not None:
            out["day"] = when.date()
            if day_alone(leaves) is None:
                out["time"] = when.time()
        if kind == "flight":
            carrier = str(p.get("carrier") or "").upper()
            number = str(p.get("flight_number") or "")
            if CARRIER.match(carrier):
                out["carrier"] = carrier
            if FLIGHT_NO.match(number):
                out["number"] = int(number)
            for end in ("origin", "destination"):
                code = str(p.get(end) or "").upper()
                if IATA_AIRPORT.match(code) and self.airports.airport(code) is not None:
                    out[end] = code
            return out
        if number := str(p.get("train_number") or "").strip():
            out["number"] = number
        for end in ("origin", "destination"):
            if station := _norm(p.get(end) if isinstance(p.get(end), str) else None):
                out[end] = station
        return out

    def recompute_range(self, trip_id: int | None) -> Trip | None:
        """Dates from the live rows. No rows at all → the trip is deleted for good (merge/move
        emptied it) and None; removed rows only → None too, but the trip stays (hidden) for its
        bookings to be restored into. No trip (an unsorted booking's) → None."""
        if trip_id is None:
            return None
        rows = self.led.trip_items.by_trip(trip_id, include_removed=True)
        if not rows:
            self.led.trips.delete(trip_id)
            return None
        live = [r for r in rows if r["removed_at"] is None]
        if not live:
            return None
        start = min(r["start_at"][:10] for r in live)
        # a date-line flight lands "before" it leaves: the trip ends on its latest start or end
        end = max(max(r["start_at"], r["end_at"] or r["start_at"])[:10] for r in live)
        trip = self.led.trips.get(trip_id)
        if trip is not None and (trip["start_date"], trip["end_date"]) != (start, end):
            self.led.trips.update(trip_id, start_date=start, end_date=end)
        return self.get(trip_id)

    # ------------------------------------------------------------------ settle
    def settle(self, item_id: int) -> TripItem:
        """Make what is stored on the booking follow from its stored fields, in one transaction
        (joining one that is open):

        1. the booking is rebuilt from its stored fields and validated as a fresh parse is;
        2. a flight's derived fields (FLIGHT_DERIVED) become the rebuilt ones; nothing else of
           the payload changes (`owner_edits`, `alt_keys`, `raised`, `looked_at` stay);
        3. a flight or a train, or a booking under a draft key, takes the key its fields make,
           and keeps the key it had as an alias (the key it takes is no alias of it any more); a
           stay or a car with a real key keeps it (a corrected code is an alias, see edit_item).
           When another booking holds that key, as its key or an alias: a draft is merged into
           that booking (`_complete_draft`), and settle returns the booking; any other booking,
           and a draft holding another flight-log record than the booking's, raises
           KeyCollision, and nothing changed;
        4. the checks open on it are worked out (`checks.open_checks`); a flight the flight log
           still lists as the owner's while the owner is off it (`checks.still_listed`) is
           stamped in `raised` when that begins, and the stamp goes when it ends;
        5. with no start it is in no trip; with a start and no trip it goes where the grouping
           puts it, a trip holding one of its confirmation codes first; with a start and a trip
           it stays (a move made on the page is never undone); each trip touched is redated;
        6. its sync state is worked out (`checks.sync_state`);
        7. what differs is written in one UPDATE, so a second settle writes nothing.

        A flight's printed zone and offset (`rules.PRINTED_ZONES`) are handed back to the
        rebuild, so its instants are the ones first filed; an end whose time or airport the
        owner edited loses them first. A flight whose fields are still the flight log's own
        (FLIGHT_LOG_SOURCE) skips steps 1-3 and carries only the checks a flight log explains.
        A booking whose stored fields cannot be rebuilt (they validate to nothing, or hold a
        value of the wrong type) keeps its payload, key and checks as stored, is still placed and
        has its sync state worked out; the reason is logged."""
        with self._tx():
            row = self._row(item_id)
            try:
                fields, old_trip, new_trip = self._settled(row)
            except KeyCollision as clash:
                if not is_draft(row["item_key"]):
                    raise
                return self._complete_draft(row, clash.other_id)
            if fields:
                self.led.trip_items.update(item_id, **fields)
            for tid in dict.fromkeys((old_trip, new_trip)):
                self.recompute_range(tid)
            return self.item(item_id)  # type: ignore[return-value]

    def _settled(self, row: sqlite3.Row) -> tuple[dict, int | None, int | None]:
        """settle's reading of one row: the columns to write, the trip it was in and the trip it
        is in now. Only placement writes before the UPDATE (a trip found or made for it)."""
        kind, key, status = row["kind"], row["item_key"], row["status"]
        stored = Ledger.trip_items_payload(row)
        passengers = Ledger.trip_items_passengers(row)
        told = None if row["told_json"] is None else json.loads(row["told_json"])
        on = self.airtrail_on()
        owner = checks.owner(kind, passengers, stored, self.book)
        # in the reconcile: a flight log is kept, nothing holds the flight out of it, and it is
        # not a cancelled flight that was never sent
        in_reconcile = (
            on
            and kind == "flight"
            and row["sync_hold"] is None
            and not (status == "cancelled" and told is None)
        )
        payload = dict(stored)
        # since when the flight log lists as the owner's a flight the owner is off: stamped as
        # that begins, so a look made earlier leaves its check open, and dropped as it ends
        raised = payload.get("raised") or {}
        if checks.still_listed(owner, told) != (checks.STILL_LISTS in raised):
            raised = {k: v for k, v in raised.items() if k != checks.STILL_LISTS}
            if checks.still_listed(owner, told):
                raised[checks.STILL_LISTS] = now_iso()
            payload["raised"] = raised
        as_logged = self._as_the_flight_log_said(kind, stored)
        new_key = key
        try:
            if as_logged:
                # steps 1-3 are skipped: the flight log vouches for these fields, so the booking
                # carries none of the validator's findings, only the checks a flight log explains
                item = Item(
                    kind=kind,
                    key=key,
                    payload=stored,
                    pnr=row["confirmation"],
                    passengers=passengers,
                    classified=self.book.classify(passengers),
                    status=status,
                    confidence=1.0,
                )
            else:
                if kind == "flight":
                    # an end whose time or airport the owner set is read in the airport's zone
                    edited = set(payload.get("owner_edits") or {})
                    for end, (time_field, airport, _) in PRINTED_ZONES.items():
                        if edited & {time_field, airport}:
                            drop_printed_zones(payload, end)
                item = rebuild_item(
                    kind, payload, refdata=self.airports, book=self.book, key=key
                )
                if kind == "flight":
                    for field in FLIGHT_DERIVED:
                        value = item.payload.get(field)
                        if value != payload.get(field) and (value is not None or field in payload):
                            payload[field] = value
            opened = checks.open_checks(
                item,
                payload,
                owner=owner,
                in_reconcile=in_reconcile,
                sync_error=row["sync_error"],
                sync_error_at=row["sync_error_at"],
                told=told,
            )
        except (ValueError, TypeError) as exc:  # NothingToFile is a ValueError
            log.warning("trip item %s keeps its stored fields (%s)", row["id"], type(exc).__name__)
            payload, opened = stored, tuple(json.loads(row["checks_json"]))
        else:
            if not as_logged:
                new_key = self._settled_key(row, payload)
        old_trip = row["trip_id"]
        span = _span(kind, payload)
        if span is None:
            new_trip, start_at, end_at = None, None, None
            start_tz, end_tz = row["start_tz"], row["end_tz"]
        else:
            start_at, end_at, start_tz, end_tz = span
            start_tz, end_tz = start_tz or row["start_tz"], end_tz or row["end_tz"]
            new_trip = old_trip if old_trip is not None else self._place(kind, payload, row)
        state = checks.sync_state(
            kind=kind,
            airtrail_on=on,
            hold=row["sync_hold"],
            owner=owner,
            cancelled=status == "cancelled",
            told=told,
            open=opened,
            desired=checks.desired(payload, passengers, status, self.book),
        )
        want = {
            "item_key": new_key,
            "trip_id": new_trip,
            "start_at": start_at,
            "end_at": end_at,
            "start_tz": start_tz,
            "end_tz": end_tz,
            "sync_state": state,
        }
        fields: dict = {k: v for k, v in want.items() if row[k] != v}
        if payload != stored:
            fields["payload"] = payload
        if list(opened) != json.loads(row["checks_json"]):
            fields["checks"] = list(opened)
        return fields, old_trip, new_trip

    @staticmethod
    def _as_the_flight_log_said(kind: str, payload: dict) -> bool:
        """A flight whose stored fields are still the flight log's own (FLIGHT_LOG_SOURCE): no
        email has changed it since, and the owner has edited none of its FLIGHT_FIELDS."""
        if kind != "flight" or payload.get("source") != FLIGHT_LOG_SOURCE:
            return False
        edited = payload.get("owner_edits")
        return not (isinstance(edited, dict) and set(edited) & set(FLIGHT_FIELDS))

    def _settled_key(self, row: sqlite3.Row, payload: dict) -> str:
        """The key the row takes (settle's step 3): the one its fields make, even when it is one
        of the row's own aliases (a flight moved back to a day it left); that key leaves
        `payload["alt_keys"]` and the key it leaves joins them. A flight or a train under a real
        key whose fields no longer make one (a city typed for its origin) takes a draft key of
        its own (`keys.own_draft_key`), so the next email for the leg it now describes can
        complete it (`drafts_like`), and the key it leaves still finds it. KeyCollision when
        another booking holds the key, as its key or an alias."""
        key = row["item_key"]
        if row["kind"] not in REKEYED and not is_draft(key):
            return key
        new = real_key(row["kind"], payload, airports=self.airports)
        if new is None:
            if is_draft(key) or row["kind"] not in REKEYED:
                return key
            new = own_draft_key(row["id"])
        if new == key:
            return key
        other = self._find_by_key(new)
        if other is not None and other["id"] != row["id"]:
            raise KeyCollision(row["id"], other["id"])
        aliases = [k for k in _alt_keys(payload) if k != new]
        payload["alt_keys"] = list(dict.fromkeys([*aliases, key]))
        return new

    def _complete_draft(self, draft: sqlite3.Row, booking_id: int) -> TripItem:
        """The draft `draft` makes the key the booking `booking_id` holds, so it is that booking:
        it is merged into it, inside settle's transaction. The booking keeps every value it has
        and the draft fills only what it lacks (never a field the owner edited on the booking,
        blank or not, and nothing of LEFT_WITH_THE_DRAFT); their travellers, the names they
        print, their seats and their confirmation codes are joined (never a traveller the owner
        took off the booking, nor one an email newer than the draft's took off it); both rows'
        owner edits are kept (the booking's on a clash), and the owner's "no" about the leg
        holds; a soft finding
        the draft's email raised joins the booking's (`raised`, the later time of each), while a
        look at the draft is no look at the booking; and the draft's key and aliases become the
        booking's aliases. The draft's rows in the log of what each message did point at the
        booking, the draft row goes (and an emptied trip with it), and the booking — where it
        was, in its trip, removed or in a deleted trip, with its link to the flight log — is
        settled and returned. A draft and a booking that each hold a record in the flight log,
        two different ones, are never merged (KeyCollision, nothing changed): one record would be
        left behind there, which nothing deletes."""
        booking = self._row(booking_id)
        records = (draft["airtrail_flight_id"], booking["airtrail_flight_id"])
        if None not in records and records[0] != records[1]:
            raise KeyCollision(draft["id"], booking_id)
        mine = Ledger.trip_items_payload(booking)
        theirs = Ledger.trip_items_payload(draft)
        payload = dict(mine)
        edited = set(mine.get("owner_edits") or {})
        for field, value in theirs.items():
            held = mine.get(field)
            if field in (*LEFT_WITH_THE_DRAFT, "raised", "alt_keys") or value in BLANK:
                continue
            if held in BLANK:
                if field not in edited:
                    payload[field] = value
            elif field in JOINED and isinstance(held, list) and isinstance(value, list):
                payload[field] = list(dict.fromkeys([*held, *value]))
            elif field in JOINED_MAPS and isinstance(held, dict) and isinstance(value, dict):
                payload[field] = {**value, **held}
        self._stamp(payload, theirs.get("raised"))
        keys = [*_alt_keys(mine), draft["item_key"], *_alt_keys(theirs)]
        payload["alt_keys"] = [k for k in dict.fromkeys(keys) if k != booking["item_key"]]
        names = Ledger.trip_items_passengers(booking)
        # the draft brings back nobody the owner took off, nor anyone an email newer than the
        # draft's took off
        off = self._taken_off(mine) | self._removed_since(mine, names, theirs.get("email_date"))
        if off:
            payload["passengers"] = self._not_taken_off(payload.get("passengers"), off)
        conf = booking["confirmation"]
        for code in (draft["confirmation"] or "").split(", "):
            conf = _join_codes(conf, code or None)
        joining = self._not_taken_off(Ledger.trip_items_passengers(draft), off)
        self.led.trip_items.update(
            booking_id,
            payload=payload,
            passengers=list(dict.fromkeys([*names, *joining])),
            confirmation=conf,
        )
        self.led.items.repoint(draft["id"], booking_id)
        self.led.trip_items.delete(draft["id"])
        self.recompute_range(draft["trip_id"])
        return self.settle(booking_id)

    def _place(self, kind: str, payload: dict, row: sqlite3.Row) -> int:
        """The trip for a dated booking that has none: a trip holding one of its confirmation
        codes when there is one, else the one the date and place rules pick (find_or_create)."""
        preferred = None
        for code in (row["confirmation"] or "").split(", "):
            if code and (preferred := self.trip_for_pnr(code)) is not None:
                break
        item = SimpleNamespace(kind=kind, payload=payload)
        return self.find_or_create(item, preferred_id=preferred).id

    def sync_done(self, item_id: int, *, flight_id: int | None, told: dict | None) -> TripItem:
        """The flight log holds the booking as flight `flight_id` and was told `told`: the row
        records both, forgets the last attempt's error and its place in the queue, and settles."""
        with self._tx():
            if self.led.trip_items.get(item_id) is None:
                raise KeyError(item_id)
            self.led.trip_items.update(
                item_id,
                airtrail_flight_id=flight_id,
                told=told,
                sync_error=None,
                sync_error_at=None,
                sync_attempts=0,
                sync_after=None,
                sync_claimed_at=None,
            )
            return self.settle(item_id)

    def due_for_sync(self, *, now: dt.datetime) -> list[TripItem]:
        """The flights a sync pass may send at `now`, oldest change first: `pending`, or `blocked`
        by an airport the flight log did not know and by nothing else (the pass looks the airport
        up again); not removed and not in a deleted trip (the flight log keeps what it has and
        gains nothing new until the trip is restored); not waiting out a backoff (`sync_after`
        empty or past); and not claimed by another pass, unless that claim is older than
        CLAIM_SECONDS."""
        rows = self.led.trip_items.due_for_sync(
            now=stamp(now),
            claimed_before=stamp(now - dt.timedelta(seconds=CLAIM_SECONDS)),
            blocked_only_by="airtrail_airport",
        )
        return [self._item(r) for r in rows]  # type: ignore[misc]

    def claim_sync(self, item_id: int, *, now: dt.datetime, seconds: int = CLAIM_SECONDS) -> bool:
        """Claim the flight for one sync pass at `now`: one UPDATE that succeeds only when no
        claim is held or the one held is older than `seconds`, so of two passes at once one
        claims it. Whether this call claimed it. Bookkeeping only: nothing settle reads changes,
        so it does not settle."""
        cur = self.led.conn.execute(
            "UPDATE trip_items SET sync_claimed_at=? WHERE id=?"
            " AND (sync_claimed_at IS NULL OR sync_claimed_at < ?)",
            (stamp(now), item_id, stamp(now - dt.timedelta(seconds=seconds))),
        )
        return cur.rowcount == 1

    def release_sync(
        self, item_id: int, *, now: dt.datetime, retry_at: dt.datetime | None = None
    ) -> None:
        """Give up the claim taken at `now`, when it is still that one (a pass that outlived its
        claim never frees another pass's). With `retry_at` the attempt broke on an error of
        tripbox's own, not an answer of the flight log's: it is counted and the flight is not
        tried again before then, so one broken flight never holds up the rest. Bookkeeping only,
        like claim_sync: no check and no alert follow from it."""
        if retry_at is None:
            self.led.conn.execute(
                "UPDATE trip_items SET sync_claimed_at=NULL WHERE id=? AND sync_claimed_at=?",
                (item_id, stamp(now)),
            )
            return
        self.led.conn.execute(
            "UPDATE trip_items SET sync_claimed_at=NULL, sync_after=?,"
            " sync_attempts=sync_attempts+1 WHERE id=? AND sync_claimed_at=?",
            (stamp(retry_at), item_id, stamp(now)),
        )

    def sync_failed(
        self, item_id: int, *, code: str, now: dt.datetime, retry_at: dt.datetime | None
    ) -> TripItem:
        """The sync attempt at `now` did not finish, for the reason `code` (SYNC_ERRORS): the row
        records why and when, counts the attempt, is not tried again before `retry_at` (None:
        whenever it is due again), gives up its claim, and settles — a refusal or an unknown
        airport is a check on the booking then, an outage leaves it pending."""
        if code not in SYNC_ERRORS:
            raise ValueError(f"not a sync error: {code!r}")
        with self._tx():
            row = self.led.trip_items.get(item_id)
            if row is None:
                raise KeyError(item_id)
            self.led.trip_items.update(
                item_id,
                sync_error=code,
                sync_error_at=stamp(now),
                sync_attempts=row["sync_attempts"] + 1,
                sync_after=None if retry_at is None else stamp(retry_at),
                sync_claimed_at=None,
            )
            return self.settle(item_id)

    def sync_detached(self, item_id: int) -> TripItem:
        """The flight log no longer holds the booking's flight as the owner's (it was deleted
        there by hand, the owner was taken off it there, or the record under its id is another
        flight): the row forgets the flight and what it was told, is held out of the reconcile
        (`sync_hold` `deleted`, so nothing makes the flight again unasked), drops the last
        attempt's error and its claim, and settles."""
        with self._tx():
            if self.led.trip_items.get(item_id) is None:
                raise KeyError(item_id)
            self.led.trip_items.update(
                item_id,
                airtrail_flight_id=None,
                told=None,
                sync_hold="deleted",
                sync_error=None,
                sync_error_at=None,
                sync_attempts=0,
                sync_after=None,
                sync_claimed_at=None,
            )
            return self.settle(item_id)

    def sync_logged(self, item_id: int, digest: str) -> None:
        """A dry run logged the flight's request for what `digest` says it would be sent for, so
        it is neither logged nor due again until that changes. Bookkeeping only, like
        claim_sync."""
        self.led.conn.execute(
            "UPDATE trip_items SET sync_logged=? WHERE id=?", (digest, item_id)
        )

    # ------------------------------------------------------------------ the flight log switched
    def settle_flights(self) -> int:
        """settle() for every flight, removed or not, in one transaction, once the flight log was
        switched on or off: a flight's sync state, and the checks only a flight log explains,
        follow from whether one is kept. How many were settled (`_settled_or_left`)."""
        with self._tx():
            return sum(self._settled_or_left(row["id"]) for row in self.led.trip_items.flights())

    def hold_past(self, *, today: dt.date) -> int:
        """The flight log is being switched on, and what is held back from it is the history it
        never knew about. Every flight filed while no flight log was kept (at or after the time
        AIRTRAIL_OFF_AT_KV says it was last switched off; any, while it never was), never sent
        to one (no flight id, nothing told) and held out for nothing else, whose local day is
        before `today`, is held out of the reconcile as `past`: it had left by then, and goes
        only on request (release_past). A flight filed while a flight log was kept keeps its
        place in the queue across an off and on, whatever its day; so does one filed after the
        switch. A flight with no day is not held. Removed bookings and those in deleted trips
        are held too, so restoring one sends nothing. Each one held is settled
        (`_settled_or_left`); how many."""
        held = 0
        with self._tx():
            off_at = self.led.kv.get(AIRTRAIL_OFF_AT_KV)
            since = parse_iso(off_at) if off_at else None
            for row in self.led.trip_items.flights():
                sent = row["airtrail_flight_id"] is not None or row["told_json"] is not None
                if sent or row["sync_hold"] is not None:
                    continue
                if since is not None and parse_iso(row["created_at"]) < since:
                    continue  # filed while a flight log was kept
                left = _flight_day(Ledger.trip_items_payload(row))
                if left is None or left >= today:
                    continue
                self.led.trip_items.update(row["id"], sync_hold="past")
                self._settled_or_left(row["id"])
                held += 1
        return held

    def _settled_or_left(self, item_id: int, **left: object) -> bool:
        """settle() where one booking must not stop the rest (the switch of the flight log, which
        every command's context runs, and an enrolment): a booking whose key is now another
        booking's (KeyCollision, which changes nothing) is logged and left out of the
        reconcile, its sync state `off` and the columns `left` names written (the hold it
        keeps), so no state settled before can send it. A booking gone by its turn (settling
        another took it in, as a draft completed) is passed over. Whether it was settled."""
        try:
            self.settle(item_id)
        except KeyCollision:
            log.warning("trip item %s is left out: its key is another booking's", item_id)
            self.led.trip_items.update(item_id, sync_state="off", **left)
            return False
        except KeyError:
            if self.led.trip_items.get(item_id) is not None:
                raise
            log.info("trip item %s is gone: another booking took it in", item_id)
            return False
        return True

    def release_past(self, *, since: dt.date, apply: bool) -> int:
        """The flights held out as `past` (hold_past) that would be queued if they came back into
        the reconcile: dated on or after `since`, seen by the owner (not removed, not in a
        deleted trip), not cancelled (a cancelled flight never sent is never sent) and not a
        flight the owner is off (a companion's alone, or one the owner said no to). How many.
        With `apply` each comes back into the reconcile and is settled: `pending` for the
        owner's, `blocked` while a check is open or nobody is named, and the next sync pass looks
        for it in the flight log before it creates it; one whose key is now another booking's
        stays held and off (`_settled_or_left`) and is not counted. Without `apply` nothing
        changes."""
        found = 0
        with self._tx():
            for row in self.led.trip_items.flights():
                if row["sync_hold"] != "past" or row["status"] == "cancelled" or self._hidden(row):
                    continue
                payload = Ledger.trip_items_payload(row)
                left = _flight_day(payload)
                if left is None or left < since:
                    continue
                names = Ledger.trip_items_passengers(row)
                if checks.owner("flight", names, payload, self.book) == "off":
                    continue
                if apply:
                    self.led.trip_items.update(row["id"], sync_hold=None)
                    if not self._settled_or_left(row["id"], sync_hold="past"):
                        continue
                found += 1
        return found

    def forget_flight_log(self, *, today: dt.date, apply: bool) -> tuple[int, int, int]:
        """The flight log the ledger knew is no longer the one kept. Every flight that holds a
        flight id or anything told, removed or not, is counted: how many, and of them how many
        are dated from `today` on (one with no day among them) and how many before. With `apply`
        each forgets the flight id, what was told, the last attempt's error, its count and
        backoff, the claim and a dry run's digest; one whose local day is before `today` and
        that is held out for nothing is held out as `past` (history the flight log now kept
        never knew: it goes only on request, release_past), any other hold stays, and each is
        settled (`_settled_or_left`), so an upcoming flight the owner is on is `pending` and the
        next sync pass looks for it there before it creates it. Without `apply` nothing
        changes."""
        upcoming = past = 0
        with self._tx():
            for row in self.led.trip_items.flights():
                if row["airtrail_flight_id"] is None and row["told_json"] is None:
                    continue
                left = _flight_day(Ledger.trip_items_payload(row))
                before = left is not None and left < today
                past += before
                upcoming += not before
                if not apply:
                    continue
                hold = "past" if before and row["sync_hold"] is None else row["sync_hold"]
                self.led.trip_items.update(
                    row["id"],
                    airtrail_flight_id=None,
                    told=None,
                    sync_hold=hold,
                    sync_error=None,
                    sync_error_at=None,
                    sync_attempts=0,
                    sync_after=None,
                    sync_claimed_at=None,
                    sync_logged=None,
                )
                self._settled_or_left(row["id"], sync_hold=hold)
        return upcoming + past, upcoming, past

    # ------------------------------------------------------------------ repairs
    def _tx(self) -> AbstractContextManager[None]:
        """One transaction of the ledger's, joining one that is open."""
        return self.led.transaction()

    def rename(self, trip_id: int, title: str) -> Trip:
        title = _clean_text(title, "title")
        if title:
            self.led.trips.update(trip_id, title=title)
        return self.get(trip_id)  # type: ignore[return-value]

    def set_notes(self, trip_id: int, notes: str | None) -> Trip:
        clean = (notes or "").replace("\r\n", "\n").strip()
        self.led.trips.update(trip_id, notes=clean or None)
        return self.get(trip_id)  # type: ignore[return-value]

    def set_place(self, trip_id: int, place: str | None, *, look_again: bool = False) -> Trip:
        """The city the trip is about: bookings there on nearby dates prefer this trip (the
        same-place grouping). The title is the owner's and stays. A new place takes an automatic
        photo away; with `look_again` (automatic covers on) the same place saved again clears
        what is no photo, so its photo is looked for again (`CoverStore.look_again`)."""
        clean = _clean_text(place, "place")
        with self._tx():
            row = self.led.trips.get(trip_id)
            self.led.trips.update(trip_id, place=clean)
            if row is not None and row["place"] != clean:
                self.covers.place_changed(trip_id, clean)
            elif row is not None and clean and look_again:
                self.covers.look_again(trip_id)
        return self.get(trip_id)  # type: ignore[return-value]

    def merge(self, src_id: int, dst_id: int) -> Trip:
        if src_id == dst_id:
            return self.get(dst_id)  # type: ignore[return-value]
        with self._tx():
            src, dst = self.led.trips.get(src_id), self.led.trips.get(dst_id)
            if src is None or dst is None:
                raise KeyError(src_id if src is None else dst_id)
            self.led.conn.execute(
                "UPDATE trip_items SET trip_id=? WHERE trip_id=?", (dst_id, src_id)
            )
            if src["notes"]:  # the merged-away trip's notes travel along
                self.led.trips.update(
                    dst_id,
                    notes="\n\n".join(x for x in (dst["notes"], src["notes"]) if x),
                )
            self.covers.merged(src_id, dst_id)
            self.recompute_range(src_id)
            self.recompute_range(dst_id)
        return self.get(dst_id)  # type: ignore[return-value]

    def remove_item(self, item_id: int) -> Trip | None:
        """The owner's repair for a booking that should never have been filed: the row is hidden
        (`removed_at`) and nothing else changes — the log of what each email did and AirTrail
        keep theirs. The row is still found by its key, so an identical re-forward is a
        duplicate of it and leaves it hidden, and an email sent before the removal (a late copy
        that adds a traveller) is merged into it and leaves it hidden too; an email sent after
        the removal revives the same row, and *Recently deleted* restores it. Returns the trip,
        or None when it has no visible booking left."""
        ti = self.item(item_id)
        if ti is None or ti.removed_at:
            raise KeyError(item_id)
        with self._tx():
            self.led.trip_items.update(item_id, removed_at=now_iso())
            return self.recompute_range(ti.trip_id)

    def restore_item(self, item_id: int) -> Trip | None:
        """Back into its trip, or — when that trip was deleted since — into whichever live trip
        the grouping picks for it (possibly a new one). A booking with no date comes back with no
        trip, and None. Ends in settle."""
        with self._tx():
            ti = self.item(item_id)
            if ti is None or not ti.removed_at:
                raise KeyError(item_id)
            if ti.trip_id is None:
                self.led.trip_items.update(item_id, removed_at=None)
                self.settle(item_id)
                return None
            target = self.get(ti.trip_id)
            if target is None:
                try:
                    target = self.find_or_create(ti)
                except ValueError:  # the grouping cannot date it: a trip of its own, as move does
                    target = self.get(self._new_trip_for(ti))  # type: ignore[assignment]
            self.led.trip_items.update(item_id, removed_at=None, trip_id=target.id)
            if target.id != ti.trip_id:
                self.recompute_range(ti.trip_id)
            self.settle(item_id)
            return self.recompute_range(target.id)

    def delete_trip(self, trip_id: int) -> None:
        """Hide the whole trip. Its rows stay as they are (restore brings them all back), and so
        does the log of what each email did. AirTrail is never touched."""
        row = self.led.trips.get(trip_id)
        if row is None or row["deleted_at"]:
            raise KeyError(trip_id)
        self.led.trips.update(trip_id, deleted_at=now_iso())

    def restore_trip(self, trip_id: int) -> Trip | None:
        """Undo delete_trip. Bookings removed on their own before the delete stay removed. None
        when the trip has no visible booking to show."""
        row = self.led.trips.get(trip_id)
        if row is None or not row["deleted_at"]:
            raise KeyError(trip_id)
        with self._tx():
            self.led.trips.update(trip_id, deleted_at=None)
            return self.recompute_range(trip_id)

    def purge_deleted(
        self, *, now: dt.datetime | None = None, days: int = RETENTION_DAYS
    ) -> tuple[int, int]:
        """Hard-delete what has sat in Recently deleted for `days` or more, plus trips that never
        got a booking (a failed trip link) once they are a day old. Returns (bookings, trips)."""
        now = now or util.now()
        cutoff = (now - dt.timedelta(days=days)).isoformat(timespec="seconds")
        items = trips = 0
        with self._tx():
            c = self.led.conn
            emptied: dict[int, None] = {}
            for r in c.execute(
                "SELECT id, trip_id FROM trip_items WHERE removed_at IS NOT NULL"
                " AND removed_at <= ?",
                (cutoff,),
            ).fetchall():
                self.led.items.forget(r["id"])
                self.led.trip_items.delete(r["id"])
                items += 1
                emptied[r["trip_id"]] = None
            for tid in emptied:
                t = self.led.trips.get(tid)
                if t is None or t["deleted_at"]:
                    continue  # a deleted trip keeps its own retention clock
                self.recompute_range(tid)
                if self.led.trips.get(tid) is None:
                    trips += 1
            for t in c.execute(
                "SELECT id FROM trips WHERE deleted_at IS NOT NULL AND deleted_at <= ?",
                (cutoff,),
            ).fetchall():
                for r in self.led.trip_items.by_trip(t["id"], include_removed=True):
                    self.led.items.forget(r["id"])
                    self.led.trip_items.delete(r["id"])
                    items += 1
                self.led.trips.delete(t["id"])
                trips += 1
            day_ago = (now - dt.timedelta(days=1)).isoformat(timespec="seconds")
            for t in self.led.trips.empty_older_than(day_ago):
                self.led.trips.delete(t["id"])
                trips += 1
        return items, trips

    def _new_trip_for(self, ti: TripItem) -> int:
        """A trip of this booking's own, titled from it, dated from its stored span."""
        dates = grouping.item_dates(ti) or (
            dt.date.fromisoformat(ti.start_at[:10]),
            dt.date.fromisoformat((ti.end_at or ti.start_at)[:10]),
        )
        place = grouping.trip_place(grouping.trip_title(ti, self.home))
        return self.led.trips.insert(
            title=place or "Trip",
            place=None if place.lower() == "trip" else place,
            start_date=dates[0].isoformat(),
            end_date=dates[1].isoformat(),
        )

    def move_item(self, item_id: int, dst_trip_id: int | None) -> Trip:
        """Move one item to another trip, or (None) to a new trip titled from the item. A
        booking with no date has no trip to move to (ValueError). Ends in settle, which keeps
        the trip chosen here."""
        with self._tx():
            ti = self.item(item_id)
            if ti is None:
                raise KeyError(item_id)
            if ti.start_at is None:
                raise ValueError("a booking with no date is in no trip until it has one")
            if dst_trip_id is None:
                dst_trip_id = self._new_trip_for(ti)
            self.led.trip_items.update(item_id, trip_id=dst_trip_id)
            self.recompute_range(ti.trip_id)
            self.settle(item_id)
            return self.get(dst_trip_id)  # type: ignore[return-value]
