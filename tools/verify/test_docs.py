# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What the README and the guides say is held to what is there: every link and picture points at a
file (and every `#anchor` at a heading), every setting named is a setting, every `tripbox`
command is one the program has, every address is an example, loopback or a public project named
here with its reason, and prose fits 100 columns."""

import datetime as dt
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = (
    "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "TRADEMARKS.md", "CLA.md",
    "CHANGELOG.md", "THIRD_PARTY_NOTICES.md",
)  # fmt: skip
TOOL_READMES = ("tools/verify/README.md", "tools/maintainer/README.md")
INTEGRATION = "integrations/airtrail/README.md"
# the documents the project ships, which must all exist
SHIPPED_DOCS = (
    "README.md", "docs/configuration.md", "docs/mail.md", "docs/models.md", "docs/privacy.md",
    "docs/security.md", "docs/airtrail.md", "docs/reverse-proxy.md", "docs/architecture.md",
    "docs/ROADMAP.md", "docs/install.md", "AGENTS.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
    "SECURITY.md", "CHANGELOG.md", ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/parse-problem.yml", ".github/ISSUE_TEMPLATE/feature.yml",
)  # fmt: skip
# Public hosts the docs may name, each with why.
PUBLIC_HOSTS = {
    "github.com": "this project, AirTrail and Docker's release notes are hosted there",
    "good-work-dojo.github.io": "the demo of invented trips",
    "ghcr.io": "the registry that serves the image",
    "imap.gmail.com": "Gmail's own IMAP server, the one a Gmail mailbox is read from",
    "mx.google.com": "the name Gmail's authentication results carry",
    "schema.org": "the vocabulary of the structured data the sample carries",
    "cloudflareaccess.com": "where a Cloudflare Access team's keys are served",
    "api.telegram.org": "Telegram's bot interface, which the notifier calls",
    "wikipedia.org": "where the worker looks up a trip's place for a cover photo",
    "wikimedia.org": "where the worker looks up and downloads freely licensed cover photos",
}
EXAMPLE = re.compile(r"(^|\.)example(\.(com|org|net))?$")
TLDS = "com|org|net|io|dev|app|ai|co|eu|uk|de|fr"
HOST = re.compile(rf"(?<![\w@./-])((?:[a-z0-9][a-z0-9-]*\.)+(?:{TLDS}))(?![\w-])", re.I)
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?:/\d{1,2})?(?![\w.])")
GONE = ("selfcheck", "backfill-airtrail", "fixtures-from-archive")


def doc_files() -> list[Path]:
    found = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    found += [ROOT / name for name in POLICY if (ROOT / name).is_file()]
    found += [ROOT / name for name in (*TOOL_READMES, INTEGRATION) if (ROOT / name).is_file()]
    return found


def prose_files() -> list[Path]:
    """The documents this repository writes in prose: not the legal files it adapts, and not
    the Gmail recipe, which is generated from the sender list."""
    skip = {ROOT / name for name in POLICY} | {ROOT / "docs" / "gmail-filters.md"}
    return [p for p in doc_files() if p not in skip]


def split(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str, str]]]:
    """(line number, line) outside fences, and (line number, language, line) inside them."""
    outside, inside, fence, lang = [], [], None, ""
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(\s*)(`{3,})(.*)$", line)
        if m and fence is None:
            fence, lang = m.group(2), m.group(3).strip()
            continue
        if m and fence is not None and m.group(2) == fence and not m.group(3).strip():
            fence = None
            continue
        (inside.append((n, lang, line)) if fence else outside.append((n, line)))
    return outside, inside


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    outside, _ = split(path.read_text(encoding="utf-8"))
    for _, line in outside:
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if m:
            base = slug(m.group(1))
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.add(base if n == 0 else f"{base}-{n}")
    return out


def spans(path: Path) -> list[str]:
    """Inline code spans outside fences, a span that wraps onto the next line kept whole."""
    outside, _ = split(path.read_text(encoding="utf-8"))
    paragraphs, held = [], []
    for _, line in outside:
        if line.strip():
            held.append(line.strip())
        elif held:
            paragraphs.append(" ".join(held))
            held = []
    paragraphs += [" ".join(held)] if held else []
    return [s for para in paragraphs for s in re.findall(r"`([^`]+)`", para)]


# ----------------------------------------------------------------------------- links
def test_every_file_the_readme_and_the_docs_name_is_in_the_tree():
    bad = []
    for path in doc_files():
        text = path.read_text(encoding="utf-8")
        outside, _ = split(text)
        for n, line in outside:
            for target in re.findall(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", line):
                if re.match(r"^(https?:|mailto:)", target):
                    continue
                name, _, anchor = target.partition("#")
                there = (path.parent / name).resolve() if name else path
                where = f"{path.relative_to(ROOT)}:{n}: {target}"
                if not there.exists():
                    bad.append(where)
                elif anchor and there.suffix == ".md" and anchor not in anchors(there):
                    bad.append(where + " (no such heading)")
    assert not bad, bad


# ----------------------------------------------------------------------------- settings
def test_every_setting_a_doc_names_is_a_setting():
    from tripbox.config import COMPOSE_ONLY, KEY_BY_NAME

    known = set(KEY_BY_NAME) | {name for name, *_ in COMPOSE_ONLY}
    bad = []
    for path in doc_files():
        for name in sorted(set(re.findall(r"\bTRIPBOX_[A-Z0-9_]+\b", path.read_text("utf-8")))):
            if name not in known and not name.endswith("_"):
                bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert not bad, bad


# ----------------------------------------------------------------------------- commands
def commands(path: Path) -> list[tuple[str, str | None]]:
    """The `tripbox WORD [WORD]` of each inline code span and of each shell fence's lines."""
    found = []
    pieces = spans(path)
    _, inside = split(path.read_text(encoding="utf-8"))
    pieces += [line for _, lang, line in inside if lang in ("sh", "bash", "shell", "console")]
    for piece in pieces:
        # a command stands first in its span or line, or after `worker` or `-m`: a sentence that
        # quotes the program's output and starts a clause with its name is no command
        pattern = r"(?:^|\bworker |-m )tripbox ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?"
        for m in re.finditer(pattern, piece):
            found.append((m.group(1), m.group(2)))
    return found


