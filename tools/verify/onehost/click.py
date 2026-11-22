#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Clicks the Trips button as a person would: Chromium, told that the hostname is the proxy's
address (`--host-resolver-rules=MAP <host> <proxy>`), opens AirTrail's demo door, which signs it
in, finds the element named Trips, and clicks it.

Usage: python3 click.py [--host flights.example.com] [--proxy proxy] [--path /tripbox]
                        [--timeout 60]

Steps, each printed as `dock click: <step> ok` once it passes:
  demo   http://<host>/demo signs the browser in to AirTrail (a public demo, DEMO_MODE=true) and
         ends on AirTrail's map at /
  link   the element whose accessible name is Trips is a link to <path>
  click  a click on it ends at <path>/login, on a page with tripbox's password field
The last line is `dock click: ok` (exit 0) or `dock click: FAIL <step>` (exit 1). Playwright's
Python package and its Chromium (the screenshot stack's image) are all it needs.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlsplit

PREFIX = "dock click:"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Click the Trips button.")
    parser.add_argument("--host", default="flights.example.com")
    parser.add_argument("--proxy", default="proxy")
    parser.add_argument("--path", default="/tripbox")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    origin = f"http://{args.host}"
    wait = args.timeout * 1000
    step = "demo"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=[
                "--disable-dev-shm-usage",
                f"--host-resolver-rules=MAP {args.host} {args.proxy}",
            ])
            try:
                page = browser.new_page()
                page.set_default_timeout(wait)
                page.goto(origin + "/demo", wait_until="load")
                if urlsplit(page.url).path != "/":
                    raise RuntimeError(f"the demo door ended at {urlsplit(page.url).path}")
                print(f"{PREFIX} demo ok", flush=True)

                step = "link"
                trips = page.get_by_role("link", name="Trips", exact=True)
                trips.wait_for(state="visible")
                if trips.count() != 1 or trips.get_attribute("href") != args.path:
                    raise RuntimeError("the element named Trips is no one link to the trips page")
                print(f"{PREFIX} link ok", flush=True)

                step = "click"
                trips.click()
                page.wait_for_url(lambda url: urlsplit(url).path == args.path + "/login")
                field = page.locator("form input[type=password][name=password]")
                field.wait_for(state="visible")
                if urlsplit(page.url).netloc != args.host or field.count() != 1:
                    raise RuntimeError("the click did not end on tripbox's sign-in page")
                print(f"{PREFIX} click ok", flush=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - any failure names its step, never a page's text
        print(f"{PREFIX} {step}: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
              if str(exc) else f"{PREFIX} {step}: {type(exc).__name__}", file=sys.stderr)
        print(f"{PREFIX} FAIL {step}")
        return 1
    print(f"{PREFIX} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
