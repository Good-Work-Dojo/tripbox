# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page's address: one setting (TRIPBOX_BASE_URL) gives its origin and its base path, the
outermost wrapper takes the base off every request, and every link and redirect is built from
the setting, never from the request."""

import datetime as dt
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from helpers.web import ORIGIN, Page, client_for, raw_get, starlette_of
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.staticfiles import StaticFiles
from test_web_auth import PROXY, SIGNED
from test_web_security import MODES, settings_in
from test_web_views import (
    HDR,
    marked_trip,
    save,
    seed_past,
    seed_trip,
    seen_on,
    undated_stay,
)

import tripbox.web
from tripbox.config import RESERVED_SEGMENTS, Settings
from tripbox.ledger import Ledger
from tripbox.notify.text import booking_line, check_card
from tripbox.trips import format as fmt
from tripbox.trips import ical
from tripbox.web.app import create_app
from tripbox.web.middleware import SECURITY_HEADERS
from tripbox.web.urls import Urls

WEB = Path(tripbox.web.__file__).resolve().parent


def test_starlette_routes_on_the_path_alone_once_the_prefix_is_gone(tmp_path):
    """What the base path relies on in the installed Starlette: once `path` and `raw_path` have
    lost the base and `root_path` is empty, a Route, a Mount of static files and a Mount at ""
    of a Router each match on the path alone; that mount hands its routes the whole path and
    runs its middleware for a path none of them takes; a 404 raised inside it is the app's own
    404; and a trailing slash is a 404, never a redirect."""
    (tmp_path / "app.css").write_text("body{}")
    ran: list[str] = []

    class Noted:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            ran.append(scope["path"])
            await self.app(scope, receive, send)

    def seen(request):
        s = request.scope
        return PlainTextResponse(f"{s['path']}|{s['root_path']}|{s['path_params'].get('n', '')}")

    def gone(request):
        raise HTTPException(404)

    def own_404(request, exc):
        return PlainTextResponse("own 404", status_code=exc.status_code)

    inner = Router(
        routes=[Route("/trips/{n:int}", seen), Route("/gone", gone)], redirect_slashes=False
    )
    app = Starlette(
        routes=[
            Route("/healthz", seen),
            Mount("/static", StaticFiles(directory=tmp_path)),
            Mount("", app=inner, middleware=[Middleware(Noted)]),
        ],
        exception_handlers={404: own_404},
    )
    app.router.redirect_slashes = False
    status, _, body = raw_get(app, b"/healthz")
    assert (status, body) == (200, b"/healthz||") and ran == []
    status, _, body = raw_get(app, b"/static/app.css")
    assert (status, body) == (200, b"body{}") and ran == []
    status, _, body = raw_get(app, b"/trips/7")
    assert (status, body) == (200, b"/trips/7||7") and ran == ["/trips/7"]
    for path in ("/nothing", "/gone", "/trips/7/", "/healthz/"):
        status, headers, body = raw_get(app, path.encode())
        assert (status, body) == (404, b"own 404"), path
        assert "location" not in headers, path
        assert ran[-1] == path


# ----------------------------------------------------------------------------- the setting
PROBLEM = (
    "TRIPBOX_BASE_URL: not an address the page can live at — use the address people open, such"
    " as https://trips.example.com or https://example.com/tripbox"
)


def read(value: str) -> Settings:
    return Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_BASE_URL": value})


def problems_of(s: Settings) -> list[str]:
    return [str(p) for p in s.problems if p.key == "TRIPBOX_BASE_URL"]


def test_a_base_url_is_an_address_with_at_most_a_plain_path():
    unset = Settings.from_env({"TRIPBOX_AUTH": "none"})
    assert (unset.base_url, unset.origin, unset.base_path) == (ORIGIN, ORIGIN, "")
    assert unset.link_base is None and unset.problems == ()  # no notice links a local page
    for value, origin, base in (
        ("https://trips.example.com", "https://trips.example.com", ""),
        ("https://trips.example.com/", "https://trips.example.com", ""),
        ("https://example.com/tripbox/", "https://example.com", "/tripbox"),
        ("http://127.0.0.1:8110/a/b", "http://127.0.0.1:8110", "/a/b"),
        ("https://[2001:db8::1]:8443/x.y~z_-1", "https://[2001:db8::1]:8443", "/x.y~z_-1"),
        ("HTTPS://Trips.Example.com/Sample", "https://trips.example.com", "/Sample"),
    ):
        s = read(value)
        assert problems_of(s) == [], value
        assert (s.origin, s.base_path) == (origin, base), value
        assert s.base_url == s.link_base == origin + base, value
    for value in (
        "trips.example.com",
        "ftp://trips.example.com",
        "https://",
        "https:///tripbox",
        "https://trips.example.com/?past=all",
        "https://trips.example.com/tripbox?",
        "https://trips.example.com#top",
        "https://alex:S3CRET@example.com",
        "https://alex@example.com/tripbox",
        "https://trips.example.com//tripbox",
        "https://trips.example.com/a//b",
        "https://trips.example.com//",
        "https://trips.example.com/trip%62ox",
        "https://trips.example.com/a b",
        "https://trips.example.com:99999",
        "https://trips.example.com:x",
        "https://trips.example.com:",
        "https://trips.example.com/a/../b",
        "https://trips.example.com/./a",
        "https://trips.example.com\\tripbox",
        "https://trips.example.com/ü",
    ):
        s = read(value)
        assert problems_of(s) == [PROBLEM], value  # the value is never shown
        assert s.link_base is None and s.base_url == ORIGIN, value
        assert s.feature("web").state == "invalid", value
        assert [p.feature for p in s.problems] == ["web"], value  # it stops the page alone


def test_a_base_path_that_names_a_route_is_refused():
    for segment in RESERVED_SEGMENTS:
        said = (
            f'TRIPBOX_BASE_URL: its path begins with "{segment}", a name the page uses'
            " — choose another path, such as /tripbox"
        )
        for value in (f"https://example.com/{segment}", f"https://example.com/{segment}/more/"):
            s = read(value)
            assert problems_of(s) == [said], value
            assert s.link_base is None and s.base_url == ORIGIN, value
            assert s.feature("web").state == "invalid", value
        deeper = read(f"https://example.com/a/{segment}")  # only the first segment is the page's
        assert problems_of(deeper) == [] and deeper.base_path == f"/a/{segment}"


def first_segments(routes, prefix: str = ""):
    """The first segment of every route's whole path, the routes inside each mount too."""
    for route in routes:
        path = prefix + route.path
        if path:
            yield path.split("/")[1]
        if isinstance(route, Mount):
            yield from first_segments(route.routes, path)


