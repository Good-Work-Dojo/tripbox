#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Photograph the trips page: full-page screenshots at two widths and two colour schemes, and a
horizontal-overflow measure at a narrow width, for every page the ledger gives rise to.

    python shoot.py --base-url http://web:8110[/base/path] --out /out
                    [--password P] [--fresh-url URL] [--max-trips N] [--fail-on-overflow]
                    [--expect-covers]

With a password (`--password`, else the SCREENS_PASSWORD variable) the page is behind its own
sign-in: the pages are found signed in, every browser context signs in through the login form
before it loads its page, and the login page is photographed as it first shows and after a wrong
password. The pages are found from the page itself (the index, the trips it links, a trip's share
page, the shared page of a link made through that page's form and opened with no cookie, the edit
page of one booking of each kind, the deleted list, and a trip that does not exist), so any
ledger works (the first trip is taken a second time with its ⋯ sheet open, as `sheet-open`, so
the photo controls are in a shot); the pages that add a booking and Settings have fixed paths,
and an upload's status page is the one the paste form lands on once a fixed line is sent from it
in the browser (the same line each time, so every shot is of one upload). Once every other page
of the
ledger is taken, the first trip is taken again with the display choices of DAY_FIRST_24H saved
from its context (24-hour, day first), so the pages before it show the default. With
`--fresh-url` (else the SCREENS_FRESH_URL variable), the address of a brand-new install's page
under the same base path and password, its list is photographed last, as `first-run`.
Playwright is imported only in main(); the helpers above it need nothing but the standard
library (`send_paste` and `sign_in_form` drive a page main() hands it).

Every request a page makes is judged by its origin: one to another origin is blocked and counted,
and one to the page's own origin outside its base path is counted. Once the pictures are taken the
checks of checks.py (beside this file) run, and each one that is not ok fails the run: no violation
of the page's content policy, no request to another origin or outside the base, no control without
a name (the photo file input and its upload button included), every ⋯ sheet of a trip closing on
Escape, every page behind the sign-in, a shared page that runs no script, asks for nothing but
itself, the stylesheet, the font and the icon, and is in no store of the service worker's once
opened signed in, and a wrong link that is a bare 404, and saved copies for offline use, a trip's
cover photo among them (`offline`, in a context of its own, whose saved trip is photographed as
`offline-copy` after every page of the ledger).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SHOT_VIEWPORTS = ((390, 844), (1280, 800))
SCHEMES = ("light", "dark")
NARROW = (320, 640)
SETTLE_MS = 150
MISSING_TRIP = "/trips/999999"
EDIT_KINDS = ("flight", "lodging", "car", "train")  # one edit page of each kind found
ADD_PAGES = (
    ("add", "/add"),
    ("add-paste", "/add/paste"),
    ("add-upload", "/add/upload"),
    ("add-flight", "/add/flight"),  # the by-hand form, as every kind's is the edit form
)
PASTE_LINE = "Booking confirmation for the Sample family, pasted by the screenshot harness."
TRIP_PAGE = re.compile(r"/trips/\d+$")  # a trip's own page
STATUS_PAGE = re.compile(r"/uploads/[^/?#]+$")  # where a sent paste lands
LANDING_MS = 30000  # how long a sent paste may take to land on its status page
WRONG_PASSWORD = "not-the-harness-password"  # what the login-wrong page is sent
DAY_FIRST_24H = {"clock": "24", "dates": "dmy", "maps": "osm"}  # the styled trip's choices
# The share page's form that makes a link (its action, its fields) and a link it lists
LINK_FORM = re.compile(r'<form method="post" action="([^"]+)"[^>]*>(.*?)</form>', re.S)
TICKED = re.compile(r'<input type="checkbox" name="(\w+)" checked>')
WRONG_LINK = "harness-wrong-link-0000000000000"  # 32 URL-safe characters no link holds
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _beside(name: str):
    """The module `name`.py that lies beside this file, loaded from its path."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checks = _beside("checks")

OVERFLOW_JS = """() => {
  const vw = window.innerWidth;
  const els = [];
  for (const el of document.querySelectorAll('body *')) {
    if (els.length >= 5) break;
    if (el.getBoundingClientRect().right > vw + 0.5) {
      const cls = el.classList && el.classList.length ? '.' + el.classList[0] : '';
      els.push(el.tagName.toLowerCase() + cls);
    }
  }
  return {px: document.documentElement.scrollWidth - vw, elements: els};
}"""


def _links(html: str, pattern: str) -> list[str]:
    seen: list[str] = []
    for m in re.finditer(r'href="(' + pattern + r')"', html):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def trip_links(html: str, base_path: str) -> list[str]:
    """Distinct trip page paths under base_path, in page order."""
    return _links(html, re.escape(base_path) + r"/trips/\d+")


def edit_links(html: str, base_path: str) -> list[tuple[str, str]]:
    """The first item edit page path under base_path of each kind of booking, as (kind, path) in
    page order: the links that name their booking's kind (`data-edit`)."""
    page = re.compile(re.escape(base_path) + r"/items/\d+/edit")
    found: dict[str, str] = {}
    for tag in re.finditer(r"<a\b[^>]*>", html):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag.group(0)))
        kind, href = attrs.get("data-edit"), attrs.get("href", "")
        if kind and page.fullmatch(href):
            found.setdefault(kind, href)
    return list(found.items())


