# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page's boundary: a short list of routes anyone may reach, named in `app.PUBLIC`, and every
other route inside one private mount whose one middleware asks the login, so a route is private
unless it is named public and a path no route takes is challenged like a private one; the
security headers on every response of every kind; and one same-origin rule and one body limit
for every POST."""

import asyncio
import dataclasses
import datetime as dt
import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

import pytest
import uvicorn
from helpers.sample import CONFIG_DIR
from helpers.web import PEER, Page, client_for, raw_get, starlette_of
from jwt import PyJWKClient
from starlette.routing import Mount, Route, Router
from test_passwords import FAKE, cheap_hash
from test_web_auth import ACCESS, PROXY, SIGNED, STRANGERS, Keys
from test_web_auth import PUBLIC as ADDRESS
from test_web_views import seed_trip

from tripbox import cli, uploads, util
from tripbox.config import ConfigError, Settings
from tripbox.ledger import Ledger
from tripbox.trips import ical, shares
from tripbox.web import auth, views
from tripbox.web.app import PUBLIC, create_app
from tripbox.web.middleware import LIMIT, SECURITY_HEADERS, LoginRequired
from tripbox.web.urls import Urls

# Every login the page offers, with the settings it needs beside TRIPBOX_AUTH: each test of the
# boundary runs under every one, at a public address (behind a login proxy, the tests' own
# requests come from the proxy: see Login.nobody for one from a stranger)
MODES = {
    "cloudflare": ACCESS,
    "password": {"TRIPBOX_PASSWORD_HASH": cheap_hash(FAKE)},
    "proxy": PROXY,
}
# What a browser that loads a page says it takes
PAGE_LOAD = [("accept", "text/html,application/xhtml+xml,*/*;q=0.8")]


def sign_in_to(app, base: str) -> dict[str, str]:
    """The Cookie header of a session made through the page's own form (password mode)."""
    r = Page(app, base).post(
        "/login",
        data={"password": FAKE},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    (pair,) = [
        line.split(";", 1)[0]
        for line in r.headers.get_list("set-cookie")
        # a sign-in at the root over https also drops the older names: those lines hold no value
        if line.split("=", 1)[0].endswith("tripbox_session")
        and not line.split(";", 1)[0].endswith("=")
    ]
    return {"Cookie": pair}


@dataclass
class Login:
    """A login the page offers (`mode`, a TRIPBOX_AUTH value) and how a request signs in: on
    the page `page` built last, or the one `serving` names."""

    mode: str
    keys: Keys
    app: object = None
    base: str = ""
    session: dict[str, str] | None = None

    def page(self, tmp_path, monkeypatch, base: str, address: str = ADDRESS, **kw):
        """The page under this login at `address`, served under `base`, and its context."""
        env = {**MODES[self.mode], "TRIPBOX_BASE_URL": address + base}
        c, ctx = client_for(tmp_path, monkeypatch, base=base, auth=self.mode, env=env, **kw)
        self.serving(c.app, base)
        return c, ctx

    def serving(self, app, base: str) -> None:
        self.app, self.base, self.session = app, base, None

    def signed_in(self) -> dict[str, str]:
        """The headers of a request someone is signed in on (behind a proxy: from the proxy)."""
        if self.mode == "cloudflare":
            return {"Cf-Access-Jwt-Assertion": self.keys.token()}
        if self.mode == "password":
            if self.session is None:
                self.session = sign_in_to(self.app, self.base)
            return self.session
        if self.mode == "proxy":
            return dict(SIGNED)
        raise AssertionError(f"no way to sign in under {self.mode}")

    def nobody(self) -> list[tuple[list[tuple[str, str]], tuple[str, int]]]:
        """How a request nobody is signed in on is sent, as its headers and its socket peer:
        with no credential from the tests' own peer (behind a proxy: from the proxy, with no
        user's header); behind a proxy also from a peer it does not trust, with the header that
        signs a user in from the proxy, which must change nothing."""
        sent = [([], PEER)]
        if self.mode == "proxy":
            sent.append((list(SIGNED.items()), STRANGERS[0]))
        return sent


@pytest.fixture(params=list(MODES))
def login(request, monkeypatch) -> Login:
    keys = Keys()
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: keys.jwks)
    return Login(request.param, keys)


