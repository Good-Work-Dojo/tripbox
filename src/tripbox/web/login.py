# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Password sign-in (TRIPBOX_AUTH=password): one shared password, set in the settings as itself or
as the line `tripbox passwd` prints, never chosen in the browser. A session is a random token in
a cookie, of which the ledger keeps the SHA-256; a browser that has signed in before keeps a
device cookie, which gives it a throttle bucket of its own, so a stranger's wrong passwords never
lock the owner out. Each cookie's name and its Secure flag follow TRIPBOX_BASE_URL alone: at the
root of an https origin a `__Host-` name, which no other host can set or shadow; elsewhere over
https a `__Secure-` one; over http the bare name."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import logging
import math
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection, Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from tripbox import passwords, util
from tripbox.config import ConfigError
from tripbox.log import add_secrets
from tripbox.web.auth import Authenticator
from tripbox.web.urls import Urls

if TYPE_CHECKING:
    from tripbox.config import Settings
    from tripbox.ledger import Ledger

log = logging.getLogger("tripbox.web.login")

SESSION = "tripbox_session"
DEVICE = "tripbox_device"
SECURE = "__Secure-"  # the name's prefix, and the Secure flag, when the base URL is https ...
HOST = "__Host-"  # ... and this one instead when it is also the origin's root (base "")
SESSION_SECONDS = 15_552_000  # 180 days: the session cookie's Max-Age
DEVICE_SECONDS = 34_560_000  # 400 days: the device cookie's
IDLE = dt.timedelta(days=30)  # a session not seen for this long is over
LIFE = dt.timedelta(days=180)  # and so is one made this long ago, seen or not
TOUCH = dt.timedelta(hours=1)  # a session's seen_at is rewritten at most this often
FAILURES = 10  # wrong passwords that close a throttle bucket ...
WINDOW = 900  # ... within this many seconds; it opens as they age past it
SALT_KV = "login_salt"  # the ledger's salt for a password set as itself
USER = "password"  # who a signed-in request is (`scope["state"]["user"]`)
WRONG = "That password is not right."
TOO_MANY = "Too many wrong passwords. Try again in {n} minutes."
TOO_MANY_ONE = "Too many wrong passwords. Try again in 1 minute."  # one minute or less left
# A cookie's value as this page writes it; anything else is no token of ours
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The values of a cookie name looked at: a browser may send one for each path that matches, and a
# sibling host may set some of them (a `__Host-` name, which only this host can set at "/", is not
# capped)
_MAX_VALUES = 4


def cookie_prefix(urls: Urls) -> str:
    """The cookies' names' prefix: `__Host-` at the root of an https origin (a name only this host
    can set, at the path "/"), `__Secure-` under a sub-path of one, none over http."""
    if not urls.secure:
        return ""
    return HOST if urls.base == "" else SECURE


