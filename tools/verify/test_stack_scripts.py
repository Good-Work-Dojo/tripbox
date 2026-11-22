# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The three stack runners a workflow starts (stack_smoke.sh, browser_checks.sh,
flightlog_e2e.sh), run against a stub `docker` and `curl` early on PATH: no container starts and
nothing is reached. The stub docker records every call, the directory it was made in and the
variables compose would read; `python3` is the real interpreter but for the page check, which the
stub answers. Each script refuses without TRIPBOX_SMOKE=1 and says what it would start, names the
step that failed, and takes its stack down whenever it may have started it."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SMOKE = HERE / "stack_smoke.sh"
BROWSER = HERE / "browser_checks.sh"
FLIGHTLOG = HERE / "flightlog_e2e.sh"
IMAGE = "example.test/tripbox:candidate"
KEYS = ("E2E_AIRTRAIL_API_KEY", "E2E_AIRTRAIL_HAND_KEY", "E2E_AIRTRAIL_NARROW_KEY",
        "E2E_AIRTRAIL_FULL_KEY", "E2E_AIRTRAIL_OTHER_KEY")
IDS = ("a" * 64, "b" * 64)

# Every call: its arguments on one line in STUB_LOG, and a JSON line in STUB_CALLS with its
# arguments, its directory and the variables compose would read. A call whose line holds
# STUB_FAIL fails. `ps -q` prints two ids, `inspect` STUB_INSPECT, the shoot prints its checks'
# summary, and an `up` keeps a copy of the settings file of its directory.
DOCKER = r"""#!/usr/bin/env bash
line="$*"
printf '%s\n' "$line" >> "$STUB_LOG"
"$STUB_PYTHON" -c '
import json, os, sys
names = ["TRIPBOX_IMAGE", "E2E_TRIPBOX_IMAGE", "E2E_AIRTRAIL_PORT", "SCREENS_TRIPBOX_IMAGE",
         "SCREENS_BROWSER_IMAGE", "SCREENS_USER", "COMPOSE_PROJECT_NAME", "TRIPBOX_PASSWORD"]
names += [n for n in os.environ if n.startswith("E2E_AIRTRAIL_") and n.endswith("_KEY")]
seen = {n: os.environ[n] for n in names if n in os.environ}
print(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(), "env": seen}))
' "$@" >> "$STUB_CALLS"
case " $line " in *" up "*) [ -f .env ] && cp .env "$STUB_DIR/env-at-up" ;; esac
case " $line " in *" psql "*) cat > /dev/null ;; esac
if [ -n "${STUB_FAIL:-}" ]; then
  case "$line" in *"$STUB_FAIL"*) exit 1 ;; esac
fi
case " $line " in
  *" ps -q "*) printf '%s\n' "${STUB_IDS:-}" ;;
  " inspect "*) cat "$STUB_INSPECT" ;;
  *"shoot.py"*) printf '%s\n' "checks: ${STUB_CHECKS:-8 of 8 ok}" "screens: ok" ;;
esac
exit 0
"""
# the port check exits 7 (nothing listens) unless STUB_CURL says otherwise; the wait for AirTrail
# (a -w status) is a 200
CURL = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$STUB_LOG"
for a in "$@"; do [ "$a" = -w ] && { printf 200; exit 0; }; done
exit "${STUB_CURL:-7}"
"""
PYTHON = r"""#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in *quickstart_check.py)
    printf 'page %s\n' "$*" >> "$STUB_LOG"
    echo "quickstart check: ok"; exit "${STUB_PAGE:-0}" ;;
  esac