def settings_in(mode: str) -> Settings:
    """The page's settings under the login `mode` (no login: on localhost)."""
    if mode == "none":
        return Settings.from_env({"TRIPBOX_AUTH": "none"})
    return Settings.from_env({"TRIPBOX_AUTH": mode, **MODES[mode], "TRIPBOX_BASE_URL": ADDRESS})


def private_mount(app) -> Mount:
    (mount,) = [r for r in starlette_of(app).routes if r.name == "private"]
    return mount


def spellings(path: str, base: str) -> set[bytes]:
    """The route path `path` (written from `/`) sent under `base` as itself, and each way of
    spelling it that a proxy, a browser or a router might read as it: with a trailing `/`, with
    `//` in front, with the base twice, with `;x`, `%00` or `%0a` after it, upper-cased, and
    behind `/static/../`, `/static/%2e%2e/` and `/feed/..%2f`."""
    at = base + path
    return {
        s.encode()
        for s in (
            at,
            at + "/",
            "/" + at,
            base + at,
            at + ";x",
            at + "%00",
            at + "%0a",
            at.upper(),
            base + "/static/.." + path,
            base + "/static/%2e%2e" + path,
            base + "/feed/..%2f" + path[1:],
        )
    }


def dump(ctx) -> list[str]:
    """Every row of every table of the ledger, as SQL."""
    return list(ctx.ledger.conn.iterdump())


def test_no_private_route_answers_without_a_login_at_any_base_or_spelling(
    tmp_path, monkeypatch, base, login
):
    """Every route of the private mount, its parameters filled from a seeded ledger: signed in,
    each one that answers a GET answers it; with nobody signed in (each way `Login.nobody`
    sends), no spelling of any of them, under the base or with the base taken off, loaded as a
    page, is a 200 or shows the trip, and the only place it is sent on to is the login page,
    with a `next` inside the base; a spelling that holds a control character is a bare 404. Nor
    does a HEAD, an OPTIONS or a same-origin POST to any of them, which leave the ledger as it
    was."""
    c, ctx = login.page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    (item, *_) = ctx.trips.items(trip.id)
    note = b"A note of the Sample family."
    token, _ = uploads.submit(ctx.ledger, note, name=None, submitted_by=None)
    link = shares.create(ctx.ledger, trip.id, shares.FIELDS)
    values = {
        "trip_id": trip.id,
        "item_id": item.id,
        "kind": "hotel",
        "token": token,
        "link_id": link.id,
    }
    routes = private_mount(c.app).routes
    assert len(routes) > 20
    # making and revoking a share link are among them, taken in by the loop below
    assert {"make_link", "revoke_link"} <= {route.name for route in routes}
    title = trip.title.encode()
    urls = Urls.of(ctx.settings)
    signed = list(login.signed_in().items())
    paths = []
    for route in routes:
        assert isinstance(route, Route), route
        path = route.path_format.format(**{name: values[name] for name in route.param_convertors})
        if "GET" in (route.methods or ()):
            status, _, body = raw_get(c.app, (base + path).encode(), signed)
            assert status == 200, path
        paths.append((path, route.methods or set()))
    before = dump(ctx)

    def refused(sent: bytes, status: int, headers: dict[str, str], body: bytes) -> None:
        assert status != 200 and title not in body, sent
        if b"%00" in sent or b"%0a" in sent:
            assert (status, body) == (404, b""), sent
        if "location" in headers:
            where = urlsplit(headers["location"])
            assert (where.scheme, where.netloc, where.path) == ("", "", urls.path("/login")), sent
            asked = parse_qsl(where.query, keep_blank_values=True)
            assert [name for name, _ in asked] in ([], ["next"]), sent
            assert all(urls.inside(value) for _, value in asked), sent

    for path, methods in paths:
        for nobody, peer in login.nobody():
            load = [*nobody, *PAGE_LOAD]
            for sent in spellings(path, base) | spellings(path, ""):
                refused(sent, *raw_get(c.app, sent, load, client=peer))
            for sent in {(base + path).encode(), path.encode()}:
                for method in ("HEAD", "OPTIONS"):
                    status, said, body = raw_get(c.app, sent, load, method=method, client=peer)
                    refused(sent, status, said, body)
                    assert status != 405, (method, sent)  # refused before a route was looked at
                if "POST" in methods:
                    same = [("sec-fetch-site", "same-origin"), *load]
                    status, said, body = raw_get(c.app, sent, same, method="POST", client=peer)
                    assert status == 401 and "location" not in said, sent
                    if login.mode == "password":  # a browser's form: the way back in, only
                        assert b"Your sign-in has ended" in body and title not in body, sent
                    else:
                        assert body == b"unauthorized", sent
    assert dump(ctx) == before
    status, _, body = raw_get(c.app, f"{base}/trips/{trip.id}".encode(), signed)
    assert status == 200 and title in body  # what the refusals are searched for


