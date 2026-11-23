# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The workflows, each read as text and as YAML: every one passes the workflow rules and uses
GitHub's own actions alone, pinned by commit; no script interpolates an expression; ci.yml runs
ci.sh's stages under the same names, then the stack and the browser; arm64.yml runs the stack when
what builds the image changes, a pull request's change included; the flight log's end to end runs
only when its paths change. The release's and the demo site's
workflows are held to their jobs, their permissions and what their scripts do, run in bash against
stub `gh`, `git` and `docker`."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CHECKER = HERE / "check_workflows.py"
PINNED = re.compile(r" +(- )?uses: actions/[a-z-]+@[0-9a-f]{40} # v[0-9]+\.[0-9]+\.[0-9]+")


def text(name: str) -> str:
    return (WORKFLOWS / name).read_text("utf-8")


def load(name: str) -> dict:
    flow = yaml.safe_load(text(name))
    flow["on"] = flow.pop(True, flow.get("on"))  # YAML 1.1 reads the key `on` as true
    return flow


def flows() -> list[str]:
    names = sorted(p.name for p in WORKFLOWS.glob("*.y*ml"))
    assert names
    return names


def step(job: dict, name: str) -> dict:
    (found,) = [s for s in job["steps"] if s.get("name") == name]
    return found


# ---------------------------------------------------------------- every workflow


def test_every_workflow_passes_the_rules_and_uses_githubs_actions_alone():
    r = subprocess.run([sys.executable, str(CHECKER), str(WORKFLOWS)], capture_output=True,
                       text=True, check=False)
    assert r.returncode == 0, r.stderr
    checkouts = set()
    for name in flows():
        for line in text(name).splitlines():
            if "uses:" in line:
                assert PINNED.fullmatch(line), f"{name}: {line}"
                if "actions/checkout@" in line:
                    checkouts.add(line.split("uses: ")[1])
    assert len(checkouts) == 1, "one checkout, at one commit, everywhere"
    for name in flows():
        for job in load(name)["jobs"].values():
            for s in job.get("steps", []):
                if s.get("uses", "").startswith("actions/checkout@"):
                    assert s["with"]["persist-credentials"] is False, name


def test_no_script_interpolates_an_expression():
    """Every value a script reads reaches it through its environment, never written into it."""
    for name in flows():
        for job_name, job in load(name)["jobs"].items():
            for s in job.get("steps", []):
                if "run" in s:
                    assert "${{" not in s["run"], f"{name}: {job_name}: {s.get('name')}"


def test_pull_requests_run_with_no_condition_and_never_as_the_target():
    """The repository is public: a pull request's jobs run with no `if:` (a fork of it is public
    too), and nothing runs in the base repository's context."""
    with_pr = [n for n in flows() if "pull_request" in (load(n)["on"] or {})]
    assert set(with_pr) == {"ci.yml", "flightlog.yml", "arm64.yml"}
    for name in with_pr:
        for job_name, job in load(name)["jobs"].items():
            assert "if" not in job, f"{name}: {job_name}"
    for name in flows():
        assert "pull_request_target" not in text(name)
        assert "repository.private" not in text(name), name


# ---------------------------------------------------------------- ci.yml and ci.sh


def ci_sh_stages() -> list[str]:
    return re.findall(r'^echo "== (.+) =="$', (HERE / "ci.sh").read_text("utf-8"), re.M)


def test_ci_yml_and_ci_sh_run_the_same_stages():
    flow = load("ci.yml")
    assert flow["on"] == {"push": {"branches": ["main"]}, "pull_request": None,
                          "workflow_dispatch": None}
    assert flow["permissions"] == {"contents": "read"}
    (job,) = flow["jobs"].values()
    assert list(flow["jobs"]) == ["test"]
    assert (job["runs-on"], job["timeout-minutes"]) == ("ubuntu-24.04", 40)
    names = [s["name"] for s in job["steps"]]
    assert names[0] == "Check out"
    assert names[1:] == [*ci_sh_stages(), "stack", "browser"]
    assert ci_sh_stages() == [
        "AGENTS.md byte budget", "workflow rules", "licence headers", "test image",
        "dependency licences", "lint", "tests", "a new install as any user", "corpus scorecard",
        "tools lint", "tools tests", "demo site", "secrets"]
    # only the workflow starts a stack and a browser: on the maintainers' host those are runs of
    # their own
    gate = (HERE / "ci.sh").read_text("utf-8")
    assert "stack_smoke" not in gate and "browser_checks" not in gate
    assert step(job, "stack")["run"] == \
        "TRIPBOX_SMOKE=1 tools/verify/stack_smoke.sh --image tripboxoss:rt"
    assert step(job, "browser")["run"] == \
        "TRIPBOX_SMOKE=1 tools/verify/browser_checks.sh --image tripboxoss:rt"


def python_of(root: Path) -> subprocess.CompletedProcess:
    """ci.sh's lines that read its Python image, run in bash against the Dockerfile in `root`."""
    gate = (HERE / "ci.sh").read_text("utf-8")
    start = gate.index('PYTHON="$(')
    end = gate.index("\n", gate.index("|| {", start))
    script = f'W={root}\n{gate[start:end]}\nprintf "%s" "$PYTHON"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def test_the_gate_reads_its_python_image_from_the_dockerfile(tmp_path):
    """The checks before the build run in the Dockerfile's own base, tag and digest, so a
    Dependabot bump of the Dockerfile reaches them too."""
    gate = (HERE / "ci.sh").read_text("utf-8")
    assert not re.search(r"python:3\.\d+-slim@sha256", gate), "no second copy of the pin"
    (line,) = [ln for ln in (ROOT / "Dockerfile").read_text("utf-8").splitlines()
               if ln.startswith("FROM python:")]
    r = python_of(ROOT)
    assert r.returncode == 0, r.stderr
    assert r.stdout == line.removeprefix("FROM ").removesuffix(" AS runtime")
    assert re.fullmatch(r"python:3\.\d+-slim@sha256:[0-9a-f]{64}", r.stdout)
    two = "FROM python:3.14-slim AS runtime\nFROM python:3.15-slim AS runtime\n"
    for body in ("FROM scratch\n", two):
        (tmp_path / "Dockerfile").write_text(body)
        r = python_of(tmp_path)
        assert r.returncode == 2 and r.stdout == "", body
        assert "the Dockerfile has no one 'FROM python:… AS runtime' line" in r.stderr


