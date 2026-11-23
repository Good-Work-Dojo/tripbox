# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox demo export`: the demo page as static, read-only files. Every page is the real one,
asked for in-process over a copy of a seeded demo ledger, then rewritten so that nothing in the
tree reaches the network, a script or a form; two exports of one day are the same tree."""

import datetime as dt
import hashlib
import posixpath
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tripbox import cli, corpus, demo
from tripbox.web import app as webapp
from tripbox.web import export

TODAY = dt.date(2027, 6, 16)
CONTROLS = ("input", "select", "textarea", "button")


class Page(HTMLParser):
    """A written page read back: its tags, in order, as (name, attributes)."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text = text
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag

    def find(self, name: str) -> list[dict[str, str | None]]:
        return [a for t, a in self.tags if t == name]


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def pages_of(files: dict[str, bytes]) -> dict[str, Page]:
    return {n: Page(b.decode("utf-8")) for n, b in files.items() if n.endswith(".html")}


@pytest.fixture(scope="module")
def corp():
    return corpus.load(corpus.default_dir())


@pytest.fixture(scope="module")
def site(tmp_path_factory, corp):
    out = tmp_path_factory.mktemp("site") / "demo"
    done = export.export(corp, out, TODAY)
    return out, done


def test_the_export_writes_the_list_the_past_list_and_every_trip(site):
    out, done = site
    files = tree(out)
    trips = sorted(n for n in files if n.startswith("trips/"))
    assert len(trips) == 7 and all(n.endswith(".html") for n in trips)
    assert {
        "index.html", "past.html", ".nojekyll", "static/app.css", "static/demo.css",
        "static/favicon.svg", "static/apple-touch-icon.png", "static/fonts/OFL.txt",
    } <= set(files)
    assert files[".nojekyll"] == b""
    assert not any(n.endswith(".js") for n in files)
    assert (done.pages, done.files) == (9, 10)
    assert done.pages + done.files == len(files)
    # the list links at least one trip, and the list of every past trip links them all
    index, past = pages_of(files)["index.html"], pages_of(files)["past.html"]
    assert any(a.get("href", "").startswith("trips/") for a in index.find("a"))
    linked = {a["href"] for a in past.find("a") if a.get("href", "").startswith("trips/")}
    assert linked == set(trips)


def test_two_exports_of_one_day_are_the_same_bytes(site, corp, tmp_path):
    again = tmp_path / "again"
    export.export(corp, again, TODAY)
    assert tree(again) == tree(site[0])


def test_every_address_is_relative_and_resolves(site):
    out, _ = site
    files = tree(out)
    checked = 0
    for name, page in pages_of(files).items():
        here = posixpath.dirname(name)
        for tag, attrs in page.tags:
            for key in ("href", "src"):
                value = attrs.get(key)
                if value is None or value.startswith("#"):
                    continue
                parts = urlsplit(value)
                if tag == "a" and parts.scheme:  # the about line's source link alone
                    assert value == "https://github.com/Good-Work-Dojo/tripbox", (name, value)
                    continue
                assert not parts.scheme and not parts.netloc and not value.startswith("/"), value
                target = posixpath.normpath(posixpath.join(here, parts.path))
                assert target in files, (name, value)
                checked += 1
    assert checked > 40
    # from a trip, the way back to the list goes up one folder
    trip = pages_of(files)["trips/1.html"]
    assert "../index.html" in [a.get("href") for a in trip.find("a")]


def test_a_link_to_a_page_that_is_not_exported_is_switched_off(site):
    out, _ = site
    pages = pages_of(tree(out))
    for name, page in pages.items():
        for attrs in page.find("a"):
            href = attrs.get("href") or ""
            for word in ("settings", "/add", "edit", "share", "trip.ics", "deleted"):
                assert word not in href, (name, href)
    index = pages["index.html"]
    off = {a.get("aria-label"): a for a in index.find("a") if a.get("aria-label")}
    for label in ("Settings", "Add a booking"):
        a = off[label]
        assert "href" not in a and a["aria-disabled"] == "true"
        assert "demo-off" in a["class"].split()
    trip = pages["trips/1.html"]
    disabled = [a for a in trip.find("a") if a.get("aria-disabled") == "true"]
    # the share page, the calendar file and a booking's edit page are among them
    assert len(disabled) >= 3 and all("href" not in a for a in disabled)
    assert any(a.get("data-edit") for a in disabled)
    assert all("demo-off" in a["class"].split() for a in disabled)


def test_every_form_is_disabled_and_no_script_or_worker_is_left(site):
    out, _ = site
    seen = 0
    for name, page in pages_of(tree(out)).items():
        assert not page.find("script") and "<script" not in page.text, name
        for attrs in page.find("form"):
            assert "action" not in attrs and "method" not in attrs, name
        for tag in CONTROLS:
            for attrs in page.find(tag):
                assert "disabled" in attrs, (name, tag, attrs)
                seen += 1
        for attrs in page.find("link"):
            assert attrs.get("rel") != "manifest", name
        for attrs in page.find("html"):
            assert "data-base" not in attrs and "data-sw" not in attrs, name
        assert "sw.js" not in page.text and "manifest" not in page.text
        (csp,) = [a for a in page.find("meta") if a.get("http-equiv") == "Content-Security-Policy"]
        assert csp["content"] == export.CSP
        assert page.find("link")[-1]["href"].endswith("static/demo.css")
    assert seen > 20