def test_each_login_is_built_from_its_setting_and_no_login_only_on_localhost():
    local = settings_in("none")
    nobody = auth.build(local, lambda: Ledger.memory())
    assert isinstance(nobody, auth.NoLogin) and nobody.logout_url() is None
    access = auth.build(settings_in("cloudflare"), lambda: Ledger.memory())
    assert isinstance(access, auth.CloudflareAccess) and access.routes() == []
    team = ACCESS["TRIPBOX_ACCESS_TEAM"]
    assert access.logout_url() == f"https://{team}.cloudflareaccess.com/cdn-cgi/access/logout"
    own = auth.build(settings_in("password"), lambda: Ledger.memory())
    assert own.name == "password" and own.logout_url() is None
    assert [(r.path, r.name) for r in own.routes()] == [("/login", "login"), ("/logout", "logout")]
    public = dataclasses.replace(local, base_url=ADDRESS)  # built by hand, past its checks
    with pytest.raises(ConfigError, match="localhost"):
        auth.build(public, lambda: Ledger.memory())
    proxy = auth.build(settings_in("proxy"), lambda: Ledger.memory())
    assert isinstance(proxy, auth.ProxyLogin) and proxy.name == "proxy"
    assert proxy.routes() == [] and proxy.logout_url() is None
    # a password or a proxy login whose settings have a web problem, built by hand, is refused
    unset = Settings.from_env({"TRIPBOX_AUTH": "password", "TRIPBOX_BASE_URL": ADDRESS})
    with pytest.raises(ConfigError, match="TRIPBOX_PASSWORD: not set"):
        auth.build(unset, lambda: Ledger.memory())
    with pytest.raises(ConfigError, match="TRIPBOX_TRUSTED_PROXIES: not set"):
        auth.build(Settings.from_env({"TRIPBOX_AUTH": "proxy"}), lambda: Ledger.memory())
    # and so is one that trusts every address, however it was built
    everyone = dataclasses.replace(
        settings_in("proxy"), trusted_proxies=(ipaddress.ip_network("::/0"),)
    )
    with pytest.raises(ConfigError, match="TRIPBOX_TRUSTED_PROXIES"):
        auth.build(everyone, lambda: Ledger.memory())


def test_the_public_routes_are_these_and_no_others():
    """A name added to PUBLIC opens a route to anyone: the list is pinned, so adding one is a
    change this test is changed for."""
    assert PUBLIC == (
        "healthz",
        "static",
        "feed",
        "shared",
        "manifest",
        "worker",
        "login",
        "logout",
    )


