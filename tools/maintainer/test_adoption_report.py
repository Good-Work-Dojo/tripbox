# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tools/maintainer/adoption-report.sh, run against a stub `gh` on PATH that logs every call and
answers from canned JSON. Nothing reaches GitHub; every number and name here is invented."""

# ruff: noqa: E501

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "tools/maintainer/adoption-report.sh"
REPO = "Example-Org/example-app"
R = f"repos/{REPO}"
PULLS = "image pulls: GitHub's API does not give them; see https://github.com/Example-Org/example-app/pkgs/container/example-app"

FAKE_GH = r'''
import json, os, sys
argv = sys.argv[1:]
scenario = json.load(open(os.environ["FAKE_SCENARIO"]))
method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
endpoint = [a for a in argv[1:] if not a.startswith("-") and a not in ("GET", "POST")][0]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": argv, "method": method, "endpoint": endpoint}) + "\n")
ans = scenario.get(f"{method} {endpoint}")
if ans is None:
    print(f"stub gh: no answer for {method} {endpoint}", file=sys.stderr)
    sys.exit(3)
if ans.get("status", 200) >= 400:
    print(f"gh: Forbidden (HTTP {ans['status']})", file=sys.stderr)
    sys.exit(1)
print(json.dumps(ans["body"]))
'''


def answers(traffic: bool = True) -> dict:
    q = f"search/issues?q=repo:{REPO}"
    out = {
        f"GET {R}": {"body": {"stargazers_count": 12, "forks_count": 3, "subscribers_count": 4,
                              "watchers_count": 12}},
        f"GET {q}+is:issue+is:open&per_page=1": {"body": {"total_count": 2}},
        f"GET {q}+is:issue+is:closed&per_page=1": {"body": {"total_count": 5}},
        f"GET {q}+is:pr+is:open&per_page=1": {"body": {"total_count": 1}},
        f"GET {q}+is:pr+is:closed&per_page=1": {"body": {"total_count": 6}},
    }
    for what, body in (("views", {"count": 40, "uniques": 11}), ("clones", {"count": 9, "uniques": 5}),
                       ("popular/referrers", [{"referrer": "example.net", "count": 20, "uniques": 8}]),
                       ("popular/paths", [{"path": f"/{REPO}", "title": "app", "count": 30, "uniques": 10}])):
        out[f"GET {R}/traffic/{what}"] = {"body": body} if traffic else {"status": 403}
    return out


class Rig:
    def __init__(self, tmp: Path, traffic: bool = True):
        self.tmp = tmp
        (tmp / "tools/maintainer").mkdir(parents=True)
        shutil.copy2(ROOT / SCRIPT, tmp / SCRIPT)
        (tmp / "bin").mkdir()
        gh = tmp / "bin/gh"
        gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
        gh.chmod(0o755)
        self.log = tmp / "calls.jsonl"
        (tmp / "scenario.json").write_text(json.dumps(answers(traffic)), encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        self.log.unlink(missing_ok=True)
        env = {"PATH": f"{self.tmp / 'bin'}:/usr/local/bin:/usr/bin:/bin", "HOME": str(self.tmp),
               "TMPDIR": str(self.tmp), "FAKE_LOG": str(self.log),
               "FAKE_SCENARIO": str(self.tmp / "scenario.json"), "GH_TOKEN": "invented-gh-token-value"}
        return subprocess.run([str(self.tmp / SCRIPT), "--repo", REPO, *args], env=env, capture_output=True,
                              text=True, timeout=60, check=False)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_the_script_is_executable_bash_and_changes_nothing_by_construction():
    p = ROOT / SCRIPT
    text = p.read_text("utf-8")
    assert p.stat().st_mode & stat.S_IXUSR and text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text and "Good-Work-Dojo/tripbox" in text
    for word in ("-X POST", "-X PUT", "-X PATCH", "-X DELETE", "GH_TOKEN", "release create"):
        assert word not in text, word


def test_every_call_is_a_read(rig):
    r = rig.run()
    assert r.returncode == 0, r.stderr
    calls = rig.calls()
    assert calls and {c["method"] for c in calls} == {"GET"}
    assert not any(a in ("--input", "-f", "-F", "--field", "--raw-field") for c in calls for a in c["argv"])
    for line in ("stars 12, forks 3, watchers 4", "views (14 days)     40 total, 11 unique",
                 "clones (14 days)    9 total, 5 unique", "issues              2 open, 5 closed",
                 "pull requests       1 open, 6 closed", "example.net  20 total, 8 unique",
                 f"/{REPO}  30 total, 10 unique"):
        assert line in r.stdout, line
    assert "invented-gh-token" not in r.stdout + r.stderr


def test_traffic_without_push_access_reads_as_unavailable(tmp_path):
    rig = Rig(tmp_path, traffic=False)
    text = rig.run()
    assert text.returncode == 0, text.stderr
    assert "views (14 days)     unavailable (needs push access)" in text.stdout
    assert "top referrers: unavailable (needs push access)" in text.stdout
    doc = json.loads(rig.run("--json").stdout)
    assert doc["views_14_days"] is None and doc["clones_14_days"] is None
    assert doc["top_referrers"] is None and doc["top_paths"] is None
    assert doc["stars"] == 12 and doc["issues"] == {"open": 2, "closed": 5}


def test_traffic_that_fails_for_another_reason_says_it_could_not_be_read(tmp_path):
    rig = Rig(tmp_path)
    scenario = json.loads((tmp_path / "scenario.json").read_text("utf-8"))
    scenario[f"GET {R}/traffic/views"] = {"status": 502}
    scenario[f"GET {R}/traffic/popular/paths"] = {"status": 502}
    (tmp_path / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert "views (14 days)     unavailable (could not be read: run this again)" in r.stdout
    assert "top paths: unavailable (could not be read: run this again)" in r.stdout
    assert "clones (14 days)    9 total, 5 unique" in r.stdout and "needs push access" not in r.stdout


def test_json_is_one_object(rig):
    r = rig.run("--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert isinstance(doc, dict) and r.stdout.count("\n") > 1
    assert doc["repository"] == REPO and doc["watchers"] == 4 and doc["forks"] == 3
    assert doc["views_14_days"] == {"total": 40, "unique": 11}
    assert doc["pull_requests"] == {"open": 1, "closed": 6}
    assert doc["top_referrers"] == [{"referrer": "example.net", "total": 20, "unique": 8}]


def test_image_pulls_are_said_to_be_unavailable_with_the_packages_address(rig):
    assert PULLS in rig.run().stdout.splitlines()
    assert json.loads(rig.run("--json").stdout)["image_pulls"] == PULLS[len("image pulls: "):]


@pytest.mark.parametrize("args", [("--nope",), ("--repo",)])
def test_a_bad_argument_is_a_usage_error_before_any_call(rig, args):
    r = subprocess.run([str(rig.tmp / SCRIPT), *args], env={"PATH": f"{rig.tmp / 'bin'}:/usr/bin:/bin",
                       "FAKE_LOG": str(rig.log)}, capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 2 and not rig.log.exists()
