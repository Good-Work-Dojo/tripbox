# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
import hashlib
import logging
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest
import uvicorn
from helpers.sample import CONFIG_DIR

from tripbox import cli, util
from tripbox.ledger import Ledger, db
from tripbox.trips.store import TripStore
from tripbox.validate import checks

CURRENT = db.SCHEMA_VERSION  # the version this code writes
NEXT = CURRENT + 1  # the synthetic version the upgrade tests invent beyond it

OUTDATED = (
    "this ledger was written by an earlier development version of tripbox and this version"
    " cannot read it; move the file away and start again with an empty one"
)


def test_open_creates_schema_and_is_idempotent(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == CURRENT
        tables = {
            r[0] for r in led.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "messages",
            "items",
            "kv",
            "ref_cache",
            "aeroapi_calls",
            "enrichment",
        } <= tables
        assert "review_queue" not in tables
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == CURRENT
        assert led.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_reading_a_message_again_replaces_its_rows(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        mid = led.messages.insert(sha256="s", source="imap", status="archived")
        other = led.messages.insert(sha256="t", source="imap", status="processed")
        log = dict(kind="flight", item_key="k", payload={})
        filed = led.items.add(message_id=mid, n=0, outcome="filed", trip_item_id=1, **log)
        failed = led.items.add(message_id=mid, n=1, outcome="failed", **log)
        kept = led.items.add(message_id=other, n=0, outcome="duplicate", trip_item_id=1, **log)
        # the message's rows go, and no other message's
        assert sorted(led.items.delete_for_message(mid)) == [filed, failed]
        assert led.items.by_message(mid) == [] and led.items.get(failed) is None
        assert [r["id"] for r in led.items.by_trip_item(1)] == [kept]
        # the new reading writes its own
        again = led.items.add(message_id=mid, n=0, outcome="duplicate", trip_item_id=1, **log)
        assert [r["id"] for r in led.items.by_message(mid)] == [again]
        assert led.items.delete_for_message(mid) == [again]


def test_kv_set_overwrites(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        assert led.kv.get("last_uid") is None
        led.kv.set("last_uid", "5")
        led.kv.set("last_uid", "9")
        assert led.kv.get("last_uid") == "9"


def test_the_log_keeps_one_row_per_booking_per_message(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        first = led.messages.insert(
            sha256="abc", source="cli", status="archived", message_id="<m1>", mail_uid=7
        )
        later = led.messages.insert(sha256="def", source="cli", status="archived")
        key = "DL|1234|2027-06-22|MSP"
        stay = led.items.add(
            message_id=first,
            n=1,
            kind="lodging",
            item_key="lodging|ABC123",
            outcome="filed",
            payload={"pnr": "ABC123"},
            trip_item_id=2,
        )
        flight = led.items.add(
            message_id=first,
            n=0,
            kind="flight",
            item_key=key,
            outcome="filed",
            payload={"carrier": "DL"},
            confidence=0.9,
            trip_item_id=1,
        )
        again = led.items.add(
            message_id=later,
            n=0,
            kind="flight",
            item_key=key,
            outcome="updated",
            detail="add_passengers",
            payload={"carrier": "DL", "passengers": ["Sam Sample"]},
            trip_item_id=1,
        )
        assert led.messages.get(first)["mail_uid"] == 7
        # a message's rows in the order of its bookings; a booking's rows, one per message that
        # touched it, oldest first
        assert [r["id"] for r in led.items.by_message(first)] == [flight, stay]
        assert [r["id"] for r in led.items.by_trip_item(1)] == [flight, again]
        row = led.items.get(again)
        assert (row["message_id"], row["n"], row["kind"]) == (later, 0, "flight")
        assert (row["item_key"], row["outcome"]) == (key, "updated")
        assert (row["detail"], row["confidence"]) == ("add_passengers", None)
        assert led.items.payload(row) == {"carrier": "DL", "passengers": ["Sam Sample"]}
        assert row["created_at"] and led.items.get(flight)["confidence"] == 0.9
        assert led.items.get(stay)["detail"] is None
        # two bookings became one: the log follows the one that stays; a purged booking is
        # forgotten, its rows kept
        led.items.repoint(2, 1)
        assert [r["id"] for r in led.items.by_trip_item(1)] == [stay, flight, again]
        assert led.items.by_trip_item(2) == []
        led.items.forget(1)
        assert led.items.by_trip_item(1) == [] and led.items.get(flight)["trip_item_id"] is None
        assert [r["id"] for r in led.items.by_message(first)] == [flight, stay]


def test_the_log_takes_only_its_outcomes():
    led = Ledger.memory()
    mid = led.messages.insert(sha256="s", source="cli", status="archived")
    outcomes = ("filed", "updated", "duplicate", "ignored", "failed")
    row = dict(message_id=mid, kind="flight", item_key="k", payload={})
    for n, outcome in enumerate(outcomes):
        led.items.add(n=n, outcome=outcome, **row)
    for n, word in enumerate(("written", "held"), start=len(outcomes)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            led.items.add(n=n, outcome=word, **row)
    assert tuple(r["outcome"] for r in led.items.by_message(mid)) == outcomes


def test_duplicate_sha_is_rejected(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        led.messages.insert(sha256="same", source="imap", status="archived")
        try:
            led.messages.insert(sha256="same", source="imap", status="archived")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate sha256 must be rejected")
        assert led.messages.by_sha("same") is not None


def test_concurrent_writer_waits_instead_of_raising(tmp_ledger_path):
    """A second connection's write must wait (busy_timeout) for the holder's txn, not fail."""
    holder = Ledger.open(tmp_ledger_path)
    holder.conn.execute("BEGIN IMMEDIATE")
    holder.conn.execute("INSERT INTO kv(k, v) VALUES ('x', '1')")
    elapsed = {}

    def writer():
        other = Ledger.open(tmp_ledger_path)  # its own connection (SQLite objects are per-thread)
        t0 = time.monotonic()
        other.kv.set("y", "2")  # blocks until the holder commits
        elapsed["s"] = time.monotonic() - t0
        other.close()

    th = threading.Thread(target=writer)
    th.start()
    time.sleep(0.5)
    holder.conn.execute("COMMIT")
    th.join(timeout=15)
    assert not th.is_alive(), "writer never finished"
    assert elapsed["s"] >= 0.4
    assert holder.kv.get("x") == "1" and holder.kv.get("y") == "2"
    holder.close()


def test_ref_cache_expires(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        led.refcache.put("airport", "MSP", {"id": 9001})
        assert led.refcache.get("airport", "MSP") == {"id": 9001}
        led.conn.execute("UPDATE ref_cache SET fetched_at='2020-01-01T00:00:00+00:00'")
        assert led.refcache.get("airport", "MSP") is None


def test_trip_repos_round_trip():
    led = Ledger.memory()
    tid = led.trips.insert(
        title="Philadelphia",
        place="Philadelphia",
        start_date="2027-09-16",
        end_date="2027-09-21",
        id=10,
    )
    assert tid == 10 and led.trips.get(10)["title"] == "Philadelphia"
    led.trips.update(10, notes="gate code 1234")
    assert led.trips.get(10)["notes"] == "gate code 1234"
    iid = led.trip_items.insert(
        trip_id=10,
        item_key="SY|9304|2027-09-16|MSP",
        kind="flight",
        status="confirmed",
        start_at="2027-09-16T10:25",
        end_at="2027-09-16T13:58",
        start_tz="America/Chicago",
        end_tz="America/New_York",
        confirmation="QTWZSV",
        payload={"flight_code": "SY9304"},
        passengers=["Alex Sample"],
        airtrail_flight_id=10,
        source_message_id=None,
    )
    row = led.trip_items.by_key("SY|9304|2027-09-16|MSP")
    assert row["id"] == iid and led.trip_items.payload(row) == {"flight_code": "SY9304"}
    led.trip_items.update(
        iid,
        payload={"flight_code": "SY9304", "seats": ["8A"]},
        passengers=["Alex Sample", "Sam Sample"],
    )
    row = led.trip_items.get(iid)
    assert led.trip_items.passengers(row) == ["Alex Sample", "Sam Sample"]
    assert [r["id"] for r in led.trip_items.by_trip(10)] == [iid]
    assert led.trip_items.by_pnr("QTWZSV")[0]["id"] == iid
    assert [t["id"] for t in led.trips.all()] == [10]
    led.trip_items.delete(iid)
    led.trips.delete(10)
    assert led.trips.all() == [] and led.trip_items.by_trip(10) == []


EXPECTED_COLUMNS = {
    "messages": (
        "id message_id sha256 source mail_uid received_at email_date from_addr subject origin_ok"
        " archive_path status document_type parser error answer_json found_at processed_at"
        " created_at"
    ),
    "items": (
        "id message_id n kind item_key outcome detail payload_json confidence trip_item_id"
        " created_at"
    ),
    "ref_cache": "kind code json fetched_at",
    "llm_calls": "id message_id model input_tokens output_tokens cost_usd created_at",
    "aeroapi_calls": "id month item_id ident result_sets est_cost_usd created_at",
    "enrichment": "item_id attempts last_attempt enriched_at fields_set_json",
    "trips": "id title place start_date end_date notes deleted_at created_at updated_at",
    "trip_items": (
        "id trip_id item_key kind status start_at end_at start_tz end_tz confirmation"
        " payload_json passengers_json checks_json sync_state sync_hold airtrail_flight_id"
        " told_json sync_error sync_error_at sync_attempts sync_after sync_claimed_at"
        " sync_logged source_message_id removed_at created_at updated_at"
    ),
    "kv": "k v",
    "schema_version": "v",
    "uploads": (
        "id token kind name sha256 size body submitted_by state message_id error attempts"
        " created_at updated_at"
    ),
    "sessions": "token_hash cred created_at seen_at",
    "devices": "id_hash created_at seen_at",
    "share_links": "id trip_id token token_hash shows created_at revoked_at",
    "covers": (
        "trip_id source state place digest large small upload credit_json error attempts"
        " retry_after created_at updated_at"
    ),
}
EXPECTED_INDEXES = {
    "messages_mid",
    "items_key",
    "items_message",
    "items_trip_item",
    "trips_dates",
    "trip_items_trip",
    "trip_items_sync",
    "share_links_trip",
    "covers_digest",
}


def _shape(led):
    """The ledger's own tables with their columns, and its indexes; SQLite's own (its
    `sqlite_sequence`, which ids that are never given twice need) are not the ledger's."""
    tables = {
        r[0]
        for r in led.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    cols = {t: [r[1] for r in led.conn.execute(f"PRAGMA table_info({t})")] for t in tables}
    idx = {
        r[0]
        for r in led.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    return cols, idx


def test_fresh_ledger_is_the_current_schema(tmp_ledger_path):
    expected = {t: c.split() for t, c in EXPECTED_COLUMNS.items()}
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == CURRENT == 2
        assert _shape(led) == (expected, EXPECTED_INDEXES)
    with Ledger.memory() as led:
        assert led.version() == CURRENT
        assert _shape(led) == (expected, EXPECTED_INDEXES)


def _seed_current(path) -> None:
    with Ledger.open(path) as led:
        mid = led.messages.insert(sha256="s1", source="cli", status="processed")
        led.items.add(
            message_id=mid,
            n=0,
            kind="flight",
            item_key="ZZ|9001|2031-03-14|STL",
            outcome="filed",
            payload={},
        )
        led.trips.insert(
            title="Lisbon",
            place="Lisbon, Portugal",
            start_date="2031-03-14",
            end_date="2031-03-21",
            id=10,
        )
        led.trip_items.insert(
            trip_id=10,
            item_key="ZZ|9001|2031-03-14|STL",
            kind="flight",
            status="confirmed",
            start_at="2031-03-14T09:30",
            end_at=None,
            start_tz=None,
            end_tz=None,
            confirmation="ZZTEST",
            payload={},
            passengers=["Alex Sample"],
        )


def _with_a_note(schema: str) -> str:
    """The schema with one more column on trips, and an index on it."""
    end = "  updated_at TEXT NOT NULL\n);\nCREATE INDEX IF NOT EXISTS trips_dates"
    assert schema.count(end) == 1
    noted = schema.replace(
        end, end.replace("NOT NULL\n", "NOT NULL,\n  synthetic_note TEXT\n")
    )
    return noted + "CREATE INDEX IF NOT EXISTS trips_note ON trips(synthetic_note);\n"


def _add_the_note(conn) -> None:
    conn.execute("ALTER TABLE trips ADD COLUMN synthetic_note TEXT")
    conn.execute("CREATE INDEX trips_note ON trips(synthetic_note)")


@pytest.fixture
def next_schema(monkeypatch, tmp_ledger_path):
    """A file of this version with rows (`_seed_current`); then this code is the next version's: its
    schema adds a column with an index to trips (SCHEMA, SCHEMA_SHAPE), and this version's shape
    is a released one."""
    _seed_current(tmp_ledger_path)
    schema = _with_a_note(db.SCHEMA)
    monkeypatch.setattr(db, "RELEASED_SHAPES", {**db.RELEASED_SHAPES, CURRENT: db.SCHEMA_SHAPE})
    monkeypatch.setattr(db, "SCHEMA", schema)
    monkeypatch.setattr(db, "SCHEMA_SHAPE", _digest(schema))
    return schema


@pytest.fixture
def next_version(monkeypatch, next_schema):
    """The next version's step 2, which adds what its schema adds. It records the value of
    PRAGMA foreign_keys seen inside the step, once per call."""
    calls = []

    def step(conn):
        calls.append(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        _add_the_note(conn)

    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: step})
    return calls


def _trip_columns(led):
    return {r[1] for r in led.conn.execute("PRAGMA table_info(trips)")}


def test_a_next_version_migration_runs_once_and_keeps_rows(tmp_ledger_path, next_version):
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == NEXT
        assert "synthetic_note" in _trip_columns(led)
        assert led.trips.get(10)["title"] == "Lisbon"
        assert len(led.trip_items.by_trip(10)) == 1
        assert led.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert led.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert led.kv.get("schema_shape") == db.SCHEMA_SHAPE
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == NEXT
    assert next_version == [0]  # one call, with foreign keys off


def test_three_openers_racing_an_upgrade_leave_one_copy(
    monkeypatch, tmp_ledger_path, next_version
):
    # every opener has read version 1 before any of them takes the write lock, so the losers can
    # only skip the copy and the step by re-reading the version inside the transaction
    barrier = threading.Barrier(3)
    real_apply = Ledger._apply

    def apply_together(self, *args, **kwargs):
        barrier.wait(timeout=10)
        return real_apply(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "_apply", apply_together)
    errors: list[BaseException] = []

    def opener():
        try:
            with Ledger.open(tmp_ledger_path) as led:
                assert led.version() == NEXT
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=opener) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert len(next_version) == 1
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == NEXT
    assert len(_copies(tmp_ledger_path)) == 1


def test_a_failing_migration_leaves_the_ledger_as_it_was(
    monkeypatch, tmp_ledger_path, next_version
):
    def boom(conn):
        conn.execute("ALTER TABLE trips ADD COLUMN synthetic_note TEXT")
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: boom})
    with pytest.raises(RuntimeError, match="boom"):
        Ledger.open(tmp_ledger_path)
    con = sqlite3.connect(tmp_ledger_path)
    try:
        assert con.execute("SELECT v FROM schema_version").fetchone()[0] == CURRENT
        assert "synthetic_note" not in {r[1] for r in con.execute("PRAGMA table_info(trips)")}
    finally:
        con.close()

    # a step that leaves a dangling reference fails foreign_key_check and rolls back too
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        {**db.MIGRATIONS, NEXT: lambda conn: conn.execute("UPDATE trip_items SET trip_id=999")},
    )
    with pytest.raises(sqlite3.IntegrityError, match="foreign_key_check"):
        Ledger.open(tmp_ledger_path)
    con = sqlite3.connect(tmp_ledger_path)
    try:
        assert con.execute("SELECT v FROM schema_version").fetchone()[0] == CURRENT
        assert con.execute("SELECT trip_id FROM trip_items").fetchone()[0] == 10
    finally:
        con.close()


def test_a_step_that_commits_is_refused(monkeypatch, tmp_ledger_path, next_version):
    def committing(conn):
        conn.execute("ALTER TABLE trips ADD COLUMN synthetic_note TEXT")
        conn.execute("COMMIT")

    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: committing})
    with pytest.raises(RuntimeError, match="transaction"):
        Ledger.open(tmp_ledger_path)
    con = sqlite3.connect(tmp_ledger_path)
    try:
        assert con.execute("SELECT v FROM schema_version").fetchone()[0] == CURRENT
    finally:
        con.close()


def test_three_openers_race_a_brand_new_file(tmp_ledger_path):
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def opener():
        try:
            barrier.wait(timeout=10)
            with Ledger.open(tmp_ledger_path) as led:
                assert led.version() == CURRENT
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=opener) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    with Ledger.open(tmp_ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
        assert led.version() == CURRENT


def test_a_failed_open_releases_the_file(monkeypatch, tmp_ledger_path, next_version):
    def boom(conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: boom})
    with pytest.raises(RuntimeError, match="boom"):
        Ledger.open(tmp_ledger_path)
    con = sqlite3.connect(tmp_ledger_path, isolation_level=None, timeout=0)
    try:
        con.execute("BEGIN EXCLUSIVE")  # raises if another connection still holds the file
        con.execute("ROLLBACK")
    finally:
        con.close()


def _lisbon(led) -> None:
    led.trips.insert(
        title="Lisbon",
        place="Lisbon, Portugal",
        start_date="2031-03-14",
        end_date="2031-03-21",
        id=10,
    )


def _booking(led, key, *, trip_id, start_at) -> int:
    return led.trip_items.insert(
        trip_id=trip_id,
        item_key=key,
        kind="flight",
        status="confirmed",
        start_at=start_at,
        end_at=None,
        start_tz=None,
        end_tz=None,
        confirmation="ZZTEST",
        payload={},
        passengers=["Alex Sample"],
    )


def test_a_booking_or_a_trip_id_is_never_given_twice():
    """A button or a page names a booking or a trip by its id, so an id that went (a draft made
    one with its booking, a purge) is never given to another."""
    led = Ledger.memory()
    _lisbon(led)
    first = _booking(led, "k", trip_id=10, start_at="2031-03-14T09:30")
    led.trip_items.delete(first)
    led.trips.delete(10)
    trip = led.trips.insert(
        title="Lisbon", place=None, start_date="2031-03-14", end_date="2031-03-21"
    )
    assert trip > 10 and _booking(led, "k", trip_id=trip, start_at="2031-03-14T09:30") > first
    names = {r[0] for r in led.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sqlite_sequence" in names  # SQLite's own record of the ids given


def test_a_booking_has_no_trip_exactly_when_it_has_no_start():
    led = Ledger.memory()
    _lisbon(led)
    for trip_id, start_at in ((10, None), (None, "2031-03-14T09:30")):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _booking(led, "k", trip_id=trip_id, start_at=start_at)
    unsorted = _booking(led, "k", trip_id=None, start_at=None)
    filed = _booking(led, "k2", trip_id=10, start_at="2031-03-14T09:30")
    row = led.trip_items.get(unsorted)
    assert (row["trip_id"], row["start_at"]) == (None, None)
    item = TripStore._item(row)
    assert item.trip_id is None and item.start_at is None
    row = led.trip_items.get(filed)
    assert (row["trip_id"], row["start_at"]) == (10, "2031-03-14T09:30")
    # a change that empties one of the two alone is refused; emptying both is not
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        led.trip_items.update(filed, start_at=None)
    led.trip_items.update(filed, trip_id=None, start_at=None)
    assert led.trip_items.get(filed)["trip_id"] is None


def test_the_sync_columns_take_only_their_words():
    led = Ledger.memory()
    _lisbon(led)
    iid = _booking(led, "k", trip_id=10, start_at="2031-03-14T09:30")
    item = TripStore._item(led.trip_items.get(iid))
    assert (item.checks, item.sync_state, item.sync_hold, item.told, item.sync_error) == (
        (),
        "off",
        None,
        None,
        None,
    )
    row = led.trip_items.get(iid)
    assert (row["checks_json"], row["sync_attempts"]) == ("[]", 0)
    for column, word in (("sync_state", "held"), ("sync_hold", "x"), ("sync_error", "boom")):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            led.trip_items.update(iid, **{column: word})
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        led.trip_items.update(iid, sync_state=None)
    # the schema lists exactly these words, no more and no fewer (the covers table's own `state`
    # would shadow uploads'; its words are pinned by `test_covers_take_only_their_words`)
    listed = {
        column: tuple(re.findall(r"'(\w+)'", words))
        for column, words in re.findall(
            r"CHECK \((\w+) IN \(([^)]*)\)\)", db.SCHEMA.split("-- covers:")[0]
        )
    }
    assert listed == {
        "outcome": ("filed", "updated", "duplicate", "ignored", "failed"),
        "sync_state": checks.SYNC_STATES,
        "sync_hold": ("past", "deleted"),
        "sync_error": ("outage", "refused", "airport"),
        "kind": ("text", "eml", "pdf"),
        "state": ("waiting", "processing", "filed", "nothing", "failed"),
    }
    words = {
        "sync_state": checks.SYNC_STATES,
        "sync_hold": ("past", "deleted", None),
        "sync_error": ("outage", "refused", "airport", None),
    }
    for column, allowed in words.items():
        for word in allowed:
            led.trip_items.update(iid, **{column: word})
            assert led.trip_items.get(iid)[column] == word
    # the open checks and what the flight log was told are stored as JSON and read back
    led.trip_items.update(iid, checks=("carrier", "low_confidence"), told={"seats": ["12A"]})
    item = TripStore._item(led.trip_items.get(iid))
    assert item.checks == ("carrier", "low_confidence") and item.told == {"seats": ["12A"]}
    led.trip_items.update(iid, checks=(), told=None)
    row = led.trip_items.get(iid)
    assert (row["checks_json"], row["told_json"]) == ("[]", None)


def _digest(schema: str) -> str:
    """A schema's shape as the ledger states it: the first 12 hex digits of the SHA-256 of the
    text with every run of white space made one space."""
    return hashlib.sha256(re.sub(r"\s+", " ", schema).encode()).hexdigest()[:12]


def _state(directory) -> dict:
    """Every file in the directory, each with the SHA-256 of its bytes; a -shm by its name alone,
    since any reader of a ledger in WAL mode rewrites its index."""
    return {
        p.name: None if p.name.endswith("-shm") else hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
    }


def _settings_for(monkeypatch, tmp_path, data_dir) -> None:
    """Nothing configured but where the files go, the ledger in `data_dir`."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TRIPBOX_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))


def test_a_ledger_of_an_earlier_shape_is_refused_with_one_line_and_left_untouched(
    tmp_path, monkeypatch, capsys
):
    # the same schema less one column, stamped with the shape it had
    column = "  checks_json TEXT NOT NULL DEFAULT '[]',\n"
    earlier_schema = db.SCHEMA.replace(column, "")
    assert earlier_schema != db.SCHEMA
    earlier = tmp_path / "earlier" / "ledger.db"
    with monkeypatch.context() as m:
        m.setattr(db, "SCHEMA", earlier_schema)
        m.setattr(db, "SCHEMA_SHAPE", _digest(earlier_schema))
        with Ledger.open(earlier) as led:
            _lisbon(led)
            _booking(led, "k", trip_id=10, start_at="2031-03-14T09:30")
    # one from before shapes were stamped, with a message it read, the file in rollback mode as
    # a seeded demo leaves it
    unstamped = tmp_path / "unstamped" / "ledger.db"
    with Ledger.open(unstamped) as led:
        mid = led.messages.insert(sha256="s", source="cli", status="archived")
        led.items.add(
            message_id=mid, n=0, kind="flight", item_key="k", outcome="filed", payload={}
        )
        led.conn.execute("DELETE FROM kv WHERE k='schema_shape'")
        led.conn.execute("PRAGMA journal_mode=DELETE")
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    for path in (earlier, unstamped):
        before = _state(path.parent)
        assert list(before) == ["ledger.db"]
        with pytest.raises(db.OutdatedLedger) as refused:
            Ledger.open(path)
        assert str(refused.value) == OUTDATED
        assert _state(path.parent) == before
        _settings_for(monkeypatch, tmp_path, path.parent)
        capsys.readouterr()
        assert cli.main(["upcoming"]) == 2
        said = capsys.readouterr()
        assert said.err.splitlines() == [f"ledger: {OUTDATED}"] and said.out == ""
        assert _state(path.parent) == before
        # the page stops the same way, before it serves anything
        monkeypatch.setenv("TRIPBOX_AUTH", "none")
        assert cli.main(["web"]) == 2
        said = capsys.readouterr()
        assert said.err.splitlines() == [f"ledger: {OUTDATED}"] and served == []
        assert _state(path.parent) == before

    # one another connection still holds, all it wrote still in its -wal: the refusal leaves the
    # file and the -wal as they were, while the holder is there and after it went without a
    # checkpoint (as a process that died leaves them)
    live = tmp_path / "live" / "ledger.db"
    holder = Ledger.open(live)
    holder.conn.execute("DELETE FROM kv WHERE k='schema_shape'")
    before = _state(live.parent)
    assert list(before) == ["ledger.db", "ledger.db-shm", "ledger.db-wal"]
    with pytest.raises(db.OutdatedLedger):
        Ledger.open(live)
    assert _state(live.parent) == before
    holder.conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    holder.close()
    with pytest.raises(db.OutdatedLedger):
        Ledger.open(live)
    assert _state(live.parent) == before


def test_a_new_ledger_carries_its_shape_and_opens_again(tmp_path, monkeypatch):
    assert re.fullmatch(r"[0-9a-f]{12}", db.SCHEMA_SHAPE)
    assert db.SCHEMA_SHAPE == _digest(db.SCHEMA) == _digest(db.SCHEMA.replace("\n", "\n\t "))
    with Ledger.memory() as led:
        assert led.kv.get("schema_shape") == db.SCHEMA_SHAPE
    path = tmp_path / "ledger.db"
    with Ledger.open(path) as led:
        assert led.kv.get("schema_shape") == db.SCHEMA_SHAPE
    with Ledger.open(path) as led:
        assert led.version() == CURRENT and led.kv.get("schema_shape") == db.SCHEMA_SHAPE

    # three openers racing on a new file, each opening it again and again, leave one version row
    # and one shape row, and none of them is refused, even when the shape row takes a while to
    # write after the version row: the two become visible together or not at all
    real_migrate = Ledger.migrate

    def slow_before_the_shape(sql: str) -> None:
        if sql.startswith("INSERT INTO kv") and "'schema_shape'" in sql:
            time.sleep(0.1)

    def migrate_slowly(self):
        self.conn.set_trace_callback(slow_before_the_shape)
        try:
            real_migrate(self)
        finally:
            self.conn.set_trace_callback(None)

    monkeypatch.setattr(Ledger, "migrate", migrate_slowly)
    for n in range(3):
        fresh = tmp_path / f"race-{n}" / "ledger.db"
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def opener(fresh=fresh, barrier=barrier, errors=errors):
            try:
                barrier.wait(timeout=10)
                for _ in range(4):
                    with Ledger.open(fresh) as led:
                        assert led.kv.get("schema_shape") == db.SCHEMA_SHAPE
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=opener) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        con = sqlite3.connect(fresh)
        try:
            assert con.execute("SELECT v FROM schema_version").fetchall() == [(CURRENT,)]
            shapes = con.execute("SELECT v FROM kv WHERE k='schema_shape'").fetchall()
            assert shapes == [(db.SCHEMA_SHAPE,)]
        finally:
            con.close()
    monkeypatch.setattr(Ledger, "migrate", real_migrate)

    # a migration step writes the shape with the version, whatever the step did to kv
    monkeypatch.setattr(db, "RELEASED_SHAPES", {**db.RELEASED_SHAPES, CURRENT: db.SCHEMA_SHAPE})
    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        {**db.MIGRATIONS, NEXT: lambda conn: conn.execute("DELETE FROM kv WHERE k='schema_shape'")},
    )
    with Ledger.open(path) as led:
        assert led.version() == NEXT and led.kv.get("schema_shape") == db.SCHEMA_SHAPE


def _spy_on_reads_alone(monkeypatch, directory) -> list:
    """Each immutable read-only connection made from now on, as the names of the files beside the
    ledger at that moment; every other connection is made as before."""
    seen: list = []
    real_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        if "immutable=1" in str(target):
            seen.append(sorted(p.name for p in directory.iterdir()))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return seen


def test_a_ledger_being_written_is_checked_under_its_locks_and_never_refused(
    tmp_path, monkeypatch
):
    path = tmp_path / "ledger.db"
    with Ledger.open(path) as led:
        led.kv.set("last_uid", "5")
    seen = _spy_on_reads_alone(monkeypatch, tmp_path)
    ready, stop = threading.Event(), threading.Event()
    rounds: list[int] = []

    def writer():
        # another kv row rewritten and checkpointed into the file as fast as it goes; values of
        # changing length move the cells on the page the shape row lives on
        with Ledger.open(path) as w:
            n = 0
            while n < 20000 and (n == 0 or not stop.is_set()):
                w.kv.set("last_uid", str(n) * (n % 9 + 1))
                w.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                n += 1
                ready.set()
            rounds.append(n)

    t = threading.Thread(target=writer)
    t.start()
    refused = 0
    try:
        assert ready.wait(timeout=10)
        for _ in range(150):
            try:
                Ledger.open(path).close()
            except db.OutdatedLedger:
                refused += 1
    finally:
        stop.set()
        t.join(timeout=30)
    assert refused == 0 and rounds and rounds[0] > 1
    # the file was read alone only while nothing was beside it: the writer's own first open
    assert seen == [["ledger.db"]]
    # with the writer gone the file is read alone again before it is opened
    with Ledger.open(path) as led:
        assert led.kv.get("schema_shape") == db.SCHEMA_SHAPE
    assert seen == [["ledger.db"], ["ledger.db"]]

    # a read alone that a writer overlapped (its -journal appears meanwhile) decides nothing,
    # even when what it read looks outdated, as a torn page could: the check is made again under
    # the locks, and the healthy ledger opens
    outdated = tmp_path / "outdated" / "ledger.db"
    with Ledger.open(outdated) as led:
        led.conn.execute("DELETE FROM kv WHERE k='schema_shape'")
    journal = tmp_path / "ledger.db-journal"
    real_connect = sqlite3.connect

    def overlapped(target, *args, **kwargs):
        if "immutable=1" in str(target):
            journal.touch()
            target = str(target).replace(quote(str(path.absolute())), quote(str(outdated)))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", overlapped)
    with Ledger.open(path) as led:
        assert led.kv.get("schema_shape") == db.SCHEMA_SHAPE
    assert journal.exists()  # it was there when the read alone ended
    journal.unlink()


def test_an_outdated_file_that_cannot_be_read_alone_is_refused_with_its_header_untouched(
    tmp_path, monkeypatch
):
    path = tmp_path / "ledger.db"
    with Ledger.open(path) as led:
        led.conn.execute("DELETE FROM kv WHERE k='schema_shape'")
        led.conn.execute("PRAGMA journal_mode=DELETE")
    before = _state(tmp_path)
    assert list(before) == ["ledger.db"] and path.read_bytes()[18:20] == b"\x01\x01"
    real_connect = sqlite3.connect

    def cannot_read_alone(target, *args, **kwargs):
        if "immutable=1" in str(target):
            raise sqlite3.DatabaseError("file is not a database")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", cannot_read_alone)
    with pytest.raises(db.OutdatedLedger):
        Ledger.open(path)
    assert _state(tmp_path) == before and path.read_bytes()[18:20] == b"\x01\x01"


# ----------------------------------------------------------------------------- upgrades
NEWER = (
    "this ledger was written by a newer version of tripbox (ledger version {v}; this one reads"
    " up to {max}); upgrade tripbox, or restore a backup made with this version"
)
NOT_COPIED = (
    "the ledger needs an upgrade and its copy could not be written beside it; nothing was"
    ' changed: free some space or fix the folder ("tripbox doctor"), then start again'
)


def _copies(path) -> list:
    return sorted(path.parent.glob("ledger-before-*"))


def _version_of(path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v FROM schema_version").fetchone()[0]
    finally:
        con.close()


def test_a_ledger_of_this_version_opens_as_before(tmp_ledger_path):
    """A new ledger and one of this version are opened with the same statements as before the
    upgrade contract: the schema run as it is, a new one stamped, nothing copied."""
    seen: list[str] = []
    real_connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    def plain(sql: str) -> str:
        """The statement without its comments, white space or closing semicolon."""
        return " ".join(re.sub(r"--[^\n]*", "", sql).split()).rstrip(";").strip()

    def writes() -> list[str]:
        out = [plain(s) for s in seen if not plain(s).upper().startswith("SELECT")]
        seen.clear()
        return out

    schema = [plain(s) for s in re.sub(r"--[^\n]*", "", db.SCHEMA).split(";") if plain(s)]
    pragmas = [
        "PRAGMA journal_mode=WAL",
        "PRAGMA busy_timeout=10000",
        "PRAGMA foreign_keys=ON",
        "PRAGMA synchronous=NORMAL",
    ]
    stamp = [
        "BEGIN IMMEDIATE",
        f"INSERT INTO schema_version(v) SELECT {db.SCHEMA_VERSION} WHERE NOT EXISTS"
        " (SELECT 1 FROM schema_version)",
        f"INSERT INTO kv(k, v) SELECT 'schema_shape', '{db.SCHEMA_SHAPE}' WHERE NOT EXISTS"
        " (SELECT 1 FROM kv WHERE k='schema_shape')",
        "COMMIT",
    ]
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sqlite3, "connect", traced)
        Ledger.open(tmp_ledger_path).close()
        made = writes()
        Ledger.open(tmp_ledger_path).close()
        again = writes()
    assert made == [*pragmas, *schema, *stamp]
    assert again == [*pragmas, *schema]
    assert _copies(tmp_ledger_path) == []


def test_a_ledger_is_copied_beside_itself_before_its_first_upgrade_step(
    monkeypatch, tmp_ledger_path, next_schema, caplog
):
    at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    name = f"ledger-before-v{NEXT}-{at.strftime('%Y%m%dT%H%M%SZ')}.db"
    copy = tmp_ledger_path.with_name(name)
    seen_in_step = []

    def step(conn):
        # the copy is there, whole, before the step changes anything
        seen_in_step.append((_copies(tmp_ledger_path), _version_of(copy)))
        _add_the_note(conn)

    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: step})
    caplog.set_level(logging.INFO, logger="tripbox")
    with util.clock_at(at), Ledger.open(tmp_ledger_path) as led:
        assert led.version() == NEXT and led.trips.get(10)["title"] == "Lisbon"
    assert seen_in_step == [([copy], CURRENT)]
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert [p.name for p in tmp_ledger_path.parent.iterdir() if p.suffix == ".part"] == []
    with Ledger.memory():
        pass
    # the copy is the ledger as it was: its version, its rows, no column of the next
    con = sqlite3.connect(copy)
    try:
        assert con.execute("SELECT v FROM kv WHERE k='schema_shape'").fetchone()[0] == (
            db.RELEASED_SHAPES[CURRENT]
        )
        assert con.execute("SELECT title FROM trips WHERE id=10").fetchone()[0] == "Lisbon"
        assert "synthetic_note" not in {r[1] for r in con.execute("PRAGMA table_info(trips)")}
    finally:
        con.close()
    said = [r.getMessage() for r in caplog.records if r.name.startswith("tripbox.ledger")]
    assert said == [
        f"the ledger is upgraded from version {CURRENT} to {NEXT}; a copy is kept as {name}"
    ]
    # opened again, it is of this version: nothing more is copied
    with Ledger.open(tmp_ledger_path):
        pass
    assert _copies(tmp_ledger_path) == [copy]


def test_an_upgrade_whose_copy_cannot_be_written_changes_nothing(
    monkeypatch, tmp_path, tmp_ledger_path, next_version, capsys
):
    at = dt.datetime.now(dt.UTC).replace(microsecond=0)

    class FullDisk(sqlite3.Connection):
        """The copy's source: its backup finds the disk full."""

        def backup(self, *args, **kwargs):
            raise sqlite3.OperationalError("database or disk is full")

    real_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        if str(target).endswith("?mode=ro"):  # the copy's read-only source
            kwargs["factory"] = FullDisk
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)

    def files() -> dict:
        """Every file beside the ledger with the SHA-256 of its bytes, but a -shm (see _state)."""
        return {
            f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in tmp_path.iterdir()
            if f.is_file() and not f.name.endswith("-shm")
        }

    before = files()
    with util.clock_at(at), pytest.raises(db.OutdatedLedger) as refused:
        Ledger.open(tmp_ledger_path)
    assert str(refused.value) == NOT_COPIED
    assert next_version == []  # the step never ran
    assert _version_of(tmp_ledger_path) == CURRENT and files() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".part")) == []
    # as a command says it
    _settings_for(monkeypatch, tmp_path, tmp_path)
    with util.clock_at(at):
        assert cli.main(["upcoming"]) == 2
    said = capsys.readouterr()
    assert said.err.splitlines() == [f"ledger: {NOT_COPIED}"] and said.out == ""
    assert _version_of(tmp_ledger_path) == CURRENT and next_version == []


