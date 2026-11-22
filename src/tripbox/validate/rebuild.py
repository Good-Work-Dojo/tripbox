# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Rebuild a booking from its stored fields and validate it as a fresh parse would."""

from __future__ import annotations

from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.util import parse_iso
from tripbox.validate.keys import is_draft
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import Item, validate_extraction


class NothingToFile(ValueError):
    """The fields validate to no booking at all."""


def reservation_from_payload(kind: str, p: dict) -> Reservation:
    """Rebuild the parser output from a stored item payload so edits can be re-validated."""
    status = p.get("status") or "unknown"
    if kind == "flight":
        seg = Segment(
            carrier_iata=p.get("carrier"),
            flight_number=p.get("flight_number"),
            origin=p.get("origin"),
            destination=p.get("destination"),
            departure_local=p.get("dep_local"),
            arrival_local=p.get("arr_local"),
            # as printed beside each time, so the rebuilt instants are the ones first filed
            departure_offset=p.get("dep_offset"),
            arrival_offset=p.get("arr_offset"),
            departure_tz=p.get("dep_zone"),
            arrival_tz=p.get("arr_zone"),
            cabin=p.get("cabin"),
            seats=list(p.get("seats") or []),
            seat_by_passenger=dict(p.get("seat_by_passenger") or {}),
        )
        return Reservation(
            kind="flight",
            status=status,
            confirmation_code=p.get("pnr"),
            provider=p.get("provider"),
            passengers=list(p.get("passengers") or []),
            price=p.get("price"),
            currency=p.get("currency"),
            segments=[seg],
        )
    if kind == "lodging":
        return Reservation(
            kind="lodging",
            status=status,
            confirmation_code=p.get("confirmation_code"),
            provider=p.get("provider"),
            property_name=p.get("property_name"),
            address=p.get("address"),
            check_in=p.get("check_in"),
            check_out=p.get("check_out"),
            passengers=list(p.get("passengers") or []),
            price=p.get("price"),
            currency=p.get("currency"),
            notes=p.get("notes"),
        )
    if kind == "car":
        return Reservation(
            kind="car",
            status=status,
            confirmation_code=p.get("confirmation_code"),
            provider=p.get("provider"),
            pickup_location=p.get("pickup_location"),
            pickup_time=p.get("pickup_time"),
            dropoff_location=p.get("dropoff_location"),
            dropoff_time=p.get("dropoff_time"),
            passengers=list(p.get("passengers") or []),
            price=p.get("price"),
            currency=p.get("currency"),
            notes=p.get("notes"),
        )
    seg = Segment(
        train_number=p.get("train_number"),
        operator=p.get("provider"),
        origin=p.get("origin"),
        destination=p.get("destination"),
        departure_local=p.get("dep_local"),
        arrival_local=p.get("arr_local"),
        seats=list(p.get("seats") or []),
    )
    return Reservation(
        kind="train",
        status=status,
        confirmation_code=p.get("confirmation_code"),
        provider=p.get("provider"),
        passengers=list(p.get("passengers") or []),
        price=p.get("price"),
        currency=p.get("currency"),
        segments=[seg],
    )


def rebuild_item(
    kind: str,
    payload: dict,
    *,
    refdata: Airports,
    book: PassengerBook,
    key: str | None = None,
) -> Item:
    """Re-validate a stored (possibly edited) payload exactly like a fresh parse would: the
    booking's fields become a reservation of full confidence, and the message it came from
    (`message_id`, `email_date`, `subject`, `from`, `message_hash`) is carried over from the
    payload. The rebuilt booking's key is its real key when its fields make one; else `key`, the
    key it is filed under, so a booking still missing a part keeps the draft it has; else the
    draft key of its email's first place."""
    res = reservation_from_payload(kind, payload)
    ex = Extraction(document_type="booking", reservations=[res], confidence=1.0)
    source = {
        k: payload.get(k)
        for k in ("message_id", "email_date", "subject", "from", "message_hash")
        if k in payload
    }
    email_date = parse_iso(payload["email_date"]) if payload.get("email_date") else None
    items = validate_extraction(
        ex,
        refdata=refdata,
        book=book,
        email_date=email_date,
        source=source,
    )
    if not items:
        raise NothingToFile("nothing left to write after re-validation")
    item = items[0]
    if key and is_draft(item.key):  # no real key: the validator gave its email's first draft
        item.key = key
    return item