done
exec "$STUB_PYTHON" "$@"
"""


def inspect(policy: str = "unless-stopped", web_host: str = "127.0.0.1",
            worker_ports: dict | None = None) -> str:
    def one(service: str, ports: dict) -> dict:
        return {"Name": f"/tripbox-smoke-{service}-1",
                "Config": {"Labels": {"com.docker.compose.service": service}},
                "HostConfig": {"RestartPolicy": {"Name": policy}, "PortBindings": ports}}
    return json.dumps([
        one("web", {"8110/tcp": [{"HostIp": web_host, "HostPort": "8110"}]}),
        one("worker", worker_ports or {}),
    ])


def run(tmp_path: Path, script: Path, *args: str, switch: bool = True, inspected: str = "",
        **stub: str) -> tuple[subprocess.CompletedProcess, list[str], list[dict]]:
    """The script run with the stubs first on PATH; its result, the calls' lines and the calls."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("docker", DOCKER), ("curl", CURL), ("python3", PYTHON)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    log, calls = tmp_path / "log", tmp_path / "calls"
    log.write_text("")
    calls.write_text("")
    (tmp_path / "inspect.json").write_text(inspected or inspect())
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TRIPBOX", "E2E_", "COMPOSE_", "SCREENS_", "RUNNER_"))}
    env.update(PATH=f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}", STUB_LOG=str(log),
               STUB_CALLS=str(calls), STUB_DIR=str(tmp_path), STUB_PYTHON=sys.executable,
               STUB_INSPECT=str(tmp_path / "inspect.json"), TMPDIR=str(scratch))
    env.update({"STUB_IDS": "\n".join(IDS), **stub})
    if switch:
        env["TRIPBOX_SMOKE"] = "1"
    r = subprocess.run(["bash", str(script), *args], env=env, cwd=tmp_path, capture_output=True,
                       text=True, timeout=120, check=False)
    return r, log.read_text().splitlines(), [json.loads(c) for c in calls.read_text().splitlines()]


def compose_calls(calls: list[dict]) -> list[list[str]]:
    return [c["args"] for c in calls if c["args"][:1] == ["compose"]]


# ---------------------------------------------------------------- the switch and the port


@pytest.mark.parametrize("script, args, words", [
    (SMOKE, ("--image", IMAGE), "stack smoke: this starts and removes a stack named"
     " tripbox-smoke on 127.0.0.1:8110; set TRIPBOX_SMOKE=1 to run it"),
    (BROWSER, ("--image", IMAGE), "browser checks: this starts and removes a stack named"
     " tripbox-screens, which publishes no port; set TRIPBOX_SMOKE=1 to run it"),
    (FLIGHTLOG, (), "flight log e2e: this starts and removes a stack named tripbox-e2e on"
     " 127.0.0.1:18090; set TRIPBOX_SMOKE=1 to run it"),
], ids=["smoke", "browser", "flight-log"])
@pytest.mark.parametrize("value", [None, "", "0", "yes", "true"])
def test_each_script_refuses_without_its_switch_and_says_what_it_would_start(tmp_path, script,
                                                                            args, words, value):
    extra = {} if value is None else {"TRIPBOX_SMOKE": value}
    r, lines, _ = run(tmp_path, script, *args, switch=False, **extra)
    assert r.returncode == 2
    assert r.stderr.splitlines() == [words] and r.stdout == ""
    assert lines == [], "nothing is asked of docker or the network"
    assert not list((tmp_path / "scratch").iterdir()), "nothing is made"


@pytest.mark.parametrize("script, args", [
    (SMOKE, ()), (SMOKE, ("--image",)), (SMOKE, ("--image", "")), (SMOKE, ("--image", "-x")),
    (SMOKE, ("--image", IMAGE, "extra")), (SMOKE, ("--other", IMAGE)),
    (BROWSER, ("--image",)), (BROWSER, ("extra",)), (FLIGHTLOG, ("--image", "a b")),
])
def test_a_bad_argument_is_the_usage_and_starts_nothing(tmp_path, script, args):
    r, lines, _ = run(tmp_path, script, *args)
    assert r.returncode == 2 and r.stderr.startswith("usage: ") and lines == []


@pytest.mark.parametrize("answer", ["0", "28", "52"], ids=["it-answers", "it-says-nothing",
                                                           "an-empty-reply"])
