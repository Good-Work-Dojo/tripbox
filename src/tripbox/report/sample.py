# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox report`: one text file to attach to a parse problem — a redacted copy of an email
and what tripbox did with it.

A message of the ledger is reported from what the ledger and the archive hold, read without
writing: its row, its stored answer, its log rows and its saved email. A file the ledger does
not hold is read here, in a temporary directory that is removed, with no model, no notifier and
no flight log. Nothing here reaches the network: no model, mailbox, flight log, flight-data or
notification client is ever built, and it runs the same with TRIPBOX_NO_NETWORK set.

The redaction is `tripbox.redact`'s; this module gathers what is known of the email (the
travellers file, what was read from it, the names typed with `--name`, the names the email
itself gives away) and writes the report's lines."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tripbox import __version__, lists, redact, uploads
from tripbox.config import OFFLINE_ENV, Settings
from tripbox.parse.models import READ_BY_MODEL
from tripbox.parse.replay import WIRE_SCHEMA_VERSION
from tripbox.util import parse_iso

# The report's first lines, which say what it is before anything else is read.
HEAD = (
    "tripbox report: read all of this before you share it.",
    "tripbox cannot know every personal detail in an email. Dates, times, flight and train"
    " numbers,",
    "airports, cities, airline and hotel names and prices are kept, because they are what a"
    " parser",
    "reads. Names, addresses, codes and numbers it recognised are replaced by words in square"
    " brackets.",
    "Remove anything else you do not want to be public.",
)
# Each kind the redactor counts, as the report names it.
COUNTED = (
    ("name", "names"),
    ("email", "email addresses"),
    ("phone", "phone numbers"),
    ("reference", "booking references"),
    ("ticket", "ticket numbers"),
    ("address", "street addresses"),
    ("number", "numbers"),
    ("link", "link queries"),
)
DID, READ, EMAIL = "== what tripbox did ==", "== what was read ==", "== the email =="
NOTHING = "nothing"
FROM_LEDGER = "ledger message #{n}"
FROM_FILE = "a file, not in the ledger (read here without a model)"
NO_NAME = (
    "report: no traveller's name is known for this email: names in it are NOT replaced."
    ' Name them: --name "Alex Sample"'
)
GUESSED = (
    "report: no travellers file and no --name: only names the email marks as names were replaced;"
    ' any other is NOT. Name them: --name "Alex Sample"'
)
READ_IT = "report: read the result before you share it; tripbox cannot know every personal detail"
LISTED = "report: the newest messages"
NO_MESSAGE = "report: the ledger holds no message yet"
SHOWN = "shown: each text part decoded, as UTF-8 text, whatever it was sent as"
MAX_BYTES = 20 * 1024 * 1024  # the largest email a report is made of
NEWEST = 10  # messages the listing shows
SUBJECT_SHOWN = 60  # characters of a subject the listing shows
# What a booking's payload holds of its message, which the report's email section shows redacted.
OF_THE_MESSAGE = ("message_id", "email_date", "subject", "from", "message_hash")
_NUMBER = re.compile(r"[0-9]{1,12}")


class Refused(Exception):
    """A report that is not made: the one line that says why (exit 2, nothing written)."""


@dataclass
class Made:
    """A report: its text, and the lines standard error gets."""

    text: str
    said: list[str] = field(default_factory=list)


@dataclass
class _Held:
    """What a ledger holds of one message: its status, who read it (the `parser` word), why it
    waits, each booking's log row as the report shows it, and what was read from it (None:
    nothing)."""

    status: str
    parser: str | None
    waiting: str | None
    bookings: list[dict]
    read: dict | None


def read_by(parser: str | None) -> str:
    """Who read a message, in the report's words."""
    if parser == "kitinerary":
        return "kitinerary"
    return "a model" if parser == READ_BY_MODEL else "nobody"


def _word(value: Any) -> str:
    """A status as a report prints it outside its redacted sections: the ledger's word when it
    is one, never whatever else the column holds."""
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,24}", value) else "unknown"


