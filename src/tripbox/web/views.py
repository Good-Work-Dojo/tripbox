# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Route handlers + the presentation model the templates render. Text for copying always comes
from trips.format so the page and Telegram never disagree."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import Message

from tripbox import status as worker_status
from tripbox import uploads, util
from tripbox.ledger import Ledger
from tripbox.trips import coversniff, grouping, ical, shares
from tripbox.trips import display as displaymod
from tripbox.trips import format as fmt
from tripbox.trips.covers import Cover
from tripbox.trips.format import ICON
from tripbox.trips.store import DRY_RUN_KV, Trip, TripItem, TripStore, editable_value
from tripbox.util import parse_iso, today_local
from tripbox.validate import checks
from tripbox.web import card as statuscard
from tripbox.web.middleware import COVER
from tripbox.web.urls import Urls

log = logging.getLogger("tripbox.web")

PAST_LIMIT = 10
MAX_ID = 2**62  # SQLite INTEGER; anything larger is not an id of ours
FLASH = {  # ?f=<key> after a redirect → one toast; app.js then strips it from the URL
    "deleted": "Trip deleted — restore it from Recently deleted within 30 days",
    "removed": "Removed — restore it from Recently deleted within 30 days",
    "restored": "Restored",
    "saved": "Saved",
    "place": "Place saved",
    "looked": "Marked as right",
    "owner_yes": "Added you to this flight",
    "owner_no": "Noted: not your flight",
    "again": "It will be sent again",
    "noowner": "No owner is named in travellers.yaml",
    "stale": "The page was out of date: check again",
    "added": "Added",
    "cover_received": "Photo received — it shows in a few seconds",
    "cover_heic": "That photo is HEIC — upload a JPEG, PNG or WebP",
    "cover_type": "That file is not a JPEG, PNG or WebP photo",
    "cover_big": "That photo is larger than 10 MB",
    "cover_removed": "Photo removed",
    "place_photo": "Place saved — looking for a photo",
}
# The kinds of picture an owner's cover photo may be (`coversniff.sniff`); a HEIC one is told so
COVER_KINDS = ("jpeg", "png", "webp")
# Why the worker could not make the owner's photo into a cover (`coverimage.CoverImageError`), as
# the trip's page says it; any other word reads as the first
COVER_FAILED = {
    "unreadable": "it could not be read",
    "too_many_pixels": "it has more than 40 million pixels",
    "wrong_type": "it is not a JPEG, PNG or WebP",
}
# Where a flight stands with the flight log (its sync state), said only while one is kept; a
# flight it holds nothing for (`off`) says nothing.
SYNC_LINE = {
    "synced": "In your flight log",
    "pending": "Waiting to be sent to your flight log",
    "blocked": "Not sent to your flight log until checked",
}
DRY_RUN_LINE = "Not sent: your flight log is in dry-run mode"  # a pending flight in a dry run
# What removing a booking or deleting a trip leaves as it was: the flight log only while one is
# kept, so a page never names what the install does not have
KEPT = {
    True: "Your flight log and the saved emails are not touched.",
    False: "The saved emails are not touched.",
}
RESTORE = "Restore it from Recently deleted within 30 days."
GONE = "This flight is no longer in your flight log."  # deleted there by hand: Send again
KIND_LABEL = {"flight": "Flights", "lodging": "Hotels", "car": "Cars", "train": "Trains"}
EDIT_WHAT = {"flight": "flight", "train": "train", "lodging": "hotel", "car": "car"}
# The word of the path a booking is added by hand under (`/add/<word>`) → its kind, in the order
# the chooser lists them.
ADD_KIND = {"flight": "flight", "hotel": "lodging", "car": "car", "train": "train"}
EDIT_TEXT = {  # the store's fields (trips.store.EDITABLE) a form shows as they are
    "flight": ("carrier", "flight_number", "origin", "destination", "confirmation_code"),
    "train": ("provider", "train_number", "origin", "destination", "confirmation_code"),
    "lodging": ("property_name", "address", "confirmation_code"),
    "car": ("provider", "pickup_location", "dropoff_location", "confirmation_code"),
}
EDIT_TIMES = {  # payload field → form prefix (<prefix>_date + optional <prefix>_time)
    "flight": (("dep_local", "departs"), ("arr_local", "arrives")),
    "train": (("dep_local", "departs"), ("arr_local", "arrives")),
    "lodging": (("check_in", "check_in"), ("check_out", "check_out")),
    "car": (("pickup_time", "pickup"), ("dropoff_time", "dropoff")),
}
EDIT_NAMES = ("flight", "train")  # the forms that list the travellers, one name per line
# The form field a hard check is about: it carries aria-invalid and points at the check.
CHECK_FIELD = {
    "carrier": "carrier",
    "flight_number": "flight_number",
    "origin": "origin",
    "destination": "destination",
    "departure": "departs_date",
    "arrival_before_departure": "arrives_date",
    "stations": "origin",
    "check_in": "check_in_date",
    "check_out": "check_out_date",
    "pickup_time": "pickup_date",
    "dropoff_time": "dropoff_date",
}
STALE_FORM = "This booking changed since the page was loaded. Check it and save again."
NEXT_VERB = {"flight": "Departs", "train": "Departs", "lodging": "Check-in", "car": "Pick-up"}
# The most of a request's body the paste and upload forms read: the largest upload and room for
# the form around it. Past it the page answers 413 before the form is parsed.
BODY_CAP = uploads.MAX_BYTES + 65536
FULL = (
    f"{uploads.MAX_WAITING} uploads are waiting to be read already. Send this again once they are."
)
# What the upload form says of a refusal (`uploads.Refused.code`), and the paste form
UPLOAD_REFUSED = {
    "too_large": "That file is larger than 10 MB.",
    "wrong_type": "That file is not an email, a PDF or text.",
    "empty": "That file is empty.",
    "full": FULL,
}
PASTE_REFUSED = {
    "too_large": f"That text is longer than {uploads.MAX_TEXT_CHARS:,} characters.",
    "wrong_type": "That text holds control characters. Paste the text alone.",
    "empty": "Paste the confirmation's text first.",
    "full": FULL,
}
REFUSED_STATUS = {"too_large": 413, "wrong_type": 415, "empty": 400, "full": 429}
UPLOAD_KIND = {"text": "Text", "eml": "An email", "pdf": "A PDF"}  # an upload with no name
# What the status page of an upload given up (`uploads.MAX_READINGS`, its word `stopped`) says
STOPPED = "tripbox stopped three times while reading this. It is kept; add the booking by hand."
# The header that marks a page the service worker may keep (`_keepable`)
KEEP = "X-Tripbox-Offline"
# The page's own files (the stylesheet, the script, the fonts)
STATIC = Path(__file__).parent / "static"
# What a share link may show (`shares.FIELDS`), as its box on the share page and its list say it
LINK_LABEL = {
    "names": "Names",
    "addresses": "Addresses",
    "seats": "Seats",
    "times": "Times",
    "codes": "Confirmation codes",
    "notes": "Notes",
}


@dataclass
class Ctx:
    ledger: Ledger
    store: TripStore
    book: object
    home: tuple[str, ...]
    settings: object
    flight_log: bool = False  # one is kept: the ledger's `kv` row says so
    dry_run: bool = False  # the setting holds every flight back from it (`kv`, as configured)
    display: displaymod.Display = displaymod.Display()  # the owner's choices (`trips.display`)


