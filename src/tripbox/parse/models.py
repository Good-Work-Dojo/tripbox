# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The one extraction shape both parsers produce (kitinerary mapping and the wire schema).

Every field is optional on purpose: anything the document does not print stays None. Flat rather
than a union of reservation types so the JSON schema stays simple for structured outputs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DocumentType = Literal[
    "booking",
    "change",
    "cancellation",
    "receipt_only",
    "check_in_or_boarding",
    "marketing",
    "other",
]
ReservationKind = Literal["flight", "lodging", "car", "train", "other"]
Status = Literal["confirmed", "changed", "cancelled", "unknown"]


class Segment(BaseModel):
    """One flight or train leg. Times are local wall-clock strings exactly as printed."""

    carrier_iata: str | None = Field(None, description="2-letter marketing carrier code, e.g. DL")
    flight_number: str | None = Field(None, description="digits only, e.g. 1234")
    origin: str | None = Field(
        None, description="IATA airport code (flights) or station name (trains)"
    )
    destination: str | None = None
    departure_local: str | None = Field(
        None, description="YYYY-MM-DDTHH:MM local at origin, as printed"
    )
    arrival_local: str | None = Field(None, description="YYYY-MM-DDTHH:MM local at destination")
    departure_offset: str | None = Field(None, description="UTC offset like -05:00 only if printed")
    arrival_offset: str | None = None
    departure_tz: str | None = Field(
        None, description="IANA zone only if known, e.g. America/Chicago"
    )
    arrival_tz: str | None = None
    cabin: str | None = Field(
        None, description="economy / premium economy / business / first as printed"
    )
    seats: list[str] = Field(default_factory=list, description="seat numbers, e.g. 14A")
    seat_by_passenger: dict[str, str] = Field(default_factory=dict)
    train_number: str | None = None
    # Who is on THIS leg and whether it stands, when the source says so per leg (kitinerary emits
    # one object per passenger per flight). Empty/None = inherit the reservation's.
    passengers: list[str] = Field(default_factory=list)
    status: Status | None = None
    operator: str | None = None


