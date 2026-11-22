#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Walks the trips page through a reverse proxy in front of it, as a browser opening the page's
address would: every request goes to the proxy, its `Host` the base URL's host, and every check
is made on what the proxy hands back.

Usage: PROXY_PASSWORD=... python3 probe.py --proxy http://proxy:80 --base-url URL --sample FILE
                                           [--timeout 90] [--wait 30]

  --proxy     where the proxy listens, plain http (its own TLS is not part of the walk)
  --base-url  the page's TRIPBOX_BASE_URL, the address a browser opens
  --sample    a confirmation email to paste
  --timeout   seconds the sample may take to be filed (default 90)
  --wait      seconds the page may take to answer through the proxy at all (default 30)

It waits, quietly, until `<base>/healthz` answers through the proxy, then walks the fresh
install's check (`quickstart_check.py`, beside this file or in the directory above it) with
steps of its own between the trip and signing out, then more once signed out. Each step is
printed as `proxy probe: <step> ok` once it passes:
  health, sign-in, first-run, paste, filed, trip   as quickstart_check.py walks them
  share       the trip's share page makes a link through its form; the link it lists is under
              the base URL and opens with no cookie, under the shared page's policy
  feed        Settings names the calendar feed under the base URL; it answers with no cookie,
              a calendar
  upload      an email of 2 MiB posted to <base>/add/upload is a 303 to its status page, which
              answers: the proxy's body limit let it through
  offline     the list names <path> as its base and <path>/sw.js as its service worker; that
              script answers `no-cache`, and the manifest's scope and start are <path>/
  sign-out    as quickstart_check.py walks it
  slash       a base with a path only: the bare base is a 307 to <path>/, a path with no host
  login-page  <base>/login is a 200 with the page's content policy; its stylesheet is under
              <path>/static/
  static      that stylesheet is a 200
  paths       every redirect the walk was given is a path under <path>/, no host, and every
              cookie's Path is the base's (<path>, or / at the root)
  outside     a base with a path only: / and <path>x carry neither the page's content policy
              nor its robots header
The last line is `proxy probe: ok` (exit 0) or `proxy probe: FAIL <step>` (exit 1); a usage
error exits 2. Nothing of the password, the sample or a page's body is printed: a failure says
on stderr what was answered. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
# beside this file in a harness directory, one directory up in the repository
for where in (HERE, HERE.parent):
    if (where / "quickstart_check.py").is_file():
        sys.path.insert(0, str(where))
        break

import quickstart_check as qc  # noqa: E402

PREFIX = "proxy probe:"
UPLOAD_BYTES = 2 * 1024 * 1024
PAGE_HEADERS = ("Content-Security-Policy", "X-Robots-Tag")


class Seen(qc.Browser):
    """The browser, keeping every response it was given."""

    def __init__(self, base: str, connect: str):
        super().__init__(base, connect)
        self.seen: list[qc.Response] = []

    def request(self, *args, **kwargs) -> qc.Response:
        got = super().request(*args, **kwargs)
        self.seen.append(got)
        return got


class Tags(HTMLParser):
    """The start tags of a page, each with its attributes and the form it is in."""

    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str], str | None]] = []
        self.form: str | None = None

    def handle_starttag(self, tag, attrs):
        found = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.form = found.get("action", "")
        self.tags.append((tag, found, self.form))

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None


def tags(body: str) -> list[tuple[str, dict[str, str], str | None]]:
    parser = Tags()
    parser.feed(body)
    return parser.tags