def test_every_route_begins_with_a_reserved_segment():
    """Under every login the page offers, so that a route only one of them adds is checked."""
    for mode in ("none", *MODES):
        app = starlette_of(create_app(settings_in(mode), lambda: Ledger.memory()))
        first = set(first_segments(app.routes))
        assert "" in first, mode  # the list, at the base itself
        assert {"trips", "items", "static", "feed", "covers"} <= first, mode
        assert first - {""} <= set(RESERVED_SEGMENTS), mode
    assert len(RESERVED_SEGMENTS) == len(set(RESERVED_SEGMENTS))


def test_urls_give_paths_under_the_base_and_take_only_a_path_inside_it(base):
    for origin in ("https://trips.example.com", ORIGIN):
        urls = Urls.of(read(origin + base))
        assert (urls.origin, urls.base) == (origin, base)
        assert urls.path("/trips/7?f=saved#item-9") == f"{base}/trips/7?f=saved#item-9"
        assert urls.absolute("/feed/x.ics") == f"{origin}{base}/feed/x.ics"
        assert urls.cookie_path == (base or "/")
        assert urls.secure is origin.startswith("https://")
    for target in (f"{base}/", f"{base}/trips/7?f=saved#item-9", f"{base}/a.b/c~d"):
        assert urls.inside(target), target
    for target in (
        "",
        "trips/7",
        "https://trips.example.com/",
        f"//elsewhere.example{base}/",
        f"{base}//elsewhere.example/",
        f"{base}/\\elsewhere.example",
        f"{base}/trips/7\r\nSet-Cookie: x=y",
        f"{base}/trips/7\x00",
        f"{base}/../elsewhere",
        f"{base}/%2e%2E/elsewhere",
        f"{base}/./trips",
        f"{base}/" + "x" * 2000,
    ):
        assert not urls.inside(target), target
    if base:
        assert not urls.inside(base) and not urls.inside(f"{base}x/trips")


