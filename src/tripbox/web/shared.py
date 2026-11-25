# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A shared trip's page (`/s/<token>`): a read-only page of one trip that anyone holding its link
opens without signing in, and the owner's two posts that make and revoke a link.

The page is built here, from the stored fields of the trip's bookings, by an allow-list of its
own (`shared_view`): every booking shows its kind, its name or route and the day it is on, and
only what the link was made to show (`trips.shares.FIELDS`) adds anything more. It never goes
through the trip page's presenter, which carries what the owner alone reads (confirmation codes
in copy buttons, booking ids, checks, the flight log). A cancelled or removed booking is not
shown. Every miss under the shared prefix is the same bare 404, whatever the token or the path;
what refuses a request before the page is asked does not depend on the token either: a
cross-site POST gets the same-origin 403, a body over the limit 413, and a method outside
METHODS 405. The page writes nothing and says nothing of the token in a log."""

from __future__ import annotations

import datetime as dt
import logging

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from tripbox import util
from tripbox.trips import format as fmt
from tripbox.trips import grouping, shares
from tripbox.trips.format import ICON
from tripbox.trips.store import Trip, TripItem
from tripbox.util import today_local
from tripbox.web import views

log = logging.getLogger("tripbox.web")

# What every booking of the page holds, and what each field of a link adds to it
ALWAYS = ("icon", "kind", "role", "primary", "secondary")
ADDS = {
    "times": ("time", "tz", "arrives", "track"),
    "addresses": ("lines",),
    "names": ("names",),
    "seats": ("seats",),
    "codes": ("code",),
}
# The methods the shared route takes, each answered as a GET of a wrong link is (any other is
# 405, whatever the token): the page itself only to a GET
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# What the page holds of the trip; `notes` only when the link shows them
TRIP = ("title", "range", "days")
DAY = ("band", "bookings")


def _seats(ti: TripItem, people: list[tuple[str, list[str]]], *, named: bool) -> list[str]:
    """The booking's seats: each with its traveller's name before it when `named`, else bare;
    one printed under a name nobody owns, and one printed with no name, bare."""
    p = ti.payload
    by = {k: v for k, v in (p.get("seat_by_passenger") or {}).items() if v}
    if not by:
        return [str(s) for s in (p.get("seats") or []) if s]
    out: list[str] = []
    owned: set[str] = set()
    for name, variants in people:
        seat = next((by[v] for v in variants if v in by), None)
        owned.update(variants)
        if seat:
            out.append(f"{name} {seat}" if named else str(seat))
    out += [str(seat) for raw, seat in by.items() if raw not in owned]
    return out


def _booking(dl: fmt.DayLine, items: list[TripItem], chosen: set[str], book, home) -> dict:
    """One booking of the page as it shows on its day (`dl.role`): ALWAYS, and what each field in
    `chosen` adds (ADDS). The second line of a stay or a car (its end) names nobody again."""
    ti, role, p = dl.item, dl.role, dl.item.payload

    def zone(when: str | None) -> str | None:
        return grouping.zone_at(items, when, home) if when else None

    out: dict = {
        "icon": ICON.get(ti.kind, ICON["other"]),
        "kind": ti.kind,
        "role": role,
        "primary": "",
        "secondary": "",
    }
    time = tz = arrives = track = ""
    lines: list[str] = []
    if ti.kind in ("flight", "train"):
        tz0 = ti.start_tz if ti.kind == "flight" else zone(ti.start_at)
        tz1 = ti.end_tz if ti.kind == "flight" else zone(ti.end_at or ti.start_at)
        time, tz = fmt.time_parts(ti.start_at, tz0)
        out["primary"] = f"{p.get('origin') or '?'} → {p.get('destination') or '?'}"
        if ti.kind == "flight":
            number = f"{p.get('carrier') or ''} {p.get('flight_number') or ''}".strip()
            airline = fmt._airline(p)
            out["secondary"] = f"{number} ({airline})" if number else airline
            # a link to follow the flight, while it is near or under way, with its times
            if fmt.tracking_now(p, util.now()):
                track = fmt.track_url(p) or ""
        else:
            train = f"{p.get('provider') or 'Train'} {p.get('train_number') or ''}"
            out["secondary"] = train.strip()
        arrives = fmt.fmt_time(ti.end_at, tz1) or ""
        if arrives and ti.end_at and ti.start_at and ti.end_at[:10] != ti.start_at[:10]:
            arrives += ", " + fmt.day_label(dt.date.fromisoformat(ti.end_at[:10]))
    elif ti.kind == "lodging":
        out["primary"] = p.get("property_name") or p.get("provider") or "Hotel"
        if role == "end":
            out["secondary"] = "Check out"
            time, tz = fmt.time_parts(ti.end_at, zone(ti.end_at or ti.start_at))
        else:
            out["secondary"] = "Check in"
            time, tz = fmt.time_parts(ti.start_at, zone(ti.start_at))
            if p.get("address"):
                lines.append(str(p["address"]))
    elif ti.kind == "car":
        out["primary"] = p.get("provider") or "Rental car"
        pickup, dropoff = p.get("pickup_location") or "", p.get("dropoff_location") or ""
        if role == "end":
            out["secondary"] = "Drop off"
            time, tz = fmt.time_parts(ti.end_at, zone(ti.end_at or ti.start_at))
            where = dropoff if dropoff and dropoff != pickup else ""
        else:
            out["secondary"] = "Pick up"
            time, tz = fmt.time_parts(ti.start_at, zone(ti.start_at))
            where = pickup
        if where:
            lines.append(str(where))
    else:
        out["primary"] = p.get("provider") or ti.kind
    first = role != "end"  # who and which code: said once, on the booking's first line
    people = fmt.identities([n for n in ti.passengers if n], book) if first else []
    if "times" in chosen:
        out.update(time=time, tz=tz, arrives=arrives, track=track)
    if "addresses" in chosen:
        out["lines"] = lines
    if "names" in chosen:
        out["names"] = ", ".join(name for name, _ in people)
    if "seats" in chosen:
        seated = first and ti.kind in ("flight", "train")
        out["seats"] = ", ".join(_seats(ti, people, named="names" in chosen)) if seated else ""
    if "codes" in chosen:
        out["code"] = (ti.confirmation or "") if first else ""
    return out


def shared_view(trip: Trip, items: list[TripItem], shows, *, book, home) -> dict:
    """The page of the trip `trip` as a link showing `shows` shows it: its title, its dates and
    its days (TRIP), each day's band and bookings (DAY), each booking as `_booking` builds it,
    and the trip's notes when the link shows them. Cancelled, removed and undated bookings are
    left out, and so is one whose text cannot be made from its stored fields (logged)."""
    chosen = set(shows) & set(shares.FIELDS)
    kept = [
        ti for ti in items if ti.status != "cancelled" and ti.removed_at is None and ti.start_at
    ]
    readable: set[int] = set()
    for ti in kept:
        try:
            for role in ("start", "end"):
                fmt.item_lines(ti, role, items=kept, book=book, home_airports=home)
        except Exception:  # noqa: BLE001 - one booking must not hide the rest
            log.exception("booking #%s cannot be shown", ti.id)
        else:
            readable.add(ti.id)
    days = []
    for day, lines in fmt.group_by_day(kept, book=book, home_airports=home, selected=readable):
        bookings = []
        for dl in lines:
            try:
                bookings.append(_booking(dl, kept, chosen, book, home))
            except Exception:  # noqa: BLE001 - one booking must not hide the rest
                log.exception("booking #%s cannot be shown", dl.item.id)
        if bookings:
            days.append({"band": fmt.day_band(day), "bookings": bookings})
    view: dict = {
        "title": trip.title,
        "range": fmt.date_range(trip.start_date, trip.end_date),
        "days": days,
    }
    if "notes" in chosen:
        view["notes"] = trip.notes or ""
    return view


def _gone() -> Response:
    """What anything but a link that works gets: a 404 with no body (the headers are the
    `Headers` wrapper's, the same for every path under the shared prefix)."""
    return Response(status_code=404)


def shared(request: Request) -> Response:
    """The page a link shows (GET), in the owner's display style; anything else, and any link
    that does not work now, is `_gone`. It writes nothing and logs nothing of the token."""
    if request.method not in ("GET", "HEAD"):
        return _gone()
    token = request.path_params.get("token", "")
    with views._ctx(request) as c:
        found = shares.find(c.ledger, c.store, token, today_local())
        if found is None:
            return _gone()
        link, trip = found
        view = shared_view(trip, c.store.items(trip.id), link.shows, book=c.book, home=c.home)
    return views._render(request, "shared.html", {"view": view})


async def make_link(request: Request) -> Response:
    """A new link to the trip, showing the fields whose boxes the form ticked (named as
    `shares.FIELDS`); back to the trip's share page, at its links."""
    form = await views._form(request)
    with views._ctx(request) as c:
        t = views._trip_or_404(c, request.path_params["trip_id"])
        link = shares.create(c.ledger, t.id, [f for f in shares.FIELDS if f in form])
    log.info("share link #%s made for trip #%s", link.id, t.id)
    return views._go(request, f"/trips/{t.id}/share#links")


async def revoke_link(request: Request) -> Response:
    """The link stops working at once; back to the page the form was on (`from`: Settings, else
    the link's trip's share page). A link that never was is the 404 page."""
    form = await views._form(request)
    link_id = request.path_params["link_id"]
    with views._ctx(request) as c:
        trip_id = c.ledger.shares.revoke(link_id) if 0 < link_id <= views.MAX_ID else None
    if trip_id is None:
        raise HTTPException(404, "no such link")
    log.info("share link #%s revoked", link_id)
    if form.get("from") == "settings":
        return views._go(request, "/settings#links")
    return views._go(request, f"/trips/{trip_id}/share#links")


ROUTES: list[BaseRoute] = [
    Route("/trips/{trip_id:int}/links", make_link, methods=["POST"], name="make_link"),
    Route("/links/{link_id:int}/revoke", revoke_link, methods=["POST"], name="revoke_link"),
]
