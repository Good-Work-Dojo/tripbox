# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tools/verify/check_workflows.py: the rule that keeps every workflow off a self-hosted runner."""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "check_workflows.py"
SHA = "0123456789abcdef0123456789abcdef01234567"


def wf(
    runs_on_line: str | None = "    runs-on: ubuntu-24.04",
    uses: str | None = None,
    extra: str = "",
) -> str:
    lines = ["name: t", "on: push", "jobs:", "  build:"]
    if runs_on_line is not None:
        lines.append(runs_on_line)
    lines.append("    steps:")
    if uses is not None:
        lines.append(f"      - uses: {uses}")
    lines.append("      - run: echo hi")
    return "\n".join(lines) + "\n" + extra


def check(tmp_path: Path, text: str | bytes, name: str = "w.yml"):
    d = tmp_path / "wf"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_bytes(text if isinstance(text, bytes) else text.encode())
    r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    return r


@pytest.mark.parametrize("line", [
    "    runs-on: ubuntu-24.04",
    "    runs-on: ubuntu-24.04-arm",
    "    runs-on: 'ubuntu-24.04'",
    '    runs-on: "ubuntu-24.04-arm"',
    "    runs-on: ubuntu-24.04   # hosted",
])
def test_hosted_runner_literals_pass(tmp_path, line):
    r = check(tmp_path, wf(line))
    assert r.returncode == 0, r.stderr
    assert r.stderr == "" and r.stdout == ""


@pytest.mark.parametrize("line", [
    "    runs-on: ubuntu-latest",
    "    runs-on: [self-hosted, x]",
    "    runs-on:\n      - ubuntu-24.04",
    "    runs-on: ${{ matrix.os }}",
    "    runs-on: self-hosted",
    "    runs-on:",
    "    runs-on: ubuntu-24.04 extra",
    "    'runs-on': ubuntu-24.04",
    "    {runs-on: ubuntu-24.04}",
    "    steps-x: 1 runs-on: ubuntu-24.04",
    "    runs-on: 'ubuntu-24.04\"",
])
def test_other_runs_on_values_fail_with_file_and_line(tmp_path, line):
    r = check(tmp_path, wf(line))
    assert r.returncode == 1
    assert "w.yml:5:" in r.stderr


def test_job_without_runs_on_fails_on_the_job_line(tmp_path):
    r = check(tmp_path, wf(None))
    assert r.returncode == 1
    assert "w.yml:4:" in r.stderr


def test_second_job_without_runs_on_fails(tmp_path):
    text = wf() + "  other:\n    steps:\n      - run: x\n"
    r = check(tmp_path, text)
    assert r.returncode == 1
    assert "w.yml:8:" in r.stderr and "w.yml:4:" not in r.stderr


def test_runs_on_in_a_step_does_not_satisfy_the_job(tmp_path):
    text = (
        "on: push\njobs:\n  a:\n    steps:\n      - with:\n          x: 1\n"
        "        runs-on: ubuntu-24.04\n"
    )
    r = check(tmp_path, text)
    assert r.returncode == 1


def test_flow_style_jobs_fail(tmp_path):
    r = check(tmp_path, "on: push\njobs: {a: {runs-on: ubuntu-24.04}}\n")
    assert r.returncode == 1


def test_file_without_jobs_fails(tmp_path):
    r = check(tmp_path, "on: push\n")
    assert r.returncode == 1 and "jobs" in r.stderr


@pytest.mark.parametrize("word", ["pull_request_target", "self-hosted"])
def test_forbidden_words_fail(tmp_path, word):
    r = check(tmp_path, wf(extra=f"# fine\nenv:\n  X: {word}\n"))
    assert r.returncode == 1 and "w.yml:10:" in r.stderr


@pytest.mark.parametrize("word", ["self-hosted", "pull_request_target", "Self-Hosted"])
def test_forbidden_words_in_comments_fail_too(tmp_path, word):
    r = check(tmp_path, wf(extra=f"# never use {word}\n"))
    assert r.returncode == 1 and "w.yml:8:" in r.stderr


def test_action_by_tag_fails(tmp_path):
    r = check(tmp_path, wf(uses="actions/checkout@v4"))
    assert r.returncode == 1 and "w.yml:7:" in r.stderr