def test_no_code_builds_an_address_from_the_request():
    """Every address the page gives comes from TRIPBOX_BASE_URL (`web.urls.Urls`): nothing in
    the page's code reads one from the request or writes the old fixed path."""
    files = [p for p in sorted(WEB.rglob("*")) if p.suffix in (".py", ".html", ".js")]
    assert len(files) > 10
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in ("url_for(", "request.url", "request.base_url", '"/tripbox'):
            assert needle not in text, (path.name, needle)


# ----------------------------------------------------------------------------- the page
# What a request may say of the host and the scheme it came by: the page heeds none of it.
HOSTILE = [
    ("host", "other.example"),
    ("x-forwarded-host", "elsewhere.example"),
    ("x-forwarded-proto", "https"),
    ("forwarded", "host=elsewhere.example;proto=https"),
]
# The places outside the page it links, by origin: the fonts, a map and a flight's tracker
# The flight log's address (TRIPBOX_AIRTRAIL_PUBLIC_URL), linked from the list while one is kept
FLIGHT_LOG_ADDRESS = "https://flightlog.example"
# The sites a page may link to, each opened by a tap and never loaded by the page: a place in
# Maps, a flight's tracker, and the flight log at its configured address
OUTSIDE = ("https://maps.apple.com", "https://flightaware.com", FLIGHT_LOG_ADDRESS)


def behind_a_login(tmp_path, monkeypatch, base):
    """The page at its default address behind a login that answers whatever Host a request
    names (a login proxy's, the simplest to sign in with: with no login, a Host that does not
    name this computer is refused before anything else, test_web_auth), signed in, and its
    context."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base, auth="proxy", env=PROXY)
    return Page(c.app, base, headers=SIGNED), ctx


class Addresses(HTMLParser):
    """Every href, action, src and data-*-url of a page, as (tag, its class, attribute,
    value)."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        given = dict(attrs)
        for name, value in attrs:
            if name in ("href", "action", "src") or (
                name.startswith("data-") and name.endswith("-url")
            ):
                self.found.append((tag, given.get("class") or "", name, value or ""))


def addresses(page: str) -> list[tuple[str, str, str, str]]:
    parser = Addresses()
    parser.feed(page)
    return parser.found


