# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The texts of the notifications: a message's lines, a booking's card and a failure. Plain text,
the same for every channel; the icons are the trip text's (`trips.format.ICON`). Within a style
(`trips.format.styled`, which the pipeline and the bot enter at their doors) whose clock or date
order is not the default, a booking's days and times are said as the page says them; with none,
or the default, in a notice's own words, as always."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from types import SimpleNamespace
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tripbox.log import redact
from tripbox.trips import format as fmt
from tripbox.trips.format import ICON
from tripbox.validate import checks
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rules import Item, day_alone, parse_local

if TYPE_CHECKING:
    from tripbox.trips.store import TripItem
    from tripbox.write.executor import Outcome


def _page_words() -> bool:
    """Whether a booking's days and times take the page's words: while the owner's style has
    another clock or date order than the default (12-hour, month first)."""
    chosen = fmt.style()
    return chosen is not None and (chosen.clock, chosen.dates) != ("12", "mdy")


def fmt_local(local: str | None, tz: str | None, with_date: bool = True) -> str:
    d = parse_local(local)
    if d is None:
        return "?"
    if _page_words():  # the owner's style: the page's words, the day then the time
        day = fmt.day_label(d.date())
        if day_alone(local):
            return day if with_date else "?"
        at = fmt.fmt_time(d.strftime("%Y-%m-%dT%H:%M"), tz)
        return f"{day} {at}" if with_date else str(at)
    if day_alone(local):  # a day printed with no time shows no time
        return d.strftime("%a %Y-%m-%d") if with_date else "?"
    abbr = ""
    if tz:
        try:  # named as the page names it: `UTC+4` for a zone with no letters of its own
            abbr = " " + fmt.zone_name(d.replace(tzinfo=ZoneInfo(tz)))
        except ZoneInfoNotFoundError:
            abbr = ""
    return (d.strftime("%a %Y-%m-%d %H:%M") if with_date else d.strftime("%H:%M")) + abbr


def _when(value: object, cut: int) -> str:
    """A stay's, a car's or a train's time as a line says it: as stored, cut to `cut` characters
    (a stay's day, another's day and time); in the page's words (`_page_words`), its day, then
    its time when it has one (`fmt_local`, no zone)."""
    if not _page_words():
        return str(value or "?")[:cut]
    if not value:
        return "?"
    shown = fmt_local(str(value)[:cut], None)
    return shown if shown != "?" else str(value)[:cut]


def _another_day(dep: object, arr: object) -> bool:
    """Whether a flight lands on another day than it leaves (each by its own local day), so its
    arrival says its day; False when either is not a time."""
    left, landed = parse_local(dep), parse_local(arr)
    return left is not None and landed is not None and left.date() != landed.date()


def item_line(item: Item) -> str:
    p = item.payload
    who = (["Me"] if item.classified.me_present else []) + [
        g.split()[0] for g in item.classified.guests
    ]
    names = ", ".join(who + item.classified.others) or "—"
    if item.kind == "flight":
        dep = fmt_local(p.get("dep_local"), p.get("origin_tz"))
        arr = fmt_local(
            p.get("arr_local"),
            p.get("destination_tz"),
            with_date=_another_day(p.get("dep_local"), p.get("arr_local")),
        )
        route = f"{p.get('origin', '?')}→{p.get('destination', '?')}"
        line = f"{ICON['flight']} {p.get('flight_code', '?')} {route} {dep} → {arr} · {names}"
    elif item.kind == "lodging":
        where = p.get("property_name") or p.get("provider") or "Hotel"
        span = f"{_when(p.get('check_in'), 10)} → {_when(p.get('check_out'), 10)}"
        line = f"{ICON['lodging']} {where} · {span} · {names}"
    elif item.kind == "car":
        span = f"{_when(p.get('pickup_time'), 16)} → {_when(p.get('dropoff_time'), 16)}"
        where = p.get("pickup_location") or ""
        line = f"{ICON['car']} {p.get('provider') or 'Rental car'} · {where} · {span} · {names}"
    else:
        head = f"{p.get('provider') or 'Train'} {p.get('train_number') or ''}".strip()
        route = f"{p.get('origin') or '?'} → {p.get('destination') or '?'}"
        line = f"{ICON['train']} {head} {route} · {_when(p.get('dep_local'), 16)} · {names}"
    code = p.get("pnr") or p.get("confirmation_code")
    if code:
        line += f" · conf {code}"
    if p.get("price") is not None:
        line += f" · {p.get('currency') or ''} {p['price']:.2f}".replace("  ", " ")
    return line


