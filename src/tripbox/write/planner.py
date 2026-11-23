# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The booking plan: each booking read from an email is matched against the bookings the trip
store holds and becomes one action — file it, update the booking it is, call it a duplicate or
ignore it. Nothing is held back: what is doubtful about a booking is a check on it once it is
filed. It reads the store and writes nothing but the stamps on the booking's own payload; no
network."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from tripbox.trips.store import (
    BLANK,
    FLIGHT_DERIVED,
    FLIGHT_LOG_SOURCE,
    HAND,
    TAKEN_OFF,
    TripItem,
    TripStore,
    held_edits,
    removed_by_email,
)
from tripbox.util import now_iso, parse_iso
from tripbox.validate import checks
from tripbox.validate.keys import is_draft, own_draft_key, same_leg
from tripbox.validate.passengers import Classified, PassengerBook, people, person
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import (
    PRINTED_ZONES,
    Item,
    carry_printed_zones,
    day_alone,
    parse_local,
    without_days_alone,
    without_unreadable,
)

ActionKind = Literal["file", "update", "duplicate", "ignore"]
# Fields whose change means the booking itself moved. Cabin and seat differences between two
# receipts for the same booking are not a schedule change.
CHANGE_FIELDS: dict[str, tuple[str, ...]] = {
    "flight": ("dep_utc", "arr_utc", "date_local", "destination", "flight_code"),
    "lodging": ("check_in", "check_out", "property_name"),
    "car": ("pickup_time", "dropoff_time", "pickup_location", "dropoff_location"),
    "train": ("dep_local", "arr_local", "destination", "train_number"),
}
# Everything that describes WHEN and WHERE a booking is: an update that carries no change (a new
# name on the booking, a stale copy) keeps the stored values for these even when the email prints
# others, so a late original receipt cannot rewind a recorded schedule change through the merge.
SCHEDULE_FIELDS = (
    "dep_local",
    "arr_local",
    "dep_utc",
    "arr_utc",
    "date_local",
    "duration_min",
    "destination",
    "destination_name",
    "destination_city",
    "destination_tz",
    "flight_code",
    "flight_number",
    "train_number",
    "check_in",
    "check_out",
    "pickup_time",
    "dropoff_time",
    "pickup_location",
    "dropoff_location",
    "property_name",
)
# The kinds whose key holds a day and a number, so that a booking moved to another day or given
# another number arrives under a key the store has never seen.
MOVES = ("flight", "train")
# What the validator derives from a field alone, which an email says nothing about while the
# owner's edit of that field holds against it (`store.held_edits`).
DERIVED = {
    "dep_local": ("dep_utc", "date_local", "duration_min"),
    "arr_local": ("arr_utc", "duration_min"),
    "carrier": ("flight_code",),
    "flight_number": ("flight_code",),
}
# A flight's airport, its end's local time and that time's instant.
ENDS = (("origin", "dep_local", "dep_utc"), ("destination", "arr_local", "arr_utc"))
# What an email's payload says of the email itself, or what the plan writes on it, rather than of
# the booking; with the travellers (joined on their own), never a field a booking typed in by hand
# is filled in with (`_fills_in`).
NOT_FILLED_IN = (
    "message_id",
    "email_date",
    "subject",
    "from",
    "message_hash",
    "raised",
    "passengers",
    "passengers_inherited_from",
    "route_inherited_from",
    "airports_from_text",
    "change_recorded",
    "cancelled_email_date",
)


@dataclass
class Action:
    kind: ActionKind
    item: Item
    reason: str = ""  # update, ignore: what the email did, in one word
    trip_item_id: int | None = None  # the booking it touches
    add_passengers: list[str] = field(default_factory=list)
    remove_passengers: list[str] = field(default_factory=list)  # printed names, partial cancel
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)


@dataclass
class WritePlan:
    actions: list[Action] = field(default_factory=list)