def test_a_step_that_adds_an_indexed_column_ends_in_a_new_ledgers_shape(
    tmp_path, tmp_ledger_path, next_version
):
    with Ledger.open(tmp_ledger_path) as led:
        upgraded = _shape(led)
        assert led.version() == NEXT and led.kv.get("schema_shape") == db.SCHEMA_SHAPE
    with Ledger.open(tmp_path / "new" / "ledger.db") as led:
        assert _shape(led) == upgraded
        assert "trips_note" in upgraded[1] and "synthetic_note" in upgraded[0]["trips"]


def test_a_step_that_forgets_what_the_schema_adds_changes_nothing(
    monkeypatch, tmp_ledger_path, next_schema
):
    def no_index(conn):
        conn.execute("ALTER TABLE trips ADD COLUMN synthetic_note TEXT")

    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: no_index})
    wrong = f"^migration to v{NEXT} does not end in this version's schema$"
    with pytest.raises(RuntimeError, match=wrong):
        Ledger.open(tmp_ledger_path)
    con = sqlite3.connect(tmp_ledger_path)
    try:
        assert con.execute("SELECT v FROM schema_version").fetchone()[0] == CURRENT
        assert "synthetic_note" not in {r[1] for r in con.execute("PRAGMA table_info(trips)")}
    finally:
        con.close()


