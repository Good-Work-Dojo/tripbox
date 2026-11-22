# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Build the tripbox Starlette app, served at TRIPBOX_BASE_URL: its routes are written from `/`
and the outermost wrapper (`BasePath`) takes the base path off each request; then `Headers`,
`BodyLimit` and `SameOrigin` (`middleware`) wrap the app. With no login, `auth.LocalOnly` goes
outside them all. A route is public only when it is named in PUBLIC; every other one sits in the
private mount, behind the login."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route, Router
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

import tripbox
from tripbox.config import ConfigError, Settings
from tripbox.ledger import Ledger
from tripbox.web import auth, settings_page, shared, views
from tripbox.web.middleware import (
    LIMIT,
    SHARED,
    BasePath,
    BodyLimit,
    Headers,
    LoginRequired,
    SameOrigin,
    security_headers,
)
from tripbox.web.urls import Urls

HERE = Path(__file__).parent

# The routes anyone may reach, by name: the page's own and its login's (`Authenticator.routes`).
# The web app manifest and the service worker's script are public: a browser fetches them without
# its cookie, and they hold nothing of the owner's.
PUBLIC = ("healthz", "static", "feed", "shared", "manifest", "worker", "login", "logout")
# Every other route: inside the private mount, so nothing reaches one without the login's say.
PRIVATE: list[BaseRoute] = [
    Route("/", views.index, name="index"),
    Route("/deleted", views.deleted, name="deleted"),
    Route("/add", views.add, name="add"),
    Route("/add/paste", views.add_paste, methods=["GET", "POST"], name="add_paste"),
    Route("/add/upload", views.add_upload, methods=["GET", "POST"], name="add_upload"),
    # after the two above, which the same pattern would otherwise take
    Route("/add/{kind}", views.add_by_hand, methods=["GET", "POST"], name="add_by_hand"),
    Route("/uploads/{token}", views.upload_status, name="upload_status"),
    Route("/trips/{trip_id:int}", views.trip, name="trip"),
    Route("/trips/{trip_id:int}/share", views.share, name="share"),
    Route("/trips/{trip_id:int}/text", views.trip_text, name="trip_text"),
    Route("/trips/{trip_id:int}/trip.ics", views.trip_ics, name="trip_ics"),
    Route("/trips/{trip_id:int}/rename", views.rename, methods=["POST"], name="rename"),
    Route("/trips/{trip_id:int}/notes", views.notes, methods=["POST"], name="notes"),
    Route("/trips/{trip_id:int}/merge", views.merge, methods=["POST"], name="merge"),
    Route("/trips/{trip_id:int}/place", views.save_place, methods=["POST"], name="save_place"),
    Route("/trips/{trip_id:int}/delete", views.delete_trip, methods=["POST"], name="delete_trip"),
    Route(
        "/trips/{trip_id:int}/restore", views.restore_trip, methods=["POST"], name="restore_trip"
    ),
    Route(
        "/items/{item_id:int}/restore", views.restore_item, methods=["POST"], name="restore_item"
    ),
    Route("/items/{item_id:int}/status", views.set_status, methods=["POST"], name="set_status"),
    Route("/items/{item_id:int}/edit", views.edit_item, methods=["GET", "POST"], name="edit_item"),
    Route("/items/{item_id:int}/move", views.move_item, methods=["POST"], name="move_item"),
    Route(
        "/items/{item_id:int}/looks-right", views.looks_right, methods=["POST"], name="looks_right"
    ),
    Route("/items/{item_id:int}/owner", views.owner_answer, methods=["POST"], name="owner_answer"),
    Route(
        "/items/{item_id:int}/send-again", views.send_again, methods=["POST"], name="send_again"
    ),
    Route("/items/{item_id:int}/remove", views.remove_item, methods=["POST"], name="remove_item"),
    *settings_page.ROUTES,
    *shared.ROUTES,
]
# The paste and upload forms read up to views.BODY_CAP and answer past it with their own
# sentence; past this, the body limit answers first.
FORM_LIMITS = {"/add/paste": views.BODY_CAP + LIMIT, "/add/upload": views.BODY_CAP + LIMIT}