def merge_items(items: list[Item]) -> list[Item]:
    """Fold items that share a key within one message into one (names, seats, problems unioned).

    A parser may hand back the same segment twice (one reservation per traveller on a receipt that
    lists each ticket); filing it twice would make two bookings of one. A draft key says nothing
    about its booking, so a draft folds only into an earlier draft of a flight leg that says the
    same thing (`keys.same_leg`), and keeps that first draft's key; a stay, a car or a train
    without a real key never folds.
    """
    out: list[Item] = []
    by_key: dict[str, Item] = {}
    drafts: list[Item] = []
    for item in items:
        if is_draft(item.key):
            base = next(
                (
                    d
                    for d in drafts
                    if item.kind == d.kind == "flight" and same_leg(d.payload, item.payload)
                ),
                None,
            )
        else:
            base = by_key.get(item.key)
        if base is None or base.kind != item.kind:
            by_key[item.key] = item
            if is_draft(item.key):
                drafts.append(item)
            out.append(item)
            continue
        for name in item.passengers:
            if name not in base.passengers:
                base.passengers.append(name)
        cls, other = base.classified, item.classified
        # a NEW object: the validator hands every leg its own Classified, and mutating a shared one
        # would put the owner on another traveller's other legs
        base.classified = Classified(
            me_present=cls.me_present or other.me_present,
            guests=cls.guests + [g for g in other.guests if g not in cls.guests],
            others=cls.others + [o for o in other.others if o not in cls.others],
            matches=cls.matches + other.matches,
        )
        p, q = base.payload, item.payload
        p["passengers"] = list(
            dict.fromkeys([*(p.get("passengers") or []), *(q.get("passengers") or [])])
        )
        p["seats"] = list(dict.fromkeys([*(p.get("seats") or []), *(q.get("seats") or [])]))
        p["seat_by_passenger"] = {
            **(p.get("seat_by_passenger") or {}),
            **(q.get("seat_by_passenger") or {}),
        }
        for k, v in q.items():
            if p.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
                p[k] = v
        base.pnr = base.pnr or item.pnr
        base.problems += [x for x in item.problems if x not in base.problems]
    return out


ROUTE_FIELDS = (
    "origin",
    "destination",
    "origin_name",
    "origin_city",
    "origin_tz",
    "destination_name",
    "destination_city",
    "destination_tz",
    "dep_local",
    "arr_local",
    "dep_utc",
    "arr_utc",
    "date_local",
    "duration_min",
)
# The findings a known segment's airports and times answer.
ROUTE_FINDINGS = ("origin", "destination", "departure", "arrival_before_departure", "long_leg")


def _timed(value: object) -> bool:
    """Whether `value` prints a local time of day (not a day alone, and not nothing)."""
    return isinstance(value, str) and parse_local(value) is not None and day_alone(value) is None


def _same_departure(p: dict, held: dict) -> bool:
    """Whether the departure an email prints (`p`) is the timed departure a booking holds
    (`held`): the same local time, or a day alone on the day of that time, which says nothing
    about the time (`rules.without_days_alone`). A booking that holds no time for its departure
    is never the same as anything: two days alone say nothing of each other."""
    if not _timed(held.get("dep_local")):
        return False
    mine = p.get("dep_local")
    if _timed(mine):
        return parse_local(mine) == parse_local(held["dep_local"])
    return isinstance(mine, str) and "dep_local" not in without_days_alone(held, p)


def inherit_route(item: Item, store: TripStore) -> bool:
    """A receipt whose airports came back as city names (the model does not always pick the
    printed code) still describes a segment the store knows: same confirmation code, flight
    number, local date and printed departure time. Take that booking's resolved airports and
    times. A departure printed as a day alone is that segment only when the booking holds a time
    on that day, and then takes it."""
    codes = {p.code for p in item.problems}
    if item.kind != "flight" or not item.pnr or not codes & {"origin", "destination", "departure"}:
        return False
    p = item.payload
    for row in store.by_code("flight", item.pnr):
        op = row.payload
        if op.get("flight_code") != p.get("flight_code") or op.get("date_local") != p.get(
            "date_local"
        ):
            continue
        if not _same_departure(p, op):
            continue
        before = dict(p)
        for k in ROUTE_FIELDS:
            if op.get(k) is not None:
                p[k] = op[k]
        carry_printed_zones(p, before, op)  # the inherited times come with what they were read by
        p["route_inherited_from"] = row.id
        item.key = row.item_key
        item.problems = [x for x in item.problems if x.code not in ROUTE_FINDINGS]
        return True
    return False