def test_a_rebuilt_table_keeps_its_id_sequence(monkeypatch, tmp_ledger_path, next_schema):
    """A trip that went leaves its id given: after the table is rebuilt the next trip still
    takes a new one."""
    con = sqlite3.connect(tmp_ledger_path)
    try:
        with con:  # a trip after the seeded one, which went: its id was given
            con.execute(
                "INSERT INTO trips(id, title, start_date, end_date, created_at, updated_at)"
                " SELECT id + 1, title, start_date, end_date, created_at, updated_at FROM trips"
            )
            con.execute("DELETE FROM trips WHERE id = 11")
    finally:
        con.close()
    (create,) = re.findall(r"CREATE TABLE IF NOT EXISTS trips\(.*?\n\);", next_schema, re.S)

    def rebuild(conn):
        db.rebuild_table(conn, "trips", create, EXPECTED_COLUMNS["trips"].split())
        conn.execute("CREATE INDEX trips_dates ON trips(start_date, end_date)")
        conn.execute("CREATE INDEX trips_note ON trips(synthetic_note)")

    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: rebuild})
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == NEXT and "synthetic_note" in _trip_columns(led)
        assert led.trips.get(10)["title"] == "Lisbon" and len(led.trip_items.by_trip(10)) == 1
        assert led.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        given = led.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='trips'").fetchone()
        _lisbon_again = led.trips.insert(
            title="Lisbon", place=None, start_date="2031-03-14", end_date="2031-03-21"
        )
        assert given[0] == 11 and _lisbon_again == 12