# ----------------------------------------------------------------------------- the ledger, read
# the columns of `messages` a report reads
_COLUMNS = (
    "id", "received_at", "status", "parser", "subject", "error", "answer_json", "document_type",
    "archive_path", "sha256",
)  # fmt: skip
def _reader(path: Path) -> sqlite3.Connection | None:
    """The ledger opened as `doctor` opens it, through a connection that writes nothing and, with
    no journal beside the ledger, makes nothing beside it; None when there is no ledger."""
    if not os.path.lexists(path):
        return None
    busy = any(path.with_name(path.name + end).exists() for end in ("-wal", "-journal"))
    mode = "ro" if busy else "ro&immutable=1"
    conn = sqlite3.connect(f"file:{quote(str(path.absolute()))}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            conn.close()
            raise
        conn.close()
        return None  # a file nothing has made a ledger of yet
    # a ledger of an older tripbox may lack a column a report reads
    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for name in _COLUMNS:
        if name not in have:
            conn.close()
            raise sqlite3.OperationalError(f"no such column: messages.{name}")
    return conn


def _shown_key(key: str) -> str:
    """A booking's key as a report shows it: a draft key names its email by a digest of its
    bytes, which says nothing a reader needs."""
    parts = key.split("|")
    if parts[0] == "draft" and len(parts) == 3:
        return f"draft|{redact.MESSAGE_ID}|{parts[2]}"
    return key


def _json(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) else default
    except ValueError:
        return default


def _held(conn: sqlite3.Connection, row: sqlite3.Row) -> _Held:
    """What the ledger holds of the message `row`: what was read is its stored answer, else what
    its log rows say each booking held, else nothing."""
    rows = conn.execute(
        "SELECT * FROM items WHERE message_id=? ORDER BY n, id", (row["id"],)
    ).fetchall()
    bookings, payloads = [], []
    for item in rows:
        booking = None
        if item["trip_item_id"] is not None:
            booking = conn.execute(
                "SELECT checks_json FROM trip_items WHERE id=?", (item["trip_item_id"],)
            ).fetchone()
        checks = _json(booking["checks_json"], []) if booking is not None else []
        bookings.append(
            {
                "kind": item["kind"],
                "result": item["outcome"],
                "detail": item["detail"],
                "checks": [c for c in checks if isinstance(c, str)],
                "key": _shown_key(item["item_key"]),
            }
        )
        payload = _json(item["payload_json"], None)
        if isinstance(payload, dict):
            kept = {k: v for k, v in payload.items() if k not in OF_THE_MESSAGE}
            payloads.append({"kind": item["kind"], **kept})
    read = _json(row["answer_json"], None)
    if not isinstance(read, dict):
        read = None
        if payloads:
            read = {"document_type": row["document_type"], "reservations": payloads}
    waiting = row["error"] if row["status"] == "waiting" else None
    return _Held(row["status"], row["parser"], waiting, bookings, read)


def _saved(settings: Settings, row: sqlite3.Row) -> bytes:
    """The message's saved email, read from the archive and nowhere else: the file the row names
    when it lies in the archive folder, else the file of that name in the folder. It must hold
    the bytes the row was made of."""
    n = row["id"]
    missing = Refused(f"report: message {n}'s saved email is not in the archive")
    named = Path(row["archive_path"] or "")
    if not named.name:
        raise missing
    root = settings.archive_dir
    for path in (named, root.joinpath(*named.parts[-3:])):
        try:
            inside = path.resolve().is_relative_to(root.resolve())
            if not inside or not path.is_file():
                continue
            if path.stat().st_size > MAX_BYTES:
                raise Refused("report: the email is over 20 MB")
            raw = path.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(raw).hexdigest() == row["sha256"]:
            return raw
    raise missing


# ----------------------------------------------------------------------------- a file, read here
@contextlib.contextmanager
def _as_this_process(name: str, value: str) -> Iterator[None]:
    saved = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """Nothing is logged while a file is read here: a report's two streams hold the report and
    its own lines alone."""
    before = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(before)


def _read_here(settings: Settings, raw: bytes) -> _Held:
    """The bytes filed into an empty ledger in a temporary directory that is removed, under
    settings made of the zone, the home airports and the travellers file alone, with the
    network switched off, no model, no notifier and no flight log; then what that ledger holds
    of them, with the extractor's own mapping for what was read."""
    from tripbox.ledger import Ledger
    from tripbox.pipeline.run import build_context, process_raw, process_text

    with contextlib.ExitStack() as stack:
        base = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="tripbox-report-")))
        stack.enter_context(_quiet())
        stack.enter_context(_as_this_process(OFFLINE_ENV, "1"))
        stack.enter_context(_as_this_process("TRIPBOX_CONFIG_DIR", str(settings.config_dir)))
        # the zone is the process's while the file is read, and what it was once it is
        stack.callback(time.tzset)
        stack.enter_context(_as_this_process("TZ", settings.tz))
        time.tzset()
        own = Settings.from_env(
            {
                "TZ": settings.tz,
                "TRIPBOX_HOME_AIRPORTS": ",".join(settings.home_airports),
                "TRIPBOX_CONFIG_DIR": str(settings.config_dir),
                "TRIPBOX_DATA_DIR": str(base / "data"),
                "TRIPBOX_ARCHIVE_DIR": str(base / "archive"),
                "TRIPBOX_LOGS_DIR": str(base / "logs"),
            }
        )
        ledger = Ledger.memory()
        stack.callback(ledger.close)
        try:
            ctx = build_context(own, ledger=ledger, with_model=False, with_notifier=False)
            if uploads.looks_like_email(raw):
                res = process_raw(ctx, raw, source="report", trusted=True, notify=False)
            else:
                text = raw.decode("utf-8", errors="replace")
                res = process_text(ctx, text, source="report", notify=False)
        except Exception:  # noqa: BLE001 - a file that cannot be read here is a report of that
            return _Held("failed", None, None, [], None)
        row = ledger.conn.execute(
            "SELECT * FROM messages WHERE id=?", (res.message_row_id,)
        ).fetchone()
        if row is None:
            return _Held(res.status, None, None, [], None)
        held = _held(ledger.conn, row)
        parsed = res.parsed
        mapped = parsed is not None and (parsed.parser != "none" or parsed.extraction.reservations)
        held.read = parsed.extraction.model_dump(mode="json") if mapped else None
        return held