@pytest.mark.parametrize("mode", ["none", *MODES])
def test_every_route_is_public_by_name_or_inside_the_private_mount(mode):
    app = starlette_of(create_app(settings_in(mode), lambda: Ledger.memory()))
    *public, private = app.routes
    assert public and all(route.name in PUBLIC for route in public), [r.name for r in public]
    assert {"healthz", "static", "feed", "shared"} <= {route.name for route in public}
    assert len({route.name for route in public}) == len(public)
    assert isinstance(private, Mount) and (private.name, private.path) == ("private", "")
    assert isinstance(private.app, LoginRequired)
    inner = private.app.app
    assert isinstance(inner, Router) and inner.routes == private.routes
    assert not inner.redirect_slashes and not app.router.redirect_slashes
    assert not {route.name for route in private.routes} & set(PUBLIC)
    assert {"index", "trip", "edit_item"} <= {route.name for route in private.routes}


def test_a_path_no_route_takes_is_challenged_then_is_the_404_page(
    tmp_path, monkeypatch, base, login
):
    c, ctx = login.page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    signed = Page(c.app, base, headers=login.signed_in())
    assert signed.get(f"/trips/{trip.id}").status_code == 200
    assert signed.get("/nothing").status_code == 404  # the 404 page once signed in
    assert c.get("/nothing").status_code == 401  # challenged like a private route
    assert c.get("/healthz").text == "ok"
    assert c.get("/static/app.css").status_code == 200
    # a path even a mount at "" cannot match (no leading slash) is challenged too
    assert raw_get(c.app, b"*")[0] == 401
    assert raw_get(c.app, b"*", login.signed_in().items())[0] == 404
    # one that holds a line break is a bare 404 before anything looks at it, signed in or not
    for raw in (f"{base}/nothing%0aelse".encode(), f"{base}/trips/{trip.id}%0a/x".encode()):
        assert raw_get(c.app, raw)[::2] == (404, b""), raw
        assert raw_get(c.app, raw, login.signed_in().items())[::2] == (404, b""), raw


# ----------------------------------------------------------------------------- posts
REFUSED = (
    "This page is set up for {origin}. Open it there, or set TRIPBOX_BASE_URL to the address you"
    " use."
)
FORM = {"content-type": "application/x-www-form-urlencoded"}


def notes_of(ctx, trip_id: int) -> str | None:
    return ctx.trips.get(trip_id).notes


def test_a_post_from_another_origin_is_refused_whatever_host_it_claims(
    tmp_path, monkeypatch, base, login
):
    c, ctx = login.page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    host = ADDRESS.removeprefix("https://")
    signed = login.signed_in()
    evil = {"Origin": "https://evil.example", "Host": "evil.example"}
    for claims in (
        evil,
        {**evil, "X-Forwarded-Host": host, "X-Forwarded-Proto": "https"},
        {**evil, "Forwarded": f"host={host};proto=https"},
        {"Origin": "https://evil.example", "Host": host},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site", "Origin": "https://other.example.com"},
        {},
    ):
        r = c.post(
            f"/trips/{trip.id}/notes",
            data={"notes": "refused"},
            headers={**signed, **claims},
            follow_redirects=False,
        )
        assert r.status_code == 403, claims
        assert r.text == REFUSED.format(origin=ADDRESS), claims
        assert r.headers["content-type"].startswith("text/plain"), claims
    assert notes_of(ctx, trip.id) is None
    for n, claims in enumerate(
        (
            {"Origin": ADDRESS, "Host": "evil.example"},
            {"Sec-Fetch-Site": "same-origin", "Host": "evil.example"},
        )
    ):
        r = c.post(
            f"/trips/{trip.id}/notes",
            data={"notes": f"taken {n}"},
            headers={**signed, **claims},
            follow_redirects=False,
        )
        assert (r.status_code, r.headers["location"]) == (303, c.url(f"/trips/{trip.id}")), claims
        assert notes_of(ctx, trip.id) == f"taken {n}"