def test_the_secret_scan_is_the_same_command_in_both_and_the_image_is_pinned():
    job = load("ci.yml")["jobs"]["test"]
    scan = step(job, "secrets")
    image = scan["env"]["GITLEAKS"]
    assert re.fullmatch(r"ghcr\.io/gitleaks/gitleaks:v[0-9.]+@sha256:[0-9a-f]{64}", image)
    assert scan["run"].splitlines() == [
        'docker pull "$GITLEAKS"',
        'docker run --rm --network none -v "$PWD:/repo:ro" "$GITLEAKS" dir /repo'
        " --config /repo/.gitleaks.toml --redact --no-banner --exit-code 1"]
    gate = (HERE / "ci.sh").read_text("utf-8")
    assert f"GITLEAKS={image}\n" in gate
    assert 'dkr pull "$GITLEAKS"' in gate
    assert ('dkr run -v "$SCANNED:/repo:ro" "$GITLEAKS" dir /repo --config /repo/.gitleaks.toml'
            " --redact --no-banner --exit-code 1") in gate
    assert "git -C \"$W\" ls-files -z --cached --others --exclude-standard" in gate
    assert gate.index("== secrets ==") > gate.index("== demo site ==")
    assert (ROOT / ".gitleaks.toml").is_file()


def test_the_scorecard_is_run_as_any_user_on_a_read_only_root_in_both():
    job = load("ci.yml")["jobs"]["test"]
    assert step(job, "corpus scorecard")["run"] == (
        "docker run --rm --network none --user 12345:54321 --read-only --tmpfs /tmp"
        " tripboxoss:ci python -m tripbox eval --recorded")
    gate = (HERE / "ci.sh").read_text("utf-8")
    assert ('dkr run --user "$ANY_USER" --read-only --tmpfs /tmp "$IMAGE" python -m tripbox eval'
            " --recorded") in gate


# ---------------------------------------------------------------- arm64.yml


ARM64_PATHS = ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "compose.yaml",
               "src/tripbox/parse/**", "tests/corpus/**", "tools/verify/stack_smoke.sh",
               "tools/verify/quickstart_check.py", ".github/workflows/arm64.yml"]


def test_the_arm64_workflow_runs_the_stack_when_what_builds_the_image_changes():
    """On a push to main and on a pull request alike (a Dependabot update of the base image or the
    packages is proven on arm64 before it is merged), and by hand."""
    flow = load("arm64.yml")
    assert flow["on"] == {"push": {"branches": ["main"], "paths": ARM64_PATHS},
                          "pull_request": {"paths": ARM64_PATHS}, "workflow_dispatch": None}
    for path in ("Dockerfile", "pyproject.toml", "uv.lock"):
        assert path in ARM64_PATHS, path
    (job,) = flow["jobs"].values()
    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert step(job, "Stack")["run"] == \
        "TRIPBOX_SMOKE=1 tools/verify/stack_smoke.sh --image tripboxoss:arm64"
    names = [s.get("name") for s in job["steps"]]
    assert names.index("Stack") > names.index("Build runtime image")
    for path in ARM64_PATHS:
        assert list(ROOT.glob(path)), path


# ---------------------------------------------------------------- flightlog.yml


FLIGHTLOG_PATHS = [
    "src/tripbox/write/**", "src/tripbox/trips/backfill.py", "src/tripbox/enrich/**",
    "tests/test_flightlog_*.py", "tests/helpers/e2e.py", "tests/corpus/**",
    "tools/verify/airtrail/**", "tools/verify/airtrail_test_key.py",
    "tools/verify/flightlog_e2e.sh", "Dockerfile", "uv.lock", ".github/workflows/flightlog.yml",
]


def test_the_flight_log_workflow_runs_only_when_its_paths_change():
    flow = load("flightlog.yml")
    assert flow["on"] == {
        "push": {"branches": ["main"], "paths": FLIGHTLOG_PATHS},
        "pull_request": {"paths": FLIGHTLOG_PATHS},
        "workflow_dispatch": None,
    }
    for path in FLIGHTLOG_PATHS:
        assert list(ROOT.glob(path)), path
    # what the run reads is among the paths: its compose file, its tests and their helper
    for read in ("tools/verify/airtrail/compose.yml", "tests/test_flightlog_e2e.py",
                 "tests/test_flightlog_matrix.py", "tests/helpers/e2e.py"):
        assert any(Path(read).match(p) or read.startswith(p.rstrip("*")) for p in FLIGHTLOG_PATHS)
    assert flow["permissions"] == {"contents": "read"}
    assert list(flow["jobs"]) == ["e2e"]
    job = flow["jobs"]["e2e"]
    assert (job["runs-on"], job["timeout-minutes"]) == ("ubuntu-24.04", 20)
    runs = [s["run"] for s in job["steps"] if "run" in s]
    assert runs == ["docker build --target test -t tripboxoss:test .",
                    "TRIPBOX_SMOKE=1 tools/verify/flightlog_e2e.sh"]


# ---------------------------------------------------------------- what may write, what is uploaded

WRITES = {
    ("release.yml", "bundle", "contents"),
    ("release.yml", "build-amd64", "packages"),
    ("release.yml", "build-arm64", "packages"),
    ("release.yml", "join", "packages"),
    ("release.yml", "remove", "packages"),
    ("pages.yml", "deploy", "pages"),
    ("pages.yml", "deploy", "id-token"),
    ("dock-patch.yml", "applies", "issues"),  # its weekly check opens an issue
}


def test_only_the_named_jobs_may_write():
    found = set()
    for name in flows():
        flow = load(name)
        top = flow["permissions"]
        assert isinstance(top, dict) and set(top.values()) <= {"read", "none"}, name
        for job_name, job in flow["jobs"].items():
            perms = job.get("permissions", {})
            assert isinstance(perms, dict), f"{name}: {job_name}: never read-all or write-all"
            for scope, level in perms.items():
                assert level in ("read", "write", "none"), (name, job_name, scope)
                if level == "write":
                    found.add((name, job_name, scope))
    assert found == WRITES
    for name in ("release.yml", "pages.yml"):
        assert load(name)["permissions"] == {}, name
        for job_name, job in load(name)["jobs"].items():
            assert job.get("permissions"), f"{name}: {job_name} names what it needs"