@contextmanager
def _ctx(request: Request) -> Iterator[Ctx]:
    """The request's ledger and store, with every text said in the owner's style for as long as
    it is held (`format.styled`: the page's door)."""
    state = request.app.state
    led = state.ledger_factory()
    try:
        store = TripStore(led, state.settings.home_airports, book=state.book)
        chosen = displaymod.read(led)
        with fmt.styled(chosen):
            yield Ctx(
                led,
                store,
                state.book,
                state.settings.home_airports,
                state.settings,
                flight_log=store.airtrail_on(),
                dry_run=led.kv.get(DRY_RUN_KV) == "on",
                display=chosen,
            )
    finally:
        if getattr(state, "owns_ledger", False):
            led.close()


def _render(request: Request, name: str, context: dict, status: int = 200) -> Response:
    """The template `name`; the security headers are the `Headers` wrapper's."""
    return request.app.state.templates.TemplateResponse(request, name, context, status_code=status)


def _keepable(request: Request, response: Response) -> Response:
    """Marks `response` (`KEEP: 1`) as one the service worker may keep for offline use: only
    while saved copies are on, for a 200 shown to somebody signed in. The list and a trip page
    alone call this; the worker keeps nothing that is not marked."""
    signed_in = (request.scope.get("state") or {}).get("user")
    if request.app.state.settings.offline and response.status_code == 200 and signed_in:
        response.headers[KEEP] = "1"
    return response


def _flash(request: Request) -> str | None:
    return FLASH.get(request.query_params.get("f") or "")


def _said(c: Ctx) -> dict[str, str]:
    """The sentences a page asks to be confirmed, and Recently deleted's note, for this install:
    what stays as it was names the flight log only while one is kept."""
    kept = KEPT[c.flight_log]
    return {
        "remove": f"Remove this booking? {kept} {RESTORE}",
        "remove_from_trip": f"Remove this booking from the trip? {kept} {RESTORE}",
        "delete_trip": f"Delete this trip? {kept} {RESTORE}",
        "deleted_note": f"Everything here is removed for good 30 days after it was deleted. {kept}",
    }


def _day(iso: str | None) -> str:
    """'2031-03-13T20:08:00+00:00' → 'Thu Mar 13' in the server's local zone."""
    if not iso:
        return ""
    try:
        return fmt.day_label(parse_iso(iso).astimezone().date())
    except ValueError:
        return ""


def _id(value: str | None) -> int | None:
    """A trip/item id from user input, or None. str.isdigit() accepts '²' (int() does not), and
    a 20-digit number overflows SQLite; neither deserves a 500."""
    s = (value or "").strip()
    if not s.isascii() or not s.isdecimal() or int(s) > MAX_ID:
        return None
    return int(s)


def _trip_or_404(c: Ctx, trip_id: int) -> Trip:
    trip = c.store.get(trip_id) if 0 < trip_id <= MAX_ID else None
    if trip is None:
        raise HTTPException(404, "no such trip")
    return trip


def _item_or_404(c: Ctx, raw_id: str) -> TripItem:
    """A booking the owner can see (`TripStore.visible`), of a live trip or of none (a booking
    with no date); a stale tab acting on a removed row or a deleted trip's row gets a 404 and
    changes nothing."""
    ti = c.store.visible(_id(raw_id) or -1)
    if ti is None:
        raise HTTPException(404, "no such item")
    return ti


# ----------------------------------------------------------------------------- presentation
def _people(items: list[TripItem], book) -> str:
    names: list[str] = []
    for ti in items:
        if ti.status == "cancelled":
            continue
        for n in fmt.display_names(ti.passengers, book):
            if n not in names:
                names.append(n)
    return ", ".join(names)


def _counts(counts: dict[str, int]) -> str:
    return " · ".join(f"{ICON[k]} {counts[k]}" for k in grouping.KIND_ORDER if counts.get(k))


def _next_up(trip: Trip, items: list[TripItem], home, now: dt.datetime) -> str:
    kind, ti = grouping.next_up(items, now)
    if kind == "departs" and ti is not None:
        when = fmt.day_label(dt.date.fromisoformat(ti.start_at[:10]))
        t = fmt.fmt_time(ti.start_at, ti.start_tz or grouping.zone_at(items, ti.start_at, home))
        return f"{NEXT_VERB.get(ti.kind, 'Starts')} {when}" + (f" · {t}" if t else "")
    if kind == "ongoing":
        return f"Ongoing · back {fmt.day_label(dt.date.fromisoformat(trip.end_date))}"
    return ""


def card(
    c: Ctx,
    trip: Trip,
    *,
    now: dt.datetime,
    with_next: bool,
    to_check: int = 0,
    cover: Cover | None = None,
) -> dict:
    """A trip's card on the list; `to_check` is how many of its bookings have a check open, and
    `photo` the digest of its ready cover (the tile without one)."""
    items = c.store.items(trip.id)
    return {
        "trip": trip,
        "range": fmt.date_range(trip.start_date, trip.end_date),
        "counts": _counts(c.store.counts(trip.id)),
        "people": _people(items, c.book),
        "next": _next_up(trip, items, c.home, now) if with_next else "",
        "year": int(trip.start_date[:4]),
        "to_check": to_check,
        "photo": _ready(cover),
    }


def _ready(cover: Cover | None) -> str | None:
    """The digest of a cover the page can show, None for the tile."""
    return cover.digest if cover is not None and cover.state == "ready" else None


def _web_page(value: object) -> str | None:
    """An address a credit links to, only when it is a web page's: http or https, with a host
    and no space or control character in it."""
    address = str(value or "").strip()
    if not address or any(ch.isspace() or not ch.isprintable() for ch in address):
        return None
    try:
        parts = urlsplit(address)
    except ValueError:
        return None
    return address if parts.scheme in ("http", "https") and parts.hostname else None


def _photo(cover: Cover | None) -> dict:
    """What a trip's page shows of its cover: the ready photo's `digest` (else the tile); a
    Wikimedia photo's `credit`, its parts each as text, the licence and the source linked to
    their pages; whether the owner's photo is `pending`, or why the worker could not use it
    (`failed`); and whether there is a photo to remove (the owner's, in any state, or a ready
    one)."""
    out: dict = {"digest": _ready(cover), "credit": [], "pending": False, "failed": ""}
    out["removable"] = cover is not None and (cover.source == "owner" or cover.state == "ready")
    if cover is None:
        return out
    if cover.source == "owner":
        out["pending"] = cover.state == "pending"
        if cover.state == "failed":
            out["failed"] = COVER_FAILED.get(cover.error or "", COVER_FAILED["unreadable"])
    elif out["digest"] and cover.source == "wikimedia":
        credit = cover.credit or {}
        parts = [
            (credit.get("artist"), None),
            (credit.get("licence"), credit.get("licence_url")),
            ("Wikimedia Commons", credit.get("source_url")),
        ]
        out["credit"] = [
            {"text": str(text).strip(), "href": _web_page(href)}
            for text, href in parts
            if str(text or "").strip()
        ]
    return out


def _sync_line(ti: TripItem, c: Ctx) -> str:
    """Where a flight stands with the flight log, while one is kept; '' for anything else."""
    if not c.flight_log or ti.kind != "flight" or ti.sync_state not in SYNC_LINE:
        return ""
    if ti.sync_state == "pending" and c.dry_run:
        return DRY_RUN_LINE
    return SYNC_LINE[ti.sync_state]


