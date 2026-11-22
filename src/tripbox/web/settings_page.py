# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The Settings page, behind the login like every private page: the status card, the display
choices (`trips.display`), the calendar feed's address and a new one for it, the shared links, how
to sign out under the login in use, and the about line. Its three posts save the display
choices, make a new feed address (the old one ends at once) and, with the page's own password,
end every session; each comes from the page itself (`SameOrigin`) and goes back through
`views._go`."""

from __future__ import annotations

import logging

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from tripbox import status as worker_status
from tripbox import util
from tripbox.trips import display, ical
from tripbox.web import card as statuscard
from tripbox.web import views
from tripbox.web.login import PasswordLogin

log = logging.getLogger("tripbox.web")

# Each choice as the form offers it: its legend, then each value's label, in CHOICES's order
CHOICES = {
    "clock": ("Clock", {"12": "12-hour", "24": "24-hour"}),
    "dates": ("Dates", {"mdy": "Month first (Mar 13)", "dmy": "Day first (13 Mar)"}),
    "maps": ("Maps", {"apple": "Apple Maps", "google": "Google Maps", "osm": "OpenStreetMap"}),
}


def _choices(chosen: display.Display) -> list[dict]:
    """The form's three groups: each one's name, legend and options, the one chosen checked."""
    groups = []
    for name, values in display.CHOICES.items():
        legend, labels = CHOICES[name]
        groups.append(
            {
                "name": name,
                "legend": legend,
                "options": [
                    {"value": v, "label": labels[v], "checked": getattr(chosen, name) == v}
                    for v in values
                ],
            }
        )
    return groups


def _link_groups(rows: list[dict]) -> list[dict]:
    """The share links that work, under their trips' titles, each trip once, in the order of its
    first link."""
    groups: dict[int, dict] = {}
    for row in rows:
        trip = row["trip"]
        groups.setdefault(trip.id, {"title": trip.title, "links": []})["links"].append(row)
    return list(groups.values())


def settings(request: Request) -> Response:
    authenticator = request.app.state.authenticator
    with views._ctx(request) as c:
        token = ical.feed_token(c.ledger)
        link_groups = _link_groups(views.link_rows(c, request))
        return views._render(
            request,
            "settings.html",
            {
                "status_rows": statuscard.rows(worker_status.read(c.ledger), util.now()),
                "choices": _choices(c.display),
                "feed_url": views._urls(request).absolute(f"/feed/{token}.ics"),
                "link_groups": link_groups,
                "login": authenticator.name,
                "logout_url": authenticator.logout_url(),
                "flash": views._flash(request),
            },
        )


async def save_display(request: Request) -> Response:
    """The display choices the form posts; one the page does not offer is refused (400) and
    nothing is stored."""
    form = await views._form(request)
    chosen = {name: form[name] for name in display.CHOICES if name in form}
    with views._ctx(request) as c:
        try:
            saved = display.write(c.ledger, **chosen)
        except ValueError:
            raise HTTPException(400, "that choice is not offered") from None
    log.info("display: clock %s, dates %s, maps %s", saved.clock, saved.dates, saved.maps)
    return views._go(request, "/settings?f=saved")


async def new_feed(request: Request) -> Response:
    """A new address for the calendar feed: the old one is a bare 404 from now on."""
    await views._form(request)
    with views._ctx(request) as c:
        ical.feed_token(c.ledger, rotate=True)
    log.info("calendar feed: a new address")
    return views._go(request, "/settings#calendar")


async def end_sessions(request: Request) -> Response:
    """With the page's own password: every session ends, then this browser is signed out. Under
    any other login the page holds no session of its own (404)."""
    authenticator = request.app.state.authenticator
    if not isinstance(authenticator, PasswordLogin):
        raise HTTPException(404, "this page has no sessions of its own")
    await views._form(request)
    return await authenticator.end_everywhere(request)


ROUTES: list[BaseRoute] = [
    Route("/settings", settings, name="settings"),
    Route("/settings/display", save_display, methods=["POST"], name="save_display"),
    Route("/settings/feed", new_feed, methods=["POST"], name="new_feed"),
    Route("/settings/sessions", end_sessions, methods=["POST"], name="end_sessions"),
]
