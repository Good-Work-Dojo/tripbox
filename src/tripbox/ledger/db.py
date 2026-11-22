# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""SQLite ledger: schema + migrations, connection settings (WAL, busy timeout), repositories."""

from __future__ import annotations

import filecmp
import glob
import hashlib
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from tripbox import util
from tripbox.ledger import repo
from tripbox.log import FILE_ONLY

log = logging.getLogger("tripbox.ledger")

SCHEMA_VERSION = 1

# Steps keyed by the version they produce; each takes the open connection and runs inside one
# transaction with foreign keys off (see Ledger._apply). A step uses `conn.execute` only and never
# commits, rolls back or calls `executescript`: any of those ends the transaction, which makes the
# step non-atomic and releases the write lock a waiting opener needs held. SCHEMA never runs on a
# ledger of an older version: the steps make everything the new version adds, and the last one
# commits only if the ledger then has a new ledger's tables, columns and indexes. A table is
# rebuilt with `rebuild_table`. Empty until the first schema change after release.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

# The shape (SCHEMA_SHAPE) each released version's ledger states, by its version: a ledger of an
# older version is upgraded only from the shape that version was released with. A step to a
# version short of this one stamps that version's entry, so every such step needs one
# (`_each_step_is_released`, at import).
RELEASED_SHAPES: dict[int, str] = {}


def _each_step_is_released() -> None:
    """RuntimeError for a step to a version short of this one with no RELEASED_SHAPES entry,
    which it must stamp."""
    for target in sorted(MIGRATIONS):
        if target < SCHEMA_VERSION and target not in RELEASED_SHAPES:
            raise RuntimeError(f"the step to v{target} has no released shape")


_each_step_is_released()

# messages.message_id is indexed but NOT unique. The same email legitimately arrives twice (a Gmail
# filter forward and a later "forward as attachment" carry the original Message-ID); whether the
# second copy is processed is a policy decision in pipeline/run.py, not a constraint violation.
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- messages: one row per email or paste read. status is archived while it is read (or when a
-- reading stopped half-way), waiting when nothing read it and only the model can (error: why,
-- pipeline.run.WAIT_REASONS; so too while it is read again), else processed, ignored, rejected
-- or failed. found_at is when its
-- bookings first reached the ledger, which every reading of it dates its findings by.
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  message_id TEXT,
  sha256 TEXT UNIQUE NOT NULL,
  source TEXT NOT NULL,
  mail_uid INTEGER,
  received_at TEXT,
  email_date TEXT,
  from_addr TEXT,
  subject TEXT,
  origin_ok INTEGER NOT NULL DEFAULT 0,
  archive_path TEXT,
  status TEXT NOT NULL,
  document_type TEXT,
  parser TEXT,
  error TEXT,
  answer_json TEXT,
  found_at TEXT,
  processed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_mid ON messages(message_id);
-- items is the log of what each message did: one row per booking it read (n: its place in the
-- message, from 0), written once. outcome is what became of the booking, detail the word beside
-- updated or ignored or a failure's sentence, item_key the booking's key once the message was
-- filed, payload_json what the message said, trip_item_id the booking. Reading a message again
-- deletes its rows and writes them anew.
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id),
  n INTEGER NOT NULL,
  kind TEXT NOT NULL,
  item_key TEXT NOT NULL,
  outcome TEXT NOT NULL
    CHECK (outcome IN ('filed', 'updated', 'duplicate', 'ignored', 'failed')),
  detail TEXT,
  payload_json TEXT NOT NULL,
  confidence REAL,
  trip_item_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_key ON items(item_key);
