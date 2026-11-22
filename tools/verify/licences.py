#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The dependency licence audit: every Python package the runtime image installs is under a licence
the project takes (ALLOWED), and the generated block of THIRD_PARTY_NOTICES.md lists exactly those
packages.

    licences.py --collect
        inside the runtime image, with no network: prints each installed distribution as JSON (its
        name, home page and what it says of its licence)
    licences.py --dists FILE [--write] [ROOT]
        on the host: checks that JSON against the allow-list and the block of
        ROOT/THIRD_PARTY_NOTICES.md between the two marker lines; --write rewrites that block and
        nothing else

A licence is read from `License-Expression`, else from a `License` field that is itself an SPDX
expression, else from licence classifiers that each name one licence. An expression passes only
when every licence it names is allowed (so `MIT OR GPL-3.0-only` fails); GPL, AGPL, LGPL and SSPL
are refused by not being listed. A block line is `name — licence — home page`, sorted by name, with
no version, so a version bump leaves the file alone. `tripbox` itself is not in the closure. Exit 0
when clean, 1 on a finding, 2 when FILE cannot be read."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTICES = "THIRD_PARTY_NOTICES.md"
BEGIN = "<!-- licences:begin -->"
END = "<!-- licences:end -->"
SELF = "tripbox"
ALLOWED = frozenset(
    {
        "MIT",
        "MIT-0",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "0BSD",
        "PSF-2.0",
        "MPL-2.0",
        "Unlicense",
        "CC0-1.0",
    }
)
# a package whose metadata names its licence without a version ("BSD License") and whose licence
# file, read in the installed package of the version given, is the licence given here; another
# version is read again by hand before it is accepted
OVERRIDES = {"jinja2": ("3.1.6", "BSD-3-Clause")}
# classifiers that name one licence and its version; one without a version (Apache Software
# License, BSD License) names none
CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "License :: CC0 1.0 Universal (CC0 1.0) Public Domain Dedication": "CC0-1.0",
}
COPYLEFT = re.compile(r"^(A|L)?GPL|^SSPL", re.I)
_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+-]+)")


@dataclass(frozen=True)
class Package:
    name: str
    licence: str | None  # an SPDX expression, None when none could be read
    home: str


def parse_expression(expr: str) -> list[str] | None:
    """the licence ids an SPDX expression names (AND, OR and parentheses; a WITH exception is not
    taken), None when it is no expression of that form"""
    tokens, pos = [], 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            return None
        tokens.append(m.group(1))
        pos = m.end()
        if not expr[pos:].strip():
            break
    ids, depth, want_id = [], 0, True
    for t in tokens:
        if t == "(" and want_id:
            depth += 1
        elif t == ")" and not want_id and depth:
            depth -= 1
        elif t in ("AND", "OR") and not want_id:
            want_id = True
        elif (
            want_id
            and t not in ("(", ")", "AND", "OR", "WITH")
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", t)
        ):
            ids.append(t)
            want_id = False
        else:
            return None
    return ids if ids and depth == 0 and not want_id else None


def allowed(licence: str) -> bool:
    """every licence the expression names is on the allow-list"""
    ids = parse_expression(licence)
    return ids is not None and all(i in ALLOWED for i in ids)


def licence_of(dist: dict) -> str | None:
    """a distribution's licence as an SPDX expression, from what --collect read"""
    if (dist.get("license_expression") or "").strip():
        return dist["license_expression"].strip()
    text = (dist.get("license") or "").strip()
    ids = parse_expression(text) if text and "\n" not in text else None
    if ids and all(i in ALLOWED or COPYLEFT.search(i) for i in ids):
        return text
    classifiers = [c for c in dist.get("classifiers") or [] if c.startswith("License ::")]
    named = {CLASSIFIERS[c] for c in classifiers if c in CLASSIFIERS}
    if len(named) == 1 and len(classifiers) == len(named):
        return next(iter(named))
    read = OVERRIDES.get(_normal(dist["name"]))
    return read[1] if read is not None and dist.get("version") == read[0] else None


