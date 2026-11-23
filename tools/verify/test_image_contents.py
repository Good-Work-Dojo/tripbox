# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The runtime image carries the corpus, where the package finds it beside its source tree, and the
build context keeps it; it runs as any user and carries an extractor on every architecture; the
arm64 workflow builds and tries it on an arm64 runner."""

import re
from pathlib import Path

from test_check_workflows import run_commands
from test_ignore_files import docker_ignores

ROOT = Path(__file__).resolve().parents[2]
COPY_CORPUS = "COPY tests/corpus/ ./tests/corpus/"
PARSER = ROOT / "src" / "tripbox" / "parse" / "kitinerary.py"


def test_the_runtime_stage_carries_the_corpus():
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    runtime = lines[: lines.index("FROM runtime AS test")]
    assert COPY_CORPUS in runtime
    last_sync = max(i for i, line in enumerate(runtime) if line.startswith("RUN uv sync"))
    assert runtime.index(COPY_CORPUS) > last_sync
    for kept in (
        "tests/corpus/01-markup-flight/email.eml",
        "tests/corpus/config/travellers.yaml",
        "tests/corpus/corpus.json",
    ):
        assert not docker_ignores(kept), kept


LICENCE_COPIES = (
    "COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./",
    "COPY LICENSES/ ./LICENSES/",
)


def test_the_licence_files_are_copied_after_the_last_sync():
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    runtime = lines[: lines.index("FROM runtime AS test")]
    last_sync = max(i for i, line in enumerate(runtime) if line.startswith("RUN uv sync"))
    for copy in LICENCE_COPIES:
        assert copy in runtime, copy
        assert runtime.index(copy) > last_sync, copy
    for kept in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "LICENSES/GPL-3.0-only.txt"):
        assert (ROOT / kept).is_file(), kept
        assert not docker_ignores(kept), kept


def test_the_image_starts_the_worker_and_carries_no_cron():
    text = (ROOT / "Dockerfile").read_text()
    for gone in ("supercronic", "crontab", "ENTRYPOINT"):
        assert gone not in text, gone
    assert 'CMD ["python", "-m", "tripbox", "work"]' in text
    assert not (ROOT / "crontab").exists()
    assert not (ROOT / "scripts" / "entrypoint.sh").exists()
    lines = text.splitlines()
    assert any(line.startswith("FROM ") and line.endswith(" AS runtime") for line in lines)
    assert "FROM runtime AS test" in lines


def stages() -> tuple[list[str], list[str]]:
    """The Dockerfile's `runtime` stage and its `test` stage, as lines."""
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    at = lines.index("FROM runtime AS test")
    return lines[:at], lines[at:]


def instructions(lines: list[str]) -> list[str]:
    """The stage's instructions, each with its continuation lines joined on a newline; blank
    lines and comments are left out."""
    out: list[str] = []
    continued = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if continued:
            out[-1] += "\n" + line
        else:
            out.append(line)
        continued = line.rstrip().endswith("\\")
    return out


def installed(steps: list[str]) -> list[str]:
    """Every package an `apt-get install` of these instructions names, in order."""
    install = r"apt-get install -y --no-install-recommends ([^;&\\\n]+)"
    found = re.findall(install, "\n".join(steps))
    return [word for words in found for word in words.split()]