PAGES_ACTIONS = ("actions/configure-pages@", "actions/deploy-pages@",
                 "actions/upload-pages-artifact@")


def test_nothing_is_uploaded_but_the_pages_artifact():
    for name in flows():
        t = text(name)
        assert "actions/upload-artifact" not in t and "actions/cache" not in t, name
        uploads = [line for line in t.splitlines() if "actions/upload-pages-artifact@" in line]
        assert len(uploads) == (name == "pages.yml"), name
        for job_name, job in load(name)["jobs"].items():
            for s in job.get("steps", []):
                run = s.get("run", "")
                if "gh release upload" in run:
                    assert (name, job_name) == ("release.yml", "bundle")
                assert "curl -T" not in run and "--upload-file" not in run, name


def test_pages_is_gated_whole_on_its_variable():
    flow = load("pages.yml")
    assert flow["on"] == {"release": {"types": ["published"]}, "schedule": [{"cron": "41 5 * * 1"}],
                          "workflow_dispatch": None}
    jobs = flow["jobs"]
    assert set(jobs) == {"build", "deploy"}
    assert jobs["build"]["if"] == "vars.TRIPBOX_PAGES == 'true' && !github.event.release.prerelease"
    assert jobs["deploy"]["if"] == (
        "vars.TRIPBOX_PAGES == 'true' && needs.build.outputs.skip != 'true'")
    assert jobs["deploy"]["needs"] == "build"
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["deploy"]["environment"]["name"] == "github-pages"
    uses = {job: [s["uses"].split("@")[0] for s in jobs[job]["steps"] if "uses" in s]
            for job in jobs}
    assert uses == {"build": ["actions/checkout", "actions/upload-pages-artifact"],
                    "deploy": ["actions/configure-pages", "actions/deploy-pages"]}
    assert "enablement" not in text("pages.yml")
    # the site is the demo, exported with no network and checked before it is uploaded
    (site,) = [s for s in jobs["build"]["steps"] if "demo export" in s.get("run", "")]
    assert "--network none" in site["run"] and "tripbox demo export --out /out/site" in site["run"]
    assert site["run"].splitlines()[-1] == \
        'python3 tools/verify/demo_site_check.py "$RUNNER_TEMP/demo/site"'
    upload = next(s for s in jobs["build"]["steps"] if "upload-pages-artifact" in s.get("uses", ""))
    assert upload["with"] == {"path": "${{ runner.temp }}/demo/site"}
    # the weekly run builds the latest full release, under the same gate, never the branch's code
    latest, checkout = jobs["build"]["steps"][:2]
    assert latest["if"] == "github.event_name == 'schedule'"
    assert 'gh api "repos/$REPOSITORY/releases/latest" --jq .tag_name' in latest["run"]
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in latest["run"]
    assert checkout["with"] == {
        "ref": "${{ steps.latest.outputs.ref }}", "persist-credentials": False
    }
    for other in flows():
        if other != "pages.yml":
            assert not any(a in text(other) for a in PAGES_ACTIONS), other


PAGES_GH = """echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "release list") printf '%b' "${STUB_RELEASED:-}" ;;
  *) exit 3 ;;
esac
"""


@pytest.mark.parametrize("released, tag, skip", [
    ("", "v1.0.0", "false"),
    ("v1.0.0\\n", "v1.0.0", "false"),
    ("v1.0.0\\nv1.1.0-rc.1\\n", "v1.0.1", "false"),
    ("v1.0.0\\nv1.1.0\\nv1.0.1\\n", "v1.0.1", "true"),
    ("v1.0.0\\nv2.0.0\\n", "v1.9.9", "true"),
], ids=["first", "listed", "above-a-candidate", "older-line", "older-major"])
def test_a_release_below_the_highest_leaves_the_demo_site_as_it_is(
    tmp_path, released, tag, skip
):
    """The release.yml rule for `latest`: a patch to an older line builds and deploys nothing."""
    jobs = load("pages.yml")["jobs"]
    s = step(jobs["build"], "The highest release")
    assert s["if"] == "github.event_name == 'release'"
    assert s["env"]["TAG"] == "${{ github.event.release.tag_name }}"
    r = run_step(tmp_path, s, stubs(tmp_path, gh=PAGES_GH), GH_TOKEN="sample",
                 REPOSITORY="Good-Work-Dojo/tripbox-rc", TAG=tag, STUB_RELEASED=released)
    assert r.returncode == 0, r.stderr
    assert outputs(tmp_path) == {"skip": skip}
    assert calls(tmp_path) == [
        "gh release list --repo Good-Work-Dojo/tripbox-rc --exclude-drafts --exclude-pre-releases"
        " --limit 1000 --json tagName --jq .[].tagName"]
    assert (f"{tag} is below the highest full release" in r.stdout) == (skip == "true")
    # every later step and the deploy job are skipped with it; the build says why
    names = [x.get("name") for x in jobs["build"]["steps"]]
    for later in names[names.index("The highest release") + 1:]:
        assert step(jobs["build"], later)["if"] == "steps.highest.outputs.skip != 'true'", later
    assert jobs["build"]["outputs"] == {"skip": "${{ steps.highest.outputs.skip }}"}


def test_a_release_tag_of_another_form_stops_the_demo_build(tmp_path):
    s = step(load("pages.yml")["jobs"]["build"], "The highest release")
    r = run_step(tmp_path, s, stubs(tmp_path, gh=PAGES_GH), GH_TOKEN="sample",
                 REPOSITORY="Good-Work-Dojo/tripbox-rc", TAG="v1.0;id")
    assert r.returncode != 0 and "not vX.Y.Z" in r.stderr
    assert (tmp_path / "output").read_text() == "" and calls(tmp_path) == []


# ---------------------------------------------------------------- dependabot

ECOSYSTEMS = {
    "uv": ["/"],
    "docker": ["/", "/tools/verify/screens"],
    "docker-compose": ["/tools/verify/airtrail", "/tools/verify/onehost"],
    "github-actions": ["/"],
}