def parser_commands() -> dict[str, set[str]]:
    from tripbox.cli import build_parser

    top = next(a for a in build_parser()._actions if a.dest == "command")
    out = {}
    for name, sub in top.choices.items():
        nested = next((a for a in sub._actions if a.choices and a.dest != "help"), None)
        inner = getattr(nested, "choices", None)
        out[name] = set(inner) if inner and nested.dest != "command" else set()
    return out


def test_no_doc_names_a_command_the_cli_lacks():
    have = parser_commands()
    bad = []
    for path in doc_files():
        text = path.read_text(encoding="utf-8")
        bad += [f"{path.relative_to(ROOT)}: {w}" for w in GONE if w in text]
        for word, second in commands(path):
            if word not in have:
                bad.append(f"{path.relative_to(ROOT)}: tripbox {word}")
            elif have[word] and second and not second.startswith("-") and second not in have[word]:
                bad.append(f"{path.relative_to(ROOT)}: tripbox {word} {second}")
    assert not bad, bad


# ----------------------------------------------------------------------------- addresses
def test_every_address_in_the_docs_is_an_example_or_a_public_project():
    from tripbox import lists

    senders = set(lists.trusted_sender_domains())  # airlines' own domains, in the Gmail recipe
    bad = []
    for path in doc_files():
        text = path.read_text(encoding="utf-8")
        where = path.relative_to(ROOT)
        if "/home/" in text or "C:\\Users" in text:
            bad.append(f"{where}: a home path")
        for host in HOST.findall(text):
            h = host.lower()
            ok = EXAMPLE.search(h) or any(h == p or h.endswith("." + p) for p in PUBLIC_HOSTS)
            ok = ok or h in senders
            if not ok:
                bad.append(f"{where}: {host}")
        for found in IPV4.findall(text):
            try:
                ip = ipaddress.ip_address(found)
            except ValueError:
                continue  # not an address: a version number
            docs = any(
                ip in ipaddress.ip_network(net)
                for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
            )
            if not (ip.is_loopback or docs or found == "0.0.0.0"):
                bad.append(f"{where}: {found}")
    assert not bad, bad


# AirTrail's repository, the one address every link to it uses (its Docker Hub name is another)
AIRTRAIL_REPO = "johanohly/AirTrail"
AIRTRAIL_LINK = re.compile(r"github\.com/([\w.-]+/airtrail)(?![\w-])", re.I)


def test_every_link_to_airtrail_names_its_one_repository():
    paths = [*doc_files(), *sorted((ROOT / "integrations" / "airtrail").glob("*"))]
    found = {}
    for path in paths:
        if path.is_file():
            for name in AIRTRAIL_LINK.findall(path.read_text(encoding="utf-8")):
                found.setdefault(name, []).append(str(path.relative_to(ROOT)))
    assert AIRTRAIL_REPO in found, "no link to AirTrail's repository found"
    assert {name: where for name, where in found.items() if name != AIRTRAIL_REPO} == {}


