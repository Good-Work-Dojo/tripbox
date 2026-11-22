# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Pure trip logic shared by the executor, the store, the text renderer and the web UI: which
dates an item spans, what a trip is called, which existing trip an item belongs to, how items
sort within a trip and which time zone a hotel or car time is in."""

from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING, Protocol

from tripbox import lists
from tripbox.validate.rules import parse_local

if TYPE_CHECKING:
    from tripbox.trips.store import TripItem

TRIP_SLACK = dt.timedelta(days=2)
NEARBY_DAYS = 10  # an open-jaw return within this many days of a same-place trip extends it
KIND_ORDER = ("flight", "lodging", "car", "train")  # how kinds are listed in counts and chips
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_HAS_TIME = re.compile(r"\d{1,2}:\d{2}")


class HasKindAndPayload(Protocol):
    kind: str
    payload: dict


def _date(value: str | None) -> dt.date | None:
    d = parse_local(value)
    return d.date() if d else None


def local_str(value: str | None) -> str | None:
    """Normalise a printed local time to 'YYYY-MM-DDTHH:MM', or 'YYYY-MM-DD' when no time was
    printed (a hotel check-in day), or None when it does not parse."""
    d = parse_local(value)
    if d is None:
        return None
    if _HAS_TIME.search(str(value)):
        return d.strftime("%Y-%m-%dT%H:%M")
    return d.strftime("%Y-%m-%d")


def item_dates(item: HasKindAndPayload) -> tuple[dt.date, dt.date] | None:
    p = item.payload
    if item.kind == "flight" or item.kind == "train":
        d0 = _date(p.get("dep_local"))
        d1 = _date(p.get("arr_local")) or d0
    elif item.kind == "lodging":
        d0, d1 = _date(p.get("check_in")), _date(p.get("check_out"))
    elif item.kind == "car":
        d0, d1 = _date(p.get("pickup_time")), _date(p.get("dropoff_time"))
    else:
        return None
    if d0 is None:
        return None
    d1 = d1 or d0
    if item.kind in ("lodging", "car") and d1 < d0:
        d1 = d0  # a check-out before the check-in is a bad extraction, not a span (item_span too)
    return (min(d0, d1), max(d0, d1))  # a flight can land the day before it left (date line)


def item_span(item: HasKindAndPayload) -> tuple[str, str | None, str | None, str | None]:
    """(start_at, end_at, start_tz, end_tz) as stored on trip_items. A hotel/car end that is
    missing or earlier than its start becomes None: a bad extraction never inverts a range."""
    p = item.payload
    if item.kind in ("flight", "train"):
        start, end = local_str(p.get("dep_local")), local_str(p.get("arr_local"))
        tzs = (
            (p.get("origin_tz"), p.get("destination_tz")) if item.kind == "flight" else (None, None)
        )
    elif item.kind == "lodging":
        start, end, tzs = local_str(p.get("check_in")), local_str(p.get("check_out")), (None, None)
    elif item.kind == "car":
        start, end = local_str(p.get("pickup_time")), local_str(p.get("dropoff_time"))
        tzs = (None, None)
    else:
        raise ValueError(f"unknown item kind {item.kind}")
    if start is None:
        raise ValueError(f"{item.kind} item has no start time")
    if item.kind in ("lodging", "car") and end is not None:
        # same zone on both ends: an end before the start is a bad extraction. Flights and trains
        # cross zones (one flown east over the date line lands, by the clock, before it leaves)
        # and are ordered in UTC by validation.
        if parse_local(end) < parse_local(start):  # type: ignore[operator]
            end = None
    return start, end, tzs[0], tzs[1]


# Postal codes of any shape (00000, 000-0000, ZZ99 9ZZ, Z9Z 9Z9) at either end of an
# address part, US state/province codes and the US country tokens: stripped before a part is
# judged to be the city. A house or unit number ("2", "77", "4B") is left alone so a street or an
# "Apt 4B" line is never mistaken for a city; a part that ends in a street suffix, starts with a
# street prefix, or is only a unit word ("Suite", "Terminal") never is either.
_POSTAL = re.compile(
    r"^(?:\d{4,}|\d{3,}-\d{2,}|(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{2,6})$"
)
_STATE_CODE = re.compile(r"^[A-Z]{2}$")
_ABBREVIATION = re.compile(r"^(?:[A-Z]{2,3}|(?:[A-Z]\.){2,3})$")  # DC, NSW, Q.R.
_COUNTRY = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
_STREET_SUFFIX = frozenset(
    "st street ave avenue rd road blvd boulevard dr drive ln lane hwy highway pkwy parkway "
    "ct court pl place sq square ter terrace cir circle trl trail strasse straße platz".split()
)
_STREET_PREFIX = frozenset("rue calle via avenida av paseo carrer".split())
_UNIT_WORDS = frozenset(
    "apt apartment suite ste unit floor fl room rm terminal gate po box level bldg building "
    "dept".split()
)


def _place_part(part: str) -> tuple[str, bool]:
    """One comma-separated address part with postal codes, state codes and 'US' stripped, and
    whether a postal code was attached (the surest sign of the city line: '00000 Paris',
    'Philadelphia PA 00000', 'Tokyo 000-0000')."""
    toks = part.split()
    postal = False
    while toks and (
        _POSTAL.match(toks[-1]) or _STATE_CODE.match(toks[-1]) or toks[-1].lower() in _COUNTRY
    ):
        postal |= bool(_POSTAL.match(toks[-1]))
        toks.pop()
    while toks and _POSTAL.match(toks[0]):
        postal = True
        toks.pop(0)
    return " ".join(toks), postal


def _is_city(cand: str) -> bool:
    toks = [t.rstrip(".") for t in cand.lower().split()]
    return (
        len(cand) > 2
        and bool(toks)
        and not re.search(r"\d", cand)
        and not _ABBREVIATION.match(cand)
        and cand.lower() not in lists.us_states()
        and cand.lower() not in _COUNTRY
        and toks[-1] not in _STREET_SUFFIX
        and (toks[0] not in _STREET_PREFIX or len(toks) == 1)
        and not all(t in _UNIT_WORDS for t in toks)
    )


def city_from_address(address: str | None) -> str | None:
    """ "100 Example Street, Philadelphia, PA, 00000, US" / "Philadelphia, PA 00000" / "200
    Example Avenue, Philadelphia PA 00000 US" / "8 Rue Exemple, 00000 Paris, France" -> the city.
    The street (first part) is never the city; a state or country name is never the city either,
    so a street with the city glued on ("100 Example Street Rochester, Minnesota 00000") yields
    None."""
    if not address:
        return None
    parts = [x.strip() for x in address.split(",") if x.strip()]
    if len(parts) < 2:
        return None
    # "Philadelphia, PA 00000" / "Paris, France": no street at all — the first part is the city
    start = (
        0
        if len(parts) == 2
        and not re.search(r"\d", parts[0])
        and (not _place_part(parts[1])[0] or not re.search(r"\d", parts[1]))
        else 1
    )
    cands = [_place_part(raw) for raw in parts[start:]]
    # the part that carried a postal code first ("00000 Cancún" beats the district before it),
    # then any other part that reads as a city
    for with_postal in (True, False):
        for cand, postal in cands:
            if postal == with_postal and _is_city(cand):
                return cand.title() if cand.isupper() else cand
    return None


def trip_title(item: HasKindAndPayload, home_airports: tuple[str, ...]) -> str:
    """'<place> · <Mon YYYY>' — the place is the city of the first non-home airport, the hotel's
    city, or the car's pick-up city."""
    p = item.payload
    dates = item_dates(item)
    when = f"{MONTHS[dates[0].month - 1]} {dates[0].year}" if dates else ""
    place = None
    if item.kind == "flight":
        dest, orig = (p.get("destination") or "").upper(), (p.get("origin") or "").upper()
        if dest and dest not in home_airports:
            place = p.get("destination_city") or dest
        elif orig and orig not in home_airports:
            place = p.get("origin_city") or orig
        else:
            place = p.get("destination_city") or dest
    elif item.kind == "lodging":
        place = city_from_address(p.get("address")) or p.get("property_name")
    elif item.kind == "car":
        place = (
            p.get("pickup_city")
            or city_from_address(p.get("pickup_location"))
            or p.get("pickup_location")
        )
    elif item.kind == "train":
        place = p.get("destination")
    place = (place or "Trip").strip()
    return f"{place} · {when}".strip(" ·")