def origin_of(url: str, scheme: str = "") -> tuple[str, str, int | None] | None:
    """(scheme, host, port) of an http(s) or ws(s) URL, its default port written out (a
    scheme-relative one takes `scheme`; a port that is no number is None); None for a URL that
    names no such host: data:, about:, blob: and the like never leave the page."""
    parts = urlparse(url)
    s = (parts.scheme or scheme).lower()
    if s not in DEFAULT_PORTS or not parts.netloc:
        return None
    try:
        port = parts.port or DEFAULT_PORTS[s]
    except ValueError:
        port = None
    return s, (parts.hostname or "").lower(), port


def is_other_origin(url: str, origin: str) -> bool:
    """True for an http(s) or ws(s) URL, or a scheme-relative one, whose scheme, host or port
    differ from `origin`'s (the page's, scheme://host[:port]), a default port written out or
    not."""
    base = origin_of(origin)
    seen = origin_of(url, base[0] if base else "")
    return seen is not None and seen != base


def escapes_base(url: str, origin: str, base_path: str) -> bool:
    """True for a request to the page's own origin whose path is not under `base_path` + "/": a
    page that asks its server for something outside the base it is served under."""
    base = origin_of(origin)
    if base is None or origin_of(url, base[0]) != base:
        return False
    return not (urlparse(url).path or "/").startswith(base_path + "/")


def launch_options(*origins: str) -> dict:
    """How Chromium is launched for a run against the pages at `origins`. Inside the stack a page
    is plain http on a host that is not localhost, where Chromium sends no fetch metadata with a
    form post (`Sec-Fetch-Site`), and the page refuses a post that carries neither it nor its
    configured origin. The page is meant to be reached over https, where the browser sends it,
    so the browser is told to take these origins as secure and then sends the header itself.
    Only the full Chromium honours that flag (`channel` chromium, its new headless mode); the
    headless shell Playwright starts by default ignores it."""
    return {
        "channel": "chromium",
        "args": [
            "--disable-dev-shm-usage",
            f"--unsafely-treat-insecure-origin-as-secure={','.join(origins)}",
        ],
    }


def page_url(origin: str, p: dict) -> str:
    """The address of the planned page `p`: its path at its own origin (a brand-new install's),
    else at `origin`."""
    return (p.get("origin") or origin) + p["path"]


class MissedLanding(RuntimeError):
    """A sent paste did not land on its status page."""


class NotSignedIn(RuntimeError):
    """The login form did not sign the browser in."""


def shared_link(html: str, base_path: str) -> str:
    """The path of the first shared page a page lists (a link's Copy link: its whole address),
    under base_path; "" for none."""
    m = re.search(
        r'data-copy="(?:https?://[^/"]+)?(' + re.escape(base_path) + r'/s/[A-Za-z0-9_-]+)"', html
    )
    return m.group(1) if m else ""


def link_form(html: str, trip: str) -> tuple[str, dict[str, str]] | None:
    """The form of the share page of the trip page `trip` that makes a link: its action and what
    it posts as it stands (each box ticked by default, `on`); None when the page has none."""
    for m in LINK_FORM.finditer(html):
        if m.group(1) == f"{trip}/links":
            return m.group(1), {name: "on" for name in TICKED.findall(m.group(2))}
    return None


def missed_landing(answers: list[int]) -> str:
    """Why a sent paste did not land on its status page, from the statuses of the answers its
    post got, in order: the last of them, or that none came."""
    if not answers:
        return "the paste form's post got no answer"
    return f"the paste form's post was answered {answers[-1]}, not sent on to its status page"