def test_a_ledger_from_a_newer_version_is_refused_untouched(
    monkeypatch, tmp_path, tmp_ledger_path, capsys
):
    _seed_current(tmp_ledger_path)
    con = sqlite3.connect(tmp_ledger_path)
    con.execute(f"UPDATE schema_version SET v={NEXT}")
    con.commit()
    con.close()
    before = _state(tmp_path)
    assert list(before) == ["ledger.db"]
    with pytest.raises(db.OutdatedLedger) as refused:
        Ledger.open(tmp_ledger_path)
    newer = NEWER.format(v=NEXT, max=CURRENT)
    assert str(refused.value) == newer
    assert _state(tmp_path) == before
    _settings_for(monkeypatch, tmp_path, tmp_path)
    assert cli.main(["upcoming"]) == 2
    said = capsys.readouterr()
    assert said.err.splitlines() == [f"ledger: {newer}"] and said.out == ""
    assert _state(tmp_path) == before and _copies(tmp_ledger_path) == []


def test_an_older_ledger_that_states_no_released_shape_is_refused(
    monkeypatch, tmp_path, tmp_ledger_path, next_version
):
    """A ledger of an older version is upgraded only from the shape that version was released
    with, and only when a step leads to every version after it; else it is refused untouched."""
    before = _state(tmp_path)
    released = dict(db.RELEASED_SHAPES)
    for shapes, steps in (({}, None), ({CURRENT: "0" * 12}, None), (released, {})):
        monkeypatch.setattr(db, "RELEASED_SHAPES", shapes)
        if steps is not None:
            monkeypatch.setattr(db, "MIGRATIONS", steps)
        with pytest.raises(db.OutdatedLedger) as refused:
            Ledger.open(tmp_ledger_path)
        assert str(refused.value) == OUTDATED
        assert _state(tmp_path) == before and next_version == []
    assert _copies(tmp_ledger_path) == []


