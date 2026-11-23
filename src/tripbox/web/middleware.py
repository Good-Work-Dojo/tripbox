# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page's wrappers, outermost first: `BasePath` takes the base path off each request, so every
route is written from `/`; `Headers` puts the security headers on every response; `BodyLimit`
refuses a body past its limit; `SameOrigin` refuses a cross-site form post. Inside the app,
`LoginRequired` asks the login before any private route or a path no route takes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import unquote_to_bytes

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

if TYPE_CHECKING:
    from tripbox.web.auth import Authenticator


def _policy(scripts: str) -> str:
    """The page's content policy, its scripts `scripts` ('self', or 'none')."""
    return (
        f"default-src 'self'; script-src {scripts}; style-src 'self'; img-src 'self';"
        " font-src 'self'; connect-src 'self'; worker-src 'self'; manifest-src 'self';"
        " form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
    )


SECURITY_HEADERS = {
    # everything the page loads is its own (its font included): no other site, no inline script
    # or style, and no page may frame it
    "Content-Security-Policy": _policy("'self'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    # a household's private page is never meant to be in a search engine's index
    "X-Robots-Tag": "noindex, nofollow",
}
# Under SHARED (a shared trip's page, which anyone holding its link opens, and its bare 404): no
# script runs, the address is sent to no site it links, and a search engine indexes nothing (as
# everywhere)
SHARED = "/s/"
SHARED_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": _policy("'none'"),
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}
# The service worker's script: never kept stale (`no-cache`), so a browser asks for it again
# whenever it checks for a new release
WORKER = "/sw.js"
# A cover photo's address: its picture's SHA-256, `-s` for the small one. A photo never changes
# under its name, so the browser keeps one it was given (COVER_KEPT), and only for this user.
COVER = re.compile(r"/covers/([0-9a-f]{64})(-s)?\.jpg")
COVER_KEPT = "private, max-age=31536000, immutable"
# The most of a request's body the page reads, unless its path has a limit of its own
LIMIT = 65_536


def security_headers(path: str, status: int = 200) -> dict[str, str]:
    """What every response to `path` (written from `/`) with `status` carries: SECURITY_HEADERS,
    SHARED_HEADERS in their place under SHARED, and, unless it is a static file's,
    `Cache-Control`: `no-cache` for the service worker's script (WORKER), COVER_KEPT for a cover
    photo given (a 200 at a COVER address: a refusal there, the login's challenge or a 404, is
    never kept), `no-store` for every other."""
    if path.startswith("/static/"):
        return dict(SECURITY_HEADERS)
    if path.startswith(SHARED):
        return {**SHARED_HEADERS, "Cache-Control": "no-store"}
    if path == WORKER:
        return {**SECURITY_HEADERS, "Cache-Control": "no-cache"}
    if status == 200 and COVER.fullmatch(path):
        return {**SECURITY_HEADERS, "Cache-Control": COVER_KEPT}
    return {**SECURITY_HEADERS, "Cache-Control": "no-store"}


