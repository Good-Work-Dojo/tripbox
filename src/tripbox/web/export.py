# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A static, read-only copy of the demo page: the list, the list with every past trip, and each
trip either links, as plain files a host such as GitHub Pages can serve.

The pages are the real ones. `create_app` runs over a copy of a seeded demo ledger with no login
and no saved copies, under a base path of its own (`EXPORT_BASE`), and each page is asked for
in-process (an ASGI call, no socket) with the clock held at noon of the demo's day. One worker
record is written into the copy at that instant, by the function the worker's beat uses, so no
page says the worker stopped.

Each page is then rewritten with `html.parser`: an address under the base that names a written
page or file becomes relative to the page it stands on; a link to anything else is switched off;
every form and control is disabled; every script, the manifest link and the attributes that
start the service worker go; the head gains a content policy that allows the page's own
stylesheet, images and fonts and nothing else; the body begins with the banner and ends with the
about line (what the page is, its licence and where its source code is). The same day gives the
same tree, byte for byte."""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import posixpath
import shutil
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from tripbox import corpus, demo, util
from tripbox.web import app as webapp

BANNER = "Demo: invented trips. Read-only."
# The base path the pages are rendered under; every address under it is rewritten or switched off.
EXPORT_BASE = "/demo-copy"
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none';"
    " form-action 'none'"
)
# What the page is served from, in the files of the package, and what is copied beside the pages.
STATIC = webapp.HERE / "static"
DEMO_CSS = webapp.HERE / "demo" / "demo.css"
SCRIPTS = ("app.js", "clear.js")  # the page's scripts: never copied
INDEX, PAST = "index.html", "past.html"
CONTROLS = frozenset({"input", "select", "textarea", "button"})
VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
CONTENT_TYPE = "text/html"


class ExportError(RuntimeError):
    """A page that did not render as the page does (its address and status)."""


@dataclass(frozen=True)
class Exported:
    """What an export wrote: pages (HTML files) and files (everything else)."""

    pages: int
    files: int


# ----------------------------------------------------------------------------- asking the page
async def _get(app, target: str) -> tuple[int, bytes]:
    """`target` (as the page is addressed, under the base) asked of `app` in-process: its status
    and body."""
    path, _, query = target.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 80),
    }
    body = bytearray()
    status = [0]

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status[0] = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return status[0], bytes(body)


async def _fetch(app, paths: list[str]) -> dict[str, tuple[int, bytes]]:
    """Each of `paths` asked of `app` (`_get`), in order."""
    return {target: await _get(app, target) for target in paths}


class _Links(HTMLParser):
    """The trip pages a page links to, in page order: `<a href>` under the base that names one."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        parts = urlsplit(href)
        prefix = EXPORT_BASE + "/trips/"
        if not parts.scheme and not parts.netloc and parts.path.startswith(prefix):
            rest = parts.path[len(prefix) :]
            if rest.isdigit() and rest not in self.found:
                self.found.append(rest)


def _trip_ids(page: bytes) -> list[str]:
    links = _Links()
    links.feed(page.decode("utf-8"))
    links.close()
    return links.found


