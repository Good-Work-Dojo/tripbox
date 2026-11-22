# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What the worker (`tripbox work`) says of itself: the record it writes under the ledger's `kv`
row KV_WORKER at every beat, and whether one is running. It reads the ledger and the clock and
nothing else, so the page's container takes it with no code of the worker's.

The record is JSON: `pid` and `host` (the process that writes it), `started_at` and `beat_at`
(UTC, ISO-8601 to the second), `busy` (true while a part that may run long is under way: reading
an upload, a sync pass), `stopped_at` (when the worker stopped and gave the ledger up; null while
it runs), `features` (each of mail, model, airtrail, notifications and enrichment: on, off or
invalid), `last` (each of poll, enrich, sync and uploads: null until it first ends, then
`at`, `ok` and `code`, a word such as `exit_1`, `timeout`, `outage` or `key_rejected`, never a
message), `mailbox` (the address mail is read from while mail is on, else null: the one setting
it carries, which only a signed-in page shows), `waiting` (how many messages wait for the model,
by reason) and `dry_run` (whether nothing is sent to the flight log)."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

from tripbox import util

if TYPE_CHECKING:
    from tripbox.ledger import Ledger

KV_WORKER = "worker"
STALE_SECONDS = 30  # a record whose last beat is this far from now is a worker that stopped
# the same for a record written as a part that may run long began: longer than one message's
# longest reading (the extractor's time, then the model's over every retry)
BUSY_SECONDS = 900


def read(ledger: Ledger) -> dict | None:
    """The worker's record; None when none was written, or what is there is no record."""
    text = ledger.kv.get(KV_WORKER)
    if not text:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def live(record: dict | None, now: dt.datetime) -> bool:
    """Whether `record` is a worker's that runs: it did not stop, and its `beat_at` is less than
    STALE_SECONDS from `now` (BUSY_SECONDS while it is `busy`), before it or after it (a clock
    set back never keeps a stopped worker alive)."""
    if record is None or record.get("stopped_at"):
        return False
    try:
        beat = util.parse_iso(str(record.get("beat_at")))
    except ValueError:
        return False
    limit = BUSY_SECONDS if record.get("busy") is True else STALE_SECONDS
    return abs((now - beat).total_seconds()) < limit


def alive(ledger: Ledger, now: dt.datetime) -> bool:
    """Whether a worker runs on the ledger (`live`)."""
    return live(read(ledger), now)