@pytest.mark.parametrize(
    "ref",
    ["actions/checkout@main", "actions/checkout@0123abc", "docker://alpine:3", "actions/checkout"],
)
def test_other_unpinned_action_refs_fail(tmp_path, ref):
    assert check(tmp_path, wf(uses=ref)).returncode == 1


def test_sha_pinned_action_with_version_comment_passes(tmp_path):
    r = check(tmp_path, wf(uses=f"actions/checkout@{SHA} # v4.2.0"))
    assert r.returncode == 0, r.stderr


def test_uppercase_or_long_sha_fails(tmp_path):
    assert check(tmp_path, wf(uses=f"actions/checkout@{SHA.upper()}")).returncode == 1
    assert check(tmp_path, wf(uses=f"actions/checkout@{SHA}0")).returncode == 1


def test_sub_path_action_and_local_path_pass(tmp_path):
    assert check(tmp_path, wf(uses=f"actions/cache/restore@{SHA}")).returncode == 0
    assert check(tmp_path, wf(uses="./.github/actions/x")).returncode == 0


ONLY_GITHUBS = "only GitHub's own actions (actions/*) may be used"


@pytest.mark.parametrize("ref", [
    f"owner/repo@{SHA}",
    f"owner/repo/sub/dir@{SHA}",
    f"docker/login-action@{SHA}",
    f"actions-x/checkout@{SHA}",
    f"Actions/checkout@{SHA}",
    f"my-actions/checkout@{SHA}",
    f"'octo/actions@{SHA}'",
])
def test_an_action_that_is_not_githubs_own_fails_with_its_line(tmp_path, ref):
    r = check(tmp_path, wf(uses=ref))
    assert r.returncode == 1
    assert r.stderr.splitlines() == [f"{tmp_path / 'wf' / 'w.yml'}:7: {ONLY_GITHUBS}"]


@pytest.mark.parametrize("ref", [
    f"actions/checkout@{SHA}",
    f"actions/upload-pages-artifact@{SHA} # v5.0.0",
    f"'actions/deploy-pages@{SHA}'",
    "./local/action",
])
def test_githubs_own_actions_and_a_local_path_pass(tmp_path, ref):
    r = check(tmp_path, wf(uses=ref))
    assert r.returncode == 0, r.stderr


def test_an_unpinned_action_of_another_owner_fails_once_as_unpinned(tmp_path):
    r = check(tmp_path, wf(uses="owner/repo@v1"))
    assert r.returncode == 1 and len(r.stderr.splitlines()) == 1
    assert "40-hex commit" in r.stderr and ONLY_GITHUBS not in r.stderr


def test_yaml_extension_and_other_files_in_dir(tmp_path):
    d = tmp_path / "wf"
    d.mkdir()
    (d / "a.yaml").write_text(wf("    runs-on: ubuntu-latest"))
    (d / "notes.txt").write_text("self-hosted")
    r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "a.yaml:5:" in r.stderr and "notes.txt" not in r.stderr


def test_non_utf8_file_fails(tmp_path):
    r = check(tmp_path, b"\xff\xfe\x00bad")
    assert r.returncode == 1 and "UTF-8" in r.stderr


def test_missing_directory_is_clean(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope")], capture_output=True, text=True
    )
    assert r.returncode == 0 and r.stderr == ""


def test_default_directory_is_github_workflows(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "x.yml").write_text(wf("    runs-on: ubuntu-latest"))
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 1 and "x.yml:5:" in r.stderr


# ---- raw-line rules: a `#` inside a quoted string never hides a key

def test_quoted_hash_does_not_hide_a_self_hosted_runner(tmp_path):
    text = 'on: push\njobs:\n  deploy: {name: "Deploy #2", runs-on: [self-hosted, other-label]}\n'
    r = check(tmp_path, text)
    assert r.returncode == 1 and "w.yml:3:" in r.stderr


def test_quoted_hash_does_not_hide_an_unpinned_action(tmp_path):
    r = check(tmp_path, wf(extra='      - {name: "Checkout #1", uses: actions/checkout@v4}\n'))
    assert r.returncode == 1 and "w.yml:8:" in r.stderr


def test_commented_runs_on_fails(tmp_path):
    r = check(tmp_path, wf(extra="    # runs-on: self-hosted\n"))
    assert r.returncode == 1 and "w.yml:8:" in r.stderr