def present(dl: fmt.DayLine, items: list[TripItem], c: Ctx) -> dict:
    """One line of the page: the booking `dl.item` as it shows on its day (`dl.role`: start,
    end or single; a booking with no date shows no time), with what is to check on it
    (`checks`: each open check's code, sentence and whether it is hard), the answers that apply
    (`can`, `checks.actions`), the sentence of a flight deleted in the flight log (`gone`) and
    where a flight stands with the flight log (`sync_line`). The second line of a stay or a car
    (its end) carries none of these: the first says them."""
    ti, role, p = dl.item, dl.role, dl.item.payload
    # where you are at a time; a booking with no date has no time
    zone = lambda when: grouping.zone_at(items, when, c.home) if when else None  # noqa: E731
    people = fmt.identities([n for n in ti.passengers if n], c.book)
    names = ", ".join(d for d, _ in people)
    out: dict = {
        "id": ti.id,
        "kind": ti.kind,
        "role": role,  # start | end | single — the end is a hotel's check-out / a car's drop-off
        "icon": ICON.get(ti.kind, ICON["other"]),
        "cancelled": ti.status == "cancelled",
        "time": "",
        "tz": "",
        "primary": "",
        "secondary": "",
        "lines": [],
        "conf": ti.confirmation,
        "names": names,
        "seats": "",
        "maps_url": None,
        "track_url": None,
        "text": "\n".join(fmt.item_lines(ti, role, items=items, book=c.book, home_airports=c.home)),
    }
    if ti.kind in ("flight", "train"):
        tz0 = ti.start_tz if ti.kind == "flight" else zone(ti.start_at)
        tz1 = ti.end_tz if ti.kind == "flight" else zone(ti.end_at or ti.start_at)
        out["time"], out["tz"] = fmt.time_parts(ti.start_at, tz0)
        out["primary"] = f"{p.get('origin') or '?'} → {p.get('destination') or '?'}"
        if ti.kind == "flight":
            code = f"{p.get('carrier') or ''} {p.get('flight_number') or ''}".strip()
            out["secondary"] = f"{code} ({fmt._airline(p)})" if code else fmt._airline(p)
            if p.get("flight_code"):
                out["track_url"] = f"https://flightaware.com/live/flight/{p['flight_code']}"
        else:
            out["secondary"] = (
                f"{p.get('provider') or 'Train'} {p.get('train_number') or ''}".strip()
            )
        arr = fmt.fmt_time(ti.end_at, tz1)
        if arr:
            if ti.end_at and ti.start_at and ti.end_at[:10] != ti.start_at[:10]:
                arr += ", " + fmt.day_label(dt.date.fromisoformat(ti.end_at[:10]))
            out["lines"].append({"text": f"arrives {arr}"})
        seats = fmt._seats(ti, people)
        out["seats"] = ", ".join(seats)
    elif ti.kind == "lodging":
        out["primary"] = p.get("property_name") or p.get("provider") or "Hotel"
        if role == "end":
            out["secondary"] = "Check out"
            out["time"], out["tz"] = fmt.time_parts(ti.end_at, zone(ti.end_at or ti.start_at))
        else:
            out["secondary"] = "Check in"
            out["time"], out["tz"] = fmt.time_parts(ti.start_at, zone(ti.start_at))
            if p.get("address"):
                out["maps_url"] = displaymod.maps_url(c.display, str(p["address"]))
                out["lines"].append({"text": str(p["address"]), "url": out["maps_url"]})
    elif ti.kind == "car":
        out["primary"] = p.get("provider") or "Rental car"
        pickup, dropoff = p.get("pickup_location") or "", p.get("dropoff_location") or ""
        if role == "end":
            out["secondary"] = "Drop off"
            out["time"], out["tz"] = fmt.time_parts(ti.end_at, zone(ti.end_at or ti.start_at))
            where = dropoff if dropoff and dropoff != pickup else ""
        else:
            out["secondary"] = "Pick up"
            out["time"], out["tz"] = fmt.time_parts(ti.start_at, zone(ti.start_at))
            where = pickup
        if where:
            out["maps_url"] = displaymod.maps_url(c.display, where)
            out["lines"].append({"text": where, "url": out["maps_url"]})
    else:
        out["primary"] = p.get("provider") or ti.kind
    out.update(checks=[], can=frozenset(), gone="", sync_line="")
    if role == "end":  # the second appearance of a hotel/car: no conf/names/marks again
        out["conf"], out["names"] = None, ""
        return out
    out["checks"] = [
        {"code": code, "text": checks.LABELS.get(code, code), "hard": code in checks.HARD}
        for code in ti.checks
    ]
    out["can"] = checks.actions(ti.checks, hold=ti.sync_hold, flight_log=c.flight_log)
    out["gone"] = GONE if "send_again" in out["can"] else ""
    out["sync_line"] = _sync_line(ti, c)
    return out


def _bare(ti: TripItem, role: str = "single") -> dict:
    """A booking the page cannot present (a stored field it cannot read): its kind and, on its
    first line, its code, with nothing to check and no answer, so one bad booking never breaks
    the page it is on."""
    kind = EDIT_WHAT.get(ti.kind, ti.kind).capitalize()
    code = ti.confirmation if role != "end" else None
    return {
        "id": ti.id,
        "kind": ti.kind,
        "role": role,
        "icon": ICON.get(ti.kind, ICON["other"]),
        "cancelled": ti.status == "cancelled",
        "time": "",
        "tz": "",
        "primary": kind,
        "secondary": "",
        "lines": [],
        "conf": code,
        "names": "",
        "seats": "",
        "maps_url": None,
        "track_url": None,
        "text": kind + (f" · conf {code}" if code else ""),
        "checks": [],
        "can": frozenset(),
        "gone": "",
        "sync_line": "",
    }


def _line(dl: fmt.DayLine, items: list[TripItem], c: Ctx) -> dict:
    """`present`, or the booking bare when it cannot be presented (logged)."""
    try:
        return present(dl, items, c)
    except Exception:  # noqa: BLE001 - one booking must not hide the rest
        log.exception("booking #%s cannot be shown", dl.item.id)
        return _bare(dl.item, dl.role)


def _readable(c: Ctx, items: list[TripItem]) -> set[int]:
    """The ids of the bookings whose text can be made from their stored fields; each one that
    cannot is logged, so one bad booking never breaks a page, a text or a calendar."""
    shown: set[int] = set()
    for ti in items:
        try:
            for role in ("start", "end"):
                fmt.item_lines(ti, role, items=items, book=c.book, home_airports=c.home)
        except Exception:  # noqa: BLE001 - one booking must not hide the rest
            log.exception("booking #%s cannot be shown", ti.id)
        else:
            shown.add(ti.id)
    return shown


def _text_of(c: Ctx, items: list[TripItem], selected: set[int] | None) -> set[int]:
    """What a trip's text takes: the bookings `selected` (by default every one not cancelled)
    whose text can be made; one that cannot is left out."""
    shown = _readable(c, items)
    if selected is None:
        selected = {ti.id for ti in items if ti.status != "cancelled"}
    return selected & shown


def _days(c: Ctx, items: list[TripItem]) -> list[dict]:
    """The trip's days, each with its lines. A booking whose text cannot be made from its stored
    fields is shown bare on its first day (after that day's other lines), so the rest of the
    trip is still shown."""
    shown = _readable(c, items)  # the page shows cancelled items too (struck through)
    bare = [ti for ti in items if ti.id not in shown]
    by_day = {
        day: [_line(dl, items, c) for dl in lines]
        for day, lines in fmt.group_by_day(
            items, book=c.book, home_airports=c.home, selected=shown
        )
    }
    for ti in bare:
        by_day.setdefault(dt.date.fromisoformat(ti.start_at[:10]), []).append(_bare(ti))
    return [
        {"date": day, "band": fmt.day_band(day), "lines": lines}
        for day, lines in sorted(by_day.items())
    ]


def _unsorted(c: Ctx) -> list[dict]:
    """The bookings with no date, each as a trip page shows a booking (with no time)."""
    out = []
    for ti in c.store.unsorted():
        role = "single" if ti.kind in ("flight", "train") else "start"
        out.append(_line(fmt.DayLine(ti, role, []), [ti], c))
    return out