def _codes(row: TripItem) -> list[str]:
    """The confirmation codes a booking holds (two bookings on one flight join theirs)."""
    return [c for c in (row.confirmation or "").split(", ") if c]


def inherit_passengers(
    item: Item,
    store: TripStore,
    book: PassengerBook,
    row: TripItem | None = None,
    *,
    cancellation: bool = False,
) -> bool:
    """A change, cancellation or receipt for a confirmation code the store already knows often
    prints no traveller names. The booking is known, so its names apply. `row` is the booking
    the item is for (under its key, or the one it moved from): its names are the ones taken,
    when it holds the item's code, since each leg has its own travellers. For a cancellation
    (`cancellation`) that code must be the booking's only one: where two bookings share a
    flight, a cancellation under one code that names nobody does not say whose seat it cancels.
    An item that is no booking the store holds takes names only when the bookings of its kind
    under the code that the owner can see all carry the same travellers (one booking agrees
    with itself), one of them is no draft and none holds more than one code: where legs differ,
    which one's travellers are on a new leg is a guess, and a guess that names the owner would
    be sent to the flight log; a draft is no leg the store can tell, so it vouches for no other;
    and a booking of seats booked apart under several codes does not say whose seat a new leg
    under one of them is. Only the finding that nobody is named goes; every other finding still
    holds."""
    if not item.pnr or not any(p.code == "owner_unknown" for p in item.problems):
        return False
    if row is None:
        rows = store.by_code(item.kind, item.pnr)
        if any(len(_codes(r)) > 1 for r in rows):
            # seats booked apart share that booking: whose seat a new leg under one of its codes
            # is, it does not say, and a guess that names the owner would be sent
            return False
        aboard = [people(_held_names(r), book) for r in rows]
        legs = [r for r in rows if not is_draft(r.item_key)]
        if not legs or any(who != aboard[0] for who in aboard):
            return False
        row = legs[-1]
    elif item.pnr not in _codes(row):
        return False  # another booking's code: its travellers are not this email's
    elif cancellation and len(_codes(row)) > 1:
        return False  # two bookings on one flight: nothing says whose seat this cancels
    printed = [n for n in (row.payload.get("passengers") or row.passengers) if n and n.strip()]
    if not printed:
        return False
    known = book.classify(printed)
    cls = item.classified
    item.classified = Classified(  # a new object, as in merge_items
        me_present=cls.me_present or known.me_present,
        guests=cls.guests + [g for g in known.guests if g not in cls.guests],
        others=cls.others + [o for o in known.others if o not in cls.others],
        matches=cls.matches + known.matches,
    )
    item.passengers = item.passengers or list(printed)
    item.payload["passengers"] = item.payload.get("passengers") or list(printed)
    item.payload["passengers_inherited_from"] = row.id
    item.problems = [p for p in item.problems if p.code != "owner_unknown"]
    return True


def _not_held(
    kind: str, old: dict, new: dict, fields: tuple[str, ...]
) -> tuple[dict, tuple[str, ...]]:
    """What an email (`new`) may still change of a booking (`old`) while the owner's edits hold
    against it (`store.held_edits`): `new` without each field an edit holds and what is derived
    from it alone (DERIVED), and the fields to compare. A flight's airport the owner set reads
    that end's time in another zone than the email's, so for that end its local time is compared
    instead of its instant."""
    held = held_edits(old, new)
    if not held:
        return new, fields
    gone = set(held).union(*(DERIVED.get(f, ()) for f in held))
    if kind == "flight":
        for airport, local, instant in ENDS:
            if airport in held:
                gone.add(instant)
                fields = (*fields, local) if local not in held else fields
    return {k: v for k, v in new.items() if k not in gone}, tuple(dict.fromkeys(fields))


def _diff(old: dict, new: dict, fields: tuple[str, ...]) -> dict[str, tuple[Any, Any]]:
    """The fields `new` prints a value for that differs from `old`'s; a day printed alone on the
    day of a time `old` holds prints nothing for that end (`rules.without_days_alone`)."""
    new = without_days_alone(old, new)
    changes: dict[str, tuple[Any, Any]] = {}
    for f in fields:
        o, n = old.get(f), new.get(f)
        if n is not None and o != n:
            changes[f] = (o, n)
    return changes