def booking_line(ti: TripItem, book: PassengerBook) -> str:
    """The line `item_line` gives, for a booking as the store holds it."""
    who = book.classify(ti.passengers)
    return item_line(SimpleNamespace(kind=ti.kind, payload=ti.payload, classified=who))


def to_check(codes: Iterable[str]) -> str:
    """The sentences of the checks `codes`, in their order."""
    return " ".join(checks.LABELS.get(code, code) for code in codes)


def booking_link(ti: TripItem, link_base: str) -> str:
    """The booking's address on the page at `link_base` (TRIPBOX_BASE_URL): its trip's page at
    the booking, or the list for a booking with no trip."""
    return link_base + ti.page


def check_card(ti: TripItem, book: PassengerBook, *, link_base: str | None = None) -> str:
    """A booking's own message: its line, the sentences of the checks open on it (none when
    none is) and, when the page's address is set (`link_base`), the link to the booking there
    (the list for a booking with no trip)."""
    lines = [booking_line(ti, book)]
    if ti.checks:
        lines.append(to_check(ti.checks))
    if link_base:
        lines.append(booking_link(ti, link_base))
    return "\n".join(lines)


# What an ignored email was, said for a person (the plan gives the word).
IGNORED = {
    "older_copy": "an older copy of a booking that has changed since",
    "unknown_cancellation": "a cancellation that does not say whose seat it cancels",
}
# Where a filed or updated flight stands with the flight log once the message's pass is done,
# said only when one is kept; a flight the owner is not on (`off`) says nothing.
FLIGHT_LOG = {
    "synced": "✅ in your flight log",
    "pending": "⏳ waiting to go to your flight log",
    "blocked": "not sent to your flight log until checked",
}


def outcome_line(o: Outcome, link_base: str | None = None, *, flight_log: bool = False) -> str:
    """The booking's line and what the email did to it; a booking marked to check ends with
    `to check:` and the sentences of its checks. With `flight_log` (one is kept) a flight says
    where it stands with it; without, nothing names one."""
    base = item_line(o.action.item)
    a = o.action
    marked = f" · to check: {to_check(o.checks)}" if o.checks else ""
    tags: list[str] = []
    if o.result == "failed":
        tags.append(f"⚠️ failed: {o.error}")
    elif o.result == "ignored":
        return f"⏭ {base}\n    ignored: {IGNORED.get(a.reason, a.reason)}{marked}"
    elif o.result == "duplicate":
        tags.append("already recorded")
    elif o.result == "filed" and a.item.status == "cancelled":
        tags.append("❌ CANCELLED")
    elif o.result == "updated":
        if a.reason == "cancel":
            tags.append("❌ CANCELLED")
        if a.add_passengers:
            tags.append("➕ " + ", ".join(a.add_passengers))
        if a.remove_passengers:
            tags.append("➖ " + ", ".join(a.remove_passengers))
        if a.changes:
            desc = ", ".join(f"{k} {ov}→{nv}" for k, (ov, nv) in a.changes.items())
            tags.append(f"✏️ changed: {desc}")
        elif a.reason in ("changed", "rescheduled"):
            tags.append("✏️ changed")
    flight = a.item.kind == "flight"
    if flight_log and o.result in ("filed", "updated") and flight and o.sync in FLIGHT_LOG:
        tags.append(FLIGHT_LOG[o.sync])
    if o.trip_id:
        tag = f'trip "{o.trip_title}"' if o.trip_title else f"trip #{o.trip_id}"
        if link_base:  # Telegram auto-links a plain URL: one tap from the notice to the trip
            tag += f" {link_base}/trips/{o.trip_id}"
        tags.append(tag)
    elif o.trip_title:  # the booking's trip is in Recently deleted: named, never linked (404)
        tags.append(f'trip "{o.trip_title}" (deleted)')
    if not tags:
        return base + ("\n    " + marked.removeprefix(" · ") if marked else "")
    return base + "\n    " + " · ".join(tags) + marked


