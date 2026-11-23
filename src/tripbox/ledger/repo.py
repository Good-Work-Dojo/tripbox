# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Typed-ish repositories over the ledger tables. Every write is a single autocommit statement."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from typing import Any

from tripbox.util import now_iso, parse_iso


def _update(conn: sqlite3.Connection, table: str, row_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))


class KV:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def get(self, k: str, default: str | None = None) -> str | None:
        row = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k: str, v: str) -> None:
        self.c.execute(
            "INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v)
        )

    def setdefault(self, k: str, v: str) -> str:
        """The value of `k`, written as `v` first when there is none (of two writers, the first
        one's stands)."""
        self.c.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO NOTHING", (k, v))
        return self.get(k, v) or v


class Messages:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def insert(
        self,
        *,
        sha256: str,
        source: str,
        status: str,
        message_id: str | None = None,
        mail_uid: int | None = None,
        received_at: str | None = None,
        email_date: str | None = None,
        from_addr: str | None = None,
        subject: str | None = None,
        origin_ok: int = 0,
        archive_path: str | None = None,
    ) -> int:
        cur = self.c.execute(
            "INSERT INTO messages(message_id, sha256, source, mail_uid, received_at,"
            " email_date, from_addr, subject, origin_ok, archive_path, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                sha256,
                source,
                mail_uid,
                received_at,
                email_date,
                from_addr,
                subject,
                int(origin_ok),
                archive_path,
                status,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def get(self, row_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM messages WHERE id=?", (row_id,)).fetchone()

    def by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM messages WHERE sha256=?", (sha256,)).fetchone()

    def by_message_id(self, message_id: str) -> list[sqlite3.Row]:
        """Every delivery of one email (message_id is not unique), oldest first."""
        return self.c.execute(
            "SELECT * FROM messages WHERE message_id=? ORDER BY id", (message_id,)
        ).fetchall()

    def update(self, row_id: int, **fields: Any) -> None:
        _update(self.c, "messages", row_id, fields)

    def set_answer(self, row_id: int, answer_json: str) -> None:
        """Keep the model's answer for this message, as it was read."""
        _update(self.c, "messages", row_id, {"answer_json": answer_json})

    def answer(self, row_id: int) -> str | None:
        row = self.c.execute("SELECT answer_json FROM messages WHERE id=?", (row_id,)).fetchone()
        return row["answer_json"] if row else None

    def finish(self, row_id: int, status: str, **fields: Any) -> None:
        """The reading ended as `status`; the reason a message waited goes unless `fields` say
        why this one ended so (`error`)."""
        _update(
            self.c,
            "messages",
            row_id,
            {"status": status, "processed_at": now_iso(), "error": None, **fields},
        )

    def by_status(self, status: str) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM messages WHERE status=? ORDER BY id", (status,)
        ).fetchall()

    def waiting(self) -> dict[str, int]:
        """How many messages wait, by the reason each waits for (its `error`)."""
        rows = self.c.execute(
            "SELECT error, COUNT(*) FROM messages WHERE status='waiting' GROUP BY error"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def wait_for(self, reason: str) -> list[int]:
        """Every message that waits now waits for `reason`: the ids of those that waited for
        another."""
        rows = self.c.execute(
            "UPDATE messages SET error=? WHERE status='waiting' AND error IS NOT ? RETURNING id",
            (reason, reason),
        ).fetchall()
        return sorted(r[0] for r in rows)

    def by_error(self, error: str) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM messages WHERE error=? ORDER BY id", (error,)
        ).fetchall()

    def counts_since(self, since_iso: str) -> dict[str, int]:
        rows = self.c.execute(
            "SELECT status, COUNT(*) FROM messages WHERE created_at >= ? GROUP BY status",
            (since_iso,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def all(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM messages ORDER BY id").fetchall()


class Items:
    """The log of what each message did: one row per booking the message read, written once.
    Only two statements change a row: `repoint` (two bookings became one) and `forget` (the
    booking was purged). A message read again has its rows deleted (`delete_for_message`) and
    written anew."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def add(
        self,
        *,
        message_id: int,
        n: int,
        kind: str,
        item_key: str,
        outcome: str,
        payload: dict,
        detail: str | None = None,
        confidence: float | None = None,
        trip_item_id: int | None = None,
    ) -> int:
        cur = self.c.execute(
            "INSERT INTO items(message_id, n, kind, item_key, outcome, detail, payload_json,"
            " confidence, trip_item_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                n,
                kind,
                item_key,
                outcome,
                detail,
                json.dumps(payload, default=str),
                confidence,
                trip_item_id,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def get(self, row_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM items WHERE id=?", (row_id,)).fetchone()

    def by_message(self, message_id: int) -> list[sqlite3.Row]:
        """A message's rows in the order of its bookings."""
        return self.c.execute(
            "SELECT * FROM items WHERE message_id=? ORDER BY n, id", (message_id,)
        ).fetchall()

    def by_trip_item(self, trip_item_id: int) -> list[sqlite3.Row]:
        """The rows of the messages that read this booking, oldest first."""
        return self.c.execute(
            "SELECT * FROM items WHERE trip_item_id=? ORDER BY id", (trip_item_id,)
        ).fetchall()

    def delete_for_message(self, message_id: int) -> list[int]:
        """Delete a message's rows, for another reading of it to write its own; their ids."""
        ids = [
            r[0]
            for r in self.c.execute(
                "SELECT id FROM items WHERE message_id=? ORDER BY id", (message_id,)
            ).fetchall()
        ]
        self.c.execute("DELETE FROM items WHERE message_id=?", (message_id,))
        return ids

    def repoint(self, old_trip_item_id: int, new_trip_item_id: int) -> None:
        """Two bookings became one: the rows of the one that goes point at the one that stays."""
        self.c.execute(
            "UPDATE items SET trip_item_id=? WHERE trip_item_id=?",
            (new_trip_item_id, old_trip_item_id),
        )

    def forget(self, trip_item_id: int) -> None:
        """The booking was purged: its rows stay, pointing at no booking."""
        self.c.execute("UPDATE items SET trip_item_id=NULL WHERE trip_item_id=?", (trip_item_id,))

    @staticmethod
    def payload(row: sqlite3.Row) -> dict:
        return json.loads(row["payload_json"])


class RefCache:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def get(self, kind: str, code: str, max_age_days: int = 90) -> Any | None:
        row = self.c.execute(
            "SELECT json, fetched_at FROM ref_cache WHERE kind=? AND code=?", (kind, code)
        ).fetchone()
        if row is None:
            return None
        age = (parse_iso(now_iso()) - parse_iso(row["fetched_at"])).days
        if age > max_age_days:
            return None
        return json.loads(row["json"])

    def put(self, kind: str, code: str, obj: Any) -> None:
        self.c.execute(
            "INSERT INTO ref_cache(kind, code, json, fetched_at) VALUES (?,?,?,?)"
            " ON CONFLICT(kind, code) DO UPDATE SET json=excluded.json,"
            " fetched_at=excluded.fetched_at",
            (kind, code, json.dumps(obj), now_iso()),
        )

    def clear(self) -> int:
        """Drop every cached answer; how many there were."""
        return self.c.execute("DELETE FROM ref_cache").rowcount


class LLMCalls:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def add(
        self,
        *,
        message_id: int | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        self.c.execute(
            "INSERT INTO llm_calls(message_id, model, input_tokens, output_tokens,"
            " cost_usd, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (message_id, model, input_tokens, output_tokens, cost_usd, now_iso()),
        )

    def month_cost(self, month: str) -> tuple[int, float]:
        row = self.c.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM llm_calls"
            " WHERE substr(created_at,1,7)=?",
            (month,),
        ).fetchone()
        return int(row[0]), float(row[1])

    def day_count(self, day: str) -> int:
        """The calls made on `day` (YYYY-MM-DD, UTC: every time is stored in UTC)."""
        row = self.c.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE substr(created_at,1,10)=?", (day,)
        ).fetchone()
        return int(row[0])


class AeroApiCalls:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def add(
        self, *, month: str, item_id: int | None, ident: str, result_sets: int, est_cost_usd: float
    ) -> None:
        self.c.execute(
            "INSERT INTO aeroapi_calls(month, item_id, ident, result_sets, est_cost_usd,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (month, item_id, ident, result_sets, est_cost_usd, now_iso()),
        )

    def month_total(self, month: str) -> tuple[int, float]:
        row = self.c.execute(
            "SELECT COUNT(*), COALESCE(SUM(est_cost_usd),0) FROM aeroapi_calls WHERE month=?",
            (month,),
        ).fetchone()
        return int(row[0]), float(row[1])


class Enrichment:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def get(self, item_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM enrichment WHERE item_id=?", (item_id,)).fetchone()

    def bump(self, item_id: int) -> None:
        self.c.execute(
            "INSERT INTO enrichment(item_id, attempts, last_attempt) VALUES (?,1,?)"
            " ON CONFLICT(item_id) DO UPDATE SET attempts=attempts+1,"
            " last_attempt=excluded.last_attempt",
            (item_id, now_iso()),
        )

    def mark_done(self, item_id: int, fields_set: dict) -> None:
        self.c.execute(
            "INSERT INTO enrichment(item_id, attempts, last_attempt, enriched_at, fields_set_json)"
            " VALUES (?,1,?,?,?) ON CONFLICT(item_id)"
            " DO UPDATE SET enriched_at=excluded.enriched_at,"
            " fields_set_json=excluded.fields_set_json",
            (item_id, now_iso(), now_iso(), json.dumps(fields_set)),
        )


class Trips:
    """tripbox trips. Rows are created/maintained by trips.store.TripStore only."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def insert(
        self,
        *,
        title: str,
        place: str | None,
        start_date: str,
        end_date: str,
        id: int | None = None,  # noqa: A002 - tests pin trip ids
    ) -> int:
        ts = now_iso()
        cur = self.c.execute(
            "INSERT INTO trips(id, title, place, start_date, end_date, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (id, title, place, start_date, end_date, ts, ts),
        )
        return int(cur.lastrowid)

    def get(self, trip_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()

    def all(self) -> list[sqlite3.Row]:
        """The trips the owner sees: not deleted, and holding at least one booking that is not
        removed. A just-created empty trip is reached by `get`, never listed."""
        return self.c.execute(
            "SELECT t.* FROM trips t WHERE t.deleted_at IS NULL AND EXISTS"
            " (SELECT 1 FROM trip_items i WHERE i.trip_id=t.id AND i.removed_at IS NULL)"
            " ORDER BY t.start_date, t.id"
        ).fetchall()

    def deleted(self) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM trips WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC, id DESC"
        ).fetchall()

    def empty_older_than(self, cutoff: str) -> list[sqlite3.Row]:
        """Trips with no trip_items rows at all (a failed trip link leaves one) created at or
        before `cutoff` — the purge sweeps them."""
        return self.c.execute(
            "SELECT t.* FROM trips t WHERE t.created_at <= ? AND NOT EXISTS"
            " (SELECT 1 FROM trip_items i WHERE i.trip_id=t.id) ORDER BY t.id",
            (cutoff,),
        ).fetchall()

    def update(self, trip_id: int, **fields: Any) -> None:
        _update(self.c, "trips", trip_id, {**fields, "updated_at": now_iso()})

    def delete(self, trip_id: int) -> None:
        self.c.execute("DELETE FROM trips WHERE id=?", (trip_id,))


class TripItems:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def insert(
        self,
        *,
        trip_id: int | None,
        item_key: str,
        kind: str,
        status: str,
        start_at: str | None,
        end_at: str | None,
        start_tz: str | None,
        end_tz: str | None,
        confirmation: str | None,
        payload: dict,
        passengers: Iterable[str],
        airtrail_flight_id: int | None = None,
        source_message_id: int | None = None,
    ) -> int:
        ts = now_iso()
        cur = self.c.execute(
            "INSERT INTO trip_items(trip_id, item_key, kind, status, start_at, end_at, start_tz,"
            " end_tz, confirmation, payload_json, passengers_json, airtrail_flight_id,"
            " source_message_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trip_id,
                item_key,
                kind,
                status,
                start_at,
                end_at,
                start_tz,
                end_tz,
                confirmation,
                json.dumps(payload, default=str),
                json.dumps(list(passengers)),
                airtrail_flight_id,
                source_message_id,
                ts,
                ts,
            ),
        )
        return int(cur.lastrowid)

    def get(self, item_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM trip_items WHERE id=?", (item_id,)).fetchone()

    def by_key(self, item_key: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM trip_items WHERE item_key=?", (item_key,)).fetchone()

    def by_trip(self, trip_id: int, *, include_removed: bool = False) -> list[sqlite3.Row]:
        cond = "" if include_removed else " AND removed_at IS NULL"
        return self.c.execute(
            f"SELECT * FROM trip_items WHERE trip_id=?{cond} ORDER BY start_at, id", (trip_id,)
        ).fetchall()

    def keyed_by_a_printed_day(self, kind: str) -> list[sqlite3.Row]:
        """The bookings of `kind` whose key's third part (a day, where the key has one) is not
        written YYYY-MM-DD: keys tripbox 1.0.0 made from a day as it was printed. A code key has
        no third part and is never among them."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE kind=? AND item_key LIKE '%|%|%'"
            " AND item_key NOT LIKE 'draft|%'"
            " AND item_key NOT GLOB '*|*|[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
            " ORDER BY id",
            (kind,),
        ).fetchall()

    def by_pnr(self, confirmation: str) -> list[sqlite3.Row]:
        """`confirmation` holds one code or several joined by ', ' (two bookings on one flight).
        Removed rows are not grouping hints."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE removed_at IS NULL"
            " AND instr(', ' || confirmation || ', ', ', ' || ? || ', ') > 0 ORDER BY id",
            (confirmation,),
        ).fetchall()

    def by_code(self, kind: str, confirmation: str) -> list[sqlite3.Row]:
        """Rows of `kind` whose `confirmation` holds the code (alone, or among several joined
        by ', '), removed or not, by id."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE kind=?"
            " AND instr(', ' || confirmation || ', ', ', ' || ? || ', ') > 0 ORDER BY id",
            (kind, confirmation),
        ).fetchall()

    def without_code(self, kind: str, source: str) -> list[sqlite3.Row]:
        """Rows of `kind` from `source` (the payload's `source`) that hold no confirmation code
        and are not removed, by id."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE kind=? AND removed_at IS NULL"
            " AND COALESCE(confirmation, '') = '' AND json_valid(payload_json)"
            " AND json_extract(payload_json, '$.source') = ? ORDER BY id",
            (kind, source),
        ).fetchall()

    def by_alias(self, item_key: str) -> sqlite3.Row | None:
        """A row whose payload lists `item_key` under alt_keys (a confirmation code the owner
        corrected on the page: emails printing either code must land on this row)."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE json_valid(payload_json) AND EXISTS"
            " (SELECT 1 FROM json_each(payload_json, '$.alt_keys') WHERE value = ?)",
            (item_key,),
        ).fetchone()

    def removed(self) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM trip_items WHERE removed_at IS NOT NULL"
            " ORDER BY removed_at DESC, id DESC"
        ).fetchall()

    def unsorted(self) -> list[sqlite3.Row]:
        """Rows in no trip (a booking with no date) that are not removed, oldest first."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE trip_id IS NULL AND removed_at IS NULL"
            " ORDER BY created_at, id"
        ).fetchall()

    # a row the owner can see (not removed, in no trip or in one not deleted) with a check open
    _FLAGGED = (
        " removed_at IS NULL AND json_array_length(checks_json) > 0 AND (trip_id IS NULL"
        " OR trip_id IN (SELECT id FROM trips WHERE deleted_at IS NULL))"
    )

    def flagged(self, trip_id: int | None = None) -> list[sqlite3.Row]:
        """Visible rows with a check open: one trip's by start, or (None) every one, those in
        trips first by start, then those in none, oldest first."""
        if trip_id is not None:
            return self.c.execute(
                f"SELECT * FROM trip_items WHERE trip_id=? AND{self._FLAGGED}"
                " ORDER BY start_at, id",
                (trip_id,),
            ).fetchall()
        return self.c.execute(
            f"SELECT * FROM trip_items WHERE{self._FLAGGED}"
            " ORDER BY trip_id IS NULL, start_at, created_at, id"
        ).fetchall()

    def due_for_sync(
        self, *, now: str, claimed_before: str, blocked_only_by: str
    ) -> list[sqlite3.Row]:
        """Rows not removed and in no trip or one not deleted, whose sync is `pending`, or
        `blocked` with `blocked_only_by` as their one open check, whose `sync_after` is empty or
        at most `now` and whose claim is empty or earlier than `claimed_before`; oldest
        `updated_at` first. Every time is UTC ISO-8601 to the second, so they compare as text."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE removed_at IS NULL"
            " AND (trip_id IS NULL OR trip_id IN (SELECT id FROM trips WHERE deleted_at IS NULL))"
            " AND (sync_state='pending' OR (sync_state='blocked' AND checks_json=?))"
            " AND (sync_after IS NULL OR sync_after <= ?)"
            " AND (sync_claimed_at IS NULL OR sync_claimed_at < ?)"
            " ORDER BY updated_at, id",
            (json.dumps([blocked_only_by]), now, claimed_before),
        ).fetchall()

    def flights(self) -> list[sqlite3.Row]:
        """Every flight, removed or not and in any trip, by id."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE kind='flight' ORDER BY id"
        ).fetchall()

    def sent_flights(self) -> list[sqlite3.Row]:
        """Flights linked to a record in the flight log (`airtrail_flight_id`), not removed and in
        no trip or one not deleted, by id."""
        return self.c.execute(
            "SELECT * FROM trip_items WHERE kind='flight' AND airtrail_flight_id IS NOT NULL"
            " AND removed_at IS NULL"
            " AND (trip_id IS NULL OR trip_id IN (SELECT id FROM trips WHERE deleted_at IS NULL))"
            " ORDER BY id"
        ).fetchall()

    def flights_shown(self) -> list[sqlite3.Row]:
        """Flights the owner can see (not removed, in no trip or one not deleted): their sync
        state, their hold and their open checks, by id."""
        return self.c.execute(
            "SELECT id, sync_state, sync_hold, checks_json FROM trip_items WHERE kind='flight'"
            " AND removed_at IS NULL"
            " AND (trip_id IS NULL OR trip_id IN (SELECT id FROM trips WHERE deleted_at IS NULL))"
            " ORDER BY id"
        ).fetchall()

    def flag_counts(self) -> dict[int | None, int]:
        """Trip id (None: no trip) → the number of its visible rows with a check open."""
        rows = self.c.execute(
            f"SELECT trip_id, COUNT(*) FROM trip_items WHERE{self._FLAGGED} GROUP BY trip_id"
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def update(self, item_id: int, **fields: Any) -> None:
        if "payload" in fields:
            fields["payload_json"] = json.dumps(fields.pop("payload"), default=str)
        if "passengers" in fields:
            fields["passengers_json"] = json.dumps(list(fields.pop("passengers")))
        if "checks" in fields:
            fields["checks_json"] = json.dumps(list(fields.pop("checks")))
        if "told" in fields:
            told = fields.pop("told")
            fields["told_json"] = None if told is None else json.dumps(told, default=str)
        _update(self.c, "trip_items", item_id, {**fields, "updated_at": now_iso()})

    def delete(self, item_id: int) -> None:
        self.c.execute("DELETE FROM trip_items WHERE id=?", (item_id,))

    @staticmethod
    def payload(row: sqlite3.Row) -> dict:
        return json.loads(row["payload_json"])

    @staticmethod
    def passengers(row: sqlite3.Row) -> list[str]:
        return json.loads(row["passengers_json"] or "[]")


class _Kept:
    """A table of rows named by a hash (`KEY`), each with when it was made and last seen, of
    which `add` keeps the KEEP newest by `seen_at` (the one just added among them)."""

    TABLE = ""
    KEY = ""
    KEEP = 50

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def _add(self, key: str, **more: str) -> None:
        ts = now_iso()
        cols = ", ".join((self.KEY, *more, "created_at", "seen_at"))
        marks = ", ".join("?" * (len(more) + 3))
        self.c.execute(
            f"INSERT INTO {self.TABLE}({cols}) VALUES ({marks})", (key, *more.values(), ts, ts)
        )
        self.c.execute(
            f"DELETE FROM {self.TABLE} WHERE rowid NOT IN"
            f" (SELECT rowid FROM {self.TABLE} ORDER BY seen_at DESC, rowid DESC LIMIT ?)",
            (self.KEEP,),
        )

    def get(self, key: str) -> sqlite3.Row | None:
        return self.c.execute(f"SELECT * FROM {self.TABLE} WHERE {self.KEY}=?", (key,)).fetchone()

    def touch(self, key: str) -> None:
        """Seen now."""
        self.c.execute(f"UPDATE {self.TABLE} SET seen_at=? WHERE {self.KEY}=?", (now_iso(), key))

    def end(self, key: str) -> None:
        self.c.execute(f"DELETE FROM {self.TABLE} WHERE {self.KEY}=?", (key,))

    def end_all(self) -> None:
        self.c.execute(f"DELETE FROM {self.TABLE}")


class Sessions(_Kept):
    """Who is signed in to the page: the SHA-256 of each session's token (never the token) and
    `cred`, the fingerprint of the page's origin and password hash it was made under."""

    TABLE, KEY = "sessions", "token_hash"

    def add(self, token_hash: str, cred: str) -> None:
        self._add(token_hash, cred=cred)

    def keep_only(self, cred: str) -> None:
        """Every session made under another password ends."""
        self.c.execute("DELETE FROM sessions WHERE cred<>?", (cred,))