def trip_place(title: str) -> str:
    return title.split(" · ", 1)[0].strip()


def place_key(title_or_place: str) -> str:
    """Compare places loosely: an airport's city can carry a region ("City, Region", as a
    flight log's records may print it) where a hotel or a car gives the city alone — same
    place."""
    return trip_place(title_or_place).split(",", 1)[0].strip().lower()


def _trip_range(trip: dict) -> tuple[dt.date | None, dt.date | None]:
    return _date(trip.get("start_date")), _date(trip.get("end_date"))


def choose_trip(
    trips: list[dict], d0: dt.date, d1: dt.date, place: str | None = None
) -> dict | None:
    """Smallest live trip whose dated range (with slack) covers [d0, d1]; when several do, one
    for the same place wins (a Denver booking during the Philadelphia dates joins Denver, not
    Philadelphia). A trip whose bookings are all cancelled is over and never chosen."""
    best: tuple[tuple[int, int], dict] | None = None
    want = (place or "").strip().lower()
    for t in trips:
        if t.get("cancelled"):
            continue
        s, e = _trip_range(t)
        if s is None or e is None:
            continue
        if s - TRIP_SLACK <= d0 and d1 <= e + TRIP_SLACK:
            same_place = bool(want) and trip_place(str(t.get("title") or "")).lower() == want
            rank = (0 if same_place else 1, (e - s).days)
            if best is None or rank < best[0]:
                best = (rank, t)
    return best[1] if best else None


