# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The README's picture runner, with a stand-in for the browser: what it asks of the browser, the
pictures it refuses, the requests it blocks and the trip it photographs."""

import contextlib
import importlib.util
import struct
import urllib.request
import zlib
from pathlib import Path

import pytest

SCREENS = Path(__file__).resolve().parent / "screens"


def load():
    spec = importlib.util.spec_from_file_location("readme_shots", SCREENS / "readme.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


readme = load()


def chunk(kind: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(*extra: bytes, pad: int = 0) -> bytes:
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    data = chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00" + b"\x00" * pad))
    return readme.PNG_MAGIC + header + b"".join(extra) + data + chunk(b"IEND")


class Route:
    def __init__(self, url):
        self.request = type("Request", (), {"url": url})()
        self.did = None

    def abort(self):
        self.did = "abort"

    def continue_(self):
        self.did = "continue"


class Page:
    def __init__(self, ctx):
        self.ctx = ctx

    def goto(self, url, wait_until=None):
        self.ctx.browser.opened.append(url)
        with urllib.request.urlopen(url) as answer:  # the site is really served
            assert answer.status == 200
        for other in self.ctx.browser.asks:  # what the page would also ask for
            route = Route(other)
            self.ctx.handler(route)
            self.ctx.browser.routes.append((other, route.did))

    def evaluate(self, script):
        self.ctx.browser.evaluated.append(script)

    def screenshot(self, path, **kw):
        self.ctx.browser.shots.append((Path(path).name, self.ctx.kw, kw))
        Path(path).write_bytes(self.ctx.browser.picture)


class Context:
    def __init__(self, browser, kw):
        self.browser, self.kw, self.handler = browser, kw, None

    def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler

    def new_page(self):
        return Page(self)

    def close(self):
        pass


class Browser:
    def __init__(self, picture=None, asks=()):
        self.picture = picture or png()
        self.asks = list(asks)
        self.opened, self.shots, self.routes, self.evaluated = [], [], [], []

    def new_context(self, **kw):
        return Context(self, kw)


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    (root / "trips").mkdir(parents=True)
    (root / "index.html").write_text(
        '<a href="static/x">x</a><a href="trips/7.html">Seven</a><a href="trips/2.html">Two</a>'
    )
    (root / "trips" / "7.html").write_text("<p>seven</p>")
    (root / "trips" / "2.html").write_text("<p>two</p>")
    return root


def go(site, tmp_path, browser):
    @contextlib.contextmanager
    def launch():
        yield browser

    return readme.run(site, tmp_path / "out", launch)


def test_four_pictures_at_the_two_widths_in_the_light_scheme(site, tmp_path, capsys):
    browser = Browser()
    assert go(site, tmp_path, browser) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "readme shots: ok 4"
    names = [n for n, _, _ in browser.shots]
    assert sorted(names) == ["trip-1280.png", "trip-390.png", "trips-1280.png", "trips-390.png"]
    for name, ctx, shot in browser.shots:
        width = 390 if "390" in name else 1280
        assert ctx == {
            "viewport": {"width": width, "height": 844 if width == 390 else 800},
            "device_scale_factor": 2 if width == 390 else 1,
            "color_scheme": "light",
            "reduced_motion": "reduce",
        }
        assert shot == {"full_page": False, "animations": "disabled"}  # the viewport alone
    assert {p.name for p in (tmp_path / "out").iterdir()} == set(names)
    assert all(readme.SETTLE_FONTS == s for s in browser.evaluated)


def test_a_picture_over_the_limit_or_with_a_text_chunk_fails_by_name(site, tmp_path, capsys):
    big = png() + b"\x00" * readme.LIMIT
    assert go(site, tmp_path / "a", Browser(big)) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"readme shots: FAIL trips-390.png is over {readme.LIMIT} bytes"
    )
    for kind in (b"tEXt", b"zTXt", b"iTXt"):
        text = png(chunk(kind, b"Software\x00x"))
        assert go(site, tmp_path / kind.decode(), Browser(text)) == 1
        assert capsys.readouterr().out.splitlines()[-1] == (
            "readme shots: FAIL trips-390.png holds a text chunk"
        )
    assert go(site, tmp_path / "b", Browser(b"not a picture")) == 1
    assert "FAIL trips-390.png is not a PNG" in capsys.readouterr().out
    assert readme.png_chunks(png()) == [b"IHDR", b"IDAT", b"IEND"]


def test_a_request_to_another_origin_is_blocked_and_fails_the_run(site, tmp_path, capsys):
    browser = Browser(asks=["https://elsewhere.example/font.woff2", "data:text/plain,x"])
    assert go(site, tmp_path, browser) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (
        "readme shots: FAIL a request to another origin (elsewhere.example)"
    )
    assert browser.routes[0] == ("https://elsewhere.example/font.woff2", "abort")
    assert readme.other_origin("https://elsewhere.example/", "http://127.0.0.1:5")
    assert readme.other_origin("http://127.0.0.1:6/", "http://127.0.0.1:5")
    assert not readme.other_origin("http://127.0.0.1:5/a", "http://127.0.0.1:5")
    assert not readme.other_origin("data:text/plain,x", "http://127.0.0.1:5")
    own = Browser(asks=["http://127.0.0.1:1/never"])
    assert go(site, tmp_path / "own", own) == 1  # another port is another origin


def test_the_trip_photographed_is_the_first_the_list_links(site, tmp_path):
    browser = Browser()
    assert go(site, tmp_path, browser) == 0
    opened = [u.split("/", 3)[3] for u in browser.opened]
    assert sorted(set(opened)) == ["index.html", "trips/7.html"]  # seven, not two
    assert readme.first_trip('<a href="#a"><a href="trips/9.html"><a href="trips/1.html">') == (
        "trips/9.html"
    )
    assert readme.first_trip("<p>none</p>") is None


def test_a_site_with_no_list_or_no_trip_fails_before_a_browser_opens(tmp_path, capsys):
    (tmp_path / "site").mkdir()
    assert go(tmp_path / "site", tmp_path, None) == 1
    said = capsys.readouterr().out.splitlines()[-1]
    assert said == "readme shots: FAIL no index.html in the site"
    (tmp_path / "site" / "index.html").write_text("<p>nothing</p>")
    assert go(tmp_path / "site", tmp_path, None) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "readme shots: FAIL the list links no trip"


def test_the_browser_image_installs_under_its_constraints_file():
    lines = (SCREENS / "Dockerfile").read_text().splitlines()
    pins = [ln for ln in lines if "playwright==" in ln]
    assert len(pins) == 1 and pins[0].endswith("playwright==1.65.2")
    assert "-c /constraints.txt" in pins[0]
    assert "COPY constraints.txt /constraints.txt" in lines
    pinned = (SCREENS / "constraints.txt").read_text().splitlines()
    assert "greenlet==3.5.6" in pinned and "playwright==1.65.2" in pinned