def large_email() -> bytes:
    """An email of UPLOAD_BYTES, made up here: a header block and a filler body."""
    head = (
        b"From: Sample family <family@example.com>\r\n"
        b"To: trips@example.com\r\n"
        b"Subject: A large confirmation\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    )
    line = b"This line pads the message past a proxy's default body limit.\r\n"
    body = line * ((UPLOAD_BYTES - len(head)) // len(line) + 1)
    return (head + body)[:UPLOAD_BYTES]


def multipart(field: str, name: str, data: bytes) -> tuple[str, bytes]:
    boundary = "probe-" + secrets.token_hex(12)
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
        "Content-Type: message/rfc822\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", head + data + tail


def path_of(url: str) -> str:
    return urlsplit(url).path


def signed_in_steps(b: Seen, trip: str, step) -> None:
    path = b.base_path

    with step("share"):
        r = b.request("GET", trip + "/share")
        qc.expect(r, 200, "the share page")
        action = path_of(trip) + "/links"
        ticked = {
            a["name"]: "on"
            for tag, a, form in tags(r.body)
            if tag == "input" and form == action and a.get("type") == "checkbox"
            and "checked" in a and a.get("name")
        }
        if not any(tag == "form" and a.get("action") == action for tag, a, _ in tags(r.body)):
            raise qc.Fail("the share page has no form that makes a link")
        r = b.request("POST", b.origin + action, ticked)
        qc.expect(r, 303, "making a link")
        r = b.request("GET", trip + "/share")
        qc.expect(r, 200, "the share page")
        links = [a["data-copy"] for _, a, _ in tags(r.body) if "/s/" in a.get("data-copy", "")]
        if len(links) != 1 or not links[0].startswith(b.url("/s/")):
            raise qc.Fail("the share page lists no one link under the base URL")
        r = b.request("GET", links[0], jar={})
        qc.expect(r, 200, "the shared link with no cookie")
        if "script-src 'none'" not in r.header("Content-Security-Policy"):
            raise qc.Fail("the shared link is not under the shared page's policy")

    with step("feed"):
        r = b.request("GET", b.url("/settings"))
        qc.expect(r, 200, "Settings")
        feeds = [a["data-copy"] for _, a, _ in tags(r.body) if "/feed/" in a.get("data-copy", "")]
        if len(feeds) != 1 or not feeds[0].startswith(b.url("/feed/")):
            raise qc.Fail("Settings names no one calendar feed under the base URL")
        r = b.request("GET", feeds[0], jar={})
        qc.expect(r, 200, "the calendar feed with no cookie")
        if not r.header("Content-Type").startswith("text/calendar"):
            raise qc.Fail("the calendar feed is not a calendar")
        if not r.body.startswith("BEGIN:VCALENDAR"):
            raise qc.Fail("the calendar feed does not begin as a calendar")

    with step("upload"):
        r = b.request("POST", b.url("/add/upload"),
                      raw=multipart("file", "large.eml", large_email()))
        if r.status == 413:
            raise qc.Fail("an upload of 2 MiB answered 413: the proxy's body limit refused it")
        qc.expect(r, 303, "an upload of 2 MiB")
        status = b.location(r, b.url("/add/upload"))
        if not status.startswith(b.url("/uploads/")):
            raise qc.Fail("the upload did not lead to its status page")
        qc.expect(b.request("GET", status), 200, "the upload's status page")

    with step("offline"):
        r = b.request("GET", b.url("/"))
        qc.expect(r, 200, "the list")
        html = next((a for tag, a, _ in tags(r.body) if tag == "html"), {})
        if html.get("data-base") != path or html.get("data-sw") != path + "/sw.js":
            raise qc.Fail("the list does not name the base path and its service worker")
        r = b.request("GET", b.url("/sw.js"))
        qc.expect(r, 200, "the service worker's script")
        if "javascript" not in r.header("Content-Type"):
            raise qc.Fail("the service worker's script is not a script")
        if "no-cache" not in r.header("Cache-Control"):
            raise qc.Fail("the service worker's script came back without no-cache")
        r = b.request("GET", b.url("/manifest.webmanifest"))
        qc.expect(r, 200, "the manifest")
        try:
            manifest = json.loads(r.body)
        except ValueError:
            raise qc.Fail("the manifest is not JSON") from None
        if not isinstance(manifest, dict) or (
            manifest.get("scope"), manifest.get("start_url")) != (path + "/", path + "/"):
            raise qc.Fail("the manifest's scope or start is not the base path")


def signed_out_steps(b: Seen, step) -> None:
    path = b.base_path
    cookie_path = path or "/"

    if path:
        with step("slash"):
            r = b.request("GET", b.base, jar={})
            qc.expect(r, 307, "the bare base")
            if r.header("Location") != path + "/":
                raise qc.Fail("the bare base did not lead to its path and /")

    with step("login-page"):
        r = b.request("GET", b.url("/login"), jar={})
        qc.expect(r, 200, "the login page")
        if "default-src 'self'" not in r.header("Content-Security-Policy"):
            raise qc.Fail("the login page came back without the page's content policy")
        sheets = [a.get("href", "") for tag, a, _ in tags(r.body)
                  if tag == "link" and a.get("rel") == "stylesheet"]
        if len(sheets) != 1 or not sheets[0].startswith(path + "/static/"):
            raise qc.Fail("the login page's stylesheet is not under the base's static files")

    with step("static"):
        qc.expect(b.request("GET", b.origin + sheets[0], jar={}), 200, "the stylesheet")

    with step("paths"):
        for got in b.seen:
            where = got.header("Location")
            if where:
                parts = urlsplit(where)
                if parts.scheme or parts.netloc or not parts.path.startswith(path + "/"):
                    raise qc.Fail(f"a {got.status} led to a place outside the base path")
            for key, line in got.headers:
                if key.lower() != "set-cookie":
                    continue
                attrs = [part.strip() for part in line.split(";")[1:]]
                given = [a.split("=", 1)[1] for a in attrs if a.lower().startswith("path=")]
                if given != [cookie_path]:
                    raise qc.Fail("a cookie's Path is not the base's")

    if path:
        with step("outside"):
            for there in (b.origin + "/", b.origin + path + "x"):
                r = b.request("GET", there, jar={})
                if any(r.header(name) for name in PAGE_HEADERS):
                    raise qc.Fail(f"{path_of(there)} came back with a header of the page's")


def ready(b: Seen, seconds: float) -> None:
    """Waits until the page answers its health check through the proxy, or `seconds` pass."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if b.request("GET", b.url("/healthz"), jar={}).status == 200:
                break
        except qc.Fail:
            pass
        time.sleep(0.5)
    b.seen.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk the page through a reverse proxy.")
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--wait", type=float, default=30.0)
    args = parser.parse_args(argv)
    proxy = urlsplit(args.proxy)
    if proxy.scheme != "http" or not proxy.netloc or proxy.path not in ("", "/"):
        parser.error("--proxy must be http://host[:port]")
    base = args.base_url.rstrip("/")
    if urlsplit(base).scheme not in ("http", "https") or not urlsplit(base).netloc:
        parser.error("--base-url must be an http or https address")
    password = os.environ.get("PROXY_PASSWORD", "")
    try:
        with open(args.sample, "rb") as fh:
            sample = fh.read(qc.SAMPLE_CAP + 1)
    except OSError as exc:
        parser.error(f"cannot read the sample: {exc.strerror or exc}")
    if not password or len(sample) > qc.SAMPLE_CAP:
        parser.error("PROXY_PASSWORD is empty, or the sample is over 2 MiB")

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

    b = Seen(base, proxy.netloc)
    try:
        ready(b, args.wait)
        qc.walk(b, password, sample.decode("utf-8", "replace"), args.timeout, Step,
                then=signed_in_steps)
        signed_out_steps(b, Step)
    except qc.Fail as fail:
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
