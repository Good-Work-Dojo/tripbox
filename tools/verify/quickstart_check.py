#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Walks a fresh install's page as its first user does: health, sign-in, the first run's list,
a pasted confirmation, its filing, the trip it made, and signing out.

Usage: python3 tools/verify/quickstart_check.py --base-url URL --password-file FILE
                                               --sample FILE [--timeout 90]

  --base-url       the address the page is opened at (its TRIPBOX_BASE_URL)
  --password-file  a file whose first line is the page's password
  --sample         a confirmation email to paste
  --timeout        seconds the sample may take to be filed (default 90)

Steps, in order, each printed as `quickstart check: <step> ok` once it passes:
  health     <base>/healthz says ok
  sign-in    a wrong password is a 401; the right one a 303 that sets a session cookie
  first-run  the list says `No trips yet`
  paste      the sample, posted to <base>/add/paste, is a 303 to <base>/uploads/...
  filed      that page says `Filed` and links a page under <base>/trips/, within the timeout
  trip       that link is a 200, and the list no longer says `No trips yet`
  sign-out   a post to <base>/logout is a 303, and the cookie the browser held then is sent to
             the login page
Every POST carries an `Origin` header, the base URL's origin. `walk` takes a browser that may
open its connections to another address (a proxy in front of the page, `Browser(connect=...)`)
and steps of a caller's own to run, signed in, between `trip` and `sign-out` (`then`). The last
line is `quickstart check: ok` (exit 0) or `quickstart check: FAIL <step>` (exit 1); a usage error
exits 2. Nothing of the sample, the password or a page's body is printed: a failure says on stderr
what was answered (a status, a place) and nothing more. Standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

PREFIX = "quickstart check:"
REQUEST_SECONDS = 10
BODY_CAP = 4 * 1024 * 1024
SAMPLE_CAP = 2 * 1024 * 1024
FIRST_RUN = "No trips yet"
HTML = "text/html,application/xhtml+xml"


class Fail(Exception):
    """A step that did not pass; the message says what was answered."""


class Response:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: str):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name: str) -> str:
        return next((v for k, v in self.headers if k.lower() == name.lower()), "")

    def cookies(self) -> list[tuple[str, str, bool]]:
        """Each Set-Cookie as (name, value, dropped)."""
        found = []
        for key, line in self.headers:
            if key.lower() != "set-cookie":
                continue
            pair, *attrs = [part.strip() for part in line.split(";")]
            name, _, value = pair.partition("=")
            dropped = value == "" or any(a.lower().replace(" ", "") == "max-age=0" for a in attrs)
            found.append((name, value, dropped))
        return found


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


class Browser:
    """A client with a cookie jar that follows no redirect. With `connect` (host:port) every
    request is sent there over plain HTTP, its `Host` the base URL's: what a proxy in front of
    the page receives from a browser, the proxy's own TLS aside."""

    def __init__(self, base: str, connect: str | None = None):
        parts = urlsplit(base)
        self.scheme, self.netloc = parts.scheme, parts.netloc
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.base = base
        self.base_path = parts.path
        self.connect = connect
        self.jar: dict[str, str] = {}

    def url(self, path: str) -> str:
        return self.base + path

    def request(
        self, method: str, url: str, form: dict[str, str] | None = None,
        jar: dict[str, str] | None = None, raw: tuple[str, bytes] | None = None,
    ) -> Response:  # fmt: skip
        """`form` is posted url-encoded; `raw` is a POST's (Content-Type, body) instead."""
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != self.origin:
            raise Fail("a link leads off the page's origin")
        headers = {"Accept": HTML}
        if self.connect:
            conn = http.client.HTTPConnection(self.connect, timeout=REQUEST_SECONDS)
            headers["Host"] = parts.netloc
        else:
            secure = parts.scheme == "https"
            kind = http.client.HTTPSConnection if secure else http.client.HTTPConnection
            conn = kind(parts.netloc, timeout=REQUEST_SECONDS)
        cookies = self.jar if jar is None else jar
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        body = None
        if method == "POST":
            headers["Origin"] = self.origin
            if raw is not None:
                headers["Content-Type"], body = raw
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = urlencode(form or {}).encode()
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request(method, target, body=body, headers=headers)
            raw = conn.getresponse()
            data = raw.read(BODY_CAP)
            got = Response(raw.status, raw.getheaders(), data.decode("utf-8", "replace"))
        except (OSError, http.client.HTTPException) as exc:
            raise Fail(f"{method} {parts.path or '/'}: no answer ({type(exc).__name__})") from None
        finally:
            conn.close()
        if jar is None:
            for name, value, dropped in got.cookies():
                if dropped:
                    self.jar.pop(name, None)
                else:
                    self.jar[name] = value
        return got

    def location(self, r: Response, url: str) -> str:
        return urljoin(url, r.header("Location"))


