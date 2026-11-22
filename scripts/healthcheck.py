#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Container healthcheck. The worker mode reads the ledger through `tripbox.status`, so the page
and the check agree on liveness; the bot and web modes are stdlib only.

worker (default):    the ledger's `worker` record is live by the page's own rule
                     (`tripbox.status.live`): a beat less than 30 s old (900 s while busy),
                     not stopped. The mail poll's outcome is not part of health.
bot:                 data/bot.heartbeat mtime < 3 min.
web:                 GET http://127.0.0.1:8110/healthz -> 200 "ok": the port the image's page
                     listens on (config.CONTAINER_PORT), on every interface; the page answers
                     its own paths without the base path too.
"""
import datetime as dt
import os
import sqlite3
import sys
import time

DATA = os.environ.get("TRIPBOX_DATA_DIR", "/app/data")
mode = sys.argv[1] if len(sys.argv) > 1 else "worker"


def fail(msg: str) -> None:
    print(f"UNHEALTHY: {msg}")
    sys.exit(1)


if mode == "web":
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8110/healthz", timeout=10) as r:
            body = r.read(16).decode()
    except Exception as exc:  # noqa: BLE001
        fail(f"healthz: {exc}")
    if body.strip() != "ok":
        fail(f"healthz body {body!r}")
    print("ok")
    sys.exit(0)

if mode == "bot":
    path = os.path.join(DATA, "bot.heartbeat")
    try:
        age = time.time() - os.stat(path).st_mtime
    except FileNotFoundError:
        fail("no heartbeat file yet")
    if age > 180:
        fail(f"heartbeat {age:.0f}s old")
    print("ok")
    sys.exit(0)

db = os.path.join(DATA, "ledger.db")
if not os.path.exists(db):
    fail("ledger not created yet")


class _Kv:
    """Just enough of a ledger for `status.read`: its `kv.get`, over a read-only connection."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.kv = self
        self._con = con

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._con.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return row[0] if row else default


try:
    from tripbox import status

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        record = status.read(_Kv(con))
    finally:
        con.close()
    live = status.live(record, dt.datetime.now(dt.UTC))
except Exception as exc:  # noqa: BLE001
    fail(f"ledger: {exc}")
if not live:
    fail("no live worker record")
print("ok")
