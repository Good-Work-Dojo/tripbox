# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The README's named blocks: each fenced block that follows a line `<!-- block: NAME -->`.

`blocks(text)` gives `{name: the block's lines joined by newlines}`, the fences left out. The
quick start's runner and the README's tests read the commands, the settings and the sample from
here, so what the README shows is what is run and checked. A name given twice, a marker with no
fenced block after it (only blank lines may stand between) and a block never closed are errors.
Usage: python3 tools/verify/readme_blocks.py README.md NAME   (prints the block)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

MARKER = re.compile(r"^<!-- block: ([a-z][a-z0-9-]*) -->\s*$")
FENCE = re.compile(r"^(`{3,})[A-Za-z0-9_-]*\s*$")


class BlockError(ValueError):
    """The README's blocks are not as the runner and the tests need them."""


def blocks(text: str) -> dict[str, str]:
    lines = text.splitlines()
    found: dict[str, str] = {}
    n = 0
    while n < len(lines):
        marked = MARKER.match(lines[n])
        n += 1
        if marked is None:
            continue
        name = marked.group(1)
        if name in found:
            raise BlockError(f"the block {name} is given twice")
        while n < len(lines) and not lines[n].strip():
            n += 1
        opened = FENCE.match(lines[n]) if n < len(lines) else None
        if opened is None:
            raise BlockError(f"the marker of the block {name} is not followed by a fenced block")
        n += 1
        body = []
        while n < len(lines) and lines[n].rstrip() != opened.group(1):
            body.append(lines[n])
            n += 1
        if n >= len(lines):
            raise BlockError(f"the block {name} is never closed")
        n += 1
        found[name] = "\n".join(body)
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: readme_blocks.py README.md NAME", file=sys.stderr)
        return 2
    try:
        found = blocks(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, BlockError) as exc:
        print(f"readme_blocks: {exc}", file=sys.stderr)
        return 1
    if argv[2] not in found:
        print(f"readme_blocks: the README has no block {argv[2]}", file=sys.stderr)
        return 1
    print(found[argv[2]])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