def test_a_request_is_served_with_or_without_the_prefix(tmp_path, monkeypatch, base):
    """The page answers under its base, and also when a proxy in front of it took the base off;
    either way its redirects and links carry the base. A path that only looks like the base (a
    longer first segment, the base twice, the base spelled encoded) is not under it."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    stripped = Page(c.app, base, stripped=True)
    for page in (c, stripped):
        for path in ("/", f"/trips/{trip.id}", "/deleted", "/add", "/healthz", "/static/app.css"):
            assert page.get(path).status_code == 200, (page.stripped, path)
        listing = page.get("/").text
        assert f'href="{c.url(f"/trips/{trip.id}")}"' in listing, page.stripped
        r = page.post(
            f"/trips/{trip.id}/notes", data={"notes": "x"}, headers=HDR, follow_redirects=False
        )
        assert (r.status_code, r.headers["location"]) == (303, c.url(f"/trips/{trip.id}"))
    if base:
        encoded = base.replace(base[1], f"%{ord(base[1]):02X}", 1)
        for raw in (f"{base}x/trips/{trip.id}", f"{base}{base}/trips/{trip.id}", encoded + "/"):
            status, headers, _ = raw_get(c.app, raw.encode())
            assert status == 404 and "location" not in headers, raw


def test_a_path_holding_a_control_character_is_a_bare_404(tmp_path, monkeypatch, base):
    """Before routing, at every base, signed in or not (no login, and a login nobody passed): a
    path that holds a character below 0x20 or 0x7f, decoded or sent as it is, is a 404 with an
    empty body and the security headers, never a page a route pattern takes with its line break
    and never a challenge."""
    c, _ = client_for(tmp_path, monkeypatch, base=base)
    locked, _ = client_for(
        tmp_path / "locked", monkeypatch, base=base, auth="password", env=MODES["password"]
    )
    page_load = [("accept", "text/html")]
    for app in (c.app, locked.app):
        for path in ("/healthz", "/deleted", "/static/app.css"):
            assert raw_get(app, f"{base}{path}".encode(), page_load)[0] in (200, 303, 401)
            sent = []
            for suffix in ("%0a", "%0A", "%00", "%09", "%7f", "%7F", "%0d%0aX-Sent:%20x"):
                sent += [f"{base}{path}{suffix}".encode(), f"{path}{suffix}".encode()]
                sent.append(f"{base}{path[:3]}{suffix}{path[3:]}".encode())
            sent += [f"{base}{path}".encode() + b"\n", f"{base}{path}".encode() + b"\x7f"]
            for raw in sent:
                status, headers, body = raw_get(app, raw, page_load)
                assert (status, body) == (404, b""), raw
                assert "location" not in headers and headers["cache-control"] == "no-store", raw
                for name, value in SECURITY_HEADERS.items():
                    assert headers[name.lower()] == value, (raw, name)
    assert raw_get(c.app, f"{base}/healthz".encode())[2] == b"ok"


def test_the_bare_base_goes_to_its_slash_with_a_path_only_location(tmp_path, monkeypatch, base):
    c, _ = behind_a_login(tmp_path, monkeypatch, base)
    if not base:  # the base is the host's root: "/" is the list itself
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 200 and "location" not in r.headers
        return
    for raw, where in ((base, base + "/"), (f"{base}?past=all", f"{base}/?past=all")):
        for headers in ((), HOSTILE):
            status, said, _ = raw_get(c.app, raw.encode(), headers)
            assert (status, said["location"]) == (307, where), (raw, headers)
    assert c.get("").status_code == 200  # followed, it is the list


def test_every_redirect_and_link_stays_inside_the_base(tmp_path, monkeypatch, base):
    """A ledger with a booking of every kind of mark, one with no date, an upload and a trip in
    Recently deleted: every href, action, src and data-*-url of every page begins with the base
    or is a named outside link, and every POST answers with a Location under the base that the
    page serves."""
    env = {"TRIPBOX_AIRTRAIL_PUBLIC_URL": FLIGHT_LOG_ADDRESS}
    c, ctx = client_for(tmp_path, monkeypatch, base=base, env=env)
    st = ctx.trips
    trip = marked_trip(ctx)
    seed_past(ctx, 1)
    other = next(t for t in st.all_trips() if t.id != trip.id)
    undated = st.upsert_item(None, undated_stay(pnr="HOT9"))
    hotel, car = st.item_by_key("lodging|HOTEL1"), st.item_by_key("car|CAR123")
    unnamed = next(ti for ti in st.items(trip.id) if ti.checks == ("owner_unknown",))
    gone = next(ti for ti in st.items(trip.id) if ti.sync_hold == "deleted")
    locations: list[str] = []

    def post(path: str, data: dict | None = None, **kw) -> str:
        r = c.post(path, data=data or {}, headers=HDR, follow_redirects=False, **kw)
        assert r.status_code == 303, (path, r.status_code)
        locations.append(r.headers["location"])
        return r.headers["location"]

    landed = c.local(post("/add/paste", {"text": "A pasted confirmation for the Sample family."}))
    pages = {
        path: c.get(path)
        for path in (
            "/",
            "/?past=all",
            "/deleted",
            "/add",
            f"/add?trip={trip.id}",
            "/add/paste",
            "/add/upload",
            *(f"/add/{word}" for word in ("flight", "hotel", "car", "train")),
            f"/trips/{trip.id}",
            f"/trips/{trip.id}/share",
            *(f"/items/{ti.id}/edit" for ti in [*st.items(trip.id), undated]),
            landed,
            "/trips/999999",
        )
    }
    pages["refused paste"] = c.post("/add/paste", data={"text": " "}, headers=HDR)
    pages["refused edit"] = save(c, hotel.id, {"check_in_date": "soon"})
    seen = seen_on(c, pages[f"/trips/{trip.id}"].text, hotel.id)
    # every way a form comes back: to the trip, the booking, the list, the unsorted group, an
    # upload's page
    post(f"/trips/{trip.id}/rename", {"title": "Sample trip"})
    post(f"/trips/{trip.id}/notes", {"notes": "Gate code"})
    post(f"/trips/{trip.id}/place", {"place": "Sample place"})
    post(f"/items/{hotel.id}/looks-right", {"seen": seen})
    post(f"/items/{unnamed.id}/owner", {"on_it": "yes"})
    post(f"/items/{gone.id}/send-again")
    post(f"/items/{car.id}/status", {"status": "cancelled"})
    locations.append(save(c, car.id, {"provider": "Sample cars"}).headers["location"])
    post(f"/items/{car.id}/move", {"to": "new"})
    post(f"/trips/{other.id}/merge", {"into": str(trip.id)})
    post(f"/items/{hotel.id}/remove")
    post(f"/items/{hotel.id}/restore")
    post("/add/hotel", {"property_name": "Sample inn", "trip": ""})
    post("/add/upload", files={"file": ("sample.txt", b"An itinerary of the Sample family.")})
    locations.append(save(c, undated.id, {"property_name": "Sample lodge"}).headers["location"])
    post(f"/items/{undated.id}/remove")
    post(f"/trips/{trip.id}/delete")
    post(f"/trips/{trip.id}/restore")
    urls = Urls.of(ctx.settings)
    linked: set[str] = set()
    for name, r in pages.items():
        for tag, _, attribute, value in addresses(r.text):
            parts = urlsplit(value)
            named = f"{parts.scheme}://{parts.netloc}" in OUTSIDE
            assert urls.inside(value) or named, (name, tag, attribute, value)
            if named:
                linked.add(f"{parts.scheme}://{parts.netloc}")
    assert linked == set(OUTSIDE)
    assert len(locations) == 19
    for where in locations:
        assert urls.inside(where), where
        assert c.get(c.local(where.split("#")[0])).status_code == 200, where


def test_no_response_takes_its_host_or_scheme_from_the_request(tmp_path, monkeypatch, base):
    c, ctx = behind_a_login(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    token = ical.feed_token(ctx.ledger)
    hostile = dict(HOSTILE)
    answers = [
        c.get(path, headers=hostile)
        for path in (
            "/",
            f"/trips/{trip.id}",
            f"/trips/{trip.id}/share",
            "/add",
            "/trips/999999",
            "/settings",
        )
    ]
    r = c.post(
        f"/trips/{trip.id}/notes",
        data={"notes": "x"},
        headers={**hostile, **HDR},
        follow_redirects=False,
    )
    assert (r.status_code, r.headers["location"]) == (303, c.url(f"/trips/{trip.id}"))
    answers.append(r)
    for answer in answers:
        said = answer.text + "\n".join(f"{k}: {v}" for k, v in answer.headers.items())
        for word in ("other.example", "elsewhere.example", "https://localhost"):
            assert word not in said, (answer.url, word)
    feed = f"{ORIGIN}{base}/feed/{token}.ics"  # the setting's address, whoever asked
    assert f'value="{feed}"' in answers[5].text  # Settings


def test_the_feed_address_and_notice_links_carry_the_base(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    token = ical.feed_token(ctx.ledger)
    feed = f"{ORIGIN}{base}/feed/{token}.ics"
    assert f'value="{feed}"' in c.get("/settings").text
    r = c.get(c.local(feed.removeprefix(ORIGIN)))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    link_base = ctx.settings.link_base
    assert link_base == ORIGIN + base
    ti = ctx.trips.items(trip.id)[0]
    card = check_card(ti, ctx.book, link_base=link_base).splitlines()[-1]
    assert card == f"{ORIGIN}{base}/trips/{trip.id}#item-{ti.id}"
    line = fmt.trip_line(trip, ctx.trips, dt.date.today(), link_base)
    assert line.endswith(f" {ORIGIN}{base}/trips/{trip.id}")
    for link in (card, line.rsplit(" ", 1)[1]):
        assert c.get(c.local(link.removeprefix(ORIGIN).split("#")[0])).status_code == 200, link
    # with no address set, a notice links nothing
    unset = Settings.from_env({"TRIPBOX_AUTH": "none"}).link_base
    assert unset is None and check_card(ti, ctx.book, link_base=unset) == booking_line(ti, ctx.book)
    assert fmt.trip_line(trip, ctx.trips, dt.date.today(), unset).count("://") == 0