def holds(ecosystem: str, folder: Path) -> bool:
    """Whether the folder holds a file of the ecosystem's kind."""
    if ecosystem == "uv":
        return (folder / "uv.lock").is_file() and (folder / "pyproject.toml").is_file()
    if ecosystem == "docker":
        return (folder / "Dockerfile").is_file()
    if ecosystem == "docker-compose":
        return any(named_images(f) for f in folder.glob("compose.y*ml"))
    return bool(list((folder / ".github" / "workflows").glob("*.yml")))


def named_images(compose: Path) -> list[str]:
    """The images a compose file names itself, not through a variable."""
    found = re.findall(r"^\s+image:\s*(\S+)\s*$", compose.read_text("utf-8"), re.M)
    return [i for i in found if "${" not in i]


def test_dependabot_watches_the_four_ecosystems_where_their_files_are():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text("utf-8"))
    assert config["version"] == 2
    watched = {}
    for update in config["updates"]:
        eco = update["package-ecosystem"]
        assert eco not in watched, eco
        watched[eco] = update.get("directories") or [update["directory"]]
        assert update["schedule"] == {"interval": "weekly"}, eco
        assert update["groups"] == {"minor-and-patch": {"update-types": ["minor", "patch"]}}, eco
    assert watched == ECOSYSTEMS
    for eco, folders in watched.items():
        for folder in folders:
            assert holds(eco, ROOT / folder.lstrip("/")), (eco, folder)
    # and nowhere else: every Dockerfile, and every compose file that names an image itself
    def tracked(pattern):
        return [p for p in ROOT.rglob(pattern)
                if not any(part.startswith(".") for part in p.relative_to(ROOT).parts)]
    dockerfiles = {"/" + str(p.parent.relative_to(ROOT)).replace(".", "").strip("/")
                   for p in tracked("Dockerfile")}
    assert {d if d != "/" else "/" for d in dockerfiles} == set(ECOSYSTEMS["docker"])
    composes = {"/" + str(p.parent.relative_to(ROOT)) for p in tracked("compose.y*ml")
                if named_images(p)}
    assert composes == set(ECOSYSTEMS["docker-compose"])


def dependabot_ignores(ecosystem: str) -> dict[str, list[str]]:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text("utf-8"))
    (update,) = [u for u in config["updates"] if u["package-ecosystem"] == ecosystem]
    return {i["dependency-name"]: i.get("update-types") for i in update.get("ignore", [])}


def test_dependabot_never_moves_the_python_minor_or_major_version():
    """A new Python is a change by hand (.python-version, ruff's target, the lock's wheels); a
    new digest of the same tag still comes as a routine update."""
    assert dependabot_ignores("docker")["python"] == [
        "version-update:semver-minor", "version-update:semver-major"]
    for name in ("uv", "docker-compose", "github-actions"):
        assert "python" not in dependabot_ignores(name), name
    minor = (ROOT / ".python-version").read_text("utf-8").strip()
    assert re.search(rf"^FROM python:{re.escape(minor)}-slim@sha256:[0-9a-f]{{64}} AS runtime$",
                     (ROOT / "Dockerfile").read_text("utf-8"), re.M)
    target = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["tool"]["ruff"]
    assert target["target-version"] == "py" + minor.replace(".", "")


def test_dependabot_never_moves_the_screenshot_browsers_version():
    """Its version is written in three places (test_screens holds them equal), which one
    Dependabot pull request cannot change together; a new digest of the same tag still comes."""
    assert dependabot_ignores("docker")["*playwright/python"] == [
        "version-update:semver-patch", "version-update:semver-minor",
        "version-update:semver-major"]
    base = (ROOT / "tools/verify/screens/Dockerfile").read_text("utf-8")
    assert "FROM mcr.microsoft.com/playwright/python:v" in base
    pins = (ROOT / "tools/maintainer/README.md").read_text("utf-8")
    pins = pins[pins.index("### Pins Dependabot cannot see"):]
    for place in ("tools/verify/screens/Dockerfile", "tools/verify/screens/constraints.txt",
                  "KITINERARY_VERSION", "KITINERARY_SHA256", "GITLEAKS", "BUILDKIT"):
        assert place in pins, place


# ---------------------------------------------------------------- the release

RELEASE = "release.yml"
JOBS = {"prepare": "ubuntu-24.04", "bundle": "ubuntu-24.04", "build-amd64": "ubuntu-24.04",
        "build-arm64": "ubuntu-24.04-arm", "join": "ubuntu-24.04", "verify-amd64": "ubuntu-24.04",
        "verify-arm64": "ubuntu-24.04-arm", "remove": "ubuntu-24.04"}
SOURCE = "https://github.com/Good-Work-Dojo/tripbox-rc"
REVISION = "3f" * 20
TAG_SHA = "1a" * 20
DISPATCHED = "2b" * 20


def release() -> dict:
    return load(RELEASE)


def test_the_release_runs_when_a_release_is_published_or_by_hand_for_one():
    flow = release()
    assert flow["on"]["release"] == {"types": ["published"]}
    tag = flow["on"]["workflow_dispatch"]["inputs"]["tag"]
    assert (tag["required"], tag["type"]) == (True, "string")
    assert tag["description"] == "an existing release's tag, to publish it again"
    assert set(flow["on"]) == {"release", "workflow_dispatch"}
    assert flow["concurrency"] == {
        "group": "release-${{ github.event.release.tag_name || inputs.tag }}",
        "cancel-in-progress": False}
    assert flow["permissions"] == {}
    assert {n: j["runs-on"] for n, j in flow["jobs"].items()} == JOBS
    for n, job in flow["jobs"].items():
        assert 0 < job["timeout-minutes"] <= 45, n
    needs = {n: j.get("needs") for n, j in flow["jobs"].items()}
    assert needs == {
        "prepare": None, "bundle": "prepare", "build-amd64": "prepare", "build-arm64": "prepare",
        "join": ["prepare", "build-amd64", "build-arm64"], "verify-amd64": ["prepare", "join"],
        "verify-arm64": ["prepare", "join"],
        "remove": ["prepare", "bundle", "build-amd64", "build-arm64", "join", "verify-amd64",
                   "verify-arm64"]}
    assert flow["jobs"]["prepare"]["permissions"] == {"contents": "read"}
    for n in ("verify-amd64", "verify-arm64"):
        assert flow["jobs"][n]["permissions"] == {"contents": "read", "packages": "read"}