# ----------------------------------------------------------------------------- the report
def _bytes_of(message: str, stdin) -> bytes:
    """The bytes of a file, or of standard input for `-`: at most one byte past the limit."""
    if message == "-":
        return (stdin if stdin is not None else sys.stdin.buffer).read(MAX_BYTES + 1)
    cannot = Refused(f"report: cannot read {message}")
    try:
        fd = os.open(message, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise cannot from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise cannot
        with os.fdopen(os.dup(fd), "rb") as f:
            return f.read(MAX_BYTES + 1)
    except OSError:
        raise cannot from None
    finally:
        os.close(fd)


def _names(typed: Sequence[str]) -> list[str]:
    out = []
    for name in typed:
        shown = " ".join(name.split())
        if not 2 <= len(shown) <= 80:
            raise Refused("report: --name takes a name of 2 to 80 characters")
        out.append(shown)
    return out


def make(settings: Settings, message: str, names: Sequence[str] = (), *, stdin=None) -> Made:
    """The report of `message`: a message's number in the ledger, a file, or `-` for `stdin`
    (bytes). `names` are travellers' names to replace as well. Refused: what cannot be reported,
    in one line, a ledger that cannot be read (locked, damaged, of another version) included."""
    typed = _names(names)
    try:
        return _make(settings, message, typed, stdin)
    except sqlite3.Error as exc:
        raise _unreadable(exc) from None


def _unreadable(exc: sqlite3.Error) -> Refused:
    """The one line for a ledger SQLite cannot read: its own words, which name no value."""
    said = " ".join(str(exc).split())[:120] or type(exc).__name__
    return Refused(
        f"report: cannot read the ledger ({said}); stop tripbox's services and try again"
    )


def _make(settings: Settings, message: str, typed: list[str], stdin) -> Made:
    conn = _reader(settings.ledger_path)
    try:
        if _NUMBER.fullmatch(message):
            n = int(message)
            row = None
            if conn is not None:
                row = conn.execute("SELECT * FROM messages WHERE id=?", (n,)).fetchone()
            if row is None:
                raise Refused(f"report: no message {n} in the ledger")
            raw = _saved(settings, row)
            return _write(settings, raw, typed, FROM_LEDGER.format(n=n), _held(conn, row))
        raw = _bytes_of(message, stdin)
        if len(raw) > MAX_BYTES:
            raise Refused("report: the email is over 20 MB")
        if raw.startswith(uploads.PDF_MAGIC):
            raise Refused("report: a PDF is never put in a report; describe what it shows instead")
        if not raw.strip() or uploads.sniff(raw) is None:
            raise Refused("report: this is neither text nor an email")
        # an email is what tripbox files of it: the bytes from its first header on
        raw = uploads.email_of(raw) or raw
        row = None
        if conn is not None:
            digest = hashlib.sha256(raw).hexdigest()
            row = conn.execute("SELECT * FROM messages WHERE sha256=?", (digest,)).fetchone()
        if row is not None:
            source = FROM_LEDGER.format(n=row["id"])
            return _write(settings, raw, typed, source, _held(conn, row))
    finally:
        if conn is not None:
            conn.close()
    return _write(settings, raw, typed, FROM_FILE, _read_here(settings, raw))


def _dumped(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def _write(settings: Settings, raw: bytes, typed: list[str], source: str, held: _Held) -> Made:
    """The report's text and its lines for standard error. The three sections are redacted
    twice: a first time to find what each rule replaces in any of them, and then, with all of
    that known, for the report, so that one value has one placeholder in the bookings' keys, in
    what was read and in the email, whichever of them a label or a key named it in."""
    mail = uploads.email_of(raw)
    # pasted text has no header to keep or drop, and none to take a name from
    text = None if mail is not None else redact.pasted(raw)
    known = redact.known_from(
        settings.travellers,
        held.read,
        typed,
        redact.harvest(mail) if mail is not None else redact.harvest_text(text),
        lists.trusted_sender_domains(),
    )
    did: dict[str, Any] = {"status": held.status}
    if held.waiting:
        did["waiting"] = held.waiting
    did["bookings"] = held.bookings

    def sections_of(r: redact.Redactor) -> list[str]:
        """The sections in the report's own order, which is the order its placeholders are
        numbered in; the email's own labels are read before anything is redacted."""
        if mail is not None:
            r.learn(mail)
        else:
            r.learn_text(text)
        done = _dumped(r.value(did))
        read = NOTHING if held.read is None else _dumped(r.value(held.read))
        shown = r.email(mail) if mail is not None else r.text(text)
        return [DID, done, "", READ, read, "", EMAIL, shown]

    first = redact.Redactor(known)
    sections_of(first)
    r = redact.Redactor(dataclasses.replace(known, found=first.found))
    sections = sections_of(r)
    counts = [(r.counts[kind], words) for kind, words in COUNTED]
    removed = (r.removed_headers, r.removed_attachments)
    head = [
        *HEAD,
        "",
        f"version: {__version__}",
        f"wire schema: {WIRE_SCHEMA_VERSION}",
        f"source: {source}",
        f"status: {_word(held.status)}",
        f"read by: {read_by(held.parser)}",
        "replaced: " + ", ".join(f"{words} {n}" for n, words in counts),
        f"removed: headers {removed[0]}, attachments {removed[1]}",
        SHOWN,
        "",
    ]
    # what was no text in the email is no text here either: the report can always be written
    text = "\n".join([*head, *sections]).encode("utf-8", "replace").decode("utf-8")
    said = [
        "report: replaced "
        + ", ".join(f"{n} {words}" for n, words in counts[:-1])
        + f" and {counts[-1][0]} {counts[-1][1]}; removed {removed[0]} headers and"
        f" {removed[1]} attachments"
    ]
    book = settings.travellers
    given = bool(typed or getattr(book, "me_aliases", ()) or getattr(book, "guests", ()))
    if not known.names:
        said.append(NO_NAME)
    elif not given:
        # a harvest can take a word that is no traveller's name and miss one that is
        said.append(GUESSED)
    said.append(READ_IT)
    return Made(text if text.endswith("\n") else text + "\n", said)


def write_new(path: str, text: str) -> None:
    """The report in a new file its owner alone may read; never over a file that is there."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        raise Refused(f"report: {path} exists; choose another name") from None
    except OSError:
        raise Refused(f"report: cannot write {path}") from None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise Refused(f"report: cannot write {path}") from None


# ----------------------------------------------------------------------------- the listing
def _received(value: str | None) -> str:
    try:
        when = parse_iso(value or "")
    except ValueError, TypeError, AttributeError:
        return "unknown"
    return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def newest(settings: Settings) -> list[str]:
    """The ten newest messages of the ledger, one line each, newest first; one line when it
    holds none."""
    try:
        conn = _reader(settings.ledger_path)
    except sqlite3.Error as exc:
        raise _unreadable(exc) from None
    if conn is None:
        return [NO_MESSAGE]
    try:
        rows = conn.execute(
            "SELECT m.id, m.received_at, m.status, m.parser, m.subject,"
            " (SELECT COUNT(*) FROM items i WHERE i.message_id = m.id) AS bookings"
            " FROM messages m ORDER BY m.id DESC LIMIT ?",
            (NEWEST,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _unreadable(exc) from None
    finally:
        conn.close()
    if not rows:
        return [NO_MESSAGE]
    lines = [LISTED]
    for row in rows:
        subject = "".join(ch if ch.isprintable() else " " for ch in str(row["subject"] or ""))
        lines.append(
            f"  #{row['id']}  {_received(row['received_at'])}  {_word(row['status'])}"
            f"  read by {read_by(row['parser'])}  {row['bookings']} booking(s)"
            f"  {subject[:SUBJECT_SHOWN]}"
        )
    return lines