CREATE INDEX IF NOT EXISTS items_message ON items(message_id);
CREATE INDEX IF NOT EXISTS items_trip_item ON items(trip_item_id);
CREATE TABLE IF NOT EXISTS ref_cache(
  kind TEXT NOT NULL, code TEXT NOT NULL, json TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(kind, code)
);
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY, message_id INTEGER, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL, created_at TEXT NOT NULL
);
-- aeroapi_calls.item_id and enrichment.item_id hold a trip item's id (trip_items.id): the
-- flight that enrichment looked up.
CREATE TABLE IF NOT EXISTS aeroapi_calls(
  id INTEGER PRIMARY KEY, month TEXT NOT NULL, item_id INTEGER, ident TEXT, result_sets INTEGER,
  est_cost_usd REAL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment(
  item_id INTEGER PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT,
  enriched_at TEXT, fields_set_json TEXT
);
-- trips and trip_items hold the one current state of each booking, one row per booking
-- (source_message_id: the last message that filed or changed it; NULL for one added by hand or
-- from AirTrail). Maintained only by trips.store.TripStore (the executor and the web repairs go
-- through it). Their ids are never given twice (AUTOINCREMENT): a Telegram button and a page
-- name a booking or a trip by its id, and a row goes when a draft becomes one with its booking
-- and when Recently deleted is purged. A migration that rebuilds either table carries its row
-- of sqlite_sequence over to the new table, or the next row takes an id given before.
CREATE TABLE IF NOT EXISTS trips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  place TEXT,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  notes TEXT,
  deleted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trips_dates ON trips(start_date, end_date);
CREATE TABLE IF NOT EXISTS trip_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id INTEGER REFERENCES trips(id),
  item_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  start_at TEXT,
  end_at TEXT,
  start_tz TEXT,
  end_tz TEXT,
  confirmation TEXT,
  payload_json TEXT NOT NULL,
  passengers_json TEXT NOT NULL,
  checks_json TEXT NOT NULL DEFAULT '[]',
  sync_state TEXT NOT NULL DEFAULT 'off'
    CHECK (sync_state IN ('off', 'blocked', 'pending', 'synced')),
  sync_hold TEXT CHECK (sync_hold IN ('past', 'deleted')),
  airtrail_flight_id INTEGER,
  told_json TEXT,
  sync_error TEXT CHECK (sync_error IN ('outage', 'refused', 'airport')),
  sync_error_at TEXT,
  sync_attempts INTEGER NOT NULL DEFAULT 0,
  sync_after TEXT,
  sync_claimed_at TEXT,
  sync_logged TEXT,
  source_message_id INTEGER REFERENCES messages(id),
  removed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((trip_id IS NULL) = (start_at IS NULL))
);
CREATE INDEX IF NOT EXISTS trip_items_trip ON trip_items(trip_id);
CREATE INDEX IF NOT EXISTS trip_items_sync ON trip_items(sync_state);
-- uploads is the waiting list of what the page was handed to read (a paste, an email file, a
-- PDF): the page adds a row, the worker claims the oldest waiting one, reads it through the
-- pipeline and finishes it, which drops its body. token names it outside the server (its id
-- never leaves it); sha256 makes the same bytes one upload; name is shown, never opened;
-- message_id is the message it became; attempts counts the times a worker took it to read.
CREATE TABLE IF NOT EXISTS uploads(
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('text', 'eml', 'pdf')),
  name TEXT,
  sha256 TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL,
  body BLOB,
  submitted_by TEXT,
  state TEXT NOT NULL DEFAULT 'waiting'
    CHECK (state IN ('waiting', 'processing', 'filed', 'nothing', 'failed')),
  message_id INTEGER REFERENCES messages(id),
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- sessions: who is signed in to the page with its password. A session is a random token in the
-- browser's cookie, of which only its SHA-256 is kept here; cred is a fingerprint of the
-- password's hash it was made under (a changed password ends it); seen_at is rewritten at most
-- once an hour. devices: the browsers that have signed in before, by the SHA-256 of their
-- cookie's id, each with a sign-in throttle of its own. Each keeps its 50 newest by seen_at.
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  cred TEXT NOT NULL,
  created_at TEXT NOT NULL,
  seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices(
  id_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  seen_at TEXT NOT NULL
);
-- share_links: the links that show one trip, read-only, to anyone who holds one, without
-- signing in. token is kept so the owner can copy the link again; a link is found by
-- token_hash (its SHA-256) alone. shows: the fields it shows (trips.shares.FIELDS), as JSON.
-- A trip that goes for good takes its links with it (a merge empties and deletes its source).
CREATE TABLE IF NOT EXISTS share_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  token TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  shows TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS share_links_trip ON share_links(trip_id);
"""

# What SCHEMA says, white space aside. Version 1 is released, so an in-place edit of SCHEMA is
# refused by a test and a change goes through a migration. The version alone cannot tell a ledger
# made before an edit: every ledger states the shape it was written with (`kv` row
# `schema_shape`), and one stating another shape than its version's (this one's, or an older
# one's RELEASED_SHAPES), or none, is refused at open.
SCHEMA_SHAPE: str = hashlib.sha256(re.sub(r"\s+", " ", SCHEMA).encode()).hexdigest()[:12]

OUTDATED = (
    "this ledger was written by an earlier development version of tripbox and this version"
    " cannot read it; move the file away and start again with an empty one"
)
NEWER = (
    "this ledger was written by a newer version of tripbox (ledger version {v}; this one reads"
    " up to {max}); upgrade tripbox, or restore a backup made with this version"
)
NOT_COPIED = (
    "the ledger needs an upgrade and its copy could not be written beside it; nothing was"
    ' changed: free some space or fix the folder ("tripbox doctor"), then start again'
)
UPGRADED = "the ledger is upgraded from version %d to %d; a copy is kept as %s"


class OutdatedLedger(RuntimeError):
    """A ledger this version does not open, and leaves as it was; its text is the one line a
    command prints (`ledger: …`). As such: one written with another SCHEMA than this one, or an
    older version's with no released shape or no step to this version. Nothing converts it: no
    install exists from before the first release, and the bookings it held are filed again when
    their emails are read into a new ledger."""


class NewerLedger(OutdatedLedger):
    """A ledger of a later version than this one reads."""


class CopyNotWritten(OutdatedLedger):
    """A ledger that needs an upgrade whose copy could not be written beside it: no step ran."""


def _stated_version(conn: sqlite3.Connection) -> int | None:
    """The version the ledger states, when this version can open it: one of this version that
    states SCHEMA_SHAPE, or an older one that states its RELEASED_SHAPES and has a step to every
    version after it. None for a ledger without a version row: a new one, or one still being
    made by an opener racing this one (the version and the shape are written in one
    transaction). Else NewerLedger (a later version) or OutdatedLedger."""
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('schema_version', 'kv')"
        )
    }
    if "schema_version" not in tables:
        return None
    # the version and the shape in one statement, so in one snapshot: an upgrade step that
    # commits between two reads would pair one version's number with the next one's shape
    shape_of = "(SELECT v FROM kv WHERE k='schema_shape')" if "kv" in tables else "NULL"
    version, shape = conn.execute(
        f"SELECT (SELECT v FROM schema_version LIMIT 1), {shape_of}"
    ).fetchone()
    if version is None:
        return None
    version = int(version)
    if version > SCHEMA_VERSION:
        raise NewerLedger(NEWER.format(v=version, max=SCHEMA_VERSION))
    stated = SCHEMA_SHAPE if version == SCHEMA_VERSION else RELEASED_SHAPES.get(version)
    steps = all(n in MIGRATIONS for n in range(version + 1, SCHEMA_VERSION + 1))
    if stated is None or shape is None or shape != stated or not steps:
        raise OutdatedLedger(OUTDATED)
    return version


def _in_use(path: Path) -> bool:
    """A -wal or a -journal beside the file: a connection in WAL mode has it open, a writer in
    rollback mode is changing it, or one of them died doing so. With neither, nothing writes it."""
    return any(path.with_name(path.name + end).exists() for end in ("-wal", "-journal"))


def _refuse_from_the_file(path: Path) -> None:
    """Refuse a file this version does not open (`_stated_version`) from its bytes alone,
    before anything opens it for writing, when
    nothing is changing it: no -wal and no -journal beside it, before the read and after. The
    read goes through an immutable read-only connection, which takes no lock and creates,
    changes or removes nothing; it is exact only of a file nobody writes meanwhile (a checkpoint
    or a rollback-mode commit under it could hand it a torn page). Otherwise, and when it cannot
    read the file, it decides nothing: `Ledger.open` checks on its real connection, before any
    statement that writes."""
    if not path.is_file() or path.stat().st_size == 0 or _in_use(path):
        return
    try:
        conn = sqlite3.connect(f"file:{quote(str(path.absolute()))}?mode=ro&immutable=1", uri=True)
        try:
            _stated_version(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return
    except OutdatedLedger:
        if not _in_use(path):
            raise


def _set_wal(conn: sqlite3.Connection, tries: int = 200) -> None:
    """Switch a rollback-mode file to WAL (a restored backup snapshot, a brand-new ledger).
    SQLite's deadlock avoidance makes this PRAGMA fail at once with 'database is locked' when
    another connection is mid-way through the same switch, without consulting the busy handler
    — so three containers opening the same fresh file at deploy time need a short retry."""
    import time

    for i in range(tries):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if i == tries - 1:
                raise
            time.sleep(0.05)


def _schema_of(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """The ledger's own tables with their columns' names, and its indexes with their tables and
    columns; SQLite's own (`sqlite_sequence`, a UNIQUE's index) are not the ledger's."""
    names = conn.execute(
        "SELECT type, name, tbl_name FROM sqlite_master"
        " WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {
        name: frozenset(r[1] for r in conn.execute(f'PRAGMA table_info("{name}")'))
        for kind, name, _ in names
        if kind == "table"
    }
    indexes = {
        name: (table, tuple(r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')))
        for kind, name, table in names
        if kind == "index"
    }
    return tables, indexes


def _new_schema() -> tuple[dict, dict]:
    """What a new ledger of this version holds (`_schema_of`)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        return _schema_of(conn)
    finally:
        conn.close()


def rebuild_table(
    conn: sqlite3.Connection, table: str, create_sql: str, columns: list[str] | tuple[str, ...]
) -> None:
    """Inside a migration step: `table` made again by `create_sql` (its CREATE TABLE, naming
    `table`), keeping its rows. The new table is made beside the old one, `columns` (each in
    both) are copied into it, the table's row of `sqlite_sequence` is carried over (so an id is
    never given twice), the old one is dropped and the new one renamed; never `ALTER TABLE …
    RENAME` the old one first, which rewrites every reference to it. Its indexes go with the old
    table: the step makes them again."""
    beside = f"{table}__rebuilt"
    made, n = re.subn(
        rf"^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?){re.escape(table)}\b",
        rf"\g<1>{beside}",
        create_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if n != 1:
        raise ValueError(f"create_sql does not create {table}")
    conn.execute(made)
    listed = ", ".join(f'"{c}"' for c in columns)
    conn.execute(f'INSERT INTO "{beside}"({listed}) SELECT {listed} FROM "{table}"')
    counted = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    given = None
    if counted is not None:
        given = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{beside}" RENAME TO "{table}"')
    if given is not None:
        now = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        seq = max(given[0], now[0] if now is not None else 0)
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, seq))


def _copies_of(path: Path, end: str) -> list[Path]:
    """The copies beside the ledger at `path` made before this version's upgrade whose names end
    in `end`, oldest first (their names hold the time)."""
    return sorted(path.parent.glob(f"{glob.escape(path.stem)}-before-v{SCHEMA_VERSION}-*{end}"))


def _sweep_parts(path: Path) -> None:
    """Remove what a copy that was killed left: its `.part`, and SQLite's files beside it. Only
    the opener holding the write lock copies, so none of them is being written."""
    for part in _copies_of(path, ".db.part"):
        for left in (part, *(part.with_name(part.name + e) for e in ("-wal", "-shm", "-journal"))):
            left.unlink(missing_ok=True)


def _same_as_the_last_copy(path: Path, part: Path) -> str | None:
    """The name of the newest finished copy when `part` holds the same bytes, else None. A copy
    that cannot be read (another user made it) is not the same: a fresh one is taken."""
    try:
        finished = sorted(
            (c for c in _copies_of(path, ".db") if c.is_file()),
            key=lambda c: (c.stat().st_mtime_ns, c.name),
        )
        if finished and filecmp.cmp(finished[-1], part, shallow=False):
            return finished[-1].name
    except OSError:
        return None
    return None


def write_copy(path: Path, to: Path, source: sqlite3.Connection | None = None) -> None:
    """Write the ledger at `path` into `to`, a new file made here (mode 600; one already there
    is an error), through SQLite's online backup: one consistent state of the ledger whatever
    writes it meanwhile, read from `source` when given, else from a second, read-only
    connection; then flushed to disk. `to` is removed when the copy fails. Never a plain copy
    of the file, which misses what its -wal holds."""
    os.close(os.open(to, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        reader = source
        if reader is None:
            reader = sqlite3.connect(f"file:{quote(str(path.absolute()))}?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(str(to))
            try:
                reader.backup(copy)
            finally:
                copy.close()
        finally:
            if source is None:
                reader.close()
        os.chmod(to, 0o600)
        fd = os.open(to, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        to.unlink(missing_ok=True)
        raise


def _stated_by_copy(part: Path) -> int | None:
    """The version a finished copy states, read without a lock or a file made beside it."""
    copy = sqlite3.connect(f"file:{quote(str(part.absolute()))}?mode=ro&immutable=1", uri=True)
    try:
        stated = copy.execute("SELECT v FROM schema_version").fetchone()
    finally:
        copy.close()
    return None if stated is None else int(stated[0])


def _copy_beside(path: Path, version: int) -> str:
    """Copy the ledger at `path`, as it stands at `version`, beside itself as
    `<name>-before-v<SCHEMA_VERSION>-<UTC time>.db` (mode 600; `write_copy`): written as
    `.part` (what a killed copy left is swept first), checked to state `version`, then renamed,
    unless it holds the same bytes as the newest copy before this version: that one is named
    instead. The copy's name; CopyNotWritten, leaving no `.part`, when it cannot be written."""
    when = util.now().strftime("%Y%m%dT%H%M%SZ")
    name = f"{path.stem}-before-v{SCHEMA_VERSION}-{when}.db"
    final, part = path.with_name(name), path.with_name(name + ".part")
    try:
        try:
            _sweep_parts(path)
            write_copy(path, part)
            if _stated_by_copy(part) != version:
                raise OSError("the copy does not state the version copied")
            same = _same_as_the_last_copy(path, part)
            if same is not None:
                # a start that failed after its copy, again: the ledger did not change since,
                # so that copy stands for this one (none is ever removed)
                part.unlink()
                return same
            n = 1
            while os.path.lexists(final):  # made this same second, of a ledger since changed
                n += 1
                name = f"{path.stem}-before-v{SCHEMA_VERSION}-{when}-{n}.db"
                final = path.with_name(name)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    except (OSError, sqlite3.Error) as exc:
        # the command says it in one line; why, for the log file
        log.warning(
            "the ledger's copy before its upgrade could not be written (%s)", exc, extra=FILE_ONLY
        )
        raise CopyNotWritten(NOT_COPIED) from exc
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)  # the rename itself
        finally:
            os.close(fd)
    except OSError:
        pass
    return name


