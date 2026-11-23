# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The Trips button of AirTrail's dock (integrations/airtrail/): the patch itself, read and applied
by a real `git` to a throwaway copy of the lines it touches; build.sh and patch-check.sh with stub
`git` (its clone and ls-remote; every other git command is the real one), `docker` and `gh` first
on PATH, so nothing is fetched, built or posted; and the weekly workflow's text."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
INTEGRATION = ROOT / "integrations" / "airtrail"
PATCH = INTEGRATION / "trips-dock.patch"
BUILD = INTEGRATION / "build.sh"
CHECK = INTEGRATION / "patch-check.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "dock-patch.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
NAV = "src/lib/components/NavigationDock.svelte"
ITEM = "src/lib/components/dock/DockTooltipItem.svelte"
PLACEHOLDER = "__TRIPBOX_TRIPS_URL__"
DEFAULT_REPO = "https://github.com/johanohly/AirTrail.git"
BACKUP = ("Back up AirTrail's database before you start this image: AirTrail upgrades its "
          "database on every start.")
DIGEST = "sha256:" + "e" * 64
# A Dockerfile shaped as upstream's: an overlay pinned by digest, two stages from one tag-only
# image (one built on the builder's own platform), a release stage from another.
DOCKERFILE = (
    f"FROM example/overlay:1@{DIGEST} AS overlay\n"
    "\n"
    "FROM --platform=$BUILDPLATFORM oven/bun:debian AS build\n"
    "RUN true\n"
    "FROM oven/bun:debian AS install\n"
    "FROM node:22-slim AS release\n"
    "COPY --from=install /a /b\n"
)
REAL_GIT = shutil.which("git")

GIT_STUB = """#!/usr/bin/env bash
# clone copies $STUB_UPSTREAM; ls-remote prints $STUB_TAGS; sparse-checkout does nothing; every
# other command is the real git's.
printf '%s\\n' "$*" >>"$STUB_LOG.git"
sub=("$@")
while [ "${sub[0]:-}" = -c ] || [ "${sub[0]:-}" = -C ]; do sub=("${sub[@]:2}"); done
case "${sub[0]:-}" in
  clone)
    [ -z "${STUB_CLONE_FAIL:-}" ] || exit 128
    dest="${@: -1}"
    mkdir -p "$dest" && cp -R "$STUB_UPSTREAM/." "$dest/"
    exit $?
    ;;
  ls-remote)
    for tag in $STUB_TAGS; do printf '%s\\trefs/tags/%s\\n' "$(printf 'a%.0s' {1..40})" "$tag"; done
    exit 0
    ;;
esac
case " $* " in *" sparse-checkout "*) exit 0 ;; esac
exec "$REAL_GIT" "$@"
"""
DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\t%s\\n' "$PWD" "$*" >>"$STUB_LOG.docker"
cp docker/Dockerfile "$STUB_LOG.dockerfile" 2>/dev/null || true
exit 0
"""
GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "${*//$'\\n'/\\\\n}" >>"$STUB_LOG.gh"
if [ "$1 $2" = "issue list" ]; then cat "$STUB_OPEN" 2>/dev/null; fi
exit 0
"""


def preimage(patch_text: str) -> dict[str, str]:
    """Per file the patch changes, the lines its hunks hold of that file before the change (their
    context, in order): a copy of upstream's two files the patch applies to, offsets aside."""
    files: dict[str, list[str]] = {}
    current = None
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            current, in_hunk = line.split(" b/", 1)[1], False
            files[current] = []
        elif line.startswith("@@") and current:
            in_hunk = True
        elif in_hunk and current and line[:1] in (" ", "-"):
            files[current].append(line[1:])
        elif in_hunk and line == "":
            files[current].append("")
    return {name: "\n".join(lines) + "\n" for name, lines in files.items()}