def test_a_base_url_written_with_its_default_port_takes_the_origin_a_browser_sends(
    tmp_path, monkeypatch, base, login
):
    c, ctx = login.page(tmp_path, monkeypatch, base, address=ADDRESS + ":443")
    assert ctx.settings.origin == ADDRESS
    trip = seed_trip(ctx)
    r = c.post(
        f"/trips/{trip.id}/notes",
        data={"notes": "taken"},
        headers={**login.signed_in(), "Origin": ADDRESS},
        follow_redirects=False,
    )
    assert r.status_code == 303 and notes_of(ctx, trip.id) == "taken"
    r = c.post(f"/trips/{trip.id}/notes", data={"notes": "x"}, headers=login.signed_in())
    assert (r.status_code, r.text) == (403, REFUSED.format(origin=ADDRESS))


# ----------------------------------------------------------------------------- headers
def test_the_security_headers_are_these_and_no_others():
    """What every response carries: the content policy (pinned in full by test_web_views), no
    framing, no sniffing, the address kept to the page's own origin, and no search engine's
    index (a household's private page is never meant to be in one)."""
    assert set(SECURITY_HEADERS) == {
        "Content-Security-Policy",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "X-Robots-Tag",
    }
    assert {k: v for k, v in SECURITY_HEADERS.items() if k != "Content-Security-Policy"} == {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Robots-Tag": "noindex, nofollow",
    }


def guarded(r, *, static: bool = False) -> None:
    """Each security header once, with its value; `Cache-Control: no-store` unless `static`."""
    for name, value in SECURITY_HEADERS.items():
        assert r.headers.get_list(name) == [value], (r.request.url.path, r.status_code, name)
    store = r.headers.get_list("cache-control")
    assert store == ([] if static else ["no-store"]), (r.request.url.path, r.status_code)


def test_every_kind_of_response_carries_the_security_headers(tmp_path, monkeypatch, base, login):
    c, ctx = login.page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    signed = Page(c.app, base, headers=login.signed_in(), raise_server_exceptions=False)
    same = {"Sec-Fetch-Site": "same-origin"}
    token = ical.feed_token(ctx.ledger)
    answers = {
        "a page": signed.get("/"),
        "a redirect": signed.post(
            f"/trips/{trip.id}/notes", data={"notes": "x"}, headers=same, follow_redirects=False
        ),
        "the 404 page": signed.get("/trips/999999"),
        "a path no route takes": signed.get("/nothing"),
        "a method a route does not take": signed.get(f"/trips/{trip.id}/rename"),
        "the feed's bare 404": c.get("/feed/nope.ics"),
        "the feed": c.get(f"/feed/{token}.ics"),
        "a 401": c.get(f"/trips/{trip.id}"),
        "a 403": signed.post(f"/trips/{trip.id}/notes", data={"notes": "x"}),
        "a 413": signed.post(
            f"/trips/{trip.id}/notes", content=b"n" * (LIMIT + 1), headers={**same, **FORM}
        ),
        "text": signed.get(f"/trips/{trip.id}/text"),
        "a calendar": signed.get(f"/trips/{trip.id}/trip.ics"),
        "health": c.get("/healthz"),
    }
    if base:
        answers["the bare base"] = c.get("", follow_redirects=False)
    if login.mode == "password":  # the login's own answers
        page_load = dict(PAGE_LOAD)
        answers["the login page"] = c.get("/login")
        answers["a page load sent to sign in"] = c.get(
            f"/trips/{trip.id}", headers=page_load, follow_redirects=False
        )
        answers["a wrong password"] = c.post("/login", data={"password": "no"}, headers=same)
        answers["a sign-out"] = c.post("/logout", headers=same, follow_redirects=False)
    expected = {
        "a page": 200,
        "a redirect": 303,
        "the 404 page": 404,
        "a path no route takes": 404,
        "a method a route does not take": 405,
        "the feed's bare 404": 404,
        "the feed": 200,
        "a 401": 401,
        "a 403": 403,
        "a 413": 413,
        "text": 200,
        "a calendar": 200,
        "health": 200,
        "the bare base": 307,
        "the login page": 200,
        "a page load sent to sign in": 303,
        "a wrong password": 401,
        "a sign-out": 303,
    }
    for kind, r in answers.items():
        assert r.status_code == expected[kind], kind
        guarded(r)
    assert answers["the feed's bare 404"].content == b""
    assert answers["a calendar"].headers["content-type"].startswith("text/calendar")
    assert answers["text"].headers["content-type"].startswith("text/plain")
    static = c.get("/static/app.css")
    assert static.status_code == 200
    guarded(static, static=True)

    def broken():
        raise RuntimeError("S3CRET-DETAIL of the failure")

    monkeypatch.setattr(views, "today_local", broken)
    r = signed.get("/")
    assert (r.status_code, r.text) == (500, "Something went wrong.")
    assert r.headers["content-type"].startswith("text/plain") and "S3CRET" not in r.text
    guarded(r)