def test_the_extractor_is_kdes_on_amd64_and_debians_elsewhere():
    runtime, test = stages()
    steps = instructions(runtime)
    text = "\n".join(runtime)
    assert "ARG KITINERARY_SOURCE=auto" in steps
    # auto is kde on amd64 and debian on any other architecture
    assert 'arch="$(dpkg --print-architecture)"' in text
    assert 'if [ "$arch" = amd64 ]; then source=kde; else source=debian; fi' in text
    # kde: the static build, checked against its SHA-256 and refused off amd64
    assert re.search(r"^ARG KITINERARY_SHA256=[0-9a-f]{64}$", text, re.M)
    assert 'echo "${KITINERARY_SHA256}  /tmp/k.tgz" | sha256sum -c -' in text
    assert "kitinerary-extractor-x86_64-${KITINERARY_VERSION}.tgz" in text
    assert re.search(r'kde\)[^;]*\n *\[ "\$arch" = amd64 \] \|\|', text)
    # debian: the release's own package at the version it holds, any Debian revision of it (a
    # security update still builds), found where dpkg put it
    assert "'libkitinerary-bin=24.12.3-*'" in installed(steps)
    assert "dpkg -L libkitinerary-bin" in text
    # either way, one link is what the parser runs
    links = [line.strip() for line in runtime if "ln -s" in line]
    assert links == ['ln -s "$bin" /usr/local/bin/kitinerary-extractor; \\']
    assert re.search(r'^BINARY = "kitinerary-extractor"$', PARSER.read_text(), re.M)
    # nothing else is installed, and every package list fetched is cleaned in the same layer
    assert installed(steps) == ["ca-certificates", "tzdata", "curl",
                                "'libkitinerary-bin=24.12.3-*'"]
    assert installed(instructions(test)) == ["git"]
    for step in instructions(runtime + test):
        if "apt-get update" in step:
            assert "rm -rf /var/lib/apt/lists/*" in step


def test_the_image_runs_as_a_plain_user_and_lets_any_user_read_it():
    runtime, test = stages()
    steps = instructions(runtime)
    # read by any user, written by none but the owner (no group or other write bit survives)
    chmod = "RUN chmod -R a+rX,go-w /app/src /app/scripts /app/config /app/tests"
    assert chmod in steps
    copies = [i for i, s in enumerate(steps) if s.startswith("COPY ") and "--from" not in s]
    assert steps.index(chmod) > max(copies)
    # the plain user is the last thing the stage sets before its health check and command
    assert [s for s in steps if s.startswith("USER ")] == ["USER 1000:1000"]
    assert steps[steps.index("USER 1000:1000") + 1].startswith("HEALTHCHECK ")
    assert re.search(r"\bHOME=/tmp\b", "\n".join(s for s in steps if s.startswith("ENV ")))
    # the test stage is root throughout (the lint and the tools' tests run as root) and lets any
    # user read what it adds, since the app suite runs as an arbitrary one
    tsteps = instructions(test)
    assert tsteps[1] == "USER root"
    assert [s for s in tsteps if s.startswith("USER ")] == ["USER root"]
    tcopies = [i for i, s in enumerate(tsteps) if s.startswith("COPY ")]
    tchmod = [i for i, s in enumerate(tsteps) if s.startswith("RUN chmod -R a+rX,go-w ")]
    assert len(tchmod) == 1 and tchmod[0] > max(tcopies)
    for added in ("/app/tests", "/app/docs", "/app/compose.yaml", "/app/.env.example"):
        assert added in tsteps[tchmod[0]].split()


WORKFLOWS = ROOT / ".github" / "workflows"
ANY_USER = "--user 12345:54321"
SMOKE_ENV = (
    "-e TRIPBOX_DATA_DIR=/tmp/data -e TRIPBOX_LOGS_DIR=/tmp/logs"
    " -e TRIPBOX_ARCHIVE_DIR=/tmp/archive -e TRIPBOX_CONFIG_DIR=/tmp/config"
)


def smoke_command(image: str) -> str:
    """The new-install smoke as a workflow runs it: as any user, on a read-only root."""
    return (
        f'docker run --rm --network none {ANY_USER} --read-only --tmpfs /tmp -v "$PWD:/repo:ro"'
        f" -w /repo -e PYTHONDONTWRITEBYTECODE=1 {SMOKE_ENV}"
        f" {image} python /repo/tools/verify/standalone_smoke.py"
    )


def checkout_pin(text: str) -> str:
    found = re.findall(r"^ +uses: (actions/checkout@[0-9a-f]{40})", text, re.M)
    assert len(found) == 1
    return found[0]


