#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Guardrail for .github/workflows: every job runs on a GitHub-hosted runner named by a literal,
and every action is GitHub's own, pinned to a commit.

A workflow here runs on a GitHub-hosted runner and on no other. The check is line-based, works on
the raw text (no comment or quoting heuristics), and fails closed: it is meant to catch a careless
or copied workflow, and anything it cannot recognise is reported. The rules:
  * a line mentioning `runs-on` (comments included) must be exactly `<indent>runs-on: ubuntu-24.04`
    or `ubuntu-24.04-arm`, optionally quoted, optionally followed by whitespace and a `#` comment;
    the next code line may not be indented deeper (that would continue the scalar);
  * a line mentioning `self-hosted` or `pull_request_target` (comments included) fails;
  * a line mentioning `uses:` must be exactly `<indent>[- ]uses: <value>` where the value is a
    local `./` path or `owner/repo[/path]@<40 lowercase hex>`;
  * an action that is not a local path is GitHub's own: its owner is exactly `actions`
    (`actions/<name>[/path]@...`), so no third party's code runs with a workflow's token;
  * the file needs a top-level block-style `jobs:`; every line at job indentation must be a plain
    job key, and every job needs a conforming `runs-on` line at the indentation of its other keys;
  * only a single `---` as the first line of code is accepted; any other `---` or `...` document
    marker fails;
  * a second top-level `jobs` key (any spelling) fails.
Usage: check_workflows.py [DIR]   (default .github/workflows; a missing DIR is clean)
Prints one `file:line: reason` per failure on stderr and exits 1; exits 0 silently when clean.
"""
import re
import sys
from pathlib import Path

RUNS_ON_OK = re.compile(
    r"""^(?P<indent> *)runs-on: +(?P<q>['"]?)ubuntu-24\.04(?:-arm)?(?P=q)"""
    r"""(?:[ \t]+#.*)?[ \t]*$"""
)
RUNS_ON_ANY = re.compile(r"runs-on", re.IGNORECASE)
FORBIDDEN = re.compile(r"self-hosted|pull_request_target", re.IGNORECASE)
USES_ANY = re.compile(r"""\buses['"]?[ \t]*:""")
USES_OK = re.compile(
    r"""^ *(?:- )?uses: +(?P<q>['"]?)"""
    r"""(?:\./\S*|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^@\s'"]+)?@[0-9a-f]{40})(?P=q)"""
    r"""(?:[ \t]+#.*)?[ \t]*$"""
)
GITHUBS_OWN = re.compile(r"""^ *(?:- )?uses: +['"]?(?:\./|actions/)""")
DOC_MARKER = re.compile(r"^(?:---|\.\.\.)(?:[ \t]|$)")
DOC_START = re.compile(r"^---[ \t]*(?:#.*)?$")
JOBS_ANY = re.compile(r"""^['"]?jobs['"]?[ \t]*:""")
JOBS = re.compile(r"^jobs:[ \t]*(?:#.*)?$")
JOB_KEY = re.compile(r"^[A-Za-z0-9_-]+:[ \t]*(?:#.*)?$")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_code(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def check_text(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    first_code = next((i for i, line in enumerate(lines) if is_code(line)), None)
    for i, line in enumerate(lines):
        n = i + 1
        ro = RUNS_ON_OK.match(line)
        if RUNS_ON_ANY.search(line) and not ro:
            out.append(
                (n, "runs-on must be exactly `runs-on: ubuntu-24.04` or `ubuntu-24.04-arm`")
            )
        if ro:
            nxt = next((x for x in lines[i + 1:] if is_code(x)), None)
            if nxt is not None and indent_of(nxt) > len(ro.group("indent")):
                out.append((n, "runs-on value is followed by a more deeply indented line"))
        m = FORBIDDEN.search(line)
        if m:
            out.append((n, f"`{m.group(0).lower()}` is not allowed"))
        if USES_ANY.search(line) and not USES_OK.match(line):
            out.append(
                (n, "`uses:` must be a plain `uses: ./path` or `owner/repo@<40-hex commit>` line")
            )
        elif USES_ANY.search(line) and not GITHUBS_OWN.match(line):
            out.append((n, "only GitHub's own actions (actions/*) may be used"))
        if DOC_MARKER.match(line) and not (i == first_code and DOC_START.match(line)):
            out.append((n, "YAML document markers are not allowed"))

    jobs_lines = [i for i, line in enumerate(lines) if JOBS_ANY.match(line)]
    if len(jobs_lines) > 1:
        out.append((jobs_lines[1] + 1, "more than one top-level `jobs:` key"))
    jobs_at = next((i for i, line in enumerate(lines) if JOBS.match(line)), None)
    if jobs_at is None:
        out.append(
            (jobs_lines[0] + 1, "`jobs:` must be a plain block mapping of jobs")
            if jobs_lines
            else (1, "no top-level `jobs:` key")
        )
        return out

    # The job blocks: keys at the first indentation level below `jobs:`, up to the next top-level
    # key. Each entry is [line number of the job key, indent of its first child, runs-on lines].
    jobs: list[list] = []
    job_indent = None
    for i in range(jobs_at + 1, len(lines)):
        line = lines[i]
        if not is_code(line):
            continue
        ind = indent_of(line)
        if ind == 0:
            break
        if job_indent is None:
            job_indent = ind
        if ind <= job_indent:
            if ind == job_indent and JOB_KEY.match(line.strip()):
                jobs.append([i + 1, -1, []])
            else:
                out.append((i + 1, "unrecognised job key (use plain `name:` at job indentation)"))
            continue
        if not jobs:
            continue
        job = jobs[-1]
        if job[1] == -1:
            job[1] = ind
        if ind == job[1] and RUNS_ON_OK.match(line):
            job[2].append(i + 1)
    if not jobs:
        out.append((jobs_at + 1, "no job under `jobs:` could be recognised (use block style)"))
    for num, _child, hits in jobs:
        if not hits:
            out.append((num, "job has no conforming `runs-on` line"))
    return out


def main(argv: list[str]) -> int:
    d = Path(argv[1] if len(argv) > 1 else ".github/workflows")
    if not d.exists():
        return 0
    if not d.is_dir():
        print(f"{d}:1: not a directory", file=sys.stderr)
        return 1
    failures: list[str] = []
    try:
        entries = sorted(d.iterdir())
    except OSError:
        print(f"{d}:1: unreadable", file=sys.stderr)
        return 1
    for f in entries:
        if not f.name.lower().endswith((".yml", ".yaml")):
            continue
        try:
            if f.is_dir():
                continue
            text = f.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            failures.append(f"{f}:1: not valid UTF-8")
            continue
        except OSError:
            failures.append(f"{f}:1: unreadable")
            continue
        failures += [f"{f}:{n}: {why}" for n, why in sorted(check_text(text))]
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