def send_paste(page, text: str, timeout_ms: float = LANDING_MS):
    """Fill the paste form on `page` (a Playwright page showing it) with `text`, send it, and wait
    for the status page it lands on; that page's response. When it does not land there,
    MissedLanding says what the post was answered."""
    answers: list[int] = []

    def seen(response) -> None:
        if response.request.method == "POST":
            answers.append(response.status)

    page.on("response", seen)
    try:
        page.fill('textarea[name="text"]', text)
        with page.expect_navigation(
            url=STATUS_PAGE, wait_until="load", timeout=timeout_ms
        ) as landed:
            page.click('form button[type="submit"]')
        return landed.value
    except Exception as exc:  # the wait timed out, or the page went elsewhere
        raise MissedLanding(missed_landing(answers)) from exc
    finally:
        page.remove_listener("response", seen)


def send_password(page, typed: str, timeout_ms: float = LANDING_MS):
    """Type `typed` into the login form on `page` (a Playwright page showing it), send it and wait
    for the page that answers; that page's response."""
    page.fill('input[name="password"]', typed)
    with page.expect_navigation(wait_until="load", timeout=timeout_ms) as landed:
        page.click('form button[type="submit"]')
    return landed.value


def sign_in_form(page, login_url: str, password: str, timeout_ms: float = LANDING_MS) -> None:
    """Sign the browser context of `page` in through the login form at `login_url`; NotSignedIn
    when the answer is not a page off the login page."""
    page.goto(login_url, wait_until="load")
    landed = send_password(page, password, timeout_ms)
    status = landed.status if landed is not None else None
    if status != 200 or urlparse(landed.url).path == urlparse(login_url).path:
        raise NotSignedIn(f"the login form was answered {status}")


