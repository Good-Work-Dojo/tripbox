#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The licence header every source file carries: two comment lines in the file's own comment syntax,

    SPDX-License-Identifier: AGPL-3.0-only
    Additional terms: see NOTICE

placed where the file's format allows a comment without changing what it does: after a shebang (and
a Python encoding line after it) and after a Dockerfile's parser directives, else first (a comment
above a Python module's docstring leaves the docstring the module's first statement). In an HTML
template the two lines are Jinja comments closed with `-#}`, so a rendered page is the same bytes
as before.

Sources are the files of the kinds in STYLES under src/, tests/, tools/,
scripts/, integrations/ and .github/, and Dockerfile and compose.yaml at the top. Data carries
none: JSON, .eml, .txt, .md, .toml, lock files, fonts, pictures, .ics, dotfiles, everything under
tests/corpus/ and tests/data/, and the proxy recipes under docs/proxy/ (a reader copies each into a
proxy's own file). One file is another licence's (OTHER): its text before the first `diff --git`
line must hold `SPDX-License-Identifier: <that licence>`, the licence's text must be in LICENSES/,
and --write never touches it.

Usage: tools/verify/spdx.py [--write] [ROOT]   ROOT defaults to the repository holding this script.
Without --write it prints `spdx: <path> lacks its header` per file and exits 1 when any lacks it;
with --write it adds the header, leaving every other byte as it was. Reads and writes files only."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LICENSE_ID = "SPDX-License-Identifier: AGPL-3.0-only"
TERMS = "Additional terms: see NOTICE"

SOURCE_DIRS = ("src", "tests", "tools", "scripts", "integrations", ".github")
TOP_FILES = ("Dockerfile", "compose.yaml")
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
SKIP_PREFIXES = ("tests/corpus/", "tests/data/")
OTHER = {"integrations/airtrail/trips-dock.patch": "GPL-3.0-only"}


@dataclass(frozen=True)
class Style:
    """how a kind of file writes a comment line, and which leading lines must stay first"""

    open: str
    close: str = ""
    keep_first: re.Pattern[str] | None = (
        None  # leading lines that stay above the header, while they match
    )
    keep_most: int = 1  # and at most this many of them

    def lines(self) -> list[str]:
        return [f"{self.open}{text}{self.close}" for text in (LICENSE_ID, TERMS)]


SHEBANG = re.compile(r"#!.*")
HASH = Style("# ", keep_first=SHEBANG)
PYTHON = Style("# ", keep_first=re.compile(r"#!.*|#.*coding[:=].*"), keep_most=2)
SLASHES = Style("// ")
DOCKERFILE = Style(
    "# ", keep_first=re.compile(r"#\s*(syntax|escape|check)\s*=.*", re.I), keep_most=3
)
CSS = Style("/* ", " */")
HTML = Style("{# ", " -#}")

STYLES = {
    ".py": PYTHON,
    ".sh": HASH,
    ".yaml": HASH,
    ".yml": HASH,
    ".js": SLASHES,
    ".css": CSS,
    ".html": HTML,
}


def style_of(rel: str) -> Style | None:
    """the comment style of the file at `rel` (relative to the root), None if it takes no header"""
    name = rel.rsplit("/", 1)[-1]
    if name == "Dockerfile":
        return DOCKERFILE
    if name.startswith("."):
        return None
    return STYLES.get(Path(name).suffix)


def sources(root: Path = ROOT) -> list[Path]:
    """every source file under `root` that takes the header, in path order"""
    found = [root / name for name in TOP_FILES if (root / name).is_file()]
    for top in SOURCE_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if (
                p.is_file()
                and not SKIP_PARTS & set(p.relative_to(root).parts)
                and not rel.startswith(SKIP_PREFIXES)
                and style_of(rel) is not None
            ):
                found.append(p)
    return sorted(found)


def split(text: str, style: Style) -> tuple[list[str], list[str]]:
    """the lines that stay above the header, and the rest"""
    lines = text.splitlines(keepends=True)
    n = 0
    while (
        style.keep_first
        and n < min(len(lines), style.keep_most)
        and style.keep_first.fullmatch(lines[n].rstrip("\r\n"))
    ):
        n += 1
    return lines[:n], lines[n:]


def read(p: Path) -> str:
    """a file's text with its line endings as they are"""
    return p.read_bytes().decode("utf-8")


def has_header(text: str, style: Style) -> bool:
    """the header sits in its place: after what must stay first, and above anything that must (a
    shebang below it no longer works)"""
    _, rest = split(text, style)
    if [line.rstrip("\r\n") for line in rest[:2]] != style.lines():
        return False
    after = rest[2].rstrip("\r\n") if len(rest) > 2 else ""
    return not (style.keep_first and style.keep_first.fullmatch(after))


def with_header(text: str, style: Style) -> str:
    """`text` with the header in its place (unchanged when it is there already)"""
    if has_header(text, style):
        return text
    head, rest = split(text, style)
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    return "".join(head) + "".join(f"{line}\n" for line in style.lines()) + "".join(rest)


def other_findings(root: Path) -> tuple[list[str], int]:
    """the lines to print for the files of another licence, and how many of them there are"""
    out, present = [], 0
    for rel, licence in OTHER.items():
        p = root / rel
        if not p.is_file():
            continue
        present += 1
        head = read(p).split("diff --git", 1)[0]
        if f"SPDX-License-Identifier: {licence}" not in head:
            out.append(f"spdx: {rel} lacks its header")
        text = f"LICENSES/{licence}.txt"
        if not (root / text).is_file():
            out.append(f"spdx: {text} is missing (the licence of {rel})")
    return out, present


def missing(root: Path = ROOT) -> list[str]:
    """the sources under `root` without the header, as paths relative to it"""
    out = []
    for p in sources(root):
        rel = p.relative_to(root).as_posix()
        if not has_header(read(p), style_of(rel)):
            out.append(rel)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check or add the licence header of every source file."
    )
    ap.add_argument("--write", action="store_true", help="add the header where it is missing")
    ap.add_argument("root", nargs="?", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    lacking = missing(args.root)
    if args.write:
        for rel in lacking:
            p = args.root / rel
            mode = p.stat().st_mode
            p.write_bytes(with_header(read(p), style_of(rel)).encode("utf-8"))
            p.chmod(mode)
        print(f"spdx: added the header to {len(lacking)} file(s)")
        return 0
    others, present = other_findings(args.root)
    findings = [f"spdx: {rel} lacks its header" for rel in lacking] + others
    for line in findings:
        print(line)
    if findings:
        return 1
    print(f"spdx: {len(sources(args.root)) + present} sources, all carry the header")
    return 0


if __name__ == "__main__":
    sys.exit(main())
