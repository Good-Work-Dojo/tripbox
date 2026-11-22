# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Checks: what is doubtful about a filed booking, which of those doubts are open, what a flight
log should hold for a flight and where the flight's sync with it stands.

A hard check names a field that is wrong and closes only when the field is fixed. A soft check
names a doubt and closes when the owner looks at the booking (Looks right, or saving the edit
form); one an email raised reopens only when a newer email raises it again. Everything here is
pure: it reads only what it is given (no ledger, no clock, no settings), and every time it is
given is an ISO-8601 string in UTC."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from tripbox.util import parse_iso
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rules import FUTURE_LIMIT, MAX_SEGMENT, PAST_SLACK, Item

# Cleared only by fixing the field.
HARD = (
    "carrier",
    "flight_number",
    "origin",
    "destination",
    "departure",
    "arrival_before_departure",
    "check_in",
    "check_out",
    "pickup_time",
    "dropoff_time",
    "stations",
    "airtrail_airport",
)
# Cleared by Looks right or by saving the edit form.
SOFT = (
    "low_confidence",
    "unverified_sender",
    "owner_unknown",
    "date_past",
    "date_far",
    "long_leg",
    "missing_name",
    "unknown_cancellation",
    "airtrail_refused",
    "airtrail_still_lists",
)
ALL = HARD + SOFT
SYNC_STATES = ("off", "blocked", "pending", "synced")
# What the owner can say about a booking: the buttons of its box on the page and of its card.
ACTIONS = ("looks_right", "on_it", "send_again")
# The check, and the word beside an `ignored` row of the log, of a cancellation that could not be
# matched for certain: one for no booking filed, or one that does not say whose seat it cancels.
UNKNOWN_CANCELLATION = "unknown_cancellation"
# The check of a flight the flight log holds as the owner's while the owner is off it
# (`still_listed`). Nothing is ever written to such a flight, so the owner is told to tidy it
# there; settle stamps in `payload["raised"]` when that began, and drops the stamp when it ends.
STILL_LISTS = "airtrail_still_lists"

# Soft checks the booking's own fields raise: each is open only while the booking still shows it.
FROM_THE_BOOKING = ("owner_unknown", "date_past", "date_far", "long_leg", "missing_name")
# Soft checks an email raises: the booking cannot show them again once it is filed, so each is
# open from the email's stamp until the owner looks.
FROM_AN_EMAIL = ("low_confidence", "unverified_sender", "unknown_cancellation")