def test_public_hosts_each_have_a_reason():
    assert all(reason.strip() for reason in PUBLIC_HOSTS.values())


# ----------------------------------------------------------------------------- prose
def test_prose_lines_fit_100_columns():
    bad = []
    for path in prose_files():
        outside, _ = split(path.read_text(encoding="utf-8"))
        for n, line in outside:
            if len(line) <= 100 or line.lstrip().startswith("|") or line.startswith("    "):
                continue
            if re.fullmatch(r"\s*(?:[-*]\s+)?(?:!?\[[^\]]*\]\([^)]*\)|<?https?://\S+>?)\s*", line):
                continue
            bad.append(f"{path.relative_to(ROOT)}:{n}: {len(line)}")
    assert not bad, bad


# ----------------------------------------------------------------------------- the lists
def test_every_shipped_doc_exists():
    assert [name for name in SHIPPED_DOCS if not (ROOT / name).is_file()] == []


# One key phrase of each line of the roadmap, lower-cased.
ROADMAP_LINES = (
    "sign in with airtrail", "tripit", "pdfs for every provider", "webhook",
    "other providers' authentication headers", "ferry", "more languages", "more than one user",
    "outlook", "patched airtrail image", "`export` command", "table of models",
    "importable gmail filters", "prices for more than one model", "forward-only sender list",
    "claim on a message's row", "last_poll_ok", "mail backfill --quiet", "field-length limits",
    "structured-data email", "without `--recorded`", "parser that reads a model's answer",
    "image signing",
    "share links that end on a date you choose", "each client address",
    "shared-secret header", "flight tracker", "list of the saved copies", "screen reader",
    "keeps its notes when it is emptied", "dark scheme", "memory limits as settings",
    "scheduled backups", "stops at once when told", "traefik recipe that was run",
    "run of the compose stack on arm64", "docker desktop", "digest for each saved email",
    "given-up upload", "two stays under one itinerary number", "companions read back",
    "card on the page for the key", "own word in enrichment", "registry with a port",
    "overall deadline", "without the passengers permission", "backfill linking a booking",
    "rebooked in day and number",
)  # fmt: skip


@pytest.mark.parametrize("phrase", ROADMAP_LINES)
def test_the_roadmap_holds_every_line(phrase):
    text = " ".join((ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8").lower().split())
    assert phrase in text


# ----------------------------------------------------------------------------- the changelog
def test_every_release_in_the_changelog_is_dated_and_still_read_by_the_release_tools(tmp_path):
    """Keep a Changelog's heading, `## [X.Y.Z] - YYYY-MM-DD`: the notes release.yml and
    post-publish.sh publish, and the version post-publish.sh reads, come from it whole."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = [ln for ln in text.splitlines() if ln.startswith("## [")]
    released = [h for h in headings if h != "## [Unreleased]"]
    assert released
    for heading in released:
        m = re.fullmatch(r"## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})", heading)
        assert m, heading
        assert dt.date.fromisoformat(m.group(2)) >= dt.date(2026, 11, 22), heading
    first = re.fullmatch(r"## \[([^\]]+)\].*", released[0]).group(1)
    tool = ROOT / "tools" / "verify" / "release_names.py"
    notes = subprocess.run([sys.executable, "-I", str(tool), "notes", str(ROOT / "CHANGELOG.md"),
                            f"v{first}"], capture_output=True, text=True, check=False)
    assert notes.returncode == 0 and notes.stdout.strip(), notes.stderr
    assert not [ln for ln in notes.stdout.splitlines() if ln.startswith("## ")]
    # post-publish.sh's own reading of the first versioned section (its `version` command)
    script = (ROOT / "tools" / "maintainer" / "post-publish.sh").read_text(encoding="utf-8")
    start = "TOOL=\"$(cat <<'PY'\n"
    reader = script[script.index(start) + len(start) : script.index("\nPY\n)\"")]
    (tmp_path / "tool.py").write_text(reader, encoding="utf-8")
    version = subprocess.run([sys.executable, "-I", str(tmp_path / "tool.py"), "version",
                              str(ROOT / "CHANGELOG.md")], capture_output=True, text=True,
                             check=False)
    assert (version.returncode, version.stdout) == (0, f"v{first}\n"), version.stderr