def test_runs_on_comment_needs_whitespace_before_the_hash(tmp_path):
    r = check(tmp_path, wf("    runs-on: ubuntu-24.04#x"))
    assert r.returncode == 1 and "w.yml:5:" in r.stderr


def test_runs_on_continuation_line_fails(tmp_path):
    text = wf("    runs-on: ubuntu-24.04\n      other-label")
    r = check(tmp_path, text)
    assert r.returncode == 1 and "w.yml:5:" in r.stderr


def test_runs_on_followed_by_a_sibling_key_passes(tmp_path):
    text = wf("    runs-on: ubuntu-24.04\n    # note\n    timeout-minutes: 5")
    assert check(tmp_path, text).returncode == 0


@pytest.mark.parametrize("line", [
    "      - uses: 'actions/checkout@v4'",
    "      - name: x\n        uses: actions/checkout@v4 extra",
    "      - run: echo 'uses: actions/checkout@v4'",
    "      - {uses: actions/checkout@" + SHA + "}",
    "      - uses: actions/checkout@" + SHA + " extra",
    "      - uses: actions/checkout@" + SHA + "#v1",
    '      - "uses": actions/checkout@' + SHA,
    "      - uses: \"actions/checkout@" + SHA + "'",
    "      - run: echo hi uses: ./x",
])
def test_uses_must_be_a_plain_pinned_line(tmp_path, line):
    r = check(tmp_path, wf(extra=line + "\n"))
    assert r.returncode == 1 and "w.yml:" in r.stderr


def test_quoted_pinned_uses_passes(tmp_path):
    assert check(tmp_path, wf(uses=f"'actions/checkout@{SHA}'  # v7")).returncode == 0


# ---- job level: every line must be a plain job key

@pytest.mark.parametrize("key", ['"deploy":', "'deploy':", "deploy :", "deploy: &dep"])
def test_odd_job_key_spellings_fail(tmp_path, key):
    text = f"on: push\njobs:\n  {key}\n    uses: octo/repo/.github/workflows/x.yml@{SHA}\n"
    r = check(tmp_path, text)
    assert r.returncode == 1 and "w.yml:3:" in r.stderr


def test_job_level_uses_line_fails(tmp_path):
    r = check(tmp_path, f"on: push\njobs:\n  uses: octo/repo/x.yml@{SHA}\n")
    assert r.returncode == 1 and "w.yml:3:" in r.stderr


def test_second_document_fails(tmp_path):
    r = check(tmp_path, wf(extra="---\njobs:\n  x:\n    uses: ./y\n"))
    assert r.returncode == 1 and "w.yml:8:" in r.stderr


@pytest.mark.parametrize("marker", ["...", "--- x"])
def test_other_document_markers_fail(tmp_path, marker):
    assert check(tmp_path, marker + "\n" + wf()).returncode == 1


def test_jobs_without_any_job_fails_without_a_runs_on_mention(tmp_path):
    r = check(tmp_path, "on: push\njobs:\n  # empty\n")
    assert r.returncode == 1 and "w.yml:2:" in r.stderr and "recognised" in r.stderr


# ---- file selection

def test_extensions_match_case_insensitively_and_bare_names(tmp_path):
    d = tmp_path / "wf"
    d.mkdir()
    (d / "A.YML").write_text(wf("    runs-on: ubuntu-latest"))
    (d / ".yml").write_text(wf("    runs-on: ubuntu-latest"))
    (d / "b.YaMl").write_text(wf("    runs-on: ubuntu-latest"))
    r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    assert r.returncode == 1
    for name in ("A.YML:5:", "/.yml:5:", "b.YaMl:5:"):
        assert name in r.stderr


def test_dir_that_is_a_file_fails(tmp_path):
    f = tmp_path / "notadir"
    f.write_text("x")
    r = subprocess.run([sys.executable, str(SCRIPT), str(f)], capture_output=True, text=True)
    assert r.returncode == 1 and "not a directory" in r.stderr and "Traceback" not in r.stderr


def test_unreadable_file_is_reported_not_a_traceback(tmp_path):
    d = tmp_path / "wf"
    d.mkdir()
    (d / "x.yml").symlink_to(d / "loop.yml")
    (d / "loop.yml").symlink_to(d / "x.yml")
    r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    assert r.returncode == 1 and "unreadable" in r.stderr and "Traceback" not in r.stderr


