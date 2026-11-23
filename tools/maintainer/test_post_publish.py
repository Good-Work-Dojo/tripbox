# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tools/maintainer/post-publish.sh, run against a stub `gh`, `git`, `docker` and `curl` on PATH that
log every call to a file and answer from canned JSON; the publish gate is the real one, scanning what
the stub `docker save` writes. Each test runs the script from a copy of the files it reads, in its
own temporary directory, so a test can change ci.yml or CHANGELOG.md. Nothing reaches GitHub or a
registry; every name here is invented."""

# ruff: noqa: E501

from __future__ import annotations

import json
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "tools/maintainer/post-publish.sh"
REPO = "Example-Org/example-app"
HEAD = "3f" * 20
SITE = "https://example-org.github.io/example-app/"
BANNER = "Demo: invented trips. Read-only."
IMAGE = "ghcr.io/example-org/example-app"
ACTIONS_APP = 15368
TOKEN = "Quillfeatherzanthe"  # an invented denylist token
TREE_FILES = (SCRIPT, "tools/verify/release_names.py", "tools/publish_gate.py", "CHANGELOG.md", "pyproject.toml",
              ".github/workflows/ci.yml")

SITE_REPO = "Example-Org/example-site"
SITE_TIP = "a1" * 20
SITE_BASE = "b2" * 20
SKIP = "[CF-Pages-Skip]"
MERGE_MESSAGE = f"{SKIP} Merge origin/product/tripbox"
SITE_PUSH = "push origin product/tripbox:product/tripbox"
SITE_BASE_FILE = "origin/main:site/assets/analytics.js"
SITE_MUTATIONS = ("push", "checkout", "merge ", "branch", "rebase", "reset", "commit")
ANC_BUNDLE = "merge-base --is-ancestor refs/remotes/origin/product/tripbox refs/tripbox-bundle/tip"
ANC_LOCAL = "merge-base --is-ancestor refs/remotes/origin/product/tripbox refs/heads/product/tripbox"
BUNDLE_IN_LOCAL = "merge-base --is-ancestor refs/tripbox-bundle/tip refs/heads/product/tripbox"
LOCAL_IN_BUNDLE = "merge-base --is-ancestor refs/heads/product/tripbox refs/tripbox-bundle/tip"
MERGE_TREE = "merge-tree --write-tree refs/tripbox-bundle/tip refs/remotes/origin/product/tripbox"
MERGE = f"merge --no-ff -m {MERGE_MESSAGE} refs/remotes/origin/product/tripbox"

FAKE_GH = r'''
import json, os, sys
argv = sys.argv[1:]
scenario = json.load(open(os.environ["FAKE_SCENARIO"]))
entry = {"tool": "gh", "argv": argv}


def log():
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(entry) + "\n")


def answer(key):
    """a key's answer; a list is answered in order, its last item from then on"""
    ans = scenario["answers"][key]
    if not isinstance(ans, list):
        return ans
    seen_path = os.environ["FAKE_LOG"] + ".seen"
    seen = json.load(open(seen_path)) if os.path.exists(seen_path) else {}
    n = seen.get(key, 0)
    seen[key] = n + 1
    json.dump(seen, open(seen_path, "w"))
    return ans[min(n, len(ans) - 1)]


if argv[:2] == ["auth", "status"]:
    log()
    # as gh prints it: the account, and a token line, on both streams
    print("Logged in to github.com account sample-maintainer", file=sys.stderr)
    print(f"  - Token: {os.environ.get('GH_TOKEN', '')}")
    print(f"  - Token: {os.environ.get('GH_TOKEN', '')}", file=sys.stderr)
    sys.exit(0)
if argv[:2] == ["pr", "list"]:
    log()
    print(scenario.get("pr_merged" if "merged" in argv else "pr_list", ""))
    sys.exit(0)
if argv[:2] == ["pr", "create"]:
    entry.update(method="CREATE", endpoint="pr create")
    log()
    if scenario.get("pr_create_fails"):
        print("gh: could not create the pull request", file=sys.stderr)
        sys.exit(1)
    print("https://github.com/example-org/example-site/pull/1")
    sys.exit(0)
if argv[:2] == ["release", "create"]:
    entry.update(method="CREATE", endpoint="release " + argv[2],
                 notes=open(argv[argv.index("--notes-file") + 1]).read())
    log()
    sys.exit(0)
if argv[:1] == ["api"]:
    method, body, endpoint, i = None, None, None, 1
    while i < len(argv):
        a = argv[i]
        if a == "-X":
            method, i = argv[i + 1], i + 2
        elif a == "--input":
            body, i = json.load(open(argv[i + 1])), i + 2
        elif a.startswith("-"):
            i += 1
        else:
            endpoint, i = endpoint or a, i + 1
    method = method or ("POST" if body is not None else "GET")
    entry.update(method=method, endpoint=endpoint, body=body)
    log()
    key = f"{method} {endpoint}"
    if key not in scenario["answers"]:
        if method == "GET":
            print(f"stub gh: no answer for {key}", file=sys.stderr)
            sys.exit(3)
        sys.exit(0)
    ans = answer(key)
    if ans.get("status", 200) >= 400:
        print(f"gh: {ans.get('message', 'Not Found')} (HTTP {ans['status']})", file=sys.stderr)
        sys.exit(1)
    if "body" in ans:
        print(json.dumps(ans["body"]))
    sys.exit(0)
print("stub gh: unexpected call", file=sys.stderr)
sys.exit(3)
'''

FAKE_GIT = r'''
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": "git", "argv": argv}) + "\n")
whole = json.load(open(os.environ["FAKE_SCENARIO"]))
site = whole.get("site_git")
if site is not None and argv[:1] == ["-C"] and argv[1] == site["dir"]:
    ans = site["answers"].get(" ".join(argv[2:]))
    if ans is None:
        print("stub git: unexpected call", file=sys.stderr)
        sys.exit(3)
    print(ans.get("out", ""), end="")
    if ans.get("err"):
        print(ans["err"], file=sys.stderr)
    sys.exit(ans.get("rc", 0))
scenario = whole.get("git", {})
if argv[2:] == ["rev-parse", "HEAD"]:
    print(scenario.get("head", ""))
elif argv[2:] == ["status", "--porcelain"]:
    print(scenario.get("status", ""), end="")
else:
    print("stub git: unexpected call", file=sys.stderr)
    sys.exit(3)
'''

FAKE_DOCKER = r'''
import io, json, os, sys, tarfile
argv = sys.argv[1:]
scenario = json.load(open(os.environ["FAKE_SCENARIO"])).get("docker", {})
cfg = os.environ.get("DOCKER_CONFIG")
entry = {"tool": "docker", "argv": argv, "docker_config": cfg, "docker_host": os.environ.get("DOCKER_HOST"),
         "config_entries": sorted(os.listdir(cfg)) if cfg and os.path.isdir(cfg) else None}
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\n")
if argv[:2] == ["context", "inspect"]:
    print(scenario.get("context_host", ""))
    sys.exit(0)
if argv[0] == "pull":
    if scenario.get("pull", 0):
        print(scenario.get("pull_error", "Error response from daemon: denied"), file=sys.stderr)
    sys.exit(scenario.get("pull", 0))
if argv[:2] == ["manifest", "inspect"]:
    print(json.dumps({"manifests": [{"platform": {"os": "linux", "architecture": a}}
                                    for a in scenario.get("arches", ["amd64", "arm64"])]
                      + [{"platform": {"os": "unknown", "architecture": "unknown"}}]}))