def _fills_in(kind: str, old: dict, said: dict) -> bool:
    """Whether an email fills in a field the booking (`old`) lacks: `said`, what it prints that
    the owner's edits do not hold, gives a value for a field the booking holds nothing for. What
    the email says of itself (NOT_FILLED_IN) does not count, nor a printed zone (it goes with its
    time), nor what settle derives for a flight from its other fields (`store.FLIGHT_DERIVED`);
    a schedule field counts only when the booking holds no such key, which is when the merge
    takes it (`executor.Executor._said`)."""
    skip = set(NOT_FILLED_IN).union(*(keys for *_, keys in PRINTED_ZONES.values()))
    if kind == "flight":
        skip.update(FLIGHT_DERIVED)
    return any(
        value not in BLANK
        and old.get(k) in BLANK
        and k not in skip
        and not (k in SCHEDULE_FIELDS and k in old)
        for k, value in said.items()
    )


def _older_copy(new_payload: dict, old_payload: dict, document_type: str) -> bool:
    """A plain booking copy (a receipt forwarded late, for one) that was sent before the email
    whose data the booking holds must never rewind it; only change/cancellation documents and
    newer mail may move a booking."""
    if document_type != "booking":
        return False
    return earlier(new_payload.get("email_date"), old_payload.get("email_date"))


def earlier(a: object, b: object) -> bool:
    """Whether the time `a` is before the time `b`, as instants (two dates may print different
    offsets). A time that is missing or cannot be read is not earlier than anything."""
    try:
        return parse_iso(str(a)) < parse_iso(str(b)) if a and b else False
    except ValueError:
        return False


def _held_names(row: TripItem) -> list[str]:
    return [*row.passengers, *(row.payload.get("passengers") or [])]


def _lacking(item: Item, row: TripItem, book: PassengerBook) -> list[str]:
    """The travellers `item` names that the booking `row` does not hold yet, that the owner did
    not take off it (`store.TAKEN_OFF`) and that no email newer than `item`'s took off it
    (`store.removed_by_email`): the owner and each companion by the name the book gives them,
    anyone else as printed; each once."""
    off = row.payload.get(TAKEN_OFF)
    held = people([*_held_names(row), *(off if isinstance(off, list) else [])], book)
    held |= removed_by_email(row.payload, item.payload.get("email_date"), book)
    out: list[str] = []
    for name in item.passengers:
        who = person(name, book) if name else None
        if who is None or who in held:
            continue
        held.add(who)
        out.append(book.match(name).canonical)
    return out


def _cancellation(
    item: Item, row: TripItem, book: PassengerBook, riding: set[tuple[str, str]] | None = None
) -> Action:
    """What a cancellation does to the booking `row` it names, by who it names among the
    travellers the booking holds (people told apart as `passengers.person` tells them):

    - the owner, when the booking holds the owner; nobody; every traveller the booking holds;
      or the booking holds no name: the booking is cancelled;
    - some of them, while others remain: those travellers leave the booking (a name it prints
      that the booking does not hold is left alone);
    - only people the booking does not hold, while it holds others: nothing — it cancels nobody
      this booking has (the same partial cancellation read a second time, say).

    On a booking that holds travellers and not the owner, the owner's is one more name it does
    not hold: a companion's or a relative's flight is not cancelled because the owner cancelled
    a seat of their own.

    `riding` holds, when another booking of the same message moves the row, the travellers that
    leg carries. One of them is not removed: the new leg names them, and its merge would bring
    them back or not by which of the two the email lists first."""
    held: dict[tuple[str, str], list[str]] = {}
    for name in dict.fromkeys(_held_names(row)):
        who = person(name, book) if name else None
        if who is not None:
            held.setdefault(who, []).append(name)
    named = people(item.passengers, book)
    hit = named & held.keys()
    owner = ("known", book.me_name)
    if not named or not held or hit == held.keys() or owner in hit:
        return Action("update", item, reason="cancel", trip_item_id=row.id)
    leaving = hit - (riding or set())
    if not leaving:
        return Action("duplicate", item, trip_item_id=row.id)
    gone = [name for who, names in held.items() if who in leaving for name in names]
    return Action(
        "update", item, reason="remove_passengers", trip_item_id=row.id, remove_passengers=gone
    )