# ----------------------------------------------------------------------------- routes
def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


def manifest(request: Request) -> Response:
    """The web app manifest: the page's name, its start and scope the base, and its icons."""
    urls = _urls(request)
    body = {
        "name": "tripbox",
        "short_name": "Trips",
        "start_url": urls.path("/"),
        "scope": urls.path("/"),
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [
            {
                "src": urls.path("/static/apple-touch-icon.png"),
                "sizes": "180x180",
                "type": "image/png",
            },
            {"src": urls.path("/static/favicon.svg"), "sizes": "any", "type": "image/svg+xml"},
        ],
    }
    return JSONResponse(body, media_type="application/manifest+json")


def worker(request: Request) -> Response:
    """The service worker's script, named for the base and the release (`app.state.version`),
    asked with and without the base alike: the one that keeps saved copies (`sw.js`), or, while
    they are off, one that removes them and unregisters itself (`sw_off.js`). `Headers` sends it
    `no-cache`, so a browser checks for a new release on every update. `files` are what every
    saved copy needs to show as itself, at the addresses the pages ask for them (the stylesheet
    and the script with the release's version, each font as the stylesheet names it), which the
    worker keeps when it is installed."""
    state = request.app.state
    name = "sw.js" if state.settings.offline else "sw_off.js"
    fonts = sorted(p.name for p in (STATIC / "fonts").glob("*.woff2"))
    files = [
        f"/static/app.css?v={state.version}",
        f"/static/app.js?v={state.version}",
        *(f"/static/fonts/{font}" for font in fonts),
    ]
    return state.templates.TemplateResponse(
        request, name, {"version": state.version, "files": files}, media_type="text/javascript"
    )


def index(request: Request) -> Response:
    """The list: Unsorted, Upcoming and the past trips. A ledger that holds no trip, no booking
    and nothing deleted is a first run: what to do first, then the status card. Past one, while
    no worker runs the list starts by saying so, and an empty Upcoming says where to forward a
    confirmation while a worker reads mail."""
    with _ctx(request) as c:
        today = today_local()
        clock = util.now()
        now = clock.astimezone().replace(tzinfo=None)  # wall time in the process zone
        record = worker_status.read(c.ledger)
        marked = c.store.flag_counts()
        covers = c.store.covers.all()  # every card's, in one pass
        upcoming = [
            card(
                c, t, now=now, with_next=True, to_check=marked.get(t.id, 0), cover=covers.get(t.id)
            )
            for t in c.store.upcoming(today)
        ]
        past_all = c.store.past(today, limit=None)
        show_all = request.query_params.get("past") == "all"
        past: list[dict] = []
        prev_year = today.year
        for t in past_all if show_all else past_all[:PAST_LIMIT]:
            cd = card(
                c, t, now=now, with_next=False, to_check=marked.get(t.id, 0), cover=covers.get(t.id)
            )
            cd["year_header"] = str(cd["year"]) if cd["year"] != prev_year else ""
            prev_year = cd["year"]
            past.append(cd)
        unsorted = _unsorted(c)
        deleted_count = sum(c.store.deleted_counts())
        first_run = not (upcoming or past_all or unsorted or deleted_count)
        page = _render(
            request,
            "trips.html",
            {
                "upcoming": upcoming,
                "past": past,
                "more_past": len(past_all) > PAST_LIMIT and not show_all,
                "unsorted": unsorted,
                # the flight log, linked only while one is kept and its address is set
                "flight_log_url": c.settings.airtrail_public_url if c.flight_log else "",
                "said": _said(c),
                "seen": int(clock.timestamp()),  # when the page was rendered
                "deleted_count": deleted_count,
                "flash": _flash(request),
                "first_run": first_run,
                "status_rows": statuscard.rows(record, clock) if first_run else [],
                # a first run's card says it in its own row
                "no_worker": not first_run and not worker_status.live(record, clock),
                "forward_to": statuscard.mailbox(record, clock),
            },
        )
        return _keepable(request, page)


def _trip_options(trips: list[Trip]) -> list[dict]:
    """Trips as a select offers them: newest first, each with its dates."""
    return [
        {"id": o.id, "title": o.title, "range": fmt.date_range(o.start_date, o.end_date)}
        for o in sorted(trips, key=lambda x: x.start_date, reverse=True)
    ]


def trip(request: Request) -> Response:
    """A trip's page: its photo or tile, its notes, its days and their bookings, and its ⋯ sheet
    (the place and the photo, which finds one too while automatic covers are on)."""
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        items = c.store.items(t.id)
        others = _trip_options([o for o in c.store.all_trips() if o.id != t.id])
        page = _render(
            request,
            "trip.html",
            {
                "trip": t,
                "photo": _photo(c.store.covers.get(t.id)),
                "find_photo": c.settings.covers == "wikimedia",
                "range": fmt.date_range(t.start_date, t.end_date),
                "days": _days(c, items),
                "people": _people(items, c.book),
                "others": others,
                "empty": not items,
                "to_check": c.store.flag_counts().get(t.id, 0),
                "said": _said(c),
                "seen": int(util.now().timestamp()),  # when the page was rendered
                "flash": _flash(request),
            },
        )
        return _keepable(request, page)


def deleted(request: Request) -> Response:
    """Recently deleted: trips and bookings the owner put away, restorable for 30 days."""
    with _ctx(request) as c:
        trips = [
            {
                "trip": t,
                "range": fmt.date_range(t.start_date, t.end_date),
                "when": _day(t.deleted_at),
            }
            for t in c.store.deleted_trips()
        ]
        items = []
        for ti, t in c.store.removed_items():
            items.append(
                {
                    "id": ti.id,
                    "line": _one_line(c, ti),  # bare when it cannot be shown
                    # a booking with no date has no day to show
                    "date": fmt.day_label(dt.date.fromisoformat(ti.start_at[:10]))
                    if ti.start_at
                    else "",
                    "trip": t.title if t else "",
                    "when": _day(ti.removed_at),
                }
            )
        return _render(
            request,
            "deleted.html",
            {"trips": trips, "items": items, "said": _said(c)},
        )


def _text_options(request: Request) -> dict:
    q = request.query_params
    selected = None
    if "items" in q:
        ids = (_id(x) for x in q["items"].split(","))
        selected = {i for i in ids if i is not None}
    flag = lambda k: q.get(k, "1") not in ("0", "false", "no")  # noqa: E731
    return {
        "selected": selected,
        "include_conf": flag("conf"),
        "include_names": flag("names"),
        "include_notes": flag("notes"),
    }


def trip_text(request: Request) -> Response:
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        items = c.store.items(t.id)
        options = _text_options(request)
        options["selected"] = _text_of(c, items, options["selected"])
        text = fmt.trip_text(t, items, book=c.book, home_airports=c.home, **options)
        return PlainTextResponse(text)


def link_rows(c: Ctx, request: Request, trip_id: int | None = None) -> list[dict]:
    """The share links that work today (of the trip `trip_id`, or of every trip), oldest first,
    as the share page and Settings list them: what each shows, the last day it works, its whole
    address and its trip."""
    urls = _urls(request)
    rows = []
    for link, trip, until in shares.live(c.ledger, c.store, today_local(), trip_id):
        rows.append(
            {
                "id": link.id,
                "trip": trip,
                "shows": ", ".join(LINK_LABEL[f] for f in link.shows),
                "until": fmt.date_range(until.isoformat(), until.isoformat()),
                "url": urls.absolute("/s/" + link.token),
            }
        )
    return rows