def test_a_step_that_fails_at_every_start_leaves_one_copy(
    monkeypatch, tmp_ledger_path, next_schema
):
    """A step that fails is a crash, and a restart policy starts the roles again and again: the
    ledger, unchanged, is copied once. A copy a killed start left half-written goes, and a
    ledger that did change in between gets a copy of its own; no copy is ever removed."""
    def failing(conn):
        _add_the_note(conn)
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: failing})
    at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    killed = tmp_ledger_path.with_name(f"ledger-before-v{NEXT}-20000101T000000Z.db.part")
    left = [killed, *(killed.with_name(killed.name + end) for end in ("-wal", "-shm", "-journal"))]
    for p in left:
        p.write_bytes(b"half a copy")
    for minutes in range(4):
        with util.clock_at(at + dt.timedelta(minutes=minutes)), pytest.raises(RuntimeError):
            Ledger.open(tmp_ledger_path)
    (first,) = _copies(tmp_ledger_path)
    assert first.name == f"ledger-before-v{NEXT}-{at.strftime('%Y%m%dT%H%M%SZ')}.db"
    assert [p for p in left if p.exists()] == [] and _version_of(tmp_ledger_path) == CURRENT
    con = sqlite3.connect(tmp_ledger_path)
    with con:
        con.execute("INSERT INTO kv(k, v) VALUES ('changed', '1')")
    con.close()
    for minutes in (10, 11):
        with util.clock_at(at + dt.timedelta(minutes=minutes)), pytest.raises(RuntimeError):
            Ledger.open(tmp_ledger_path)
    assert len(_copies(tmp_ledger_path)) == 2 and first.exists()


