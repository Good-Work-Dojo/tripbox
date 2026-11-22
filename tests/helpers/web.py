# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page under test at a base path: a client whose requests are written from `/` and sent under
the base, and the bases every page test runs at."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tripbox.ledger import Ledger
from tripbox.web.app import create_app

if TYPE_CHECKING:
    from tripbox.pipeline.run import Context

# The base paths a page test runs at (the `base` fixture): the host's root, one segment, two
BASES = ("", "/tripbox", "/a/b")
ORIGIN = "http://localhost:8110"
# The socket peer of every request a test sends (a Page's unless it names a `client`, and
# raw_get's): the address a page behind a login proxy trusts as its proxy (test_web_auth.PROXY)
PEER = ("127.0.0.1", 50000)


def attribution_line() -> str:
    """The line NOTICE's term 7(b) asks a copy to keep in the pages' about line."""
    text = (Path(__file__).resolve().parents[2] / "NOTICE").read_text(encoding="utf-8")
    (line,) = re.findall(r'the line "([^"]+)" in a copy of', " ".join(text.split()))
    return line


class Page(TestClient):
    """A client of the page served under `base`: `get` and `post` take a path written from `/`
    and send it under the base or, `stripped`, without it, as a proxy that strips the base sends
    it; `url(p)` is the address a page must link for the path `p`. Its requests come from PEER
    unless `client` names another peer."""

    def __init__(self, app, base: str = "", *, stripped: bool = False, **kw) -> None:
        kw.setdefault("client", PEER)
        super().__init__(app, base_url=ORIGIN, **kw)
        self.base = base
        self.stripped = stripped

    def url(self, path: str) -> str:
        return self.base + path

    def local(self, address: str) -> str:
        """The path, written from `/`, of an address the page gives (a link or a Location),
        which lies under the base."""
        assert address.startswith(self.base + "/"), address
        return address[len(self.base) :]

    def _sent(self, path: str) -> str:
        return path if self.stripped else self.url(path)

    def get(self, path: str, **kw):  # type: ignore[override]
        return super().get(self._sent(path), **kw)

    def post(self, path: str, **kw):  # type: ignore[override]
        return super().post(self._sent(path), **kw)


def starlette_of(app) -> Starlette:
    """The Starlette app inside the page's wrappers (each holds the next as `app`)."""
    while not isinstance(app, Starlette):
        app = app.app
    return app


def raw_get(
    app, raw_path: bytes, headers=(), *, client=PEER, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    """GET (or `method`, with an empty body) `raw_path` (the bytes of the request line's target,
    query included) from the ASGI `app` through a scope built by hand as a server builds it
    (`path` percent-decoded, `root_path` empty, the socket's peer `client`, None for none), for
    spellings a client would normalise. `headers`: (name, value) pairs, a Host of localhost:8110
    unless they name one. The status, the response's headers by lower-case name and its body."""
    target, _, query = raw_path.partition(b"?")
    if not any(k.lower() == "host" for k, _ in headers):
        headers = [("host", "localhost:8110"), *headers]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(target.decode("latin-1")),
        "raw_path": target,
        "root_path": "",
        "query_string": query,
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": client,
        "server": ("localhost", 8110),
        "state": {},
    }
    sent: list[dict] = []
    asked = False
    done = asyncio.Event()

    async def receive() -> dict:
        # the empty body once; then the client goes once the whole response is sent (a file
        # response listens for that while it streams)
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    named = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], named, body


def client_for(
    tmp_path,
    monkeypatch,
    *,
    base: str = "",
    auth: str = "none",
    airtrail: bool = True,
    stripped: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[Page, Context]:
    """The page over a new ledger (`test_run.make_ctx`, with a flight log kept unless
    `airtrail` is false) and its context: its settings are read from make_ctx's environment,
    the login `auth`, the page's address under `base` and `env`. The client's requests come from
    PEER (behind a login proxy: from the proxy, when `env` trusts PEER)."""
    from test_run import make_ctx  # a test module: imported once a page is built

    page_env = {"TRIPBOX_AUTH": auth, "TRIPBOX_BASE_URL": ORIGIN + base, **(env or {})}
    ctx, _ = make_ctx(tmp_path, monkeypatch, [], airtrail=airtrail, env=page_env)
    s = ctx.settings
    app = create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)
    return Page(app, base, stripped=stripped), ctx