def _day(kind: str, p: dict) -> str | None:
    """The local day a flight or a train leaves on, as its key holds it."""
    value = p.get("date_local") if kind == "flight" else None
    return str(value or p.get("dep_local") or "")[:10] or None


def moved_from(item: Item, store: TripStore, book: PassengerBook) -> TripItem | None:
    """The booking a flight or a train moved from, when the store holds it under another key: a
    booking of the same kind that holds the item's confirmation code under another real key, or
    under its own draft key (a booking an edit on the page left with no day or no number, which
    is still that booking, `keys.own_draft_key`; a draft an email filed never is), with the same
    origin and destination (for a flight, the same carrier too), and either the same number on
    another day or another number on the same day (a booking with no day has no day of its
    own, so the same number moves it to the email's). When the item names any traveller,
    only a booking that shares one with it counts, so another traveller's receipt on a split
    booking never moves the owner's flight; and a booking that holds more than one code (seats
    booked apart on one flight) counts only when the item names everyone it holds or names the
    owner, so another traveller's change under one of its codes never moves the owner's seat.
    Exactly one such booking is the one that moved; none or several mean it did not, and the
    item is a booking of its own. The bookings the owner can see decide; only when none of them
    could have moved is one that was put away the booking that moved (a change email brings it
    back)."""
    if item.kind not in MOVES or not item.pnr or is_draft(item.key):
        return None
    p = item.payload
    number = "flight_code" if item.kind == "flight" else "train_number"
    same: tuple[str, ...] = ("origin", "destination")
    if item.kind == "flight":
        same = ("carrier", *same)
    day = _day(item.kind, p)
    if day is None:
        return None
    mine = people(item.passengers, book)
    owner = ("known", book.me_name)

    def could_be(row: TripItem) -> bool:
        op = row.payload
        if row.item_key == item.key:
            return False
        if is_draft(row.item_key) and row.item_key != own_draft_key(row.id):
            return False
        if any(op.get(k) != p.get(k) for k in same):
            return False
        same_number = bool(p.get(number)) and op.get(number) == p.get(number)
        same_day = _day(item.kind, op) == day
        if same_number == same_day:  # neither a move to another day nor another number that day
            return False
        held = people(_held_names(row), book)
        if len(_codes(row)) > 1 and not (mine and (mine >= held or owner in mine)):
            # bookings made apart on one flight: a change under one code moves the others' seats
            # only when it names everyone the booking holds, or the owner, whose booking it is
            return False
        return not mine or bool(mine & held)

    for hidden in (False, True):
        found = [r for r in store.by_code(item.kind, item.pnr, hidden=hidden) if could_be(r)]
        if found:
            return found[0] if len(found) == 1 else None
    return None


def _for_a_flight_it_left(
    item: Item, row: TripItem, book: PassengerBook, *, riding: set[tuple[str, str]] | None
) -> bool:
    """Whether a cancellation found for the booking `row` cancels a flight (or a train) the
    booking is no longer on, and so changes nothing of it: cancelled and rebooked, whichever
    arrives first. Only a flight or a train leaves a key; a stay's or a car's other key is a
    code the owner corrected, and that booking is still the one cancelled.

    When another booking of the same message moves it, `riding` holds the travellers that leg
    names (everyone the booking holds, when it names nobody), and the email itself says who
    goes on: the cancellation is for the flight being left exactly when everyone it takes off
    (the held travellers it names; all of them when it prints no name) is among them. A
    traveller it takes off whom the new leg leaves out has lost their seat, and to leave the
    owner live on a flight the email took them off is the one outcome that must not happen.

    With no such leg, the booking has left the flight when a recorded change moved it and the
    cancellation comes under a key it left. Nothing then says who that change carried, so the
    cancellation is for the flight it left only as a whole: it prints no name, or names every
    traveller the booking holds (a name the booking does not hold counts for nothing).

    Any other cancellation is decided like any cancellation of the booking (`_cancellation`)."""
    if row.kind not in MOVES:
        return False
    held = people(_held_names(row), book)
    named = people(item.passengers, book)
    if riding is not None:
        return (named & held if named else held) <= riding
    left = row.item_key != item.key and bool(row.payload.get("change_recorded"))
    return left and (not named or named >= held)


