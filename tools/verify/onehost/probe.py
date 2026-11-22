#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Walks AirTrail and tripbox at one hostname through the reverse proxy in front of both, as a
browser opening that hostname would: every request goes to the proxy, its `Host` the hostname.

Usage: ONEHOST_PASSWORD=... ONEHOST_AIRTRAIL_KEY=... python3 probe.py --proxy http://proxy:80
           --host flights.example.com --path /tripbox --sample FILE [--timeout 90] [--wait 120]

  --proxy    where the proxy listens, plain http
  --host     the hostname both are served at
  --path     tripbox's path under it (its TRIPBOX_BASE_URL is http://<host><path>)
  --sample   a confirmation email whose flights are the owner's, to paste
  --timeout  seconds the pasted email may take to reach AirTrail (default 90)
  --wait     seconds the hostname may take to answer at all (default 120)

ONEHOST_PASSWORD is tripbox's password; ONEHOST_AIRTRAIL_KEY an API key of AirTrail's that
reads the owner's flights. Steps, each printed as `onehost probe: <step> ok` once it passes:
  airtrail          / ends, through any redirect, at a page of AirTrail's (it loads /_app/);
                    /api/v1 names the instance AirTrail
  tripbox           <path>/healthz says ok; <path> is a 307 to <path>/; signing in is a 303 to
                    a place under <path>/, and every cookie it sets has the Path <path>
  neighbours        <path>x is AirTrail's answer and carries no header of tripbox's (the content
                    policy its health check came with); <path>/api/v1 is tripbox's answer:
                    signed out, every path under <path> is sent to sign-in before it is
                    routed, so a 303 to a place under <path>/ with tripbox's content policy
  flight-log-link   the trips list links the flight log at http://<host>
  journey           the sample pasted at <path>/add/paste is filed, and AirTrail's API, through
                    the proxy, then lists exactly two flights whose note begins `tripbox`, each
                    with one user and three guests aboard
The last line is `onehost probe: ok` (exit 0) or `onehost probe: FAIL <step>` (exit 1); a usage
error exits 2. Nothing of the password, the key, the sample or a page's body is printed: a
failure says on stderr what was answered. Standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

HERE = Path(__file__).resolve().parent
# beside this file in a harness directory, one directory up in the repository
for where in (HERE, HERE.parent):
    if (where / "quickstart_check.py").is_file():
        sys.path.insert(0, str(where))
        break

import quickstart_check as qc  # noqa: E402

PREFIX = "onehost probe:"
REDIRECTS = 5
PAGES = 20  # pages of AirTrail's flight list read at most, a hundred flights each
POLICY = "Content-Security-Policy"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href") or "")


def links(body: str) -> list[str]:
    parser = Links()
    parser.feed(body)
    return parser.hrefs


def follow(b: qc.Browser, path: str) -> qc.Response:
    """GET `path` with no cookie, following redirects on the hostname, at most REDIRECTS: the
    last answer, with every answer on the way (itself the last) in its `chain`."""
    url = b.origin + path
    seen = []
    for _ in range(REDIRECTS + 1):
        r = b.request("GET", url, jar={})
        seen.append(r)
        if r.status not in (301, 302, 303, 307, 308):
            r.chain = seen
            return r
        url = urljoin(url, r.header("Location"))
    raise qc.Fail(f"{path} redirected more than {REDIRECTS} times")


def flights(b: qc.Browser, key: str) -> list[dict]:
    """The flights AirTrail lists for the key's user, through the proxy."""
    out: list[dict] = []
    cursor = None
    for _ in range(PAGES):
        query = {"limit": "100", **({"cursor": cursor} if cursor else {})}
        target = "/api/v1/flights?" + urlencode(query)
        conn = http.client.HTTPConnection(b.connect, timeout=qc.REQUEST_SECONDS)
        try:
            conn.request("GET", target, headers={"Host": b.netloc, "Accept": "application/json",
                                                 "Authorization": f"Bearer {key}"})
            raw = conn.getresponse()
            status, data = raw.status, raw.read(qc.BODY_CAP)
        except (OSError, http.client.HTTPException) as exc:
            raise qc.Fail(f"AirTrail's flight list: no answer ({type(exc).__name__})") from None
        finally:
            conn.close()
        if status != 200:
            raise qc.Fail(f"AirTrail's flight list answered {status}")
        try:
            body = json.loads(data)
        except ValueError:
            raise qc.Fail("AirTrail's flight list is not JSON") from None
        page = body.get("data") if isinstance(body, dict) else None
        if not isinstance(page, list):
            raise qc.Fail("AirTrail's flight list holds no list")
        out += [f for f in page if isinstance(f, dict)]
        cursor = ((body.get("page") or {}) if isinstance(body.get("page"), dict) else {}).get(
            "nextCursor")
        if not cursor:
            return out
    raise qc.Fail(f"AirTrail's flight list is longer than {PAGES} pages")


def aboard(flight: dict) -> tuple[int, int]:
    """(users, guests) on a flight as AirTrail lists it: a guest is a passenger with no user."""
    users = guests = 0
    for p in flight.get("passengers") or []:
        if isinstance(p, dict) and (isinstance(p.get("user"), dict) or p.get("userId")):
            users += 1
        else:
            guests += 1
    return users, guests


