# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Turn an Extraction into Items ready to file, each with the Problems found in it."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tripbox import lists
from tripbox.parse.models import READ_BY_MODEL, Extraction, Reservation
from tripbox.validate.keys import draft_key, real_key
from tripbox.validate.passengers import Classified, PassengerBook, name_tokens

__all__ = ["Item", "Problem", "name_tokens", "parse_local", "validate_extraction"]
from tripbox.validate.refdata import Airports

IATA_AIRPORT = re.compile(r"^[A-Z]{3}$")
CARRIER = re.compile(r"^[A-Z0-9]{2}$")
FLIGHT_NO = re.compile(r"^\d{1,4}$")
# Anchored: anything that is not a plain local date-time falls through to the printed formats
# (so "2027-06-22 6:00 AM" is never read as midnight). A trailing Z/offset is tolerated and
# ignored here; the offset is carried separately by the segment.
LOCAL = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?)?"
    r"\s*(?:Z|[+-]\d{2}:?\d{2})?$"
)
PAST_SLACK = dt.timedelta(days=2)
FUTURE_LIMIT = dt.timedelta(days=400)
MAX_SEGMENT = dt.timedelta(hours=20)
# More bookings than one email holds: a model's answer that yields more is not believed, and
# every booking of it is marked `low_confidence`, so none reaches a flight log before a look.
MAX_BOOKINGS = 20
# The zone and the UTC offset an email prints beside a flight's departure and arrival, kept in the
# payload as printed (absent when not printed) so that the booking rebuilt from its stored fields
# reads each local time exactly as it was first read. Each belongs to its end's local time and
# airport, and goes when either changes without it: end → (local time, airport, printed keys).
PRINTED_ZONES = {
    "dep": ("dep_local", "origin", ("dep_offset", "dep_zone")),
    "arr": ("arr_local", "destination", ("arr_offset", "arr_zone")),
}


def carry_printed_zones(merged: dict, old: dict, new: dict) -> None:
    """After `merged = merge_payload(old, new)`: each end's printed zone and offset are those of
    the payload its local time came from — `new`'s when `new` prints that time, else `old`'s while
    the end's airport is still `old`'s, else none."""
    for time_field, airport, keys in PRINTED_ZONES.values():
        if new.get(time_field) not in (None, ""):
            source = new
        elif merged.get(airport) == old.get(airport):
            source = old
        else:
            source = {}
        for key in keys:
            if source.get(key):
                merged[key] = source[key]
            else:
                merged.pop(key, None)


def drop_printed_zones(payload: dict, *ends: str) -> None:
    """Forget what was printed beside these ends ("dep", "arr"): a time or an airport changed by
    hand is read in the airport's zone."""
    for end in ends:
        for key in PRINTED_ZONES[end][2]:
            payload.pop(key, None)


def _shipped_name(carrier: str) -> str | None:
    airline = lists.airlines().get(carrier)
    return airline.name if airline else None


# Airlines whose confirmations print the name but not the IATA code (JetBlue, Southwest ...). The
# name is on the document, so resolving it to the code here is a lookup, not an inference by the
# model. Longest name wins ("air france" before "air"); anything else leaves the carrier unread,
# a check on the filed flight. The words come from the airline list (tripbox.lists).
def carrier_from_name(provider: str | None) -> str | None:
    if not provider:
        return None
    words = " ".join(re.sub(r"[^a-z ]", " ", provider.lower()).split())
    for name, code in lists.airline_words():
        if re.search(rf"\b{re.escape(name)}\b", words):
            return code
    return None


# Some receipts print only city names next to each flight and the airport codes elsewhere, as a
# pair in a baggage table ("MSP-ORD"); a model reading one may return the cities. A code pair
# printed in the same email whose airports' cities match both ends of the leg is a lookup, like
# the airline words. No fit, or more than one (Chicago: ORD and MDW) -> the airport stays
# unread, a check on the filed leg.
CODE_PAIR = re.compile(r"\b([A-Z]{3})\s{0,3}(?:-|–|—|→)\s{0,3}([A-Z]{3})\b")


def _city_tokens(s: str | None) -> list[str]:
    ascii_ = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", ascii_.lower())