# ----------------------------------------------------------------------------- rewriting a page
class _Rewriter(HTMLParser):
    """One page, rewritten (see the module). `here` is the page's own path in the export (such as
    `trips/3.html`), `pages` and `files` what is written, `about` the about line's HTML."""

    def __init__(
        self, here: str, pages: dict[str, str], files: set[str], about: str, source: str
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.here = here
        self.pages = pages  # the address of a page under the base -> its path in the export
        self.files = files
        self.about = about
        self.source = source
        self.out: list[str] = []
        self.skipping = 0  # inside a <script>

    # -- addresses
    def _target(self, address: str) -> str | None:
        """The path in the export an address under the base names, or None."""
        parts = urlsplit(address)
        if parts.scheme or parts.netloc:
            return None
        path = parts.path
        if path == EXPORT_BASE:
            path += "/"
        if not path.startswith(EXPORT_BASE + "/"):
            return None
        inside = path[len(EXPORT_BASE) :]
        key = inside + (f"?{parts.query}" if parts.query else "")
        if key in self.pages:
            return self.pages[key]
        if inside.startswith("/static/") and inside[1:] in self.files:
            return inside[1:]
        return None

    def _relative(self, target: str, fragment: str) -> str:
        start = posixpath.dirname(self.here) or "."
        return posixpath.relpath(target, start) + (f"#{fragment}" if fragment else "")

    # -- tags
    def _tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
        kept: list[tuple[str, str | None]] = []
        disable_link = False
        values = dict(attrs)
        if tag == "form":
            attrs = [(k, v) for k, v in attrs if k not in ("action", "method")]
        if tag == "link" and "manifest" in (values.get("rel") or "").split():
            return []
        for name, value in attrs:
            if name in ("data-base", "data-sw"):
                continue
            if name in ("href", "src") and value is not None and tag != "base":
                address = value
                parts = urlsplit(address)
                if tag == "a" and not parts.scheme and not parts.netloc and not parts.path:
                    kept.append((name, value))  # a fragment on this page
                    continue
                target = self._target(address)
                if target is not None:
                    kept.append((name, self._relative(target, parts.fragment)))
                elif tag == "a":
                    if value == self.source:
                        kept.append((name, value))
                    else:
                        disable_link = True
                continue
            kept.append((name, value))
        if disable_link:
            classes = [v for k, v in kept if k == "class" and v]
            kept = [(k, v) for k, v in kept if k != "class"]
            kept.append(("class", " ".join([*classes, "demo-off"])))
            kept.append(("aria-disabled", "true"))
        if tag in CONTROLS and not any(k == "disabled" for k, _ in kept):
            kept.append(("disabled", None))
        return kept

    @staticmethod
    def _text(tag: str, attrs: list[tuple[str, str | None]], closing: str = "") -> str:
        written = "".join(
            f" {k}" if v is None else f' {k}="{html.escape(v, quote=True)}"' for k, v in attrs
        )
        return f"<{tag}{written}{closing}>"

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.skipping += 1
            return
        if tag == "link" and "manifest" in (dict(attrs).get("rel") or "").split():
            return
        self.out.append(self._text(tag, self._tag(tag, attrs)))
        if tag == "head":
            self.out.append(f'<meta http-equiv="Content-Security-Policy" content="{CSP}">')
        elif tag == "body":
            self.out.append(f'<p class="demo-banner" role="note">{BANNER}</p>')

    def handle_startendtag(self, tag, attrs):
        if tag == "script":
            return
        if tag == "link" and "manifest" in (dict(attrs).get("rel") or "").split():
            return
        self.out.append(self._text(tag, self._tag(tag, attrs), " /"))

    def handle_endtag(self, tag):
        if tag == "script":
            self.skipping = max(0, self.skipping - 1)
            return
        if self.skipping:
            return
        if tag == "head":
            depth = posixpath.relpath("static/demo.css", posixpath.dirname(self.here) or ".")
            self.out.append(f'<link rel="stylesheet" href="{depth}">')
        elif tag == "body":
            self.out.append(self.about)
        if tag not in VOID:
            self.out.append(f"</{tag}>")

    # -- the rest is copied as it came
    def handle_data(self, data):
        if not self.skipping:
            self.out.append(data)

    def handle_entityref(self, name):
        if not self.skipping:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skipping:
            self.out.append(f"&#{name};")

    def handle_comment(self, data):
        if not self.skipping:
            self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.out.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.out.append(f"<![{data}]>")


def _rewrite(
    page: bytes, here: str, pages: dict[str, str], files: set[str], about: str, source: str
) -> bytes:
    rewriter = _Rewriter(here, pages, files, about, source)
    rewriter.feed(page.decode("utf-8"))
    rewriter.close()
    return "".join(rewriter.out).encode("utf-8")


# ----------------------------------------------------------------------------- the export
def _static_files() -> dict[str, Path]:
    """What is copied beside the pages, by its path under `static/`: the stylesheet, the fonts
    with their licence and the two icons; never a script."""
    found = {}
    for path in sorted(STATIC.rglob("*")):
        if path.is_file() and path.name not in SCRIPTS:
            found[path.relative_to(STATIC).as_posix()] = path
    return found


def _render(c: corpus.Corpus, seeded: Path, today: dt.date) -> tuple[dict[str, bytes], str]:
    """The pages of the demo `seeded` (a `demo.seed` directory), as `path in the export -> HTML`,
    and the about line. The ledger is used in a copy; `seeded` is never written."""
    from tripbox.pipeline.run import build_context
    from tripbox.worker import Worker

    with tempfile.TemporaryDirectory(prefix="tripbox-export-") as tmp:
        work = Path(tmp)
        shutil.copytree(seeded / "data", work / "data")
        s = corpus.settings(
            c,
            data_dir=work / "data",
            archive_dir=work / "archive",
            logs_dir=work / "logs",
            config_dir=seeded / corpus.CONFIG_DIR,
            environ={
                "TRIPBOX_BASE_URL": "http://localhost" + EXPORT_BASE,
                "TRIPBOX_OFFLINE": "false",
            },
        )
        from zoneinfo import ZoneInfo

        noon = dt.datetime.combine(today, dt.time(12), tzinfo=ZoneInfo(s.tz))
        with corpus.process_as(s), util.clock_at(noon):
            ctx = build_context(s, with_model=False, with_notifier=False)
            try:
                worker = Worker(ctx, clock=util.now)
                worker.started_at = noon
                with ctx.ledger.transaction():
                    worker._write(noon)
            finally:
                ctx.ledger.close()
            from tripbox.ledger import Ledger

            app = webapp.create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)
            fetched = asyncio.run(_fetch(app, [f"{EXPORT_BASE}/", f"{EXPORT_BASE}/?past=all"]))
            raw = {}
            for target, name in ((f"{EXPORT_BASE}/", INDEX), (f"{EXPORT_BASE}/?past=all", PAST)):
                status, body = fetched[target]
                if status != 200:
                    raise ExportError(f"{target}: status {status}")
                raw[name] = body
            ids = list(dict.fromkeys([*_trip_ids(raw[INDEX]), *_trip_ids(raw[PAST])]))
            trips = asyncio.run(_fetch(app, [f"{EXPORT_BASE}/trips/{i}" for i in ids]))
            for i in ids:
                status, body = trips[f"{EXPORT_BASE}/trips/{i}"]
                if status != 200:
                    raise ExportError(f"{EXPORT_BASE}/trips/{i}: status {status}")
                raw[f"trips/{i}.html"] = body
            templates = app.state.templates
            about = templates.get_template("_about.html").render(
                version=templates.env.globals["version"],
                source_url=s.source_url,
            )
        source = s.source_url
    pages = {"/": INDEX, "/?past=all": PAST}
    pages.update({f"/trips/{i}": f"trips/{i}.html" for i in ids})
    files = {f"static/{p}" for p in _static_files()} | {"static/demo.css"}
    about_block = f'<div class="page demo-about">{about.strip()}</div>'
    written = {
        here: _rewrite(body, here, pages, files, about_block, source) for here, body in raw.items()
    }
    return written, source


