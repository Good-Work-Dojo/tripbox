# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Password sign-in, at each base: one shared password; a session is a random token in a cookie,
kept in the ledger as its SHA-256 and ended by a changed password, by time and by sign-out; a
throttle a stranger fills only for strangers, so the owner's own browser always signs in; cookie
names and their Secure flag decided by the base URL alone."""

import base64
import datetime as dt
import hashlib
import logging
import os
import re
import sqlite3
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
import uvicorn
from helpers.sample import CONFIG_DIR
from helpers.web import ORIGIN, Page, attribution_line, client_for, raw_get
from test_passwords import FAKE, cheap_hash
from test_web_views import seed_trip

from tripbox import cli, passwords, util
from tripbox.config import Settings, report
from tripbox.ledger import Ledger
from tripbox.ledger.repo import Sessions
from tripbox.web.login import _Signed
from tripbox.web.urls import Urls

PASSWORD = FAKE
HASHED = cheap_hash(PASSWORD)
HTTPS = "https://trips.example"
SAME = {"Sec-Fetch-Site": "same-origin"}
PAGE_LOAD = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}
WRONG = "That password is not right."
T0 = dt.datetime(2027, 6, 1, 9, 0, tzinfo=dt.UTC)


def page(tmp_path, monkeypatch, base, *, address=ORIGIN, env=None):
    """The page under password sign-in at `address`, served under `base` (a cheap hash of
    PASSWORD unless `env` says otherwise), and its context."""
    env = {"TRIPBOX_PASSWORD_HASH": HASHED, "TRIPBOX_BASE_URL": address + base, **(env or {})}
    return client_for(tmp_path, monkeypatch, base=base, auth="password", env=env)


def fresh(c: Page) -> Page:
    """A client of the same page that holds no cookie."""
    return Page(c.app, c.base, stripped=c.stripped)


def sign_in(c: Page, password: str = PASSWORD, *, next: str | None = None, cookie: str = "", **kw):
    """POST the login form from a client holding no cookie but `cookie` (a Cookie header)."""
    data = {"password": password, **({"next": next} if next is not None else {})}
    headers = {**SAME, **({"Cookie": cookie} if cookie else {}), **kw.pop("headers", {})}
    return fresh(c).post("/login", data=data, headers=headers, follow_redirects=False, **kw)


def set_cookies(r) -> dict[str, str]:
    """Each Set-Cookie line of a response, by the cookie's name."""
    return {line.split("=", 1)[0]: line for line in r.headers.get_list("set-cookie")}


def value(line: str) -> str:
    return line.split(";", 1)[0].split("=", 1)[1]


def session_of(r, name: str = "tripbox_session") -> str:
    return value(set_cookies(r)[name])


def as_cookie(name: str, token: str) -> dict[str, str]:
    return {"Cookie": f"{name}={token}"}


def get(c: Page, path: str, cookie: dict[str, str] | None = None, **kw):
    return fresh(c).get(path, headers={**(cookie or {}), **kw.pop("headers", {})}, **kw)