def share(request: Request) -> Response:
    """The share page: the trip's text to pick and copy, then its share links (a form to make
    one, the boxes of `shares.DEFAULT` ticked, and each link that works)."""
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        items = c.store.items(t.id)
        days = _days(c, items)
        for d in days:  # the end-of-stay line of a hotel/car is not a separate thing to pick
            d["lines"] = [ln for ln in d["lines"] if ln["role"] != "end"]
        days = [d for d in days if d["lines"]]  # a check-out-only day has nothing to pick
        order = grouping.KIND_ORDER
        kinds = sorted({ti.kind for ti in items}, key=lambda k: order.index(k) if k in order else 9)
        return _render(
            request,
            "share.html",
            {
                "trip": t,
                "days": days,
                "chips": [("all", "All")] + [(k, KIND_LABEL.get(k, k.title())) for k in kinds],
                "text": fmt.trip_text(
                    t,
                    items,
                    book=c.book,
                    home_airports=c.home,
                    selected=_text_of(c, items, None),
                ),
                "link_fields": [
                    {"name": f, "label": LINK_LABEL[f], "on": f in shares.DEFAULT}
                    for f in shares.FIELDS
                ],
                "links": link_rows(c, request, t.id),
            },
        )


def trip_ics(request: Request) -> Response:
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        body = ical.trip_ics(t, c.store.items(t.id), book=c.book, home_airports=c.home)
        slug = re.sub(r"[^a-z0-9]+", "-", t.title.lower()).strip("-") or "trip"
        return Response(
            body,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": f'inline; filename="{slug}.ics"'},
        )


def cover_image(request: Request) -> Response:
    """A cover photo by its file name (`middleware.COVER`: the picture's SHA-256, `-s` for the
    small size), the worker's JPEG as the ledger holds it, never decoded here: only a ready
    cover's, of a trip that is not deleted. Any other name, and one no shown cover holds, is the
    404 page. `Headers` gives the photo its long `Cache-Control`, and nothing else under it."""
    named = COVER.fullmatch("/covers/" + request.path_params["file"])
    if named is None:
        raise HTTPException(404, "no such photo")
    with _ctx(request) as c:
        data = c.store.covers.shown_image(named[1], "small" if named[2] else "large")
    if data is None:
        raise HTTPException(404, "no such photo")
    return Response(data, media_type="image/jpeg")


def feed(request: Request) -> Response:
    """All trips for a subscribed calendar. Auth-exempt (the app's own token guards it); a wrong
    token is a bare 404, never a redirect, so a calendar app shows an error instead of storing
    Access's login page as 'the calendar'."""
    with _ctx(request) as c:
        given = request.path_params.get("token") or ""
        good = c.ledger.kv.get(ical.KV_FEED_TOKEN) or ""
        # compare_digest wants ASCII; a token with other characters is simply not ours
        if not given or not good or not given.isascii() or not secrets.compare_digest(given, good):
            return Response(status_code=404)
        body = ical.feed_ics(c.store, book=c.book, home_airports=c.home)
        return Response(body, media_type="text/calendar; charset=utf-8")


async def _form(request: Request) -> dict:
    return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}


def _urls(request: Request) -> Urls:
    return request.app.state.urls


def _go(request: Request, path: str) -> Response:
    """The redirect after a form to `path`, written from `/`: a path under the page's base,
    never an absolute address."""
    return RedirectResponse(_urls(request).path(path), 303)


def _back(request: Request, trip_id: int, flash: str | None = None) -> Response:
    return _go(request, f"/trips/{trip_id}" + (f"?f={flash}" if flash else ""))


def _home(request: Request, flash: str | None = None) -> Response:
    return _go(request, "/" + (f"?f={flash}" if flash else ""))


async def rename(request: Request) -> Response:
    form = await _form(request)
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        c.store.rename(t.id, form.get("title", ""))
        log.info("rename trip #%s -> %r", t.id, form.get("title", "")[:80])
        return _back(request, t.id)


async def notes(request: Request) -> Response:
    form = await _form(request)
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        c.store.set_notes(t.id, form.get("notes", ""))
        log.info("notes trip #%s (%d chars)", t.id, len(form.get("notes", "")))
        return _back(request, t.id)


async def merge(request: Request) -> Response:
    form = await _form(request)
    with _ctx(request) as c:
        src = _trip_or_404(c, request.path_params["trip_id"])
        into = _id(form.get("into"))
        if into is None:
            raise HTTPException(400, "into must be a trip id")
        dst = _trip_or_404(c, into)
        if dst.id != src.id:
            c.store.merge(src.id, dst.id)
            log.info("merge trip #%s into #%s", src.id, dst.id)
        return _back(request, dst.id)


async def remove_item(request: Request) -> Response:
    """Drop a mis-filed booking from its trip (the ledger log and AirTrail are untouched)."""
    await _form(request)  # a well-formed POST body, even though nothing in it is needed
    with _ctx(request) as c:
        ti = _item_or_404(c, str(request.path_params["item_id"]))
        trip = c.store.remove_item(ti.id)
        log.info("remove item #%s (%s %s) from trip #%s", ti.id, ti.kind, ti.item_key, ti.trip_id)
        if trip is None:  # the booking had no trip, or its trip is now empty
            return _home(request, "removed")
        return _back(request, trip.id, "removed")


async def delete_trip(request: Request) -> Response:
    """Hide the whole trip (restorable for 30 days). AirTrail and the email log keep theirs."""
    await _form(request)
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        c.store.delete_trip(t.id)
        log.info("delete trip #%s %r", t.id, t.title)
        return _home(request, "deleted")


async def restore_trip(request: Request) -> Response:
    await _form(request)
    with _ctx(request) as c:
        tid = request.path_params["trip_id"]
        try:
            trip = c.store.restore_trip(tid if 0 < tid <= MAX_ID else -1)
        except KeyError:
            raise HTTPException(404, "no such deleted trip") from None
        log.info("restore trip #%s", tid)
        if trip is None:
            return _home(request, "restored")
        return _back(request, trip.id, "restored")


async def restore_item(request: Request) -> Response:
    await _form(request)
    with _ctx(request) as c:
        iid = _id(str(request.path_params["item_id"])) or -1
        try:
            trip = c.store.restore_item(iid)
        except KeyError:
            raise HTTPException(404, "no such removed item") from None
        if trip is None:  # a booking with no date comes back with no trip
            log.info("restore item #%s with no trip", iid)
            return _home(request, "restored")
        log.info("restore item #%s into trip #%s", iid, trip.id)
        return _back(request, trip.id, "restored")


def _split_local(value) -> tuple[str, str]:
    """'2027-06-22T15:00' → ('2027-06-22', '15:00'); '2027-06-22' → ('2027-06-22', '')."""
    s = grouping.local_str(str(value)) if value else None
    if not s:
        return "", ""
    return (s[:10], s[11:16]) if len(s) > 10 else (s, "")


def _edit_values(ti: TripItem) -> dict:
    """The booking's fields as its edit form shows them, by the form's names: text as held, each
    time as a date and a time of day, the travellers one name per line."""
    f = {}
    for k in EDIT_TEXT[ti.kind]:
        value = editable_value(ti, k)
        f[k] = "" if value is None else str(value)
    for field, prefix in EDIT_TIMES[ti.kind]:
        f[f"{prefix}_date"], f[f"{prefix}_time"] = _split_local(editable_value(ti, field))
    if ti.kind in EDIT_NAMES:
        f["passengers"] = "\n".join(editable_value(ti, "passengers"))
    return f


