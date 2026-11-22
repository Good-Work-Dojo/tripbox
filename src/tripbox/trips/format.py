# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The one trip-text renderer: Telegram /share and /upcoming, the CLI and the web share page
all produce their text here, so a trip reads the same everywhere. Plain text, no
markdown, local times with zone abbreviations, ready to paste into a message.

Times and dates follow the style a door sets (`styled`, the owner's `trips.display` choices):
the page for each request, a message's notices, the bot's replies, the CLI's texts. With no
style set (or the default one) every text reads as it always has: 12-hour, month first."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tripbox import lists
from tripbox.trips import grouping
from tripbox.trips.store import Trip, TripItem, TripStore
from tripbox.validate.passengers import name_tokens

if TYPE_CHECKING:
    from tripbox.trips.display import Display
    from tripbox.validate.passengers import PassengerBook

INDENT = "   "
# A booking's kind as an icon, in trip text, on the page and in the notifications.
ICON = {"flight": "✈️", "lodging": "🏨", "car": "🚗", "train": "🚆", "other": "📎"}


@dataclass
class DayLine:
    item: TripItem
    role: str  # single | start | end
    lines: list[str]


# ----------------------------------------------------------------------------- the style
# The style the door in force set (`styled`); None, as at every door that sets none: the default
_STYLE: ContextVar[Display | None] = ContextVar("tripbox_display", default=None)


@contextmanager
def styled(display: Display | None) -> Iterator[None]:
    """Within it, every text of this module (and a notice's, `notify.text`) follows `display`;
    None sets no style. Entered once at each door, never left set: the value is this context's
    alone (a thread's, a request's task), so one request's style never reaches another."""
    token = _STYLE.set(display)
    try:
        yield
    finally:
        _STYLE.reset(token)


def style() -> Display | None:
    """The style in force, or None when no door set one."""
    return _STYLE.get()


def _day_first() -> bool:
    chosen = _STYLE.get()
    return chosen is not None and chosen.dates == "dmy"


def _clock(d: dt.datetime) -> str:
    """The time of day: '9:45 PM', or '21:45' on the 24-hour clock."""
    chosen = _STYLE.get()
    if chosen is not None and chosen.clock == "24":
        return d.strftime("%H:%M")
    return d.strftime("%I:%M %p").lstrip("0")


# ----------------------------------------------------------------------------- small formatters
def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def _month_day(d: dt.date) -> str:
    """'Mar 13', or '13 Mar' day first."""
    return f"{d.day} {d:%b}" if _day_first() else d.strftime("%b ") + str(d.day)


def day_label(date: dt.date) -> str:
    """'Thu Mar 13', or 'Thu 13 Mar' day first."""
    return date.strftime("%a ") + _month_day(date)


def _with_year(text: str, year: int) -> str:
    """'Thu Mar 13, 2031', or 'Thu 13 Mar 2031' day first."""
    return f"{text} {year}" if _day_first() else f"{text}, {year}"


def date_range(start: str, end: str) -> str:
    s, e = _d(start), _d(end)
    if s == e:
        return _with_year(day_label(s), s.year)
    if s.year == e.year:
        return _with_year(f"{day_label(s)} – {day_label(e)}", e.year)
    return f"{_with_year(day_label(s), s.year)} – {_with_year(day_label(e), e.year)}"


def short_range(start: str, end: str, today: dt.date) -> str:
    s, e = _d(start), _d(end)
    core = _month_day(s) if s == e else f"{_month_day(s)} – {_month_day(e)}"
    return core if s.year == today.year == e.year else _with_year(core, e.year)


def day_band(date: dt.date) -> str:
    """A day's band on a trip's page: 'THURSDAY, MARCH 13, 2031', or 'THURSDAY 13 MARCH 2031'
    day first."""
    if _day_first():
        return f"{date:%A} {date.day} {date:%B} {date.year}".upper()
    return (date.strftime("%A, %B ") + f"{date.day}, {date.year}").upper()


def zone_name(at: dt.datetime) -> str:
    """The name of the zone of the aware time `at` at that time: its abbreviation ('CDT'), or,
    where that has no letters ('+04', '-03', '+0530'), its offset from UTC ('UTC+4', 'UTC-3',
    'UTC+5:30'; 'UTC' for none)."""
    name = at.strftime("%Z")
    offset = at.utcoffset()
    if any(ch.isalpha() for ch in name) or offset is None:
        return name
    minutes = round(offset.total_seconds() / 60)
    if not minutes:
        return "UTC"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{'-' if minutes < 0 else '+'}{hours}" + (f":{rest:02d}" if rest else "")


def _zone(d: dt.datetime, tz: str | None) -> str | None:
    """The name of the zone `tz` at the local time `d` (`zone_name`); None for no zone or one
    unknown."""
    if not tz:
        return None
    try:
        return zone_name(d.replace(tzinfo=ZoneInfo(tz)))
    except ZoneInfoNotFoundError:
        return None


def fmt_time(local: str | None, tz: str | None) -> str | None:
    """'2031-05-06T09:45' + 'America/Chicago' -> '9:45 AM CDT' (`zone_name`), '09:45 CDT' on
    the 24-hour clock; date-only -> None."""
    if not local or len(local) <= 10:
        return None
    d = dt.datetime.fromisoformat(local)
    zone = _zone(d, tz)
    return _clock(d) if zone is None else f"{_clock(d)} {zone}"


def time_parts(local: str | None, tz: str | None) -> tuple[str, str]:
    """`fmt_time`'s time of day and zone apart, as the page shows them: ('9:45 PM', 'CDT');
    ('9:45 PM', '') with no zone; ('', '') for no time."""
    if not local or len(local) <= 10:
        return "", ""
    d = dt.datetime.fromisoformat(local)
    return _clock(d), _zone(d, tz) or ""


def trip_heading(trip: Trip) -> str:
    return f"{trip.title} · {date_range(trip.start_date, trip.end_date)}"


def identities(names: list[str], book: PassengerBook | None) -> list[tuple[str, list[str]]]:
    """(display name, printed variants) per person: the owner first, then everyone else in printed
    order. The same person printed two ways (SAMPLE/SAM and SAM TAYLOR SAMPLE) is one
    entry; a bare first name from a greeting is nobody (same rule as PassengerBook.classify)."""
    seen: dict[str, tuple[str, list[str]]] = {}
    for raw in names:
        toks = name_tokens(raw)
        if len(toks) < 2:
            continue
        m = book.match(raw) if book is not None else None
        if m is not None and m.kind == "me":
            key, disp = "me", m.canonical.split()[0]
        elif m is not None and m.kind == "guest":
            key, disp = f"guest:{m.canonical}", m.canonical.split()[0]
        else:
            key, disp = "other:" + " ".join(toks), " ".join(t.title() for t in toks)
        seen.setdefault(key, (disp, []))[1].append(raw)
    ordered = sorted(seen.items(), key=lambda kv: 0 if kv[0] == "me" else 1)  # stable
    return [(disp, variants) for _, (disp, variants) in ordered]


def display_names(names: list[str], book: PassengerBook | None) -> list[str]:
    return [disp for disp, _ in identities(names, book)]


def _seats(ti: TripItem, people: list[tuple[str, list[str]]]) -> list[str]:
    """One seat per person, in people order; a seat printed under a name nobody owns is appended."""
    p = ti.payload
    by: dict[str, str] = {k: v for k, v in (p.get("seat_by_passenger") or {}).items() if v}
    if not by:
        return [s for s in (p.get("seats") or []) if s]
    out: list[str] = []
    owned: set[str] = set()
    for _, variants in people:
        seat = next((by[v] for v in variants if v in by), None)
        owned.update(variants)
        if seat:
            out.append(seat)
    for raw, seat in by.items():
        if raw not in owned and seat not in out:
            out.append(seat)
    return out


def _detail_line(
    ti: TripItem, book: PassengerBook | None, *, include_conf: bool, include_names: bool
) -> str | None:
    parts: list[str] = []
    if include_conf and ti.confirmation:
        parts.append(f"conf {ti.confirmation}")
    people = identities([n for n in ti.passengers if n], book)
    if include_names and people:
        parts.append(", ".join(disp for disp, _ in people))
    if ti.kind in ("flight", "train"):
        seats = _seats(ti, people)
        if seats:
            parts.append("seats " + ", ".join(seats))
    return " · ".join(parts) if parts else None


def _airline(p: dict) -> str:
    code = (p.get("carrier") or "").upper()
    airline = lists.airlines().get(code)
    return (airline.short if airline else None) or p.get("airline_name") or code or "Flight"


# ----------------------------------------------------------------------------- per item lines
def item_lines(
    ti: TripItem,
    role: str,
    *,
    items: list[TripItem],
    book: PassengerBook | None,
    home_airports: tuple[str, ...],
    include_conf: bool = True,
    include_names: bool = True,
) -> list[str]:
    p = ti.payload
    icon = ICON.get(ti.kind, ICON["other"])
    # where you are at a time; a booking with no date (Recently deleted lists one) has no time
    zone = lambda when: grouping.zone_at(items, when, home_airports) if when else None  # noqa: E731
    lines: list[str] = []
    detail = _detail_line(ti, book, include_conf=include_conf, include_names=include_names)
    if ti.kind == "flight":
        dep = fmt_time(ti.start_at, ti.start_tz) or "?"
        arr = fmt_time(ti.end_at, ti.end_tz) or "?"
        if ti.end_at and ti.end_at[:10] != ti.start_at[:10]:
            arr += ", " + day_label(_d(ti.end_at))
        head = f"{icon} {_airline(p)} {p.get('flight_number') or ''}".rstrip()
        lines.append(
            f"{head} · {p.get('origin') or '?'} {dep} → {p.get('destination') or '?'} {arr}"
        )
        if detail:
            lines.append(INDENT + detail)
    elif ti.kind == "train":
        dep = fmt_time(ti.start_at, zone(ti.start_at)) or "?"
        arr = fmt_time(ti.end_at, zone(ti.end_at or ti.start_at)) or "?"
        head = f"{icon} {p.get('provider') or 'Train'} {p.get('train_number') or ''}".rstrip()
        route = f"{p.get('origin') or '?'} {dep} → {p.get('destination') or '?'} {arr}"
        lines.append(f"{head} · {route}")
        if detail:
            lines.append(INDENT + detail)
    elif ti.kind == "lodging":
        name = p.get("property_name") or p.get("provider") or "Hotel"
        if role == "end":
            t = fmt_time(ti.end_at, zone(ti.end_at or ti.start_at))
            lines.append(f"{icon} {name} · check-out" + (f" {t}" if t else ""))
        else:
            t = fmt_time(ti.start_at, zone(ti.start_at))
            lines.append(f"{icon} {name} · check-in" + (f" {t}" if t else ""))
            if p.get("address"):
                lines.append(INDENT + str(p["address"]))
            if detail:
                lines.append(INDENT + detail)
    elif ti.kind == "car":
        provider = p.get("provider") or "Rental car"
        pickup, dropoff = p.get("pickup_location") or "", p.get("dropoff_location") or ""
        if role == "end":
            t = fmt_time(ti.end_at, zone(ti.end_at or ti.start_at))
            lines.append(f"{icon} {provider} · drop off" + (f" {t}" if t else ""))
            if dropoff and dropoff != pickup:
                lines.append(INDENT + dropoff)
        else:
            t = fmt_time(ti.start_at, zone(ti.start_at))
            lines.append(f"{icon} {provider} · pick up" + (f" {t}" if t else ""))
            if pickup:
                lines.append(INDENT + pickup)
            if detail:
                lines.append(INDENT + detail)
    else:
        lines.append(f"{icon} {p.get('provider') or ti.kind}")
    if ti.status == "cancelled":
        lines[0] = "❌ CANCELLED " + lines[0]
    return lines


def _entries(ti: TripItem) -> list[tuple[str, str, str]]:
    """(date, sort-time-key, role) for every day an item appears on."""
    start_day, start_t = ti.start_at[:10], ti.start_at[11:16]
    if ti.kind in ("flight", "train") or not ti.end_at:
        return [(start_day, start_t, "single" if ti.kind in ("flight", "train") else "start")]
    end_day, end_t = ti.end_at[:10], ti.end_at[11:16]
    out = [(start_day, start_t, "start")]
    if end_day != start_day or end_t != start_t:
        out.append((end_day, end_t, "end"))
    return out


def group_by_day(
    items: list[TripItem],
    *,
    book: PassengerBook | None,
    home_airports: tuple[str, ...],
    selected: set[int] | None = None,
    include_conf: bool = True,
    include_names: bool = True,
) -> list[tuple[dt.date, list[DayLine]]]:
    """Days in order, each with its lines. Default selection = every non-cancelled item."""
    if selected is not None:
        chosen = [ti for ti in items if ti.id in selected]
    else:
        chosen = [ti for ti in items if ti.status != "cancelled"]
    days: dict[str, list[tuple[tuple[int, str, int], DayLine]]] = {}
    for seq, ti in enumerate(sorted(chosen, key=grouping.sort_key)):
        for day, t, role in _entries(ti):
            # timed entries in time order; a date-only check-out/drop-off happens in the
            # morning (before the flight home), a date-only check-in/pick-up after arriving
            key = (0 if t else (-1 if role == "end" else 1), t, seq)
            lines = item_lines(
                ti,
                role,
                items=items,
                book=book,
                home_airports=home_airports,
                include_conf=include_conf,
                include_names=include_names,
            )
            days.setdefault(day, []).append((key, DayLine(ti, role, lines)))
    return [
        (_d(day), [dl for _, dl in sorted(entries, key=lambda e: e[0])])
        for day, entries in sorted(days.items())
    ]


# ----------------------------------------------------------------------------- whole texts
def trip_text(
    trip: Trip,
    items: list[TripItem],
    *,
    book: PassengerBook | None,
    home_airports: tuple[str, ...],
    include_conf: bool = True,
    include_names: bool = True,
    include_notes: bool = True,
    selected: set[int] | None = None,
) -> str:
    blocks = [trip_heading(trip)]
    if include_notes and trip.notes:
        blocks.append("Notes: " + trip.notes.strip())
    for day, lines in group_by_day(
        items,
        book=book,
        home_airports=home_airports,
        selected=selected,
        include_conf=include_conf,
        include_names=include_names,
    ):
        blocks.append("\n".join([day_label(day)] + [ln for dl in lines for ln in dl.lines]))
    return "\n\n".join(blocks)


def upcoming_text(
    store: TripStore,
    today: dt.date,
    *,
    book: PassengerBook | None,
    home_airports: tuple[str, ...],
    max_trips: int = 5,
) -> str:
    trips = store.upcoming(today)[:max_trips]
    if not trips:
        return "Nothing upcoming."
    return "\n\n".join(
        trip_text(t, store.items(t.id), book=book, home_airports=home_airports) for t in trips
    )


def counts_text(counts: dict[str, int]) -> str:
    return " ".join(
        f"{ICON[k]}{counts[k]}" for k in (*grouping.KIND_ORDER, "other") if counts.get(k)
    )


def trip_line(trip: Trip, store: TripStore, today: dt.date, link_base: str | None = None) -> str:
    """The trip on one line, linked to its page when the page's address is set (`link_base`)."""
    line = f"#{trip.id} {trip.title} · {short_range(trip.start_date, trip.end_date, today)}"
    c = counts_text(store.counts(trip.id))
    if c:
        line += f" · {c}"
    if trip.cancelled:
        line += " · cancelled"
    if link_base:
        line += f" {link_base}/trips/{trip.id}"
    return line


def trip_list_text(
    store: TripStore, today: dt.date, *, link_base: str | None = None, past_limit: int = 5
) -> str:
    up, past = store.upcoming(today), store.past(today, limit=past_limit)
    if not up and not past:
        return "No trips yet."
    out = [trip_line(t, store, today, link_base) for t in up] or ["Nothing upcoming."]
    if past:
        out += ["", "Past:"] + [trip_line(t, store, today, link_base) for t in past]
    return "\n".join(out)