def test_each_page_carries_the_banner_once_and_the_about_line(site):
    out, _ = site
    for name, page in pages_of(tree(out)).items():
        assert page.text.count(export.BANNER) == 1, name
        (banner,) = [a for a in page.find("p") if a.get("class") == "demo-banner"]
        assert banner["role"] == "note"
        assert page.text.index('class="demo-banner"') < page.text.index("<main")
        assert 'class="about muted small"' in page.text and "Source code" in page.text
        assert "free software under the GNU Affero General Public License" in page.text


def test_no_page_says_the_worker_stopped(site):
    for name, text in tree(site[0]).items():
        if name.endswith(".html"):
            assert "The worker is not running" not in text.decode("utf-8"), name


def dump_of(root: Path) -> str:
    return demo.dump(root / "data" / "ledger.db", root / "archive")


def test_the_seeded_ledger_is_not_written(corp, tmp_path):
    demo.seed(corp, tmp_path / "seeded", TODAY)
    before = dump_of(tmp_path / "seeded")
    files = tree(tmp_path / "seeded")
    pages, _ = export._render(corp, tmp_path / "seeded", TODAY)
    assert "index.html" in pages
    assert dump_of(tmp_path / "seeded") == before and tree(tmp_path / "seeded") == files


def test_the_real_page_is_as_before(site, corp, tmp_path):
    """The export changes no file the page is made of, and the page itself still has what the
    export takes out: its scripts and its forms."""
    def digest() -> str:
        h = hashlib.sha256()
        for sub in ("static", "templates"):
            for p in sorted((webapp.HERE / sub).rglob("*")):
                if p.is_file():
                    h.update(p.read_bytes())
        return h.hexdigest()

    before = digest()
    export.export(corp, tmp_path / "again", TODAY)
    assert digest() == before
    served = tree(tmp_path / "again")
    assert b"<script" not in served["trips/1.html"]
    assert (webapp.HERE / "static" / "app.js").is_file()
    assert "app.js" in (webapp.HERE / "templates" / "base.html").read_text()


def run(capsys, *args: str) -> tuple[int, list[str], list[str]]:
    code = cli.main(["demo", "export", *args])
    seen = capsys.readouterr()
    return code, seen.out.splitlines(), seen.err.splitlines()


def test_export_from_the_command_line(corp, tmp_path, capsys, monkeypatch):
    out = tmp_path / "site"
    code, said, err = run(capsys, "--out", str(out), "--today", "2027-06-16")
    assert (code, err) == (0, [])
    assert said == [f"demo: 9 pages and 10 files written to {out}, today 2027-06-16"]
    # the refusals demo seed has, word for word
    code, said, err = run(capsys, "--out", str(out), "--today", "2027-06-16")
    assert (code, said, err) == (2, [], [f"demo: {out} is not empty"])
    fresh = tmp_path / "fresh"
    code, _, err = run(capsys, "--out", str(fresh), "--today", "27-6-16")
    assert (code, err) == (2, ["demo: --today 27-6-16 is not a day written YYYY-MM-DD"])
    code, _, err = run(capsys, "--out", str(fresh), "--today", "0001-01-01")
    assert (code, err) == (2, ["demo: --today is out of range"])
    assert not fresh.exists()
    # a folder that cannot be written
    code, _, err = run(capsys, "--out", str(out / "index.html" / "x"), "--today", "2027-06-16")
    assert (code, err) == (2, [f"demo: cannot write {out / 'index.html' / 'x'}"])
    # a write that fails midway leaves what the next run refuses, and says so
    halfway = tmp_path / "halfway"

    def breaks(c, to, today):
        to.mkdir()
        (to / "index.html").write_text("part\n")
        raise OSError("the disk is full")

    with monkeypatch.context() as broken:
        broken.setattr(export, "export", breaks)
        code, _, err = run(capsys, "--out", str(halfway), "--today", "2027-06-16")
    assert (code, err) == (2, [f"demo: cannot write {halfway}; remove it and export again"])
    # an email that was not filed: its line, nothing written, exit 1
    real = demo.seed

    def one_fails(c, to, today):
        seeded = real(c, to, today)
        return demo.Seeded(seeded.emails - 1, seeded.trips, seeded.bookings, seeded.unsorted,
                           ("sample-entry: failed",), seeded.ledger)

    monkeypatch.setattr(demo, "seed", one_fails)
    code, said, err = run(capsys, "--out", str(fresh), "--today", "2027-06-16")
    assert (code, said, err) == (1, [], ["demo: FAILED sample-entry: failed"])
    assert not fresh.exists()


def test_the_export_opens_no_socket(corp, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the export reached for the network")

    for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    done = export.export(corp, tmp_path / "site", TODAY)
    assert done.pages == 9


def test_a_demo_ledger_with_a_ready_cover_exports_tiles_only(tmp_path, corp, monkeypatch):
    """The export keeps the tile whatever the seeded ledger holds: no page names a cover file and
    no `.cover` holds an image."""
    from tripbox.ledger import Ledger

    real = demo.seed

    def seeded_with_covers(c, out, today):
        done = real(c, out, today)
        led = Ledger.open(next((out / "data").glob("*.db")))
        try:
            with led.transaction():
                for trip_id in [r[0] for r in led.conn.execute("SELECT id FROM trips")]:
                    led.covers.put(
                        trip_id, source="owner", state="ready", digest="ab" * 32,
                        large=b"L", small=b"S",
                    )
        finally:
            led.close()
        return done

    monkeypatch.setattr(demo, "seed", seeded_with_covers)
    out = tmp_path / "demo"
    export.export(corp, out, TODAY)
    files = tree(out)
    assert not any(b"/covers/" in b or b"covers/" in b for b in files.values())
    for name, page in pages_of(files).items():
        assert "<img" not in page.text, name
        assert 'class="cover photo' not in page.text and "has-photo" not in page.text, name
