# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Deterministic extraction: KDE's kitinerary-extractor (schema.org JSON-LD) -> Extraction."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tripbox.log import redact
from tripbox.parse.models import Extraction, Reservation, Segment

log = logging.getLogger("tripbox.kitinerary")

BINARY = "kitinerary-extractor"
TIMEOUT = 90  # seconds the extractor may take over one message
CANCELLED = "ReservationCancelled"
_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


class KitineraryError(RuntimeError):
    pass


def run_extractor(
    data: bytes, suffix: str, context_date: str | None, timeout: int = TIMEOUT
) -> list[dict]:
    """Run the binary on bytes written to a temp file; [] when nothing was recognised."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        cmd = [BINARY]
        if context_date:
            cmd += ["-c", context_date]
        cmd += ["-o", "JsonLd", tmp.name]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise KitineraryError(f"{BINARY} not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise KitineraryError(f"{BINARY} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        err = redact(proc.stderr.decode(errors="replace"))[:300]
        raise KitineraryError(f"{BINARY} exit {proc.returncode}: {err}")
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return []
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise KitineraryError(f"{BINARY} produced non-JSON output: {redact(out)[:120]!r}") from exc
    return parsed if isinstance(parsed, list) else [parsed]


def split_datetime(value: Any) -> tuple[str | None, str | None, str | None]:
    """schema.org datetime (str or QDateTime) -> (local YYYY-MM-DDTHH:MM, offset, IANA tz)."""
    tz = None
    if isinstance(value, dict):
        tz = value.get("timezone")
        value = value.get("@value")
    if not isinstance(value, str):
        return None, None, tz
    m = _DT.match(value.strip())
    if not m:
        return None, None, tz
    offset = m.group(3)
    if offset == "Z":
        offset = "+00:00"
    elif offset and ":" not in offset:
        offset = offset[:3] + ":" + offset[3:]
    return f"{m.group(1)}T{m.group(2)}", offset, tz


def _name(obj: Any) -> str | None:
    if isinstance(obj, dict):
        n = obj.get("name")
        return n.strip() if isinstance(n, str) and n.strip() else None
    if isinstance(obj, str) and obj.strip():
        return obj.strip()
    return None


def _address(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    addr = obj.get("address") if "address" in obj else obj
    if isinstance(addr, str):
        return addr.strip() or None
    if not isinstance(addr, dict):
        return None
    parts = [
        addr.get(k)
        for k in (
            "streetAddress",
            "addressLocality",
            "addressRegion",
            "postalCode",
            "addressCountry",
        )
    ]
    joined = ", ".join(str(p).strip() for p in parts if p)
    return joined or None


def _status(obj: dict) -> str:
    st = str(obj.get("reservationStatus") or "")
    if CANCELLED in st:
        return "cancelled"
    if "ReservationConfirmed" in st:
        return "confirmed"
    return "unknown"


def _price(obj: dict) -> tuple[float | None, str | None]:
    price = obj.get("totalPrice")
    cur = obj.get("priceCurrency")
    try:
        return (float(price) if price not in (None, "") else None), (str(cur) if cur else None)
    except TypeError, ValueError:
        return None, (str(cur) if cur else None)


def _flight_number(raw: Any, carrier: str | None) -> str | None:
    """'B6123', 'G4 123', 'F9 9010', 'DL 1234', 1234 -> the number alone. Extractors pass the
    printed designator through; stripping every non-digit would turn B6123 into 6123."""
    if raw is None:
        return None
    s = str(raw).strip()
    if carrier and s.upper().startswith(carrier.upper()):
        s = s[len(carrier) :]
    m = re.search(r"(\d+)\s*$", s)
    return m.group(1) if m else (re.sub(r"\D", "", s) or None)


def _flight_segment(flight: dict) -> Segment:
    dep_local, dep_off, dep_tz = split_datetime(flight.get("departureTime"))
    arr_local, arr_off, arr_tz = split_datetime(flight.get("arrivalTime"))
    airline = flight.get("airline") or {}
    carrier = (airline.get("iataCode") or None) if isinstance(airline, dict) else None
    return Segment(
        carrier_iata=carrier,
        flight_number=_flight_number(flight.get("flightNumber"), carrier),
        origin=(flight.get("departureAirport") or {}).get("iataCode") or None,
        destination=(flight.get("arrivalAirport") or {}).get("iataCode") or None,
        departure_local=dep_local,
        arrival_local=arr_local,
        departure_offset=dep_off,
        arrival_offset=arr_off,
        departure_tz=dep_tz,
        arrival_tz=arr_tz,
    )


def _seg_key(s: Segment) -> tuple:
    return (s.carrier_iata, s.flight_number, s.departure_local, s.origin)


def _leg_status(seg: Segment, status: str) -> None:
    """Per-leg status from the per-passenger objects: a cancelled object cancels the leg, a
    confirmed one confirms it unless another object already cancelled it."""
    if status == "cancelled":
        seg.status = "cancelled"
    elif status == "confirmed" and seg.status != "cancelled":
        seg.status = "confirmed"


def _reservation_status(res: Reservation) -> str:
    """A multi-leg reservation is cancelled only when every leg is; one cancelled leg next to
    confirmed ones is a schedule change (old return cancelled, new return confirmed)."""
    legs = [s.status for s in res.segments if s.status]
    if not legs:
        return res.status
    if all(st == "cancelled" for st in legs):
        return "cancelled"
    if any(st == "confirmed" for st in legs):
        return "confirmed"
    return res.status


def map_jsonld(objects: list[dict]) -> Extraction:
    """Group kitinerary's per-passenger objects into reservations keyed by confirmation code."""
    groups: dict[tuple, Reservation] = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        t = obj.get("@type")
        code = obj.get("reservationNumber") or None
        passenger = _name(obj.get("underName"))
        status = _status(obj)
        price, currency = _price(obj)
        res_for = obj.get("reservationFor") or {}
        if t == "FlightReservation":
            seg = _flight_segment(res_for)
            seat = obj.get("airplaneSeat")
            key = ("flight", code or _seg_key(seg))
            res = groups.setdefault(
                key,
                Reservation(
                    kind="flight",
                    confirmation_code=code,
                    status=status,
                    provider=_name(res_for.get("airline")),
                    price=price,
                    currency=currency,
                ),
            )
            existing = next((s for s in res.segments if _seg_key(s) == _seg_key(seg)), None)
            if existing is None:
                res.segments.append(seg)
                existing = seg
            if passenger and passenger not in res.passengers:
                res.passengers.append(passenger)
            if passenger and passenger not in existing.passengers:
                existing.passengers.append(passenger)  # who is on THIS leg
            if isinstance(seat, str) and seat.strip():
                if seat not in existing.seats:
                    existing.seats.append(seat)
                if passenger:
                    existing.seat_by_passenger[passenger] = seat
            _leg_status(existing, status)
        elif t == "LodgingReservation":
            ci, _, _ = split_datetime(obj.get("checkinTime"))
            co, _, _ = split_datetime(obj.get("checkoutTime"))
            key = ("lodging", code or (_name(res_for), ci))
            res = groups.setdefault(
                key,
                Reservation(
                    kind="lodging",
                    confirmation_code=code,
                    status=status,
                    provider=_name(res_for),
                    property_name=_name(res_for),
                    address=_address(res_for),
                    check_in=ci,
                    check_out=co,
                    price=price,
                    currency=currency,
                ),
            )
            if passenger and passenger not in res.passengers:
                res.passengers.append(passenger)
        elif t == "RentalCarReservation":
            pu, _, _ = split_datetime(obj.get("pickupTime"))
            do, _, _ = split_datetime(obj.get("dropoffTime"))
            company = _name((res_for or {}).get("rentalCompany")) or _name(res_for)
            key = ("car", code or (company, pu))
            res = groups.setdefault(
                key,
                Reservation(
                    kind="car",
                    confirmation_code=code,
                    status=status,
                    provider=company,
                    pickup_location=_name(obj.get("pickupLocation"))
                    or _address(obj.get("pickupLocation")),
                    pickup_time=pu,
                    dropoff_location=_name(obj.get("dropoffLocation"))
                    or _address(obj.get("dropoffLocation")),
                    dropoff_time=do,
                    price=price,
                    currency=currency,
                    notes=(_name(res_for) if _name(res_for) != company else None),
                ),
            )
            if passenger and passenger not in res.passengers:
                res.passengers.append(passenger)
        elif t == "TrainReservation":
            dep_local, dep_off, dep_tz = split_datetime(res_for.get("departureTime"))
            arr_local, arr_off, arr_tz = split_datetime(res_for.get("arrivalTime"))
            seg = Segment(
                origin=_name(res_for.get("departureStation")),
                destination=_name(res_for.get("arrivalStation")),
                departure_local=dep_local,
                arrival_local=arr_local,
                departure_offset=dep_off,
                arrival_offset=arr_off,
                departure_tz=dep_tz,
                arrival_tz=arr_tz,
                train_number=str(res_for.get("trainNumber"))
                if res_for.get("trainNumber")
                else None,
                operator=_name(res_for.get("provider")),
            )
            key = ("train", code or (seg.train_number, dep_local))
            res = groups.setdefault(
                key,
                Reservation(
                    kind="train",
                    confirmation_code=code,
                    status=status,
                    provider=_name(res_for.get("provider")),
                    price=price,
                    currency=currency,
                ),
            )
            leg = next(
                (
                    s
                    for s in res.segments
                    if (s.train_number, s.departure_local, s.origin)
                    == (seg.train_number, seg.departure_local, seg.origin)
                ),
                None,
            )
            if leg is None:
                res.segments.append(seg)
                leg = seg
            if passenger and passenger not in res.passengers:
                res.passengers.append(passenger)
            if passenger and passenger not in leg.passengers:
                leg.passengers.append(passenger)
            _leg_status(leg, status)
            ticket = obj.get("reservedTicket") or {}
            seat = (
                ((ticket.get("ticketedSeat") or {}).get("seatNumber"))
                if isinstance(ticket, dict)
                else None
            )
            if seat and str(seat) not in leg.seats:
                leg.seats.append(str(seat))
        else:
            log.debug("ignoring kitinerary object of type %s", t)
    reservations = list(groups.values())
    if not reservations:
        return Extraction(document_type="other", reservations=[], confidence=1.0)
    for res in reservations:
        res.status = _reservation_status(res)
    all_cancelled = all(r.status == "cancelled" for r in reservations)
    return Extraction(
        document_type="cancellation" if all_cancelled else "booking",
        reservations=reservations,
        confidence=1.0,
    )


def extract(data: bytes, *, suffix: str = ".eml", context_date: str | None = None) -> Extraction:
    return map_jsonld(run_extractor(data, suffix, context_date))


def extract_file(path: Path, context_date: str | None = None) -> Extraction:
    return extract(path.read_bytes(), suffix=path.suffix or ".eml", context_date=context_date)
