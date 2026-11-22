# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What ships names no private tool of the maintainers' host, and tools/verify's README says what
each file of its folder is. The tree outside `docs/dev` and `tools/private` (both export-ignored,
see .gitattributes) is read as it stands: the files git would commit (tracked, or new and not
ignored), so a contributor's ignored file, `.env` included, is never read; a tree that is no git
work tree is read whole."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "tools" / "verify"
SELF = "tools/verify/test_shipped_names.py"

# Folders that do not ship: the export drops the first two; the others are tool caches and scratch.
NOT_SHIPPED = ("docs/dev", "tools/private", ".git", ".superpowers")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}

# What names the maintainers' own host and tools, each as a pattern built so that this file does not
# match itself.
PRIVATE_NAMES = {
    "a private tool": re.compile("tools/" + "private"),
    "a home path of the host": re.compile("~/" + "tripbox-"),
    "a port of the program's range": re.compile(r"\b183[0-2][0-9]\b|\b18330\b"),
    "the program's label or project prefix": re.compile("program=" + "tripboxoss|tripboxoss" + "-"),
}

# The few files that must name one, each for the reason given. A file not listed here names none.
ALLOWED = {
    # the gate's one switch to the host's docker guard, and the lock and label of its plain path
    "tools/verify/ci.sh": ("a private tool", "the program's label or project prefix"),
    "tools/verify/narrow.sh": ("a private tool", "the program's label or project prefix"),
    # the export rule that drops the private folders
    ".gitattributes": ("a private tool",),
    # the licence-header check leaves the private folder alone, and its tests prove it
    "tools/verify/spdx.py": ("a private tool",),
    "tools/verify/test_spdx.py": ("a private tool",),
    # the build context excludes the private folder, and the test proves it
    "tools/verify/test_ignore_files.py": ("a private tool",),
    # scans for these very words
    "tools/verify/test_screens.py": ("a private tool",),
    "tools/verify/test_github_files.py": ("a private tool",),
}
# The contributor's guide, which the export writes as AGENTS.md (rewriting this path, and every
# other mention of it, to AGENTS.md). Until then AGENTS.md holds the program's own rules.
GUIDE = "AGENTS.md"
if GUIDE != "AGENTS.md":
    ALLOWED["AGENTS.md"] = tuple(PRIVATE_NAMES)


def _committed() -> list[Path]:
    """The files git would commit, as the secret scan reads them; every file when this is no
    git work tree."""
    listed = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ROOT), "ls-files", "-z", "--cached",
         "--others", "--exclude-standard"],
        capture_output=True, check=False,
    )  # fmt: skip
    if listed.returncode != 0:
        return sorted(ROOT.rglob("*"))
    return sorted(ROOT / name for name in listed.stdout.decode().split("\0") if name)


def shipped_files() -> list[Path]:
    found = []
    for path in _committed():
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if any(rel == n or rel.startswith(n + "/") for n in NOT_SHIPPED):
            continue
        found.append(path)
    return found


def test_no_shipped_file_names_a_private_tool():
    files = shipped_files()
    assert len(files) > 100, "the walk found the tree"
    problems = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if rel == SELF or path.stat().st_size > 4_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for what, pattern in PRIVATE_NAMES.items():
            if what not in ALLOWED.get(rel, ()) and pattern.search(text):
                problems.append(f"{rel}: names {what}")
    assert problems == []


def test_every_allowance_is_still_needed():
    """A file that no longer names what it is allowed to is taken off the list."""
    for rel, whats in ALLOWED.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for what in whats:
            assert PRIVATE_NAMES[what].search(text), f"{rel} no longer names {what}"


def test_every_file_of_tools_verify_is_in_its_readme():
    readme = (VERIFY / "README.md").read_text(encoding="utf-8")
    missing = []
    for path in sorted(VERIFY.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name == "README.md":
            continue
        rel = path.relative_to(VERIFY).as_posix()
        # a file at the top is named as it is; one in a folder by its path, or as code that
        # begins with its name (`probe.py --proxy …`), never by a word of prose
        named = rel in readme if "/" not in rel else rel in readme or f"`{path.name}" in readme
        if not named:
            missing.append(rel)
    assert missing == []