def _edit_fields(kind: str, form: dict) -> dict:
    """Form → store fields: a date plus an optional time are one local stamp, and a time with no
    date is sent as `T<time>`, which reads as no date and the store refuses; the travellers one
    name per line. Only posted keys count, so a partial form never clears what it did not
    show."""
    out = {k: form[k] for k in EDIT_TEXT[kind] if k in form}
    for field, prefix in EDIT_TIMES[kind]:
        if f"{prefix}_date" in form or f"{prefix}_time" in form:
            d = form.get(f"{prefix}_date", "").strip()
            t = form.get(f"{prefix}_time", "").strip()
            out[field] = f"{d}T{t}" if t else d
    if kind in EDIT_NAMES and "passengers" in form:
        out["passengers"] = form["passengers"].splitlines()
    return out


def _shown(ti: TripItem) -> str:
    """A short digest of the booking's fields as its edit form shows them: a save from a form
    whose digest is not the booking's as it now stands is refused."""
    text = json.dumps(_edit_values(ti), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _seen(value: str | None) -> str:
    """When the form was shown, from the seconds since the epoch it carries, as the ledger
    writes a time and never later than now (a look never moves back, so a time ahead would pass
    what is raised until then unseen); '' when it is no number of plain digits."""
    s = (value or "").strip()
    if not (s.isascii() and s.isdecimal() and len(s) <= 18):
        return ""
    try:
        when = dt.datetime.fromtimestamp(int(s), dt.UTC)
    except (OverflowError, OSError, ValueError):
        return ""
    return min(when, util.now()).isoformat(timespec="seconds")


def _refused_field(kind: str, field: str | None) -> str | None:
    """The form field a refusal of the store's field `field` is about: a time's date, else the
    field of that name."""
    return {f: f"{prefix}_date" for f, prefix in EDIT_TIMES[kind]}.get(field or "", field)


def _booking_form(
    request: Request,
    kind: str,
    values: dict,
    *,
    error: str | None = None,
    refused: str | None = None,
    status: int = 200,
    **context,
) -> Response:
    """edit.html, the one form a booking of `kind` is edited and added by, showing `values` by
    the form's names, with the sentence of a refusal tied to the form field `refused` it is
    about, and what the caller says of the page (`heading`, `action`, `button`, `back`,
    `back_label`, and the checks, digest and trips it shows)."""
    return _render(
        request,
        "edit.html",
        {
            "kind": kind,
            "f": values,
            "error": error,
            "refused": refused,
            "checks": [],
            "invalid": {},
            **context,
        },
        status,
    )


def _edit_page(
    request: Request,
    ti: TripItem,
    values: dict,
    *,
    error: str | None = None,
    refused: str | None = None,
    status: int = 200,
) -> Response:
    """The edit form of the booking `ti` showing `values`: its open checks above the fields, each
    hard one tied to its field; the sentence of a refusal, tied to the form field `refused` it
    is about; and, hidden, the digest of the booking as it stands and the time it was shown."""
    return _booking_form(
        request,
        ti.kind,
        values,
        error=error,
        refused=refused,
        status=status,
        heading=f"Edit {EDIT_WHAT[ti.kind]}",
        action=f"/items/{ti.id}/edit",
        button="Save",
        back=ti.page if ti.trip_id is not None else "/#unsorted",
        back_label="Back to the trip" if ti.trip_id is not None else "Back to the list",
        checks=[{"code": c, "text": checks.LABELS.get(c, c)} for c in ti.checks],
        invalid={CHECK_FIELD[c]: c for c in ti.checks if c in CHECK_FIELD},
        shown=_shown(ti),
        seen=int(util.now().timestamp()),
    )


class _Changed(Exception):
    """The booking is no longer as the form showed it: `item` is how it stands now."""

    def __init__(self, item: TripItem) -> None:
        super().__init__(item.id)
        self.item = item


async def edit_item(request: Request) -> Response:
    """GET the form for any booking; POST the corrections (owner edits outlive older emails).
    The booking is read again, compared with what the form showed and written under one lock:
    when it changed since the form was shown (an email, another tab), nothing is stored and the
    form shows it as it is now (409). A save the store refuses shows what was typed and why
    (400). A saved booking is shown in its trip, or in the list's unsorted group."""
    with _ctx(request) as c:
        ti = _item_or_404(c, str(request.path_params["item_id"]))
        if ti.kind not in EDIT_WHAT:
            raise HTTPException(404, "this booking is not edited here")
        if request.method != "POST":
            return _edit_page(request, ti, _edit_values(ti))
        form = await _form(request)
        try:
            with c.ledger.transaction():
                now = _item_or_404(c, str(ti.id))
                if form.get("shown") != _shown(now):
                    raise _Changed(now)
                saved = c.store.edit_item(
                    ti.id, _edit_fields(now.kind, form), seen=_seen(form.get("seen"))
                )
        except _Changed as changed:
            current = changed.item
            return _edit_page(request, current, _edit_values(current), error=STALE_FORM, status=409)
        except ValueError as exc:
            current = c.store.visible(ti.id) or ti
            typed = {**_edit_values(current), **form}
            field = _refused_field(ti.kind, getattr(exc, "field", None))
            return _edit_page(request, current, typed, error=str(exc), refused=field, status=400)
        log.info("edit item #%s (%s %s)", ti.id, ti.kind, ti.item_key)
        if saved.trip_id is None:  # no date: in the list's unsorted group
            return _go(request, "/?f=saved#unsorted")
        return _go(request, f"/trips/{saved.trip_id}?f=saved#item-{saved.id}")


async def save_place(request: Request) -> Response:
    """The city the trip is about, as the owner typed it (empty: none): bookings there on nearby
    dates join this trip, and a new one takes an automatic photo away (`set_place`). The trip
    keeps its title. While automatic covers are on, a new place that leaves the trip with no
    photo of the owner's is looked up by the worker, and the toast says so."""
    form = await _form(request)
    with _ctx(request) as c:
        t = _trip_or_404(c, request.path_params["trip_id"])
        trip = c.store.set_place(t.id, form.get("place", ""))
        log.info("place trip #%s -> %r", t.id, (trip.place or "")[:80])
        looked_up = (
            c.settings.covers == "wikimedia"
            and bool(trip.place)
            and trip.place != t.place
            and c.store.covers.get(t.id) is None
        )
        return _back(request, t.id, "place_photo" if looked_up else "place")


async def set_status(request: Request) -> Response:
    """Mark a booking cancelled or confirmed by hand. The page sends nothing: the next sync pass
    tells the flight log of a flight it holds."""
    form = await _form(request)
    with _ctx(request) as c:
        ti = _item_or_404(c, str(request.path_params["item_id"]))
        status = form.get("status", "")
        if status not in ("cancelled", "confirmed"):
            raise HTTPException(400, "status must be cancelled or confirmed")
        c.store.set_status(ti.id, status)
        log.info("mark item #%s (%s %s) %s", ti.id, ti.kind, ti.item_key, status)
        return _go(request, ti.page)


def _to_item(request: Request, ti: TripItem, flash: str) -> Response:
    """Back to the booking with the toast `flash`: its trip's page at the booking, or the list's
    unsorted group for a booking with no trip."""
    if ti.trip_id is None:
        return _go(request, f"/?f={flash}#unsorted")
    return _go(request, f"/trips/{ti.trip_id}?f={flash}#item-{ti.id}")


def _applies(c: Ctx, ti: TripItem, action: str) -> bool:
    """Whether the answer `action` applies to the booking as it stands now (`checks.actions`, the
    rule its box was drawn by): one from a page rendered before the booking changed does
    nothing."""
    return action in checks.actions(ti.checks, hold=ti.sync_hold, flight_log=c.flight_log)


async def looks_right(request: Request) -> Response:
    """Looks right, from a booking's box: a look at the time the page was rendered (the form's
    `seen`, never later than now), so a check raised since stays open. A form that does not say
    when stamps nothing. That form, an answer that no longer applies and a look that closes
    nothing (all that is open was raised since) each come back with the toast that the page was
    out of date."""
    form = await _form(request)
    with _ctx(request) as c:
        with c.ledger.transaction():
            ti = _item_or_404(c, str(request.path_params["item_id"]))
            at = _seen(form.get("seen"))
            if not at or not _applies(c, ti, "looks_right"):
                return _to_item(request, ti, "stale")
            after = c.store.looks_right(ti.id, at=at)
        if set(ti.checks) <= set(after.checks):
            return _to_item(request, after, "stale")
        log.info("looks right item #%s (%s %s)", ti.id, ti.kind, ti.item_key)
        return _to_item(request, after, "looked")


async def owner_answer(request: Request) -> Response:
    """The owner's answer, from a flight's box, to whether they were on it (`on_it`: yes or no;
    anything else is refused, 400). It acts only while nobody is named on the flight; a yes with
    no owner in the travellers file changes nothing and says so."""
    form = await _form(request)
    answer = form.get("on_it", "")
    with _ctx(request) as c:
        ti = _item_or_404(c, str(request.path_params["item_id"]))
        if answer not in ("yes", "no"):
            raise HTTPException(400, "on_it must be yes or no")
        try:
            with c.ledger.transaction():
                ti = _item_or_404(c, str(ti.id))
                if not _applies(c, ti, "on_it"):
                    return _to_item(request, ti, "stale")
                after = c.store.owner_answer(ti.id, answer == "yes")
        except ValueError:  # the store's refusal: no owner is named to put on the flight
            return _to_item(request, ti, "noowner")
        log.info("owner answer item #%s (%s): %s", ti.id, ti.item_key, answer)
        return _to_item(request, after, f"owner_{answer}")


async def send_again(request: Request) -> Response:
    """Send again, from the box of a flight deleted by hand in the flight log: it comes back into
    the reconcile, which the next pass of the worker carries out (the page sends nothing)."""
    await _form(request)
    with _ctx(request) as c:
        with c.ledger.transaction():
            ti = _item_or_404(c, str(request.path_params["item_id"]))
            if not _applies(c, ti, "send_again"):
                return _to_item(request, ti, "stale")
            after = c.store.send_again(ti.id)
        log.info("send again item #%s (%s)", ti.id, ti.item_key)
        return _to_item(request, after, "again")


async def move_item(request: Request) -> Response:
    form = await _form(request)
    with _ctx(request) as c:
        ti = _item_or_404(c, str(request.path_params["item_id"]))
        to = form.get("to", "")
        if ti.start_at is None:
            raise HTTPException(400, "a booking with no date is in no trip until it has one")
        if to == "new":
            trip = c.store.move_item(ti.id, None)
        elif (dst := _id(to)) is not None:
            _trip_or_404(c, dst)
            trip = c.store.move_item(ti.id, dst)
        else:
            raise HTTPException(400, "to must be a trip id or new")
        log.info("move item #%s (%s) from trip #%s to #%s", ti.id, ti.kind, ti.trip_id, trip.id)
        return _back(request, trip.id)


# ----------------------------------------------------------------------------- adding
class _TooLarge(Exception):
    """More of a request's body came than BODY_CAP."""


def _counted(request: Request) -> Request:
    """The request with its body counted as it arrives: the read that brings more than BODY_CAP
    bytes, whatever length the request declared, raises _TooLarge, so the form parser never
    takes in more than that and the rest is never read."""
    receive = request.receive
    came = 0

    async def counting() -> Message:
        nonlocal came
        message = await receive()
        if message["type"] == "http.request":
            came += len(message.get("body", b""))
            if came > BODY_CAP:
                raise _TooLarge
        return message

    return Request(request.scope, counting)


def _declares_too_much(request: Request) -> bool:
    """A request that says its body is longer than BODY_CAP: answered before a byte is read."""
    length = request.headers.get("content-length", "").strip()
    return length.isascii() and length.isdecimal() and int(length) > BODY_CAP


def _add_page(
    request: Request, name: str, *, code: str | None = None, text: str = ""
) -> Response:
    """The paste or the upload form (`name`); with `code`, the refusal's sentence, announced and
    tied to the field, and its status."""
    said = (PASTE_REFUSED if name == "paste.html" else UPLOAD_REFUSED).get(code or "")
    return _render(
        request,
        name,
        {"error": said, "text": text},
        REFUSED_STATUS.get(code or "", 200),
    )


def _take(
    request: Request, form: str, body: bytes, *, name: str | None, typed: str = ""
) -> Response:
    """Store what the form `form` was handed (`uploads.submit`) and go to its status page; a
    refusal shows the form again with its sentence (and what was typed, unless it was too
    large). Who handed it in is who the login says is signed in (`LoginRequired` puts it in the
    request's state)."""
    who = (request.scope.get("state") or {}).get("user")
    with _ctx(request) as c:
        try:
            token, new = uploads.submit(c.ledger, body, name=name, submitted_by=who)
        except uploads.Refused as refused:
            log.info("upload refused: %s (%d bytes)", refused.code, len(body))
            kept = "" if refused.code == "too_large" else typed
            return _add_page(request, form, code=refused.code, text=kept)
    log.info("upload %s (%d bytes)", "stored" if new else "already stored", len(body))
    return _go(request, f"/uploads/{token}")


def _from_trip(c: Ctx, request: Request) -> Trip | None:
    """The trip a page to add a booking was opened from (`?trip=<id>`), when that is a trip that
    is not deleted; None for anything else."""
    trip_id = _id(request.query_params.get("trip"))
    return c.store.get(trip_id) if trip_id else None


def add(request: Request) -> Response:
    """Add a booking: from a confirmation's text or file, or by hand. Opened from a trip
    (`?trip=<id>`), its way back is that trip and each by-hand form starts with that trip."""
    with _ctx(request) as c:
        t = _from_trip(c, request)
    return _render(
        request,
        "add.html",
        {
            "back": f"/trips/{t.id}" if t else "/",
            "query": f"?trip={t.id}" if t else "",
            "by_hand": [
                {"word": word, "icon": ICON[kind], "label": f"A {word}"}
                for word, kind in ADD_KIND.items()
            ],
        },
    )


def _blank(kind: str, owner: str = "") -> dict:
    """The add form of a booking of `kind` with nothing filled in, by the form's names: its
    travellers, where it lists them, start with `owner`; its trip is left to the dates."""
    f = dict.fromkeys(EDIT_TEXT[kind], "")
    for _, prefix in EDIT_TIMES[kind]:
        f[f"{prefix}_date"] = f[f"{prefix}_time"] = ""
    if kind in EDIT_NAMES:
        f["passengers"] = owner
    f["trip"] = ""
    return f


def _add_form(
    request: Request,
    c: Ctx,
    kind: str,
    values: dict,
    t: Trip | None,
    *,
    error: str | None = None,
    refused: str | None = None,
    status: int = 200,
) -> Response:
    """The edit form as it adds a booking of `kind` by hand, showing `values`, opened from the
    trip `t` (or from none): titled Add a <kind>, posting to its own path, with one more field,
    the trip it goes into — the dates decide unless one of the trips, newest first, is chosen."""
    query = f"?trip={t.id}" if t else ""
    trips = c.store.all_trips()
    if t is not None and t.id not in {o.id for o in trips}:
        trips.append(t)  # an emptied trip is listed nowhere else, but the page came from it
    return _booking_form(
        request,
        kind,
        values,
        error=error,
        refused=refused,
        status=status,
        heading=f"Add a {EDIT_WHAT[kind]}",
        action=f"/add/{EDIT_WHAT[kind]}{query}",
        button="Add",
        back=f"/add{query}",
        back_label="Back to Add a booking",
        adding=True,
        trips=_trip_options(trips),
    )


async def add_by_hand(request: Request) -> Response:
    """GET the form for a booking typed in by hand (`/add/<word>`, one of ADD_KIND; any other is
    the 404 page), the travellers of a flight or a train starting with the owner's name when the
    travellers file names one; POST it to the store (`TripStore.add_by_hand`). A booking the
    store refuses shows what was typed and why (400); one it files is shown in its trip, or in
    the list's unsorted group."""
    kind = ADD_KIND.get(request.path_params["kind"])
    if kind is None:
        raise HTTPException(404, "no such kind of booking")
    form = await _form(request) if request.method == "POST" else None
    with _ctx(request) as c:
        t = _from_trip(c, request)
        if form is None:
            values = _blank(kind, c.book.me_name)
            values["trip"] = str(t.id) if t else ""
            return _add_form(request, c, kind, values, t)
        try:
            ti = c.store.add_by_hand(kind, _edit_fields(kind, form), trip_id=_id(form.get("trip")))
        except ValueError as exc:
            typed = {**_blank(kind), **form}
            field = _refused_field(kind, getattr(exc, "field", None))
            return _add_form(
                request, c, kind, typed, t, error=str(exc), refused=field, status=400
            )
        log.info("add item #%s (%s %s) by hand", ti.id, ti.kind, ti.item_key)
        return _to_item(request, ti, "added")


async def add_paste(request: Request) -> Response:
    """GET the paste form; POST its `text`, stored for the worker to read (the page reads none
    of it)."""
    if request.method != "POST":
        return _add_page(request, "paste.html")
    try:
        if _declares_too_much(request):
            raise _TooLarge
        async with _counted(request).form(max_part_size=BODY_CAP) as form:
            text = form.get("text")
    except _TooLarge:
        return _add_page(request, "paste.html", code="too_large")
    text = text if isinstance(text, str) else ""
    # a copied block often begins with an empty line: what is stored starts at its first
    # character, and the form shows back what was typed
    return _take(request, "paste.html", text.lstrip().encode("utf-8"), name=None, typed=text)


async def add_upload(request: Request) -> Response:
    """GET the upload form; POST its `file` (multipart), stored for the worker to read. The body
    is counted as it arrives and refused past BODY_CAP before the form is parsed; what the file
    is comes from its bytes, never from its name or declared type."""
    if request.method != "POST":
        return _add_page(request, "upload.html")
    try:
        if _declares_too_much(request):
            raise _TooLarge
        async with _counted(request).form(max_files=1, max_part_size=BODY_CAP) as form:
            part = form.get("file")
            if isinstance(part, UploadFile):
                body, name = await part.read(), part.filename
            else:
                body, name = b"", None
    except _TooLarge:
        return _add_page(request, "upload.html", code="too_large")
    return _take(request, "upload.html", body, name=name)


async def remove_cover(request: Request) -> Response:
    """Remove photo: the trip goes back to its tile, whatever photo it had or was waiting for,
    and no automatic one is looked up for it until its place changes (`CoverStore.remove`)."""
    await _form(request)
    with _ctx(request) as c, c.ledger.transaction():
        t = _trip_or_404(c, request.path_params["trip_id"])
        c.store.covers.remove(t.id)
    log.info("cover photo of trip #%s removed", t.id)
    return _back(request, t.id, "cover_removed")


async def upload_cover(request: Request) -> Response:
    """The owner's own cover photo for the trip `?trip=<id>` (in the address, so a body refused
    before it is read still knows its trip; one the owner cannot see is the 404 page, before a
    byte is read), its `photo` a multipart file stored as it came for the worker, the one
    reader of a picture, and replacing whatever the trip had. Counted as it arrives and
    refused past BODY_CAP, as an upload is; past 10 MB, as HEIC or as anything but a JPEG, a
    PNG or a WebP by its first bytes (never its name or declared type), the trip's page says
    why and nothing is stored."""
    with _ctx(request) as c:
        t = _trip_or_404(c, _id(request.query_params.get("trip")) or -1)
    try:
        if _declares_too_much(request):
            raise _TooLarge
        async with _counted(request).form(max_files=1, max_part_size=BODY_CAP) as form:
            part = form.get("photo")
            data = await part.read() if isinstance(part, UploadFile) else b""
    except _TooLarge:
        return _back(request, t.id, "cover_big")
    kind = coversniff.sniff(data)
    if len(data) > uploads.MAX_BYTES:
        refused = "cover_big"
    elif kind not in COVER_KINDS:
        refused = "cover_heic" if kind == "heic" else "cover_type"
    else:
        with _ctx(request) as c, c.ledger.transaction():
            _trip_or_404(c, t.id)  # still one the owner can see
            c.store.covers.upload(t.id, data)
        log.info("cover photo for trip #%s stored (%s, %d bytes)", t.id, kind, len(data))
        return _back(request, t.id, "cover_received")
    log.info("cover photo for trip #%s refused: %s (%d bytes)", t.id, refused, len(data))
    return _back(request, t.id, refused)


def _size(n: int) -> str:
    if n < 1024:
        return "1 byte" if n == 1 else f"{n} bytes"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _one_line(c: Ctx, ti: TripItem) -> str:
    """A booking on one line, as Recently deleted lists it; its kind when that cannot be made."""
    try:
        return fmt.item_lines(ti, "start", items=[ti], book=c.book, home_airports=c.home)[0]
    except Exception:  # noqa: BLE001 - one booking must not hide the rest
        log.exception("booking #%s cannot be shown", ti.id)
        bare = _bare(ti)
        return f"{bare['icon']} {bare['text']}"


def _bookings_of(c: Ctx, message_id: int) -> list[dict]:
    """The bookings the message filed, updated or matched, the owner can see, each with where
    the page shows it: its trip at the booking, or the list's Unsorted group."""
    out: list[dict] = []
    seen: set[int] = set()
    for row in c.ledger.items.by_message(message_id):
        item_id = row["trip_item_id"]
        if item_id is None or item_id in seen:
            continue
        seen.add(item_id)
        ti = c.store.visible(item_id)
        if ti is None:
            continue
        href = ti.page if ti.trip_id is not None else "/#unsorted"
        out.append({"href": href, "text": _one_line(c, ti)})
    return out


def upload_status(request: Request) -> Response:
    """One upload's state, named by its token (a token that names none is the 404 page): its
    shown name and size, never its body. While it waits the page reloads itself every few
    seconds (no script); while no worker beats on the ledger, a waiting upload's page says that
    it waits for one."""
    with _ctx(request) as c:
        row = c.ledger.uploads.get(request.path_params["token"])
        if row is None:
            raise HTTPException(404, "no such upload")
        state = row["state"]
        filed = state == "filed" and row["message_id"] is not None
        return _render(
            request,
            "upload_status.html",
            {
                "state": state,
                "waiting": state in ("waiting", "processing"),
                # nothing reads it until a worker beats on the ledger
                "no_worker": state == "waiting"
                and not worker_status.alive(c.ledger, util.now()),
                "label": row["name"] or UPLOAD_KIND[row["kind"]],
                "size": _size(row["size"]),
                "no_model": row["error"] == "no_model",
                # a worker stopped every time it read this: given up, kept
                "stopped": STOPPED if state == "failed" and row["error"] == "stopped" else None,
                # why its message waits for the model, which reads it later
                "waits": uploads.WAITING.get(row["error"]) if state == "nothing" else None,
                "bookings": _bookings_of(c, row["message_id"]) if filed else [],
            },
        )