def stubs(tmp_path: Path, **scripts: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in scripts.items():
        (bindir / name).write_text("#!/usr/bin/env bash\n" + body)
        (bindir / name).chmod(0o755)
    return bindir


def run_step(tmp_path: Path, s: dict, bindir: Path, cwd: Path = ROOT,
             **env: str) -> subprocess.CompletedProcess:
    """A step's script in bash, as the runner gives it: its environment, a temporary folder, an
    outputs file, a summary file, the stubs first on PATH and the repository as its folder."""
    for f in ("output", "summary", "log"):
        (tmp_path / f).write_text("")
    full = {"PATH": f"{bindir}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "output"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), "STUB_LOG": str(tmp_path / "log"),
            "STUB_DIR": str(tmp_path), **env}
    assert set(s.get("env", {})) <= set(full), "the test gives the step every variable it reads"
    shell = ["bash", "-eo", "pipefail", "-c"] if s.get("shell") == "bash" else ["bash", "-e", "-c"]
    return subprocess.run([*shell, s["run"]], env=full, cwd=cwd, capture_output=True, text=True,
                          timeout=120, check=False)


def outputs(tmp_path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())


def calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "log").read_text().splitlines()


PREPARE_GH = """echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "release view") [ -n "${STUB_NO_RELEASE:-}" ] && { echo "release not found" >&2; exit 1; }
                  echo "$STUB_PRERELEASE" ;;
  "release list") printf '%b' "${STUB_RELEASED:-}" ;;
  *) exit 3 ;;
esac
"""
PREPARE_GIT = """echo "git $*" >> "$STUB_LOG"
case "$1" in
  rev-parse) [ -n "${STUB_NO_TAG:-}" ] && exit 1; echo "$STUB_TAG_SHA" ;;
  show) echo 1893456000 ;;
  checkout) ;;
  *) exit 3 ;;
esac
"""


def first_release_tree(tmp_path: Path) -> Path:
    """The files the release step reads, as they stood at the first release (version 1.0.0),
    whatever the repository's own version now is."""
    tree = tmp_path / "tree"
    (tree / "tools/verify").mkdir(parents=True)
    shutil.copy2(HERE / "release_names.py", tree / "tools/verify/release_names.py")
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    (tree / "pyproject.toml").write_text(
        re.sub(r'^version = "[^"]*"', 'version = "1.0.0"', pyproject, count=1, flags=re.M), "utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")
    head, first = changelog.split("\n## [", 1)[0], changelog[changelog.index("\n## [1.0.0]"):]
    (tree / "CHANGELOG.md").write_text(head + first, "utf-8")
    return tree


def run_prepare(tmp_path: Path, event: str, prerelease: str, tag: str = "v1.0.0-rc.1",
                **stub: str) -> subprocess.CompletedProcess:
    s = next(s for s in release()["jobs"]["prepare"]["steps"] if s.get("id") == "names")
    bindir = stubs(tmp_path, gh=PREPARE_GH, git=PREPARE_GIT)
    return run_step(tmp_path, s, bindir, first_release_tree(tmp_path), EVENT=event,
                    GH_TOKEN="sample", TAG=tag, REPOSITORY="Good-Work-Dojo/tripbox-rc",
                    SOURCE=SOURCE, DISPATCHED=DISPATCHED, STUB_PRERELEASE=prerelease,
                    STUB_TAG_SHA=TAG_SHA, **stub)


@pytest.mark.parametrize("event, prerelease, tag, sha, tags, rehearsal", [
    ("release", "true", "v1.0.0-rc.1", TAG_SHA, "1.0.0-rc.1", "true"),
    ("release", "false", "v1.0.0", TAG_SHA, "1.0.0 1.0 1 latest", "false"),
    ("workflow_dispatch", "true", "v1.0.0-rc.1", DISPATCHED, "1.0.0-rc.1", "true"),
    ("workflow_dispatch", "false", "v1.0.0", TAG_SHA, "1.0.0 1.0 1 latest", "false"),
], ids=["a-published-prerelease", "a-published-release", "by-hand-a-prerelease",
        "by-hand-a-release"])
def test_a_dispatched_rehearsal_builds_the_commit_it_was_dispatched_on_and_a_release_its_tag(
    tmp_path, event, prerelease, tag, sha, tags, rehearsal
):
    r = run_prepare(tmp_path, event, prerelease, tag, STUB_RELEASED="v0.9.0\\n")
    assert r.returncode == 0, r.stderr
    got = outputs(tmp_path)
    assert got == {"sha": sha, "created": "1893456000",
                   "image": "ghcr.io/good-work-dojo/tripbox-rc", "version": tag[1:],
                   "rehearsal": rehearsal, "tags": tags}
    log = calls(tmp_path)
    assert f"gh release view {tag} --repo Good-Work-Dojo/tripbox-rc --json isPrerelease --jq" \
        " .isPrerelease" in log
    assert f"git checkout -q --detach {sha}" in log
    assert (f"git rev-parse --verify --quiet refs/tags/{tag}^{{commit}}" in log) == (sha == TAG_SHA)
    # every later job builds and labels what this job read
    jobs = release()["jobs"]
    for name in ("bundle", "build-amd64", "build-arm64", "verify-amd64", "verify-arm64"):
        (checkout,) = [s for s in jobs[name]["steps"]
                       if s.get("uses", "").startswith("actions/checkout@")]
        assert checkout["with"]["ref"] == "${{ needs.prepare.outputs.sha }}", name
    assert "github.sha" not in "".join(str(jobs[n]) for n in jobs if n != "prepare")


@pytest.mark.parametrize("stub, tag, words", [
    ({"STUB_NO_RELEASE": "1"}, "v1.0.0", "there is no release v1.0.0"),
    ({"STUB_NO_TAG": "1"}, "v1.0.0", "names no commit"),
    ({}, "v1.0.1", "release: the tag v1.0.1 is not the package's version"),
    ({}, "v1.0", "not of the form"),
    ({}, "v1.0.0;id", "not of the form"),
    ({}, "v9.0.0-rc.1", "is not the package's version"),
], ids=["no-release", "no-tag", "another-version", "another-form", "a-command",
        "a-candidate-of-another"])
def test_a_release_that_cannot_be_built_stops_before_anything_is_pushed(tmp_path, stub, tag, words):
    r = run_prepare(tmp_path, "release", "false", tag, **stub)
    assert r.returncode != 0 and words in r.stderr
    assert "sha=" not in (tmp_path / "output").read_text()


