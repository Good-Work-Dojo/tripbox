# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox backup` and `tripbox restore`: one archive of what an install keeps, and the ledger
(with the saved emails, when it holds them) put back from one.

An archive is a gzip'd tar of mode 600, laid out as the install directory is: `MANIFEST.json`
first, then `data/ledger.db`, `config/<each regular file directly in the config folder>` and,
with mail, `archive/<every regular file under the archive folder>`; every member a regular file
of mode 600 owned by 0. Its ledger is one consistent state of the ledger, taken through SQLite's
online backup from a read-only connection (`db.write_copy`) while tripbox may be writing it, then
made one plain file. The ledger is never opened through `Ledger.open`, so any ledger SQLite can
read is backed up and none is upgraded.

`restore` reads only an archive `backup` made: the whole of it is read, and its ledger checked,
before anything is changed. It never writes the config folder (it compares it and says what
differs), never runs while a worker or anything else holds the ledger, and copies the ledger it
replaces beside it first (`write_copy` again), so a restore can be undone."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO
from urllib.parse import quote

from tripbox import __version__, status, util
from tripbox.config import Settings
from tripbox.files import mounted
from tripbox.ledger import db

FORMAT = 1
MANIFEST = "MANIFEST.json"
LEDGER = "data/ledger.db"
CONFIG = "config/"
ARCHIVE = "archive/"
MANIFEST_MAX = 1024 * 1024  # bytes of a manifest read; one larger is no backup's
HEADER_MAX = 64 * 1024  # bytes of a member's extended header (its long name)
NAME_MAX = 4096  # bytes of a member's name
NAMES_MAX = 32 * 1024 * 1024  # bytes of names and their folders held while an archive is read
# emails a backup's manifest may name: restore holds each one staged by its name and path until
# the ledger is back, a few hundred bytes apiece, so this many stays a small part of the 1 GB the
# worker's container has; backup refuses to write a backup with more
EMAILS_MAX = 200_000
HEADROOM = 64 * 1024 * 1024  # bytes a restore leaves free on the disk it writes
CHUNK = 1024 * 1024
STAMP = "%Y%m%dT%H%M%SZ"
VERSION_RE = re.compile(r"[0-9A-Za-z.+-]{1,40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SHAPE_RE = re.compile(r"[0-9a-f]{1,64}")
MADE_AT_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[+-]\d\d:\d\d|Z)")
# What reading an archive's bytes raises when they are no archive, or one cut short; the tar
# reader raises ValueError of its own for a number in an extended header that is none
READ_ERRORS = (tarfile.TarError, EOFError, zlib.error, OSError, ValueError)

NO_LEDGER = "backup: no ledger at {path}; there is nothing to back up yet"
NO_FOLDER = "backup: {folder} is not there; mount a folder at it or give --out FILE"
THERE = "backup: {file} is already there"
CANNOT_WRITE = "backup: cannot write {file}"
CANNOT_READ = "backup: cannot read the ledger at {path}"
MAIL_INSIDE = "backup: a backup with mail holds every saved email; write it outside {folder}"
WITHOUT_MAIL = "saved emails are not in it (--with-mail adds them)"
TOO_MANY_EMAILS = (
    "backup: this install holds {n:,} saved emails, more than one backup can carry ({max:,});"
    " back up without --with-mail, and copy the archive folder by hand"
)

NOT_A_BACKUP = "restore: {file} is not a tripbox backup"
NEWER_FORMAT = (
    "restore: this backup was made by a newer tripbox (format {n}); upgrade tripbox first"
)
NEWER_LEDGER = (
    "restore: this backup's ledger is from a newer tripbox (ledger version {v}); upgrade tripbox"
    " first"
)
DAMAGED = "restore: the backup's ledger is damaged; nothing was changed"
NOT_READ = "restore: the backup's ledger is not one this version reads; nothing was changed"
# an archive whose manifest was read, then a member is missing or its bytes cannot be read (cut
# short, a header or deflate data that fails, gzip's CRC-32 or length): damage, not a crafting
CUT_OR_CHANGED = "restore: {file} is damaged (cut short, or its bytes changed); nothing was changed"
WORKER = (
    "restore: a worker is running on this ledger; stop tripbox first (docker compose down), and"
    " if you just did, wait a minute"
)
UNREADABLE_HERE = "restore: the ledger here cannot be read; move it away and run again"
RESTORE_CANNOT_WRITE = "restore: cannot write {name}; nothing was changed"
# a file left beside a ledger no longer here was moved (and said) before another could not be
SET_ASIDE_CANNOT_WRITE = "restore: cannot write {name}; nothing else was changed"
RESTORED_BUT = (
    "restore: the ledger is restored, but {name} could not be written; run restore again to add"
    " the missing emails"
)
LEFT_ASIDE = "restore: {name}, left by a ledger no longer here, is kept as {kept}"
STAGE_PREFIX = ".tripbox-restore-"
LEFT_BESIDE = ("-wal", "-shm", "-journal")  # what SQLite keeps beside a ledger
# What SQLite says of a file that holds no ledger's tables
NOT_A_LEDGER = "no such table:"
EMAIL_SUFFIX = ".eml"  # a saved email is a regular file whose name ends so
# What a backup or a restore that was killed leaves is removed by the next one once nothing in
# it changed for this long: one younger may be another run's under way. Said in this line.
LEFTOVER_SECONDS = 3600
SWEPT = "{command}: removed {n} of an earlier run"
_BESIDE = r"(?:-wal|-shm|-journal)?"
# a backup's ledger copy, and its archive under the name `backup` gives it, both being written
BACKUP_LEFT = (
    re.compile(rf"\.tripbox-backup-[0-9a-f]{{16}}\.db\.part{_BESIDE}"),
    re.compile(r"\.tripbox-backup-\d{8}T\d{6}Z\.tar\.gz\.[0-9a-f]{16}\.part"),
)
# a restore's stage folders (in the data and the archive folders)
STAGE_LEFT = re.compile(re.escape(STAGE_PREFIX) + r".+")