# ----------------------------------------------------------------------------- body limit
def raw_post(app, raw_path: bytes, chunks, *, headers=(), length: int | None = None):
    """POST the chunks to `raw_path` one receive at a time, with no declared length unless
    `length`, as a server hands a body on; (status, its body, how many body bytes the app took).
    `headers`: (name, value) pairs."""
    taken = 0
    source = iter(chunks)
    sent: list[dict] = []
    done = asyncio.Event()
    ended = False

    async def receive() -> dict:
        nonlocal taken, ended
        if ended:  # the whole body is in: the client goes once the response is sent
            await done.wait()
            return {"type": "http.disconnect"}
        chunk = next(source, None)
        if chunk is None:
            ended = True
            return {"type": "http.request", "body": b"", "more_body": False}
        taken += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()

    named = [("host", "localhost:8110"), *headers]
    if length is None:
        named.append(("transfer-encoding", "chunked"))
    else:
        named.append(("content-length", str(length)))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": raw_path.decode("latin-1"),
        "raw_path": raw_path,
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in named],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 8110),
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, taken


def endless(head: bytes, piece: bytes, total: int):
    """`head`, then `piece` again and again until `total` bytes have gone."""
    yield head
    gone = len(head)
    while gone < total:
        yield piece
        gone += len(piece)


def test_an_oversize_post_is_refused_before_it_is_read(tmp_path, monkeypatch, base, login):
    c, ctx = login.page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    notes = f"{base}/trips/{trip.id}/notes".encode()
    form = [("content-type", FORM["content-type"]), ("sec-fetch-site", "same-origin")]
    signed = [*form, *login.signed_in().items()]
    piece = b"x" * 16384
    # a declared length past the limit: answered before a byte is read, signed in or not
    for headers in (form, signed):
        body = endless(b"notes=", piece, LIMIT + 2**20)
        status, said, taken = raw_post(c.app, notes, body, headers=headers, length=LIMIT + 1)
        assert (status, said, taken) == (413, b"too large", 0)
    # no declared length: counted as it arrives, and refused once past the limit
    body = endless(b"notes=", piece, LIMIT + 2**20)
    status, said, taken = raw_post(c.app, notes, body, headers=signed)
    assert (status, said) == (413, b"too large")
    assert LIMIT < taken <= LIMIT + len(piece)
    assert notes_of(ctx, trip.id) is None
    # up to the limit, the form is read
    body = b"notes=" + b"y" * (LIMIT - len(b"notes="))
    status, _, taken = raw_post(c.app, notes, [body[:30000], body[30000:]], headers=signed)
    assert (status, taken) == (303, LIMIT)
    assert notes_of(ctx, trip.id) == "y" * (LIMIT - len(b"notes="))