def export(c: corpus.Corpus, out: Path, today: dt.date) -> Exported:
    """Write the demo of `c`, its days counted from `today`, into the folder `out` (made if it
    is not there). `demo.DemoError` before anything is written for a corpus or a day `demo.seed`
    refuses and for an `out` that holds anything; an email that was not filed is `Failed`
    (nothing is written); an OSError for a folder that cannot be written."""
    out = Path(out).absolute()
    if out.is_dir() and any(out.iterdir()):
        raise demo.DemoError(f"{out} is not empty")
    with tempfile.TemporaryDirectory(prefix="tripbox-seed-") as tmp:
        seeded = demo.seed(c, Path(tmp) / "demo", today)
        if seeded.failed:
            raise Failed(seeded.failed)
        pages, _ = _render(c, Path(tmp) / "demo", today)
    out.mkdir(parents=True, exist_ok=True)
    for here, body in pages.items():
        target = out / here
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    files = _static_files()
    for name, path in files.items():
        target = out / "static" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    (out / "static" / "demo.css").write_bytes(DEMO_CSS.read_bytes())
    (out / ".nojekyll").write_bytes(b"")
    return Exported(len(pages), len(files) + 2)


class Failed(RuntimeError):
    """An email of the demo that was not filed (`<entry>: <status>`), so nothing was written."""

    def __init__(self, failed: tuple[str, ...]) -> None:
        super().__init__("; ".join(failed))
        self.failed = failed