def city_matches(printed: str, airport: dict | None) -> bool:
    """The printed city starts with the airport's whole municipality: a city printed with more
    after it ("CHICAGO-OHARE") fits Chicago, a municipality given with its region ("City,
    Region") is its city alone, and the first word of a two-word city does not fit it."""
    have = _city_tokens(str((airport or {}).get("municipality") or "").split(",")[0])
    want = _city_tokens(printed)
    return bool(have) and want[: len(have)] == have


def printed_route(
    origin: str, dest: str, text: str | None, refdata: Airports
) -> tuple[str, str] | None:
    if not (origin and dest and text) or (IATA_AIRPORT.match(origin) and IATA_AIRPORT.match(dest)):
        return None

    def fits(printed: str, code: str) -> bool:
        if IATA_AIRPORT.match(printed):
            return printed == code
        return city_matches(printed, refdata.airport(code))

    hits = {(o, d) for o, d in CODE_PAIR.findall(text) if fits(origin, o) and fits(dest, d)}
    return hits.pop() if len(hits) == 1 else None


@dataclass(frozen=True)
class Problem:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class Item:
    kind: str  # flight | lodging | car | train
    key: str  # its real key (validate.keys.real_key), else a draft key from its email
    payload: dict
    pnr: str | None
    passengers: list[str]  # names as printed
    classified: Classified
    status: str  # confirmed | changed | cancelled | unknown
    confidence: float
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


_PRINTED_FORMATS = (
    "%a %b %d %Y %I:%M %p",  # Thu Mar 13 2031 9:40 AM
    "%a, %b %d, %Y %I:%M %p",  # Thu, Mar 13, 2031 9:40 AM
    "%b %d, %Y %I:%M %p",
    "%b %d %Y %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %I:%M %p",
    "%a %b %d %Y",
    "%b %d, %Y",
)


def _read_local(value: str | None) -> tuple[dt.datetime, bool] | None:
    """`value` read as parse_local reads it, and whether it carries a time of day. That is told
    from what was printed — a time part, in any of the forms read — never from the time read, so
    a time printed as 00:00 is a time and a day alone (a printed zone or offset beside it
    included) is not."""
    if not value:
        return None
    v = value.strip()
    m = LOCAL.match(v)
    if m:
        hh, mm = (m.group(2) or "0").zfill(2), m.group(3) or "00"
        try:
            return dt.datetime.fromisoformat(f"{m.group(1)}T{hh}:{mm}"), m.group(2) is not None
        except ValueError:  # 2027-02-29, month 13: not a date, so not a usable time
            return None
    v = re.sub(r"\s+", " ", v.replace(",", ", ").replace(" ,", ","))
    v = re.sub(r"\s+,", ",", v)
    for fmt in _PRINTED_FORMATS:
        try:
            return dt.datetime.strptime(v, fmt), "%M" in fmt
        except ValueError:
            continue
    return None


def parse_local(value: str | None) -> dt.datetime | None:
    """'YYYY-MM-DD[THH:MM]' (the contract) plus a few printed formats the model may echo. A day
    alone reads as its midnight here; a caller that must not invent a time asks day_alone."""
    read = _read_local(value)
    return read[0] if read else None


def day_alone(value: str | None) -> str | None:
    """The day `value` prints, as 'YYYY-MM-DD', when it prints a day and no time of day; else
    None (a time is printed, or it is no date)."""
    read = _read_local(value)
    return read[0].strftime("%Y-%m-%d") if read and not read[1] else None


def without_days_alone(held: Mapping, new: dict) -> dict:
    """What the payload `new`, read from an email for the booking whose stored payload is `held`,
    says about it: `new` without each local time of a leg's end (`dep_local`, `arr_local`) that
    it prints as a day alone on the day of a time `held` holds for that end. A day printed alone
    says nothing about the time, so for every merge and every comparison such an email does not
    print that field, and the held time stays with what it was read by. A day alone on another
    day, or where `held` holds no time, is what the email says and is kept."""
    out = new
    for end in ("dep_local", "arr_local"):
        said, kept = new.get(end), held.get(end)
        if not (isinstance(said, str) and isinstance(kept, str)):
            continue
        read = _read_local(kept)
        if read and read[1] and day_alone(said) == read[0].strftime("%Y-%m-%d"):
            out = {k: v for k, v in out.items() if k != end}
    return out