def digest(token: str) -> str:
    """What the ledger keeps of a token: its SHA-256, in hex."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class Throttle:
    """Wrong passwords counted per bucket, in memory: FAILURES of them within WINDOW seconds close
    the bucket until the oldest is WINDOW seconds old. A browser that has signed in before uses
    its own bucket (the device's hash); every other request shares SHARED. Admission, the check
    and the count run under one lock, so one password is hashed at a time and a closed bucket
    hashes nothing; a good password empties its bucket. A restart empties them all."""

    SHARED = ""

    def __init__(self, failures: int = FAILURES, window: int = WINDOW) -> None:
        self.failures = failures
        self.window = window
        self._lock = threading.Lock()
        self._wrong: dict[str, list[float]] = {}

    def _recent(self, bucket: str, now: float) -> list[float]:
        """The bucket's wrong passwords still within the window (a time ahead of the clock, which
        went back, is not); a bucket left with none is dropped."""
        recent = [t for t in self._wrong.get(bucket, ()) if 0 <= now - t < self.window]
        if recent:
            self._wrong[bucket] = recent
        else:
            self._wrong.pop(bucket, None)
        return recent

    def attempt(self, bucket: str, check: Callable[[], bool]) -> tuple[bool, int]:
        """(True, 0) when `check` says yes; (False, 0) when it says no, which is counted;
        (False, seconds until it opens) while the bucket is closed, `check` not called."""
        with self._lock:
            now = util.now().timestamp()
            recent = self._recent(bucket, now)
            if len(recent) >= self.failures:
                return False, max(1, math.ceil(recent[-self.failures] + self.window - now))
            if check():
                self._wrong.pop(bucket, None)
                return True, 0
            self._wrong[bucket] = [*recent, now]
            if len(recent) + 1 == self.failures:
                who = "a browser that signed in before" if bucket else "browsers new to the page"
                log.warning(
                    "sign-in: %d wrong passwords within %d minutes; closed to %s for %d minutes",
                    self.failures,
                    self.window // 60,
                    who,
                    self.window // 60,
                )
            return False, 0


@dataclass(frozen=True)
class _Signed:
    """A good sign-in: the new session's token and the device's id, both for cookies only (never
    in its repr)."""

    token: str = field(repr=False)
    device: str = field(repr=False)


class PasswordLogin(Authenticator):
    """The page's own sign-in: see the module's docstring. `cred`, the first 16 hex of the
    SHA-256 of the page's origin and the password's hash line, is stamped on every session: at
    start every session under another one ends, so a changed password, or a move to another
    origin (from http to https), ends every session and a restart none."""

    name = "password"

    def __init__(
        self,
        settings: Settings,
        ledger_factory: Callable[[], Ledger],
        *,
        owns_ledger: bool = True,
    ) -> None:
        problems = [str(p) for p in settings.stopping("web")]
        if settings.tripbox_auth != "password" or problems:
            raise ConfigError("\n".join(problems) or "TRIPBOX_AUTH is not password")
        self.urls = Urls.of(settings)
        self._open = ledger_factory
        self._owns = owns_ledger
        prefix = cookie_prefix(self.urls)  # the setting decides, never a request
        self.session_name = prefix + SESSION
        self.device_name = prefix + DEVICE
        # what a page at the root of an https origin called them before it took `__Host-` names
        self.older = (SECURE + SESSION, SECURE + DEVICE) if prefix == HOST else ()
        self.throttle = Throttle()
        self._warned: dt.datetime | None = None  # when a refused write was last logged
        with self._ledger() as led:
            self.hash = self._hash(settings, led)
            made_under = f"{settings.origin}\n{self.hash.line}".encode()
            self.cred = hashlib.sha256(made_under).hexdigest()[:16]
            led.sessions.keep_only(self.cred)
        add_secrets([self.hash.line])  # masked in the log, should anything ever hold it

    # ------------------------------------------------------------------------- set up
    @contextmanager
    def _ledger(self) -> Iterator[Ledger]:
        led = self._open()
        try:
            yield led
        finally:
            if self._owns:
                led.close()

    @staticmethod
    def _hash(settings: Settings, led: Ledger) -> passwords.Hash:
        """The hash line set, or the password set hashed once with the ledger's salt (made on
        first use), so a restart gives the same line."""
        if settings.password_hash.strip():
            return passwords.parse_hash(settings.password_hash)
        fresh = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")
        kept = led.kv.setdefault(SALT_KV, fresh)
        try:
            salt = base64.urlsafe_b64decode(kept + "=" * (-len(kept) % 4))
        except (binascii.Error, ValueError):
            salt = b""
        if len(salt) != passwords.SALT_BYTES:  # not one this page wrote: a new one
            led.kv.set(SALT_KV, fresh)
            salt = base64.urlsafe_b64decode(fresh + "==")  # 22 characters: 16 bytes
        return passwords.parse_hash(passwords.make_hash(settings.password, salt))

    # ------------------------------------------------------------------------- cookies
    @staticmethod
    def _values(request: HTTPConnection, name: str) -> list[str]:
        """The values the request's cookies give `name` that have a token's shape, in the order
        sent: at most _MAX_VALUES of them, unless `name` is a `__Host-` one."""
        most = 0 if name.startswith(HOST) else _MAX_VALUES
        found: list[str] = []
        for header in request.headers.getlist("cookie"):
            for part in header.split(";"):
                key, sep, value = part.strip().partition("=")
                if sep and key == name and _TOKEN.fullmatch(value) and value not in found:
                    found.append(value)
                    if len(found) == most:
                        return found
        return found

    def _cookie(self, name: str, value: str, max_age: int) -> str:
        """A Set-Cookie line: HttpOnly, SameSite=Lax, the base's path, never a Domain; Secure over
        https (and for a name that asks for it)."""
        line = (
            f"{name}={value}; HttpOnly; SameSite=Lax; Path={self.urls.cookie_path};"
            f" Max-Age={max_age}"
        )
        asks = name.startswith((SECURE, HOST))
        return line + "; Secure" if self.urls.secure or asks else line

    # ------------------------------------------------------------------------- who is signed in
    def authenticate(self, request: HTTPConnection) -> str | None:
        tokens = self._values(request, self.session_name)
        if not tokens:
            return None
        with self._ledger() as led:
            return USER if any(self._good(led, token) for token in tokens) else None

    def _good(self, led: Ledger, token: str) -> bool:
        """Whether `token` is a session's: made under this password and origin, seen within IDLE
        and made within LIFE. One that is not any longer ends; one seen over TOUCH ago is seen
        now. A ledger that refuses either write (locked, or read-only) changes neither answer:
        the one is ended all the same, the other stays signed in (`_refused`)."""
        key = digest(token)
        row = led.sessions.get(key)
        if row is None:
            return False
        now = util.now()
        seen, made = util.parse_iso(row["seen_at"]), util.parse_iso(row["created_at"])
        if row["cred"] != self.cred or now - seen >= IDLE or now - made >= LIFE:
            try:
                led.sessions.end(key)
            except sqlite3.OperationalError as e:
                self._refused("a session that ended", e)
            return False
        if now - seen >= TOUCH:
            try:
                led.sessions.touch(key)
            except sqlite3.OperationalError as e:
                self._refused("a session's last visit", e)
        return True

    def _refused(self, what: str, e: sqlite3.OperationalError) -> None:
        """Log a write the ledger refused, at most once every TOUCH: a ledger that refuses
        every write would otherwise log on every page load."""
        now = util.now()
        if self._warned is not None and dt.timedelta(0) <= now - self._warned < TOUCH:
            return
        self._warned = now
        log.warning("sign-in: %s could not be written (%s)", what, e)

    def _next(self, value: object) -> str | None:
        """`value`, when it is a page under the base (`Urls.inside`) other than this login's
        own; else None."""
        if not isinstance(value, str) or not self.urls.inside(value):
            return None
        path = value.split("?", 1)[0].split("#", 1)[0]
        if path in (self.urls.path("/login"), self.urls.path("/logout")):
            return None
        return value

    def challenge(self, request: HTTPConnection) -> Response:
        """A page load (GET or HEAD accepting text/html) goes to the login page, with the page it
        asked for as `next`: its route path as the app sees it (the base already off) put back
        under the base, with its query. Anything else is a 401, never a redirect: for a browser's
        form (another method accepting text/html) a page linking the login page, with the page
        the form came from as `next` (`_came_from`); for a script the plain word."""
        method = request.scope.get("method", "")
        if "text/html" not in request.headers.get("accept", "").lower():
            return PlainTextResponse("unauthorized", status_code=401)
        where = self.urls.path("/login")
        if method in ("GET", "HEAD"):
            query = request.scope.get("query_string", b"").decode("latin-1")
            asked = self.urls.path(request.scope.get("path", "")) + (f"?{query}" if query else "")
            if self._next(asked):
                where += "?" + urlencode({"next": asked})
            return RedirectResponse(where, status_code=303)
        came_from = self._came_from(request.headers.get("referer", ""))
        if came_from:
            where += "?" + urlencode({"next": came_from})
        return request.app.state.templates.TemplateResponse(
            Request(request.scope), "ended.html", {"login": where}, status_code=401
        )

    def _came_from(self, referer: str) -> str | None:
        """The path of `referer` when it is an address of this page's origin and a page under the
        base the login could send back to (`_next`); else None. Its query is left: what was
        filtered can be filtered again."""
        try:
            parts = urlsplit(referer)
        except ValueError:
            return None
        if f"{parts.scheme}://{parts.netloc}".lower() != self.urls.origin.lower():
            return None
        return self._next(parts.path)

    # ------------------------------------------------------------------------- the routes
    def routes(self) -> list[BaseRoute]:
        return [
            Route("/login", self.login, methods=["GET", "POST"], name="login"),
            Route("/logout", self.logout, methods=["POST"], name="logout"),
        ]

    def logout_url(self) -> str | None:
        return None  # signing out is a post to /logout, from the page itself

    def _page(
        self,
        request: Request,
        goto: str | None,
        *,
        error: str = "",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        context = {
            "next": goto,
            "error": error,
            "signed_out": request.method == "GET" and request.query_params.get("f") == "out",
        }
        return request.app.state.templates.TemplateResponse(
            request, "login.html", context, status_code=status, headers=headers
        )

    async def login(self, request: Request) -> Response:
        if request.method == "POST":
            return await self._sign_in(request)
        goto = self._next(request.query_params.get("next"))
        if await run_in_threadpool(self.authenticate, request):
            return RedirectResponse(goto or self.urls.path("/"), status_code=303)
        return self._page(request, goto)

    async def _sign_in(self, request: Request) -> Response:
        async with request.form() as form:
            typed, goto = form.get("password"), self._next(form.get("next"))
        typed = typed if isinstance(typed, str) else ""
        devices = self._values(request, self.device_name)
        if self.older:  # a device known under the older name keeps its bucket
            devices += self._values(request, self.older[1])
        signed = await run_in_threadpool(self._check, devices, typed)
        if isinstance(signed, int):
            if not signed:
                return self._page(request, goto, error=WRONG, status=401)
            minutes = math.ceil(signed / 60)
            said = TOO_MANY_ONE if minutes == 1 else TOO_MANY.format(n=minutes)
            return self._page(
                request, goto, error=said, status=429, headers={"Retry-After": str(signed)}
            )
        r = RedirectResponse(goto or self.urls.path("/"), status_code=303)
        for name, value, seconds in (
            (self.session_name, signed.token, SESSION_SECONDS),
            (self.device_name, signed.device, DEVICE_SECONDS),
        ):
            r.headers.append("set-cookie", self._cookie(name, value, seconds))
        for name in self.older:  # the newer names stand in for them
            r.headers.append("set-cookie", self._cookie(name, "", 0))
        return r

    def _check(self, devices: list[str], typed: str) -> _Signed | int:
        """Off the event loop: the typed password through the throttle, in the bucket of the
        known device among `devices` (else the shared one). A good one makes a session and keeps
        the device (a new one when none was known); else 0 for a wrong one, or the seconds until
        a closed bucket opens."""
        with self._ledger() as led:
            device = next((d for d in devices if led.devices.get(digest(d))), None)
        bucket = digest(device) if device else Throttle.SHARED
        good, wait = self.throttle.attempt(bucket, lambda: passwords.matches(self.hash, typed))
        if not good:
            return wait
        token = secrets.token_urlsafe(32)
        with self._ledger() as led:
            led.sessions.add(digest(token), self.cred)
            if device:
                led.devices.touch(digest(device))
            else:
                device = secrets.token_urlsafe(16)
                led.devices.add(digest(device))
        return _Signed(token, device)

    async def logout(self, request: Request) -> Response:
        """Ends the session the request carries, if any, on the server; tells the browser to drop
        the session cookie under each of its names and what it cached (and, when the page has its
        origin to itself, what it stored); the device cookie stays."""
        tokens = [t for name in self._session_names() for t in self._values(request, name)]
        if tokens:
            await run_in_threadpool(self._end, tokens)
        return self._signed_out()

    async def end_everywhere(self, request: Request) -> Response:
        """Ends every session on the server, this browser's and every other's, then signs this
        browser out as `logout` does. The known devices stay known: each browser keeps its own
        throttle bucket."""
        await run_in_threadpool(self._end_all)
        log.info("sign-in: every session ended")
        return self._signed_out()

    def _signed_out(self) -> Response:
        """The answer of a sign-out: to the login page, saying so, with the session cookie
        dropped under each of its names and the site's cache (and, when the page has its origin
        to itself, its storage) cleared."""
        r = RedirectResponse(self.urls.path("/login") + "?f=out", status_code=303)
        for name in self._session_names():
            r.headers.append("set-cookie", self._cookie(name, "", 0))
        # under a sub-path the origin may be another app's too: its storage is not ours to clear
        r.headers["Clear-Site-Data"] = '"cache", "storage"' if self.urls.base == "" else '"cache"'
        return r

    def _session_names(self) -> tuple[str, ...]:
        """Every name a session cookie of this page may have had: the bare and `__Secure-` ones
        (an origin moved between http and https), and the `__Host-` one where it is used."""
        return (SESSION, SECURE + SESSION) + ((HOST + SESSION,) if self.older else ())

    def _end(self, tokens: list[str]) -> None:
        with self._ledger() as led:
            for token in tokens:
                led.sessions.end(digest(token))

    def _end_all(self) -> None:
        with self._ledger() as led:
            led.sessions.end_all()