def _stale(item: Item, row: TripItem, document_type: str) -> bool:
    """Whether an email that is no cancellation may not move the booking `row`: a booking copy
    never rewinds what a newer email, or a change already recorded, established (a receipt and
    the itinerary of its change may arrive in the same second)."""
    return _older_copy(item.payload, row.payload, document_type) or (
        document_type == "booking" and bool(row.payload.get("change_recorded"))
    )


def _known(
    item: Item,
    row: TripItem,
    *,
    moved: bool,
    cancellation: bool,
    document_type: str,
    book: PassengerBook,
    airports: Airports,
    riding: set[tuple[str, str]] | None = None,
) -> Action:
    """What an email does to the booking `row` it is for; `moved` says the row was found by
    `moved_from`, under another key, and `riding` holds, when another booking of the same
    message moves the row, the travellers that booking carries. A value that fails its hard
    check on the email (`rules.without_unreadable`, against `airports`) is no change, and
    neither is one the owner's edit holds against the email (`_not_held`). A
    cancellation that names nobody, for a booking that holds travellers it took no names from
    (`inherit_passengers`), does not say whose seat it cancels: it is ignored, and marks the
    booking (`checks.UNKNOWN_CANCELLATION`). An email that adds no traveller and changes
    nothing of a booking typed in by hand (`payload["source"]` `store.HAND`) still fills in what
    the booking lacks and it prints (`fills_in`, recording no change); one that fills nothing in
    is a duplicate."""
    old = row.payload
    if cancellation:
        if _for_a_flight_it_left(item, row, book, riding=riding):
            return Action("ignore", item, reason="older_copy", trip_item_id=row.id)
        if any(p.code == "owner_unknown" for p in item.problems) and _held_names(row):
            # it names nobody, and the booking's names are not its (another code, or a code
            # two bookings share): it cancels nobody's seat, and the owner is asked to look
            return Action("ignore", item, reason=checks.UNKNOWN_CANCELLATION, trip_item_id=row.id)
        return _cancellation(item, row, book, riding)
    stale = _stale(item, row, document_type)
    fields = CHANGE_FIELDS.get(item.kind, ())
    if row.kind == "flight" and old.get("source") == FLIGHT_LOG_SOURCE:
        # its fields are still the flight log's own, which may hold its instants with no zone
        # to read them in: the local times its email prints count too, so the email is merged
        fields = ("dep_local", "arr_local", *fields)
    said, fields = _not_held(
        item.kind, old, without_unreadable(item.kind, item.payload, airports), fields
    )
    changes = {} if stale else _diff(old, said, fields)
    new_names = _lacking(item, row, book)
    if new_names:
        return Action(
            "update",
            item,
            reason="add_passengers",
            trip_item_id=row.id,
            add_passengers=new_names,
            changes=changes,
        )
    if changes or (moved and not stale):
        return Action(
            "update",
            item,
            reason="rescheduled" if moved else "changed",
            trip_item_id=row.id,
            changes=changes,
        )
    if stale and row.item_key != item.key:
        # found by moved_from, or under a key the booking has left: the copy describes the
        # booking as it was
        return Action("ignore", item, reason="older_copy", trip_item_id=row.id)
    if old.get("source") == HAND and _fills_in(item.kind, old, said):
        # typed in by hand, it lacks what the owner left empty, which its email prints: taken
        # as no change, so a later change email still finds and moves the booking (its code)
        return Action("update", item, reason="fills_in", trip_item_id=row.id)
    return Action("duplicate", item, trip_item_id=row.id)