# The start and the end of each kind, which a hard check reads, with what the validator derives
# from each alone.
_TIMES = {
    "flight": {"dep_local": ("date_local",), "arr_local": ()},
    "train": {"dep_local": (), "arr_local": ()},
    "lodging": {"check_in": (), "check_out": ()},
    "car": {"pickup_time": (), "dropoff_time": ()},
}
# Everything a flight's email says of its two ends together.
_FLIGHT_ENDS = ("dep_local", "arr_local", "dep_utc", "arr_utc", "date_local", "duration_min")


def _inverted(kind: str, p: Mapping) -> bool:
    """Whether the payload `p` prints its end before its start, as the validator finds it: a
    flight's arrival before its departure (instants), a stay's check-out before its check-in or
    a car's drop-off before its pick-up (days)."""
    if kind == "flight":
        dep, arr = p.get("dep_utc"), p.get("arr_utc")
        if not (isinstance(dep, str) and isinstance(arr, str)):
            return False
        try:
            return dt.datetime.fromisoformat(arr) < dt.datetime.fromisoformat(dep)
        except ValueError:
            return False
    if kind not in ("lodging", "car"):
        return False
    start, end = (p.get(name) for name in _TIMES[kind])
    begins = parse_local(start) if isinstance(start, str) else None
    ends = parse_local(end) if isinstance(end, str) else None
    return begins is not None and ends is not None and ends.date() < begins.date()


def without_unreadable(kind: str, new: dict, airports: Airports) -> dict:
    """What the payload `new`, read from an email for a booking the store holds, says about it:
    `new` without each value that fails its hard check — a carrier code or a flight number that
    is not one, an airport code `airports` does not know (a city where a code belongs), a time
    that does not read as a date — and what the validator derives from that value alone; and,
    when the email prints a booking's end before its start (an overnight arrival without its
    next day, a check-out before the check-in), without either end. Such a value says nothing,
    so the booking keeps what it holds; one the email does not print is left to the merge,
    which never takes a blank."""

    def unread(name: str, ok: Callable[[str], object]) -> bool:
        value = new.get(name)
        return isinstance(value, str) and value.strip() != "" and not ok(value)

    def known(code: str) -> bool:
        code = code.upper()
        return bool(IATA_AIRPORT.match(code)) and airports.airport(code) is not None

    gone: set[str] = set()
    for name, derived in _TIMES.get(kind, {}).items():
        if unread(name, parse_local):
            gone |= {name, *derived}
    if kind == "flight":
        if unread("carrier", lambda v: CARRIER.match(v.upper())):
            gone |= {"carrier", "flight_code"}
        if unread("flight_number", lambda v: FLIGHT_NO.match(v.strip())):
            gone |= {"flight_number", "flight_code"}
        gone |= {end for end in ("origin", "destination") if unread(end, known)}
    if _inverted(kind, new):
        gone |= set(_FLIGHT_ENDS if kind == "flight" else _TIMES[kind])
    return {k: v for k, v in new.items() if k not in gone} if gone else new


def localize(
    naive: dt.datetime | None, offset: str | None, tz: str | None, fallback_tz: str | None
) -> dt.datetime | None:
    """Attach a zone: printed IANA zone > printed offset > the airport's zone. None if unknown."""
    if naive is None:
        return None

    def zone(name: str | None) -> dt.datetime | None:
        if not name:
            return None
        try:
            return naive.replace(tzinfo=ZoneInfo(name))
        except ZoneInfoNotFoundError:
            return None

    if (printed := zone(tz)) is not None:
        return printed
    if offset:
        sign = -1 if offset.startswith("-") else 1
        h, m = offset.lstrip("+-").split(":")
        return naive.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=int(h), minutes=int(m))))
    return zone(fallback_tz)


def to_utc_iso(d: dt.datetime) -> str:
    return d.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plausible(dep: dt.datetime, email_date: dt.datetime | None) -> Problem | None:
    if email_date is None:
        return None
    if dep < email_date - PAST_SLACK:
        return Problem(
            "date_past", f"departure {dep.isoformat()} is before the email ({email_date.date()})"
        )
    if dep > email_date + FUTURE_LIMIT:
        return Problem("date_far", f"departure {dep.isoformat()} is >400 days after the email")
    return None


