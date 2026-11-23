# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The real travellers file a user writes into config/ stays out of git and out of every image
built from the clone; the shipped example stays in both. The build context is nothing but what the
image copies."""
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REAL = "config/travellers.yaml"
EXAMPLE = "config/travellers.example.yaml"


def git_ignores(tmp_path: Path, path: str) -> bool:
    """git's own reading of the repository's .gitignore, in a throwaway repository."""
    repo = tmp_path / "repo"
    if not repo.exists():
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    r = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", "--no-index", path], check=False
    )
    assert r.returncode in (0, 1), r
    return r.returncode == 0


def ignore_patterns() -> list[str]:
    """The .dockerignore's patterns, in order, without blank lines and comments."""
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def docker_pattern(pattern: str) -> re.Pattern[str]:
    """A pattern as Docker reads it: `*` and `?` stay within one path segment, and `**` spans any
    number of them, none included."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            i += 2
            if pattern.startswith("/", i):
                i += 1
            out += ".*" if i == len(pattern) else "(.*/)?"
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def docker_ignores(path: str) -> bool:
    """The build context's rule: the last pattern that matches the path or a directory above it
    wins, and a pattern that starts with ! keeps what it matches."""
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    ignored = False
    for line in ignore_patterns():
        keep = line.startswith("!")
        pattern = docker_pattern(line.removeprefix("!").strip("/"))
        if any(pattern.fullmatch(c) for c in candidates):
            ignored = not keep
    return ignored


def test_the_real_travellers_file_is_ignored_by_git_and_by_docker(tmp_path):
    assert git_ignores(tmp_path, REAL) and not git_ignores(tmp_path, EXAMPLE)
    assert docker_ignores(REAL) and not docker_ignores(EXAMPLE)
    assert docker_ignores(".env") and not docker_ignores(".env.example")  # the reader reads


def test_the_folders_an_install_writes_are_ignored_by_git(tmp_path):
    """tripbox run from the clone writes its ledger, mail, logs and backups beside it."""
    for written in ("data/ledger.db", "archive/a.eml", "logs/tripbox-worker.log",
                    "backups/tripbox-backup.tar.gz"):
        assert git_ignores(tmp_path, written), written
    assert not git_ignores(tmp_path, "src/tripbox/backup.py")


def copy_sources() -> list[str]:
    """Every path the Dockerfile copies from the build context."""
    sources = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and not any(w.startswith("--from") for w in words):
            sources += [w.rstrip("/") for w in words[1:-1] if not w.startswith("--")]
    return sources


def test_the_build_context_is_nothing_but_what_the_image_copies():
    assert ignore_patterns()[0] == "*"
    sources = copy_sources()
    assert {
        "src",
        "scripts",
        "config",
        "tests",
        "compose.yaml",
        "docs/configuration.md",
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES.md",
        "LICENSES",
    } <= set(sources)
    for source in sources:
        assert not docker_ignores(source), source
        # what a copied folder holds comes too, but for the agents' notes and the real travellers
        # file a user writes
        for f in (ROOT / source).rglob("*") if (ROOT / source).is_dir() else ():
            rel = f.relative_to(ROOT).as_posix()
            if f.is_file() and "__pycache__" not in rel and rel != REAL:
                assert docker_ignores(rel) == (f.name in ("AGENTS.md", "CLAUDE.md")), rel
    for left_out in (
        ".git",
        ".git/config",
        ".env",
        ".gitignore",
        "docs/README.md",
        "tools",
        "fixtures",
        ".github/workflows/ci.yml",
        ".superpowers",
        "AGENTS.md",
        "CLAUDE.md",
        "src/tripbox/web/AGENTS.md",
        "src/tripbox/web/CLAUDE.md",
        "src/tripbox/write/AGENTS.md",
        "src/tripbox/write/CLAUDE.md",
        "src/tripbox/__pycache__/cli.cpython-314.pyc",
        "tests/__pycache__",
        "tests/corpus/x.pyc",
    ):
        assert docker_ignores(left_out), left_out
    # a wheel built from the tree leaves the agents' notes out too
    wheel = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]
    assert wheel["targets"]["wheel"]["exclude"] == ["**/AGENTS.md", "**/CLAUDE.md"]


def test_the_build_context_rule_reads_patterns_as_docker_does():
    assert docker_pattern("src/**/AGENTS.md").fullmatch("src/AGENTS.md")
    assert docker_pattern("src/**/AGENTS.md").fullmatch("src/tripbox/web/AGENTS.md")
    assert docker_pattern("src/**/AGENTS.md").fullmatch("src/tripbox/write/AGENTS.md")
    assert docker_pattern("**/__pycache__").fullmatch("__pycache__")
    assert docker_pattern("**/__pycache__").fullmatch("src/tripbox/__pycache__")
    assert not docker_pattern("*").fullmatch("src/tripbox")
    assert docker_pattern("*.pyc").fullmatch("x.pyc") and not docker_pattern("*.pyc").fullmatch(
        "src/x.pyc"
    )