def rows(ctx, table: str) -> list[dict]:
    with Ledger.open(ctx.settings.ledger_path) as led:
        return [dict(r) for r in led.conn.execute(f"SELECT * FROM {table}")]


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ----------------------------------------------------------------------------- signing in
def test_signing_in_sets_a_session_and_goes_to_next_inside_the_base(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    shown = c.get("/login")
    assert shown.status_code == 200
    assert "<h1>Sign in</h1>" in shown.text and "Sign in</button>" in shown.text
    field = re.search(r"<input [^>]*name=\"password\"[^>]*>", shown.text).group(0)
    assert 'type="password"' in field and 'autocomplete="current-password"' in field
    assert re.search(r'<label[^>]*for="password"[^>]*>Password</label>', shown.text)
    assert "aria-describedby" not in field and "aria-invalid" not in field
    # a wrong password: the page again, the error tied to the field, no cookie
    r = sign_in(c, "not the password", next=c.url(f"/trips/{trip.id}"))
    assert r.status_code == 401 and set_cookies(r) == {}
    field = re.search(r"<input [^>]*name=\"password\"[^>]*>", r.text).group(0)
    said = re.search(r'<p [^>]*id="(?P<id>[^"]+)"[^>]*>(?P<text>[^<]*)</p>', r.text)
    assert said["text"] == WRONG and f'aria-describedby="{said["id"]}"' in field
    assert 'aria-invalid="true"' in field
    assert f'name="next" value="{c.url(f"/trips/{trip.id}")}"' in r.text  # kept for the next try
    # the right one: both cookies, and on to `next`
    goto = c.url(f"/trips/{trip.id}?past=all")
    r = sign_in(c, next=goto)
    assert (r.status_code, r.headers["location"]) == (303, goto)
    assert set(set_cookies(r)) == {"tripbox_session", "tripbox_device"}
    token, device = session_of(r), value(set_cookies(r)["tripbox_device"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token) and re.fullmatch(r"[A-Za-z0-9_-]{22}", device)
    shown = get(c, f"/trips/{trip.id}", as_cookie("tripbox_session", token))
    assert shown.status_code == 200 and trip.title in shown.text
    # the ledger keeps only what each token hashes to
    (session,) = rows(ctx, "sessions")
    assert session["token_hash"] == digest(token) and session["seen_at"] == session["created_at"]
    assert [d["id_hash"] for d in rows(ctx, "devices")] == [digest(device)]
    with Ledger.open(ctx.settings.ledger_path) as led:
        kept = "\n".join(led.conn.iterdump())
    assert token not in kept and device not in kept and PASSWORD not in kept
    # no `next`: the list; signed in already, the login page goes on
    r = sign_in(c)
    assert (r.status_code, r.headers["location"]) == (303, c.url("/"))
    signed = as_cookie("tripbox_session", token)
    r = get(c, f"/login?next={goto}", signed, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, goto)
    r = get(c, "/login", signed, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, c.url("/"))
    # a proxy that strips the base hands the form on as it is: the same answer, under the base
    stripped = Page(c.app, base, stripped=True)
    r = sign_in(stripped, next=goto)
    assert (r.status_code, r.headers["location"]) == (303, goto)


def test_the_about_line_shows_the_notices_attribution_line(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    html = fresh(c).get("/login").text
    (about,) = re.findall(r'<p class="about[^"]*">(.*?)</p>', html, re.S)
    words = " ".join(unescape(re.sub(r"<[^>]+>", " ", about)).split())
    attribution = attribution_line()
    assert f"· {attribution} · free software under the GNU Affero General Public License" in words
    assert words.endswith("with no warranty · Source code")
    assert 'rel="noopener noreferrer">Source code</a>' in about


def test_next_outside_the_base_goes_home(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    home = c.url("/")
    token = session_of(sign_in(c))
    signed = as_cookie("tripbox_session", token)
    outside = [
        "https://evil.example/",
        f"https://evil.example{base}/",
        "//evil.example/",
        f"//evil.example{base}/",
        f"{base}//evil.example/",
        f"{base}/\\evil.example",
        f"{base}\\/evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        f"{base}/../elsewhere",
        f"{base}/%2e%2e/elsewhere",
        f"{base}/trips\r\nSet-Cookie: x=y",
        c.url("/login"),
        c.url("/login?next=/"),
        c.url("/logout"),
        "",
        f"{base}/" + "x" * 2000,
    ]
    if base:
        outside += ["/", base, f"{base}x/trips", "/trips"]
    for goto in outside:
        r = sign_in(c, next=goto)
        assert (r.status_code, r.headers["location"]) == (303, home), goto
        asked = "/login?" + urlencode({"next": goto})
        r = get(c, asked, signed, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, home), goto
        shown = get(c, asked)
        assert shown.status_code == 200 and 'name="next"' not in shown.text, goto
    # 100,000 characters: in the query, whose length no body limit bounds (sent as a server
    # hands it on: a client refuses an address this long)
    asked = (c.url("/login?") + urlencode({"next": f"{base}/" + "x" * 100_000})).encode()
    status, headers, _ = raw_get(c.app, asked, list(signed.items()))
    assert (status, headers["location"]) == (303, home)
    status, _, body = raw_get(c.app, asked)
    assert status == 200 and b'name="next"' not in body
    # and in a form as long as the body limit lets through
    r = sign_in(c, next=f"{base}/" + "x" * 60_000)
    assert (r.status_code, r.headers["location"]) == (303, home)


def test_the_cookie_path_is_the_base(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    path = base or "/"
    r = sign_in(c)
    lines = set_cookies(r)
    for name in ("tripbox_session", "tripbox_device"):
        assert re.search(rf"; Path={re.escape(path)};", lines[name]), lines[name]
    signed = as_cookie("tripbox_session", session_of(r))
    out = fresh(c).post("/logout", headers={**SAME, **signed}, follow_redirects=False)
    cleared = set_cookies(out)
    assert set(cleared) == {"tripbox_session", "__Secure-tripbox_session"}
    for line in cleared.values():
        assert re.search(rf"; Path={re.escape(path)};", line), line
    assert Urls.of(ctx.settings).cookie_path == path


def test_cookies_are_secure_exactly_when_the_base_url_is_https(tmp_path, monkeypatch, base):
    """The cookies' names and their Secure flag follow TRIPBOX_BASE_URL, whatever a forwarded
    header says of the scheme; each page reads its own name only."""
    path = base or "/"
    claims = [
        {"X-Forwarded-Proto": "https"},
        {"X-Forwarded-Proto": "http"},
        {"Forwarded": "proto=https"},
        {"Forwarded": "proto=http"},
        {},
    ]
    kinds = ((ORIGIN, "", ""), (HTTPS, "__Secure-", "; Secure"))
    for n, (address, prefix, secure) in enumerate(kinds):
        c, _ = page(tmp_path / str(n), monkeypatch, base, address=address)
        for said in claims:
            r = sign_in(c, headers=said)
            assert r.status_code == 303, (address, said)
            lines = set_cookies(r)
            assert set(lines) == {f"{prefix}tripbox_session", f"{prefix}tripbox_device"}
            token = value(lines[f"{prefix}tripbox_session"])
            device = value(lines[f"{prefix}tripbox_device"])
            assert lines[f"{prefix}tripbox_session"] == (
                f"{prefix}tripbox_session={token}; HttpOnly; SameSite=Lax; Path={path};"
                f" Max-Age=15552000{secure}"
            ), (address, said)
            assert lines[f"{prefix}tripbox_device"] == (
                f"{prefix}tripbox_device={device}; HttpOnly; SameSite=Lax; Path={path};"
                f" Max-Age=34560000{secure}"
            ), (address, said)
            assert get(c, "/", as_cookie(f"{prefix}tripbox_session", token)).status_code == 200
            other = "" if prefix else "__Secure-"  # the other name is never read
            assert get(c, "/", as_cookie(f"{other}tripbox_session", token)).status_code == 401
            out = fresh(c).post(
                "/logout",
                headers={**SAME, **said, **as_cookie(f"{prefix}tripbox_session", token)},
                follow_redirects=False,
            )
            assert set_cookies(out) == {
                "tripbox_session": f"tripbox_session=; HttpOnly; SameSite=Lax; Path={path};"
                f" Max-Age=0{secure}",
                "__Secure-tripbox_session": "__Secure-tripbox_session=; HttpOnly;"
                f" SameSite=Lax; Path={path}; Max-Age=0; Secure",
            }, (address, said)


# ----------------------------------------------------------------------------- sessions
def test_a_session_slides_for_30_days_and_ends_at_180(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)

    def seen(token: str) -> str:
        return next(r["seen_at"] for r in rows(ctx, "sessions") if r["token_hash"] == digest(token))

    with util.clock_at(T0):
        kept = as_cookie("tripbox_session", session_of(sign_in(c)))
        idle = as_cookie("tripbox_session", session_of(sign_in(c)))
    token = kept["Cookie"].split("=", 1)[1]
    # seen_at is rewritten at most once an hour
    with util.clock_at(T0 + dt.timedelta(minutes=59)):
        assert get(c, "/", kept).status_code == 200
    assert seen(token) == T0.isoformat()
    with util.clock_at(T0 + dt.timedelta(minutes=61)):
        assert get(c, "/", kept).status_code == 200
    assert seen(token) == (T0 + dt.timedelta(minutes=61)).isoformat()
    # 30 days without a visit end it
    with util.clock_at(T0 + dt.timedelta(days=30)):
        assert get(c, "/", idle).status_code == 401
    assert len(rows(ctx, "sessions")) == 1  # the ended one is gone from the ledger
    # one seen every 29 days lives until 180 days after it was made, and no longer
    day = 0
    while day + 29 < 180:
        day += 29
        with util.clock_at(T0 + dt.timedelta(days=day)):
            assert get(c, "/", kept).status_code == 200, day
    with util.clock_at(T0 + dt.timedelta(days=180) - dt.timedelta(seconds=1)):
        assert get(c, "/", kept).status_code == 200
    with util.clock_at(T0 + dt.timedelta(days=180)):
        assert get(c, "/", kept).status_code == 401
        r = get(c, "/", kept, headers=PAGE_LOAD, follow_redirects=False)
        assert r.status_code == 303 and urlsplit(r.headers["location"]).path == c.url("/login")
    assert rows(ctx, "sessions") == []


def test_a_changed_password_ends_every_session(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    first = as_cookie("tripbox_session", session_of(sign_in(c)))
    second = as_cookie("tripbox_session", session_of(sign_in(c)))
    devices = rows(ctx, "devices")
    # a restart with the same password keeps every session
    c, ctx = page(tmp_path, monkeypatch, base)
    assert get(c, "/", first).status_code == get(c, "/", second).status_code == 200
    # another password ends them all, in the ledger too; known devices stay known
    other = "another-plainly-fake-password"
    c, ctx = page(tmp_path, monkeypatch, base, env={"TRIPBOX_PASSWORD_HASH": cheap_hash(other)})
    assert rows(ctx, "sessions") == []
    assert get(c, "/", first).status_code == get(c, "/", second).status_code == 401
    assert rows(ctx, "devices") == devices
    assert sign_in(c).status_code == 401 and sign_in(c, other).status_code == 303
    # a password set as itself is hashed once at start with the ledger's own salt: a restart
    # gives the same hash, so its sessions last; the hash line instead is another password
    plain = {"TRIPBOX_PASSWORD": other, "TRIPBOX_PASSWORD_HASH": ""}
    c, ctx = page(tmp_path, monkeypatch, base, env=plain)
    assert rows(ctx, "sessions") == []
    with Ledger.open(ctx.settings.ledger_path) as led:
        salt = led.kv.get("login_salt")
    assert salt and re.fullmatch(r"[A-Za-z0-9_-]{22}", salt)
    third = as_cookie("tripbox_session", session_of(sign_in(c, other)))
    c, ctx = page(tmp_path, monkeypatch, base, env=plain)
    assert get(c, "/", third).status_code == 200
    with Ledger.open(ctx.settings.ledger_path) as led:
        assert led.kv.get("login_salt") == salt


def test_a_move_to_https_ends_every_session(tmp_path, monkeypatch, base):
    """A session is made under the page's origin as well as its password: one made while the page
    was reached over http (where its token may have been seen on the way) is no session once the
    page moves to https, whatever name its cookie is sent under."""
    c, ctx = page(tmp_path, monkeypatch, base)
    token = session_of(sign_in(c))
    c, ctx = page(tmp_path, monkeypatch, base)  # the same address: it lasts
    assert get(c, "/", as_cookie("tripbox_session", token)).status_code == 200
    c, ctx = page(tmp_path, monkeypatch, base, address=HTTPS)
    assert rows(ctx, "sessions") == []
    for name in ("__Secure-tripbox_session", "tripbox_session"):
        assert get(c, "/", as_cookie(name, token)).status_code == 401, name
    assert sign_in(c).status_code == 303  # the password is the same: signing in again works


def test_a_visit_that_cannot_be_written_keeps_the_owner_signed_in(
    tmp_path, monkeypatch, base, caplog
):
    """The hourly rewrite of a session's last visit is a write: a ledger that refuses it (locked,
    or read-only) leaves the page load signed in, with one line in the log that holds no token."""
    c, ctx = page(tmp_path, monkeypatch, base)
    with util.clock_at(T0):
        token = session_of(sign_in(c))

    def refused(self, key):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Sessions, "touch", refused)
    caplog.set_level(logging.DEBUG, logger="tripbox")
    with util.clock_at(T0 + dt.timedelta(hours=2)):
        r = get(c, "/", as_cookie("tripbox_session", token))
    assert r.status_code == 200
    (line,) = [rec for rec in caplog.records if rec.name == "tripbox.web.login"]
    assert line.levelname == "WARNING" and "could not be written" in line.getMessage()
    assert token not in caplog.text and digest(token) not in caplog.text
    (row,) = rows(ctx, "sessions")
    assert row["seen_at"] == T0.isoformat()  # unchanged, and the session still stands


def test_a_good_sign_ins_repr_shows_neither_token():
    signed = _Signed("session-token-not-for-a-log", "device-token-not-for-a-log")
    assert "token-not-for-a-log" not in repr(signed) and "token-not-for-a-log" not in str(signed)


def test_two_cookies_of_one_name_are_each_looked_at(tmp_path, monkeypatch, base):
    """A browser may send a cookie name once for each path that matches: a good session among
    them signs the request in, in either order; none good, it is not; more than four of them
    and only the first four are looked at."""
    c, _ = page(tmp_path, monkeypatch, base)
    good = session_of(sign_in(c))
    other = "N" * 43  # a token's shape, no session's
    for pair in ((good, other), (other, good)):
        cookie = {"Cookie": "; ".join(f"tripbox_session={t}" for t in pair)}
        assert get(c, "/", cookie).status_code == 200, pair
    split = {"Cookie": f"tripbox_session={other}"}
    two_headers = [("cookie", f"tripbox_session={other}"), ("cookie", f"tripbox_session={good}")]
    assert get(c, "/", split).status_code == 401
    assert fresh(c).get("/", headers=two_headers).status_code == 200
    many = [f"tripbox_session={'M' * 42}{n}" for n in range(4)] + [f"tripbox_session={good}"]
    assert get(c, "/", {"Cookie": "; ".join(many)}).status_code == 401
    assert get(c, "/", {"Cookie": "; ".join(many[1:])}).status_code == 200


def test_500_sign_ins_leave_50_sessions(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    made = [sign_in(c) for _ in range(500)]
    assert {r.status_code for r in made} == {303}
    assert len(rows(ctx, "sessions")) == 50 and len(rows(ctx, "devices")) == 50
    tokens = [session_of(r) for r in made]
    assert {r["token_hash"] for r in rows(ctx, "sessions")} == {digest(t) for t in tokens[-50:]}
    assert get(c, "/", as_cookie("tripbox_session", tokens[-1])).status_code == 200
    assert get(c, "/", as_cookie("tripbox_session", tokens[-50])).status_code == 200
    assert get(c, "/", as_cookie("tripbox_session", tokens[0])).status_code == 401


# ----------------------------------------------------------------------------- the throttle
@pytest.fixture
def hashed(monkeypatch) -> list[int]:
    """One entry per password the page hashes."""
    calls: list[int] = []
    real = passwords.hashlib.scrypt

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(passwords.hashlib, "scrypt", counted)
    return calls


def test_a_stranger_fills_only_the_shared_bucket_and_a_known_device_still_signs_in(
    tmp_path, monkeypatch, base, hashed
):
    c, ctx = page(tmp_path, monkeypatch, base)
    with util.clock_at(T0):
        owner = sign_in(c)
        device = as_cookie("tripbox_device", value(set_cookies(owner)["tripbox_device"]))["Cookie"]
        for n in range(10):
            r = sign_in(c, f"a stranger's guess {n}")
            assert r.status_code == 401, n
        assert len(hashed) == 11
        for guess in ("a stranger's eleventh guess", PASSWORD):
            r = sign_in(c, guess)
            assert r.status_code == 429 and r.headers["retry-after"] == "900"
            assert "Too many wrong passwords. Try again in 15 minutes." in r.text
            assert set_cookies(r) == {}
        assert len(hashed) == 11  # a closed bucket hashes nothing
        # the owner's browser has a bucket of its own
        r = sign_in(c, cookie=device)
        assert r.status_code == 303 and len(hashed) == 12
        assert value(set_cookies(r)["tripbox_device"]) == device.split("=", 1)[1]  # kept
        assert sign_in(c, "the owner mistyped", cookie=device).status_code == 401
        # a device cookie the page never gave, or one with a session beside it, is a stranger's
        for cookie in (
            "tripbox_device=made-up-device-id",
            f"tripbox_session={session_of(r)}",
            "tripbox_device=" + "y" * 22,
        ):
            r = sign_in(c, cookie=cookie)
            assert r.status_code == 429, cookie
        assert len(hashed) == 13
        # the shared bucket stays closed for strangers, the owner's own still takes its guesses
        assert sign_in(c, cookie=device).status_code == 303
    assert len(rows(ctx, "devices")) == 1


def test_a_closed_bucket_opens_after_15_minutes(tmp_path, monkeypatch, base, hashed):
    c, _ = page(tmp_path, monkeypatch, base)
    with util.clock_at(T0):
        for n in range(10):
            assert sign_in(c, f"a stranger's guess {n}").status_code == 401
        assert sign_in(c).status_code == 429
    for later, wait, minutes in ((300, 600, 10), (899, 1, 1), (840, 60, 1), (839, 61, 2)):
        with util.clock_at(T0 + dt.timedelta(seconds=later)):
            r = sign_in(c)
        assert (r.status_code, r.headers["retry-after"]) == (429, str(wait)), later
        said = "1 minute" if minutes == 1 else f"{minutes} minutes"
        assert f"Try again in {said}." in r.text, later
        assert "1 minutes" not in r.text, later
    assert len(hashed) == 10
    with util.clock_at(T0 + dt.timedelta(seconds=900)):
        assert sign_in(c, "one more guess").status_code == 401  # open again: hashed, counted
        assert sign_in(c).status_code == 303  # and the right one empties the bucket
        for n in range(9):
            assert sign_in(c, f"a stranger's guess {n}").status_code == 401
        assert sign_in(c).status_code == 303


# ----------------------------------------------------------------------------- signing out
def test_sign_out_ends_the_session_on_the_server_and_clears_the_site(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    r = sign_in(c)
    token, device = session_of(r), value(set_cookies(r)["tripbox_device"])
    signed = as_cookie("tripbox_session", token)
    other = as_cookie("tripbox_session", session_of(sign_in(c)))  # another browser's
    assert get(c, "/", signed).status_code == 200
    out = fresh(c).post("/logout", headers={**SAME, **signed}, follow_redirects=False)
    assert (out.status_code, out.headers["location"]) == (303, c.url("/login?f=out"))
    expected = '"cache", "storage"' if base == "" else '"cache"'
    assert out.headers.get_list("clear-site-data") == [expected]
    assert set(set_cookies(out)) == {"tripbox_session", "__Secure-tripbox_session"}
    assert all("Max-Age=0" in line for line in set_cookies(out).values())
    # the old cookie replayed is refused: the session is gone from the ledger
    assert get(c, "/", signed).status_code == 401
    r = get(c, "/", signed, headers=PAGE_LOAD, follow_redirects=False)
    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == c.url("/login")
    assert digest(token) not in {s["token_hash"] for s in rows(ctx, "sessions")}
    assert get(c, "/", other).status_code == 200  # another browser stays signed in
    # the device stays known
    assert [d["id_hash"] for d in rows(ctx, "devices")].count(digest(device)) == 1
    shown = get(c, "/login?f=out")
    assert shown.status_code == 200 and "You are signed out." in shown.text
    assert "You are signed out." not in get(c, "/login").text
    # signing out with no session, or a stale one, says the same
    for headers in (SAME, {**SAME, **signed}):
        out = fresh(c).post("/logout", headers=headers, follow_redirects=False)
        assert (out.status_code, out.headers["location"]) == (303, c.url("/login?f=out"))
        assert out.headers.get_list("clear-site-data") == [expected]


def test_sign_out_and_sign_in_need_a_same_origin_post(tmp_path, monkeypatch, base, hashed):
    c, ctx = page(tmp_path, monkeypatch, base)
    token = session_of(sign_in(c))
    signed = as_cookie("tripbox_session", token)
    before = len(hashed)
    for claims in (
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site", "Origin": "https://other.example"},
        {"Origin": "null"},
        {},
    ):
        r = fresh(c).post(
            "/login", data={"password": PASSWORD}, headers=claims, follow_redirects=False
        )
        assert r.status_code == 403 and set_cookies(r) == {}, claims
        r = fresh(c).post("/logout", headers={**claims, **signed}, follow_redirects=False)
        assert r.status_code == 403 and set_cookies(r) == {}, claims
        assert "clear-site-data" not in r.headers, claims
    assert len(hashed) == before  # nothing was hashed
    assert get(c, "/", signed).status_code == 200  # still signed in
    r = fresh(c).post(
        "/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert r.status_code == 303
    r = fresh(c).post("/logout", headers={"Origin": ORIGIN, **signed}, follow_redirects=False)
    assert r.status_code == 303 and get(c, "/", signed).status_code == 401


# ----------------------------------------------------------------------------- the challenge
def test_a_page_request_goes_to_the_login_page_and_a_fetch_gets_401(tmp_path, monkeypatch, base):
    c, ctx = page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    urls = Urls.of(ctx.settings)
    asked = f"/trips/{trip.id}?past=all"
    for sent in {c.url(asked), asked}:  # under the base, and as a proxy that strips it sends it
        for method in ("GET", "HEAD"):
            r = fresh(c).request(method, sent, headers=PAGE_LOAD, follow_redirects=False)
            assert r.status_code == 303, (sent, method)
            where = urlsplit(r.headers["location"])
            assert where.path == c.url("/login") and not where.netloc
            assert parse_qsl(where.query) == [("next", c.url(asked))]
            assert urls.inside(c.url(asked))
    # anything but a page load is a 401, never a redirect: a fetch, another method, a post
    for method, headers in (
        ("GET", {"Accept": "application/json"}),
        ("GET", {}),
        ("OPTIONS", PAGE_LOAD),
        ("DELETE", PAGE_LOAD),
        ("PUT", PAGE_LOAD),
    ):
        r = fresh(c).request(
            method, c.url(f"/trips/{trip.id}"), headers=headers, follow_redirects=False
        )
        assert (r.status_code, r.text) == (401, "unauthorized"), (method, headers)
        assert "location" not in r.headers
    r = fresh(c).post(
        f"/trips/{trip.id}/notes",
        data={"notes": "x"},
        headers={**SAME, **PAGE_LOAD},
        follow_redirects=False,
    )
    assert (r.status_code, r.text) == (401, "unauthorized") and "location" not in r.headers
    assert ctx.trips.get(trip.id).notes is None
    # a path the login page could not send back to: the login page with no `next`
    for raw in (f"{base}//elsewhere.example/".encode(), (f"{base}/" + "x" * 100_000).encode()):
        status, headers, _ = raw_get(c.app, raw, list(PAGE_LOAD.items()))
        assert (status, headers["location"]) == (303, c.url("/login")), raw[:40]
    # followed, the redirect signs in and lands on the page asked for
    r = get(c, asked, headers=PAGE_LOAD, follow_redirects=False)
    goto = dict(parse_qsl(urlsplit(r.headers["location"]).query))["next"]
    r = sign_in(c, next=goto)
    assert (r.status_code, r.headers["location"]) == (303, c.url(asked))
    shown = get(c, c.local(r.headers["location"]), as_cookie("tripbox_session", session_of(r)))
    assert shown.status_code == 200 and trip.title in shown.text


# ----------------------------------------------------------------------------- secrets
def test_the_password_and_tokens_reach_no_log_or_problem(tmp_path, monkeypatch, base, caplog):
    """The page as `tripbox web` serves it, its log file and every logger at DEBUG: what is typed,
    the hash and every token stay out of each, and out of every problem and doctor's report."""
    for name in [n for n in os.environ if n.startswith("TRIPBOX_")]:
        monkeypatch.delenv(name)
    env = {
        "TRIPBOX_AUTH": "password",
        "TRIPBOX_BASE_URL": HTTPS + base,
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
    }
    hidden: list[str] = [PASSWORD, HASHED]
    said: list[str] = []
    caplog.set_level(logging.DEBUG)
    for login in ({"TRIPBOX_PASSWORD_HASH": HASHED}, {"TRIPBOX_PASSWORD": PASSWORD}):
        for name, setting in {**env, **login}.items():
            monkeypatch.setenv(name, setting)
        for name in {"TRIPBOX_PASSWORD", "TRIPBOX_PASSWORD_HASH"} - set(login):
            monkeypatch.delenv(name, raising=False)
        served: list = []
        monkeypatch.setattr(uvicorn, "run", lambda app, served=served, **kw: served.append(app))
        assert cli.main(["web"]) == 0
        (app,) = served
        if "TRIPBOX_PASSWORD" in login:  # the line the page derived from it is a secret too
            with Ledger.open(tmp_path / "data" / "ledger.db") as led:
                salt = led.kv.get("login_salt")
            derived = passwords.make_hash(PASSWORD, base64.urlsafe_b64decode(salt + "=="))
            assert derived != HASHED
            hidden.append(derived)
            hidden.append(derived.rsplit(":", 1)[1])  # its key alone
        c = Page(app, base)
        r = sign_in(c)
        assert r.status_code == 303
        lines = set_cookies(r)
        token = value(lines["__Secure-tripbox_session"])
        hidden += [token, value(lines["__Secure-tripbox_device"])]
        signed = as_cookie("__Secure-tripbox_session", token)
        assert get(c, "/", signed).status_code == 200
        if "TRIPBOX_PASSWORD_HASH" in login:  # the bucket closes, which is logged
            for n in range(11):
                assert sign_in(c, f"{PASSWORD}-{n}").status_code == (429 if n == 10 else 401)
        out = fresh(c).post("/logout", headers={**SAME, **signed}, follow_redirects=False)
        assert out.status_code == 303 and get(c, "/", signed).status_code == 401
        assert get(c, "/", signed, headers=PAGE_LOAD, follow_redirects=False).status_code == 303
    assert any("wrong passwords" in record.getMessage() for record in caplog.records)
    said += [record.getMessage() for record in caplog.records] + [caplog.text]
    said += [p.read_text() for p in (tmp_path / "logs").glob("*")]
    # every problem and doctor's every row, the values set in every wrong way
    for wrong in (
        {"TRIPBOX_PASSWORD": "S3CRET-a"},
        {"TRIPBOX_PASSWORD": PASSWORD, "TRIPBOX_PASSWORD_HASH": HASHED},
        {"TRIPBOX_PASSWORD_HASH": PASSWORD},
        {"TRIPBOX_PASSWORD_HASH": HASHED + "x"},
        {"TRIPBOX_PASSWORD": PASSWORD},
        {"TRIPBOX_PASSWORD": "S3CRET-a" * 200},  # longer than a browser may send
        {"TRIPBOX_PASSWORD": HASHED},  # a hash line where the password goes
    ):
        s = Settings.from_env({**env, **wrong})
        said += [str(p) for p in s.problems] + report(s) + [repr(s)]
    hidden.append("S3CRET-a")
    for text in said:
        for secret in hidden:
            assert secret not in text, secret[:12]


def test_a_ledger_that_refuses_writes_warns_once_and_never_fails_a_request(
    tmp_path, monkeypatch, base, caplog
):
    """A ledger that refuses every write (a read-only mount, a file another user owns): a
    session's visit that cannot be written keeps it signed in and one that should end is ended
    all the same (the login page, never a 500); the log says so at most once an hour."""
    c, ctx = page(tmp_path, monkeypatch, base)
    with util.clock_at(T0):
        kept = as_cookie("tripbox_session", session_of(sign_in(c)))
        idle = as_cookie("tripbox_session", session_of(sign_in(c)))

    def refused(self, key):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(Sessions, "touch", refused)
    monkeypatch.setattr(Sessions, "end", refused)
    caplog.set_level(logging.DEBUG, logger="tripbox")

    def warnings() -> int:
        said = [r for r in caplog.records if r.name == "tripbox.web.login"]
        return sum(r.levelname == "WARNING" for r in said)

    for minutes in (120, 150, 179):
        with util.clock_at(T0 + dt.timedelta(minutes=minutes)):
            assert get(c, "/", kept).status_code == 200, minutes
    assert warnings() == 1
    with util.clock_at(T0 + dt.timedelta(minutes=181)):
        assert get(c, "/", kept).status_code == 200
    assert warnings() == 2
    with util.clock_at(T0 + dt.timedelta(days=30, minutes=1)):
        assert get(c, "/", idle).status_code == 401
        r = get(c, "/", idle, headers=PAGE_LOAD, follow_redirects=False)
        assert r.status_code == 303 and urlsplit(r.headers["location"]).path == c.url("/login")
    assert warnings() == 3
    assert len(rows(ctx, "sessions")) == 2  # nothing was written