def test_an_earlier_copy_this_user_cannot_read_is_not_the_same(
    monkeypatch, tmp_ledger_path, next_schema
):
    """A copy an earlier start left that this user cannot read (another user made it) says
    nothing of the ledger as it stands: the upgrade takes a fresh copy instead of refusing."""
    def failing(conn):
        _add_the_note(conn)
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: failing})
    at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    with util.clock_at(at), pytest.raises(RuntimeError, match="boom"):
        Ledger.open(tmp_ledger_path)
    (first,) = _copies(tmp_ledger_path)

    def unreadable(a, b, shallow=True):
        raise PermissionError(13, "Permission denied", str(a))

    monkeypatch.setattr(db.filecmp, "cmp", unreadable)
    with util.clock_at(at + dt.timedelta(minutes=1)), pytest.raises(RuntimeError, match="boom"):
        Ledger.open(tmp_ledger_path)
    assert len(_copies(tmp_ledger_path)) == 2 and first.exists()
    assert _version_of(tmp_ledger_path) == CURRENT


def _with_a_tag(schema: str) -> str:
    """The schema with a second column on trips, after the note."""
    note = "  synthetic_note TEXT\n"
    assert schema.count(note) == 1
    return schema.replace(note, "  synthetic_note TEXT,\n  synthetic_tag TEXT\n")