def test_the_paste_and_upload_forms_keep_their_own_larger_limit_and_sentence(
    tmp_path, monkeypatch, base, login
):
    c, ctx = login.page(tmp_path, monkeypatch, base)
    signed = [("sec-fetch-site", "same-origin"), *login.signed_in().items()]
    upload = f"{base}/add/upload".encode()
    multipart = [("content-type", "multipart/form-data; boundary=sample"), *signed]
    piece = b"x" * 65536
    past_both = views.BODY_CAP + LIMIT + 1
    status, said, taken = raw_post(
        c.app, upload, endless(b"", piece, 2**20), headers=multipart, length=past_both
    )
    assert (status, said, taken) == (413, b"too large", 0)
    status, said, taken = raw_post(
        c.app, upload, endless(b"", piece, 2**20), headers=multipart, length=views.BODY_CAP + 1
    )
    assert (status, taken) == (413, 0)
    assert b"That file is larger than 10 MB." in said
    # a paste longer than the limit of any other form is read
    text = "A long itinerary of the Sample family. " * (LIMIT // 30)
    paste = Page(c.app, base, headers={**login.signed_in(), "Sec-Fetch-Site": "same-origin"})
    r = paste.post("/add/paste", data={"text": text}, follow_redirects=False)
    assert len(text) > LIMIT and r.status_code == 303


# ----------------------------------------------------------------------------- forwarded headers
# What a request may say of where it came from: a proxy's word, which anyone can send
FORWARDED = [
    ("x-forwarded-for", "203.0.113.7"),
    ("x-forwarded-proto", "https"),
    ("x-forwarded-host", "elsewhere.example"),
    ("x-forwarded-port", "8443"),
    ("x-real-ip", "203.0.113.7"),
    ("forwarded", "for=203.0.113.7;host=elsewhere.example;proto=https"),
]
# The socket's own peer: an address uvicorn would never take a forwarded header from, and the
# loopback address it would by default, were forwarded headers believed
PEERS = (("192.0.2.10", 40000), ("127.0.0.1", 40000))


def test_a_forwarded_header_changes_nothing_the_page_sees(tmp_path, monkeypatch, base, login):
    """The page as `tripbox web` hands it to uvicorn, inside the app uvicorn builds from it: from
    either peer (behind a login proxy, each one the proxy), the client is the socket's peer and
    the scheme the socket's, whatever a forwarded header says, and every answer is the same with
    those headers as without."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    page_env = {
        **MODES[login.mode],
        "TRIPBOX_AUTH": login.mode,
        "TRIPBOX_BASE_URL": ADDRESS + base,
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
    }
    if login.mode == "proxy":
        page_env["TRIPBOX_TRUSTED_PROXIES"] = ",".join(host for host, _ in PEERS)
    for name, value in page_env.items():
        monkeypatch.setenv(name, value)
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append((app, kw)))
    assert cli.main(["web"]) == 0
    ((page, options),) = served
    login.serving(page, base)
    seen: list[tuple] = []

    async def sees(scope, receive, send):
        """The page, noting what it is handed."""
        seen.append((scope["client"], scope["scheme"]))
        await page(scope, receive, send)

    config = uvicorn.Config(sees, **options, log_config=None)
    config.load()
    signed = list(login.signed_in().items())
    asked = [
        (f"{base}/".encode(), signed),
        (f"{base}/healthz".encode(), []),
        (f"{base}/trips/999999".encode(), signed),
        (f"{base}/".encode(), []),
    ]
    with util.clock_at(dt.datetime.now(dt.UTC)):
        for peer in PEERS:
            statuses = []
            for path, headers in asked:
                plain = raw_get(config.loaded_app, path, headers, client=peer)
                told = raw_get(config.loaded_app, path, [*headers, *FORWARDED], client=peer)
                assert told == plain, (peer, path)
                statuses.append(plain[0])
            assert statuses == [200, 200, 404, 401], peer
    assert seen == [(peer, "http") for peer in PEERS for _ in range(2 * len(asked))]
