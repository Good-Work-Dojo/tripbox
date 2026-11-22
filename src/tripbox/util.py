# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Small shared helpers (the clock and time formatting, month keys, the process zone, reading a
small file)."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path

UTC = dt.UTC
# The most a user's settings file (travellers, list overrides) may hold: a few lines, never more.
SMALL_FILE_BYTES = 64 * 1024
# The instant `clock_at` holds the clock at (in UTC), or None while the clock runs.
_held: dt.datetime | None = None


def read_small_file(path: Path) -> bytes | None:
    """The bytes of a small file a user writes; None when nothing is at `path`. Anything else that
    is not a regular file of at most SMALL_FILE_BYTES that may be read raises OSError: a link
    whose target is missing (not an absent file), a directory, a pipe, a larger file, one without
    read permission. It opens without blocking, so a pipe never stalls it, checks the open
    descriptor before reading, reads at most one byte past the limit and closes the descriptor
    on every path."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        if os.path.lexists(path):
            raise  # a link whose target is not there is not an absent file
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > SMALL_FILE_BYTES:
            raise OSError("not a regular file of at most the size limit")
        chunks: list[bytes] = []
        left = SMALL_FILE_BYTES + 1
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > SMALL_FILE_BYTES:
        raise OSError("grew past the size limit")
    return raw


def now() -> dt.datetime:
    """The time in UTC: the real clock, or the instant `clock_at` holds. Every time the ledger
    stores comes from here."""
    held = _held
    return held if held is not None else dt.datetime.now(UTC)


@contextlib.contextmanager
def clock_at(when: dt.datetime) -> Iterator[None]:
    """While inside, `now()` returns `when` (an aware time) in UTC, and `now_iso()` and
    `today_local()` follow it, also where a module imported them by name. Holds nest: the inner
    one wins, and the one before returns on the way out, also on an exception. Process-wide, so
    it is for one-shot commands and tests, never a server."""
    global _held
    if when.utcoffset() is None:
        raise ValueError("clock_at needs a time with a zone")
    before = _held
    _held = when.astimezone(UTC)
    try:
        yield
    finally:
        _held = before


def now_iso() -> str:
    """UTC ISO-8601 with seconds, e.g. 2031-03-14T09:15:00+00:00 (what the ledger stores)."""
    return now().isoformat(timespec="seconds")


def month_key(when: dt.datetime | None = None) -> str:
    return (when or now()).strftime("%Y-%m")


def use_zone(tz: str) -> None:
    """Make `tz` the process's zone: local time here (today_local, the page's day labels) and in
    every subprocess started from now on (kitinerary-extractor) means the configured zone, not
    whatever the process started with (the image says UTC)."""
    os.environ["TZ"] = tz
    time.tzset()


def today_local() -> dt.date:
    """Today in the process's zone (the configured one, see use_zone) — what "upcoming" means."""
    return now().astimezone().date()


def merge_payload(old: dict, new: dict) -> dict:
    """Stored payload after an update: new values win, but a partial email (one passenger's receipt,
    a cancellation without times) never erases what an earlier email established."""
    out = dict(old)
    for k, v in new.items():
        if v in (None, "", [], {}):
            continue
        if k in ("passengers", "seats") and isinstance(out.get(k), list):
            out[k] = list(dict.fromkeys([*out[k], *v]))
        elif k == "seat_by_passenger" and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def parse_iso(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)