def walk(b: qc.Browser, path: str, password: str, key: str, sample: str, timeout: float,
         step) -> None:
    with step("airtrail"):
        r = follow(b, "/")
        qc.expect(r, 200, "the hostname's /")
        if "/_app/" not in r.body:
            raise qc.Fail("the hostname's / did not end at a page of AirTrail's")
        r = b.request("GET", b.origin + "/api/v1", jar={})
        qc.expect(r, 200, "/api/v1")
        try:
            name = (json.loads(r.body).get("instance") or {}).get("name")
        except (ValueError, AttributeError):
            name = None
        if name != "AirTrail":
            raise qc.Fail("/api/v1 does not name the instance AirTrail")

    with step("tripbox"):
        r = b.request("GET", b.url("/healthz"), jar={})
        qc.expect(r, 200, "tripbox's health check")
        if r.body.strip() != "ok":
            raise qc.Fail("tripbox's health check did not say ok")
        own = r.header(POLICY)
        if not own:
            raise qc.Fail("tripbox's health check came without its content policy")
        r = b.request("GET", b.base, jar={})
        qc.expect(r, 307, f"{path}")
        if r.header("Location") != path + "/":
            raise qc.Fail(f"{path} did not lead to {path}/")
        r = b.request("POST", b.url("/login"), {"password": password})
        qc.expect(r, 303, "signing in")
        where = urlsplit(r.header("Location"))
        if where.scheme or where.netloc or not where.path.startswith(path + "/"):
            raise qc.Fail("signing in led outside tripbox's path")
        cookies = [line for k, line in r.headers if k.lower() == "set-cookie"]
        if not cookies:
            raise qc.Fail("signing in set no cookie")
        for line in cookies:
            attrs = [part.strip() for part in line.split(";")[1:]]
            given = [a.split("=", 1)[1] for a in attrs if a.lower().startswith("path=")]
            if given != [path]:
                raise qc.Fail(f"a cookie's Path is not {path}")

    with step("neighbours"):
        r = follow(b, path + "x")
        for got in r.chain:
            if got.header(POLICY) == own or any(
                    k.lower().startswith("x-tripbox") for k, _ in got.headers):
                raise qc.Fail(f"{path}x came back with a header of tripbox's")
        if "/_app/" not in r.body:
            raise qc.Fail(f"{path}x is not AirTrail's answer")
        # signed out, tripbox sends every path under its own to sign-in before routing it
        r = b.request("GET", b.url("/api/v1"), jar={})
        qc.expect(r, 303, f"{path}/api/v1")
        where = urlsplit(r.header("Location"))
        if where.scheme or where.netloc or not where.path.startswith(path + "/"):
            raise qc.Fail(f"{path}/api/v1 led outside tripbox's path")
        if r.header(POLICY) != own:
            raise qc.Fail(f"{path}/api/v1 is not tripbox's answer")

    with step("flight-log-link"):
        r = b.request("GET", b.url("/"))
        qc.expect(r, 200, "the trips list")
        if not {b.origin, b.origin + "/"} & set(links(r.body)):
            raise qc.Fail(f"the trips list does not link the flight log at {b.origin}")

    with step("journey"):
        r = b.request("POST", b.url("/add/paste"), {"text": sample})
        qc.expect(r, 303, "the paste")
        upload = b.location(r, b.url("/add/paste"))
        if not upload.startswith(b.url("/uploads/")):
            raise qc.Fail("the paste did not lead to an upload's page")
        deadline = time.monotonic() + timeout
        while "Filed" not in b.request("GET", upload).body:
            if time.monotonic() >= deadline:
                raise qc.Fail(f"the paste was not filed within {timeout:g} s")
            time.sleep(1)
        while True:
            ours = [f for f in flights(b, key) if str(f.get("note") or "").startswith("tripbox")]
            if len(ours) == 2 and all(aboard(f) == (1, 3) for f in ours):
                break
            if time.monotonic() >= deadline:
                raise qc.Fail(f"AirTrail lists {len(ours)} flights from tripbox, people aboard "
                              f"{sorted(aboard(f) for f in ours)}, after {timeout:g} s")
            time.sleep(2)


def ready(b: qc.Browser, seconds: float) -> None:
    """Waits until tripbox's health check answers through the proxy, or `seconds` pass."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if b.request("GET", b.url("/healthz"), jar={}).status == 200:
                return
        except qc.Fail:
            pass
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk AirTrail and tripbox at one hostname.")
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--wait", type=float, default=120.0)
    args = parser.parse_args(argv)
    proxy = urlsplit(args.proxy)
    if proxy.scheme != "http" or not proxy.netloc or proxy.path not in ("", "/"):
        parser.error("--proxy must be http://host[:port]")
    if not args.host or any(c in args.host for c in "/:@ "):
        parser.error("--host must be a hostname")
    path = args.path.rstrip("/")
    if not path.startswith("/") or any(c in path for c in "?#: "):
        parser.error("--path must be a path such as /tripbox")
    password = os.environ.get("ONEHOST_PASSWORD", "")
    key = os.environ.get("ONEHOST_AIRTRAIL_KEY", "")
    try:
        with open(args.sample, "rb") as fh:
            sample = fh.read(qc.SAMPLE_CAP + 1)
    except OSError as exc:
        parser.error(f"cannot read the sample: {exc.strerror or exc}")
    if not password or not key or len(sample) > qc.SAMPLE_CAP:
        parser.error("ONEHOST_PASSWORD or ONEHOST_AIRTRAIL_KEY is empty, or the sample is over "
                     "2 MiB")

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

    b = qc.Browser(f"http://{args.host}{path}", proxy.netloc)
    try:
        ready(b, args.wait)
        walk(b, path, password, key, sample.decode("utf-8", "replace"), args.timeout, Step)
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
