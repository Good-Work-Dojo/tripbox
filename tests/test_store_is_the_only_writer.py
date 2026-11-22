# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Trips and their bookings are written by the trip store alone (through the ledger's
repositories): no other module of the app holds a statement or a repository call that writes
either table, so every write of a booking ends where the store settles it. The log of what each
message did is written once: the ledger's repository holds the only statements that change a row
of it."""

import re
from pathlib import Path

import tripbox
from tripbox.ledger import repo

# matched without regard to case, as SQL keywords may be written in any
WRITES = re.compile(
    r"trip_items\.(?:insert|update|delete)\("
    r"|\btrips\.(?:insert|update|delete)\("
    r"|\binsert\s+(?:or\s+\w+\s+)?into\s+(?:trips|trip_items)\b"
    r"|\breplace\s+into\s+(?:trips|trip_items)\b"
    r"|\bupdate\s+(?:or\s+\w+\s+)?(?:trips|trip_items)\s+set\b"
    r"|\bdelete\s+from\s+(?:trips|trip_items)\b",
    re.IGNORECASE,
)
WRITERS = ("trips/store.py", "ledger/repo.py")
LOG_CHANGES = re.compile(r"\bupdate\s+(?:or\s+\w+\s+)?items\s+set\b", re.IGNORECASE)


def _sources() -> list[tuple[str, str]]:
    root = Path(tripbox.__file__).resolve().parent
    return [
        (path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
    ]


def test_only_the_store_writes_trips_and_trip_items():
    found: dict[str, list[str]] = {}
    seen: dict[str, list[str]] = {}
    for name, text in _sources():
        hits = WRITES.findall(text)
        (seen if name in WRITERS else found).update({name: hits} if hits else {})
    assert found == {}
    # the patterns find the writes where they belong, so an empty dict above means something
    assert set(seen) == set(WRITERS)
    assert any(re.match(r"(?i)delete from trip_items", h) for h in seen["ledger/repo.py"])
    assert any(re.match(r"(?i)delete from trips\b", h) for h in seen["ledger/repo.py"])


def test_a_log_row_is_written_once():
    found = {
        name: len(hits) for name, text in _sources() if (hits := LOG_CHANGES.findall(text))
    }
    # `repoint` (two bookings became one) and `forget` (the booking was purged)
    assert found == {"ledger/repo.py": 2}
    assert not hasattr(repo.Items, "update")
    # nothing hands the log's table to the repositories' generic UPDATE helper either
    assert all(
        not re.search(r"_update\([^)]*[\"']items[\"']", text) for _, text in _sources()
    )