def test_minor_major_and_latest_stay_on_the_highest_line(tmp_path):
    r = run_prepare(tmp_path, "release", "false", "v1.0.0", STUB_RELEASED="v1.0.0\\nv1.2.0\\n")
    assert r.returncode == 0, r.stderr
    assert outputs(tmp_path)["tags"] == "1.0.0"


BUNDLE_GH = """echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "release upload") cp "$4" "$STUB_DIR/uploaded" ;;
  "release view") printf '%s' "${STUB_BODY:-}" ;;
  "release edit") cp "$7" "$STUB_DIR/notes" ;;
  *) exit 3 ;;
esac
"""


@pytest.mark.parametrize("body, edited", [("", True), ("  \\n", True), ("Written by hand.", False)])
def test_the_release_uploads_the_bundle_the_quick_start_fetches(tmp_path, body, edited):
    sys.path.insert(0, str(HERE))
    import readme_blocks  # noqa: PLC0415

    quickstart = readme_blocks.blocks((ROOT / "README.md").read_text("utf-8"))["quickstart"]
    first = quickstart.splitlines()[0]
    url = re.search(r"https://\S+", first).group(0)
    asset = url.rsplit("/", 1)[1]
    assert url.endswith("/releases/latest/download/" + asset) and asset == "tripbox.tar.gz"
    (job,) = [release()["jobs"]["bundle"]]
    assert job["permissions"] == {"contents": "write"}
    build, attach = [s for s in job["steps"] if "run" in s]
    # the bundle is built with no token in reach; only the step that calls gh holds one
    assert "env" not in build and "gh " not in build["run"]
    assert build["run"] == \
        f'python3 tools/maintainer/install_bundle.py --out "$RUNNER_TEMP/{asset}"'
    assert attach["env"]["GH_TOKEN"] == "${{ github.token }}"
    bindir = stubs(tmp_path, gh=BUNDLE_GH)
    r = run_step(tmp_path, build, bindir)
    assert r.returncode == 0, r.stderr
    r = run_step(tmp_path, attach, bindir, GH_TOKEN="sample",
                 REPOSITORY="Good-Work-Dojo/tripbox-rc", TAG="v1.0.0-rc.1",
                 STUB_BODY=body.replace("\\n", "\n"))
    assert r.returncode == 0, r.stderr
    log = calls(tmp_path)
    assert log[0] == (f"gh release upload v1.0.0-rc.1 {tmp_path}/{asset} --repo"
                      " Good-Work-Dojo/tripbox-rc --clobber")
    import tarfile  # noqa: PLC0415

    with tarfile.open(tmp_path / "uploaded") as tar:
        assert "tripbox/compose.yaml" in tar.getnames()
    assert any(c.startswith("gh release edit") for c in log) == edited
    if edited:
        assert (tmp_path / "notes").read_text().startswith("The first release.")


BUILD_DOCKER = """printf '%s\\n' "$@" >"$STUB_ARGS"
prev=
for a in "$@"; do
  if [ "$prev" = --metadata-file ]; then printf '%s' "$STUB_METADATA" >"$a"; fi
  prev="$a"
done
"""


def build_step(job: str) -> dict:
    return next(s for s in release()["jobs"][job]["steps"] if s.get("id") == "build")


@pytest.mark.parametrize("job, platform", [("build-amd64", "linux/amd64"),
                                           ("build-arm64", "linux/arm64")])
def test_the_builds_push_by_digest_with_no_attestation(tmp_path, job, platform):
    s = build_step(job)
    assert s["env"]["PLATFORM"] == platform
    assert build_step("build-amd64")["run"] == build_step("build-arm64")["run"], "one script"
    good = "sha256:" + "ef" * 32
    bindir = stubs(tmp_path, docker=BUILD_DOCKER)
    image = "ghcr.io/good-work-dojo/tripbox-rc"
    r = run_step(tmp_path, s, bindir, PLATFORM=platform, IMAGE=image, VERSION="1.0.0-rc.1",
                 SOURCE=SOURCE, REVISION=REVISION, CREATED="1893456000",
                 STUB_ARGS=str(tmp_path / "args"),
                 STUB_METADATA=f'{{"containerimage.digest": "{good}"}}')
    assert r.returncode == 0, r.stderr
    sys.path.insert(0, str(HERE))
    import release_names as rn  # noqa: PLC0415

    meta = rn.build_arguments(rn.labels(SOURCE, "1.0.0-rc.1", REVISION, "1893456000"))
    assert (tmp_path / "args").read_text().splitlines() == [
        "buildx", "build", "--target", "runtime", "--platform", platform, "--provenance=false",
        "--sbom=false", *meta, "--output",
        f"type=image,name={image},push-by-digest=true,name-canonical=true,push=true",
        "--metadata-file", str(tmp_path / "build.json"), "."]
    assert outputs(tmp_path) == {"digest": good}
    # a metadata file with no digest fails the step and gives none
    r = run_step(tmp_path, s, bindir, PLATFORM=platform, IMAGE=image, VERSION="1.0.0-rc.1",
                 SOURCE=SOURCE, REVISION=REVISION, CREATED="1893456000",
                 STUB_ARGS=str(tmp_path / "args"), STUB_METADATA="{}")
    assert r.returncode != 0 and "no image digest" in r.stderr and outputs(tmp_path) == {}
    # the builder is BuildKit pinned by digest, started before the build
    steps = release()["jobs"][job]["steps"]
    (builder,) = [x for x in steps if "docker buildx create" in x.get("run", "")]
    assert re.fullmatch(r"moby/buildkit@sha256:[0-9a-f]{64}", builder["env"]["BUILDKIT"])
    assert steps.index(builder) < steps.index(s)
    assert re.search(r"BUILDKIT: moby/buildkit@sha256:[0-9a-f]{64} # v0\.34\.2\n", text(RELEASE))
    assert "QEMU" not in text(RELEASE).upper() and "setup-buildx" not in text(RELEASE)
    assert release()["jobs"][job]["outputs"] == {"digest": "${{ steps.build.outputs.digest }}"}