def _asset_version(static: Path = HERE / "static", version: str = tripbox.__version__) -> str:
    """The release's own name: the first 12 hex of a hash of the package `version` and every
    static file (the stylesheet, the scripts, the font), by name and content. It is the `?v=` of
    every static link, so a new version of any one busts the phone's (and the edge's) cache, and
    it names the service worker's stores, so a new release replaces the saved copies."""
    h = hashlib.sha256(version.encode("utf-8") + b"\0")
    for path in sorted(p for p in static.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(static)).encode("utf-8") + b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def _not_found(request: Request, exc: HTTPException) -> Response:
    # a calendar app wants nothing but the status; a shared link that is not one learns nothing
    if request.scope.get("path", "").startswith(("/feed/", SHARED)):
        return Response(status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "404.html", {"detail": exc.detail or "not found"}, status_code=404
    )


def _server_error(request: Request, exc: Exception) -> Response:
    """What a request that broke the page gets: one plain sentence, never the error, with the
    security headers itself (it is sent from outside the app's other handlers)."""
    headers = security_headers(request.scope.get("path", ""))
    return PlainTextResponse("Something went wrong.", status_code=500, headers=headers)


def _routes(authenticator: auth.Authenticator) -> list[BaseRoute]:
    """The public routes, then the private mount, last, which takes every path they do not."""
    public: list[BaseRoute] = [
        Route("/healthz", views.healthz, name="healthz"),
        Route("/manifest.webmanifest", views.manifest, name="manifest"),
        Route("/sw.js", views.worker, name="worker"),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        # every path under /feed/: a calendar app gets the feed or a bare 404, never a login
        # page or a redirect
        Mount(
            "/feed",
            app=Router([Route("/{token}.ics", views.feed)], redirect_slashes=False),
            name="feed",
        ),
        # every path under /s/: a shared trip's page or a bare 404 (whatever the method), never
        # the login's challenge, which would put the path in a login page and a log line
        Mount(
            SHARED.rstrip("/"),
            app=Router(
                [Route("/{token}", shared.shared, methods=shared.METHODS)],
                redirect_slashes=False,
            ),
            name="shared",
        ),
        *authenticator.routes(),
    ]
    for route in public:
        if getattr(route, "name", None) not in PUBLIC:
            raise ValueError(f"a public route must be named in PUBLIC: {route!r}")
    private = Mount(
        "",
        app=Router(PRIVATE, redirect_slashes=False),
        name="private",
        middleware=[Middleware(LoginRequired, authenticator=authenticator)],
    )
    return [*public, private]


def create_app(
    settings: Settings,
    ledger_factory: Callable[[], Ledger],
    *,
    owns_ledger: bool = False,
) -> BasePath | auth.LocalOnly:
    """The page over the ledger `ledger_factory` opens (and, when `owns_ledger`, closes again),
    inside its wrappers, outermost first: with no login `auth.LocalOnly` (whatever the path, a
    request that does not name this computer goes no further), then BasePath (whose `state` is
    the app's), Headers, BodyLimit, SameOrigin. A `web` problem of the settings is a ConfigError
    naming each one: nothing serves the page without the login TRIPBOX_AUTH names."""
    problems = [str(p) for p in settings.stopping("web")]
    if problems:
        raise ConfigError("\n".join(problems))
    authenticator = auth.build(settings, ledger_factory, owns_ledger=owns_ledger)
    urls = Urls.of(settings)
    app = Starlette(
        routes=_routes(authenticator),
        exception_handlers={404: _not_found, 500: _server_error},
    )
    app.router.redirect_slashes = False  # no absolute-URL redirects; the feed never redirects
    # a path even the private mount cannot match (no leading `/`) goes to
    # what the mount holds, behind the login, rather than to a 404 of the router's own
    app.router.default = app.routes[-1].app
    app.state.settings = settings
    app.state.urls = urls
    app.state.ledger_factory = ledger_factory
    app.state.owns_ledger = owns_ledger
    app.state.book = settings.travellers
    app.state.authenticator = authenticator  # Settings says how to sign out under it
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["base"] = urls.base  # path-only links, from the setting alone
    app.state.version = _asset_version(version=tripbox.__version__)
    templates.env.globals["asset_v"] = app.state.version
    # pages register the service worker (`app.js`) only while saved copies are on
    templates.env.globals["offline"] = settings.offline
    # the about line's (Settings and the login page): the version and where the source code is
    templates.env.globals["version"] = tripbox.__version__
    templates.env.globals["source_url"] = settings.source_url
    app.state.templates = templates
    guarded = Headers(BodyLimit(SameOrigin(app, origin=urls.origin), limits=FORM_LIMITS))
    page = BasePath(guarded, urls.base)
    return auth.LocalOnly(page) if isinstance(authenticator, auth.NoLogin) else page