def upstream(base: Path, mangle: str | None = None) -> Path:
    """A throwaway copy of upstream's tree as far as the build reads it: the two dock files (one of
    them changed so the patch no longer fits when `mangle` names it) and docker/Dockerfile."""
    tree = base / "upstream"
    for name, text in preimage(PATCH.read_text()).items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == mangle:
            text = text.replace("    LayoutList,\n", "    List,\n").replace(
                "      aria-label={item.label}\n", "      title={item.label}\n")
        path.write_text(text)
    (tree / "docker").mkdir(parents=True)
    (tree / "docker" / "Dockerfile").write_text(DOCKERFILE)
    return tree


class Bench:
    def __init__(self, tmp: Path, mangle: str | None = None, tags: str = "v3.14.0"):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True)
        for name, text in (("git", GIT_STUB), ("docker", DOCKER_STUB), ("gh", GH_STUB)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.log = tmp / "log"
        self.temp = tmp / "temp"
        self.temp.mkdir()
        self.open = tmp / "open.txt"
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "TMPDIR": str(self.temp),
            "REAL_GIT": REAL_GIT,
            "STUB_LOG": str(self.log),
            "STUB_UPSTREAM": str(upstream(tmp, mangle)),
            "STUB_TAGS": tags,
            "STUB_OPEN": str(self.open),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def run(self, script: Path, *args: str, **env: str):
        return subprocess.run(["bash", str(script), *args], env={**self.env, **env}, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60, check=False)

    def calls(self, tool: str) -> list[str]:
        path = Path(f"{self.log}.{tool}")
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def bench(tmp_path):
    if REAL_GIT is None:
        pytest.skip("git is not installed")
    return Bench(tmp_path)


# ---------------------------------------------------------------- the patch


def test_the_patch_adds_lines_to_the_two_dock_files_and_removes_none(tmp_path):
    r = subprocess.run([REAL_GIT or "git", "apply", "--numstat", str(PATCH)], cwd=tmp_path,
                       capture_output=True, text=True, check=True)
    rows = [line.split("\t") for line in r.stdout.splitlines()]
    assert {row[2]: (int(row[0]), int(row[1])) for row in rows} == {NAV: (8, 0), ITEM: (1, 0)}
    added = [line[1:] for line in PATCH.read_text().splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    # the icon in its alphabetical place, the item after Statistics, the item in the dock, the
    # browser's own navigation for a dock link
    assert added == [
        "    Luggage,",
        "  const tripsItem = {",
        "    label: 'Trips',",
        "    icon: Luggage,",
        "    testId: 'trips-button',",
        f"    href: '{PLACEHOLDER}',",
        "  };",
        "      <DockTooltipItem item={tripsItem} />",
        "      data-sveltekit-reload",
    ]
    # and it applies, by a real git, to the lines it was written against
    tree = upstream(tmp_path)
    subprocess.run([REAL_GIT or "git", "apply", str(PATCH)], cwd=tree, check=True)
    nav = (tree / NAV).read_text()
    assert "    LayoutList,\n    Luggage,\n    Map," in nav
    assert nav.index("<DockTooltipItem item={flightsStatisticsItem} />") < nav.index(
        "<DockTooltipItem item={tripsItem} />") < nav.index("{#if otherItems.length > 0}")
    # behind no permission: the line after the statistics block's end, before More's
    lines = nav.splitlines()
    at = lines.index("      <DockTooltipItem item={tripsItem} />")
    assert lines[at - 1] == "      {/if}" and lines[at + 1] == "      {#if otherItems.length > 0}"
    item = (tree / ITEM).read_text()
    assert "      aria-label={item.label}\n      data-sveltekit-reload\n    >" in item


def test_the_patch_holds_its_placeholder_once_and_its_licence_before_the_diff():
    text = PATCH.read_text()
    assert text.count(PLACEHOLDER) == 1
    head = text[: text.index("diff --git ")]
    assert head.splitlines()[0] == "SPDX-License-Identifier: GPL-3.0-only"
    assert "modifies AirTrail (copyright its authors" in " ".join(head.split())
    assert "GNU General Public License version 3" in " ".join(head.split())
    assert re.search(r"^Written for AirTrail v3\.14\.0\.", head, re.M)
    assert [line.split(" b/")[1] for line in text.splitlines()
            if line.startswith("diff --git ")] == [NAV, ITEM]


# ---------------------------------------------------------------- build.sh


@pytest.mark.parametrize(
    "args",
    [
        ("3.14.0", "/tripbox", "img:1"),
        ("v3.14", "/tripbox", "img:1"),
        ("v3.15.0-beta.1", "/tripbox", "img:1"),
        ("main", "/tripbox", "img:1"),
        ("v3.14.0", "/trip'box", "img:1"),
        ("v3.14.0", '/trip"box', "img:1"),
        ("v3.14.0", "/trip box", "img:1"),
        ("v3.14.0", "/trip$box", "img:1"),
        ("v3.14.0", "/tripbox?x=1", "img:1"),
        ("v3.14.0", "/", "img:1"),
        ("v3.14.0", "//trips.example.com/tripbox", "img:1"),
        ("v3.14.0", "tripbox", "img:1"),
        ("v3.14.0", "ftp://trips.example.com", "img:1"),
        ("v3.14.0", "https://", "img:1"),
        ("v3.14.0", "/tripbox", "-img:1"),
        ("v3.14.0", "/tripbox", "Img:1"),
        ("v3.14.0", "/tripbox"),
    ],
)
def test_build_refuses_a_tag_a_url_or_an_image_it_cannot_use(bench, args):
    r = bench.run(BUILD, *args)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert bench.calls("git") == [] and bench.calls("docker") == []
    if len(args) == 3 and args[0] == "v3.14.0" and args[2] == "img:1":
        assert ("build: the trips URL must be an address such as https://trips.example.com or "
                "a path such as /tripbox") in r.stderr


def test_build_puts_the_url_in_and_builds_from_upstreams_own_file(bench):
    clone = bench.tmp / "clone"
    r = bench.run(BUILD, "v3.14.0", "https://trips.example.com/a-b_c~d", "tripbox-x:1",
                  AIRTRAIL_BUILD_DIR=str(clone))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.splitlines()[-1] == "build: ok tripbox-x:1"
    (cloned,) = [c for c in bench.calls("git") if " clone " in f" {c} "]
    assert cloned.endswith(f"--depth 1 --branch v3.14.0 -- {DEFAULT_REPO} {clone}")
    nav = (clone / NAV).read_text()
    assert "    href: 'https://trips.example.com/a-b_c~d',\n" in nav and PLACEHOLDER not in nav
    assert bench.calls("docker") == [f"{clone}\tbuild -f docker/Dockerfile -t tripbox-x:1 ."]
    # without AIRTRAIL_BASE_IMAGES the Dockerfile is upstream's, as it came
    assert Path(f"{bench.log}.dockerfile").read_text() == DOCKERFILE
    assert list(bench.temp.iterdir()) == []  # its own temporary folder is gone

    # another repository, and by default a temporary clone, removed at the end
    other = Bench(bench.tmp / "second")
    r = other.run(BUILD, "v3.14.0", "/tripbox", "tripbox-x:2",
                  AIRTRAIL_REPO="https://git.example.com/airtrail.git")
    assert r.returncode == 0, r.stderr
    (cloned,) = [c for c in other.calls("git") if " clone " in f" {c} "]
    assert " -- https://git.example.com/airtrail.git " in cloned
    (built,) = other.calls("docker")
    assert built.split("\t")[0].startswith(str(other.temp))
    assert list(other.temp.iterdir()) == []


def test_build_pins_each_base_the_file_names_and_fails_on_one_it_does_not_find(bench):
    bases = bench.tmp / "bases.txt"
    bun, node = f"oven/bun:debian@{DIGEST}", f"node:22-slim@{DIGEST}"
    bases.write_text(f"# the two tag-only bases\noven/bun:debian {bun}\n\nnode:22-slim {node}\n")
    r = bench.run(BUILD, "v3.14.0", "/tripbox", "tripbox-x:1", AIRTRAIL_BASE_IMAGES=str(bases),
                  AIRTRAIL_BUILD_DIR=str(bench.tmp / "clone"))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert Path(f"{bench.log}.dockerfile").read_text() == (
        f"FROM example/overlay:1@{DIGEST} AS overlay\n"
        "\n"
        f"FROM --platform=$BUILDPLATFORM {bun} AS build\n"
        "RUN true\n"
        f"FROM {bun} AS install\n"
        f"FROM {node} AS release\n"
        "COPY --from=install /a /b\n"
    )
    said = []
    for text in (f"node:22-slim {node}\nalpine:3 alpine:3@{DIGEST}\n",  # names no FROM line
                 "node:22-slim\n", f"node:22-slim {node} extra\n"):     # not two images
        again = Bench(bench.tmp / f"b{len(said)}")
        bases.write_text(text)
        r = again.run(BUILD, "v3.14.0", "/tripbox", "tripbox-x:1",
                      AIRTRAIL_BASE_IMAGES=str(bases))
        assert r.returncode == 1, (text, r.stderr)
        assert r.stdout.splitlines()[-1] == "build: FAIL bases", text
        assert again.calls("docker") == [], text
        assert BACKUP not in r.stderr
        said.append(r.stderr.splitlines())
    assert "build: no FROM line of docker/Dockerfile names alpine:3" in said[0]
    assert "build: a line of AIRTRAIL_BASE_IMAGES is not <image> <image to build from>" in said[1]


def test_a_patch_that_does_not_apply_ends_at_patch_and_builds_nothing(tmp_path):
    bench = Bench(tmp_path, mangle=NAV)
    clone = tmp_path / "clone"
    r = bench.run(BUILD, "v3.15.0", "/tripbox", "tripbox-x:1", AIRTRAIL_BUILD_DIR=str(clone))
    assert r.returncode == 1
    assert r.stdout.splitlines()[-1] == "build: FAIL patch"
    assert "build: the patch does not apply to AirTrail v3.15.0; it was written for v3.14.0" in (
        r.stderr.splitlines())
    assert bench.calls("docker") == []
    # checked before anything is changed: the other file is as it came
    assert "data-sveltekit-reload" not in (clone / ITEM).read_text()
    # a clone that fails, and a build folder that is not empty, end at clone
    for env, name in (({"STUB_CLONE_FAIL": "1"}, "a"), ({}, "b")):
        again = Bench(tmp_path / name)
        folder = tmp_path / name / "full"
        folder.mkdir()
        if not env:
            (folder / "x").write_text("x")
        r = again.run(BUILD, "v3.14.0", "/tripbox", "tripbox-x:1",
                      AIRTRAIL_BUILD_DIR=str(folder), **env)
        assert (r.returncode, r.stdout.splitlines()[-1]) == (1, "build: FAIL clone"), name
        assert again.calls("docker") == []


def test_build_ends_with_the_backup_line(bench):
    r = bench.run(BUILD, "v3.14.0", "/tripbox", "tripbox-x:1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == "build: ok tripbox-x:1"
    assert r.stderr.splitlines()[-1] == BACKUP
    # a build that fails says nothing of a backup: there is no image to start
    failing = Bench(bench.tmp / "failing")
    (failing.bin / "docker").write_text("#!/usr/bin/env bash\nexit 1\n")
    r = failing.run(BUILD, "v3.14.0", "/tripbox", "tripbox-x:1")
    assert (r.returncode, r.stdout.splitlines()[-1]) == (1, "build: FAIL image")
    assert BACKUP not in r.stderr


# ---------------------------------------------------------------- patch-check.sh


def test_patch_check_takes_the_newest_version_tag(tmp_path):
    bench = Bench(tmp_path, tags="v3.9.0 v3.14.0 v3.15.0-beta.1 latest v3.10.2")
    r = bench.run(CHECK)
    assert (r.returncode, r.stdout) == (0, "patch check: v3.14.0 applies\n"), r.stderr
    calls = bench.calls("git")
    assert calls[0] == f"ls-remote --tags --refs -- {DEFAULT_REPO}"
    (cloned,) = [c for c in calls if " clone " in f" {c} "]
    assert "--depth 1 --branch v3.14.0 --filter=blob:none --sparse" in cloned
    assert any(f"sparse-checkout set --no-cone /{NAV} /{ITEM}" in c for c in calls)
    # a tag given is asked for directly
    r = bench.run(CHECK, "--tag", "v3.9.0")
    assert r.stdout == "patch check: v3.9.0 applies\n"
    assert len([c for c in bench.calls("git") if c.startswith("ls-remote")]) == 1
    for bad in (["--tag", "latest"], ["--tag"], ["--open"], ["x"]):
        assert bench.run(CHECK, *bad).returncode == 2, bad


def test_a_failing_patch_opens_one_issue_and_an_open_one_is_not_opened_twice(tmp_path):
    bench = Bench(tmp_path, mangle=ITEM, tags="v3.14.0 v3.16.1")
    title = "The dock patch no longer applies to AirTrail v3.16.1"
    bench.open.write_text("Another issue\n")
    r = bench.run(CHECK, "--open-issue")
    assert (r.returncode, r.stdout.splitlines()[-1]) == (1, "patch check: v3.16.1 FAILS")
    created = [c for c in bench.calls("gh") if c.startswith("issue create")]
    assert len(created) == 1
    assert created[0].startswith(f"issue create --title {title} --body ")
    body = created[0].split(" --body ", 1)[1]
    for said in ("v3.16.1", NAV, ITEM, "integrations/airtrail/patch-check.sh --tag v3.16.1"):
        assert said in body, said
    # once it is open, the next week's failure opens none
    bench.open.write_text(f"Another issue\n{title}\n")
    r = bench.run(CHECK, "--open-issue")
    assert r.returncode == 1
    assert len([c for c in bench.calls("gh") if c.startswith("issue create")]) == 1
    assert len([c for c in bench.calls("gh") if c.startswith("issue list")]) == 2


def test_without_open_issue_nothing_is_posted(tmp_path):
    bench = Bench(tmp_path, mangle=NAV)
    r = bench.run(CHECK)
    assert (r.returncode, r.stdout) == (1, "patch check: v3.14.0 FAILS\n")
    assert bench.calls("gh") == []
    # and a patch that applies posts nothing even when asked to
    fine = Bench(tmp_path / "fine")
    assert fine.run(CHECK, "--open-issue").returncode == 0
    assert fine.calls("gh") == []


# ---------------------------------------------------------------- the weekly workflow


def test_the_weekly_workflow_runs_only_in_the_public_repository():
    text = WORKFLOW.read_text()
    lines = [line.strip() for line in text.splitlines()]
    flow = yaml.safe_load(text)
    assert flow["name"] == "dock patch"
    triggers = flow[True]  # YAML 1.1 reads the key `on` as true
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 6 * * 1"}]
    assert "push" not in text and "pull_request" not in text
    assert flow["permissions"] == {"contents": "read"}
    assert flow["concurrency"] == {"group": "dock-patch", "cancel-in-progress": True}
    assert list(flow["jobs"]) == ["applies"]
    job = flow["jobs"]["applies"]
    # this repository alone, never a fork
    own = "github.repository == 'Good-Work-Dojo/tripbox'"
    assert f"if: {own}" in lines
    assert job["if"] == own
    assert "runs-on: ubuntu-24.04" in lines and job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 5
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    pinned = next(line for line in CI.read_text().splitlines() if "actions/checkout@" in line)
    steps = job["steps"]
    assert [s.get("uses") or s.get("run") for s in steps] == [
        pinned.split("uses: ", 1)[1].split(" #", 1)[0],
        "python3 tools/verify/check_workflows.py",
        "integrations/airtrail/patch-check.sh --open-issue",
    ]
    assert steps[0]["with"] == {"persist-credentials": False}
    assert steps[2]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    r = subprocess.run([sys.executable, str(HERE / "check_workflows.py"),
                        str(WORKFLOW.parent)], capture_output=True, text=True, check=False)
    assert (r.returncode, r.stderr) == (0, "")