_LONGEST_HOURS = int(MAX_SEGMENT.total_seconds() // 3600)

# What each check says on the page: one sentence (STILL_LISTS adds what to do about it), naming
# no product or channel.
LABELS: dict[str, str] = {
    "carrier": "The airline's two-character code is missing or unreadable.",
    "flight_number": "The flight number is missing or is not one to four digits.",
    "origin": "The departure airport is missing or is not a known airport code.",
    "destination": "The arrival airport is missing or is not a known airport code.",
    "departure": "The departure time is missing or unreadable, or its time zone is not known.",
    "arrival_before_departure": "The arrival is before the departure.",
    "check_in": "The check-in date is missing or unreadable.",
    "check_out": "The check-out date is missing or unreadable, or is before check-in.",
    "pickup_time": "The pick-up time is missing or unreadable.",
    "dropoff_time": "The drop-off time is missing or unreadable, or is before pick-up.",
    "stations": "The departure or arrival station is missing.",
    "airtrail_airport": "The flight log does not know one of this flight's airports.",
    "low_confidence": "The email was hard to read, so the details may be wrong.",
    "unverified_sender": "The sender of this email could not be verified.",
    "owner_unknown": "No traveller is named, so it is not known whether you were on this flight.",
    "date_past": f"The date is more than {PAST_SLACK.days} days before the email was sent.",
    "date_far": f"The date is more than {FUTURE_LIMIT.days} days after the email was sent.",
    "long_leg": f"The flight takes longer than {_LONGEST_HOURS} hours.",
    "missing_name": "The name of the hotel or the rental company is missing.",
    "unknown_cancellation": (
        "A cancellation email could not be matched for certain: check this booking."
    ),
    "airtrail_refused": "The flight log refused this flight.",
    "airtrail_still_lists": (
        "Your flight log still lists this flight as yours. Remove it there if you did not fly."
    ),
}


def _later(a: str, b: str) -> bool:
    """Whether the time `a` is after the time `b`."""
    return parse_iso(a) > parse_iso(b)


def _time(at: object) -> str:
    """`at`, when it is a time; else ValueError. A stamp is checked where it is written, so one
    malformed stamp can never make every later reading of the booking fail."""
    if not isinstance(at, str):
        raise ValueError(f"not a time: {at!r}")
    parse_iso(at)  # ValueError when it is not one
    return at


def codes(item: Item) -> tuple[str, ...]:
    """The item's problem codes, each once, in ALL's order. A code outside ALL is a mistake in the
    validator and raises ValueError, so no doubt is ever dropped unseen."""
    found = {p.code for p in item.problems}
    if unknown := found - set(ALL):
        raise ValueError(f"not a check code: {', '.join(sorted(unknown))}")
    return tuple(code for code in ALL if code in found)


def raise_soft(payload: dict, found: Iterable[str], at: str) -> None:
    """Stamp each soft code of `found` in `payload["raised"]` as raised at `at`, unless a later
    stamp is there already: an older copy of an email, replayed, never moves a stamp back. A hard
    code is not stamped; it is open for as long as the booking shows it. ValueError, and nothing
    stamped, when `at` is not a time."""
    at = _time(at)
    for code in found:
        if code not in SOFT:
            continue
        raised = payload.get("raised") or {}
        before = raised.get(code)
        if before is None or not _later(before, at):
            payload["raised"] = {**raised, code: at}


def looked(payload: dict, at: str) -> None:
    """The owner looked at the booking at `at` (Looks right, or saved the edit form). A look
    earlier than the one recorded (a button on an older card) changes nothing: a look never
    moves back. ValueError, and nothing stored, when `at` is not a time."""
    at = _time(at)
    before = payload.get("looked_at")
    if before is None or not _later(before, at):
        payload["looked_at"] = at


def owner(kind: str, passengers: Sequence[str], payload: Mapping, book: PassengerBook) -> str:
    """Whether the owner is on the leg: `on`, `off` or `unknown`. Only a flight can be the
    owner's, and the owner's answer "no" settles it; otherwise the names decide, and a leg that
    names nobody of two words or more is `unknown`."""
    if kind != "flight" or payload.get("owner_answer") == "no":
        return "off"
    found = book.classify(list(passengers))
    if found.me_present:
        return "on"
    if not (found.guests or found.others):
        return "unknown"
    return "off"


def still_listed(owner: str, told: Mapping | None) -> bool:
    """Whether the flight log holds, as the owner's, a flight the owner is not on: the owner is
    off the leg (`owner(...)`), and what the flight log was last told names the owner among the
    flight's people."""
    if owner != "off" or not isinstance(told, Mapping):
        return False
    people = told.get("people")
    return isinstance(people, list) and any(
        isinstance(who, Mapping) and who.get("who") == "me" for who in people
    )


def open_checks(
    item: Item,
    payload: Mapping,
    *,
    owner: str,
    in_reconcile: bool,
    sync_error: str | None = None,
    sync_error_at: str | None = None,
    told: Mapping | None = None,
) -> tuple[str, ...]:
    """The checks open on a booking, in ALL's order. `item` is the booking rebuilt from `payload`
    as it stands, `owner` is `owner(...)` for it, and `in_reconcile` says that the flight is in
    the reconcile: a flight log is on, the flight is not held out of it, and it is not a
    cancelled flight that was never sent (the caller works it out; it is false for anything but a
    flight). `sync_error` and `sync_error_at` are why and when the last sync attempt did not
    finish; `told` is what the flight log was last told of the flight.

    Five checks exist only because a flight log is kept, and follow the reconcile: nobody named
    (`owner_unknown`), the unverified sender, an airport the flight log does not know
    (`airtrail_airport`), a write it refused (`airtrail_refused`) and a flight it still lists as
    the owner's (STILL_LISTS). Out of the reconcile they are shut whatever the payload or the
    last attempt says, since nothing about the flight log can be asked or cleared then; their
    stamps and errors stay, so they open again by themselves when the flight is back in it.

    - Every hard code the booking shows is open; and `airtrail_airport` in the reconcile when the
      flight log did not know an airport, unless the owner is off the flight: nothing is sent
      for it then, so no pass asks again and nothing could close it.
    - `owner_unknown` is open exactly while the flight is in the reconcile and names nobody:
      looking never closes it, an answer or a name does.
    - Any other soft code the booking raises is open while the booking still shows it and it was
      stamped after the owner last looked (or the owner never looked).
    - A soft code an email raised is open when it was stamped after the owner last looked; the
      unverified sender only on a flight in the reconcile that the owner is on, since it guards
      the flight log alone.
    - `airtrail_refused` is open in the reconcile when the flight log refused the last attempt
      after the owner last looked, unless the owner is off the flight.
    - STILL_LISTS is open in the reconcile while the flight log holds as the owner's a flight
      the owner is off (`still_listed`), from when that began (its stamp) until the owner looks;
      with the owner on the flight again it is shut whatever is stamped."""
    shown = set(codes(item))
    raised = payload.get("raised") or {}
    looked_at = payload.get("looked_at")

    def unseen(at: str | None) -> bool:
        return looked_at is None or (at is not None and _later(at, looked_at))

    out = shown & set(HARD)
    to_send = in_reconcile and owner != "off"
    if to_send and sync_error == "airport":
        out.add("airtrail_airport")
    if in_reconcile and owner == "unknown":
        out.add("owner_unknown")
    for code in FROM_THE_BOOKING:
        if code != "owner_unknown" and code in shown and code in raised and unseen(raised[code]):
            out.add(code)
    for code in FROM_AN_EMAIL:
        if code not in raised or not unseen(raised[code]):
            continue
        if code == "unverified_sender" and not (
            in_reconcile and item.kind == "flight" and owner == "on"
        ):
            continue
        out.add(code)
    if to_send and sync_error == "refused" and unseen(sync_error_at):
        out.add("airtrail_refused")
    if in_reconcile and still_listed(owner, told) and unseen(raised.get(STILL_LISTS)):
        out.add(STILL_LISTS)
    return tuple(code for code in ALL if code in out)


def actions(open: Sequence[str], *, hold: str | None, flight_log: bool) -> frozenset[str]:
    """Which of ACTIONS apply to a booking whose open checks are `open` and whose flight is held
    out of the reconcile for `hold` (`trip_items.sync_hold`): Looks right (`looks_right`) while a
    soft check other than nobody named is open; the owner's answer (`on_it`) while nobody is
    named; Send again (`send_again`) for a flight deleted in the flight log. The last two exist
    only while a flight log is kept (`flight_log`), so neither is offered without one, whatever a
    booking settled before it was switched off holds. A hard check is fixed on the edit form, so
    it offers none of them."""
    out = set()
    if any(code in SOFT and code != "owner_unknown" for code in open):
        out.add("looks_right")
    if flight_log and "owner_unknown" in open:
        out.add("on_it")
    if flight_log and hold == "deleted":
        out.add("send_again")
    return frozenset(out)


def _seats(payload: Mapping, book: PassengerBook) -> dict[str, str]:
    """Each printed seat under "me" for the owner and under a companion's usual name for a
    companion; anyone else's seat is left out."""
    out: dict[str, str] = {}
    for printed, seat in (payload.get("seat_by_passenger") or {}).items():
        match = book.match(printed)
        if match.kind == "me":
            out["me"] = seat
        elif match.kind == "guest":
            out[match.canonical] = seat
    return out


def desired(
    payload: Mapping, passengers: Sequence[str], status: str, book: PassengerBook
) -> dict:
    """What a flight log should hold for the flight, in values that survive a JSON round trip:
    its local date, its UTC times, its flight code, its airport codes, its airline code, the
    people on it and whether it is cancelled. The people are the owner first (as "me") and then
    each companion the book knows, by their usual name and in name order, each with their printed
    seat and the cabin as printed; anyone else is never in it."""
    found = book.classify(list(passengers))
    seats = _seats(payload, book)
    cabin = payload.get("cabin")
    people = [{"who": "me", "seat": seats.get("me"), "class": cabin}] if found.me_present else []
    people += [
        {"who": name, "seat": seats.get(name), "class": cabin} for name in sorted(found.guests)
    ]
    return {
        "date": payload.get("date_local"),
        "departure": payload.get("dep_utc"),
        "arrival": payload.get("arr_utc"),
        "flight": payload.get("flight_code"),
        "from": payload.get("origin"),
        "to": payload.get("destination"),
        "airline": payload.get("carrier"),
        "people": people,
        "cancelled": status == "cancelled",
    }


def sync_state(
    *,
    kind: str,
    airtrail_on: bool,
    hold: str | None,
    owner: str,
    cancelled: bool,
    told: dict | None,
    open: Sequence[str],
    desired: dict,
) -> str:
    """Where a booking's sync with the flight log stands, by the first rule that holds: `off` for
    anything but a flight, or with no flight log (only `airtrail_on is True` is on); `off` for a
    flight held out of the reconcile, and for a cancelled flight that was never sent, since no
    answer could send it; `blocked` while nobody is named; `off` for a flight the owner is not
    on; `blocked` while any check is open; `synced` when the flight log was told what it should
    hold; else `pending`."""
    if kind != "flight" or airtrail_on is not True:
        return "off"
    if hold is not None:
        return "off"
    if cancelled and told is None:
        return "off"
    if owner == "unknown":
        return "blocked"
    if owner != "on":
        return "off"
    if open:
        return "blocked"
    if desired == told:
        return "synced"
    return "pending"