class Ledger:
    def __init__(self, conn: sqlite3.Connection, path: Path | None = None) -> None:
        self.conn = conn
        self.path = path  # the file, or None in memory
        self.kv = repo.KV(conn)
        self.messages = repo.Messages(conn)
        self.items = repo.Items(conn)
        self.refcache = repo.RefCache(conn)
        self.llm = repo.LLMCalls(conn)
        self.aeroapi = repo.AeroApiCalls(conn)
        self.enrichment = repo.Enrichment(conn)
        self.trips = repo.Trips(conn)
        self.trip_items = repo.TripItems(conn)
        self.uploads = repo.Uploads(conn, self.transaction)
        self.sessions = repo.Sessions(conn)
        self.devices = repo.Devices(conn)
        self.shares = repo.Shares(conn)

    @classmethod
    def open(cls, path: Path | str) -> Ledger:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _refuse_from_the_file(p)
        # a -wal already there is another connection's, or one a process that died left
        theirs = p.with_name(p.name + "-wal").exists()
        conn = sqlite3.connect(str(p), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            # under the locks, before the first statement that can write: the switch to WAL
            # rewrites a rollback-mode file's header
            _stated_version(conn)
            _set_wal(conn)
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            ledger = cls(conn, p)
            ledger.migrate()
        except BaseException as exc:
            if theirs and isinstance(exc, OutdatedLedger):
                # closing as the last connection would fold that -wal into the file and
                # remove it: a refused ledger is left as it was
                conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            conn.close()
            raise
        return ledger

    @classmethod
    def memory(cls) -> Ledger:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ledger = cls(conn)
        ledger.migrate()
        return ledger

    def migrate(self) -> None:
        """A new ledger is made and stamped, one of this version made sure of (SCHEMA, every
        statement of which makes only what is missing), and one of an older version upgraded
        step by step, never by SCHEMA: before the first step runs, it is copied beside itself
        (`_copy_beside`; none in memory, which is always new)."""
        found = _stated_version(self.conn)  # before any statement of SCHEMA touches the file
        if found is None or found == SCHEMA_VERSION:
            self.conn.executescript(SCHEMA)
            if found is None:
                self._stamp()
            return
        _each_step_is_released()
        copied: tuple[int, str] | None = None
        for target in range(found + 1, SCHEMA_VERSION + 1):
            copied = self._apply(target, MIGRATIONS[target], copy=copied is None) or copied
        if copied is not None:
            log.info(UPGRADED, copied[0], SCHEMA_VERSION, copied[1])

    def _stamp(self) -> None:
        """Write a new ledger's version and shape. Each is one INSERT … WHERE NOT EXISTS, so
        openers racing on a brand-new file write exactly one of each, and both run in one
        transaction, so no opener finds the version without the shape and refuses the file."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "INSERT INTO schema_version(v) SELECT ? WHERE NOT EXISTS"
                " (SELECT 1 FROM schema_version)",
                (SCHEMA_VERSION,),
            )
            c.execute(
                "INSERT INTO kv(k, v) SELECT 'schema_shape', ? WHERE NOT EXISTS"
                " (SELECT 1 FROM kv WHERE k='schema_shape')",
                (SCHEMA_SHAPE,),
            )
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise

    def _apply(
        self, target: int, step: Callable[[sqlite3.Connection], None], *, copy: bool = False
    ) -> tuple[int, str] | None:
        """Run one migration step atomically. The connection is in autocommit mode
        (isolation_level=None), so Python opens no transaction of its own and the explicit
        BEGIN IMMEDIATE below owns it, DDL included. Foreign keys are switched off before BEGIN
        (the pragma does nothing inside a transaction): with them on, dropping a referenced
        parent table during a rebuild runs foreign-key checks and, with ON DELETE actions, can
        delete child rows. The version is re-read once the write lock is held: an opener that lost a
        race finds it already at the target and commits without running the step. With `copy`,
        a step about to run first has the ledger copied beside itself (`_copy_beside`; the
        version it was copied at and the copy's name are returned), and none runs when that
        fails. The last step must leave a new ledger's tables, columns and indexes. The step's
        version and its shape (SCHEMA_SHAPE for the last, else its RELEASED_SHAPES entry) are
        written together, whatever the step did to `kv`."""
        c = self.conn
        copied = None
        c.execute("PRAGMA foreign_keys=OFF")
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                version = int(c.execute("SELECT v FROM schema_version").fetchone()[0])
                if version >= target:
                    c.execute("COMMIT")
                    return None
                if copy and self.path is not None:
                    copied = (version, _copy_beside(self.path, version))
                step(c)
                if not c.in_transaction:
                    raise RuntimeError(
                        f"migration to v{target} ended the transaction; a step must not commit"
                    )
                bad = c.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise sqlite3.IntegrityError(
                        f"foreign_key_check failed after migration to v{target}: {len(bad)} rows"
                    )
                if target == SCHEMA_VERSION and _schema_of(c) != _new_schema():
                    raise RuntimeError(
                        f"migration to v{target} does not end in this version's schema"
                    )
                # a step short of this version ends in its own released shape: a ledger left
                # there (the next step failed, or another opener reads it now) is an older one
                # to upgrade, never one refused
                shape = SCHEMA_SHAPE if target == SCHEMA_VERSION else RELEASED_SHAPES[target]
                c.execute("UPDATE schema_version SET v=?", (target,))
                c.execute(
                    "INSERT INTO kv(k, v) VALUES ('schema_shape', ?)"
                    " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (shape,),
                )
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        finally:
            c.execute("PRAGMA foreign_keys=ON")
        return copied

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """What runs inside is written all at once or not at all (BEGIN IMMEDIATE … COMMIT,
        ROLLBACK on any error); inside a transaction already open it joins that one."""
        c = self.conn
        if c.in_transaction:
            yield
            return
        c.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    trip_items_payload = staticmethod(repo.TripItems.payload)
    trip_items_passengers = staticmethod(repo.TripItems.passengers)

    def version(self) -> int:
        return int(self.conn.execute("SELECT v FROM schema_version").fetchone()[0])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