JOIN_DOCKER = """printf '%s\\n' "$*" >> "$STUB_LOG"
case "$*" in
  "buildx imagetools inspect "*)
    printf '{"mediaType": "index", "digest": "%s", "size": 1}' "$STUB_INDEX" ;;
esac
"""


def test_the_join_tags_the_two_digests_and_reads_the_index_back(tmp_path):
    s = next(s for s in release()["jobs"]["join"]["steps"] if s.get("id") == "join")
    amd, arm, index = ("sha256:" + c * 64 for c in "abc")
    bindir = stubs(tmp_path, docker=JOIN_DOCKER)
    image = "ghcr.io/good-work-dojo/tripbox-rc"
    env = dict(IMAGE=image, VERSION="1.0.0", TAGS="1.0.0 1.0 1 latest", SOURCE=SOURCE,
               REVISION=REVISION, AMD64=amd, ARM64=arm, STUB_INDEX=index)
    r = run_step(tmp_path, s, bindir, **env)
    assert r.returncode == 0, r.stderr
    create, inspect = calls(tmp_path)
    for t in ("1.0.0", "1.0", "1", "latest"):
        assert f"--tag {image}:{t} " in create
    assert create.endswith(f"{image}@{amd} {image}@{arm}")
    assert "index:org.opencontainers.image.licenses=AGPL-3.0-only" in create
    assert f"index:org.opencontainers.image.revision={REVISION}" in create
    assert inspect.startswith(f"buildx imagetools inspect {image}:1.0.0 ")
    assert outputs(tmp_path) == {"digest": index}
    r = run_step(tmp_path, s, bindir, **{**env, "ARM64": ""})
    assert r.returncode != 0 and "gave no digest" in r.stderr and calls(tmp_path) == []
    r = run_step(tmp_path, s, bindir, **{**env, "STUB_INDEX": "sha256:short"})
    assert r.returncode != 0 and outputs(tmp_path) == {}


@pytest.mark.parametrize("job, arch", [("verify-amd64", "amd64"), ("verify-arm64", "arm64")])
def test_each_architecture_is_pulled_back_and_proven_on_its_own_runner(job, arch):
    steps = release()["jobs"][job]["steps"]
    named = {s.get("name"): s for s in steps}
    image = named["The image, its architecture and its labels"]
    assert image["env"]["ARCH"] == arch
    assert image["run"].splitlines()[1] == 'docker pull "$IMAGE:$VERSION"'
    for key in ("org.opencontainers.image.version", "org.opencontainers.image.revision",
                "org.opencontainers.image.licenses", "AGPL-3.0-only"):
        assert key in image["run"]
    assert named["Corpus scorecard"]["shell"] == "bash"
    assert named["Corpus scorecard"]["run"].splitlines()[-1] == (
        "tail -n 1 \"$RUNNER_TEMP/scorecard.txt\""
        " | grep -Eqx 'eval: ([0-9]+) of \\1 emails right'"
    )
    assert named["Stack"]["run"] == \
        'TRIPBOX_SMOKE=1 tools/verify/stack_smoke.sh --image "$IMAGE:$VERSION"'