def _taken_off(
    found: list, *, document_type: str, book: PassengerBook, airports: Airports
) -> dict[int, set[tuple[str, str]]]:
    """The travellers each cancellation of a message takes off the booking it names while the
    rest stay on it (`_cancellation`'s `remove_passengers`, decided as though no new leg moved
    the booking), by booking id. A cancellation that cancels the booking, or is an older copy,
    or cancels nobody, takes nobody off it."""
    out: dict[int, set[tuple[str, str]]] = {}
    for item, row, _moved, cancellation, _completes, _by_days in found:
        if not cancellation or row is None:
            continue
        action = _known(
            item,
            row,
            moved=False,
            cancellation=True,
            document_type=document_type,
            book=book,
            airports=airports,
        )
        if action.reason == "remove_passengers":
            out.setdefault(row.id, set()).update(people(action.remove_passengers, book))
    return out


def plan(
    items: list[Item],
    *,
    document_type: str,
    store: TripStore,
    book: PassengerBook,
    received_at: str | None = None,
) -> WritePlan:
    """One action per booking of a message (`merge_items(items)`), worked out in this order:

    1. a booking whose airports or times were not read is first repaired from the segment the
       store knows under the same confirmation code (`inherit_route`), which gives it its key;
    2. the booking it is in the store is the one filed under its key (or holding it as an
       alias), else the one it moved from (`moved_from`; but when a cancellation of the same
       message takes every traveller it names off that booking while others stay on it, those
       travellers leave the booking and it is filed on its own), else, for a booking with a
       real key, the one draft it can complete (`TripStore.drafts_like`: under its code,
       saying enough to be one leg, agreeing with it wherever both say something and, when
       both name someone, sharing a traveller; none, or several, and it completes none, nor
       one that another booking of the message could complete too), else, for a stay or a
       car, the one booking typed in by hand with no code that has its days
       (`TripStore.typed_in_like`, the same rule of one), which it fills in (`fills_in`, or a
       duplicate when it has nothing to fill in) and whose alias its code's key becomes. A
       cancellation acts only on the booking it names: neither `moved_from`, `drafts_like` nor
       `typed_in_like` is asked for one;
    3. a booking that names nobody takes the names of that booking when it holds the item's
       code (for a cancellation, when that is its only code); one that is no booking the store
       holds takes names only when the bookings under its code all carry the same travellers
       and one of them is no draft, since a draft vouches for no leg (`inherit_passengers`);
    4. its soft findings are stamped on its payload as raised at `received_at`: the time its
       message's bookings first reach the ledger, which the caller works out (`run._found_at`;
       now when none is given) — never at the email's own date, which is the sender's to
       write. Nothing is held back for a finding: it is a check on the booking once it is
       filed, which keeps a flight from the flight log until it is cleared;
    5. a draft it completes is updated with what the email prints (`completes_draft`, which
       records no change), and the store's settle files it under its real key, the draft key
       kept as an alias;
    6. with a booking: a cancellation cancels it, removes the travellers it names, or does
       nothing (`_cancellation`), unless it is for a flight or a train the booking is no longer
       on (`_for_a_flight_it_left`), which makes it an older copy: one the booking is leaving
       through another booking of the same message, when that leg names everyone the
       cancellation takes off; or one it has left through a recorded change, when the
       cancellation prints no name or names every traveller the booking holds. One that names
       nobody and took no names from the booking cancels nobody: it is ignored, and the
       booking is marked `unknown_cancellation`, stamped as the findings are. Anything else
       adds the travellers the booking lacks (never one the owner took off it), records what
       changed (never from an older copy, nor from a plain booking after a recorded change, nor
       from a value that fails its hard check or that the owner's edit holds against the email,
       which say nothing), fills in what a booking typed in by hand lacks (`fills_in`), or is a
       duplicate;
    7. with none: a cancellation is filed as the cancelled booking it says, marked
       `unknown_cancellation`; anything else is filed, with its checks (under a draft key when
       its fields do not make a real one)."""
    at = received_at or now_iso()
    looked: list[tuple[Item, TripItem | None, bool, bool, list[TripItem], bool]] = []
    for item in merge_items(items):
        if item.problems:
            inherit_route(item, store)
        cancellation = document_type == "cancellation" or item.status == "cancelled"
        row = store.find(item.key)
        moved = False
        like: list[TripItem] = []
        by_days = False
        if row is None and not cancellation:
            row = moved_from(item, store, book)
            moved = row is not None
            if row is None and not is_draft(item.key):
                like = store.drafts_like(item)
            if row is None and not like:
                # a stay or a car typed in by hand with no code is found by its days
                like = store.typed_in_like(item)
                by_days = bool(like)
        looked.append((item, row, moved, cancellation, like, by_days))
    # a draft, or a booking typed in by hand, that two bookings of the message could be says too
    # little to tell which one it is: neither is it
    fits = Counter(one.id for *_, like, _ in looked for one in like)
    found: list[tuple[Item, TripItem | None, bool, bool, bool, bool]] = []
    for item, row, moved, cancellation, like, by_days in looked:
        completes = len(like) == 1 and fits[like[0].id] == 1
        if completes:
            row = like[0]
        inherit_passengers(item, store, book, row, cancellation=cancellation)
        checks.raise_soft(item.payload, checks.codes(item), at=at)
        found.append((item, row, moved, cancellation, completes, by_days))

    def decide(
        item: Item,
        row: TripItem | None,
        moved: bool,
        cancellation: bool,
        completes: bool,
        by_days: bool,
    ) -> Action:
        if row is not None and completes and by_days:
            # a stay or a car typed in by hand with no code: the email fills in what it lacks,
            # its code included, whose key the merge makes the booking's alias
            printed = without_unreadable(item.kind, item.payload, store.airports)
            said, _ = _not_held(item.kind, row.payload, printed, ())
            if _fills_in(item.kind, row.payload, said):
                return Action("update", item, reason="fills_in", trip_item_id=row.id)
            return Action("duplicate", item, trip_item_id=row.id)
        if row is not None and completes:
            # what the draft lacked, or held as printed (a city for an airport), differs from
            # what the email prints: as changes the merge takes them, and settle then files the
            # draft under its real key
            said = without_unreadable(item.kind, item.payload, store.airports)
            changes = _diff(row.payload, said, CHANGE_FIELDS.get(item.kind, ()))
            return Action(
                "update", item, reason="completes_draft", trip_item_id=row.id, changes=changes
            )
        if row is not None:
            action = _known(
                item,
                row,
                moved=moved,
                cancellation=cancellation,
                document_type=document_type,
                book=book,
                airports=store.airports,
                riding=moving.get(row.id) if cancellation else None,
            )
            if action.reason == checks.UNKNOWN_CANCELLATION:
                checks.raise_soft(item.payload, [checks.UNKNOWN_CANCELLATION], at=at)
            return action
        if cancellation:
            # a cancellation for a booking never filed: filed as the cancelled booking it says
            item.status = "cancelled"
            checks.raise_soft(item.payload, [checks.UNKNOWN_CANCELLATION], at=at)
            return Action("file", item)
        if document_type == "change" or item.status == "changed":
            # filed as it stands after a change: an older booking copy must not rewind it
            item.payload["change_recorded"] = True
        return Action("file", item)

    # a new leg that names only travellers a cancellation of the same message takes off a
    # booking (leaving the rest on it) is for them alone: it does not move that booking, the
    # cancellation takes them off it, and the new leg is filed on its own
    taken_off = _taken_off(found, document_type=document_type, book=book, airports=store.airports)
    # the bookings this message moves, each with the travellers its new leg carries, are known
    # before any of its cancellations is decided, so the two come out alike whichever the email
    # lists first. An update moves the booking it found under another key unless it is a stale
    # copy, which only adds a traveller; a leg that names nobody carries everyone aboard
    moving: dict[int, set[tuple[str, str]]] = {}
    actions: dict[int, Action] = {}
    for n, (item, row, moved, cancellation, completes, by_days) in enumerate(found):
        if not cancellation:
            mine = people(item.passengers, book)
            if moved and row is not None and mine and mine <= taken_off.get(row.id, set()):
                row, moved = None, False
            actions[n] = decide(item, row, moved, cancellation, completes, by_days)
            if moved and row is not None and actions[n].kind == "update":
                if not _stale(item, row, document_type):
                    carried = people(item.passengers, book) or people(_held_names(row), book)
                    moving.setdefault(row.id, set()).update(carried)
    for n, entry in enumerate(found):
        if n not in actions:
            actions[n] = decide(*entry)
    return WritePlan([actions[n] for n in range(len(found))])