def test_the_smoke_refuses_a_taken_port(tmp_path, answer):
    r, lines, _ = run(tmp_path, SMOKE, "--image", IMAGE, STUB_CURL=answer)
    assert r.returncode == 2
    assert r.stderr.splitlines() == ["stack smoke: 127.0.0.1:8110 is in use"] and r.stdout == ""
    assert [line.split()[0] for line in lines] == ["curl"], "nothing is built or started"
    assert "http://127.0.0.1:8110/" in lines[0]


# ---------------------------------------------------------------- the stack smoke, step by step


def test_the_smoke_passes_its_steps_in_order(tmp_path):
    r, lines, calls = run(tmp_path, SMOKE, "--image", IMAGE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[-1] == "stack smoke: ok"
    composed = compose_calls(calls)
    assert composed == [
        ["compose", "-p", "tripbox-smoke", "up", "-d", "--wait"],
        ["compose", "-p", "tripbox-smoke", "run", "--rm", "worker", "tripbox", "doctor"],
        ["compose", "-p", "tripbox-smoke", "ps", "-q"],
        ["compose", "-p", "tripbox-smoke", "down", "-v", "--remove-orphans"],
    ]
    # every compose call is made in the unpacked folder, whose compose.yaml is the bundle's
    assert {Path(c["cwd"]).name for c in calls if c["args"][:1] == ["compose"]} == {"tripbox"}
    (page,) = [line for line in lines if line.startswith("page ")]
    assert "--base-url http://localhost:8110 " in page
    # the scratch folder is gone again
    assert not list((tmp_path / "scratch").iterdir())


def test_the_settings_file_is_the_password_line_alone(tmp_path):
    r, lines, _ = run(tmp_path, SMOKE, "--image", IMAGE)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (tmp_path / "env-at-up").read_text()
    (line,) = text.splitlines()
    assert text == line + "\n"
    m = re.fullmatch(r"TRIPBOX_PASSWORD=(\S+)", line)
    assert m, "one line: the password"
    written = m.group(1)
    # a generated password that holds a $, written $$ as compose reads it
    assert "$$" in written and re.sub(r"\$\$", "", written).count("$") == 0
    assert len(written.replace("$$", "$")) >= 10
    # the page is signed in to with the password as typed, never as written there
    (page,) = [line for line in lines if line.startswith("page ")]
    assert written not in page and "--password-file " in page


def test_the_image_is_given_to_compose_and_never_written_to_the_settings(tmp_path):
    r, _, calls = run(tmp_path, SMOKE, "--image", IMAGE)
    assert r.returncode == 0, r.stdout + r.stderr
    composed = [c for c in calls if c["args"][:1] == ["compose"]]
    assert composed and all(c["env"].get("TRIPBOX_IMAGE") == IMAGE for c in composed)
    assert IMAGE not in (tmp_path / "env-at-up").read_text()
    assert "TRIPBOX_IMAGE" not in (tmp_path / "env-at-up").read_text()
    # no setting of the caller's reaches compose's interpolation
    r, _, calls = run(tmp_path, SMOKE, "--image", IMAGE, TRIPBOX_PASSWORD="from-the-caller",
                      COMPOSE_PROJECT_NAME="another")
    assert r.returncode == 0, r.stdout + r.stderr
    for c in calls:
        assert "TRIPBOX_PASSWORD" not in c["env"] and "COMPOSE_PROJECT_NAME" not in c["env"]


def test_the_folders_are_handed_to_user_1000_through_the_image_when_the_runner_is_another(
    tmp_path,
):
    r, lines, calls = run(tmp_path, SMOKE, "--image", IMAGE)
    assert r.returncode == 0, r.stdout + r.stderr
    runs = [c["args"] for c in calls if c["args"][:1] == ["run"]]
    if os.getuid() == 1000:
        assert runs == []
        return
    chown, remove = runs
    assert chown[:7] == ["run", "--rm", "--network", "none", "--user", "0:0", "-v"]
    assert chown[8:] == [IMAGE, "chown", "1000:1000", "/work/tripbox/data", "/work/tripbox/archive",
                         "/work/tripbox/logs", "/work/tripbox/backups"]
    assert remove[8:] == [IMAGE, "rm", "-rf", "/work/tripbox"]
    # handed over before the stack starts, taken back after it is down
    order = [line.split()[:2] for line in lines if not line.startswith(("curl", "page"))]
    assert order.index(["run", "--rm"]) < order.index(["compose", "-p"])
    assert lines.index(next(x for x in lines if x.endswith("down -v --remove-orphans"))) < \
        lines.index(next(x for x in lines if " rm -rf " in x))


@pytest.mark.parametrize("inspected, why", [
    (inspect(policy="no"), "restart policy"),
    (inspect(web_host="0.0.0.0"), "published"),
    (inspect(web_host=""), "published"),
    (inspect(worker_ports={"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9000"}]}),
     "published"),
    ("[]", "containers"),
    ("not json", "JSON"),
], ids=["restart-no", "every-interface", "no-host-ip", "worker-published", "none", "garbage"])
def test_the_published_step_holds_the_port_to_loopback_and_the_restart_policy(tmp_path, inspected,
                                                                               why):
    r, _, calls = run(tmp_path, SMOKE, "--image", IMAGE, inspected=inspected)
    assert r.returncode == 1
    assert r.stdout.splitlines()[-1] == "stack smoke: FAIL published"
    assert why in r.stderr
    assert compose_calls(calls)[-1] == ["compose", "-p", "tripbox-smoke", "down", "-v",
                                        "--remove-orphans"]