def format_outcomes(
    subject: str,
    outcomes: Iterable[Outcome],
    *,
    parser: str | None = None,
    link_base: str | None = None,
    flight_log: bool = False,
    limit: int | None = None,
) -> str | None:
    """The message for an email's bookings, or None when it filed nothing new: every booking a
    copy or ignored, unless one was ignored for marking its booking. `flight_log` says that one
    is kept (the caller asks the store): only then does any line name it. With `limit`, at most
    that many bookings' lines, then how many more the page holds (MORE_ON_THE_PAGE)."""
    outs = list(outcomes)
    interesting = [
        o
        for o in outs
        if o.result not in ("duplicate", "ignored")
        or o.action.reason == checks.UNKNOWN_CANCELLATION
    ]
    if not interesting:
        return None
    head = f"📬 {subject}" + (f" ({parser})" if parser else "")
    shown = outs if limit is None else outs[:limit]
    lines = [head] + [outcome_line(o, link_base, flight_log=flight_log) for o in shown]
    if len(outs) > len(shown):
        lines.append(MORE_ON_THE_PAGE.format(n=len(outs) - len(shown)))
    return "\n".join(lines)


# The last lines of a `filed` notice: past the bookings it shows; when the message marked more
# bookings than cards were sent for; past the emails attached to one message that are told.
MORE_ON_THE_PAGE = "and {n} more on the page"
MORE_TO_CHECK = "and {n} more to check on the page"
MORE_EMAILS = "and {n} more emails were attached; see the page"


def format_failure(subject: str, error: str) -> str:
    """The failure notice: `error` masked, then cut (a secret across the cut leaves nothing)."""
    return f"⚠️ tripbox could not process “{subject}”: {redact(error)[:500]}"


# Why a message waits for the model (`run.WAIT_REASONS`), as the owner is told.
WAITING_WHY = {
    "no_model": "No model is set up, so it waits until one is.",
    "over_budget": "The model's budget is used; it is read when the budget allows.",
    "model_down": "The model cannot be reached; it is tried again every few minutes.",
}
# The notice, once a period, that the model's cap is reached: a priced model's monthly budget,
# or the calls a day of one with no known price.
BUDGET_MONTH = (
    "tripbox has used this month's model budget (${budget:.2f}). Mail that needs the model waits"
    " and is read when the budget allows; TRIPBOX_LLM_MONTHLY_BUDGET_USD raises it."
)
BUDGET_DAY = (
    "tripbox has made today's {n} model calls. Mail that needs the model waits until tomorrow"
    " (UTC); TRIPBOX_LLM_MAX_CALLS_PER_DAY raises it."
)
# The notice, once an outage, that the model cannot be reached; `word` says how it failed.
MODEL_DOWN = (
    "tripbox cannot reach the model ({word}). Mail that needs it waits and is read when it"
    " answers again."
)


def human_date(d: dt.datetime | None) -> str:
    """'2031-05-06 09:45 CDT', its zone named as the page names it (`UTC+4` for one with no
    letters)."""
    return f"{d:%Y-%m-%d %H:%M} {fmt.zone_name(d)}" if d else "?"
