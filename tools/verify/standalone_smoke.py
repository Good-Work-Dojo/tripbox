# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A brand-new install with nothing but the core settings, end to end. Run it in a container of
the test or the runtime image (the Dockerfile's `test` or `runtime` target), as any user, with no
network, a read-only root file system and a writable /tmp, the repository mounted read-only at
/repo as the working directory, and these environment variables:

    PYTHONDONTWRITEBYTECODE=1 TRIPBOX_DATA_DIR=/tmp/data TRIPBOX_LOGS_DIR=/tmp/logs
    TRIPBOX_ARCHIVE_DIR=/tmp/archive TRIPBOX_CONFIG_DIR=/tmp/config

as `python /repo/tools/verify/standalone_smoke.py`. The page signs in with its own password by
default, the one setting a new install must make: every command it runs is given SMOKE_PASSWORD
as TRIPBOX_PASSWORD, unless the environment sets a password or its hash. It creates the empty
config directory, then checks that `doctor` exits 0 with every optional feature off, no
travellers file and no problem; that `poll` exits 0 saying mail is off; that `work` starts and the
health check says `ok` within fifteen seconds; and that `web` answers its health check and,
signed in through its login form, serves the first run's trips page. The last line is
`standalone smoke: ok` (exit 0) or `standalone smoke: FAIL <doctor|poll|worker|web|page>` (exit 1),
whatever a step raises; the web and worker children are stopped either way. Standard library
only. It prints no environment value and none of the commands' output.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OFF = ("mail", "model", "airtrail", "notifications", "enrichment")
HEALTH_SECONDS = 15.0
EMPTY_PAGE = "No trips yet"  # a first run's list
SMOKE_PASSWORD = "smoke-only-password"  # the page's password, unless the environment sets one
HEALTHCHECK = Path(__file__).resolve().parents[2] / "scripts" / "healthcheck.py"


class Failed(Exception):
    """A step that did not do what a new install must; its argument is the step's name."""


def child_env() -> dict[str, str]:
    """The environment every command runs with: this one, and SMOKE_PASSWORD as the page's
    password when it sets neither a password nor its hash."""
    env = dict(os.environ)
    if not (env.get("TRIPBOX_PASSWORD") or env.get("TRIPBOX_PASSWORD_HASH")):
        env["TRIPBOX_PASSWORD"] = SMOKE_PASSWORD
    return env


def tripbox(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tripbox", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=child_env(),
    )


def row(name: str, state: str) -> str:
    """The start of a `doctor` row, laid out as the command prints it."""
    return f"{name:<15}{state}"


def check_doctor() -> None:
    r = tripbox("doctor")
    lines = r.stdout.splitlines()
    wanted = [row(name, "off") for name in OFF] + [row("travellers", "none")]
    has_rows = all(any(line.startswith(w) for line in lines) for w in wanted)
    if r.returncode != 0 or not has_rows or "problems: 0" not in lines:
        raise Failed("doctor")


def check_poll() -> None:
    r = tripbox("poll")
    if r.returncode != 0 or "mail is off:" not in r.stdout:
        raise Failed("poll")


def fetch(url: str, cookie: str = "") -> tuple[int, str]:
    """One GET to the child on the loopback address, with `cookie` as its Cookie header."""
    request = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    with urllib.request.urlopen(request, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


class _Stay(urllib.request.HTTPRedirectHandler):
    """Follows no redirect: the answer to a sign-in is the one that carries its cookie."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sign_in(base: str, password: str) -> str:
    """Sign in through the page's login form, as a browser on the page posts it: the Cookie
    header of the session it made, or "" (no login, or a refusal)."""
    request = urllib.request.Request(
        f"{base}/login",
        data=urllib.parse.urlencode({"password": password}).encode(),
        headers={
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(_Stay).open(request, timeout=30) as resp:
            lines = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as answer:  # the 303 that sets the cookie, or a refusal
        lines = answer.headers.get_all("Set-Cookie") or []
    for line in lines:
        name, _, value = line.split(";", 1)[0].strip().partition("=")
        if name.endswith("tripbox_session") and value:
            return f"{name}={value}"
    return ""


def healthy(base: str, child: subprocess.Popen) -> bool:
    """Whether the child answers its health check `ok` within HEALTH_SECONDS."""
    deadline = time.monotonic() + HEALTH_SECONDS
    while time.monotonic() < deadline and child.poll() is None:
        try:
            status, body = fetch(f"{base}/healthz")
            if status == 200 and body.strip() == "ok":
                return True
        except Exception:  # not listening yet, or an answer that is not the health check's
            pass
        time.sleep(0.25)
    return False


def worker_healthy(child: subprocess.Popen) -> bool:
    """Whether the health check says `ok` for the worker child within HEALTH_SECONDS."""
    deadline = time.monotonic() + HEALTH_SECONDS
    while time.monotonic() < deadline and child.poll() is None:
        r = subprocess.run(
            [sys.executable, str(HEALTHCHECK), "worker"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip() == "ok":
            return True
        time.sleep(0.25)
    return False


def stop(child: subprocess.Popen) -> None:
    """Stop a child, however its step ended; never raises."""
    try:
        child.terminate()
        child.wait(timeout=10)
    except Exception:
        try:
            child.kill()
            child.wait(timeout=10)
        except Exception:
            pass


def check_worker() -> None:
    child = subprocess.Popen(  # a child that cannot be started fails the worker step
        [sys.executable, "-m", "tripbox", "work"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_env(),
    )
    try:
        if not worker_healthy(child):
            raise Failed("worker")
    finally:
        stop(child)


def check_web(port: int) -> None:
    base = f"http://127.0.0.1:{port}"  # TRIPBOX_BASE_URL unset: the page is at the root
    env = child_env()
    child = subprocess.Popen(  # a child that cannot be started fails the web step
        [sys.executable, "-m", "tripbox", "web", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        if not healthy(base, child):
            raise Failed("web")
        try:
            cookie = sign_in(base, env.get("TRIPBOX_PASSWORD", ""))
            status, body = fetch(f"{base}/", cookie)
        except Exception:  # an error status, a broken or non-HTTP answer, or none at all
            raise Failed("page") from None
        if status != 200 or EMPTY_PAGE not in body:
            raise Failed("page")
    finally:
        stop(child)


def run_steps(port: int) -> str | None:
    """The name of the first step that fails, or None. Whatever a step raises (a timeout, an
    answer that is not HTTP, a child that will not start) fails that step."""
    step = "doctor"
    try:
        Path(os.environ["TRIPBOX_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
        check_doctor()
        step = "poll"
        check_poll()
        step = "worker"
        check_worker()
        step = "web"
        check_web(port)
    except Failed as exc:
        return exc.args[0]
    except Exception:
        return step
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8110, help="the port `web` listens on")
    args = parser.parse_args(argv)
    failed = run_steps(args.port)
    if failed:
        print(f"standalone smoke: FAIL {failed}")
        return 1
    print("standalone smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