class Reservation(BaseModel):
    kind: ReservationKind
    status: Status = "unknown"
    confirmation_code: str | None = Field(
        None, description="PNR / record locator / confirmation number"
    )
    provider: str | None = Field(None, description="Delta Air Lines, Hilton, Hertz, Amtrak ...")
    passengers: list[str] = Field(default_factory=list, description="names exactly as printed")
    price: float | None = None
    currency: str | None = Field(None, description="ISO 4217, e.g. USD")
    segments: list[Segment] = Field(default_factory=list)
    property_name: str | None = None
    address: str | None = None
    check_in: str | None = Field(None, description="YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    check_out: str | None = None
    pickup_location: str | None = None
    pickup_time: str | None = Field(None, description="YYYY-MM-DDTHH:MM")
    dropoff_location: str | None = None
    dropoff_time: str | None = None
    notes: str | None = None


class Extraction(BaseModel):
    document_type: DocumentType
    reservations: list[Reservation] = Field(default_factory=list)
    confidence: float = Field(1.0, ge=0, le=1, description="overall confidence 0..1")
    notes: str | None = None


def is_complete(res: Reservation) -> bool:
    """Deterministic-path completeness: enough to write without the model."""
    if res.kind == "flight":
        return bool(res.segments) and all(
            s.carrier_iata and s.flight_number and s.origin and s.destination and s.departure_local
            for s in res.segments
        )
    if res.kind == "lodging":
        return bool(res.property_name and res.check_in and res.check_out)
    if res.kind == "car":
        return bool(res.provider and res.pickup_time and res.dropoff_time)
    if res.kind == "train":
        return bool(res.segments) and all(
            s.origin and s.destination and s.departure_local for s in res.segments
        )
    return False


# ----------------------------------------------------------------------------- the wire schema
# The parser word stored for a message the model read, whichever provider answered: ledgers and
# reports hold it. It lives here, beside the model's answer shape, because the validator reads it
# and the page, which loads the validator, loads nothing else of the readers' package.
READ_BY_MODEL = "haiku"

# Anthropic structured outputs compile the JSON schema into a grammar and reject large ones
# ("Schema is too complex", seen with the ~40-field Extraction above). The model therefore answers
# in this slim shape (22 fields) which `to_extraction` maps onto Extraction.
class WireLeg(BaseModel):
    carrier: str | None = Field(
        None, description="2-letter IATA airline code (flights) or operator (trains)"
    )
    number: str | None = Field(None, description="flight or train number, digits only")
    origin: str | None = Field(None, description="IATA airport code or station name, as printed")
    destination: str | None = None
    departure: str | None = Field(None, description="YYYY-MM-DDTHH:MM local at origin, as printed")
    arrival: str | None = Field(
        None, description="YYYY-MM-DDTHH:MM local at destination, as printed"
    )
    seats: list[str] = Field(
        default_factory=list, description="'NAME: 14A' when tied to a traveller, else '14A'"
    )
    cabin: str | None = Field(None, description="cabin / fare class as printed, e.g. Main Cabin")


class WireReservation(BaseModel):
    kind: ReservationKind
    status: Status = "unknown"
    code: str | None = Field(None, description="confirmation number / PNR / record locator")
    provider: str | None = Field(
        None, description="airline, hotel brand, rental company, rail operator"
    )
    passengers: list[str] = Field(
        default_factory=list, description="traveller names exactly as printed"
    )
    legs: list[WireLeg] = Field(default_factory=list, description="flight or train legs")
    place: str | None = Field(None, description="hotel name, or rental pick-up location")
    address: str | None = Field(None, description="hotel address, or rental drop-off location")
    start: str | None = Field(
        None, description="check-in or pick-up, YYYY-MM-DD or YYYY-MM-DDTHH:MM"
    )
    end: str | None = Field(None, description="check-out or drop-off")
    price: float | None = None
    currency: str | None = Field(None, description="ISO 4217, e.g. USD")


class WireExtraction(BaseModel):
    document_type: DocumentType
    reservations: list[WireReservation] = Field(default_factory=list)
    confidence: float = Field(1.0, description="overall confidence 0..1")


def wire_shape(model: type[BaseModel] = WireExtraction) -> dict[str, Any]:
    """The shape of an answer in the wire schema: field names, types, choices, defaults and which
    fields are required, with every reference replaced by what it names and every title and
    description dropped. Wording and class names are not part of it."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            node = {**defs[node["$ref"].rsplit("/", 1)[1]], **siblings}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "title", "description"):
                continue  # annotations, not shape
            if key == "properties":
                # The keys are field names and are kept whatever they are called.
                out[key] = {name: walk(sub) for name, sub in value.items()}
            elif key in ("default", "enum", "const", "examples"):
                out[key] = value  # data, not schema
            else:
                out[key] = walk(value)
        return out

    return walk({k: v for k, v in schema.items() if k != "$defs"})


def _split_seats(seats: list[str]) -> tuple[list[str], dict[str, str]]:
    plain: list[str] = []
    by_passenger: dict[str, str] = {}
    for raw in seats:
        if ":" in raw:
            name, seat = raw.rsplit(":", 1)
            name, seat = name.strip(), seat.strip()
            if name and seat:
                by_passenger[name] = seat
                if seat not in plain:
                    plain.append(seat)
                continue
        if raw.strip() and raw.strip() not in plain:
            plain.append(raw.strip())
    return plain, by_passenger


def to_extraction(h: WireExtraction) -> Extraction:
    out: list[Reservation] = []
    for r in h.reservations:
        segments: list[Segment] = []
        for leg in r.legs:
            seats, by_pax = _split_seats(leg.seats)
            if r.kind == "train":
                seg = Segment(
                    train_number=leg.number,
                    operator=leg.carrier,
                    origin=leg.origin,
                    destination=leg.destination,
                    departure_local=leg.departure,
                    arrival_local=leg.arrival,
                    seats=seats,
                    seat_by_passenger=by_pax,
                )
            else:
                seg = Segment(
                    carrier_iata=(leg.carrier or "").upper() or None,
                    flight_number=leg.number,
                    origin=(leg.origin or "").upper() or None,
                    destination=(leg.destination or "").upper() or None,
                    departure_local=leg.departure,
                    arrival_local=leg.arrival,
                    seats=seats,
                    seat_by_passenger=by_pax,
                    cabin=leg.cabin,
                )
            segments.append(seg)
        res = Reservation(
            kind=r.kind,
            status=r.status,
            confirmation_code=r.code,
            provider=r.provider,
            passengers=list(r.passengers),
            price=r.price,
            currency=r.currency,
            segments=segments,
        )
        if r.kind == "lodging":
            res.property_name, res.address, res.check_in, res.check_out = (
                r.place,
                r.address,
                r.start,
                r.end,
            )
        elif r.kind == "car":
            res.pickup_location, res.dropoff_location = r.place, r.address
            res.pickup_time, res.dropoff_time = r.start, r.end
        elif r.kind == "other":
            res.property_name, res.address, res.check_in, res.check_out = (
                r.place,
                r.address,
                r.start,
                r.end,
            )
        out.append(res)
    return Extraction(
        document_type=h.document_type, reservations=out, confidence=max(0.0, min(1.0, h.confidence))
    )