SMOKE_STEPS = [
    # (what fails, the step named, whether the stack was started)
    ({"STUB_FAIL": " up -d --wait"}, "start", True),
    ({"STUB_PAGE": "1"}, "page", True),
    ({"STUB_FAIL": "tripbox doctor"}, "doctor", True),
    ({"STUB_FAIL": " ps -q"}, "published", True),
    ({"STUB_IDS": "not-an-id"}, "published", True),
    ({"STUB_FAIL": " down -v"}, "teardown", True),
]
if os.getuid() != 1000:  # the folders are handed over only when the runner is another user
    SMOKE_STEPS.append(({"STUB_FAIL": " chown "}, "unpack", False))
BROWSER_STEPS = [
    ({"STUB_FAIL": "demo seed"}, "seed", False),
    ({"STUB_FAIL": "build -t"}, "browser", False),
    ({"STUB_FAIL": " up -d --wait"}, "start", True),
    ({"STUB_FAIL": "shoot.py"}, "shoot", True),
    ({"STUB_CHECKS": "7 of 8 ok"}, "shoot", True),
    ({"STUB_FAIL": " down -v"}, "teardown", True),
]
FLIGHTLOG_STEPS = [
    ({"STUB_FAIL": "--wait flightlogdb"}, "database", True),
    ({"STUB_FAIL": "up -d flightlog e2e"}, "start", True),
    ({"STUB_FAIL": "flightlogdb psql"}, "key", True),
    ({"STUB_FAIL": "python -m pytest"}, "tests", True),
    ({"STUB_FAIL": " down -v"}, "teardown", True),
]
CASES = ([(SMOKE, ("--image", IMAGE), "stack smoke", *c) for c in SMOKE_STEPS]
         + [(BROWSER, ("--image", IMAGE), "browser checks", *c) for c in BROWSER_STEPS]
         + [(FLIGHTLOG, (), "flight log e2e", *c) for c in FLIGHTLOG_STEPS])


@pytest.mark.parametrize("script, args, prefix, stub, step, started", CASES,
                         ids=[f"{c[2].replace(' ', '-')}-{c[4]}-{i}" for i, c in enumerate(CASES)])
def test_each_script_names_its_failing_step_and_always_takes_its_stack_down(
    tmp_path, script, args, prefix, stub, step, started
):
    r, lines, calls = run(tmp_path, script, *args, **stub)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.splitlines()[-1] == f"{prefix}: FAIL {step}"
    downs = [line for line in lines if line.endswith(" down -v --remove-orphans")]
    composed = [line for line in lines if line.startswith("compose ")]
    if started:
        assert downs and composed[-1] == downs[-1], "the stack is taken down last"
        if step == "teardown":
            assert "still up" in r.stderr
    else:
        assert not [line for line in lines if " up " in f" {line} "], "nothing was started"
    assert not list((tmp_path / "scratch").iterdir()), "the scratch folder is removed"