elif argv[0] == "save":
    data = json.dumps([{"Config": "config.json", "Layers": []}]).encode()
    with tarfile.open(argv[argv.index("-o") + 1], "w") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
elif argv[0] == "inspect":
    print(json.dumps([{"Config": {"Env": ["PATH=/usr/bin"], "Labels": {"org.opencontainers.image.title": "app"}}}]))
elif argv[0] == "history":
    print(scenario.get("history", "IMAGE CREATED CREATED BY SIZE"))
'''

FAKE_CURL = r'''
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": "curl", "argv": argv}) + "\n")
ans = json.load(open(os.environ["FAKE_SCENARIO"])).get("curl", {}).get(argv[-1], {"code": 404, "body": ""})
with open(argv[argv.index("-o") + 1], "w") as f:
    f.write(ans["body"])
print(ans["code"], end="")
'''

PAGE = ('<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src \'self\'">'
        f'<title>Demo</title></head><body><p class="demo-banner">{BANNER}</p></body></html>')

R = f"repos/{REPO}"
ENV = f"{R}/environments/github-pages"
FIRST_RUN_CHANGES = [
    f"PUT {R}/vulnerability-alerts", f"PUT {R}/automated-security-fixes", f"PATCH {R}",
    f"PUT {R}/private-vulnerability-reporting", f"PATCH {R}/code-scanning/default-setup", f"POST {R}/rulesets",
    f"POST {R}/actions/variables", f"POST {R}/pages", f"POST {ENV}/deployment-branch-policies",
    f"POST {R}/actions/workflows/pages.yml/dispatches", "CREATE release v1.0.0",
]


def ci_contexts(tree: Path) -> list[str]:
    jobs = yaml.safe_load((tree / ".github/workflows/ci.yml").read_text("utf-8"))["jobs"]
    return [job.get("name") or job_id for job_id, job in jobs.items()]


def run_of(event: str, conclusion: str = "success", status: str = "completed", run_id: int = 41) -> dict:
    return {"workflow_runs": [{"id": run_id, "status": status, "conclusion": conclusion, "event": event,
                               "head_sha": HEAD, "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}]}


def wanted_ruleset(contexts: list[str]) -> dict:
    """the ruleset as GitHub returns it once it holds what the script asks for"""
    return {"id": 7, "name": "default-branch", "target": "branch", "source_type": "Repository", "source": REPO,
            "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}},
            "rules": [{"type": "required_status_checks", "parameters": {
                          "strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
                          "required_status_checks": [{"context": c, "integration_id": ACTIONS_APP}
                                                     for c in reversed(contexts)]}},
                      {"type": "non_fast_forward"}, {"type": "deletion"}]}


def released(tag: str, **kw) -> dict:
    """a release as GitHub lists it: published, a full release unless `kw` says"""
    return {"tag_name": tag, "draft": False, "prerelease": False, **kw}


def scenario(tree: Path, done: bool = False, tag: str = "v1.0.0", visibility: str = "public") -> dict:
    """the answers of a repository just made public (`done=False`) or of one this script already finished"""
    on = "enabled" if done else "disabled"
    pages = {"body": {"build_type": "workflow", "html_url": SITE}}
    answers = {
        f"GET {R}": {"body": {"visibility": visibility, "permissions": {"admin": True}, "default_branch": "main",
                              "full_name": REPO, "fork": False,
                              "owner": {"type": "Organization"},
                              "security_and_analysis": {"secret_scanning": {"status": on},
                                                        "secret_scanning_push_protection": {"status": on}}}},
        f"GET {R}/branches/main": {"body": {"commit": {"sha": HEAD}}},
        f"GET {R}/vulnerability-alerts": {} if done else {"status": 404},
        f"GET {R}/automated-security-fixes": {"body": {"enabled": True, "paused": False}} if done else {"status": 404},
        f"GET {R}/private-vulnerability-reporting": {"body": {"enabled": done}},
        f"GET {R}/code-scanning/default-setup": {"body": {"state": "configured" if done else "not-configured"}},
        f"GET {R}/rulesets?includes_parents=false&per_page=100": {"body": [
            {"id": 7, "name": "default-branch", "source_type": "Repository"}] if done else []},
        f"GET {R}/rulesets/7": {"body": wanted_ruleset(ci_contexts(tree))},
        f"GET {R}/actions/variables/TRIPBOX_PAGES": {"body": {"name": "TRIPBOX_PAGES", "value": "true"}}
        if done else {"status": 404},
        f"GET {R}/pages": pages if done else [{"status": 404}, pages],
        f"GET {ENV}": {"body": {"name": "github-pages", "deployment_branch_policy": {
            "protected_branches": False, "custom_branch_policies": True}}},
        f"GET {ENV}/deployment-branch-policies?per_page=100": {"body": {
            "total_count": 2 if done else 1,
            "branch_policies": [{"id": 1, "name": "main", "type": "branch"}]
            + ([{"id": 2, "name": "v*", "type": "tag"}] if done else [])}},
        f"GET {R}/actions/workflows/pages.yml/runs?event=workflow_dispatch&head_sha={HEAD}&per_page=20":
            {"body": run_of("workflow_dispatch", run_id=40)} if done
            else [{"body": {"workflow_runs": []}}, {"body": run_of("workflow_dispatch", run_id=40)}],
        f"GET {R}/releases/tags/{tag}": {"body": {"tag_name": tag}} if done else {"status": 404},
        f"GET {R}/releases?per_page=100": {"body": [released(tag)] if done else []},
        f"GET {R}/git/ref/tags/{tag}": {"status": 404},
        f"GET {R}/commits/{tag}": {"body": {"sha": HEAD}},
        f"GET {R}/actions/workflows/release.yml/runs?event=release&head_sha={HEAD}&per_page=20":
            {"body": run_of("release")},
    }
    return {"answers": answers, "docker": {}, "curl": {SITE: {"code": 200, "body": PAGE}},
            "git": {"head": HEAD, "status": ""}}


def site_answers(bundle: Path) -> dict:
    """what a clone of the site repository answers when the tripbox page is ready to go: the remote has
    the branch, which the local branch already contains, so nothing is merged"""
    b = str(bundle)
    return {
        "rev-parse --git-dir": {"out": ".git\n"},
        "remote get-url --all origin": {"out": f"https://github.com/{SITE_REPO}.git\n"},
        "remote get-url --push --all origin": {"out": f"https://github.com/{SITE_REPO}.git\n"},
        "status --porcelain": {"out": ""},
        f"bundle verify {b}": {},
        f"bundle list-heads {b} refs/heads/product/tripbox": {"out": f"{SITE_TIP} refs/heads/product/tripbox\n"},
        "fetch origin": {},
        f"cat-file -e {SITE_BASE_FILE}": {},
        "update-ref -d refs/tripbox-bundle/tip": {},
        f"fetch {b} product/tripbox:refs/tripbox-bundle/tip": {},
        "rev-parse --verify -q refs/heads/product/tripbox": {"rc": 1},
        "checkout -b product/tripbox refs/tripbox-bundle/tip": {},
        "rev-parse --verify -q refs/remotes/origin/product/tripbox": {"out": SITE_BASE + "\n"},
        ANC_BUNDLE: {},
        ANC_LOCAL: {},
        "rev-parse refs/heads/product/tripbox": {"out": SITE_TIP + "\n"},
        "log -1 --format=%s refs/tripbox-bundle/tip": {"out": f"{SKIP} Add the sections\n"},
        "log -1 --format=%s refs/heads/product/tripbox": {"out": f"{SKIP} Add the sections\n"},
        SITE_PUSH: {},
    }


class Rig:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.tree = tmp / "tree"
        for rel in TREE_FILES:
            (self.tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.tree / rel)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in (("gh", FAKE_GH), ("git", FAKE_GIT), ("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            p = self.bin / name
            p.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
            p.chmod(0o755)
        self.denylist = tmp / "denylist"
        self.denylist.mkdir()
        (self.denylist / "manual.tsv").write_text(f"name\t{TOKEN}\n", encoding="utf-8")
        self.log = tmp / "calls.jsonl"
        self.scenario_file = tmp / "scenario.json"
        (tmp / "home").mkdir()
        (tmp / "tmp").mkdir()
        self.scenario = scenario(self.tree)
        self.site = tmp / "site"
        (self.site / "tools").mkdir(parents=True)
        (self.site / "tools/check_site.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['FAKE_LOG'], 'a') as f:\n"
            "    f.write(json.dumps({'tool': 'check_site', 'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
            "sys.exit(1 if os.path.exists('fail-check') else 0)\n", encoding="utf-8")
        self.bundle = tmp / "site.bundle"
        self.bundle.write_bytes(b"invented bundle")
        self.site_git = site_answers(self.bundle)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        scenario_now = dict(self.scenario)
        scenario_now["site_git"] = {"dir": str(self.site), "answers": self.site_git}
        self.scenario_file.write_text(json.dumps(scenario_now), encoding="utf-8")
        for p in (self.log, Path(str(self.log) + ".seen")):
            p.unlink(missing_ok=True)
        env = {"PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(self.tmp / "home"),
               "TMPDIR": str(self.tmp / "tmp"), "FAKE_LOG": str(self.log), "FAKE_SCENARIO": str(self.scenario_file),
               "GH_TOKEN": "invented-gh-token-value"}
        return subprocess.run([str(self.tree / SCRIPT), *args], env=env, capture_output=True, text=True,
                              timeout=120, check=False)

    def publish(self, *args: str) -> subprocess.CompletedProcess:
        return self.run("--repo", REPO, "--denylist", str(self.denylist), *args)

    def publish_site(self, *args: str) -> subprocess.CompletedProcess:
        return self.publish("--site", str(self.site), "--site-bundle", str(self.bundle), "--site-repo", SITE_REPO,
                            *args)

    def site_calls(self) -> list[str]:
        """the git commands run in the site clone, without the `-C DIR`"""
        return [" ".join(c["argv"][2:]) for c in self.calls()
                if c["tool"] == "git" and c["argv"][:2] == ["-C", str(self.site)]]

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]

    def changes(self) -> list[str]:
        """every gh call that changes the repository, as `METHOD endpoint`"""
        return [f"{c['method']} {c['endpoint']}" for c in self.calls()
                if c["tool"] == "gh" and c.get("method") not in (None, "GET")]

    def tools(self, tool: str, verb: str | None = None) -> list[dict]:
        return [c for c in self.calls() if c["tool"] == tool and (verb is None or c["argv"][0] == verb)]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_the_script_is_executable_bash_and_names_no_private_path_or_token():
    p = ROOT / SCRIPT
    text = p.read_text("utf-8")
    assert p.stat().st_mode & stat.S_IXUSR
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
    for word in ("/home/", "GH_TOKEN", "GITHUB_TOKEN", "--force", "push -f", "+refs", "--mirror", "set -x"):
        assert word not in text, word


def test_the_default_repository_is_the_public_one_and_the_variable_is_tripbox_pages(rig):
    text = (rig.tree / SCRIPT).read_text("utf-8")
    assert 'REPO="Good-Work-Dojo/tripbox"' in text and 'VARIABLE="TRIPBOX_PAGES"' in text
    r = rig.run("--skip-gate", "--dry-run")  # no --repo: the default
    assert r.returncode == 1, "the stub answers for the example repository only"
    assert [c["endpoint"] for c in rig.calls() if c["tool"] == "gh" and "endpoint" in c] == [
        "repos/Good-Work-Dojo/tripbox"]
    assert rig.publish("--dry-run").returncode == 0
    assert f"{R}/actions/variables/TRIPBOX_PAGES" in [c.get("endpoint") for c in rig.calls()]


def test_a_private_repository_stops_the_script_before_any_change(rig):
    rig.scenario = scenario(rig.tree, visibility="private")
    r = rig.publish()
    assert r.returncode == 1
    assert f"post-publish: {REPO} is private; make it public first (nothing was changed)" in r.stderr
    assert rig.changes() == [] and rig.tools("docker") == [] and rig.tools("curl") == []
    assert [c.get("endpoint") for c in rig.calls() if c["tool"] == "gh"] == [None, R]


def test_the_steps_run_in_the_owners_order(rig):
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert rig.changes() == FIRST_RUN_CHANGES
    order = [c["tool"] if c["tool"] != "gh" else f"{c.get('method', 'auth')} {c.get('endpoint', '')}"
             for c in rig.calls()]
    assert order[:3] == ["auth ", f"GET {R}", f"GET {R}/branches/main"]
    at = {name: order.index(name) for name in (
        "GET " + R + "/vulnerability-alerts", "POST " + R + "/rulesets", "POST " + R + "/actions/variables",
        "POST " + R + "/pages", "POST " + R + "/actions/workflows/pages.yml/dispatches", "CREATE release v1.0.0")}
    assert list(at.values()) == sorted(at.values())
    first_docker, first_curl = order.index("docker"), order.index("curl")
    assert at["CREATE release v1.0.0"] < first_docker < first_curl
    pulls = [c["argv"][-1] for c in rig.tools("docker", "pull")]
    assert pulls == [f"{IMAGE}:1.0.0"]
    assert [c["argv"][-1] for c in rig.tools("docker", "save")] == pulls, "the gate scans the pulled image"
    assert "findings: 0 (accepted: 0)" in r.stdout
    assert [c["argv"][-1] for c in rig.tools("curl")] == [SITE]
    assert "invented-gh-token" not in r.stdout + r.stderr


def test_git_is_only_read_and_the_checkout_must_be_the_head_and_clean(rig):
    assert rig.publish().returncode == 0
    assert [c["argv"][2:] for c in rig.tools("git")] == [["rev-parse", "HEAD"], ["status", "--porcelain"]]
    rig.scenario["git"]["head"] = "1" * 40
    r = rig.publish()
    assert r.returncode == 1 and "this checkout is at" in r.stderr and "nothing was changed" in r.stderr
    assert rig.changes() == [] and rig.tools("docker") == []
    rig.scenario["git"] = {"head": HEAD, "status": " M CHANGELOG.md\n"}
    r = rig.publish()
    assert r.returncode == 1 and "uncommitted changes" in r.stderr and rig.changes() == []


def test_an_expected_commit_must_be_the_remote_head_and_the_checkout(rig):
    assert rig.publish("--expect-sha", HEAD[:12]).returncode == 0
    rig.scenario["git"]["head"] = "2" * 40
    r = rig.publish("--expect-sha", "2" * 12)
    assert r.returncode == 1 and rig.changes() == [], "the remote head is not the expected commit"


def test_a_dry_run_changes_nothing_and_prints_each_change(rig):
    r = rig.publish("--dry-run")
    assert r.returncode == 0, r.stderr
    assert rig.changes() == []
    assert rig.tools("docker") == [] and rig.tools("curl") == []
    assert {c["method"] for c in rig.calls() if c["tool"] == "gh" and "method" in c} == {"GET"}
    for shown in (f"would run: gh api -X PUT {R}/vulnerability-alerts", "would run: gh release create v1.0.0",
                  f"would run: gh api -X POST {R}/rulesets", f"docker pull -q {IMAGE}:1.0.0",
                  f"docker manifest inspect {IMAGE}:1.0.0", "tools/publish_gate.py images",
                  f"would run: gh api -X POST {R}/actions/variables", f"would run: gh api -X POST {R}/pages"):
        assert shown in r.stdout, shown
    assert "nothing was changed" in r.stdout


def test_a_second_run_changes_nothing_already_as_wanted(rig):
    rig.scenario = scenario(rig.tree, done=True)
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert rig.changes() == []
    for said in ("Dependabot alerts: already on", "ruleset default-branch: already as wanted",
                 "TRIPBOX_PAGES: already true", "release v1.0.0: already published"):
        assert said in r.stdout, said
    assert len(rig.tools("docker", "pull")) == 1 and len(rig.tools("curl")) == 1


def test_the_release_targets_the_expected_commit(rig):
    r = rig.publish("--expect-sha", HEAD[:12])
    assert r.returncode == 0, r.stderr
    [create] = [c for c in rig.calls() if c.get("method") == "CREATE"]
    argv = create["argv"]
    assert argv[:3] == ["release", "create", "v1.0.0"] and argv[argv.index("--target") + 1] == HEAD
    assert "--prerelease" not in argv and argv[argv.index("--repo") + 1] == REPO


def test_a_head_that_is_not_the_expected_commit_stops_it(rig):
    r = rig.publish("--expect-sha", "0" * 40)
    assert r.returncode == 1 and f"is {HEAD}, not {'0' * 40}" in r.stderr and "nothing was changed" in r.stderr
    assert rig.changes() == []


def set_version(rig: Rig, version: str) -> None:
    path = rig.tree / "pyproject.toml"
    path.write_text(path.read_text("utf-8").replace('version = "1.0.0"', f'version = "{version}"', 1), "utf-8")


def test_the_release_is_the_changelogs_first_versioned_section_with_its_notes(rig):
    (rig.tree / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- Not yet.\n\n## [2.3.4] - 2026-01-01\n\n### Added\n- An invented thing.\n"
        "\n## [2.3.3] - 2025-12-01\n\n- Older.\n\n[2.3.4]: https://example.net/2.3.4\n", encoding="utf-8")
    set_version(rig, "2.3.4")
    rig.scenario = scenario(rig.tree, tag="v2.3.4")
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    [create] = [c for c in rig.calls() if c.get("method") == "CREATE"]
    assert create["notes"] == "### Added\n- An invented thing.\n"
    assert [c["argv"][-1] for c in rig.tools("docker", "pull")] == [f"{IMAGE}:2.3.4"]
    assert rig.publish("--version", "v2.3.3").returncode == 1, "a version pyproject.toml does not hold"


def test_a_prerelease_is_refused_before_anything_changes(rig):
    (rig.tree / "CHANGELOG.md").write_text("# Changelog\n\n## [1.0.0-rc.1]\n\n- A candidate.\n", encoding="utf-8")
    set_version(rig, "1.0.0")
    for args in ((), ("--version", "v1.0.0-rc.1"), ("--version", "v1.0.0-beta")):
        r = rig.publish(*args)
        assert r.returncode == 1, args
        assert "release candidates are proven by release.yml alone" in r.stderr and "nothing was changed" in r.stderr
        assert rig.changes() == [] and rig.tools("docker") == [] and rig.tools("curl") == []


def test_the_pull_uses_no_sign_in(rig):
    assert rig.publish().returncode == 0
    for c in rig.tools("docker", "pull") + rig.tools("docker", "manifest"):
        assert c["docker_config"] and c["config_entries"] == [], c
        assert not c["docker_config"].startswith(str(rig.tmp / "home"))
    for c in rig.tools("docker", "pull"):
        assert c["argv"][-1] == f"{IMAGE}:1.0.0"


def test_a_failed_pull_prints_the_packages_visibility_page(rig):
    rig.scenario["docker"] = {"pull": 1}
    r = rig.publish()
    assert r.returncode == 1
    assert "https://github.com/orgs/Example-Org/packages/container/example-app/settings" in r.stderr
    assert rig.tools("docker", "save") == [] and rig.tools("curl") == []


def test_a_pull_the_daemon_cannot_make_says_so_and_names_no_package_setting(rig):
    rig.scenario["docker"] = {"pull": 1, "pull_error": "Cannot connect to the Docker daemon at"
                              " unix:///var/run/docker.sock. Is the docker daemon running?"}
    r = rig.publish()
    assert r.returncode == 1
    assert "check that docker runs and reaches ghcr.io" in r.stderr
    assert "Danger zone" not in r.stderr and "/settings" not in r.stderr


def test_the_anonymous_pull_keeps_the_active_contexts_daemon(rig):
    rig.scenario["docker"] = {"context_host": "unix:///tmp/sample-colima/docker.sock"}
    assert rig.publish().returncode == 0
    anonymous = rig.tools("docker", "pull") + rig.tools("docker", "manifest")
    assert anonymous and all(
        c["docker_host"] == "unix:///tmp/sample-colima/docker.sock" and c["config_entries"] == []
        for c in anonymous
    )


def test_an_image_without_both_architectures_stops(rig):
    rig.scenario["docker"] = {"arches": ["amd64"]}
    r = rig.publish()
    assert r.returncode == 1 and "holds no linux/arm64 image" in r.stderr
    assert rig.tools("curl") == []


@pytest.mark.parametrize("args", [("--denylist", "/nonexistent/denylist"),
                                  ("--skip-gate", "--denylist", "/nonexistent/denylist")])
def test_a_denylist_that_is_no_folder_or_contradicts_the_skip_is_refused_before_any_call(rig, args):
    r = rig.run("--repo", REPO, *args)
    assert r.returncode == 2 and r.stderr.startswith("post-publish: ")
    assert rig.calls() == []


def test_the_first_release_needs_the_gate_or_its_skip_by_name(rig):
    """With no full release published but this one, the scan is the launch's: the run stops
    once step 1 has read that, before anything changes."""
    rig.scenario["answers"][f"GET {R}/releases?per_page=100"] = {"body": [
        released("v1.0.0-rc.1", prerelease=True), released("v0.9.0", draft=True)]}
    r = rig.run("--repo", REPO)
    assert r.returncode == 2
    assert "v1.0.0 is the first release, whose image the publish gate scans: give --denylist DIR" in r.stderr
    assert "nothing was changed" in r.stderr
    assert write_only_calls(rig) == []


def later_release(rig: Rig, tag: str, earlier: tuple[str, ...]) -> None:
    """The tree and the answers of a release cut after `earlier` were published."""
    version = tag[1:]
    (rig.tree / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{version}] - 2027-03-01\n\n### Fixed\n- An invented fix.\n\n"
        "## [1.0.0] - 2026-11-22\n\n- The first.\n", encoding="utf-8")
    set_version(rig, version)
    rig.scenario = scenario(rig.tree, done=True, tag=tag)
    for key in (f"GET {R}/releases/tags/{tag}",):
        rig.scenario["answers"][key] = {"status": 404}
    rig.scenario["answers"][f"GET {R}/releases?per_page=100"] = {
        "body": [released(e) for e in earlier]}


def test_a_later_release_needs_no_denylist_and_repeats_no_launch_step(rig):
    later_release(rig, "v1.0.1", ("v1.0.0",))
    r = rig.run("--repo", REPO)
    assert r.returncode == 0, r.stderr
    assert rig.changes() == ["CREATE release v1.0.1"]
    [create] = [c for c in rig.calls() if c.get("method") == "CREATE"]
    assert "--latest=false" not in create["argv"], "the highest release is GitHub's latest"
    assert len(rig.tools("docker", "pull")) == 1 and rig.tools("docker", "save") == []
    assert "release v1.0.1: after 1 earlier; the highest full release is v1.0.1" in r.stdout
    assert "publish gate: skipped (no --denylist given; v1.0.1 is not the first release)" in r.stdout
    assert "no --site given: the site's page went live with the first release; nothing to do" in r.stdout
    for launch_only in ("gh pr create", SITE_BASE_FILE, "CLA Assistant"):
        assert launch_only not in r.stdout + r.stderr, launch_only
    assert rig.site_calls() == []
    # a denylist given is still used
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert len(rig.tools("docker", "save")) == 1 and "findings: 0 (accepted: 0)" in r.stdout


def test_a_patch_to_an_older_line_is_not_made_the_latest_release(rig):
    later_release(rig, "v1.0.1", ("v1.0.0", "v1.1.0"))
    r = rig.run("--repo", REPO, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "release v1.0.1: after 2 earlier; the highest full release is v1.1.0" in r.stdout
    assert "would run: gh release create v1.0.1" in r.stdout and "--latest=false" in r.stdout
    r = rig.run("--repo", REPO)
    assert r.returncode == 0, r.stderr
    [create] = [c for c in rig.calls() if c.get("method") == "CREATE"]
    assert create["argv"][-1] == "--latest=false"


def test_the_first_release_still_prints_the_launch_steps(rig):
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert "release v1.0.0: the first release" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1].startswith("by hand, once: install the CLA Assistant")
    [create] = [c for c in rig.calls() if c.get("method") == "CREATE"]
    assert "--latest=false" not in create["argv"]


def test_skipping_the_gate_still_pulls_but_scans_nothing(rig):
    r = rig.run("--repo", REPO, "--skip-gate")
    assert r.returncode == 0, r.stderr
    assert len(rig.tools("docker", "pull")) == 1 and rig.tools("docker", "save") == []
    assert "publish gate: skipped (--skip-gate)" in r.stdout


MEMBER_RULES = ["--upstream", "*", "--own", "app/*", "--upstream", "app/.venv/*",
                "--own", "app/.venv/lib/python*/site-packages/tripbox-*",
                "--own", "app/.venv/lib/python*/site-packages/_editable_impl_tripbox.pth",
                "--own", "app/.venv/bin/tripbox"]


@pytest.mark.parametrize("dry", [False, True])
def test_the_gate_counts_only_the_projects_own_files_by_rules_in_order(rig, dry):
    r = rig.publish(*(("--dry-run",) if dry else ()))
    assert r.returncode == 0, r.stderr
    prefix = "would run: " if dry else "+ "
    [line] = [line for line in r.stdout.splitlines()
              if line.startswith(prefix) and "publish_gate.py images" in line]
    argv = shlex.split(line[len(prefix):])
    rules = [(i, a) for i, a in enumerate(argv) if a in ("--upstream", "--own")]
    assert [x for i, _ in rules for x in argv[i:i + 2]] == MEMBER_RULES
    start = rules[0][0]
    assert argv[start:start + len(MEMBER_RULES)] == MEMBER_RULES, "the rules stay together, in order"
    assert argv[-1] == f"{IMAGE}:1.0.0" and argv[argv.index("--app-path") + 1] == "/app"
    if not dry:
        assert "upstream: 0 findings in 0 members (public upstream bytes, not counted)" in r.stdout


def test_a_gate_finding_stops_the_script_and_never_prints_the_token(rig):
    rig.scenario["docker"] = {"history": f"IMAGE CREATED CREATED BY\nabc 1 day ago RUN echo {TOKEN}"}
    r = rig.publish()
    assert r.returncode == 1
    assert "make the package private" in r.stderr
    assert TOKEN.lower() not in (r.stdout + r.stderr).lower()
    assert rig.tools("curl") == []


def test_no_token_is_printed(rig):
    """gh's own sign-in check prints the token it holds; the script shows none of what it says."""
    for args in ((), ("--dry-run",)):
        r = rig.publish(*args)
        assert r.returncode == 0, r.stderr
        assert "invented-gh-token" not in r.stdout + r.stderr


def test_the_ruleset_requires_the_jobs_of_ci_yml_by_their_names(rig):
    (rig.tree / ".github/workflows/ci.yml").write_text(
        "name: ci\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-24.04\n    steps:\n      - name: not a check\n"
        "        run: 'true'\n  build:\n    name: Build it\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: 'true'\n", encoding="utf-8")
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    [body] = [c["body"] for c in rig.calls() if c.get("endpoint") == f"{R}/rulesets" and c["method"] == "POST"]
    checks = body["rules"][2]["parameters"]["required_status_checks"]
    assert [c["context"] for c in checks] == ["lint", "Build it"] == ci_contexts(rig.tree)
    assert {c["integration_id"] for c in checks} == {ACTIONS_APP}
    assert [rule["type"] for rule in body["rules"]] == ["deletion", "non_fast_forward", "required_status_checks"]
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]


def test_the_shipped_ci_yml_gives_its_own_job(rig):
    assert rig.publish().returncode == 0
    [body] = [c["body"] for c in rig.calls() if c.get("endpoint") == f"{R}/rulesets"]
    checks = [c["context"] for c in body["rules"][2]["parameters"]["required_status_checks"]]
    assert checks == ci_contexts(rig.tree) and checks


def test_a_ruleset_of_the_same_name_that_differs_is_updated_in_place(rig):
    rig.scenario["answers"][f"GET {R}/rulesets?includes_parents=false&per_page=100"] = {
        "body": [{"id": 9, "name": "default-branch", "source_type": "Repository"},
                 {"id": 3, "name": "default-branch", "source_type": "Organization"}]}
    rig.scenario["answers"][f"GET {R}/rulesets/9"] = {"body": wanted_ruleset(["an-old-job"])}
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert f"PUT {R}/rulesets/9" in rig.changes() and f"POST {R}/rulesets" not in rig.changes()


def environment_changes(rig: Rig) -> list[tuple[str, dict | None]]:
    return [(f"{c['method']} {c['endpoint']}", c["body"]) for c in rig.calls()
            if c["tool"] == "gh" and c.get("method") not in (None, "GET") and c["endpoint"].startswith(ENV)]


def test_the_environment_lets_release_tags_deploy(rig):
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert environment_changes(rig) == [(f"POST {ENV}/deployment-branch-policies", {"name": "v*", "type": "tag"})]
    rig.scenario = scenario(rig.tree, done=True)
    assert rig.publish().returncode == 0 and environment_changes(rig) == []


@pytest.mark.parametrize("environment", [{"status": 404}, {"body": {"name": "github-pages", "deployment_branch_policy": {
    "protected_branches": True, "custom_branch_policies": False}}}], ids=["absent", "protected-branches-only"])
def test_an_environment_limited_to_protected_branches_moves_to_chosen_refs(rig, environment):
    rig.scenario["answers"][f"GET {ENV}"] = environment
    del rig.scenario["answers"][f"GET {ENV}/deployment-branch-policies?per_page=100"]
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert environment_changes(rig) == [
        (f"PUT {ENV}", {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}),
        (f"POST {ENV}/deployment-branch-policies", {"name": "main", "type": "branch"}),
        (f"POST {ENV}/deployment-branch-policies", {"name": "v*", "type": "tag"})]


def test_a_failed_release_run_stops_before_the_image(rig):
    rig.scenario["answers"][f"GET {R}/actions/workflows/release.yml/runs?event=release&head_sha={HEAD}&per_page=20"] = {
        "body": run_of("release", conclusion="failure")}
    r = rig.publish()
    assert r.returncode == 1 and "release.yml's run ended 'failure'" in r.stderr
    assert f"https://github.com/{REPO}/actions/runs/41" in r.stderr
    assert rig.tools("docker") == []


@pytest.mark.parametrize("page, said", [
    ("<!doctype html><title>Demo</title>" + f'<p class="demo-banner">{BANNER}</p>', "content-security policy"),
    (PAGE.replace(BANNER, "Welcome"), "banner"),
], ids=["no-policy", "no-banner"])
def test_a_demo_page_without_its_banner_or_policy_fails_the_check(rig, page, said):
    rig.scenario["curl"][SITE] = {"code": 200, "body": page}
    r = rig.publish()
    assert r.returncode == 1 and said in r.stderr


def test_a_demo_that_does_not_answer_200_fails_the_check(rig):
    rig.scenario["curl"][SITE] = {"code": 404, "body": ""}
    r = rig.publish()
    assert r.returncode == 1 and "answered 404" in r.stderr


def test_the_script_names_the_base_site_file_and_never_the_one_the_gate_denies():
    text = (ROOT / SCRIPT).read_text("utf-8")
    assert "site/assets/analytics.js" in text and SKIP in text and "product/tripbox" in text
    assert "posthog" not in text.lower()


def test_the_maintainer_readme_names_every_flag_and_the_default_repository():
    readme = (ROOT / "tools/maintainer/README.md").read_text("utf-8")
    for word in ("--repo", "--version", "--expect-sha", "--denylist", "--skip-gate", "--export-names",
                 "--timeout", "--dry-run", "--site", "--site-bundle", "--site-repo", "--json", "gh auth status", "Good-Work-Dojo/tripbox",
                 "gh release create"):
        assert word in readme, word
    assert "/home/" not in readme


def write_only_calls(rig: Rig) -> list[str]:
    return rig.changes() + [f"{c['tool']} {c['argv'][0]}" for c in rig.calls() if c["tool"] in ("docker", "curl")]


def test_a_check_that_needs_no_change_fails_before_the_first_change(rig):
    """each of these stops the run with the stub gh holding no write call at all"""
    cases = {}

    def case(name, setup, said, *args):
        cases[name] = (setup, said, args)

    # pyproject.toml holds 1.0.0
    case("version", lambda r: None, "v2.0.0 is not the version in pyproject.toml", "--version", "v2.0.0")
    case("notes", lambda r: (r.tree / "CHANGELOG.md").write_text("# Changelog\n\n## [0.9.0]\n\n- Older.\n"),
         "CHANGELOG.md has no notes for v1.0.0", "--version", "v1.0.0")
    case("jobs", lambda r: (r.tree / ".github/workflows/ci.yml").write_text("name: ci\non: [push]\n"),
         "the job of .github/workflows/ci.yml could not be read")

    def release_elsewhere(r):
        r.scenario["answers"][f"GET {R}/releases/tags/v1.0.0"] = {"body": {"tag_name": "v1.0.0"}}
        r.scenario["answers"][f"GET {R}/commits/v1.0.0"] = {"body": {"sha": "9" * 40}}

    def orphan_tag(r):
        r.scenario["answers"][f"GET {R}/git/ref/tags/v1.0.0"] = {"body": {"object": {"sha": "9" * 40}}}
        r.scenario["answers"][f"GET {R}/commits/v1.0.0"] = {"body": {"sha": "9" * 40}}

    elsewhere = "the tag v1.0.0 already exists on another commit than the head"
    case("release on another commit", release_elsewhere, elsewhere)
    case("tag with no release on another commit", orphan_tag, elsewhere)
    for name, (setup, said, args) in cases.items():
        fresh = Rig(rig.tmp / name.replace(" ", "-"))
        setup(fresh)
        r = fresh.publish(*args)
        assert r.returncode == 1, (name, r.stderr)
        assert said in r.stderr and "nothing was changed" in r.stderr, (name, r.stderr)
        assert write_only_calls(fresh) == [], name


def test_a_moved_repository_a_fork_an_odd_branch_or_a_prerelease_stops_before_any_change(rig):
    cases = {
        "moved": ({"full_name": "Example-Org/renamed-app"}, "is now Example-Org/renamed-app on GitHub"),
        "fork": ({"fork": True}, "is a fork"),
        "branch": ({"default_branch": 'main"}, {"x'}, "could not be read as a branch name"),
    }
    for name, (repo, said) in cases.items():
        fresh = Rig(rig.tmp / name)
        fresh.scenario["answers"][f"GET {R}"]["body"].update(repo)
        r = fresh.publish()
        assert r.returncode == 1 and said in r.stderr and "nothing was changed" in r.stderr, name
        assert write_only_calls(fresh) == [], name
    fresh = Rig(rig.tmp / "prerelease")
    fresh.scenario["answers"][f"GET {R}/releases/tags/v1.0.0"] = {"body": {"tag_name": "v1.0.0", "prerelease": True}}
    fresh.scenario["answers"][f"GET {R}/commits/v1.0.0"] = {"body": {"sha": HEAD}}
    r = fresh.publish()
    assert r.returncode == 1 and "marked a prerelease on GitHub" in r.stderr
    assert write_only_calls(fresh) == []


def test_a_rerun_after_a_failed_pages_run_waits_for_the_run_it_asked_for(rig):
    """The failed run stays the newest until GitHub lists the one just asked for; it is never
    taken for it."""
    old = run_of("workflow_dispatch", conclusion="failure", run_id=39)
    runs = f"GET {R}/actions/workflows/pages.yml/runs?event=workflow_dispatch&head_sha={HEAD}&per_page=20"
    rig.scenario["answers"][runs] = [{"body": old}, {"body": old}, {"body": run_of("workflow_dispatch", run_id=40)}]
    (rig.bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (rig.bin / "sleep").chmod(0o755)
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert f"POST {R}/actions/workflows/pages.yml/dispatches" in rig.changes()
    assert "pages.yml: run 40 succeeded" in r.stdout and "ended 'failure'" not in r.stderr


def test_a_read_that_fails_but_for_a_404_stops_the_script_where_it_stands(rig):
    rig.scenario["answers"][f"GET {R}/private-vulnerability-reporting"] = {"status": 502, "message": "Bad Gateway"}
    r = rig.publish()
    assert r.returncode == 1
    assert f"reading repos/{R[len('repos/'):]}/private-vulnerability-reporting failed" in r.stderr
    assert "Bad Gateway (HTTP 502)" in r.stderr
    # the switches before it are made; nothing after it is
    assert f"PUT {R}/vulnerability-alerts" in rig.changes()
    assert not [c for c in rig.changes() if "rulesets" in c or "release" in c]


def test_a_tag_on_the_head_with_no_release_is_published(rig):
    rig.scenario["answers"][f"GET {R}/git/ref/tags/v1.0.0"] = {"body": {"object": {"sha": HEAD}}}
    rig.scenario["answers"][f"GET {R}/commits/v1.0.0"] = {"body": {"sha": HEAD}}
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    assert "CREATE release v1.0.0" in rig.changes()


def test_a_rerun_after_a_failed_write_completes_without_repeating_done_changes(tmp_path):
    first = Rig(tmp_path / "first")
    first.scenario["answers"][f"POST {R}/rulesets"] = {"status": 422, "message": "Validation Failed"}
    r = first.publish()
    assert r.returncode == 1 and "the steps already done are skipped" in r.stderr
    done_before = first.changes()
    assert f"POST {R}/rulesets" in done_before and f"POST {R}/actions/variables" not in done_before
    assert "CREATE release v1.0.0" not in done_before and first.tools("docker") == []
    second = Rig(tmp_path / "second")
    # the state the first run left: step 2 made, nothing after it
    on = scenario(second.tree, done=True)["answers"]
    for key in (f"GET {R}/vulnerability-alerts", f"GET {R}/automated-security-fixes",
                f"GET {R}/private-vulnerability-reporting", f"GET {R}/code-scanning/default-setup", f"GET {R}"):
        second.scenario["answers"][key] = on[key]
    r = second.publish()
    assert r.returncode == 0, r.stderr
    assert second.changes() == [c for c in FIRST_RUN_CHANGES if c not in (
        f"PUT {R}/vulnerability-alerts", f"PUT {R}/automated-security-fixes", f"PATCH {R}",
        f"PUT {R}/private-vulnerability-reporting", f"PATCH {R}/code-scanning/default-setup")]


# ---- the site step: the tripbox page's branch pushed and its pull request opened ----

def pr_creates(rig: Rig) -> list[dict]:
    return [c for c in rig.calls() if c["tool"] == "gh" and c.get("endpoint") == "pr create"]


def test_without_a_site_the_step_prints_what_the_owner_runs_by_hand(rig):
    r = rig.publish()
    assert r.returncode == 0, r.stderr
    for shown in (f"git cat-file -e {SITE_BASE_FILE}", "product/tripbox", "gh pr create", SKIP):
        assert shown in r.stdout, shown
    assert rig.site_calls() == [] and pr_creates(rig) == []
    assert [c["argv"][2:] for c in rig.tools("git")] == [["rev-parse", "HEAD"], ["status", "--porcelain"]]


def test_the_site_flags_come_together(rig):
    for args in (("--site", str(rig.site)), ("--site-bundle", str(rig.bundle))):
        r = rig.publish(*args)
        assert r.returncode == 2 and "--site and --site-bundle" in r.stderr, args
        assert rig.calls() == [], "a usage error is made before any call"


def test_the_site_step_pushes_the_branch_and_opens_the_pull_request_after_the_demo_check(rig):
    r = rig.publish_site()
    assert r.returncode == 0, r.stderr
    got = rig.site_calls()
    assert got[got.index("fetch origin"):] == [
        "fetch origin", f"cat-file -e {SITE_BASE_FILE}", f"bundle verify {rig.bundle}",
        f"bundle list-heads {rig.bundle} refs/heads/product/tripbox", "update-ref -d refs/tripbox-bundle/tip",
        f"fetch {rig.bundle} product/tripbox:refs/tripbox-bundle/tip", "rev-parse --verify -q refs/heads/product/tripbox",
        "log -1 --format=%s refs/tripbox-bundle/tip", "rev-parse --verify -q refs/remotes/origin/product/tripbox",
        ANC_BUNDLE, "rev-parse --verify -q refs/heads/product/tripbox",
        "checkout -b product/tripbox refs/tripbox-bundle/tip", "rev-parse refs/heads/product/tripbox", SITE_PUSH], got
    assert not any(c.startswith(("merge ", "rebase", "reset")) for c in got)
    [check] = [c for c in rig.calls() if c["tool"] == "check_site"]
    assert check["argv"] == ["--no-provisional"] and check["cwd"] == str(rig.site.resolve())
    [create] = pr_creates(rig)
    argv = create["argv"]
    assert argv[argv.index("--base") + 1] == "main" and argv[argv.index("--head") + 1] == "product/tripbox"
    assert argv[argv.index("--repo") + 1] == SITE_REPO and "--force" not in argv
    assert argv[:2] == ["pr", "create"]
    order = [c["tool"] for c in rig.calls()]
    assert order.index("curl") < max(i for i, t in enumerate(order) if t == "git")
    assert order.index("check_site") < max(i for i, t in enumerate(order) if t == "git")
    assert "pull request" in r.stdout and "never merges" not in r.stderr


def test_every_check_the_site_step_needs_runs_before_the_first_change(rig):
    """`early` ones stop before the clone is fetched; `late` ones after, and each leaves the stub gh holding no
    write call and the clone's branch, merge and push untouched"""
    cases = {}

    def case(name, setup, said, late=False):
        cases[name] = (setup, said, late)

    def answer(key, **kw):
        return lambda r: r.site_git.__setitem__(key, kw)

    case("not a directory", lambda r: shutil.rmtree(r.site), "is not a directory")
    case("not a clone", lambda r: r.site_git.__setitem__("rev-parse --git-dir", {"rc": 128}),
         "is not a git clone")
    case("another remote", answer("remote get-url --all origin", out="https://github.com/Example-Org/another.git\n"),
         f"is not {SITE_REPO}")
    case("remote of a lookalike",
         answer("remote get-url --all origin", out=f"https://github.com/Evil-{SITE_REPO}.git\n"), f"is not {SITE_REPO}")
    case("a second fetch address", answer("remote get-url --all origin", out=(
        f"https://github.com/{SITE_REPO}.git\nhttps://github.com/Example-Org/another.git\n")), f"is not {SITE_REPO}")
    case("a push address elsewhere", answer("remote get-url --push --all origin", out=(
        f"git@github.com:{SITE_REPO}.git\ngit@github.com:Example-Org/another.git\n")), "a push address")
    case("a push address on another host",
         answer("remote get-url --push --all origin", out=f"https://example.net/{SITE_REPO}.git\n"), "a push address")
    case("dirty", answer("status --porcelain", out=" M site/tripbox.html\n"), "uncommitted changes")
    case("no checker", lambda r: (r.site / "tools/check_site.py").unlink(), "tools/check_site.py")
    case("no bundle", lambda r: r.bundle.unlink(), "is not a file")
    case("base site missing", answer(f"cat-file -e {SITE_BASE_FILE}", rc=1),
         "the base site is not on main; tell the owner", late=True)
    case("bundle fails verify", lambda r: r.site_git.__setitem__(
        f"bundle verify {r.bundle}", {"rc": 1, "err": "error: the bundle is bad"}), "is not a valid bundle", late=True)
    case("bundle without the branch", lambda r: r.site_git.__setitem__(
        f"bundle list-heads {r.bundle} refs/heads/product/tripbox", {"out": ""}), "holds no product/tripbox",
         late=True)
    case("unmarked tip", answer("log -1 --format=%s refs/tripbox-bundle/tip", out="Add the sections\n"),
         f"does not start with {SKIP}", late=True)

    def conflicting(r):
        r.site_git[ANC_BUNDLE] = {"rc": 1}
        r.site_git[MERGE_TREE] = {"rc": 1}

    case("a moved remote that conflicts", conflicting, "would not merge", late=True)

    def diverged(r):
        r.site_git["rev-parse --verify -q refs/heads/product/tripbox"] = {"out": SITE_BASE + "\n"}
        r.site_git[BUNDLE_IN_LOCAL] = {"rc": 1}
        r.site_git[LOCAL_IN_BUNDLE] = {"rc": 1}

    case("a diverged local branch", diverged, "has commits the bundle lacks", late=True)

    def local_unmarked(r):
        r.site_git["rev-parse --verify -q refs/heads/product/tripbox"] = {"out": SITE_TIP + "\n"}
        r.site_git[BUNDLE_IN_LOCAL] = {}
        r.site_git["log -1 --format=%s refs/heads/product/tripbox"] = {"out": "A fix\n"}

    case("an unmarked local tip", local_unmarked, f"does not start with {SKIP}", late=True)

    def old_git(r):
        r.site_git[ANC_BUNDLE] = {"rc": 1}
        r.site_git[MERGE_TREE] = {"rc": 129}

    case("a git without merge-tree --write-tree", old_git, "needs git 2.38", late=True)
    for name, (setup, said, late) in cases.items():
        fresh = Rig(rig.tmp / name.replace(" ", "-"))
        setup(fresh)
        r = fresh.publish_site()
        assert r.returncode == 1, (name, r.stderr)
        assert said in r.stderr and "nothing was changed" in r.stderr, (name, r.stderr)
        assert write_only_calls(fresh) == [], name
        got = fresh.site_calls()
        assert not any(c.startswith(SITE_MUTATIONS) for c in got), name
        assert ("fetch origin" in got) == late, name


def test_a_merged_pull_request_means_the_page_is_live_and_nothing_more_is_done(rig):
    rig.scenario["pr_merged"] = "https://github.com/example-org/example-site/pull/1"
    r = rig.publish_site()
    assert r.returncode == 0, r.stderr
    assert "already merged" in r.stdout and "the page is live" in r.stdout
    got = rig.site_calls()
    assert not any(c.startswith(SITE_MUTATIONS) or c.startswith("fetch") for c in got)
    assert pr_creates(rig) == [] and "check_site" not in [c["tool"] for c in rig.calls()]
    assert [c for c in rig.tools("gh", "pr") if "open" in c["argv"]] == []


def test_a_remote_that_moved_is_merged_in_with_a_marked_commit_never_rebased(rig):
    rig.site_git[ANC_BUNDLE] = {"rc": 1}
    rig.site_git[MERGE_TREE] = {}
    rig.site_git[MERGE] = {}
    r = rig.publish_site()
    assert r.returncode == 0, r.stderr
    got = rig.site_calls()
    assert got.count(MERGE) == 1 and MERGE.split(" -m ")[1].startswith(SKIP)
    assert got.index(MERGE_TREE) < got.index(MERGE) < got.index(SITE_PUSH)
    assert not any("rebase" in c or "reset" in c or "--force" in c for c in got)


def test_a_merge_that_conflicts_in_the_end_is_abandoned_and_nothing_is_pushed(rig):
    rig.site_git[ANC_BUNDLE] = {"rc": 1}
    rig.site_git[MERGE_TREE] = {}
    rig.site_git[MERGE] = {"rc": 1}
    rig.site_git["merge --abort"] = {}
    r = rig.publish_site()
    assert r.returncode == 1 and "could not be merged" in r.stderr
    got = rig.site_calls()
    assert "merge --abort" in got and SITE_PUSH not in got and pr_creates(rig) == []


def test_a_failing_site_check_stops_before_the_push(rig):
    (rig.site / "fail-check").write_text("x")
    r = rig.publish_site()
    assert r.returncode == 1 and "check_site.py --no-provisional" in r.stderr
    assert SITE_PUSH not in rig.site_calls() and pr_creates(rig) == []


def test_a_push_the_remote_refuses_is_reported_and_never_forced(rig):
    rig.site_git[SITE_PUSH] = {"rc": 1, "err": "! [rejected] product/tripbox (fetch first)"}
    r = rig.publish_site()
    assert r.returncode == 1 and "fetch first" in r.stderr
    assert pr_creates(rig) == []
    assert not any("force" in c for c in rig.site_calls())


def test_a_dry_run_pushes_nothing_and_prints_what_it_would_do(rig):
    r = rig.publish_site("--dry-run")
    assert r.returncode == 0, r.stderr
    got = rig.site_calls()
    assert f"cat-file -e {SITE_BASE_FILE}" in got
    assert not any(c.startswith(SITE_MUTATIONS) for c in got)
    assert pr_creates(rig) == [] and rig.changes() == []
    assert "check_site" not in [c["tool"] for c in rig.calls()]
    for shown in (f"would run: git -C {rig.site} push origin product/tripbox:product/tripbox",
                  "would run: gh pr create"):
        assert shown in r.stdout, shown


def test_a_rerun_opens_no_second_pull_request_and_pushes_nothing_already_pushed(rig):
    rig.scenario["pr_list"] = "https://github.com/example-org/example-site/pull/1"
    rig.site_git["rev-parse --verify -q refs/heads/product/tripbox"] = {"out": SITE_TIP + "\n"}
    rig.site_git["checkout product/tripbox"] = {}
    rig.site_git["merge-base --is-ancestor refs/tripbox-bundle/tip refs/heads/product/tripbox"] = {}
    rig.site_git["rev-parse --verify -q refs/remotes/origin/product/tripbox"] = {"out": SITE_TIP + "\n"}
    r = rig.publish_site()
    assert r.returncode == 0, r.stderr
    assert SITE_PUSH not in rig.site_calls() and pr_creates(rig) == []
    assert "already open: https://github.com/example-org/example-site/pull/1" in r.stdout
    assert "already pushed" in r.stdout


def test_a_branch_the_remote_does_not_have_yet_is_pushed_without_a_merge(rig):
    rig.site_git["rev-parse --verify -q refs/remotes/origin/product/tripbox"] = {"rc": 1}
    r = rig.publish_site()
    assert r.returncode == 0, r.stderr
    assert SITE_PUSH in rig.site_calls() and not any(c.startswith("merge") for c in rig.site_calls())
    assert len(pr_creates(rig)) == 1


def test_a_local_branch_behind_the_bundle_is_fast_forwarded_never_rebased(rig):
    rig.site_git["rev-parse --verify -q refs/heads/product/tripbox"] = {"out": SITE_BASE + "\n"}
    rig.site_git[BUNDLE_IN_LOCAL] = {"rc": 1}
    rig.site_git[LOCAL_IN_BUNDLE] = {}
    rig.site_git["checkout product/tripbox"] = {}
    rig.site_git["merge --ff-only refs/tripbox-bundle/tip"] = {}
    r = rig.publish_site()
    assert r.returncode == 0, r.stderr
    got = rig.site_calls()
    assert got.index("checkout product/tripbox") < got.index("merge --ff-only refs/tripbox-bundle/tip") \
        < got.index(SITE_PUSH)
    assert not any("rebase" in c or "reset" in c or "--force" in c for c in got)
    assert "merges cleanly" not in r.stdout, "the remote did not move"


def test_a_diverged_local_branch_names_what_to_do(rig):
    rig.site_git["rev-parse --verify -q refs/heads/product/tripbox"] = {"out": SITE_BASE + "\n"}
    rig.site_git[BUNDLE_IN_LOCAL] = {"rc": 1}
    rig.site_git[LOCAL_IN_BUNDLE] = {"rc": 1}
    r = rig.publish_site()
    assert r.returncode == 1
    assert "git branch -m product/tripbox product/tripbox-old" in r.stderr and "fresh clone" in r.stderr


def test_the_summary_says_the_remote_moved_only_when_it_did(rig):
    rig.site_git[ANC_BUNDLE] = {"rc": 1}
    rig.site_git[MERGE_TREE] = {}
    rig.site_git[MERGE] = {}
    assert "moved and merges cleanly" in rig.publish_site().stdout