def nearby_trip_same_place(trips: list[dict], place: str, d0: dt.date, d1: dt.date) -> dict | None:
    """An outbound under one PNR and a return days later under another: one trip. The
    closest live trip titled for the same place within NEARBY_DAYS of [d0, d1]."""
    if not place or place.lower() == "trip":
        return None
    best: tuple[int, dict] | None = None
    for t in trips:
        if t.get("cancelled"):
            continue
        if trip_place(str(t.get("title") or "")).lower() != place.lower():
            continue
        s, e = _trip_range(t)
        if s is None or e is None:
            continue
        gap = max((s - d1).days, (d0 - e).days, 0)
        if gap <= NEARBY_DAYS and (best is None or gap < best[0]):
            best = (gap, t)
    return best[1] if best else None


# ----------------------------------------------------------------------------- trip items
def sort_key(ti: TripItem) -> tuple[str, int, str, int]:
    """Day, then timed items before date-only ones (a hotel's check-in day sorts after the flight
    that lands that day), then time, then insertion order."""
    timed = len(ti.start_at) > 10
    return (ti.start_at[:10], 0 if timed else 1, ti.start_at[11:16], ti.id or 0)


def _flights(items: list[TripItem]) -> list[TripItem]:
    live = (i for i in items if i.kind == "flight" and i.status != "cancelled")
    return sorted(live, key=sort_key)


def trip_tz(items: list[TripItem], home_airports: tuple[str, ...]) -> str | None:
    """Zone of the first flight's non-home destination, else the first flight's origin."""
    flights = _flights(items)
    for f in flights:
        if (f.payload.get("destination") or "").upper() not in home_airports and f.end_tz:
            return f.end_tz
    return flights[0].start_tz if flights else None


def zone_at(items: list[TripItem], when: str, home_airports: tuple[str, ...]) -> str | None:
    """Where you are at `when` (local 'YYYY-MM-DD[THH:MM]'; a bare date counts as its end): the
    zone of the latest flight that has landed by then, else — before the first flight lands —
    the zone the trip starts in (a stay the night before the first flight reads in the origin's
    zone, not the destination's), else the trip zone. Right for multi-city trips."""
    flights = [f for f in _flights(items) if f.end_at and f.end_tz]
    at = when if len(when) > 10 else when + "T23:59"
    landed = [f for f in flights if f.end_at <= at]
    if landed:
        return max(landed, key=lambda f: f.end_at or "").end_tz
    if flights and flights[0].start_tz:
        return flights[0].start_tz
    return trip_tz(items, home_airports)


def next_up(items: list[TripItem], now: dt.datetime) -> tuple[str | None, TripItem | None]:
    """('departs', first future item) | ('ongoing', last item) | (None, None)."""
    active = sorted((i for i in items if i.status != "cancelled"), key=sort_key)
    if not active:
        return None, None
    now_s = now.strftime("%Y-%m-%dT%H:%M")
    if active[0].start_at > now_s:
        return "departs", active[0]
    return "ongoing", active[-1]  # travelling (or done): the way home is what matters