def test_each_script_passes_with_every_step(tmp_path):
    for script, args, last in ((BROWSER, ("--image", IMAGE), "browser checks: ok"),
                               (FLIGHTLOG, (), "flight log e2e: ok")):
        r, _, _ = run(tmp_path, script, *args)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.splitlines()[-1] == last


# ---------------------------------------------------------------- the browser checks


def test_the_browser_checks_run_the_screenshot_stacks_four_commands_over_a_seeded_demo(tmp_path):
    r, lines, calls = run(tmp_path, BROWSER, "--image", IMAGE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[-3:] == ["checks: 8 of 8 ok", "screens: ok", "browser checks: ok"]
    seed, build = (c for c in calls if c["args"][:1] in (["run"], ["build"]))
    assert seed["args"][:6] == ["run", "--rm", "--network", "none", "--user",
                                f"{os.getuid()}:{os.getgid()}"]
    assert seed["args"][-8:] == [IMAGE, "python", "-m", "tripbox", "demo", "seed", "--out",
                                 "/work/demo"]
    assert build["args"][:3] == ["build", "-t", "tripbox-screens-browser:local"]
    assert build["args"][-1].endswith("tools/verify/screens")
    composed = compose_calls(calls)
    assert composed == [
        ["compose", "-p", "tripbox-screens", "up", "-d", "--wait", "web", "browser"],
        ["compose", "-p", "tripbox-screens", "exec", "-T", "browser", "python3",
         "/harness/shoot.py", "--base-url", "http://web:8110", "--out", "/out"],
        ["compose", "-p", "tripbox-screens", "down", "-v", "--remove-orphans"],
    ]
    for c in calls:
        if c["args"][:1] == ["compose"]:
            folder = Path(c["cwd"])
            assert c["env"]["SCREENS_TRIPBOX_IMAGE"] == IMAGE
            assert c["env"]["SCREENS_BROWSER_IMAGE"] == "tripbox-screens-browser:local"
            assert c["env"]["SCREENS_USER"] == f"{os.getuid()}:{os.getgid()}"
    # the folder the stack ran in is the one the harness was copied to, as the README says
    assert folder.name.startswith("tripbox-screens.")
    assert seed["args"][7] == f"{folder}:/work"


# ---------------------------------------------------------------- the flight log runner


def test_the_flight_log_runner_passes_five_keys_by_name_and_prints_none(tmp_path):
    r, lines, calls = run(tmp_path, FLIGHTLOG)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[-1] == "flight log e2e: ok"
    (test,) = [c for c in calls if "pytest" in c["args"]]
    keys = [test["env"].get(name, "") for name in KEYS]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", k) for k in keys) and len(set(keys)) == 5
    for k in keys:
        assert k not in "\n".join(lines) and k not in r.stdout + r.stderr
    assert test["args"] == [
        "compose", "-p", "tripbox-e2e", "-f", str(HERE / "airtrail" / "compose.yml"), "exec", "-T",
        "e2e", "python", "-m", "pytest", "-p", "no:cacheprovider", "-rN", "--tb=short",
        "/app/tests/test_flightlog_e2e.py", "/app/tests/test_flightlog_matrix.py"]
    assert test["env"]["E2E_TRIPBOX_IMAGE"] == "tripboxoss:test"
    assert test["env"]["E2E_AIRTRAIL_PORT"] == "18090"
    psql = [c for c in calls if "psql" in c["args"]]
    assert len(psql) == 5
    assert all(c["args"][:6] == ["compose", "-p", "tripbox-e2e", "-f",
                                 str(HERE / "airtrail" / "compose.yml"), "exec"] for c in psql)
    # the image may be another one
    r, _, calls = run(tmp_path, FLIGHTLOG, "--image", IMAGE)
    assert r.returncode == 0 and {c["env"]["E2E_TRIPBOX_IMAGE"] for c in calls} == {IMAGE}