def test_every_registry_job_signs_in_with_the_token_on_standard_input_and_signs_out_at_the_end():
    jobs = release()["jobs"]
    for name in ("build-amd64", "build-arm64", "join", "verify-amd64", "verify-arm64"):
        steps = jobs[name]["steps"]
        (login,) = [s for s in steps if "docker login" in s.get("run", "")]
        assert login["env"] == {"REGISTRY_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
        assert login["run"].splitlines()[-1] == (
            'printf \'%s\' "$REGISTRY_TOKEN" | docker login ghcr.io --username "$GITHUB_ACTOR"'
            " --password-stdin")
        assert steps[-1] == {"name": "Sign out of the registry", "if": "always()",
                             "run": "docker logout ghcr.io"}
    t = text(RELEASE)
    assert t.count("secrets.GITHUB_TOKEN") == 5 and "--password " not in t and " -p " not in t


# ------------------------------------------------------------ a rehearsal removes what it pushed

AMD64, ARM64, INDEX = ("sha256:" + c * 64 for c in "abc")
OTHER1, OTHER2 = ("sha256:" + c * 64 for c in "de")
FIVE = f"101 {AMD64}\n102 {OTHER1}\n103 {ARM64}\n104 {INDEX}\n105 {OTHER2}\n"
PACKAGES_GH = r"""printf '%s\n' "$*" >> "$STUB_LOG"
deleted="$STUB_DIR/deleted"
touch "$deleted"
if [ "$1 $2" = "api --paginate" ]; then
  if [ -e "$STUB_DIR/package-gone" ] || [ -n "${STUB_NOT_FOUND:-}" ]; then
    echo "gh: Not Found (HTTP 404)" >&2; exit 1
  fi
  while read -r id name; do grep -qx "$id" "$deleted" || echo "$id $name"; done < "$STUB_VERSIONS"
  exit 0
fi
if [ "$1 $2 $3" = "api -X DELETE" ]; then
  id="${4##*/}"
  if [ -n "${STUB_REFUSE:-}" ] && [ "$id" = "$STUB_REFUSE" ]; then
    echo "gh: Forbidden (HTTP 403)" >&2; exit 1
  fi
  case "$4" in
    */versions/*) echo "$id" >> "$deleted" ;;
    *) if [ -n "${STUB_REFUSE_PACKAGE:-}" ]; then
         echo "gh: You cannot delete this package (HTTP $STUB_REFUSE_PACKAGE)" >&2; exit 1
       fi
       touch "$STUB_DIR/package-gone" ;;
  esac
  exit 0
fi
exit 3
"""
PACKAGE = "/orgs/Good-Work-Dojo/packages/container/tripbox-rc"
REMOVED_3 = "release: removed 3 package versions; 0 of this run's are left"


def remove_step() -> dict:
    (s,) = release()["jobs"]["remove"]["steps"]
    return s


def run_remove(tmp_path: Path, versions: str = FIVE, **env: str) -> subprocess.CompletedProcess:
    (tmp_path / "versions").write_text(versions)
    bindir = stubs(tmp_path, gh=PACKAGES_GH)
    full = dict(GH_TOKEN="sample", REHEARSAL="true", IMAGE="ghcr.io/good-work-dojo/tripbox-rc",
                OWNER="Good-Work-Dojo", OWNER_TYPE="Organization", AMD64=AMD64, ARM64=ARM64,
                INDEX=INDEX, AMD64_RESULT="success", ARM64_RESULT="success",
                JOIN_RESULT="success", STUB_VERSIONS=str(tmp_path / "versions"))
    full.update(env)
    return run_step(tmp_path, remove_step(), bindir, **full)


def deletes(tmp_path: Path) -> list[str]:
    return [c for c in calls(tmp_path) if c.startswith("api -X DELETE")]


def test_a_rehearsal_removes_only_the_digests_it_pushed(tmp_path):
    r = run_remove(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines() == [REMOVED_3]
    assert deletes(tmp_path) == [f"api -X DELETE {PACKAGE}/versions/{i}" for i in (101, 103, 104)]
    lists = [c for c in calls(tmp_path) if c.startswith("api --paginate")]
    listing = f'api --paginate {PACKAGE}/versions?per_page=100 --jq .[] | "\\(.id) \\(.name)"'
    assert lists == [listing] * 2
    # a build that pushed nothing gives no digest, and nothing is removed for it
    # a build that never ran pushed nothing, and nothing is removed for it
    second = tmp_path / "second"
    second.mkdir()
    r = run_remove(second, ARM64="", ARM64_RESULT="skipped")
    assert r.returncode == 0 and "removed 2 package versions" in r.stdout
    assert [c.rsplit("/", 1)[1] for c in deletes(second)] == ["101", "104"]


def test_a_user_owned_package_is_found_under_the_user(tmp_path):
    owner = "Sample-User"
    r = run_remove(tmp_path, IMAGE=f"ghcr.io/{owner.lower()}/tripbox", OWNER=owner,
                   OWNER_TYPE="User")
    assert r.returncode == 0, r.stdout + r.stderr
    under = "/".join(("", "users", owner, "packages", "container", "tripbox", "versions", ""))
    assert deletes(tmp_path)[0].startswith("api -X DELETE " + under)


ONLY_OURS = f"201 {AMD64}\n202 {ARM64}\n203 {INDEX}\n"


def test_a_package_holding_only_this_runs_versions_is_removed_whole(tmp_path):
    r = run_remove(tmp_path, versions=ONLY_OURS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines() == [REMOVED_3]
    assert deletes(tmp_path) == [f"api -X DELETE {PACKAGE}"]


@pytest.mark.parametrize("refusal", ["400", "403", "404"])
def test_a_refused_removal_of_the_whole_package_removes_each_version_instead(tmp_path, refusal):
    r = run_remove(tmp_path, versions=ONLY_OURS, STUB_REFUSE_PACKAGE=refusal)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines() == [
        f"release: the package could not be removed whole (HTTP {refusal}); removing its"
        " versions one by one", REMOVED_3]
    assert deletes(tmp_path) == [f"api -X DELETE {PACKAGE}"] + [
        f"api -X DELETE {PACKAGE}/versions/{i}" for i in (201, 202, 203)]
    assert len([c for c in calls(tmp_path) if c.startswith("api --paginate")]) == 2


def test_a_package_the_token_cannot_see_is_a_failure_never_nothing_left(tmp_path):
    r = run_remove(tmp_path, STUB_NOT_FOUND="1")
    assert r.returncode == 1
    assert r.stdout.splitlines() == ["release: the package's versions could not be read (HTTP 404)"]
    assert deletes(tmp_path) == []


def test_a_digest_the_package_does_not_list_fails_before_anything_is_deleted(tmp_path):
    r = run_remove(tmp_path, versions=f"101 {AMD64}\n102 {OTHER1}\n104 {INDEX}\n")
    assert r.returncode == 1
    assert r.stdout.splitlines() == [
        f"release: could not find {ARM64} among the package's versions"]
    assert deletes(tmp_path) == []


@pytest.mark.parametrize("job, unset", [("build-amd64", "AMD64"), ("build-arm64", "ARM64"),
                                        ("join", "INDEX")])
@pytest.mark.parametrize("result", ["failure", "cancelled", "success"])
def test_a_job_that_ran_and_gave_no_digest_is_never_counted_as_nothing_left(tmp_path, job, unset,
                                                                            result):
    results = {"build-amd64": "AMD64_RESULT", "build-arm64": "ARM64_RESULT", "join": "JOIN_RESULT"}
    r = run_remove(tmp_path, **{unset: "", results[job]: result})
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert lines[0] == f"release: could not account for what {job} pushed: it gave no digest"
    assert lines[-1].startswith("release: removed 2 package versions;")
    assert "0 of this run's are left" not in r.stdout
    # what it can account for is still removed
    assert len(deletes(tmp_path)) == 2


def test_a_run_that_pushed_nothing_removes_nothing_and_asks_nothing(tmp_path):
    r = run_remove(tmp_path, AMD64="", ARM64="", INDEX="", AMD64_RESULT="skipped",
                   ARM64_RESULT="skipped", JOIN_RESULT="skipped")
    assert r.returncode == 0
    assert r.stdout.splitlines() == [
        "release: removed 0 package versions; 0 of this run's are left"]
    assert calls(tmp_path) == []


@pytest.mark.parametrize("value", ["false", "", "True", "yes"])
def test_a_full_release_removes_nothing(tmp_path, value):
    assert release()["jobs"]["remove"]["if"] == \
        "always() && needs.prepare.outputs.rehearsal == 'true'"
    r = run_remove(tmp_path, REHEARSAL=value)
    assert r.returncode == 1 and "nothing is removed" in r.stderr
    assert calls(tmp_path) == []


def test_a_failed_removal_names_the_digest_and_fails_the_job(tmp_path):
    r = run_remove(tmp_path, STUB_REFUSE="103")
    assert r.returncode == 1
    assert r.stdout.splitlines() == [f"release: could not remove {ARM64} (HTTP 403)"]
    assert deletes(tmp_path) == [f"api -X DELETE {PACKAGE}/versions/{i}" for i in (101, 103, 104)]
    # an image that is not the owner's package is refused before anything is asked
    r = run_remove(tmp_path, IMAGE="ghcr.io/someone-else/tripbox-rc")
    assert r.returncode == 1 and calls(tmp_path) == []
    r = run_remove(tmp_path, OWNER_TYPE="Bot")
    assert r.returncode == 1 and calls(tmp_path) == []
