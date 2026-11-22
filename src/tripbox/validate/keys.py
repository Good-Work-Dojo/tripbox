# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Booking keys: the one rule by which a booking's key is built, whoever builds it (the validator,
the trip store, the backfill). A key is made only of parts the booking really has; a booking that
lacks one has no real key and carries a draft key named after its email instead, so it can never
take the key of the proper booking a later, complete receipt files."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping

from tripbox.validate.refdata import Airports

DRAFT_PREFIX = "draft|"


def segment_key(carrier: str, number: str, date_local: str, origin: str) -> str:
    return f"{carrier.upper()}|{int(number)}|{date_local}|{origin.upper()}"


def draft_key(message_hash: str, n: int) -> str:
    """The key of the booking at place `n` (from 0) among the bookings read from the email whose
    SHA-256 hex digest is `message_hash`: the same for the same email and place, and never one a
    real booking has."""
    return f"{DRAFT_PREFIX}{message_hash[:16]}|{n}"


def own_draft_key(item_id: int) -> str:
    """The draft key of trip item `item_id` itself, which a flight or a train filed under a real
    key takes when its fields stop making one: no email's draft has it (an email's is named after
    the email's own digest), and no other booking does, since an id is never given twice."""
    return draft_key(hashlib.sha256(f"trip item {item_id}".encode()).hexdigest(), 0)


def is_draft(key: str) -> bool:
    return key.startswith(DRAFT_PREFIX)


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    return value if isinstance(value, str) else ""


def _day(payload: Mapping[str, object], field: str) -> str | None:
    """The day a key takes of the field: its first ten characters, when it reads as a date."""
    from tripbox.validate.rules import parse_local  # rules imports this module

    value = _text(payload, field)
    return value[:10] if parse_local(value) is not None else None


def real_key(kind: str, payload: Mapping[str, object], *, airports: Airports) -> str | None:
    """The booking's key from its stored fields, or None when a part the key is made of is
    missing:

    - flight: `CARRIER|NUMBER|YYYY-MM-DD|ORIGIN` — a two-character carrier code, a flight number
      of one to four digits, an origin that is an airport code `airports` knows, and a departure
      (`dep_local`) that reads as a date, whose day it takes;
    - lodging: `lodging|CODE` from the confirmation code; else `lodging|<name>|<day>` from the
      property name and a check-in that reads as a date;
    - car: `car|CODE`; else `car|<provider>|<day>|<pick-up place>` from the provider and a
      pick-up time that reads as a date;
    - train: `train|<number or operator>|<day>|<station>` from the train number (else the
      operator), the departure station and a departure that reads as a date. A confirmation
      code never keys a train.

    A day is the first ten characters of a value that reads as a date (`YYYY-MM-DD` in the form
    the extractors are asked for); names, places and stations are reduced to lower-case letters
    and digits, codes to upper-case ones."""
    # the validator's own patterns and reductions; imported here because rules imports this module
    from tripbox.validate.rules import CARRIER, FLIGHT_NO, IATA_AIRPORT, _norm

    def text(field: str) -> str:
        return _text(payload, field)

    def day(field: str) -> str | None:
        return _day(payload, field)

    if kind == "flight":
        carrier, number = text("carrier").upper(), text("flight_number")
        origin, dep_day = text("origin").upper(), day("dep_local")
        if not (CARRIER.match(carrier) and FLIGHT_NO.match(number) and dep_day):
            return None
        if not IATA_AIRPORT.match(origin) or airports.airport(origin) is None:
            return None
        return segment_key(carrier, number, dep_day, origin)
    # a stay or a car with a code is keyed by the code alone: the model names the provider
    # inconsistently ("Budget", "Budget Rent A Car") and the key must survive that
    if kind == "lodging":
        if code := text("confirmation_code"):
            return f"lodging|{_norm(code).upper()}"
        name, check_in = text("property_name"), day("check_in")
        return f"lodging|{_norm(name)}|{check_in}" if name and check_in else None
    if kind == "car":
        if code := text("confirmation_code"):
            return f"car|{_norm(code).upper()}"
        provider, pickup = text("provider"), day("pickup_time")
        if not (provider and pickup):
            return None
        return f"car|{_norm(provider)}|{pickup}|{_norm(text('pickup_location'))}"
    if kind == "train":
        number, operator = text("train_number"), text("provider")
        station, dep_day = text("origin"), day("dep_local")
        if not ((number or operator) and station and dep_day):
            return None
        return f"train|{number or _norm(operator)}|{dep_day}|{_norm(station)}"
    return None


def same_leg(a: Mapping[str, object], b: Mapping[str, object]) -> bool:
    """Whether two flight legs read from one email say the same thing, so that two copies of a
    leg with no real key (a receipt that lists each ticket, read with a city for its origin) are
    one booking: each prints a carrier code, a flight number, a departure that reads as a date
    and an origin, and the carrier, the number, the departure's date and the origin as printed,
    reduced as a key reduces a place, are equal. A leg without its carrier, its number, its day
    or its origin is the same as no other leg: two legs of one through-flight share the rest."""
    from tripbox.validate.rules import (  # rules imports this module
        CARRIER,
        FLIGHT_NO,
        _norm,
        parse_local,
    )

    def leg(p: Mapping[str, object]) -> tuple[str, int, dt.date, str] | None:
        carrier, number = _text(p, "carrier").upper(), _text(p, "flight_number")
        leaves, origin = parse_local(_text(p, "dep_local")), _norm(_text(p, "origin"))
        if not (CARRIER.match(carrier) and FLIGHT_NO.match(number) and origin) or leaves is None:
            return None
        return carrier, int(number), leaves.date(), origin

    one = leg(a)
    return one is not None and one == leg(b)