def test_a_ledger_two_versions_behind_survives_a_step_that_fails_once(
    monkeypatch, tmp_ledger_path, next_schema
):
    """Two steps, each committed alone: one that is not the last states the shape its version
    was released with, so a ledger left between them (the later step failed, or another role
    opens it meanwhile) is an older ledger to upgrade, never one refused for good."""
    v_next = next_schema
    v_last = _with_a_tag(v_next)
    monkeypatch.setattr(db, "RELEASED_SHAPES", {**db.RELEASED_SHAPES, NEXT: _digest(v_next)})
    monkeypatch.setattr(db, "SCHEMA", v_last)
    monkeypatch.setattr(db, "SCHEMA_SHAPE", _digest(v_last))
    monkeypatch.setattr(db, "SCHEMA_VERSION", NEXT + 1)
    between: list = []
    failures = [RuntimeError("boom")]

    def tag(conn):
        # another role opening the ledger now reads the step before's commit, and may upgrade it
        other = sqlite3.connect(tmp_ledger_path)
        try:
            between.append(db._stated_version(other))
        finally:
            other.close()
        conn.execute("ALTER TABLE trips ADD COLUMN synthetic_tag TEXT")
        if failures:
            raise failures.pop()

    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: _add_the_note, NEXT + 1: tag})
    with pytest.raises(RuntimeError, match="boom"):
        Ledger.open(tmp_ledger_path)
    con = sqlite3.connect(tmp_ledger_path)
    try:
        assert db._stated_version(con) == NEXT
        assert con.execute("SELECT v FROM kv WHERE k='schema_shape'").fetchone()[0] == _digest(
            v_next
        )
    finally:
        con.close()
    with Ledger.open(tmp_ledger_path) as led:
        assert led.version() == NEXT + 1 and led.kv.get("schema_shape") == db.SCHEMA_SHAPE
        assert {"synthetic_note", "synthetic_tag"} <= _trip_columns(led)
        assert led.trips.get(10)["title"] == "Lisbon" and len(led.trip_items.by_trip(10)) == 1
    assert between == [NEXT, NEXT]
    # the copy made at each try states the version it was made at
    assert sorted(_version_of(c) for c in _copies(tmp_ledger_path)) == [CURRENT, NEXT]


def test_a_step_short_of_this_version_without_a_released_shape_is_refused(monkeypatch):
    """Every step but the last ends in a released version's shape, so each needs one: a missing
    entry is refused when the module is imported, not when a ledger is half upgraded."""
    db._each_step_is_released()  # as imported
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(db, "RELEASED_SHAPES", {1: "0" * 12})
    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, NEXT: _add_the_note, 3: _add_the_note})
    with pytest.raises(RuntimeError, match="^the step to v2 has no released shape$"):
        db._each_step_is_released()
    monkeypatch.setattr(db, "RELEASED_SHAPES", {1: "0" * 12, 2: "1" * 12})
    db._each_step_is_released()
    # it runs when the module is imported
    assert "\n_each_step_is_released()\n" in Path(db.__file__).read_text()


def _upgrade_gaps(version: int, shapes: dict, steps: dict) -> list[str]:
    """What an upgrade from each version below `version` lacks: the shape it was released with,
    or the step to the version after it."""
    gaps = []
    for older in range(1, version):
        if older not in shapes:
            gaps.append(f"v{older}: no released shape")
        if older + 1 not in steps:
            gaps.append(f"v{older}: no step to v{older + 1}")
    return gaps


def test_every_older_version_has_its_shape_and_its_next_step():
    """On the module's own constants: a bump of SCHEMA_VERSION comes with the old version's shape
    in RELEASED_SHAPES and the step from it in MIGRATIONS, else no ledger of that version could
    ever be upgraded."""
    assert _upgrade_gaps(db.SCHEMA_VERSION, db.RELEASED_SHAPES, db.MIGRATIONS) == []
    # what it catches: a bump with neither
    assert _upgrade_gaps(db.SCHEMA_VERSION + 1, db.RELEASED_SHAPES, db.MIGRATIONS)


def test_the_version_and_the_shape_are_read_in_one_statement(tmp_ledger_path):
    """An upgrade that commits between a read of the version and one of the shape would pair one
    version's number with the other's shape: the two are read in one statement."""
    Ledger.open(tmp_ledger_path).close()
    seen: list[str] = []
    con = sqlite3.connect(tmp_ledger_path)
    con.set_trace_callback(seen.append)
    try:
        assert db._stated_version(con) == db.SCHEMA_VERSION
    finally:
        con.close()
    reads = [s for s in seen if "schema_version" in s and "sqlite_master" not in s]
    assert len(reads) == 1 and "schema_shape" in reads[0]


def test_the_released_v1_schema_is_not_edited_in_place():
    """Version 1 is released: an installed ledger states this shape, so an edit of SCHEMA while
    SCHEMA_VERSION is 1 makes every installed ledger refuse to open."""
    released_v1 = "10bd0c645b14"
    if db.SCHEMA_VERSION == 1:
        assert db.SCHEMA_SHAPE == released_v1, (
            "SCHEMA was edited in place, which refuses every installed ledger. Put SCHEMA back,"
            " bump SCHEMA_VERSION, add a step to MIGRATIONS that makes the change, and add this"
            f" shape ({released_v1}) as RELEASED_SHAPES[1]."
        )
    else:
        assert db.RELEASED_SHAPES.get(1) == released_v1
        assert 2 in db.MIGRATIONS