class Devices(_Kept):
    """The browsers that have signed in before, by the SHA-256 of their cookie's id."""

    TABLE, KEY = "devices", "id_hash"

    def add(self, id_hash: str) -> None:
        self._add(id_hash)


class Shares:
    """The share links of trips. A link is found by the SHA-256 of its token alone (`by_hash`);
    the token is kept only so the owner can copy it again. A link's row goes with its trip
    (ON DELETE CASCADE); revoking it keeps the row and stamps `revoked_at`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def add(self, *, trip_id: int, token: str, token_hash: str, shows: str) -> int:
        cur = self.c.execute(
            "INSERT INTO share_links(trip_id, token, token_hash, shows, created_at)"
            " VALUES (?,?,?,?,?)",
            (trip_id, token, token_hash, shows, now_iso()),
        )
        return int(cur.lastrowid)

    def by_hash(self, token_hash: str) -> sqlite3.Row | None:
        return self.c.execute(
            "SELECT * FROM share_links WHERE token_hash=?", (token_hash,)
        ).fetchone()

    def by_trip(self, trip_id: int) -> list[sqlite3.Row]:
        """The trip's links that are not revoked, oldest first."""
        return self.c.execute(
            "SELECT * FROM share_links WHERE trip_id=? AND revoked_at IS NULL ORDER BY id",
            (trip_id,),
        ).fetchall()

    def live(self) -> list[sqlite3.Row]:
        """Every link that is not revoked, oldest first."""
        return self.c.execute(
            "SELECT * FROM share_links WHERE revoked_at IS NULL ORDER BY id"
        ).fetchall()

    def revoke(self, link_id: int) -> int | None:
        """Stamp the link revoked (once: a second revoke keeps the first time); the id of its
        trip, or None when there is no such link."""
        row = self.c.execute("SELECT trip_id FROM share_links WHERE id=?", (link_id,)).fetchone()
        if row is None:
            return None
        self.c.execute(
            "UPDATE share_links SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (now_iso(), link_id),
        )
        return int(row["trip_id"])