def has_control(path: str, raw: bytes) -> bool:
    """Whether a request's path holds a character below 0x20 or 0x7f: as decoded, or in its raw
    form once percent-decoded."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path) or any(
        byte < 0x20 or byte == 0x7F for byte in unquote_to_bytes(raw)
    )


class BasePath:
    """Serves `app` under `base` ("" or such as /a/b). A path holding a control character
    (`has_control`) is a 404 with an empty body and the security headers before anything else
    looks at it: a route pattern would take one ending in a line break. A request for the base
    itself is sent on to `<base>/` (307, with the security headers; a path and the query, never
    an absolute address). A path under `<base>/` loses the base, in `path` and in `raw_path`,
    both as they were sent; any other path passes as it is, as a proxy that strips the base
    hands it on. `root_path` becomes "", so routes match on the path alone. Nothing is
    normalised: an encoded spelling of the base is not the base. `state` is the app's (inside
    its wrappers)."""

    def __init__(self, app: ASGIApp, base: str) -> None:
        self.app = app
        self.base = base
        inner = app
        while getattr(inner, "state", None) is None and hasattr(inner, "app"):
            inner = inner.app
        self.state = getattr(inner, "state", None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        raw: bytes = scope.get("raw_path") or path.encode("utf-8")
        under = self.base + "/"
        if has_control(path, raw):
            if scope["type"] == "http":
                # never kept; under the shared prefix, with the shared page's own headers
                inner = path[len(self.base) :] if self.base and path.startswith(under) else path
                asked = inner if inner.startswith(SHARED) else ""
                gone = Response(status_code=404, headers=security_headers(asked, 404))
                await gone(scope, receive, send)
            else:
                await WebSocketClose(code=1008)(scope, receive, send)
            return
        if self.base and path == self.base and raw == self.base.encode():
            if scope["type"] == "http":
                query = scope.get("query_string", b"").decode("latin-1")
                where = self.base + "/" + (f"?{query}" if query else "")
                headers = security_headers(self.base, 307)
                await RedirectResponse(where, 307, headers)(scope, receive, send)
                return
        if self.base and path.startswith(under) and raw.startswith(under.encode()):
            path, raw = path[len(self.base) :], raw[len(self.base) :]
        await self.app({**scope, "path": path, "raw_path": raw, "root_path": ""}, receive, send)


class Headers:
    """Sets `security_headers` on every response of the app, whatever answered it (a page, a
    refusal, a static file, the 500 handler), replacing any of the same name, so each is sent
    once. The values are the path's and the status's: under SHARED its own (SHARED_HEADERS),
    which no view needs to set and none can loosen."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def guarded(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in security_headers(scope["path"], message["status"]).items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, guarded)


class BodyLimit:
    """Refuses a request whose body is longer than its limit (`limits` by path, else LIMIT):
    a declared length over it is answered before a byte is read; a body is counted as it
    arrives, and the read that takes it past the limit is answered instead. Answer: 413, plain
    text `too large`."""

    def __init__(self, app: ASGIApp, *, limits: Mapping[str, int]) -> None:
        self.app = app
        self.limits = dict(limits)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limits.get(scope["path"], LIMIT)
        length = (HTTPConnection(scope).headers.get("content-length") or "").strip()
        if length.isascii() and length.isdecimal() and int(length) > limit:
            await PlainTextResponse("too large", status_code=413)(scope, receive, send)
            return
        came = 0

        async def counted() -> Message:
            nonlocal came
            message = await receive()
            if message["type"] == "http.request":
                came += len(message.get("body", b""))
                if came > limit:  # the app's own handling of an HTTPException answers it
                    raise HTTPException(413, "too large")
            return message

        await self.app(scope, counted, send)


class LoginRequired:
    """The private mount's one middleware, so it runs before every route the mount holds and for
    every path no route takes: a request someone is signed in on (the authenticator's say, asked
    off the event loop: it may read the ledger) goes on with who it is in
    `scope["state"]["user"]`; any other gets the authenticator's challenge (a websocket is
    closed)."""

    def __init__(self, app: ASGIApp, *, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = HTTPConnection(scope, receive)
        user = await run_in_threadpool(self.authenticator.authenticate, request)
        if not user:
            if scope["type"] == "http":
                await self.authenticator.challenge(request)(scope, receive, send)
            else:
                await WebSocketClose(code=1008)(scope, receive, send)
            return
        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


def same_origin(headers: Mapping[str, str], origin: str) -> bool:
    """A browser form post from the page itself: Sec-Fetch-Site `same-origin` or `none`, or an
    Origin equal to the page's origin (TRIPBOX_BASE_URL's). Anything else (another site, no
    metadata) is not; Host is never asked."""
    site = (headers.get("sec-fetch-site") or "").lower()
    if site in ("same-origin", "none"):
        return True
    sent = (headers.get("origin") or "").rstrip("/").lower()
    return bool(sent) and sent == origin.rstrip("/").lower()


class SameOrigin:
    """Refuses every POST that is not `same_origin` (403, plain text naming the page's origin),
    before anything reads it."""

    def __init__(self, app: ASGIApp, *, origin: str) -> None:
        self.app = app
        self.origin = origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            if not same_origin(HTTPConnection(scope).headers, self.origin):
                said = (
                    f"This page is set up for {self.origin}. Open it there, or set"
                    " TRIPBOX_BASE_URL to the address you use."
                )
                await PlainTextResponse(said, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
