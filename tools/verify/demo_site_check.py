#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Checks an exported demo site (`tripbox demo export`) before it is published.

Usage: python3 tools/verify/demo_site_check.py DIR

Every `.html` file under DIR holds the banner once and no `<script`, no `action=`, no control
without `disabled`, and no address that begins `/` or names a scheme and host (but the source
link of the about line); every relative address resolves to a file of the tree. `index.html`
links at least one trip, and `.nojekyll` exists. The last line is `demo site: ok <n> pages`
(exit 0) or `demo site: FAIL <file>: <what>` (exit 1); a usage error exits 2. Standard library
only."""

from __future__ import annotations

import posixpath
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

BANNER = "Demo: invented trips. Read-only."
CONTROLS = frozenset({"input", "select", "textarea", "button"})
ADDRESS_ATTRS = ("href", "src", "action", "poster", "srcset", "data")


class _Page(HTMLParser):
    """What one page holds that the check is about."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.faults: list[str] = []
        self.addresses: list[str] = []
        self.banners = 0
        self.about = 0  # depth inside the about line
        self.in_banner = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "script":
            self.faults.append("a script")
        if "action" in values:
            self.faults.append("a form with an action")
        if tag in CONTROLS and "disabled" not in values:
            self.faults.append(f"a {tag} that is not disabled")
        if "about" in classes:
            self.about += 1
        if tag == "p" and "demo-banner" in classes:
            self.in_banner = True
        for key in ADDRESS_ATTRS:
            value = values.get(key)
            if not value or key == "action":
                continue
            parts = urlsplit(value)
            if parts.scheme or parts.netloc:
                if tag == "a" and self.about:  # the about line's source link
                    continue
                self.faults.append(f"the address {value}")
            elif value.startswith("/"):
                self.faults.append(f"the address {value}")
            elif parts.path:
                self.addresses.append(parts.path)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "p":
            self.in_banner = False
        if tag == "p" and self.about:
            self.about -= 1

    def handle_data(self, data):
        if self.in_banner and data.strip() == BANNER:
            self.banners += 1


def check_page(root: Path, path: Path) -> list[str]:
    """What is wrong with the page `path` of the tree `root`, each as a sentence."""
    text = path.read_text(encoding="utf-8")
    page = _Page()
    page.feed(text)
    page.close()
    faults = list(page.faults)
    if page.banners != 1:
        faults.append("no banner" if not page.banners else "more than one banner")
    here = posixpath.dirname(path.relative_to(root).as_posix())
    for address in page.addresses:
        target = posixpath.normpath(posixpath.join(here, address))
        if target.startswith("..") or not (root / target).is_file():
            faults.append(f"a link to {address}, which is no file of the site")
    return faults


def check(root: Path) -> tuple[int, str | None]:
    """The number of pages checked and the first fault as `<file>: <what>`, or None."""
    pages = sorted(root.rglob("*.html"))
    for path in pages:
        faults = check_page(root, path)
        if faults:
            return len(pages), f"{path.relative_to(root).as_posix()}: {faults[0]}"
    if not (root / "index.html").is_file():
        return len(pages), "index.html: missing"
    links = _Page()
    links.feed((root / "index.html").read_text(encoding="utf-8"))
    if not any(a.startswith("trips/") for a in links.addresses):
        return len(pages), "index.html: links no trip"
    if not (root / ".nojekyll").is_file():
        return len(pages), ".nojekyll: missing"
    return len(pages), None


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not Path(argv[1]).is_dir():
        print("usage: demo_site_check.py DIR", file=sys.stderr)
        return 2
    pages, fault = check(Path(argv[1]))
    if fault is not None:
        print(f"demo site: FAIL {fault}")
        return 1
    print(f"demo site: ok {pages} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