def test_the_arm64_workflow_builds_and_tries_the_image_on_an_arm64_runner():
    text = (WORKFLOWS / "arm64.yml").read_text()
    lines = text.splitlines()
    assert "name: arm64" in lines
    on = text[text.index("\non:\n") + 1 : text.index("\nconcurrency:\n")]
    assert on.splitlines() == [
        "on:",
        "  push:",
        "    branches: [main]",
        "    paths: ['Dockerfile', '.dockerignore', 'pyproject.toml', 'uv.lock', 'compose.yaml',",
        "            'src/tripbox/parse/**', 'tests/corpus/**', 'tools/verify/stack_smoke.sh',",
        "            'tools/verify/quickstart_check.py', '.github/workflows/arm64.yml']",
        "  pull_request:",
        "    paths: ['Dockerfile', '.dockerignore', 'pyproject.toml', 'uv.lock', 'compose.yaml',",
        "            'src/tripbox/parse/**', 'tests/corpus/**', 'tools/verify/stack_smoke.sh',",
        "            'tools/verify/quickstart_check.py', '.github/workflows/arm64.yml']",
        "  workflow_dispatch:",
    ]
    assert "pull_request_target" not in text
    assert "concurrency:\n  group: arm64-${{ github.ref }}\n  cancel-in-progress: true\n" in text
    assert "\npermissions:\n  contents: read\n\njobs:\n  smoke:\n" in text
    assert re.findall(r"^ {2}\S.*$", text[text.index("\njobs:\n") :], re.M) == ["  smoke:"]
    assert "    runs-on: ubuntu-24.04-arm" in lines and "    timeout-minutes: 25" in lines
    assert checkout_pin(text) == checkout_pin((WORKFLOWS / "ci.yml").read_text())
    assert "          persist-credentials: false" in lines
    assert run_commands(text) == [
        "python3 tools/verify/check_workflows.py",
        "docker build --target runtime -t tripboxoss:arm64 .",
        'test "$(docker run --rm --network none tripboxoss:arm64 uname -m)" = aarch64',
        f"docker run --rm --network none {ANY_USER} --read-only --tmpfs /tmp tripboxoss:arm64"
        ' python -m tripbox eval --recorded | tee "$RUNNER_TEMP/scorecard.txt"'
        " grep -Eqx 'eval: ([0-9]+) of \\1 emails right' \"$RUNNER_TEMP/scorecard.txt\""
        " grep -q '^parsers: kitinerary: 3 ' \"$RUNNER_TEMP/scorecard.txt\"",
        smoke_command("tripboxoss:arm64"),
        "TRIPBOX_SMOKE=1 tools/verify/stack_smoke.sh --image tripboxoss:arm64",
    ]
    # the scorecard's pipe fails the step when eval fails: bash named outright sets pipefail
    step = text[text.index("      - name: Corpus scorecard\n") :]
    assert step.splitlines()[1:3] == ["        shell: bash", "        run: |"]
    # the checkout is the one action: nothing is uploaded, and no secret is read
    assert len(re.findall(r"\buses:", text)) == 1 and "secrets." not in text


def test_the_ci_workflow_runs_the_app_and_a_new_install_as_any_user():
    commands = run_commands((WORKFLOWS / "ci.yml").read_text())
    assert (
        f"docker run --rm --network none {ANY_USER} -e PYTHONDONTWRITEBYTECODE=1 tripboxoss:ci"
        " pytest -q --tb=no -rN --disable-warnings -p no:cacheprovider"
    ) in commands
    assert smoke_command("tripboxoss:ci") in commands
    scorecard = [c for c in commands if "eval --recorded" in c]
    assert len(scorecard) == 1 and f" {ANY_USER} " in scorecard[0]
    assert "1000:1000" not in " ".join(commands)


def test_the_runtime_stage_marks_itself_as_the_image():
    """`TRIPBOX_IN_IMAGE=1` tells `tripbox web` its socket is the container's, published by
    compose at TRIPBOX_LISTEN: set in the runtime stage, and nowhere else."""
    runtime, test = stages()
    assert "ENV TRIPBOX_IN_IMAGE=1" in instructions(runtime)
    assert not any("TRIPBOX_IN_IMAGE" in step for step in instructions(test))