# ---- one jobs block, one leading document start

def test_second_top_level_jobs_block_fails(tmp_path):
    text = wf() + f"jobs:\n  deploy:\n    uses: octo/repo/.github/workflows/x.yml@{SHA}\n"
    r = check(tmp_path, text)
    assert r.returncode == 1 and "w.yml:8:" in r.stderr


def test_one_leading_document_start_is_allowed(tmp_path):
    assert check(tmp_path, "---\n" + wf()).returncode == 0
    assert check(tmp_path, "# header\n\n---  # start\n" + wf()).returncode == 0


def test_document_start_after_content_fails(tmp_path):
    r = check(tmp_path, "name: t\n---\n" + wf().split("\n", 1)[1])
    assert r.returncode == 1 and "w.yml:2:" in r.stderr


def test_two_leading_document_starts_fail(tmp_path):
    r = check(tmp_path, "---\n---\n" + wf())
    assert r.returncode == 1 and "w.yml:2:" in r.stderr


# ---- every spelling of a top-level jobs key counts

@pytest.mark.parametrize("key", ["jobs :", '"jobs":', "'jobs':"])
def test_second_jobs_key_in_another_spelling_fails(tmp_path, key):
    text = wf() + f"{key}\n  deploy:\n    uses: octo/repo/.github/workflows/x.yml@{SHA}\n"
    r = check(tmp_path, text)
    assert r.returncode == 1 and "w.yml:8:" in r.stderr


def test_quoted_jobs_first_and_plain_second_fails(tmp_path):
    text = f'"jobs":\n  deploy:\n    uses: octo/repo/x.yml@{SHA}\n' + wf()
    r = check(tmp_path, text)
    assert r.returncode == 1 and "w.yml:6:" in r.stderr


@pytest.mark.parametrize("key", ["jobs :", '"jobs":'])
def test_only_jobs_key_in_another_spelling_fails(tmp_path, key):
    text = f"on: push\n{key}\n  a:\n    runs-on: ubuntu-24.04\n"
    r = check(tmp_path, text)
    assert r.returncode == 1 and "w.yml:2:" in r.stderr


# ---- the pytest lines of a workflow stay quiet

REQUIRED_PYTEST_FLAGS = ("--disable-warnings", "--tb=no", "-rN")
WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def run_commands(text: str) -> list[str]:
    """Each `run:` value of a workflow, a block scalar folded onto one line."""
    commands, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        head = lines[i].lstrip(" -")
        if not head.startswith("run:"):
            i += 1
            continue
        indent = len(lines[i]) - len(lines[i].lstrip(" "))
        value, i = [head[4:].strip()], i + 1
        while i < len(lines) and (not lines[i].strip() or
                                  len(lines[i]) - len(lines[i].lstrip(" ")) > indent):
            value.append(lines[i].strip())
            i += 1
        commands.append(" ".join(v for v in value if v and v not in (">-", "|", ">")))
    return commands


def pytest_lines_missing_flags(text: str) -> list[str]:
    bad = []
    for command in run_commands(text):
        words = command.split()
        if "pytest" in words and not all(flag in words for flag in REQUIRED_PYTEST_FLAGS):
            bad.append(command)
    return bad


def test_a_pytest_line_without_the_quiet_flags_is_found():
    text = wf(extra="").replace("echo hi", "docker run img pytest -q --tb=no -rN")
    assert pytest_lines_missing_flags(text) == ["docker run img pytest -q --tb=no -rN"]
    folded = wf().replace("      - run: echo hi",
                          "      - run: >-\n          docker run img\n          pytest -q -rN")
    assert len(pytest_lines_missing_flags(folded)) == 1
    ok = wf().replace("echo hi", "pytest -q --tb=no -rN --disable-warnings tools")
    assert pytest_lines_missing_flags(ok) == []


def test_every_pytest_line_of_the_workflows_carries_the_quiet_flags():
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files
    seen = 0
    for f in files:
        text = f.read_text()
        seen += sum("pytest" in c.split() for c in run_commands(text))
        assert pytest_lines_missing_flags(text) == [], f.name
    assert seen >= 2
