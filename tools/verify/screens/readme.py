#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Photograph the README's four pictures from an exported demo site (`tripbox demo export`).

    python readme.py --site DIR --out DIR

The site in DIR is served on loopback inside this container, every request to another origin is
blocked (and fails the run), and the list and the trip under way are photographed in the light
scheme with animations off, as the viewport and not the whole page: `trips-390.png` and
`trip-390.png` (390 by 844 at scale 2), `trips-1280.png` and `trip-1280.png` (1280 by 800 at
scale 1). The trip is the first one the list links. A picture over 1,048,576 bytes, or holding a
text chunk (`tEXt`, `zTXt` or `iTXt`), fails by name. The last line is `readme shots: ok 4` or
`readme shots: FAIL <what>` (exit 1); a usage error exits 2. Playwright is imported only in
main(); the helpers need nothing but the standard library."""

from __future__ import annotations

import argparse
import functools
import http.server
import struct
import sys
import threading
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

SHOTS = (("390", 390, 844, 2), ("1280", 1280, 800, 1))  # name, width, height, scale
LIMIT = 1_048_576
TEXT_CHUNKS = (b"tEXt", b"zTXt", b"iTXt")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SETTLE_FONTS = "document.fonts.ready.then(() => true)"


class Failed(Exception):
    """A picture or a request that fails the run, as its last line says it."""


class _FirstTrip(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.found: str | None = None

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href") or ""
        if tag == "a" and self.found is None and href.startswith("trips/"):
            self.found = href


def first_trip(index: str) -> str | None:
    """The first trip page the list links, as the list writes it (`trips/<n>.html`), or None."""
    parser = _FirstTrip()
    parser.feed(index)
    parser.close()
    return parser.found


def png_chunks(data: bytes) -> list[bytes]:
    """The type of each chunk of a PNG, in order; Failed for bytes that are no PNG."""
    if not data.startswith(PNG_MAGIC):
        raise Failed("not a PNG")
    at, kinds = len(PNG_MAGIC), []
    while at + 8 <= len(data):
        (size,) = struct.unpack(">I", data[at : at + 4])
        kinds.append(data[at + 4 : at + 8])
        at += 12 + size
    return kinds


def check_picture(path: Path) -> None:
    """Failed, naming the file, for a picture over LIMIT bytes or holding a text chunk."""
    data = path.read_bytes()
    if len(data) > LIMIT:
        raise Failed(f"{path.name} is over {LIMIT} bytes")
    try:
        kinds = png_chunks(data)
    except Failed as why:
        raise Failed(f"{path.name} is {why}") from None
    if any(k in TEXT_CHUNKS for k in kinds):
        raise Failed(f"{path.name} holds a text chunk")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - the base class's name
        pass


def serve(site: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    """`site` served on a free loopback port in a thread; the server and its origin."""
    handler = functools.partial(_Quiet, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def other_origin(url: str, origin: str) -> bool:
    """Whether `url` is an http(s) or ws(s) address that is not under `origin`."""
    parts, own = urlparse(url), urlparse(origin)
    if parts.scheme not in ("http", "https", "ws", "wss"):
        return False
    return (parts.scheme, parts.netloc) != (own.scheme, own.netloc)


def shoot(browser, origin: str, trip: str, out: Path) -> None:
    """The four pictures, into `out`: each in a context of its own, light, scale as given,
    reduced motion; every request to another origin is aborted and, once the picture is taken,
    fails the run."""
    out.mkdir(parents=True, exist_ok=True)
    for name, width, height, scale in SHOTS:
        for picture, path in (("trips", "/index.html"), ("trip", f"/{trip}")):
            blocked: list[str] = []
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=scale,
                color_scheme="light",
                reduced_motion="reduce",
            )

            def gate(route, blocked=blocked):
                url = route.request.url
                if other_origin(url, origin):
                    blocked.append(urlparse(url).netloc)
                    route.abort()
                else:
                    route.continue_()

            ctx.route("**/*", gate)
            try:
                page = ctx.new_page()
                page.goto(origin + path, wait_until="load")
                page.evaluate(SETTLE_FONTS)
                target = out / f"{picture}-{name}.png"
                page.screenshot(path=str(target), full_page=False, animations="disabled")
            finally:
                ctx.close()
            if blocked:
                raise Failed(f"a request to another origin ({blocked[0]})")
            check_picture(target)


def run(site: Path, out: Path, launch) -> int:
    """The run: `site` served, the pictures taken through the browser `launch()` opens (a
    context manager), the last line said."""
    index = site / "index.html"
    if not index.is_file():
        print("readme shots: FAIL no index.html in the site")
        return 1
    trip = first_trip(index.read_text(encoding="utf-8"))
    if trip is None:
        print("readme shots: FAIL the list links no trip")
        return 1
    server, origin = serve(site)
    try:
        with launch() as browser:
            shoot(browser, origin, trip, out)
    except Failed as why:
        print(f"readme shots: FAIL {why}")
        return 1
    finally:
        server.shutdown()
        server.server_close()
    print(f"readme shots: ok {len(SHOTS) * 2}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--site", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    import contextlib

    from playwright.sync_api import sync_playwright

    @contextlib.contextmanager
    def launch():
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--disable-dev-shm-usage"])
            try:
                yield browser
            finally:
                browser.close()

    return run(args.site, args.out, launch)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