class _Stay(urllib.request.HTTPRedirectHandler):
    """Follows no redirect: the answer to a sign-in is the one that carries its cookie."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def session_cookie(set_cookies: list[str]) -> str:
    """The `name=value` of the session cookie among Set-Cookie lines (a name ending in
    `tripbox_session`, with a value), or ""."""
    for line in set_cookies:
        name, _, value = line.split(";", 1)[0].strip().partition("=")
        if name.endswith("tripbox_session") and value:
            return f"{name}={value}"
    return ""


def sign_in(origin: str, base_path: str, password: str) -> str:
    """Sign in through the page's login form, posted as a browser on the page posts it (with the
    page's own Origin): the Cookie header of the session it made, or "" when it made none."""
    request = urllib.request.Request(
        f"{origin}{base_path}/login",
        data=urllib.parse.urlencode({"password": password}).encode(),
        headers={"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_Stay).open(request, timeout=30) as r:
            lines = r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:  # the 303 that sets the cookie, or a refusal
        lines = e.headers.get_all("Set-Cookie") or []
    except OSError:
        return ""
    return session_cookie(lines)


def post_form(url: str, data: dict[str, str], origin: str, cookie: str = "") -> int:
    """POST `data` to `url` as a browser on the page posts a form (with the page's Origin, and
    `cookie` as its Cookie header when there is one), following no redirect: the answer's
    status, 0 when the connection failed."""
    headers = {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.build_opener(_Stay).open(request, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:  # the 303 a form is answered with, or a refusal
        return e.code
    except OSError:
        return 0


def make_link(origin: str, base_path: str, trip: str, cookie: str = "") -> str:
    """Make a share link of the trip page `trip` through its share page's form, as it stands
    (the boxes ticked by default): the path of the shared page the share page then lists, or ""
    when there is no form, its post is not sent on, or no link is listed."""
    form = link_form(fetch(f"{origin}{trip}/share", cookie)[1], trip)
    if form is None:
        return ""
    action, data = form
    if post_form(origin + action, data, origin, cookie) != 303:
        return ""
    return shared_link(fetch(f"{origin}{trip}/share", cookie)[1], base_path)


def file_name(place: int, page: str, width: int, scheme: str) -> str:
    return f"{place:02d}-{page}-{width}-{scheme}.png"


def outside_base(report: dict) -> int:
    """How many requests the pages made to their own origin outside the base path."""
    return sum(len(p["outside_base"]) for p in report["pages"])


def summary(report: dict) -> list[str]:
    pages = report["pages"]
    files = sum(len(p["files"]) for p in pages)
    requests = sum(len(p["other_origins"]) for p in pages)
    return [
        f"screens: {len(pages)} pages, {files} files",
        f"overflow at {NARROW[0]}: {report['overflow_pages']} page(s)",
        f"other origins: {requests} request(s) to {len(report['other_origin_hosts'])} host(s)",
        f"outside the base: {outside_base(report)} request(s)",
        checks.tally(report["checks"]),
    ]


def verdict(report: dict, failed: list[str], fail_on_overflow: bool) -> str:
    """The run's last line: the first page that failed, else the first check that is not ok,
    else an overflow when it fails the run, else ok."""
    if failed:
        return f"screens: FAIL {failed[0]}"
    check = checks.failed(report["checks"])
    if check is not None:
        return f"screens: FAIL check {check}"
    if fail_on_overflow and report["overflow_pages"] > 0:
        return "screens: FAIL overflow"
    return "screens: ok"


def failures(report: dict) -> list[str]:
    """`name: detail` of each check that is not ok, in order."""
    return [f"{c['name']}: {c['detail']}" for c in report["checks"] if not c["ok"]]


def judged(
    report: dict,
    escaped: tuple[str | None, list[str]],
    unprotected: list[str],
    guarded: int,
    shared: tuple[str | None, list[str]],
    seen: dict | None,
) -> list[dict]:
    """The eight checks of a run, in checks.NAMES's order: the first four from what each page's
    loads recorded, `escape` from the trip page it was run on and its problems (`escaped`; no
    page: no trip page was planned), `protected` from the problems of the `guarded` pages loaded
    signed out (`unprotected`), `shared` from the shared page and its problems (`shared`; no
    page: no shared page was planned), `offline` from what its context saw (`seen`, as
    `checks.offline_problems` reads it; None: it did not run)."""
    pages = report["pages"]
    trip, problems = escaped
    link, wrong = shared
    return [
        checks.csp(pages, report.get("csp_probe")),
        checks.other_origins(pages),
        checks.outside_the_base(pages),
        checks.labels(pages),
        checks.escape(trip, problems),
        checks.protected(unprotected, guarded),
        checks.shared(link, wrong),
        checks.offline(seen, checks.offline_problems(seen) if seen else []),
    ]


def offline_trips(pages: list[dict], base_path: str) -> tuple[str, str] | None:
    """The trip the offline check opens and one it never opens, from the planned pages: the
    first trip page, and the second (else a trip that does not exist); None with no trip."""
    trips = [p["path"] for p in pages if p["name"].startswith("trip-") and "display" not in p]
    if not trips:
        return None
    return trips[0], trips[1] if len(trips) > 1 else base_path + MISSING_TRIP


def fetch(url: str, cookie: str = "") -> tuple[int, str]:
    """(status, body) of a plain GET, with `cookie` as its Cookie header when there is one; a
    connection failure is status 0."""
    request = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


def fetch_bare(url: str) -> tuple[int, bytes]:
    """(status, body) of a plain GET with no cookie, the body as it came whatever the status; a
    connection failure is status 0."""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError:
        return 0, b""


def plan_pages(
    origin: str, base_path: str, max_trips: int, cookie: str = "", fresh: str = ""
) -> list[dict]:
    """The pages to photograph, in order: {"name", "path", "expect"}, and what a page needs done
    once it is loaded (`paste`, `wrong`) or before (`signed_out`: load it without signing in;
    `display`: save these display choices first, which hold for every page after it).
    The pages are found with `cookie`, a signed-in session's; after the first trip's share page,
    the shared page of a link made through that page's form (`make_link`), loaded signed out;
    with a cookie, the login page comes last, as it shows and after a wrong password. With
    `fresh`, the address of a brand-new install's page, its list comes after them all, as
    `first-run`, at its own `origin`."""
    pages = [
        {"name": "index", "path": base_path + "/", "expect": 200},
        {"name": "index-all-past", "path": base_path + "/?past=all", "expect": 200},
    ]
    trips: list[str] = []
    for p in pages:
        for link in trip_links(fetch(origin + p["path"], cookie)[1], base_path):
            if link not in trips:
                trips.append(link)
    trips = trips[:max_trips]
    for link in trips:
        pages.append({"name": "trip-" + link.rsplit("/", 1)[1], "path": link, "expect": 200})
    if trips:
        pages.append({"name": "sheet-open", "path": trips[0], "expect": 200, "sheet": True})
        pages.append({"name": "share", "path": trips[0] + "/share", "expect": 200})
        link = make_link(origin, base_path, trips[0], cookie)
        if link:
            pages.append({"name": "shared", "path": link, "expect": 200, "signed_out": True})
    edits: dict[str, str] = {}
    for link in trips:
        for kind, path in edit_links(fetch(origin + link, cookie)[1], base_path):
            edits.setdefault(kind, path)
    for kind in EDIT_KINDS:
        if kind in edits:
            pages.append({"name": "edit-" + kind, "path": edits[kind], "expect": 200})
    for name, path in ADD_PAGES:
        pages.append({"name": name, "path": base_path + path, "expect": 200})
    pages.append(
        {
            "name": "upload-status",
            "path": base_path + "/add/paste",
            "expect": 200,
            "paste": PASTE_LINE,
        }
    )
    pages.append({"name": "deleted", "path": base_path + "/deleted", "expect": 200})
    pages.append({"name": "settings", "path": base_path + "/settings", "expect": 200})
    pages.append({"name": "not-found", "path": base_path + MISSING_TRIP, "expect": 404})
    if trips:  # last of this ledger's pages: the choices it saves hold from then on
        pages.append(
            {
                "name": "trip-24h-day-first",
                "path": trips[0],
                "expect": 200,
                "display": dict(DAY_FIRST_24H),
            }
        )
    if cookie:
        login = base_path + "/login"
        pages.append({"name": "login", "path": login, "expect": 200, "signed_out": True})
        pages.append(
            {
                "name": "login-wrong",
                "path": login,
                "expect": 401,
                "signed_out": True,
                "wrong": WRONG_PASSWORD,
            }
        )
    if fresh:
        new = urlparse(fresh)
        pages.append(
            {
                "name": "first-run",
                "path": new.path.rstrip("/") + "/",
                "expect": 200,
                "origin": f"{new.scheme}://{new.netloc}",
            }
        )
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", required=True, help="including the page's base path")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--password",
        default=os.environ.get("SCREENS_PASSWORD", ""),
        help="the page's password (default: SCREENS_PASSWORD); none: a page with no login",
    )
    ap.add_argument(
        "--fresh-url",
        default=os.environ.get("SCREENS_FRESH_URL", ""),
        help="a brand-new install's page under the same base (default: SCREENS_FRESH_URL)",
    )
    ap.add_argument(
        "--expect-covers",
        action="store_true",
        help="the offline trip must hold a cover photo (a ledger given covers)",
    )
    ap.add_argument("--max-trips", type=int, default=8)
    ap.add_argument("--fail-on-overflow", action="store_true")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    u = urlparse(args.base_url)
    origin = f"{u.scheme}://{u.netloc}"
    base_path = u.path.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cookie = sign_in(origin, base_path, args.password) if args.password else ""
    if args.password and not cookie:
        print("screens: FAIL sign-in")
        return 1
    pages = plan_pages(origin, base_path, args.max_trips, cookie, args.fresh_url)
    # the origins of the run: the page's, and a brand-new install's
    origins = [origin, *{p["origin"] for p in pages if p.get("origin")}]
    report: dict = {
        "pages": [],
        "overflow_pages": 0,
        "other_origin_hosts": [],
        "csp_probe": None,
        "checks": [],
        "offline_files": [],
    }
    failed: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_options(*origins))
        login_url = f"{origin}{base_path}/login"

        def blocked(route) -> None:
            """A request to an origin not of the run is refused (the checks' contexts record
            none)."""
            if all(is_other_origin(route.request.url, o) for o in origins):
                route.abort()
                return
            route.continue_()

        def take(
            url: str,
            viewport: tuple[int, int],
            scheme: str,
            shot: Path | None,
            p: dict,
            probing: bool = False,
        ) -> dict:
            """Load the page in a fresh context, signed in through the login form first unless
            the page is `signed_out` (with `paste`, send that text from the page's paste form and
            take the page it lands on; with `wrong`, send that password from the login form;
            with `display`, save those choices from the context before the page loads);
            returns its status, its overflow measure and its controls with no name (when it is
            not photographed, `shot` None), the other hosts and the paths of its own origin
            outside the base it asked for, every path (and query) it asked of its own origin,
            how many script elements it holds, its violations of its content policy (None: no
            listener) and, `probing`, the answer of the policy's probe."""
            ctx = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]}, color_scheme=scheme
            )
            ctx.add_init_script(checks.CSP_LISTENER)
            ctx.add_init_script(checks.NO_COVER_RELOAD)
            hosts: list[str] = []
            outside: list[str] = []
            asked: list[str] = []
            own = p.get("origin") or origin  # the origin of the page loaded

            def gate(route) -> None:
                url = route.request.url
                if is_other_origin(url, own):
                    hosts.append(urlparse(url).netloc.lower())
                    route.abort()
                    return
                if escapes_base(url, own, base_path):
                    outside.append(urlparse(url).path)
                parts = urlparse(url)
                asked.append(parts.path + (f"?{parts.query}" if parts.query else ""))
                route.continue_()

            ctx.route("**/*", gate)
            page = ctx.new_page()
            try:
                if args.password and not p.get("signed_out"):
                    sign_in_form(page, f"{own}{base_path}/login", args.password)
                if p.get("display"):
                    saved = ctx.request.post(
                        f"{own}{base_path}/settings/display",
                        form=p["display"],
                        headers={"Origin": own},
                        max_redirects=0,
                    )
                    if saved.status != 303:
                        raise RuntimeError(f"the display choices were answered {saved.status}")
                resp = page.goto(url, wait_until="load")
                if p.get("paste") is not None:
                    resp = send_paste(page, p["paste"])
                if p.get("wrong") is not None:
                    resp = send_password(page, p["wrong"])
                status = resp.status if resp else None
                if p.get("sheet"):  # the trip's own ⋯ open, so its photo controls are in the shot
                    page.click("details.menu > summary")
                page.wait_for_timeout(SETTLE_MS)
                if shot is not None:
                    page.screenshot(path=str(shot), full_page=True)
                measure = page.evaluate(OVERFLOW_JS) if shot is None else None
                violations = checks.read_violations(page)
                scripts = page.evaluate(checks.SCRIPTS)
                probed = checks.probe(page) if probing else None
                unnamed = page.evaluate(checks.UNNAMED) if shot is None else None
                photo = (
                    page.evaluate(checks.PHOTO_CONTROLS)
                    if shot is None and p["expect"] == 200 and TRIP_PAGE.search(p["path"])
                    else None
                )
            finally:
                ctx.close()
            return {
                "status": status,
                "measure": measure,
                "hosts": hosts,
                "outside": outside,
                "asked": asked,
                "scripts": scripts,
                "violations": violations,
                "probe": probed,
                "unnamed": unnamed,
                "photo": photo,
            }

        def escape_on(p: dict) -> list[str]:
            """The escape check's problems on the trip page `p`, signed in."""
            ctx = browser.new_context(viewport={"width": 390, "height": 844})
            ctx.add_init_script(checks.NO_COVER_RELOAD)
            ctx.route("**/*", blocked)
            page = ctx.new_page()
            try:
                if args.password:
                    sign_in_form(page, login_url, args.password)
                page.goto(origin + p["path"], wait_until="load")
                page.wait_for_timeout(SETTLE_MS)
                return checks.escape_problems(page)
            except Exception as e:  # a page that cannot be driven fails the check, not the run
                return [f"{type(e).__name__}: {e}"]
            finally:
                ctx.close()

        def landings(ctx, guarded: list[dict]) -> dict[str, str]:
            """The path each page of `guarded` ends on, loaded in `ctx`."""
            page = ctx.new_page()
            landed: dict[str, str] = {}
            for p in guarded:
                try:
                    page.goto(page_url(origin, p), wait_until="load")
                    landed[p["name"]] = urlparse(page.url).path
                except Exception as e:  # noqa: BLE001 - a page that does not load is said
                    landed[p["name"]] = f"an error ({type(e).__name__})"
            return landed

        def protection(guarded: list[dict]) -> list[str]:
            """The protected check's problems: each page of `guarded` loaded with no cookie,
            then again after a sign-out with the ended session's cookie sent back."""
            if not args.password:
                return ["no password was given, so the sign-in was not checked"]
            login_path = base_path + "/login"
            ctx = browser.new_context()
            ctx.route("**/*", blocked)
            try:
                problems = checks.landing_problems(
                    landings(ctx, guarded), login_path, "with no cookie"
                )
            finally:
                ctx.close()
            ctx = browser.new_context()
            ctx.route("**/*", blocked)
            try:
                sign_in_form(ctx.new_page(), login_url, args.password)
                ended = [c for c in ctx.cookies() if c["name"].endswith("tripbox_session")]
                if not ended:
                    problems.append("signing in set no session cookie")
                out = ctx.request.post(
                    f"{origin}{base_path}/logout", headers={"Origin": origin}, max_redirects=0
                )
                if out.status != 303:
                    problems.append(f"the sign-out was answered {out.status}")
                ctx.add_cookies(ended)  # the ended session's cookie, sent back
                problems += checks.landing_problems(
                    landings(ctx, guarded), login_path, "after sign-out"
                )
            except Exception as e:  # a sign-in or sign-out that cannot be driven fails the check
                problems.append(f"{type(e).__name__}: {e}")
            finally:
                ctx.close()
            return problems

        def offline_on(chosen: tuple[str, str] | None, link: str) -> tuple[dict, dict | None]:
            """What the offline check sees (`checks.offline_problems` reads it), in a context of
            its own, signed in, whose network a route cuts by aborting every request: the list
            sign-in lands on, which registers the worker before it controls the page, reloaded
            with the network cut once a store holds it, whose first act is to prove that the cut
            reaches a request the service worker itself makes (else `untested`); the trip of
            `chosen` opened online, then reloaded with the network cut; the trip never opened
            and a post from the copy; the pages never kept visited online; a sign-out from
            Settings and the trip again. The stores once the shared page `link` was opened
            signed in come back too (None: not read)."""
            seen: dict = {
                "base_path": base_path,
                "want_scope": f"{origin}{base_path}/",
                "untested": None,
                "want_cover": args.expect_covers,
            }
            if chosen is None:
                seen["untested"] = "no trip page was planned"
                return seen, None
            if not args.password:
                seen["untested"] = "no password was given, so sign-out cannot be checked"
                return seen, None
            trip, never = chosen
            cut = {"on": False, "worker": 0}

            def gate(route) -> None:
                request = route.request
                if cut["on"]:
                    if getattr(request, "service_worker", None) is not None:
                        cut["worker"] += 1
                    route.abort(checks.CUT)
                    return
                if all(is_other_origin(request.url, o) for o in origins):
                    route.abort()
                    return
                route.continue_()

            ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="light")
            ctx.route("**/*", gate)
            page = ctx.new_page()
            stored = None

            def go(path: str):
                return page.goto(origin + path, wait_until="load")

            def answered(response) -> dict:
                shown = page.evaluate(checks.SHOWN_NOW)
                return {**shown, "status": response.status if response else None}

            try:
                # the list sign-in lands on registers the worker, which did not answer its load
                sign_in_form(page, login_url, args.password)
                seen["scope"] = page.evaluate(checks.WORKER_READY)["scope"]
                cdp = ctx.new_cdp_session(page)  # (6), on the list
                seen["manifest"] = cdp.send("Page.getAppManifest")
                cdp.detach()
                if seen["scope"] is None:
                    return seen, None
                seen["list_title"] = page.evaluate(checks.SHOWN_NOW)["title"]  # (7)
                listed = origin + base_path + "/"
                seen["list_kept"] = page.evaluate(checks.KEPT_SOON, listed)
                cut["on"] = True  # its first act: that list reloaded, never loaded again online
                again = page.reload(wait_until="load")
                if not cut["worker"]:
                    seen["untested"] = (
                        "cutting the network reached no request the service worker made"
                    )
                    return seen, None
                seen["list_copy"] = answered(again)
                cut["on"] = False
                go(base_path + "/")  # the worker answers from now on: the list and the trip
                go(trip)
                seen["title"] = page.evaluate(checks.SHOWN_NOW)["title"]
                seen["cover_online"] = page.evaluate(checks.COVER_IMGS)
                seen["kept"] = [base_path + "/", trip]
                for path in ("/login", "/settings", "/add", "/add/paste"):  # (4), online
                    go(base_path + path)
                go(trip + "/share")
                if link:
                    go(link)
                seen["stored"] = stored = page.evaluate(checks.STORED)
                go(trip)
                cut["on"] = True  # the trip reloaded
                seen["copy"] = answered(page.reload(wait_until="load"))  # (2)
                seen["cover_copy"] = page.evaluate(checks.COVER_IMGS)  # (8)
                for width, height in SHOT_VIEWPORTS:
                    for scheme in SCHEMES:
                        page.set_viewport_size({"width": width, "height": height})
                        page.emulate_media(color_scheme=scheme)
                        page.wait_for_timeout(SETTLE_MS)
                        name = file_name(len(pages) + 1, "offline-copy", width, scheme)
                        page.screenshot(path=str(out / name), full_page=True)
                        report["offline_files"].append(name)
                seen["no_copy"] = answered(go(never))  # (3)
                go(trip)
                with page.expect_navigation(wait_until="load") as posted:
                    page.evaluate(checks.POST_BY_SCRIPT)
                seen["refused"] = answered(posted.value)
                cut["on"] = False  # (5): signed out from Settings, as the owner does
                go(base_path + "/settings")
                login_page = re.compile(re.escape(origin + base_path + "/login") + r"(\?.*)?$")
                with page.expect_navigation(url=login_page, wait_until="load"):
                    page.click(f'form[action="{base_path}/logout"] button[type="submit"]')
                page.wait_for_timeout(SETTLE_MS)
                seen["after_stored"] = page.evaluate(checks.STORED)
                cut["on"] = True
                try:
                    seen["after"] = {**answered(go(trip)), "error": None}
                except Exception as e:  # noqa: BLE001 - no worker left to answer it
                    seen["after"] = {"saved": None, "error": type(e).__name__}
            except Exception as e:  # a step that cannot be driven fails the check, not the run
                seen["error"] = f"{type(e).__name__}: {e}"
            finally:
                ctx.close()
            return seen, stored

        for place, p in enumerate(pages, start=1):
            entry = {
                "name": p["name"],
                "path": p["path"],
                "status": None,
                "files": [],
                "overflow_px": 0,
                "overflow_elements": [],
                "other_origins": [],
                "outside_base": [],
                "csp": [],
                "unnamed": None,
                "photo": None,
                "asked": [],
                "scripts": [],
            }
            ok = True
            try:
                for width, height in SHOT_VIEWPORTS:
                    for scheme in SCHEMES:
                        name = file_name(place, p["name"], width, scheme)
                        seen = take(page_url(origin, p), (width, height), scheme, out / name, p)
                        entry["files"].append(name)
                        entry["other_origins"] += seen["hosts"]
                        entry["outside_base"] += seen["outside"]
                        entry["csp"].append(seen["violations"])
                        entry["asked"] += seen["asked"]
                        entry["scripts"].append(seen["scripts"])
                        entry["status"] = seen["status"]
                        ok = ok and seen["status"] == p["expect"]
                seen = take(page_url(origin, p), NARROW, "light", None, p, probing=place == 1)
                if place == 1:  # the policy proves itself once a run, on the first page
                    report["csp_probe"] = seen["probe"]
                entry["other_origins"] += seen["hosts"]
                entry["outside_base"] += seen["outside"]
                entry["csp"].append(seen["violations"])
                entry["asked"] += seen["asked"]
                entry["scripts"].append(seen["scripts"])
                entry["unnamed"] = seen["unnamed"]
                entry["photo"] = seen["photo"]
                entry["overflow_px"] = max(seen["measure"]["px"], 0)
                if entry["overflow_px"] > 0:
                    entry["overflow_elements"] = seen["measure"]["elements"]
                    report["overflow_pages"] += 1
            except Exception as e:  # a page that did not load is a failure, not a crash
                print(f"screens: {p['name']}: {type(e).__name__}: {e}", file=sys.stderr)
                ok = False
            if not ok:
                failed.append(p["name"])
            report["pages"].append(entry)

        trip = next((p for p in pages if p["name"].startswith("trip-")), None)
        escaped = (trip["name"], escape_on(trip)) if trip else (None, [])
        guarded = [p for p in pages if not p.get("signed_out")]
        link = next((e for e in report["pages"] if e["name"] == "shared"), None)
        wrong = fetch_bare(f"{origin}{base_path}/s/{WRONG_LINK}")
        seen, stored = offline_on(offline_trips(pages, base_path), link["path"] if link else "")
        shared = (
            (link["name"], checks.shared_problems(link, base_path, wrong, stored))
            if link
            else (None, [])
        )
        report["checks"] = judged(
            report, escaped, protection(guarded), len(guarded), shared, seen
        )
        browser.close()

    report["other_origin_hosts"] = sorted({h for e in report["pages"] for h in e["other_origins"]})
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    for line in [*summary(report), *failures(report)]:
        print(line)
    last = verdict(report, failed, args.fail_on_overflow)
    print(last)
    return 0 if last == "screens: ok" else 1


if __name__ == "__main__":
    sys.exit(main())