V1_SCHEMA = Path(__file__).parent / "data" / "ledger_v1_schema.sql"


def _make_v1(path) -> None:
    """A ledger as version 1 wrote it: the released SCHEMA text (a frozen copy, so this does not
    follow db.py), stamped with the version and shape it was released with, holding rows."""
    text = V1_SCHEMA.read_text()
    assert _digest(text) == "10bd0c645b14"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(text)
        conn.execute("INSERT INTO schema_version(v) VALUES (1)")
        conn.execute("INSERT INTO kv(k, v) VALUES ('schema_shape', '10bd0c645b14')")
        conn.execute("INSERT INTO kv(k, v) VALUES ('last_uid', '41')")
        mid = conn.execute(
            "INSERT INTO messages(sha256, source, status, created_at)"
            " VALUES ('s1', 'cli', 'processed', '2031-03-01T00:00:00+00:00')"
        ).lastrowid
        conn.execute(
            "INSERT INTO trips(id, title, place, start_date, end_date, created_at, updated_at)"
            " VALUES (10, 'Lisbon', 'Lisbon, Portugal', '2031-03-14', '2031-03-21',"
            " '2031-03-01T00:00:00+00:00', '2031-03-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO trip_items(trip_id, item_key, kind, status, start_at, payload_json,"
            " passengers_json, source_message_id, created_at, updated_at)"
            " VALUES (10,"
            " 'ZZ|9001|2031-03-14|STL', 'flight', 'confirmed', '2031-03-14T09:30', '{}', '[]', ?,"
            " '2031-03-01T00:00:00+00:00',"
            " '2031-03-01T00:00:00+00:00')",
            (mid,),
        )
        conn.execute(
            "INSERT INTO share_links(trip_id, token, token_hash, shows, created_at)"
            " VALUES (10, 't', 'h', '[]', '2031-03-01T00:00:00+00:00')"
        )
    finally:
        conn.close()


def _rows_of(path) -> dict:
    con = sqlite3.connect(path)
    try:
        return {
            t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
            for t in ("kv", "messages", "trips", "trip_items", "share_links")
        }
    finally:
        con.close()


def test_a_released_v1_ledger_upgrades_to_v2_and_keeps_every_row(tmp_ledger_path):
    _make_v1(tmp_ledger_path)
    before = _rows_of(tmp_ledger_path)
    at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    with util.clock_at(at), Ledger.open(tmp_ledger_path) as led:
        assert led.version() == 2 and led.kv.get("schema_shape") == db.SCHEMA_SHAPE
        assert led.covers.all_for_lookup() == []
        expected = {t: c.split() for t, c in EXPECTED_COLUMNS.items()}
        assert _shape(led) == (expected, EXPECTED_INDEXES)
    after = _rows_of(tmp_ledger_path)
    shape = {r[0]: r[1] for r in before["kv"]}["schema_shape"]
    assert shape == "10bd0c645b14"
    for table in ("messages", "trips", "trip_items", "share_links"):
        assert after[table] == before[table]
    assert {k: v for k, v in after["kv"] if k != "schema_shape"} == {
        k: v for k, v in before["kv"] if k != "schema_shape"
    }
    # the file as version 1 left it is kept beside the ledger
    (copy,) = _copies(tmp_ledger_path)
    assert copy.name == f"ledger-before-v2-{at.strftime('%Y%m%dT%H%M%SZ')}.db"
    assert _version_of(copy) == 1 and _rows_of(copy) == before
    cc = sqlite3.connect(copy)
    try:
        assert cc.execute("SELECT name FROM sqlite_master WHERE name='covers'").fetchall() == []
    finally:
        cc.close()


def test_deleting_a_trip_takes_its_cover(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        led.trips.insert(
            title="A", place=None, start_date="2027-05-01", end_date="2027-05-02", id=1
        )
        led.trips.insert(
            title="B", place=None, start_date="2027-06-01", end_date="2027-06-02", id=2
        )
        led.covers.put(1, source="owner", state="ready", large=b"L", small=b"S")
        led.covers.put(2, source="wikimedia", state="none")
        led.trips.delete(1)
        assert led.covers.get(1) is None and led.covers.get(2) is not None
        assert led.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_covers_round_trip(tmp_ledger_path):
    with Ledger.open(tmp_ledger_path) as led:
        for n in (1, 2, 3):
            led.trips.insert(
                title=f"T{n}", place=None, start_date="2027-05-01", end_date="2027-05-02", id=n
            )
        assert led.covers.get(1) is None and led.covers.by_digest("d1") is None
        with util.clock_at(dt.datetime(2027, 5, 1, 8, tzinfo=dt.UTC)):
            led.covers.put(1, source="owner", state="pending", upload=b"raw", digest="d1")
        with util.clock_at(dt.datetime(2027, 5, 1, 9, tzinfo=dt.UTC)):
            led.covers.put(2, source="owner", state="pending", upload=b"raw2", digest="d2")
        row = led.covers.get(1)
        assert (row["source"], row["state"], row["upload"], row["attempts"]) == (
            "owner",
            "pending",
            b"raw",
            0,
        )
        assert row["created_at"] == row["updated_at"] == "2027-05-01T08:00:00+00:00"
        assert led.covers.by_digest("d2")["trip_id"] == 2
        assert led.covers.pending_upload()["trip_id"] == 1  # the oldest
        # an upsert changes the fields given, keeps the rest and created_at, stamps updated_at
        with util.clock_at(dt.datetime(2027, 5, 1, 10, tzinfo=dt.UTC)):
            led.covers.put(1, state="ready", large=b"L", small=b"S", upload=None)
        row = led.covers.get(1)
        assert (row["state"], row["digest"], row["large"], row["upload"]) == (
            "ready",
            "d1",
            b"L",
            None,
        )
        assert row["created_at"] == "2027-05-01T08:00:00+00:00"
        assert row["updated_at"] == "2027-05-01T10:00:00+00:00"
        assert led.covers.pending_upload()["trip_id"] == 2
        led.covers.put(3, source="wikimedia", state="pending")  # not an owner's upload
        led.covers.put(2, state="failed")
        assert led.covers.pending_upload() is None
        # all_for_lookup carries no image
        looked = led.covers.all_for_lookup()
        assert [r["trip_id"] for r in looked] == [1, 2, 3]
        assert "large" not in looked[0].keys() and "upload" not in looked[0].keys()
        # move
        led.covers.delete(3)
        led.covers.move(2, 3)
        assert led.covers.get(2) is None and led.covers.get(3)["digest"] == "d2"
        led.covers.delete(3)
        assert led.covers.get(3) is None
        with pytest.raises(ValueError):
            led.covers.put(1, bogus=1)
        with pytest.raises(sqlite3.IntegrityError):
            led.covers.put(1, source="elsewhere")


def test_covers_take_only_their_words():
    with Ledger.memory() as led:
        led.trips.insert(
            title="A", place=None, start_date="2027-05-01", end_date="2027-05-02", id=1
        )
        for fields in (
            {"source": "owner", "state": "ready"},
            {"source": "removed", "state": "none"},
        ):
            led.covers.put(1, **fields)
        for fields in (
            {"source": "flickr", "state": "ready"},
            {"source": "owner", "state": "done"},
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                led.covers.put(1, **fields)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            led.covers.put(2, source="owner", state="ready")
