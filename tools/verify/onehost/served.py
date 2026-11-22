#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Says whether an AirTrail serves the Trips button: it fetches AirTrail's page, then every
script under /_app/ that the page names and those scripts name in turn, and looks for one file
that holds the trips address as a quoted string, `Trips` and `trips-button`, as the patched
dock's item does. It signs in to nothing: AirTrail's sign-in page loads the same scripts.

Usage: python3 served.py --url URL --trips URL [--host NAME]

  --url    the address AirTrail is opened at, such as https://flights.example.com
  --trips  the address the button was built with, such as /tripbox or https://trips.example.com
  --host   send every request to --url's address but name this host in it, as a browser opening
           https://NAME would through a proxy at --url (for a check from inside a compose project)

It follows at most 5 redirects on the same host and fetches at most 800 files and 64 MiB in all.
The last line is `dock link: found` (exit 0), `dock link: not found` (exit 1) or
`dock link: FAIL <what>` (exit 2): the page did not answer, or a limit was reached first.
Standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import re
import sys
from urllib.parse import urljoin, urlsplit

PREFIX = "dock link:"
MAX_FILES = 800
MAX_BYTES = 64 * 1024 * 1024
REDIRECTS = 5
SECONDS = 20
# a quoted string that names a script: a path, absolute or relative, ending .js or .mjs
SCRIPT = re.compile(r"""["'`]([^"'`\s<>()]{1,512}?\.m?js)["'`]""")
ASSETS = "/_app/"


def size(n: int) -> str:
    return f"{n // (1024 * 1024)} MiB" if n % (1024 * 1024) == 0 else f"{n} bytes"


class Fail(Exception):
    """What stopped the look."""


class Site:
    """One origin, fetched over plain http or https, with at most `max_files` files and
    `max_bytes` bytes in all."""

    def __init__(self, url: str, host: str | None, max_files: int, max_bytes: int):
        parts = urlsplit(url)
        self.scheme = parts.scheme
        self.connect = parts.netloc
        self.netloc = host or parts.netloc
        self.files = 0
        self.bytes = 0
        self.max_files = max_files
        self.max_bytes = max_bytes

    def get(self, path: str) -> tuple[int, str, bytes]:
        """(status, Location, body) of one GET."""
        if self.files >= self.max_files:
            raise Fail(f"more than {self.max_files} files")
        self.files += 1
        kind = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = kind(self.connect, timeout=SECONDS)
        try:
            conn.request("GET", path, headers={"Host": self.netloc, "Accept": "*/*"})
            raw = conn.getresponse()
            left = self.max_bytes - self.bytes
            body = raw.read(left + 1)
            status, where = raw.status, raw.getheader("Location") or ""
        except (OSError, http.client.HTTPException) as exc:
            raise Fail(f"no answer for {path} ({type(exc).__name__})") from None
        finally:
            conn.close()
        self.bytes += len(body)
        if self.bytes > self.max_bytes:
            raise Fail(f"more than {size(self.max_bytes)}")
        return status, where, body

    def path_of(self, base: str, ref: str) -> str | None:
        """The path `ref`, named in the file at `base`, stands for, when it is on this site."""
        if ref.startswith("_app/"):  # written from the site's root
            ref = "/" + ref
        elif not ref.startswith(("/", "./", "../", "http://", "https://")):
            return None
        target = urlsplit(urljoin(f"{self.scheme}://{self.netloc}{base}", ref))
        if target.netloc != self.netloc:
            return None
        return target.path


def holds_button(text: str, trips: str) -> bool:
    quoted = any(f"{q}{trips}{q}" in text for q in "\"'`")
    return quoted and "Trips" in text and "trips-button" in text


def look(site: Site, start: str, trips: str) -> bool:
    path = start
    for _ in range(REDIRECTS + 1):
        status, where, body = site.get(path)
        if status not in (301, 302, 303, 307, 308):
            break
        target = urlsplit(urljoin(f"{site.scheme}://{site.netloc}{path}", where))
        if target.netloc != site.netloc:
            raise Fail("the page leads to another host")
        path = target.path + (f"?{target.query}" if target.query else "")
    else:
        raise Fail(f"more than {REDIRECTS} redirects")
    if status != 200:
        raise Fail(f"the page answered {status}")
    queue, seen = [(path, body)], {path}
    while queue:
        at, data = queue.pop(0)
        text = data.decode("utf-8", "replace")
        if holds_button(text, trips):
            return True
        for ref in SCRIPT.findall(text):
            found = site.path_of(at, ref)
            if found is None or not found.startswith(ASSETS) or found in seen:
                continue
            seen.add(found)
            status, _where, script = site.get(found)
            if status == 200:
                queue.append((found, script))
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Does an AirTrail serve the Trips button?")
    parser.add_argument("--url", required=True)
    parser.add_argument("--trips", required=True)
    parser.add_argument("--host")
    parser.add_argument("--max-files", type=int, default=MAX_FILES, help=argparse.SUPPRESS)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    url = urlsplit(args.url)
    if url.scheme not in ("http", "https") or not url.netloc:
        parser.error("--url must be an http:// or https:// address")
    if args.host is not None and (not args.host or any(c in args.host for c in "/@ ")):
        parser.error("--host must be a hostname")
    if not args.trips or any(c in args.trips for c in "\"'` "):
        parser.error("--trips must be an address or a path")
    if not 0 < args.max_files <= MAX_FILES or not 0 < args.max_bytes <= MAX_BYTES:
        parser.error("the limits may only be lowered")
    site = Site(args.url, args.host, args.max_files, args.max_bytes)
    try:
        found = look(site, url.path or "/", args.trips)
    except Fail as fail:
        print(f"{PREFIX} FAIL {fail}")
        return 2
    print(f"{PREFIX} {'found' if found else 'not found'}")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