def expect(r: Response, status: int, what: str) -> None:
    if r.status != status:
        raise Fail(f"{what} answered {r.status}, not {status}")


def walk(b: Browser, password: str, sample: str, timeout: float, step, then=None) -> None:
    """The steps in order, each inside `step(name)`; `then(b, trip, step)`, when given, runs
    signed in after `trip` (`trip` the trip's address) and before `sign-out`."""
    with step("health"):
        r = b.request("GET", b.url("/healthz"))
        expect(r, 200, "the health check")
        if r.body.strip() != "ok":
            raise Fail("the health check did not say ok")

    with step("sign-in"):
        r = b.request("POST", b.url("/login"), {"password": password + "-not-it"})
        expect(r, 401, "a wrong password")
        r = b.request("POST", b.url("/login"), {"password": password})
        expect(r, 303, "the right password")
        if not any(not dropped for _, _, dropped in r.cookies()):
            raise Fail("signing in set no cookie")

    with step("first-run"):
        r = b.request("GET", b.url("/"))
        expect(r, 200, "the list")
        if FIRST_RUN not in r.body:
            raise Fail(f"the list does not say {FIRST_RUN!r}")

    with step("paste"):
        r = b.request("POST", b.url("/add/paste"), {"text": sample})
        expect(r, 303, "the paste")
        upload = b.location(r, b.url("/add/paste"))
        if not upload.startswith(b.url("/uploads/")):
            raise Fail("the paste did not lead to an upload's page")

    with step("filed"):
        deadline = time.monotonic() + timeout
        trip = None
        while trip is None:
            r = b.request("GET", upload)
            expect(r, 200, "the upload's page")
            if "Filed" in r.body:
                links = Links()
                links.feed(r.body)
                under = [urljoin(upload, h) for h in links.hrefs]
                trip = next((u for u in under if u.startswith(b.url("/trips/"))), None)
                if trip is None:
                    raise Fail("the upload was filed but links no trip")
                break
            if time.monotonic() >= deadline:
                raise Fail(f"the upload was not filed within {timeout:g} s")
            time.sleep(1)

    with step("trip"):
        r = b.request("GET", trip.split("#", 1)[0])
        expect(r, 200, "the trip")
        r = b.request("GET", b.url("/"))
        expect(r, 200, "the list")
        if FIRST_RUN in r.body:
            raise Fail(f"the list still says {FIRST_RUN!r}")

    if then is not None:
        then(b, trip.split("#", 1)[0], step)

    with step("sign-out"):
        held = dict(b.jar)
        r = b.request("POST", b.url("/logout"))
        expect(r, 303, "signing out")
        r = b.request("GET", b.url("/"), jar=held)
        expect(r, 303, "the list with the old cookie")
        where = urlsplit(b.location(r, b.url("/"))).path
        if where != b.base_path + "/login":
            raise Fail("the old cookie was not sent to the login page")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk a fresh install's page.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")
    if urlsplit(base).scheme not in ("http", "https") or not urlsplit(base).netloc:
        parser.error("--base-url must be an http or https address")
    try:
        with open(args.password_file, encoding="utf-8") as fh:
            password = fh.readline().rstrip("\r\n")
        with open(args.sample, "rb") as fh:
            sample = fh.read(SAMPLE_CAP + 1)
    except OSError as exc:
        parser.error(f"cannot read a file: {exc.strerror or exc}")
    if not password or len(sample) > SAMPLE_CAP:
        parser.error("the password file is empty, or the sample is over 2 MiB")

    current = {"step": None}

    class Step:
        def __init__(self, name: str):
            self.name = name

        def __enter__(self):
            current["step"] = self.name

        def __exit__(self, kind, exc, tb):
            if kind is None:
                print(f"{PREFIX} {self.name} ok", flush=True)
            return False

    try:
        walk(Browser(base), password, sample.decode("utf-8", "replace"), args.timeout, Step)
    except Fail as fail:
        print(f"{PREFIX} {current['step']}: {fail}", file=sys.stderr)
        print(f"{PREFIX} FAIL {current['step']}")
        return 1
    except Exception as exc:  # noqa: BLE001 - any surprise names its step, never its text
        print(f"{PREFIX} {current['step']}: {type(exc).__name__}", file=sys.stderr)
        print(f"{PREFIX} FAIL {current['step']}")
        return 1
    print(f"{PREFIX} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