def _norm(s: str | None) -> str:
    """Its letters and digits alone, in lower case: how a key holds a name, place or station."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def validate_flight(
    res: Reservation,
    refdata: Airports,
    book: PassengerBook,
    email_date: dt.datetime | None,
    confidence: float,
    source: dict,
    text: str | None = None,
) -> list[Item]:
    items: list[Item] = []
    for seg in res.segments:
        # Classified per leg (and a fresh object per leg): kitinerary lists who is on each flight,
        # and the planner merges leg items in place — a shared object would leak the owner onto a
        # leg the owner is not booked on.
        names = list(seg.passengers or res.passengers)
        cls = book.classify(names)
        status = seg.status or res.status
        problems: list[Problem] = []
        carrier = (
            seg.carrier_iata or carrier_from_name(res.provider or seg.operator) or ""
        ).upper()
        number = (seg.flight_number or "").strip()
        origin = (seg.origin or "").upper()
        dest = (seg.destination or "").upper()
        if pair := printed_route(origin, dest, text, refdata):
            origin, dest = pair
        if not CARRIER.match(carrier):
            problems.append(Problem("carrier", f"bad carrier code {seg.carrier_iata!r}"))
        if not FLIGHT_NO.match(number):
            problems.append(Problem("flight_number", f"bad flight number {seg.flight_number!r}"))
        for label, code in (("origin", origin), ("destination", dest)):
            if not IATA_AIRPORT.match(code):
                problems.append(Problem(label, f"bad airport code {code!r}"))
        from_ap = refdata.airport(origin) if IATA_AIRPORT.match(origin) else None
        to_ap = refdata.airport(dest) if IATA_AIRPORT.match(dest) else None
        if IATA_AIRPORT.match(origin) and from_ap is None:
            problems.append(Problem("origin", f"unknown airport {origin}"))
        if IATA_AIRPORT.match(dest) and to_ap is None:
            problems.append(Problem("destination", f"unknown airport {dest}"))
        # A time printed as a day alone is kept as that day, never as its midnight: it has no
        # instant, no order or length is worked out from it, and a departure printed so is the
        # finding `departure`.
        dep_day, arr_day = day_alone(seg.departure_local), day_alone(seg.arrival_local)
        naive_dep = None if dep_day else parse_local(seg.departure_local)
        dep = localize(
            naive_dep,
            seg.departure_offset,
            seg.departure_tz,
            (from_ap or {}).get("timezone"),
        )
        arr = localize(
            None if arr_day else parse_local(seg.arrival_local),
            seg.arrival_offset,
            seg.arrival_tz,
            (to_ap or {}).get("timezone"),
        )
        if dep_day:
            problems.append(Problem("departure", "no departure time"))
        elif dep is None and naive_dep is not None and from_ap is None:
            # the printed time is fine; it is the unresolved airport that has no zone
            problems.append(
                Problem(
                    "departure",
                    f"time zone unknown until the origin airport is fixed ({naive_dep:%H:%M})",
                )
            )
        elif dep is None and naive_dep is not None:
            # the airport is known, but neither its table row nor the email gives a zone
            problems.append(
                Problem(
                    "departure",
                    f"no time zone known for {origin} and the email prints none"
                    f" ({naive_dep:%H:%M})",
                )
            )
        elif dep is None:
            problems.append(
                Problem("departure", f"no usable departure time ({seg.departure_local!r})")
            )
        else:
            p = plausible(dep, email_date)
            if p:
                problems.append(p)
            if arr is not None:
                if arr < dep:
                    problems.append(Problem("arrival_before_departure", "arrival before departure"))
                elif arr - dep > MAX_SEGMENT:
                    problems.append(Problem("long_leg", f"segment longer than {MAX_SEGMENT}"))
        if not (cls.me_present or cls.guests or cls.others):
            problems.append(Problem("owner_unknown", "no passenger names found"))
        dep_local = dep.strftime("%Y-%m-%dT%H:%M") if dep else (dep_day or seg.departure_local)
        arr_local = arr.strftime("%Y-%m-%dT%H:%M") if arr else (arr_day or seg.arrival_local)
        date_local = dep.strftime("%Y-%m-%d") if dep else (dep_local or "")[:10]
        payload = {
            "carrier": carrier,
            "flight_number": number,
            "flight_code": f"{carrier}{int(number) if number.isdigit() else number}",
            "origin": origin,
            "destination": dest,
            "airline_name": res.provider or _shipped_name(carrier),
            "origin_name": (from_ap or {}).get("name"),
            "origin_city": (from_ap or {}).get("municipality"),
            "origin_tz": (from_ap or {}).get("timezone"),
            "destination_name": (to_ap or {}).get("name"),
            "destination_city": (to_ap or {}).get("municipality"),
            "destination_tz": (to_ap or {}).get("timezone"),
            "dep_local": dep_local,
            "arr_local": arr_local,
            "dep_utc": to_utc_iso(dep) if dep else None,
            "arr_utc": to_utc_iso(arr) if arr else None,
            "date_local": date_local,
            "duration_min": int((arr - dep).total_seconds() // 60) if dep and arr else None,
            "cabin": seg.cabin,
            "seats": list(seg.seats),
            "seat_by_passenger": dict(seg.seat_by_passenger),
            "pnr": res.confirmation_code,
            "provider": res.provider,
            "status": status,
            "passengers": names,
            "price": res.price,
            "currency": res.currency,
            **source,
        }
        if pair:
            payload["airports_from_text"] = f"{origin}-{dest}"
        for key, value in (
            ("dep_offset", seg.departure_offset),
            ("dep_zone", seg.departure_tz),
            ("arr_offset", seg.arrival_offset),
            ("arr_zone", seg.arrival_tz),
        ):
            if value:  # what localize read the time with, as printed
                payload[key] = value
        item = Item(
            kind="flight",
            key="",  # validate_extraction keys every item
            payload=payload,
            pnr=res.confirmation_code,
            passengers=list(names),
            classified=cls,
            status=status,
            confidence=confidence,
            problems=problems,
        )
        items.append(item)
    return items


def _dated(
    value: str | None,
    label: str,
    email_date: dt.datetime | None,
    problems: list[Problem],
    required: bool = True,
) -> dt.date | None:
    d = parse_local(value)
    if d is None:
        if required:
            problems.append(Problem(label, f"missing or unparseable {label} ({value!r})"))
        return None
    if email_date is not None:
        if d.date() < (email_date - PAST_SLACK).date():
            problems.append(Problem("date_past", f"{label} {d.date()} is before the email"))
        elif d.date() > (email_date + FUTURE_LIMIT).date():
            problems.append(Problem("date_far", f"{label} {d.date()} is >400 days after the email"))
    return d.date()


def validate_lodging(res: Reservation, book: PassengerBook, email_date, confidence, source) -> Item:
    problems: list[Problem] = []
    if not res.property_name:
        problems.append(Problem("missing_name", "no property name"))
    ci = _dated(res.check_in, "check_in", email_date, problems)
    co = _dated(res.check_out, "check_out", email_date, problems)
    if ci and co and co < ci:
        problems.append(Problem("check_out", "check-out before check-in"))
    cls = book.classify(res.passengers)
    payload = {
        "provider": res.provider,
        "property_name": res.property_name,
        "address": res.address,
        "check_in": res.check_in,
        "check_out": res.check_out,
        "confirmation_code": res.confirmation_code,
        "price": res.price,
        "currency": res.currency,
        "passengers": list(res.passengers),
        "status": res.status,
        "notes": res.notes,
        **source,
    }
    return Item(
        kind="lodging",
        key="",  # validate_extraction keys every item
        payload=payload,
        pnr=res.confirmation_code,
        passengers=list(res.passengers),
        classified=cls,
        status=res.status,
        confidence=confidence,
        problems=problems,
    )


def validate_car(
    res: Reservation,
    book: PassengerBook,
    email_date,
    confidence,
    source,
    refdata: Airports | None = None,
) -> Item:
    problems: list[Problem] = []
    if not res.provider:
        problems.append(Problem("missing_name", "no rental company"))
    # an airport's code as a pick-up location names the airport; the trip is titled after its city
    pickup_city = None
    code = (res.pickup_location or "").strip().upper()
    if refdata is not None and IATA_AIRPORT.match(code):
        pickup_city = (refdata.airport(code) or {}).get("municipality")
    pu = _dated(res.pickup_time, "pickup_time", email_date, problems)
    do = _dated(res.dropoff_time, "dropoff_time", email_date, problems)
    if pu and do and do < pu:
        problems.append(Problem("dropoff_time", "drop-off before pick-up"))
    cls = book.classify(res.passengers)
    payload = {
        "provider": res.provider,
        "pickup_location": res.pickup_location,
        "pickup_city": pickup_city,
        "pickup_time": res.pickup_time,
        "dropoff_location": res.dropoff_location,
        "dropoff_time": res.dropoff_time,
        "confirmation_code": res.confirmation_code,
        "price": res.price,
        "currency": res.currency,
        "passengers": list(res.passengers),
        "status": res.status,
        "notes": res.notes,
        **source,
    }
    return Item(
        kind="car",
        key="",  # validate_extraction keys every item
        payload=payload,
        pnr=res.confirmation_code,
        passengers=list(res.passengers),
        classified=cls,
        status=res.status,
        confidence=confidence,
        problems=problems,
    )


def validate_train(
    res: Reservation, book: PassengerBook, email_date, confidence, source
) -> list[Item]:
    items: list[Item] = []
    for seg in res.segments:
        names = list(seg.passengers or res.passengers)
        cls = book.classify(names)
        status = seg.status or res.status
        problems: list[Problem] = []
        if not seg.origin or not seg.destination:
            problems.append(Problem("stations", "missing origin or destination station"))
        _dated(seg.departure_local, "departure", email_date, problems)
        payload = {
            "provider": res.provider or seg.operator,
            "train_number": seg.train_number,
            "origin": seg.origin,
            "destination": seg.destination,
            "dep_local": seg.departure_local,
            "arr_local": seg.arrival_local,
            "seats": list(seg.seats),
            "confirmation_code": res.confirmation_code,
            "price": res.price,
            "currency": res.currency,
            "passengers": names,
            "status": status,
            **source,
        }
        item = Item(
            kind="train",
            key="",  # validate_extraction keys every item
            payload=payload,
            pnr=res.confirmation_code,
            passengers=list(names),
            classified=cls,
            status=status,
            confidence=confidence,
            problems=problems,
        )
        items.append(item)
    return items


def validate_extraction(
    ex: Extraction,
    *,
    refdata: Airports,
    book: PassengerBook,
    email_date: dt.datetime | None,
    source: dict | None = None,
    min_confidence: float = 0.8,
    parser: str = "kitinerary",
    trusted_sender: bool = True,
    text: str | None = None,
) -> list[Item]:
    """`text` is the email body: a flight whose airports came back as city names is resolved
    through a code pair printed elsewhere in it (`printed_route`). What is found says nothing of
    a flight log: whether one is kept, and whose flight a finding holds back from it, is worked
    out where the booking is filed (`checks.open_checks`).

    `source` names the message and is copied into every payload; its `message_hash` is the
    SHA-256 hex digest of the message's bytes. A direct call that gives none stands the digest of
    the message's Message-ID (else its date) in for it."""
    source = dict(source or {})
    if not source.get("message_hash"):
        tag = source.get("message_id") or source.get("email_date") or "?"
        source["message_hash"] = hashlib.sha256(str(tag).encode("utf-8")).hexdigest()
    items: list[Item] = []
    for res in ex.reservations:
        if res.kind == "flight":
            items.extend(
                validate_flight(res, refdata, book, email_date, ex.confidence, source, text)
            )
        elif res.kind == "lodging":
            items.append(validate_lodging(res, book, email_date, ex.confidence, source))
        elif res.kind == "car":
            items.append(validate_car(res, book, email_date, ex.confidence, source, refdata))
        elif res.kind == "train":
            items.extend(validate_train(res, book, email_date, ex.confidence, source))
    # A booking is keyed by the parts it really has; one that lacks a part gets a draft key from
    # its email and its place among the email's bookings, so two such bookings never share a key
    # and none takes the key a later, complete receipt files under.
    for n, it in enumerate(items):
        it.key = real_key(it.kind, it.payload, airports=refdata) or draft_key(
            source["message_hash"], n
        )
    if parser == READ_BY_MODEL and ex.confidence < min_confidence:
        doubt = f"model confidence {ex.confidence:.2f} < {min_confidence}"
    elif parser == READ_BY_MODEL and len(items) > MAX_BOOKINGS:
        doubt = f"the model's answer yields {len(items)} bookings, more than {MAX_BOOKINGS}"
    else:
        doubt = None
    if doubt is not None:
        for it in items:
            it.problems.append(Problem("low_confidence", doubt))
    if not trusted_sender:
        # a filter-forwarded email from a sender nobody vouched for: every flight in it carries
        # the finding, whoever it names. A flight that names nobody may yet prove to be the
        # owner's (its names taken from the booking it changes, or given later), and crafted
        # mail must not reach a flight log that way either.
        for it in items:
            if it.kind == "flight":
                it.problems.append(
                    Problem(
                        "unverified_sender",
                        f"auto-forwarded from {source.get('from') or '?'}, not a known travel"
                        " sender",
                    )
                )
    return items