class Uploads:
    """The waiting list of what the page was handed to read. The page adds a row (through
    `uploads.submit`); the worker claims the oldest waiting one and finishes it once read, which
    drops its body. Outside the server a row is named by its token alone."""

    FINISHED = ("filed", "nothing", "failed")

    def __init__(
        self, conn: sqlite3.Connection, transaction: Callable[[], AbstractContextManager[None]]
    ) -> None:
        self.c = conn
        self._transaction = transaction  # the ledger's: BEGIN IMMEDIATE, or join one open

    def add(
        self,
        *,
        token: str,
        kind: str,
        name: str | None,
        sha256: str,
        body: bytes,
        submitted_by: str | None,
    ) -> int:
        ts = now_iso()
        cur = self.c.execute(
            "INSERT INTO uploads(token, kind, name, sha256, size, body, submitted_by,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (token, kind, name, sha256, len(body), body, submitted_by, ts, ts),
        )
        return int(cur.lastrowid)

    def get(self, token: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM uploads WHERE token=?", (token,)).fetchone()

    def by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM uploads WHERE sha256=?", (sha256,)).fetchone()

    def claim_next(self) -> sqlite3.Row | None:
        """The oldest waiting upload, set `processing` in the same statement, one more reading
        counted, and returned as it now stands; None when none waits. Two workers never claim
        one row."""
        with self._transaction():
            rows = self.c.execute(
                "UPDATE uploads SET state='processing', attempts=attempts+1, updated_at=?"
                " WHERE id ="
                " (SELECT id FROM uploads WHERE state='waiting' ORDER BY id LIMIT 1)"
                " RETURNING *",
                (now_iso(),),
            ).fetchall()
        return rows[0] if rows else None

    def finish(
        self,
        upload_id: int,
        state: str,
        *,
        message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        """The upload was read: it ends `state` (filed, nothing or failed), with the message it
        became and the word for why nothing was filed; its body goes, its name and size stay."""
        if state not in self.FINISHED:
            raise ValueError(f"an upload is finished as one of {self.FINISHED}, not {state!r}")
        self.c.execute(
            "UPDATE uploads SET state=?, message_id=?, error=?, body=NULL, updated_at=?"
            " WHERE id=?",
            (state, message_id, error, now_iso(), upload_id),
        )

    def take_again(self, upload_id: int, body: bytes) -> None:
        """An upload that ended with nothing filed waits again, its body (the same bytes) stored
        anew and what the last reading said of it gone, its readings counted from none; its
        token, name and kind stay."""
        self.c.execute(
            "UPDATE uploads SET state='waiting', body=?, size=?, message_id=NULL, error=NULL,"
            " attempts=0, updated_at=? WHERE id=?",
            (body, len(body), now_iso(), upload_id),
        )

    def read_again(self, message_id: int, state: str, error: str | None, shown: int) -> None:
        """The message an upload ended as with nothing filed was read again (a replay, the
        resume pass once the model can read it): the upload ends as this reading leaves it, as
        a first reading ending so would — `filed`, `nothing` or `failed`, with the word for why
        nothing was filed, and `shown`, the message whose bookings its status page shows."""
        if state not in self.FINISHED:
            raise ValueError(f"an upload read again ends as one of {self.FINISHED}, not {state!r}")
        self.c.execute(
            "UPDATE uploads SET state=?, error=?, message_id=?, updated_at=?"
            " WHERE message_id=? AND state IN ('nothing', 'failed')",
            (state, error, shown, now_iso(), message_id),
        )

    def requeue(self) -> tuple[int, int]:
        """What a worker that stopped left being read waits again, but an upload taken
        `uploads.MAX_READINGS` times, which is given up: `failed`, with the word `stopped`, its
        body kept. How many wait again, and how many were given up."""
        from tripbox.uploads import MAX_READINGS

        with self._transaction():
            ts = now_iso()
            given_up = self.c.execute(
                "UPDATE uploads SET state='failed', error='stopped', updated_at=?"
                " WHERE state='processing' AND attempts >= ?",
                (ts, MAX_READINGS),
            ).rowcount
            back = self.c.execute(
                "UPDATE uploads SET state='waiting', updated_at=? WHERE state='processing'",
                (ts,),
            ).rowcount
        return back, given_up

    def waiting(self) -> int:
        """How many uploads are not read yet: waiting, or being read."""
        row = self.c.execute(
            "SELECT COUNT(*) FROM uploads WHERE state IN ('waiting', 'processing')"
        ).fetchone()
        return int(row[0])


class Covers:
    """The cover photo of a trip: at most one row, keyed by the trip. This is the only writer of
    `covers`; a row goes with its trip (ON DELETE CASCADE)."""

    FIELDS = frozenset(
        {
            "source",
            "state",
            "place",
            "digest",
            "large",
            "small",
            "upload",
            "credit_json",
            "error",
            "attempts",
            "retry_after",
        }
    )
    # every column but the images: what a pass over all covers reads without loading them
    LOOKUP = (
        "trip_id, source, state, place, digest, credit_json, error, attempts, retry_after,"
        " created_at, updated_at"
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def get(self, trip_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM covers WHERE trip_id=?", (trip_id,)).fetchone()

    def by_digest(self, digest: str) -> sqlite3.Row | None:
        """A cover made from the same picture (lowest trip first), or None."""
        return self.c.execute(
            "SELECT * FROM covers WHERE digest=? ORDER BY trip_id LIMIT 1", (digest,)
        ).fetchone()

    def put(self, trip_id: int, **fields: Any) -> None:
        """Make the trip's row, or change the fields given on the one it has (`created_at` is
        kept, `updated_at` is stamped). A new row needs `source` and `state`."""
        unknown = set(fields) - self.FIELDS
        if unknown:
            raise ValueError(f"not cover fields: {sorted(unknown)}")
        ts = now_iso()
        sets = ", ".join(f"{k}=?" for k in (*fields, "updated_at"))
        cur = self.c.execute(
            f"UPDATE covers SET {sets} WHERE trip_id=?", [*fields.values(), ts, trip_id]
        )
        if cur.rowcount:
            return
        cols = ["trip_id", *fields, "created_at", "updated_at"]
        self.c.execute(
            f"INSERT INTO covers({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [trip_id, *fields.values(), ts, ts],
        )

    def delete(self, trip_id: int) -> None:
        self.c.execute("DELETE FROM covers WHERE trip_id=?", (trip_id,))

    def move(self, src_trip_id: int, dst_trip_id: int) -> None:
        """The source trip's cover becomes the destination's, which has none."""
        self.c.execute(
            "UPDATE covers SET trip_id=?, updated_at=? WHERE trip_id=?",
            (dst_trip_id, now_iso(), src_trip_id),
        )

    def pending_upload(self) -> sqlite3.Row | None:
        """The oldest owner's upload the worker has not yet made into a cover."""
        return self.c.execute(
            "SELECT * FROM covers WHERE source='owner' AND state='pending'"
            " ORDER BY created_at, trip_id LIMIT 1"
        ).fetchone()

    def all_for_lookup(self) -> list[sqlite3.Row]:
        """Every cover without its images, by trip."""
        return self.c.execute(f"SELECT {self.LOOKUP} FROM covers ORDER BY trip_id").fetchall()
