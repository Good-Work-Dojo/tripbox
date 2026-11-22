# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The health check the compose file's services run: the worker's follows its heartbeat in the
ledger (the compose file itself is pinned by test_compose.py)."""

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

from tripbox import status
from tripbox.config import CONTAINER_PORT, KEY_BY_NAME
from tripbox.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
HEALTHCHECK = ROOT / "scripts" / "healthcheck.py"


def run_check(data_dir: Path, mode: str | None = None) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(HEALTHCHECK)] + ([mode] if mode else [])
    env = {**os.environ, "TRIPBOX_DATA_DIR": str(data_dir)}
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=60, check=False)


def write_record(data_dir: Path, **fields) -> None:
    ledger = Ledger.open(data_dir / "ledger.db")
    ledger.kv.set(status.KV_WORKER, json.dumps(fields))
    ledger.conn.commit()
    ledger.close()


def stamp(seconds_ago: int) -> str:
    when = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_health_check_follows_the_workers_heartbeat(tmp_path):
    assert run_check(tmp_path).returncode == 1  # no ledger
    assert run_check(tmp_path, "worker").returncode == 1

    write_record(tmp_path, beat_at=stamp(0), stopped_at=None)
    now = run_check(tmp_path)
    assert (now.returncode, now.stdout.strip()) == (0, "ok")
    assert run_check(tmp_path, "worker").returncode == 0

    write_record(tmp_path, beat_at=stamp(31), stopped_at=None)
    assert run_check(tmp_path).returncode == 1

    # a long part is under way: live for as long as the page says
    write_record(tmp_path, beat_at=stamp(60), busy=True, stopped_at=None)
    assert run_check(tmp_path).returncode == 0

    # released on a clean stop: not live, however fresh the beat
    write_record(tmp_path, beat_at=stamp(0), stopped_at=stamp(0))
    assert run_check(tmp_path).returncode == 1

    # the mail poll's outcome is not part of health
    write_record(tmp_path, beat_at=stamp(0), stopped_at=None, last={"poll": {"ok": False}})
    assert run_check(tmp_path).returncode == 0


def test_the_bot_and_web_modes_are_unchanged(tmp_path):
    assert run_check(tmp_path, "bot").returncode == 1
    beat = tmp_path / "bot.heartbeat"
    beat.write_text("x")
    assert run_check(tmp_path, "bot").returncode == 0
    old = beat.stat().st_mtime - 400
    os.utime(beat, (old, old))
    assert run_check(tmp_path, "bot").returncode == 1
    web = subprocess.run(
        [sys.executable, str(HEALTHCHECK), "web"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert web.returncode == 1 and "healthz" in web.stdout  # nothing listens here


def test_the_health_check_repeats_the_registrys_defaults():
    """The check is stdlib only for the page, so it writes out what the registry holds: the data
    folder's default and the port the page listens on inside the image; no setting moves it."""
    text = HEALTHCHECK.read_text()
    assert f'"{KEY_BY_NAME["TRIPBOX_DATA_DIR"].default}"' in text
    assert f"http://127.0.0.1:{CONTAINER_PORT}/healthz" in text
    assert "TRIPBOX_PORT" not in text and "TRIPBOX_LISTEN" not in text
