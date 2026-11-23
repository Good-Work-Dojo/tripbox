# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""iCalendar export: one trip as a download, every trip as a private subscribe feed. Flights and
cars are timed events in UTC (every client shows local time correctly and no VTIMEZONE blocks
are needed); hotels are all-day events; cancelled items carry STATUS:CANCELLED so a subscribed
calendar drops them. A booking's UID is its row id, never given twice and kept when an edit or a
later email changes its key, so edits update in place."""

from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.trips import format as fmt
from tripbox.trips import grouping
from tripbox.trips.store import Trip, TripItem, TripStore
from tripbox.validate.passengers import PassengerBook

PRODID = "-//tripbox//trips//EN"
UID_DOMAIN = "tripbox.invalid"
log = logging.getLogger("tripbox.ical")
KV_FEED_TOKEN = "tripbox_feed_token"
_EMOJI_PREFIX = re.compile(r"^(?:❌ CANCELLED )?[^\w\s]+\s*")


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def escape(text: str) -> str:
    """RFC 5545 text: CR/LF folded into \\n, other control characters dropped (an extracted
    address can carry an email's CRLF, and a bare CR inside a content line breaks parsers)."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text.replace("\\", "\\\\").replace(";", r"\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """RFC 5545 folding at 75 octets, never splitting a UTF-8 character."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    out: list[str] = []
    width = 75
    while data:
        cut = min(width, len(data))
        while cut < len(data) and (data[cut] & 0xC0) == 0x80:  # continuation byte: back off
            cut -= 1
        out.append(data[:cut].decode("utf-8"))
        data = data[cut:]
        width = 74  # continuation lines start with a space
    return "\r\n ".join(out)


def _stamp(iso: str | None) -> str:
    d = dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00")) if iso else None
    if d is None:
        d = util.now()
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return d.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _utc(local: str | None, tz: str | None) -> str | None:
    """Local 'YYYY-MM-DDTHH:MM' + zone -> 'YYYYMMDDTHHMMSSZ'; floating local when no zone."""
    if not local or len(local) <= 10:
        return None
    d = dt.datetime.fromisoformat(local)
    if tz:
        try:
            return d.replace(tzinfo=ZoneInfo(tz)).astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        except ZoneInfoNotFoundError:
            pass
    return d.strftime("%Y%m%dT%H%M%S")


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def event(
    ti: TripItem, items: list[TripItem], *, book: PassengerBook | None, home: tuple[str, ...]
) -> list[str]:
    p = ti.payload
    text = fmt.item_lines(ti, "single", items=items, book=book, home_airports=home)
    summary = _EMOJI_PREFIX.sub("", text[0]) if ti.kind in ("flight", "train") else None
    lines = [
        "BEGIN:VEVENT",
        f"UID:tripbox-item-{ti.id}@{UID_DOMAIN}",
        f"DTSTAMP:{_stamp(ti.updated_at)}",
    ]
    if ti.kind == "flight" and p.get("dep_utc"):
        lines.append(f"DTSTART:{_stamp(p['dep_utc'])}")
        if p.get("arr_utc"):
            lines.append(f"DTEND:{_stamp(p['arr_utc'])}")
        location = f"{p.get('origin') or '?'} → {p.get('destination') or '?'}"
    elif ti.kind == "lodging":
        start = _date(ti.start_at)
        end = _date(ti.end_at) if ti.end_at else start
        lines.append(f"DTSTART;VALUE=DATE:{start:%Y%m%d}")
        lines.append(f"DTEND;VALUE=DATE:{end + dt.timedelta(days=1):%Y%m%d}")
        name = p.get("property_name") or p.get("provider") or "Hotel"
        summary = f"{name} · {fmt.day_label(start)} – {fmt.day_label(end)}"
        location = str(p.get("address") or "")
    else:  # car, train, flight without UTC fields: local + the zone of where you are
        start = _utc(ti.start_at, ti.start_tz or grouping.zone_at(items, ti.start_at, home))
        end = (
            _utc(ti.end_at, ti.end_tz or grouping.zone_at(items, ti.end_at, home))
            if ti.end_at
            else None
        )
        if start is None or (ti.end_at and end is None):
            # a date-only end (or start): both ends as dates, RFC 5545 wants one value type
            d0 = _date(ti.start_at)
            d1 = _date(ti.end_at) if ti.end_at else d0
            lines.append(f"DTSTART;VALUE=DATE:{d0:%Y%m%d}")
            lines.append(f"DTEND;VALUE=DATE:{d1 + dt.timedelta(days=1):%Y%m%d}")
        else:
            lines.append(f"DTSTART:{start}")
            if end:
                lines.append(f"DTEND:{end}")
        if ti.kind == "car":
            head = fmt.item_lines(ti, "start", items=items, book=book, home_airports=home)
            summary = _EMOJI_PREFIX.sub("", head[0])
            text = head + fmt.item_lines(ti, "end", items=items, book=book, home_airports=home)
            location = str(p.get("pickup_location") or "")
        else:
            location = f"{p.get('origin') or '?'} → {p.get('destination') or '?'}"
    if ti.status == "cancelled":
        lines.append("STATUS:CANCELLED")
        summary = f"Cancelled: {summary or ti.kind}"  # clients honour STATUS unevenly
    lines.append(f"SUMMARY:{escape(summary or ti.kind)}")
    if location:
        lines.append(f"LOCATION:{escape(location)}")
    lines.append(f"DESCRIPTION:{escape(chr(10).join(text))}")
    lines.append("END:VEVENT")
    return lines


def calendar(
    name: str,
    entries: list[tuple[Trip, list[TripItem]]],
    *,
    book: PassengerBook | None,
    home: tuple[str, ...],
    description: str | None = None,
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(name)}",
    ]
    if description:
        lines.append(f"X-WR-CALDESC:{escape(description)}")
    for _trip, items in entries:
        for ti in items:
            try:
                lines += event(ti, items, book=book, home=home)
            except Exception:  # noqa: BLE001 - one booking must not sink the calendar
                log.exception("booking #%s is left out of the calendar", ti.id)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(ln) for ln in lines) + "\r\n"


def trip_ics(
    trip: Trip, items: list[TripItem], *, book: PassengerBook | None, home_airports: tuple[str, ...]
) -> str:
    return calendar(
        trip.title, [(trip, items)], book=book, home=home_airports, description=trip.notes
    )


def feed_ics(
    store: TripStore, *, book: PassengerBook | None, home_airports: tuple[str, ...]
) -> str:
    entries = [(t, store.items(t.id)) for t in store.all_trips()]
    return calendar("Trips", entries, book=book, home=home_airports)


def feed_token(ledger: Ledger, *, rotate: bool = False) -> str:
    """The secret in the all-trips feed URL (calendar apps cannot log in through Access)."""
    current = ledger.kv.get(KV_FEED_TOKEN)
    if current and not rotate:
        return current
    token = secrets.token_urlsafe(32)
    ledger.kv.set(KV_FEED_TOKEN, token)
    return token