def _normal(name: str) -> str:
    """a distribution's name as packaging compares them: case and `-`, `_`, `.` runs folded"""
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(dists: list[dict]) -> list[Package]:
    """the packages the audit covers: every distribution but the project itself, once each"""
    seen: dict[str, Package] = {}
    for d in dists:
        name = d["name"]
        if _normal(name) == SELF:
            continue
        home = (d.get("home_page") or "").strip() or f"https://pypi.org/project/{name}/"
        found = Package(name, licence_of(d), home)
        held = seen.get(_normal(name))
        # one name read twice keeps the reading the audit would refuse, never hides it
        if held is None or not _passes(found):
            seen[_normal(name)] = found if held is None or _passes(held) else held
    return sorted(seen.values(), key=lambda p: p.name.lower())


def _passes(p: Package) -> bool:
    return p.licence is not None and allowed(p.licence)


def block(packages: list[Package]) -> str:
    """the generated block, markers included: one list line per package, no version"""
    lines = [f"- {p.name} — {p.licence} — {p.home}" for p in packages]
    return "\n".join([BEGIN, *lines, END])


def current_block(text: str) -> tuple[int, int] | None:
    """where the block is in `text` (its first marker's start, its last marker's end), None unless
    there is one pair"""
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
        return None
    return text.index(BEGIN), text.index(END) + len(END)


def check(root: Path, dists: list[dict], write: bool = False) -> tuple[int, list[str]]:
    """(exit status, the lines to print) of the audit of `dists` against the notices under `root`"""
    packages = closure(dists)
    lines = []
    if not packages:
        # the runtime image always installs packages: none read is a collection that failed
        lines.append("licences: no package was read (run --collect in the runtime image)")
    for p in packages:
        if p.licence is None and _normal(p.name) in OVERRIDES:
            lines.append(
                f"licences: {p.name}: its licence was read by hand for version"
                f" {OVERRIDES[_normal(p.name)][0]} only; read this version's licence file and"
                " update OVERRIDES in tools/verify/licences.py"
            )
        elif p.licence is None:
            lines.append(f"licences: {p.name}: no licence could be read")
        elif not allowed(p.licence):
            lines.append(f"licences: {p.name}: {p.licence} is not on the allow-list")
    path = root / NOTICES
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    span = current_block(text)
    wanted = block(packages)
    if span is None:
        lines.append(f"licences: {NOTICES} has no block between {BEGIN} and {END}")
    elif text[span[0] : span[1]] != wanted:
        if write:
            path.write_text(text[: span[0]] + wanted + text[span[1] :], encoding="utf-8")
        else:
            lines.append(f"licences: {NOTICES} is not current (run with --write)")
    if lines:
        return 1, lines
    return 0, [f"licences: {len(packages)} packages, all allowed, the notices are current"]


def collect() -> list[dict]:
    """the distributions installed in this interpreter's environment (run in the runtime image)"""
    import importlib.metadata as md

    out = []
    for d in md.distributions():
        meta = d.metadata
        urls = meta.get_all("Project-URL") or []
        home = meta.get("Home-page") or ""
        if not home:
            labelled = {
                u.split(",", 1)[0].strip().lower(): u.split(",", 1)[1].strip()
                for u in urls
                if "," in u
            }
            home = next(
                (
                    labelled[k]
                    for k in ("homepage", "home", "repository", "source")
                    if k in labelled
                ),
                "",
            )
        out.append(
            {
                "name": meta["Name"],
                "version": meta["Version"],
                "license_expression": meta.get("License-Expression"),
                "license": meta.get("License"),
                "classifiers": [
                    c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")
                ],
                "home_page": home,
            }
        )
    return sorted(out, key=lambda d: d["name"].lower())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Audit the runtime image's Python licences and the notices' block."
    )
    ap.add_argument(
        "--collect", action="store_true", help="print the installed distributions as JSON"
    )
    ap.add_argument("--dists", type=Path, metavar="FILE", help="the JSON --collect printed")
    ap.add_argument("--write", action="store_true", help="rewrite the notices' block")
    ap.add_argument("root", nargs="?", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    if args.collect:
        json.dump(collect(), sys.stdout, indent=1)
        print()
        return 0
    if args.dists is None:
        ap.error("give --collect or --dists FILE")
    try:
        dists = json.loads(args.dists.read_text(encoding="utf-8"))
        if not isinstance(dists, list) or not all(
            isinstance(d, dict) and d.get("name") for d in dists
        ):
            raise ValueError("not a list of distributions")
    except (OSError, ValueError) as e:
        print(f"licences: cannot read {args.dists}: {e}")
        return 2
    rc, lines = check(args.root, dists, write=args.write)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