class Refused(Exception):
    """A refusal: its text is the one line printed (stderr, exit 2). Nothing is left behind."""


def _n(n: int, word: str) -> str:
    """`n word`, the word in the plural unless n is 1."""
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """The trips the page lists (not deleted, holding a booking that is not removed) and the
    bookings in them that are not removed, as the scorecard counts them."""
    trips = conn.execute(
        "SELECT COUNT(*) FROM trips t WHERE t.deleted_at IS NULL AND EXISTS"
        " (SELECT 1 FROM trip_items i WHERE i.trip_id=t.id AND i.removed_at IS NULL)"
    ).fetchone()[0]
    bookings = conn.execute(
        "SELECT COUNT(*) FROM trip_items i JOIN trips t ON t.id=i.trip_id"
        " WHERE i.removed_at IS NULL AND t.deleted_at IS NULL"
    ).fetchone()[0]
    return int(trips), int(bookings)


def _sha256(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass  # a folder some file systems will not flush
    finally:
        os.close(fd)


def _place(source: Path, target: Path) -> bool:
    """`source` renamed to `target` unless something is there already (False then). A hard
    link makes the name without ever replacing one; where links cannot be made the name is
    looked at first."""
    try:
        os.link(source, target)
    except FileExistsError:
        return False
    except OSError:
        if os.path.lexists(target):
            return False
        os.replace(source, target)
        return True
    os.unlink(source)
    return True


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` (that need not exist) is in `folder`, each by its real path."""
    real = path.parent.resolve() / path.name
    return real.is_relative_to(folder.resolve())


def _plain(name: str) -> bool:
    """A member's name as `backup` writes one: relative, of names that are none of "", ".",
    ".." and hold no NUL."""
    return "\0" not in name and all(p not in ("", ".", "..") for p in name.split("/"))


# ----------------------------------------------------------------------------- backup
@dataclass(frozen=True)
class _Snapshot:
    path: Path
    version: int
    shape: str
    sha256: str
    size: int
    trips: int
    bookings: int


def _snapshot(ledger: Path, folder: Path) -> _Snapshot:
    """The ledger's consistent copy in `folder` (`db.write_copy`), made one plain file (no
    -wal), with what the manifest says of it. The copy is removed when this fails."""
    part = folder / f".tripbox-backup-{secrets.token_hex(8)}.db.part"
    db.write_copy(ledger, part)
    try:
        conn = sqlite3.connect(str(part), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            version, shape = conn.execute(
                "SELECT (SELECT v FROM schema_version LIMIT 1),"
                " (SELECT v FROM kv WHERE k='schema_shape')"
            ).fetchone()
            trips, bookings = _counts(conn)
        finally:
            conn.close()
        for end in ("-wal", "-shm", "-journal"):
            part.with_name(part.name + end).unlink(missing_ok=True)
        if version is None:
            raise sqlite3.DatabaseError(f"{NOT_A_LEDGER} schema_version")
        sha, size = _sha256(part)
        return _Snapshot(part, int(version), str(shape or ""), sha, size, trips, bookings)
    except BaseException:
        part.unlink(missing_ok=True)
        for end in ("-wal", "-shm", "-journal"):
            part.with_name(part.name + end).unlink(missing_ok=True)
        raise


def _regular(folder: Path, recurse: bool, emails: bool = False) -> list[str]:
    """The regular files in `folder` (none when it is absent) by their path in it, sorted:
    directly in it, or with `recurse` under it; a link is never followed. With `emails`, the
    saved emails alone: a name ending EMAIL_SUFFIX (never what a write cut short leaves, a
    `.tmp` or a `.part`), and nothing under a restore's stage folder."""
    if not folder.is_dir():
        return []
    found = []
    for top, dirs, names in os.walk(folder, followlinks=False):
        if emails:
            dirs[:] = [d for d in dirs if not d.startswith(STAGE_PREFIX)]
        dirs.sort()
        for name in names:
            if emails and not name.endswith(EMAIL_SUFFIX):
                continue
            path = Path(top) / name
            if stat.S_ISREG(path.lstat().st_mode):
                found.append(path.relative_to(folder).as_posix())
        if not recurse:
            break
    return sorted(found)


def _newest(path: Path) -> float:
    """When `path`, or anything under it, last changed (its mtime); a link is never followed."""
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for top, dirs, names in os.walk(path, followlinks=False):
            for name in (*dirs, *names):
                newest = max(newest, os.lstat(os.path.join(top, name)).st_mtime)
    return newest


def _sweep(
    folder: Path, patterns: tuple[re.Pattern[str], ...], keep: tuple[Path, ...] = ()
) -> int:
    """Remove what a killed run left directly in `folder`: each file or folder whose name one
    of `patterns` matches whole, once nothing in it changed for LEFTOVER_SECONDS (by the
    clock, `util.now`); how many. One in `keep` (this run's own) or that cannot be removed is
    left."""
    if not folder.is_dir():
        return 0
    now = util.now().timestamp()
    removed = 0
    try:
        with os.scandir(folder) as entries:
            left = [e for e in entries if any(p.fullmatch(e.name) for p in patterns)]
    except OSError:
        return 0
    for entry in left:
        path = Path(entry.path)
        if path in keep:
            continue
        try:
            if now - _newest(path) < LEFTOVER_SECONDS:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def _say_swept(command: str, n: int, say: Callable[[str], None]) -> None:
    if n:
        say(SWEPT.format(command=command, n=_n(n, "leftover")))


def _readable(s: Settings, ledger: Path) -> None:
    """Refused unless the ledger can be read (through a read-only connection: it writes nothing
    into the ledger, though for a ledger in WAL mode SQLite leaves its -wal and -shm files
    beside it): no ledger, for a file that is none; else that it cannot be read."""
    try:
        conn = sqlite3.connect(f"file:{quote(str(ledger.absolute()))}?mode=ro", uri=True)
        try:
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF
        if code == sqlite3.SQLITE_NOTADB:
            raise Refused(NO_LEDGER.format(path=mounted(s, ledger))) from exc
        raise Refused(CANNOT_READ.format(path=mounted(s, ledger))) from exc


def _info(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = size, mtime, 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _add_file(tf: tarfile.TarFile, name: str, path: Path, mtime: int) -> None:
    """The regular file at `path` as the member `name`; a link put there meanwhile is refused
    (OSError), never followed."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{name} is no longer a regular file")
        tf.addfile(_info(name, st.st_size, mtime), f)


def _write_archive(
    part: Path, s: Settings, snap: _Snapshot, manifest: dict, configs: list, mails: list
) -> None:
    """The archive written into the new file `part` (mode 600) and flushed to disk."""
    mtime = int(util.now().timestamp())
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        # no file name in the gzip header, and its time the backup's
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=mtime) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
                text = json.dumps(manifest, indent=1, sort_keys=True).encode() + b"\n"
                tf.addfile(_info(MANIFEST, len(text), mtime), io.BytesIO(text))
                _add_file(tf, LEDGER, snap.path, mtime)
                for name in configs:
                    _add_file(tf, CONFIG + name, s.config_dir / name, mtime)
                for rel in mails:
                    _add_file(tf, ARCHIVE + rel, s.archive_dir / rel, mtime)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(part, 0o600)


def backup(
    s: Settings, *, with_mail: bool, out: str | None, say: Callable[[str], None] = print
) -> list[str]:
    """Write a backup (see the module) and return the lines said of it; Refused, leaving
    nothing behind, when it cannot. What a killed backup left in the folder it writes in is
    removed first (`_sweep`), and `say` says so at once."""
    ledger = s.ledger_path
    if not ledger.is_file():
        raise Refused(NO_LEDGER.format(path=mounted(s, ledger)))
    when = util.now()
    if out is not None:
        target = Path(out).absolute()
    else:
        if not s.backups_dir.is_dir():
            raise Refused(NO_FOLDER.format(folder=mounted(s, s.backups_dir)))
        target = s.backups_dir / f"tripbox-backup-{when.strftime(STAMP)}.tar.gz"
    shown = mounted(s, target)
    if os.path.lexists(target):
        raise Refused(THERE.format(file=shown))
    if with_mail:
        # the page's container mounts these: a backup with every saved email never goes there
        for folder in (s.data_dir, s.logs_dir, s.config_dir):
            if _inside(target, folder):
                raise Refused(MAIL_INSIDE.format(folder=mounted(s, folder)))
    folder = target.parent
    if not folder.is_dir():
        raise Refused(CANNOT_WRITE.format(file=shown))
    own = re.compile(rf"\.{re.escape(target.name)}\.[0-9a-f]{{16}}\.part")
    _say_swept("backup", _sweep(folder, (*BACKUP_LEFT, own)), say)
    _readable(s, ledger)
    part = folder / f".{target.name}.{secrets.token_hex(8)}.part"
    snap: _Snapshot | None = None
    try:
        try:
            snap = _snapshot(ledger, folder)
        except sqlite3.DatabaseError as exc:
            code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF
            if code == sqlite3.SQLITE_NOTADB or str(exc).startswith(NOT_A_LEDGER):
                raise Refused(NO_LEDGER.format(path=mounted(s, ledger))) from exc
            raise
        # listed after the ledger's copy, so every email it names is saved by now
        configs = _regular(s.config_dir, recurse=False)
        mails = _regular(s.archive_dir, recurse=True, emails=True) if with_mail else []
        if len(mails) > EMAILS_MAX:  # restore would refuse it as no backup
            raise Refused(TOO_MANY_EMAILS.format(n=len(mails), max=EMAILS_MAX))
        manifest = {
            "format": FORMAT,
            "tripbox": __version__,
            "made_at": when.astimezone(dt.UTC).isoformat(timespec="seconds"),
            "ledger": {
                "version": snap.version,
                "shape": snap.shape,
                "sha256": snap.sha256,
                "bytes": snap.size,
                "trips": snap.trips,
                "bookings": snap.bookings,
            },
            "config": configs,
            "emails": len(mails),
            "with_mail": with_mail,
        }
        _write_archive(part, s, snap, manifest, configs, mails)
        if not _place(part, target):
            raise Refused(THERE.format(file=shown))
        _fsync(folder)
    except (OSError, sqlite3.Error) as exc:
        raise Refused(CANNOT_WRITE.format(file=shown)) from exc
    finally:
        part.unlink(missing_ok=True)
        if snap is not None:
            snap.path.unlink(missing_ok=True)
    held = (
        f"{_n(len(configs), 'config file')}, {_n(len(mails), 'email')}"
        if with_mail
        else f"{_n(len(configs), 'config file')}; {WITHOUT_MAIL}"
    )
    return [
        f"backup: ledger version {snap.version}, {_n(snap.trips, 'trip')},"
        f" {_n(snap.bookings, 'booking')}",
        f"backup: {held}",
        f"backup: wrote {shown} ({target.stat().st_size:,} bytes)",
    ]


# ----------------------------------------------------------------------------- restore
class _CannotWrite(Exception):
    """A file of this install that could not be written: `path`; `moved`, when files left
    beside a ledger no longer here were moved (and said) before it."""

    def __init__(self, path: Path, moved: bool = False) -> None:
        super().__init__(str(path))
        self.path = path
        self.moved = moved


@dataclass
class _Read:
    """What reading an archive found: its manifest, the ledger written out from it, how many
    of the backup's config files differ from the folder's, and its emails: those staged to be
    added, and how many are there with the same bytes or with others."""

    manifest: dict
    ledger: Path
    config_differ: int = 0
    new_mail: list[tuple[str, Path]] = field(default_factory=list)
    mail_same: int = 0
    mail_differ: int = 0


@dataclass(frozen=True)
class _Ledger:
    """What the backup's ledger, checked, holds: sign-ins and share links only of a ledger of
    this version."""

    version: int
    trips: int
    bookings: int
    sign_ins: int | None
    links: int | None


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _valid(doc: dict) -> bool:
    """Whether a manifest of this format says what `backup` writes, in its types."""
    ledger, config = doc.get("ledger"), doc.get("config")
    return bool(
        type(doc.get("format")) is int
        and doc["format"] == FORMAT
        and isinstance(doc.get("tripbox"), str)
        and VERSION_RE.fullmatch(doc["tripbox"])
        and isinstance(doc.get("made_at"), str)
        and MADE_AT_RE.fullmatch(doc["made_at"])
        and isinstance(ledger, dict)
        and type(ledger.get("version")) is int
        and ledger["version"] >= 1
        and isinstance(ledger.get("shape"), str)
        and SHAPE_RE.fullmatch(ledger["shape"])
        and isinstance(ledger.get("sha256"), str)
        and HEX64.fullmatch(ledger["sha256"])
        and all(_count(ledger.get(k)) for k in ("bytes", "trips", "bookings"))
        and isinstance(config, list)
        and all(isinstance(n, str) and "/" not in n and _plain(n) for n in config)
        and len(set(config)) == len(config)
        and _count(doc.get("emails"))
        and doc["emails"] <= EMAILS_MAX
        and type(doc.get("with_mail")) is bool
        and (doc["with_mail"] or doc["emails"] == 0)
    )


def _manifest(data: bytes, not_backup: str) -> dict:
    """The manifest read from its bytes; Refused when it is none, or one of a newer format or
    of a newer ledger than this tripbox reads."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused(not_backup) from None
    if not isinstance(doc, dict):
        raise Refused(not_backup)
    if type(doc.get("format")) is int and doc["format"] > FORMAT:
        raise Refused(NEWER_FORMAT.format(n=doc["format"]))
    if not _valid(doc):
        raise Refused(not_backup)
    if doc["ledger"]["version"] > db.SCHEMA_VERSION:
        raise Refused(NEWER_LEDGER.format(v=doc["ledger"]["version"]))
    return doc


def _stage(src: BinaryIO, path: Path) -> str:
    """The member being read written to the new file `path` (mode 600, flushed); the SHA-256
    of its bytes. A read that fails raises as reading does; a write, _CannotWrite."""
    h = hashlib.sha256()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise _CannotWrite(path) from exc
    with os.fdopen(fd, "wb") as out:
        while chunk := src.read(CHUNK):
            h.update(chunk)
            try:
                out.write(chunk)
            except OSError as exc:
                raise _CannotWrite(path) from exc
        try:
            out.flush()
            os.fsync(out.fileno())
        except OSError as exc:
            raise _CannotWrite(path) from exc
    return h.hexdigest()


def _same(src: BinaryIO, size: int, path: Path) -> bool:
    """Whether the member being read (`size` bytes) holds what the regular file at `path`
    holds; the member is read to its end either way, and a link is never followed."""
    local = None
    try:
        st = path.lstat()
        if stat.S_ISREG(st.st_mode) and st.st_size == size:
            local = open(path, "rb")  # closed below, once the member is read
    except OSError:
        local = None
    try:
        same = local is not None
        while chunk := src.read(CHUNK):
            if same:
                try:
                    same = local.read(len(chunk)) == chunk  # type: ignore[union-attr]
                except OSError:
                    same = False
        return same
    finally:
        if local is not None:
            local.close()


class _NeverWritten(tarfile.HeaderError):
    """A header that reads whole but describes what `backup` never writes: a crafted archive,
    no backup, wherever it comes. The tar reader rewraps one met in the header an extended
    header describes (as a ReadError), so the reader is marked too (`_tripbox_never`)."""


class _Bounded(tarfile.TarInfo):
    """A member's header as `backup` writes one, else HeaderError before the tar reader acts
    on it (and reads what it says into memory): a regular file, preceded at most by one
    extended header of its own of at most HEADER_MAX bytes, which describes no sparse file.
    Never a global header, a chain of extended headers, a GNU long name or sparse file, or any
    other type. The hooks are the reader's own steps for each header it reads
    (`tests/test_backup.py` holds them to that)."""

    def _proc_member(self, reader):
        try:
            return self._checked(reader)
        except _NeverWritten:
            reader._tripbox_never = True
            raise

    def _checked(self, reader):
        if self.type == tarfile.XHDTYPE:
            if self.size > HEADER_MAX or getattr(reader, "_tripbox_extended", False):
                raise _NeverWritten("an extended header backup never writes")
            reader._tripbox_extended = True  # the header it describes is read inside
            try:
                return super()._proc_member(reader)
            finally:
                reader._tripbox_extended = False
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
            raise _NeverWritten("a member backup never writes")
        return super()._proc_member(reader)

    def _proc_gnusparse_00(self, *args):
        raise _NeverWritten("a sparse file")

    _proc_gnusparse_01 = _proc_gnusparse_10 = _proc_gnusparse_00


class _Budget:
    """What may be written while an archive is read: on each file system the stages are on,
    the space free when the reading began less HEADROOM, shared by every stage there, less
    what was already written there."""

    def __init__(self, *folders: Path) -> None:
        self.left: dict[int, int] = {}
        for folder in folders:
            self.left.setdefault(os.stat(folder).st_dev, shutil.disk_usage(folder).free - HEADROOM)

    def take(self, size: int, folder: Path, path: Path) -> None:
        """Room for `size` bytes in the stage `folder`, for `path`, or _CannotWrite."""
        disk = os.stat(folder).st_dev
        if size > self.left[disk]:
            raise _CannotWrite(path)
        self.left[disk] -= size


def _parents(name: str) -> list[str]:
    parts = name.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _starts_with_a_manifest(file: Path) -> bool:
    """Whether the archive's first member is a manifest this version reads, inflating no byte
    past it (gzip's own reader inflates far ahead, and meets damage there before the manifest
    is read): its header, then its data; at most HEADER_MAX of input at a time."""
    inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = b""

    def upto(n: int, fh: BinaryIO) -> bool:
        nonlocal out
        while len(out) < n:
            data = inflate.unconsumed_tail or fh.read(HEADER_MAX)
            if not data or inflate.eof:
                return False
            out += inflate.decompress(data, n - len(out))
        return True

    try:
        with open(file, "rb") as fh:
            if not upto(tarfile.BLOCKSIZE, fh):
                return False
            head = tarfile.TarInfo.frombuf(out[: tarfile.BLOCKSIZE], "utf-8", "surrogateescape")
            if head.name != MANIFEST or not head.isreg() or head.size > MANIFEST_MAX:
                return False
            if not upto(tarfile.BLOCKSIZE + head.size, fh):
                return False
        _manifest(out[tarfile.BLOCKSIZE :], "")
    except (*READ_ERRORS, Refused):
        return False
    return True


def _sound(gz: gzip.GzipFile) -> bool:
    """Whether the rest of the gzip stream reads to its end and passes its CRC-32 and length;
    what is read is dropped."""
    try:
        while gz.read(CHUNK):
            pass
    except READ_ERRORS:
        return False
    return True


def _read(file: Path, s: Settings, stage: Path, mail_stage: Path | None) -> _Read:
    """Read the whole archive at `file`: its manifest first, then every member, each a
    regular file under a plain relative name the manifest accounts for (no link, device,
    folder, absolute name or `..`; none twice; at most the manifest's number), the ledger
    written into `stage`, each config file compared with the folder's, each email compared
    with the one there or, with `mail_stage`, written there when none is. Only the manifest
    is held in memory (at most MANIFEST_MAX). The gzip stream is read to its end, past the tar's
    own end, so its checksum is met before anything outside the stages is written. Refused when
    it is no backup; once its manifest is read, a member missing or one that cannot be read (a
    header or data that fails, gzip's checksum) is damage, while what reads whole but is no
    backup's (`_NeverWritten`, a name or a member it never writes) is still no backup unless
    gzip's checksum then fails (`_sound`)."""
    shown = mounted(s, file)
    not_backup = NOT_A_BACKUP.format(file=shown)
    cut = CUT_OR_CHANGED.format(file=shown)
    manifest_read = False
    try:
        fh = open(file, "rb")  # closed below
    except OSError:
        raise Refused(not_backup) from None
    try:
        gz = gzip.GzipFile(fileobj=fh, mode="rb")
        tf = tarfile.open(fileobj=gz, mode="r|", tarinfo=_Bounded)

        def next_member() -> tarfile.TarInfo | None:
            m = tf.next()
            tf.members.clear()  # read as a stream: no member is kept once it is read
            return m

        def regular(m: tarfile.TarInfo) -> bool:
            return m.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and _plain(m.name)

        first = next_member()
        if first is None or first.name != MANIFEST or not regular(first):
            raise Refused(not_backup)
        if first.size > MANIFEST_MAX:
            raise Refused(not_backup)
        manifest = _manifest(tf.extractfile(first).read(MANIFEST_MAX), not_backup)  # type: ignore[union-attr]
        manifest_read = True
        found = _Read(manifest, stage / "ledger.db")
        room = _Budget(stage, *([mail_stage] if mail_stage is not None else []))
        kept = 0  # bytes of names held in `files` and `dirs`
        configs = set(manifest["config"])
        left = 1 + len(configs) + manifest["emails"]
        files: set[str] = set()
        dirs: set[str] = set()
        sha = None
        emails = 0
        while (m := next_member()) is not None:
            name = m.name
            left -= 1
            if left < 0 or m.sparse is not None or len(name.encode(errors="replace")) > NAME_MAX:
                raise Refused(not_backup)
            parents = _parents(name)
            if (
                not regular(m)
                or name in files
                or name in dirs
                or any(p in files for p in parents)
            ):
                raise Refused(not_backup)
            kept += len(name) + sum(len(p) for p in parents if p not in dirs)
            if kept > NAMES_MAX:
                raise Refused(not_backup)
            files.add(name)
            dirs.update(parents)
            src = tf.extractfile(m)
            assert src is not None
            if name == LEDGER:
                if m.size != manifest["ledger"]["bytes"]:
                    raise Refused(DAMAGED)
                room.take(m.size, stage, s.ledger_path)
                sha = _stage(src, found.ledger)
            elif name.startswith(CONFIG) and name[len(CONFIG) :] in configs:
                if not _same(src, m.size, s.config_dir / name[len(CONFIG) :]):
                    found.config_differ += 1
            elif name.startswith(ARCHIVE) and manifest["with_mail"]:
                rel = name[len(ARCHIVE) :]
                emails += 1
                target = s.archive_dir / rel
                if os.path.lexists(target):
                    if _same(src, m.size, target):
                        found.mail_same += 1
                    else:
                        found.mail_differ += 1
                elif mail_stage is not None:
                    room.take(m.size, mail_stage, target)
                    _stage(src, mail_stage / rel)
                    found.new_mail.append((rel, mail_stage / rel))
            else:
                raise Refused(not_backup)
        wanted = {LEDGER, *(CONFIG + n for n in configs)}
        if not wanted <= files or emails != manifest["emails"]:
            raise Refused(cut)  # the tar reader ends at a header that fails as at the last
        # what follows the tar's end is its padding (under one record): read to gzip's end,
        # which checks the CRC-32 and the length its trailer holds
        trailing = 0
        while chunk := gz.read(CHUNK):
            trailing += len(chunk)
            if trailing > CHUNK:
                raise Refused(not_backup)
    except Refused as refused:
        # what reads whole after a valid manifest but is no backup's may be bytes changed where
        # no header's checksum looks (an extended header's records): gzip's checksum tells
        if manifest_read and str(refused) == not_backup and not _sound(gz):
            raise Refused(cut) from refused
        raise
    except (*READ_ERRORS, RecursionError, MemoryError) as exc:
        if manifest_read and isinstance(exc, READ_ERRORS):  # `tf` is there by then
            crafted = isinstance(exc, _NeverWritten) or getattr(tf, "_tripbox_never", False)
            if not crafted or not _sound(gz):
                raise Refused(cut) from exc
        elif isinstance(exc, READ_ERRORS) and _starts_with_a_manifest(file):
            # gzip reads ahead of the tar reader: damage past the manifest can be met first
            raise Refused(cut) from exc
        raise Refused(not_backup) from exc
    finally:
        fh.close()
    if sha != manifest["ledger"]["sha256"]:
        raise Refused(DAMAGED)
    return found


def _check(staged: Path, manifest: dict) -> _Ledger:
    """The backup's ledger as written out, checked: SQLite's integrity check passes (else
    Refused, damaged), this version reads it (`db._stated_version`; a ledger of this version
    has a new one's schema, and holds tables and indexes alone; else Refused, not one this
    version reads), and it states the manifest's version and shape (else Refused, damaged: the
    archive disagrees with itself). It is read through an immutable read-only connection,
    which writes nothing."""
    uri = f"file:{quote(str(staged.absolute()))}?mode=ro&immutable=1"
    said = manifest["ledger"]
    checked = False  # SQLite's own check passed: what fails from here is the ledger's shape
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA cell_size_check=ON")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise Refused(DAMAGED)
            checked = True
            # a ledger holds tables and indexes alone; one of this version, a new one's
            made = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type NOT IN ('table', 'index')"
            ).fetchone()[0]
            if made:
                raise Refused(NOT_READ)
            version = db._stated_version(conn)
            if version is None:
                raise Refused(NOT_READ)
            if version == db.SCHEMA_VERSION and db._schema_of(conn) != db._new_schema():
                raise Refused(NOT_READ)
            shape = conn.execute("SELECT v FROM kv WHERE k='schema_shape'").fetchone()
            trips, bookings = _counts(conn)
            sign_ins = links = None
            if version == db.SCHEMA_VERSION:
                sign_ins = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                links = conn.execute(
                    "SELECT COUNT(*) FROM share_links WHERE revoked_at IS NULL"
                ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise Refused(NOT_READ if checked else DAMAGED) from exc
    except db.OutdatedLedger as exc:  # another shape, or an older one with no way up
        raise Refused(NOT_READ) from exc
    if version != said["version"] or shape is None or shape[0] != said["shape"]:
        raise Refused(DAMAGED)
    return _Ledger(version, trips, bookings, sign_ins, links)


def _record(text: str | None) -> dict | None:
    """A worker's record from its `kv` row's text, as `status.read` reads it."""
    return status.read(SimpleNamespace(kv=SimpleNamespace(get=lambda key: text)))  # type: ignore[arg-type]


def _here(path: Path) -> tuple[int, int] | None:
    """The trips and bookings of the ledger here, read without writing (and, with no -wal
    beside it, without making anything beside it), or None when there is none. Refused while
    a worker's record is live, or when the ledger cannot be read."""
    if not os.path.lexists(path):
        return None
    busy = any(path.with_name(path.name + end).exists() for end in ("-wal", "-journal"))
    mode = "ro" if busy else "ro&immutable=1"
    try:
        conn = sqlite3.connect(f"file:{quote(str(path.absolute()))}?mode={mode}", uri=True)
        try:
            row = conn.execute("SELECT v FROM kv WHERE k=?", (status.KV_WORKER,)).fetchone()
            counts = _counts(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise Refused(UNREADABLE_HERE) from exc
    if status.live(_record(row[0] if row else None), util.now()):
        raise Refused(WORKER)
    return counts


def _hold(path: Path) -> sqlite3.Connection:
    """A connection holding the ledger here alone (an exclusive lock, kept until it closes):
    Refused while anything else has it open, or a worker's record is live."""
    conn = sqlite3.connect(str(path), timeout=1.0, isolation_level=None)
    try:
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise Refused(WORKER) from exc
            raise
        row = conn.execute("SELECT v FROM kv WHERE k=?", (status.KV_WORKER,)).fetchone()
        conn.execute("COMMIT")
        if status.live(_record(row[0] if row else None), util.now()):
            raise Refused(WORKER)
    except Refused:
        conn.close()
        raise
    except sqlite3.Error as exc:
        conn.close()
        raise Refused(UNREADABLE_HERE) from exc
    return conn


def _kept_name(path: Path) -> Path:
    """`ledger-before-restore-<UTC time>[-n].db` beside the ledger at `path`: a name that neither
    it nor any file SQLite keeps beside it (`LEFT_BESIDE`) is taken by yet."""
    stamp = util.now().strftime(STAMP)
    n = 1
    while True:
        end = "" if n == 1 else f"-{n}"
        final = path.with_name(f"{path.stem}-before-restore-{stamp}{end}.db")
        taken = (final, *(final.with_name(final.name + e) for e in LEFT_BESIDE))
        if not any(os.path.lexists(p) for p in taken):
            return final
        n += 1


def _keep(path: Path, conn: sqlite3.Connection) -> Path:
    """Copy the ledger here, through the connection holding it, beside itself under
    `_kept_name` (`db.write_copy`; mode 600), never over a file."""
    final = _kept_name(path)
    part = path.with_name(f".{final.name}.{secrets.token_hex(8)}.part")
    try:
        db.write_copy(path, part, source=conn)
        if not _place(part, final):
            raise OSError(f"{final} is there")
    except (OSError, sqlite3.Error) as exc:
        raise _CannotWrite(final) from exc
    finally:
        part.unlink(missing_ok=True)
    return final


def _stop_the_worker(staged: Path) -> None:
    """The backup's ledger, written out and checked, with its worker's record marked stopped
    now when it is not: a backup taken while the worker ran holds its live record, which would
    keep a worker from starting on the restored ledger, and a restore from running, until it
    went stale. The archive's own bytes are left as they are (and were checked before)."""
    try:
        conn = sqlite3.connect(str(staged), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            row = conn.execute("SELECT v FROM kv WHERE k=?", (status.KV_WORKER,)).fetchone()
            record = _record(row[0] if row else None)
            if record is not None and not record.get("stopped_at"):
                record["stopped_at"] = util.now().astimezone(dt.UTC).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE kv SET v=? WHERE k=?", (json.dumps(record), status.KV_WORKER)
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _CannotWrite(staged) from exc


def _set_aside(path: Path, kept_as: Callable[[Path, Path], None]) -> None:
    """What SQLite left beside a ledger at `path` that is no longer there (the owner moved it
    away): its -wal holds what it wrote last, and would be read into the ledger put back, so
    each is renamed beside it under the name `_kept_name` gives, its own ending kept, never
    deleted; `kept_as` is told each one's path and where it is kept as soon as it is moved, so
    when the next cannot be (_CannotWrite, `moved` once one was), what was moved is said."""
    left = [path.with_name(path.name + end) for end in LEFT_BESIDE]
    left = [p for p in left if os.path.lexists(p)]
    if not left:
        return
    base = _kept_name(path)
    moved = False
    for p in left:
        target = base.with_name(base.name + p.name[len(path.name) :])
        try:
            if not _place(p, target):
                raise OSError(f"{target} is there")
        except OSError as exc:
            raise _CannotWrite(target, moved=moved) from exc
        moved = True
        kept_as(p, target)


def _named(s: Settings, path: object) -> str:
    """A file of this install as its owner knows it, for a line printed: relative to the data
    or archive folder (`data/ledger.db`, `archive/<name>`; one being staged by the name it is
    staged for), else its own name; every character that is not printable shown as `?`."""
    p = Path(os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else str(path))
    name = p.name or str(p)
    for top, folder in sorted(
        (("data", s.data_dir), ("archive", s.archive_dir)), key=lambda f: -len(f[1].parts)
    ):
        try:
            parts = p.relative_to(folder).parts
        except ValueError:
            continue
        if parts and parts[0].startswith(STAGE_PREFIX):
            parts = parts[1:]
        name = "/".join((top, *parts))
        break
    return "".join(c if c.isprintable() else "?" for c in name)


def _differ(n: int) -> str:
    return f"{_n(n, 'file')} {'differs' if n == 1 else 'differ'}"


def restore(s: Settings, file: str, *, apply: bool, say: Callable[[str], None] = print) -> None:
    """Put a backup's ledger (and its emails) back; without `apply`, say what that would do
    and change nothing. Each line is said as it becomes true; Refused when it cannot, and
    before anything is changed when the archive, its ledger or the ledger here stops it. With
    `apply`, what a killed restore left (its stage folders, a copy of the ledger being written)
    is removed first (`_sweep`) and said."""
    path = Path(file)
    ledger_here = s.ledger_path
    stages: list[Path] = []
    held: sqlite3.Connection | None = None
    swapped = False  # once the ledger put back has its name, only emails can be missing
    try:
        if apply:
            s.data_dir.mkdir(parents=True, exist_ok=True)
        stage = Path(
            tempfile.mkdtemp(
                prefix=STAGE_PREFIX, dir=s.data_dir if s.data_dir.is_dir() else None
            )
        )
        stages.append(stage)
        mail_stage = None
        if apply:
            s.archive_dir.mkdir(parents=True, exist_ok=True)
            mail_stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=s.archive_dir))
            stages.append(mail_stage)
        found = _read(path, s, stage, mail_stage)
        checked = _check(found.ledger, found.manifest)
        manifest = found.manifest
        if not apply:
            here = _here(ledger_here)
            say(
                f"restore: backup made {manifest['made_at']} by tripbox {manifest['tripbox']}:"
                f" ledger version {checked.version}, {_n(checked.trips, 'trip')},"
                f" {_n(checked.bookings, 'booking')}, {_n(len(manifest['config']), 'config file')},"
                f" {_n(manifest['emails'], 'email')}"
            )
            say(
                "restore: there is no ledger here yet"
                if here is None
                else f"restore: the ledger here holds {_n(here[0], 'trip')},"
                f" {_n(here[1], 'booking')}; it would be kept beside it"
            )
            say(
                f"restore: config is never written; {_differ(found.config_differ)} from the"
                " backup's"
                if found.config_differ
                else "restore: config is the same as the backup's"
            )
            say("restore: nothing was changed; run again with --apply")
            return
        # the archive is proven (read to its end, its manifest and its ledger checked): only
        # now is what a killed restore left removed
        kept_part = re.compile(
            rf"\.{re.escape(ledger_here.stem)}-before-restore-\S+\.db\.[0-9a-f]{{16}}"
            rf"\.part{_BESIDE}"
        )
        own = tuple(stages)
        swept = _sweep(s.data_dir, (STAGE_LEFT, kept_part), own)
        _say_swept("restore", swept + _sweep(s.archive_dir, (STAGE_LEFT,), own), say)
        _stop_the_worker(found.ledger)
        if os.path.lexists(ledger_here):
            held = _hold(ledger_here)
            kept = _keep(ledger_here, held)
            say(f"restore: the ledger that was here is kept as {mounted(s, kept)}")
            # out of WAL mode while it is still held: an opener that waited on the hold finds
            # a file no longer there to write, and none of its pages reach the restored ledger;
            # then no journal on disk at all, so the close after the rename deletes no file by
            # name (a newcomer's journal of the restored ledger among them)
            try:
                mode = held.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                memory = held.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]
            except sqlite3.Error as exc:
                raise _CannotWrite(ledger_here) from exc
            if mode != "delete" or memory != "memory":
                raise _CannotWrite(ledger_here)
            # what is left beside it is its own, held in the copy: it goes, as it would be read
            # into the ledger put back
            for end in LEFT_BESIDE:
                ledger_here.with_name(ledger_here.name + end).unlink(missing_ok=True)
        else:
            _set_aside(
                ledger_here,
                lambda src, aside: say(
                    LEFT_ASIDE.format(name=mounted(s, src), kept=mounted(s, aside))
                ),
            )
        os.replace(found.ledger, ledger_here)
        swapped = True
        if held is not None:  # released only once the restored ledger has its name
            held.close()
            held = None
        _fsync(s.data_dir)
        say(
            f"restore: ledger restored: version {checked.version}, {_n(checked.trips, 'trip')},"
            f" {_n(checked.bookings, 'booking')}"
        )
        added = 0
        for rel, staged in found.new_mail:
            target = s.archive_dir / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                placed = _place(staged, target)
            except (FileExistsError, NotADirectoryError):
                placed = False
            except OSError as exc:
                raise _CannotWrite(target) from exc
            if placed:
                added += 1
            else:
                with open(staged, "rb") as src:
                    if _same(src, staged.stat().st_size, target):
                        found.mail_same += 1
                    else:
                        found.mail_differ += 1
        differ = found.mail_differ
        say(
            f"restore: {_n(added, 'email')} added, {found.mail_same:,} already there,"
            f" {differ:,} {'differs and was left' if differ == 1 else 'differ and were left'}"
        )
        if found.config_differ:
            say(
                f"restore: config is not written: {_differ(found.config_differ)}; put it back"
                f" with: tar -xzf {mounted(s, path)} config"
            )
        if checked.sign_ins is not None and checked.links is not None:
            say(
                f"restore: {_n(checked.sign_ins, 'sign-in')} and"
                f" {_n(checked.links, 'share link')} are back as they were; sign out everywhere"
                " or revoke a link in Settings if one should not be"
            )
        say("restore: done; start tripbox (an older ledger is upgraded on start)")
    except (_CannotWrite, OSError) as exc:  # an OSError: a stage that could not be made
        where = exc.path if isinstance(exc, _CannotWrite) else exc.filename or s.data_dir
        if swapped:
            said = RESTORED_BUT
        elif isinstance(exc, _CannotWrite) and exc.moved:
            # every path of this failure is written as compose mounts it
            raise Refused(SET_ASIDE_CANNOT_WRITE.format(name=mounted(s, where))) from exc
        else:
            said = RESTORE_CANNOT_WRITE
        raise Refused(said.format(name=_named(s, where))) from exc
    finally:
        if held is not None:
            held.close()
        for stage in stages:
            shutil.rmtree(stage, ignore_errors=True)
