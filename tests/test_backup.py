# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox backup` and `tripbox restore`, on an install seeded from the corpus's demo (the Sample
family's 21 emails), with the clock held in 2027."""

import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import threading
import zlib
from pathlib import Path

import pytest

import tripbox
from tripbox import backup, cli, corpus, demo, status, util, worker
from tripbox.ledger import Ledger, db

AT = dt.datetime(2027, 6, 16, 17, 0, tzinfo=dt.UTC)
STAMP = "20270616T170000Z"
MADE_AT = "2027-06-16T17:00:00+00:00"
LISTS = Path(tripbox.__file__).resolve().parent / "lists"
MIB = 1024 * 1024
FOLDERS = ("data", "archive", "config", "backups", "logs")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Path:
    """The corpus's demo, seeded once for the module with today at its anchor."""
    out = tmp_path_factory.mktemp("seeded") / "demo"
    c = corpus.load(corpus.default_dir())
    with util.clock_at(AT):
        done = demo.seed(c, out, c.anchor)
    assert done.failed == ()
    return out


@pytest.fixture
def install(tmp_path, monkeypatch, seeded) -> Path:
    """An install directory holding the demo's ledger, its saved emails and two config files,
    with each folder's setting pointing into it."""
    root = tmp_path / "install"
    root.mkdir()
    for name in ("data", "archive", "config"):
        shutil.copytree(seeded / name, root / name)
    shutil.copyfile(LISTS / "us_states.txt", root / "config" / "us_states.txt")
    (root / "backups").mkdir()
    (root / "logs").mkdir()
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    for name in FOLDERS:
        monkeypatch.setenv(f"TRIPBOX_{name.upper()}_DIR", str(root / name))
    return root


def run(capsys, *argv: str) -> tuple[int, list[str], list[str]]:
    """The command, with the clock held at AT: its exit status, stdout's and stderr's lines."""
    capsys.readouterr()
    with util.clock_at(AT):
        code = cli.main(list(argv))
    said = capsys.readouterr()
    return code, said.out.splitlines(), said.err.splitlines()


def emails(root: Path) -> list[str]:
    """Every regular file under the archive folder, by its path inside it."""
    base = root / "archive"
    found = [p for p in base.rglob("*") if p.is_file() and not p.is_symlink()]
    return sorted(p.relative_to(base).as_posix() for p in found)


def state(*folders: Path) -> dict:
    """Every entry under the folders, each file with the SHA-256 of its bytes and its mode."""
    found = {}
    for folder in folders:
        for p in sorted(folder.rglob("*")):
            if p.is_symlink() or not p.is_file():
                found[str(p)] = "link" if p.is_symlink() else "folder"
            else:
                found[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode)
    return found


def dump(root: Path, seeded: Path) -> str:
    """The install's ledger as text (`demo.dump`; its messages name the seed's archive)."""
    return demo.dump(root / "data" / "ledger.db", seeded / "archive")


def members(archive: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(archive) as tf:
        return tf.getmembers()


def read_member(archive: Path, name: str) -> bytes:
    with tarfile.open(archive) as tf:
        f = tf.extractfile(name)
        assert f is not None
        return f.read()


def ledger_of(archive: Path, where: Path) -> sqlite3.Connection:
    """A connection to the ledger the archive holds, written out to `where`."""
    where.write_bytes(read_member(archive, "data/ledger.db"))
    return sqlite3.connect(where)


def default_archive(root: Path) -> Path:
    return root / "backups" / f"tripbox-backup-{STAMP}.tar.gz"


# ----------------------------------------------------------------------------- backup
def test_a_backup_holds_the_ledger_and_config_and_mail_only_when_asked(install, capsys, seeded):
    # what is not a regular file directly in config, or under archive, is not taken
    (install / "config" / "notes").mkdir()
    (install / "config" / "notes" / "inner.txt").write_text("not taken\n")
    os.symlink(install / "config" / "us_states.txt", install / "config" / "linked.txt")
    os.symlink(install / "config" / "us_states.txt", install / "archive" / "linked.eml")
    saved = emails(install)
    assert 0 < len(saved) <= 50

    code, out, err = run(capsys, "backup")
    archive = default_archive(install)
    assert (code, err) == (0, [])
    assert out == [
        "backup: ledger version 1, 7 trips, 22 bookings",
        "backup: 2 config files; saved emails are not in it (--with-mail adds them)",
        f"backup: wrote {archive} ({archive.stat().st_size:,} bytes)",
    ]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert sorted(os.listdir(install / "backups")) == [archive.name]  # nothing else left
    found = members(archive)
    assert [m.name for m in found] == [
        "MANIFEST.json",
        "data/ledger.db",
        "config/travellers.yaml",
        "config/us_states.txt",
    ]
    for m in found:
        assert (m.type, m.mode, m.uid, m.gid) == (tarfile.REGTYPE, 0o600, 0, 0), m.name
    for name in ("travellers.yaml", "us_states.txt"):
        assert read_member(archive, f"config/{name}") == (install / "config" / name).read_bytes()
    held = read_member(archive, "data/ledger.db")
    manifest = json.loads(read_member(archive, "MANIFEST.json"))
    assert manifest == {
        "format": 1,
        "tripbox": tripbox.__version__,
        "made_at": MADE_AT,
        "ledger": {
            "version": 1,
            "shape": db.SCHEMA_SHAPE,
            "sha256": hashlib.sha256(held).hexdigest(),
            "bytes": len(held),
            "trips": 7,
            "bookings": 22,
        },
        "config": ["travellers.yaml", "us_states.txt"],
        "emails": 0,
        "with_mail": False,
    }
    # the ledger it holds is the ledger, one plain file
    con = ledger_of(archive, install.parent / "held.db")
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    assert demo.dump(install.parent / "held.db", seeded / "archive") == dump(install, seeded)

    with_mail = install / "backups" / "with-mail.tar.gz"
    code, out, err = run(capsys, "backup", "--with-mail", "--out", str(with_mail))
    assert (code, err) == (0, [])
    assert out == [
        "backup: ledger version 1, 7 trips, 22 bookings",
        f"backup: 2 config files, {len(saved)} emails",
        f"backup: wrote {with_mail} ({with_mail.stat().st_size:,} bytes)",
    ]
    assert stat.S_IMODE(with_mail.stat().st_mode) == 0o600
    names = [m.name for m in members(with_mail)]
    assert names[:4] == [m.name for m in found]
    assert names[4:] == [f"archive/{rel}" for rel in saved]
    for rel in saved:
        assert read_member(with_mail, f"archive/{rel}") == (install / "archive" / rel).read_bytes()
    manifest = json.loads(read_member(with_mail, "MANIFEST.json"))
    assert (manifest["emails"], manifest["with_mail"]) == (len(saved), True)


def test_a_backup_is_taken_while_the_ledger_is_open_for_writing(install, capsys):
    ledger = install / "data" / "ledger.db"
    stop = threading.Event()
    written = []

    def writer() -> None:
        """Commits two rows at a time until told to stop (at most 2,000 times)."""
        conn = sqlite3.connect(ledger, timeout=10, isolation_level=None)
        try:
            for n in range(2000):
                if stop.is_set():
                    break
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO kv(k, v) VALUES (?, 'a')", (f"pair-{n}-a",))
                conn.execute("INSERT INTO kv(k, v) VALUES (?, 'b')", (f"pair-{n}-b",))
                conn.execute("COMMIT")
                written.append(n)
        finally:
            conn.close()

    with Ledger.open(ledger) as led:
        # committed, and still in the -wal: this connection stays open
        led.kv.set("committed", "in the wal")
        assert ledger.with_name("ledger.db-wal").stat().st_size > 0
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            code, out, err = run(capsys, "backup", "--out", str(install / "backups" / "a.tar.gz"))
        finally:
            stop.set()
            thread.join(timeout=60)
        assert (code, err) == (0, []) and written
        # a transaction under way is not in it
        led.conn.execute("BEGIN IMMEDIATE")
        led.conn.execute("INSERT INTO kv(k, v) VALUES ('uncommitted', 'x')")
        code, out, err = run(capsys, "backup", "--out", str(install / "backups" / "b.tar.gz"))
        led.conn.execute("COMMIT")
        assert (code, err) == (0, [])

    for name in ("a", "b"):
        con = ledger_of(install / "backups" / f"{name}.tar.gz", install.parent / f"{name}.db")
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            kv = dict(con.execute("SELECT k, v FROM kv"))
        finally:
            con.close()
        assert kv["committed"] == "in the wal"
        pairs = {k[: -len("-a")] for k in kv if k.startswith("pair-")}
        for pair in pairs:  # each transaction whole or not at all
            assert f"{pair}-a" in kv and f"{pair}-b" in kv, pair
        assert "uncommitted" not in kv
    assert len({k for k in kv if k.startswith("pair-")}) == 2 * len(written)


def test_a_backup_with_mail_is_refused_inside_the_data_folder(install, capsys):
    data = install / "data"
    before = state(*(install / n for n in FOLDERS if n != "logs"))
    for out in (
        data / "with-mail.tar.gz",
        data / "deeper" / "with-mail.tar.gz",
        install / "backups" / ".." / "data" / "with-mail.tar.gz",
    ):
        code, said, err = run(capsys, "backup", "--with-mail", "--out", str(out))
        assert (code, said) == (2, [])
        assert err == [
            f"backup: a backup with mail holds every saved email; write it outside {data}"
        ]
    # nor in the logs or the config folder, which the page reads too
    for name in ("logs", "config"):
        code, said, err = run(
            capsys, "backup", "--with-mail", "--out", str(install / name / "with-mail.tar.gz")
        )
        assert (code, said) == (2, [])
        assert err == [
            "backup: a backup with mail holds every saved email; write it outside"
            f" {install / name}"
        ]
    assert state(*(install / n for n in FOLDERS if n != "logs")) == before
    # without mail it may go there
    code, said, err = run(capsys, "backup", "--out", str(data / "no-mail.tar.gz"))
    assert (code, err) == (0, [])
    assert (data / "no-mail.tar.gz").is_file()


def test_a_backup_never_overwrites_and_leaves_nothing_when_it_fails(
    install, capsys, monkeypatch
):
    backups = install / "backups"
    there = backups / "there.tar.gz"
    there.write_bytes(b"kept as it is")
    code, out, err = run(capsys, "backup", "--out", str(there))
    assert (code, out, err) == (2, [], [f"backup: {there} is already there"])
    assert there.read_bytes() == b"kept as it is"
    default_archive(install).write_bytes(b"made this same second")
    code, out, err = run(capsys, "backup")
    assert (code, out) == (2, [])
    assert err == [f"backup: {default_archive(install)} is already there"]
    assert default_archive(install).read_bytes() == b"made this same second"
    for p in backups.iterdir():
        p.unlink()

    # a folder it cannot write in, or none
    nowhere = install / "nowhere" / "x.tar.gz"
    code, out, err = run(capsys, "backup", "--out", str(nowhere))
    assert (code, out, err) == (2, [], [f"backup: cannot write {nowhere}"])
    if os.geteuid() != 0:  # root writes in any folder
        backups.chmod(0o500)
        try:
            code, out, err = run(capsys, "backup")
            assert (code, out) == (2, [])
            assert err == [f"backup: cannot write {default_archive(install)}"]
        finally:
            backups.chmod(0o700)
    assert os.listdir(backups) == []

    # a write that fails half-way leaves nothing behind
    real = tarfile.TarFile.addfile
    added = []

    def full_disk(self, tarinfo, fileobj=None):
        added.append(tarinfo.name)
        if len(added) == 3:
            raise OSError(28, "No space left on device")
        return real(self, tarinfo, fileobj)

    with monkeypatch.context() as m:
        m.setattr(tarfile.TarFile, "addfile", full_disk)
        code, out, err = run(capsys, "backup", "--with-mail")
    assert (code, out) == (2, [])
    assert err == [f"backup: cannot write {default_archive(install)}"]
    assert os.listdir(backups) == []

    # no folder at the default place: mount one or give --out
    backups.rmdir()
    code, out, err = run(capsys, "backup")
    assert (code, out) == (2, [])
    assert err == [f"backup: {backups} is not there; mount a folder at it or give --out FILE"]
    assert not backups.exists()

    # no ledger yet
    ledger = install / "data" / "ledger.db"
    ledger.rename(install / "elsewhere.db")
    code, out, err = run(capsys, "backup", "--out", str(install / "x.tar.gz"))
    assert (code, out) == (2, [])
    assert err == [f"backup: no ledger at {ledger}; there is nothing to back up yet"]
    assert not (install / "x.tar.gz").exists()


def _with_a_note(schema: str) -> str:
    """The schema with one more column on trips: the next version's, for these tests."""
    end = "  updated_at TEXT NOT NULL\n);\nCREATE INDEX IF NOT EXISTS trips_dates"
    assert schema.count(end) == 1
    return schema.replace(end, end.replace("NOT NULL\n", "NOT NULL,\n  synthetic_note TEXT\n"))


def _next_version(m: pytest.MonkeyPatch) -> None:
    """From here this code is the next version's: its schema adds a column to trips, its one
    step adds it, and this version's shape is a released one."""
    schema = _with_a_note(db.SCHEMA)
    m.setattr(db, "RELEASED_SHAPES", {1: db.SCHEMA_SHAPE})
    m.setattr(db, "SCHEMA", schema)
    shape = hashlib.sha256(re.sub(r"\s+", " ", schema).encode()).hexdigest()[:12]
    m.setattr(db, "SCHEMA_SHAPE", shape)
    m.setattr(db, "SCHEMA_VERSION", 2)
    m.setattr(db, "MIGRATIONS", {2: _add_the_note})


def _add_the_note(conn) -> None:
    conn.execute("ALTER TABLE trips ADD COLUMN synthetic_note TEXT")


def test_backup_opens_no_ledger_for_writing_and_upgrades_none(install, capsys, monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("backup opened the ledger through Ledger.open")

    data = install / "data"
    before = state(data)
    with monkeypatch.context() as m:
        _next_version(m)  # the ledger here is one version older than this code
        m.setattr(Ledger, "open", refused)
        code, out, err = run(capsys, "backup")
    assert (code, err) == (0, [])
    assert out[0] == "backup: ledger version 1, 7 trips, 22 bookings"
    assert state(data) == before  # nothing written, copied or made beside it
    manifest = json.loads(read_member(default_archive(install), "MANIFEST.json"))
    assert manifest["ledger"]["version"] == 1


# ----------------------------------------------------------------------------- restore
SIGN_INS = (
    "are back as they were; sign out everywhere or revoke a link in Settings if one should not be"
)
DONE = "restore: done; start tripbox (an older ledger is upgraded on start)"
CUT = "damaged (cut short, or its bytes changed); nothing was changed"
# the crafted archives whose manifest reads, then a member is missing or cannot be read
CUT_OR_CHANGED = {"no ledger", "config missing", "mail missing", "sparse size not a number"}
NOTHING = "restore: nothing was changed; run again with --apply"


def kept_as(root: Path, n: int = 1) -> Path:
    return root / "data" / f"ledger-before-restore-{STAMP}{'' if n == 1 else f'-{n}'}.db"


def taken(root: Path, *argv: str, capsys) -> Path:
    """A backup made with `argv` into the backups folder, under its own name."""
    out = root / "backups" / f"{len(list((root / 'backups').iterdir()))}.tar.gz"
    code, _, err = run(capsys, "backup", "--out", str(out), *argv)
    assert (code, err) == (0, [])
    return out


def in_place(root: Path) -> dict:
    """What a restore that changes nothing leaves as it was: the data, archive and config
    folders, file by file."""
    return state(root / "data", root / "archive", root / "config")


def test_restore_without_apply_changes_nothing_and_says_what_it_would_do(install, capsys):
    archive = taken(install, "--with-mail", capsys=capsys)
    n = len(emails(install))
    with (install / "config" / "travellers.yaml").open("a") as f:
        f.write("# changed after the backup\n")
    before = in_place(install)
    code, out, err = run(capsys, "restore", str(archive))
    assert (code, err) == (0, [])
    assert out == [
        f"restore: backup made {MADE_AT} by tripbox {tripbox.__version__}: ledger version 1,"
        f" 7 trips, 22 bookings, 2 config files, {n} emails",
        "restore: the ledger here holds 7 trips, 22 bookings; it would be kept beside it",
        "restore: config is never written; 1 file differs from the backup's",
        NOTHING,
    ]
    assert in_place(install) == before
    # with no ledger here, and the config as it was
    (install / "config" / "travellers.yaml").write_bytes(
        read_member(archive, "config/travellers.yaml")
    )
    (install / "data" / "ledger.db").rename(install / "away.db")
    before = in_place(install)
    code, out, err = run(capsys, "restore", str(archive))
    assert (code, err) == (0, [])
    assert out[1:] == [
        "restore: there is no ledger here yet",
        "restore: config is the same as the backup's",
        NOTHING,
    ]
    assert in_place(install) == before


def _sessions_and_links(ledger: Path) -> None:
    """Three sign-ins, a share link and a revoked one."""
    with Ledger.open(ledger) as led:
        for n in range(3):
            led.sessions.add(f"{n:064x}", "cred")
        trip = led.conn.execute("SELECT id FROM trips ORDER BY id LIMIT 1").fetchone()[0]
        led.shares.add(trip_id=trip, token="t1", token_hash="1" * 64, shows="[]")
        gone = led.shares.add(trip_id=trip, token="t2", token_hash="2" * 64, shows="[]")
        led.shares.revoke(gone)


def test_restore_keeps_the_ledger_that_was_there_and_removes_its_wal(install, capsys, seeded):
    data = install / "data"
    ledger = data / "ledger.db"
    _sessions_and_links(ledger)
    before = dump(install, seeded)
    archive = taken(install, capsys=capsys)
    # written to since, and left with its -wal as a process that died leaves it
    led = Ledger.open(ledger)
    led.kv.set("after", "in the wal")
    led.conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    led.close()
    assert data.joinpath("ledger.db-wal").stat().st_size > 0

    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert out == [
        f"restore: the ledger that was here is kept as {kept_as(install)}",
        "restore: ledger restored: version 1, 7 trips, 22 bookings",
        "restore: 0 emails added, 0 already there, 0 differ and were left",
        f"restore: 3 sign-ins and 1 share link {SIGN_INS}",
        DONE,
    ]
    assert sorted(os.listdir(data)) == sorted(["ledger.db", kept_as(install).name])
    assert dump(install, seeded) == before
    assert stat.S_IMODE(kept_as(install).stat().st_mode) == 0o600
    con = sqlite3.connect(f"file:{kept_as(install)}?mode=ro&immutable=1", uri=True)
    try:  # the copy holds what the -wal held
        assert con.execute("SELECT v FROM kv WHERE k='after'").fetchone() == ("in the wal",)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    with Ledger.open(ledger) as led:
        assert led.kv.get("after") is None
    # again in the same second: the first copy stays as it is
    first = kept_as(install).read_bytes()
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert out[0] == f"restore: the ledger that was here is kept as {kept_as(install, 2)}"
    assert kept_as(install).read_bytes() == first


def test_restore_adds_missing_mail_and_never_overwrites_an_email(install, capsys):
    saved = emails(install)
    assert len(saved) >= 3
    archive = taken(install, "--with-mail", capsys=capsys)
    base = install / "archive"
    gone = saved[:2]
    kept = {rel: (base / rel).read_bytes() for rel in gone}
    for rel in gone:
        (base / rel).unlink()
    changed = base / saved[2]
    changed.write_bytes(b"changed here since\n")

    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert out[2] == (
        f"restore: 2 emails added, {len(saved) - 3} already there, 1 differs and was left"
    )
    assert emails(install) == saved
    for rel in gone:
        assert (base / rel).read_bytes() == kept[rel]
        assert stat.S_IMODE((base / rel).stat().st_mode) == 0o600
    assert changed.read_bytes() == b"changed here since\n"
    assert [p.name for p in base.iterdir() if p.name.startswith(".")] == []
    # all there now: nothing is added
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert out[2] == (
        f"restore: 0 emails added, {len(saved) - 1} already there, 1 differs and was left"
    )


def test_restore_never_writes_config_and_prints_the_line_that_does(install, capsys, tmp_path):
    archive = taken(install, capsys=capsys)
    travellers = install / "config" / "travellers.yaml"
    was = travellers.read_bytes()
    travellers.write_bytes(was + b"# changed after the backup\n")
    (install / "config" / "us_states.txt").chmod(0o400)
    before = state(install / "config")
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    fix = (
        "restore: config is not written: 1 file differs; put it back with:"
        f" tar -xzf {archive} config"
    )
    assert out[3] == fix
    assert out[-1] == DONE
    assert state(install / "config") == before
    # the line does what it says
    here = tmp_path / "elsewhere"
    here.mkdir()
    subprocess.run(fix.split("put it back with: ")[1].split(), cwd=here, check=True, timeout=60)
    assert (here / "config" / "travellers.yaml").read_bytes() == was
    # the same config: no line about it
    travellers.write_bytes(was)
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert [line for line in out if "config" in line] == []


def _worker(ledger: Path, **fields) -> None:
    """A worker's record, its last beat five seconds before AT."""
    record = {
        "pid": 1,
        "host": "h",
        "beat_at": (AT - dt.timedelta(seconds=5)).isoformat(timespec="seconds"),
        "stopped_at": None,
        **fields,
    }
    with Ledger.open(ledger) as led:
        led.kv.set(status.KV_WORKER, json.dumps(record))


def test_restore_is_refused_while_a_worker_is_live(install, capsys):
    ledger = install / "data" / "ledger.db"
    archive = taken(install, "--with-mail", capsys=capsys)
    (install / "archive" / emails(install)[0]).unlink()
    worker = (
        "restore: a worker is running on this ledger; stop tripbox first (docker compose down),"
        " and if you just did, wait a minute"
    )
    _worker(ledger)
    before = in_place(install)
    for argv in ((), ("--apply",)):
        code, out, err = run(capsys, "restore", str(archive), *argv)
        assert (code, out, err) == (2, [], [worker])
        assert in_place(install) == before
    # a stopped worker is none; but anything holding the ledger open is refused all the same
    _worker(ledger, stopped_at=(AT - dt.timedelta(seconds=1)).isoformat(timespec="seconds"))
    holder = sqlite3.connect(ledger)
    try:
        holder.execute("SELECT COUNT(*) FROM trips").fetchone()
        before = in_place(install)
        code, out, err = run(capsys, "restore", str(archive), "--apply")
        assert (code, out, err) == (2, [], [worker])
        assert in_place(install) == before
    finally:
        holder.close()
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err, out[-1]) == (0, [], DONE)


def rewrite(source: Path, target: Path, edit) -> Path:
    """`target`: the archive at `source` with its members, each (TarInfo, bytes), passed
    through `edit`, which returns those to write (or raw blocks, as bytes); at most 1 MiB."""
    with tarfile.open(source) as tf:
        got = [(m, tf.extractfile(m).read()) for m in tf.getmembers()]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as out:
        for item in edit(got):
            if isinstance(item, bytes):  # blocks written as they are
                out.fileobj.write(item)
                out.offset += len(item)
                continue
            info, data = item
            info.size = len(data) if info.isreg() else 0
            out.addfile(info, io.BytesIO(data) if info.isreg() else None)
    assert len(buf.getvalue()) <= MIB
    target.write_bytes(buf.getvalue())
    return target


def member(name: str, data: bytes = b"", kind: bytes = tarfile.REGTYPE, link: str = ""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = kind, link, 0o600
    return info, data


def manifest_with(got, **changes):
    """The members with the manifest changed: a key set to a value, or `ledger.<key>`."""
    out = []
    for info, data in got:
        if info.name == "MANIFEST.json":
            doc = json.loads(data)
            for key, value in changes.items():
                if key.startswith("ledger_"):
                    doc["ledger"][key[len("ledger_") :]] = value
                else:
                    doc[key] = value
            data = json.dumps(doc).encode()
        out.append((info, data))
    return out


def test_a_backup_from_a_newer_tripbox_is_refused(install, capsys, monkeypatch):
    archive = taken(install, capsys=capsys)
    newer = rewrite(
        archive, install.parent / "format-2.tar.gz", lambda g: manifest_with(g, format=2)
    )
    before = in_place(install)
    code, out, err = run(capsys, "restore", str(newer), "--apply")
    assert (code, out) == (2, [])
    assert err == [
        "restore: this backup was made by a newer tripbox (format 2); upgrade tripbox first"
    ]
    assert in_place(install) == before
    # a ledger of the next version, read back by this one
    with monkeypatch.context() as m:
        _next_version(m)
        Ledger.open(install / "data" / "ledger.db").close()
        ahead = taken(install, capsys=capsys)
    before = in_place(install)
    for argv in ((), ("--apply",)):
        code, out, err = run(capsys, "restore", str(ahead), *argv)
        assert (code, out) == (2, [])
        assert err == [
            "restore: this backup's ledger is from a newer tripbox (ledger version 2);"
            " upgrade tripbox first"
        ]
        assert in_place(install) == before


def _corrupt(got):
    """The ledger with a page overwritten, its manifest's hash and size made to match."""
    out = []
    for info, data in got:
        if info.name == "data/ledger.db":
            data = data[:4096] + b"\xff" * 4096 + data[8192:]
        out.append((info, data))
    held = next(d for i, d in out if i.name == "data/ledger.db")
    sha = hashlib.sha256(held).hexdigest()
    return manifest_with(out, ledger_sha256=sha, ledger_bytes=len(held))


def with_sql(work: Path, sql: str, **manifest):
    """The members with `sql` run on the ledger (a copy written in `work`), its manifest's hash
    and size made to match, and `manifest`'s changes made to it."""

    def edit(got):
        out = []
        for info, data in got:
            if info.name == "data/ledger.db":
                where = work / f"edit-{secrets.token_hex(4)}.db"
                where.write_bytes(data)
                con = sqlite3.connect(where)
                try:
                    con.executescript(sql)
                finally:
                    con.close()
                data = where.read_bytes()
            out.append((info, data))
        held = next(d for i, d in out if i.name == "data/ledger.db")
        sha = hashlib.sha256(held).hexdigest()
        return manifest_with(out, ledger_sha256=sha, ledger_bytes=len(held), **manifest)

    return edit


def test_an_archive_that_is_not_a_backup_changes_nothing(install, capsys):
    archive = taken(install, capsys=capsys)
    with_mail = taken(install, "--with-mail", capsys=capsys)
    missing = emails(install)[0]
    (install / "archive" / missing).unlink()  # a restore would add it back
    work = install.parent / "crafted"
    work.mkdir()

    def flip(got):
        """The ledger's last byte changed, and nothing else."""
        return [
            (i, d[:-1] + bytes([d[-1] ^ 1]) if i.name == "data/ledger.db" else d) for i, d in got
        ]

    cases = {
        "parent": (archive, lambda g: [*g, member("../x", b"x")]),
        "parent-inside": (archive, lambda g: [*g, member("config/../../x", b"x")]),
        "absolute": (archive, lambda g: [*g, member("/tmp/x", b"x")]),
        "symlink": (archive, lambda g: [*g, member("config/link", kind=tarfile.SYMTYPE, link="/")]),
        "hardlink": (
            archive,
            lambda g: [*g, member("config/hard", kind=tarfile.LNKTYPE, link="data/ledger.db")],
        ),
        "device": (archive, lambda g: [*g, member("config/dev", kind=tarfile.CHRTYPE)]),
        "fifo": (archive, lambda g: [*g, member("config/fifo", kind=tarfile.FIFOTYPE)]),
        "folder": (archive, lambda g: [*g, member("config/sub", kind=tarfile.DIRTYPE)]),
        "not named": (archive, lambda g: [*g, member("config/extra.yaml", b"x: 1\n")]),
        "elsewhere": (archive, lambda g: [*g, member("logs/x.log", b"x")]),
        "mail not said": (archive, lambda g: [*g, member("archive/2027/06/x.eml", b"x")]),
        "twice": (archive, lambda g: [*g, g[1]]),
        "no manifest": (archive, lambda g: g[1:]),
        "manifest last": (archive, lambda g: [*g[1:], g[0]]),
        "no ledger": (archive, lambda g: [g[0], *g[2:]]),
        "config missing": (archive, lambda g: g[:-1]),
        "mail missing": (with_mail, lambda g: g[:-1]),
        "file and folder": (
            with_mail,
            lambda g: manifest_with(
                [*g[:-1], member("archive/2027", b"x"), member("archive/2027/x.eml", b"x")],
                emails=len(g) - 4 + 1,
            ),
        ),
        "mail outside": (
            with_mail,
            lambda g: manifest_with(
                [*g, member("archive/../../escaped", b"x")], emails=len(g) - 4 + 1
            ),
        ),
        "mail link": (
            with_mail,
            lambda g: manifest_with(
                [*g, member("archive/x.eml", kind=tarfile.SYMTYPE, link="/")],
                emails=len(g) - 4 + 1,
            ),
        ),
        "big manifest": (archive, lambda g: [(g[0][0], g[0][1] + b" " * MIB), *g[1:]]),
        "manifest no json": (archive, lambda g: [(g[0][0], b"{not json"), *g[1:]]),
        "manifest deep": (archive, lambda g: [(g[0][0], b"[" * 100_000), *g[1:]]),
        "manifest format": (archive, lambda g: manifest_with(g, format="1")),
        "manifest emails": (archive, lambda g: manifest_with(g, emails=-1)),
        "manifest made_at": (archive, lambda g: manifest_with(g, made_at="\x1b[2Jnow")),
        "manifest name": (archive, lambda g: manifest_with(g, config=["../travellers.yaml"])),
    }
    def with_header(got, key: str, size: int):
        """The ledger's member carrying an extended header `key` of `size` bytes."""
        out = []
        for info, data in got:
            if info.name == "data/ledger.db":
                info.pax_headers = {key: "x" * size}
            out.append((info, data))
        return out

    cases["header of 2 MiB"] = (archive, lambda g: with_header(g, "comment", 2 * MIB))
    cases["name of 2 MiB"] = (archive, lambda g: [*g, member("config/" + "n/" * MIB + "x", b"")])

    def extended(record: dict, kind: bytes = tarfile.XHDTYPE) -> bytes:
        """An extended header's blocks, standing alone."""
        return tarfile.TarInfo._create_pax_generic_header(record, kind, "utf-8")

    def sparse(headers: dict):
        """The first config file made a sparse one by `headers`."""

        def edit(got):
            for info, _ in got:
                if info.name == "config/travellers.yaml":
                    info.pax_headers = headers
            return got

        return edit

    many = 2**50
    cases["two extended headers"] = (
        archive,
        lambda g: [g[0], extended({"comment": "x"}) * 2, *g[1:]],
    )
    cases["global header"] = (
        archive,
        lambda g: [g[0], extended({"comment": "x"}, tarfile.XGLTYPE), *g[1:]],
    )
    cases["old sparse"] = (archive, lambda g: [*g, member("config/x", kind=tarfile.GNUTYPE_SPARSE)])
    cases["sparse 0.0"] = (archive, sparse({"GNU.sparse.size": str(many)}))
    cases["sparse 0.1"] = (
        archive,
        sparse({"GNU.sparse.map": "0,0", "GNU.sparse.size": str(many)}),
    )
    cases["sparse 1.0"] = (
        archive,
        sparse(
            {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": str(many)}
        ),
    )
    # a sparse size that is no number: the tar reader raises ValueError of its own
    cases["sparse size not a number"] = (archive, sparse({"GNU.sparse.realsize": "abc"}))
    cases["manifest emails over the ceiling"] = (
        with_mail,
        lambda g: manifest_with(g, emails=backup.EMAILS_MAX + 1),
    )
    cases["more emails than named"] = (with_mail, lambda g: [*g, member("archive/x.eml", b"x")])
    cases["name of 4,097 bytes"] = (
        with_mail,
        lambda g: manifest_with(
            [*g, member("archive/" + "a/" * 2044 + "xyz", b"")], emails=len(g) - 4 + 1
        ),
    )

    def deep(got):
        """Names of under 4,096 bytes (with room for the stage's path), each with 1,900
        folders of its own."""
        extra = [member(f"archive/{n}/" + "a/" * 1900 + "x", b"") for n in range(40)]
        return manifest_with([*got, *extra], emails=len(got) - 4 + len(extra))

    cases["names that fill memory"] = (with_mail, deep)

    damaged = {
        "hash": (archive, flip),
        "size": (archive, lambda g: manifest_with(g, ledger_bytes=1)),
        "integrity": (archive, _corrupt),
        "shape": (archive, lambda g: manifest_with(g, ledger_shape="0" * 12)),
    }
    before = in_place(install)
    data_before = sorted(os.listdir(install / "data"))
    for name, (source, edit) in {**cases, **damaged}.items():
        crafted = rewrite(source, work / f"{name}.tar.gz", edit)
        for argv in ((), ("--apply",)):
            code, out, err = run(capsys, "restore", str(crafted), *argv)
            if name in damaged:
                want = "restore: the backup's ledger is damaged; nothing was changed"
            elif name in CUT_OR_CHANGED:  # a valid manifest, then a member missing or unread
                want = f"restore: {crafted} is {CUT}"
            else:
                want = f"restore: {crafted} is not a tripbox backup"
            assert (code, out, err) == (2, [], [want]), name
            assert in_place(install) == before, name
            assert sorted(os.listdir(install / "data")) == data_before, name
    # bytes that are no archive at all, and no file
    # and an archive cut before its manifest is read
    head = archive.read_bytes()[:20]
    for name, data in (("text", b"no archive\n"), ("empty", b""), ("head", head)):
        crafted = work / f"{name}.tar.gz"
        crafted.write_bytes(data)
        code, out, err = run(capsys, "restore", str(crafted), "--apply")
        assert (code, out, err) == (2, [], [f"restore: {crafted} is not a tripbox backup"]), name
    assert [p for p in install.parent.rglob("escaped")] == []
    # what the archive says it holds is written only while there is room for it, and 64 MiB
    # are left free on the disk the stages share
    ledger = install / "data" / "ledger.db"
    gone = install / "archive" / missing
    held = len(read_member(with_mail, "data/ledger.db"))
    email = len(read_member(with_mail, f"archive/{missing}"))
    assert ledger.is_file() and gone.parent.is_dir()
    unchanged = "; nothing was changed"
    for free, line in (
        (1, f"restore: cannot write data/ledger.db{unchanged}"),
        (64 * MIB + held - 1, f"restore: cannot write data/ledger.db{unchanged}"),
        (64 * MIB + held + email - 1, f"restore: cannot write archive/{missing}{unchanged}"),
    ):
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                shutil, "disk_usage", lambda path, f=free: shutil._ntuple_diskusage(f, 0, f)
            )
            code, out, err = run(capsys, "restore", str(with_mail), "--apply")
        assert (code, out, err) == (2, [], [line]), free
        assert in_place(install) == before
    # a reading that runs out of memory or of stack is refused the same way
    for trouble in (MemoryError, RecursionError):

        def fails(name, trouble=trouble):
            raise trouble()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(backup, "_parents", fails)
            code, out, err = run(capsys, "restore", str(archive), "--apply")
        assert (code, out, err) == (2, [], [f"restore: {archive} is not a tripbox backup"])
        assert in_place(install) == before
    missing = work / "missing.tar.gz"
    code, out, err = run(capsys, "restore", str(missing), "--apply")
    assert (code, out, err) == (2, [], [f"restore: {missing} is not a tripbox backup"])
    assert in_place(install) == before


def test_the_tar_reader_has_the_steps_restore_hooks():
    """restore's member check (`backup._Bounded`) hooks the tar reader's own step for each
    header it reads; a tarfile that named them otherwise would read headers past the check."""
    hooks = ("_proc_member", "_proc_gnusparse_00", "_proc_gnusparse_01", "_proc_gnusparse_10")
    assert all(hasattr(tarfile.TarInfo, name) for name in hooks)


def test_a_name_that_cannot_be_written_is_shown_plain_and_says_what_changed(
    install, capsys, monkeypatch
):
    """The name a restore could not write is the install's own, relative and with no control
    character; before the ledger is replaced nothing was changed, after it the ledger is back
    and a second run adds the emails that were not."""
    with_mail = taken(install, "--with-mail", capsys=capsys)
    saved = emails(install)
    hostile = "archive/\x1b[2J\x07x.eml"
    crafted = rewrite(
        with_mail,
        install.parent / "hostile.tar.gz",
        lambda g: manifest_with([*g, member(hostile, b"x")], emails=len(saved) + 1),
    )
    before = in_place(install)
    held = len(read_member(crafted, "data/ledger.db"))
    room = 64 * MIB + held  # for the ledger alone
    with monkeypatch.context() as m:
        m.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(0, 0, room))
        code, out, err = run(capsys, "restore", str(crafted), "--apply")
    assert (code, out) == (2, [])
    assert err == ["restore: cannot write archive/?[2J?x.eml; nothing was changed"]
    assert in_place(install) == before
    # the ledger is replaced, then an email cannot be put in its place
    gone = saved[0]
    (install / "archive" / gone).unlink()
    real = backup._place

    def refuses_mail(source, target):
        if target.is_relative_to(install / "archive"):
            raise PermissionError(13, "Permission denied", str(target))
        return real(source, target)

    with monkeypatch.context() as m:
        m.setattr(backup, "_place", refuses_mail)
        code, out, err = run(capsys, "restore", str(with_mail), "--apply")
    assert code == 2
    assert out == [
        f"restore: the ledger that was here is kept as {kept_as(install)}",
        "restore: ledger restored: version 1, 7 trips, 22 bookings",
    ]
    assert err == [
        f"restore: the ledger is restored, but archive/{gone} could not be written; run restore"
        " again to add the missing emails"
    ]
    code, out, err = run(capsys, "restore", str(with_mail), "--apply")
    assert (code, err) == (0, [])
    assert gone in emails(install) and out[-1] == DONE


def test_what_a_ledger_moved_away_left_beside_it_is_kept(install, capsys):
    """The ledger here could not be read and the owner moved it away, leaving its -wal and -shm:
    they are renamed beside where the ledger was, never deleted, and named."""
    data = install / "data"
    archive = taken(install, capsys=capsys)
    led = Ledger.open(data / "ledger.db")
    led.kv.set("after", "in the wal")
    led.conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    led.close()
    left = {end: data.joinpath(f"ledger.db{end}").read_bytes()
            for end in ("-wal", "-shm") if data.joinpath(f"ledger.db{end}").exists()}
    assert left.get("-wal")
    (data / "ledger.db").rename(install / "away.db")
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    aside = {end: data / f"ledger-before-restore-{STAMP}.db{end}" for end in left}
    assert out[: len(left)] == [
        f"restore: {data / f'ledger.db{end}'}, left by a ledger no longer here, is kept as"
        f" {aside[end]}"
        for end in left
    ]
    assert out[len(left)] == "restore: ledger restored: version 1, 7 trips, 22 bookings"
    assert not any(data.joinpath(f"ledger.db{end}").exists() for end in left)
    assert {end: p.read_bytes() for end, p in aside.items()} == left
    # put back beside the ledger moved away, they still hold what it wrote
    shutil.copyfile(install / "away.db", aside["-wal"].with_suffix(".db"))
    con = sqlite3.connect(aside["-wal"].with_suffix(".db"))
    try:
        assert con.execute("SELECT v FROM kv WHERE k='after'").fetchone() == ("in the wal",)
    finally:
        con.close()
    # a restore in the same second keeps the ledger here under a name of its own
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert out[0] == f"restore: the ledger that was here is kept as {kept_as(install, 2)}"


def test_the_held_ledgers_close_removes_no_file_of_the_restored_one(install, capsys, monkeypatch):
    """A process that opens the restored ledger the moment it has its name may make its journal
    before restore lets the ledger it held go: that close deletes no file by name."""
    archive = taken(install, capsys=capsys)
    ledger = install / "data" / "ledger.db"
    journal = ledger.with_name("ledger.db-journal")
    led = Ledger.open(ledger)  # left with its -wal, as a process that died leaves it
    led.kv.set("after", "in the wal")
    led.conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    led.close()
    real = os.replace

    def then_a_newcomer(src, dst, **kw):
        real(src, dst, **kw)
        if Path(dst) == ledger:
            journal.write_bytes(b"a newcomer's journal")

    with monkeypatch.context() as m:
        m.setattr(backup.os, "replace", then_a_newcomer)
        code, _, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert journal.read_bytes() == b"a newcomer's journal"


def test_a_backup_with_more_emails_than_restore_takes_is_refused(install, capsys, monkeypatch):
    """restore reads at most EMAILS_MAX emails from one backup, so backup refuses to write one
    with more, before writing anything, and says what to do instead."""
    n = len(emails(install))
    monkeypatch.setattr(backup, "EMAILS_MAX", n - 1)
    before = state(install / "backups", install / "data")
    code, out, err = run(capsys, "backup", "--with-mail")
    assert (code, out) == (2, [])
    assert err == [
        f"backup: this install holds {n:,} saved emails, more than one backup can carry"
        f" ({n - 1:,}); back up without --with-mail, and copy the archive folder by hand"
    ]
    assert state(install / "backups", install / "data") == before
    # without the mail it is written
    code, _, err = run(capsys, "backup")
    assert (code, err) == (0, [])


def test_a_backup_holds_every_email_its_ledger_names(install, capsys, monkeypatch):
    """Mail is listed after the ledger's copy is taken, so an email the worker saves while the
    copy is made is in the backup too."""
    real = db.write_copy
    late = install / "archive" / "saved-meanwhile.eml"

    def while_saving(path, to, source=None):
        late.write_bytes(b"saved while the copy is made")
        return real(path, to, source)

    with monkeypatch.context() as m:
        m.setattr(backup.db, "write_copy", while_saving)
        archive = taken(install, "--with-mail", capsys=capsys)
    assert read_member(archive, "archive/saved-meanwhile.eml") == late.read_bytes()


def test_a_backup_put_back_is_the_same_ledger(install, capsys, seeded):
    ledger = install / "data" / "ledger.db"
    before = dump(install, seeded)
    code, _, err = run(capsys, "backup")
    assert (code, err) == (0, [])
    with Ledger.open(ledger) as led:  # written to meanwhile, as the worker does
        led.kv.set("written_after", "the backup")
        led.sessions.add("f" * 64, "cred")
    assert dump(install, seeded) != before
    code, out, err = run(capsys, "restore", str(default_archive(install)), "--apply")
    assert (code, err) == (0, [])
    assert out[1] == "restore: ledger restored: version 1, 7 trips, 22 bookings"
    assert dump(install, seeded) == before


def test_an_older_ledger_put_back_is_upgraded_on_the_next_start(install, capsys, monkeypatch):
    ledger = install / "data" / "ledger.db"
    archive = taken(install, capsys=capsys)  # a ledger of version 1
    with monkeypatch.context() as m:
        _next_version(m)  # then this code is the next version's
        Ledger.open(ledger).close()
        code, out, err = run(capsys, "restore", str(archive), "--apply")
        assert (code, err) == (0, [])
        assert out[1] == "restore: ledger restored: version 1, 7 trips, 22 bookings"
        assert not [line for line in out if "sign-ins" in line]  # an older ledger's own
        assert out[-1] == DONE
        con = sqlite3.connect(f"file:{ledger}?mode=ro&immutable=1", uri=True)
        try:  # put back as it was made: of version 1
            assert con.execute("SELECT v FROM schema_version").fetchone() == (1,)
        finally:
            con.close()
        with Ledger.open(ledger) as led:
            assert led.version() == 2
            assert "synthetic_note" in {r[1] for r in led.conn.execute("PRAGMA table_info(trips)")}
            assert len(led.trips.all()) == 7


def test_an_opener_waiting_on_the_hold_never_reaches_the_restored_ledger(
    install, capsys, monkeypatch, seeded
):
    ledger = install / "data" / "ledger.db"
    before = dump(install, seeded)
    archive = taken(install, capsys=capsys)
    with Ledger.open(ledger) as led:  # the ledger here is no longer the backup's
        led.kv.set("which", "replaced")
    real = db.write_copy
    seen: dict = {}

    def opener() -> None:
        """A request of the page: it opens the ledger (waiting on the hold) and writes."""
        try:
            with Ledger.open(ledger) as led:
                seen["read"] = led.kv.get("which")
                led.kv.set("written_by_the_request", "yes")
        except Exception as exc:  # noqa: BLE001 - what it ends in is what is looked at
            seen["error"] = type(exc).__name__

    thread = threading.Thread(target=opener)

    def held_copy(path, to, source=None):
        real(path, to, source)
        if source is not None:  # the undo copy, made while restore holds the ledger
            thread.start()
            thread.join(timeout=0.5)  # long enough for it to be waiting on the hold
            assert thread.is_alive()

    real_replace = os.replace

    def slow_replace(source, target):
        """The rename, late: the hold must still stand meanwhile."""
        if Path(target) == ledger:
            thread.join(timeout=0.5)
        real_replace(source, target)

    monkeypatch.setattr(db, "write_copy", held_copy)
    monkeypatch.setattr(os, "replace", slow_replace)
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert (code, err, out[-1]) == (0, [], DONE)
    # released only after the rename, it found its file gone from under it: it read nothing
    assert seen == {"error": "OperationalError"}
    assert dump(install, seeded) == before  # the backup's, and nothing of the request's
    assert sorted(os.listdir(install / "data")) == sorted(["ledger.db", kept_as(install).name])
    con = sqlite3.connect(f"file:{ledger}?mode=ro&immutable=1", uri=True)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_a_backup_taken_while_the_worker_runs_restores_to_a_ledger_it_starts_on(
    install, capsys, monkeypatch
):
    ledger = install / "data" / "ledger.db"
    _worker(ledger, busy=True)  # live, and busy
    archive = taken(install, capsys=capsys)
    held = hashlib.sha256(archive.read_bytes()).hexdigest()
    # then tripbox is stopped (`docker compose down`): its record says so
    _worker(ledger, stopped_at=(AT - dt.timedelta(seconds=1)).isoformat(timespec="seconds"))
    for _ in range(2):  # put back, and again at once: the record put back is a stopped one
        code, out, err = run(capsys, "restore", str(archive), "--apply")
        assert (code, err, out[-1]) == (0, [], DONE)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == held  # the archive as it was
    with Ledger.open(ledger) as led:
        record = status.read(led)
    assert record is not None and record["busy"] is True
    assert record["stopped_at"] == MADE_AT
    monkeypatch.setattr(worker.signal, "signal", lambda signum, handler: None)
    from tripbox.config import Settings
    from tripbox.pipeline.run import build_context

    ctx = build_context(Settings.from_env(), with_model=False, with_notifier=False)
    try:
        later = AT + dt.timedelta(seconds=10)
        w = worker.Worker(ctx, spawn=lambda argv: None, clock=lambda: later, sleep=lambda s: None)
        with util.clock_at(later):
            w.start()  # not another worker's live record
        assert status.read(ctx.ledger)["pid"] == os.getpid()
    finally:
        ctx.ledger.close()


# ----------------------------------------------------------------------------- leftovers
def test_only_eml_files_are_saved_emails(install, capsys):
    """A backup with mail holds the saved emails alone: a regular file whose name ends `.eml`.
    What a write cut short leaves (`.tmp`, `.part`), any other file, and whatever is under a
    restore's stage folder are not emails."""
    saved = emails(install)
    base = install / "archive"
    beside = (base / saved[0]).parent
    (beside / f"{Path(saved[0]).name}.tmp").write_bytes(b"cut short")
    (beside / ".x.eml.part").write_bytes(b"cut short")
    (beside / "notes.txt").write_text("not an email\n")
    stage = base / f"{backup.STAGE_PREFIX}abc123"
    (stage / Path(saved[0]).parent).mkdir(parents=True)
    (stage / saved[0]).write_bytes((base / saved[0]).read_bytes())
    code, out, err = run(capsys, "backup", "--with-mail")
    assert (code, err) == (0, [])
    assert out[1] == f"backup: 2 config files, {len(saved)} emails"
    names = [m.name for m in members(default_archive(install))]
    assert names[4:] == [f"archive/{rel}" for rel in saved]


def _aged(path: Path, minutes: int) -> Path:
    """`path`, and everything under it, last changed `minutes` before AT."""
    when = (AT - dt.timedelta(minutes=minutes)).timestamp()
    for p in [path, *(path.rglob("*") if path.is_dir() else ())]:
        os.utime(p, (when, when))
    return path


def test_a_killed_runs_leftovers_are_swept_after_an_hour_and_never_sooner(install, capsys):
    """What a backup or a restore that was killed leaves (its `.part` files, its stage folders)
    is removed by the next one, once it is an hour old, and said; one younger may be another
    run's under way and is left, as is everything else."""
    backups, data, base = install / "backups", install / "data", install / "archive"
    hexes = [secrets.token_hex(8) for _ in range(4)]
    old = [
        backups / f".tripbox-backup-{hexes[0]}.db.part",
        backups / f".tripbox-backup-{STAMP}.tar.gz.{hexes[1]}.part",
    ]
    young = [
        backups / f".tripbox-backup-{hexes[2]}.db.part",
        backups / f".tripbox-backup-{STAMP}.tar.gz.{hexes[3]}.part",
    ]
    others = [backups / "notes.part", backups / ".kept.tar.gz.part"]
    for p in (*old, *young, *others):
        p.write_bytes(b"left")
    for p in (*old, *others):
        _aged(p, 61)
    for p in young:
        _aged(p, 59)
    code, out, err = run(capsys, "backup")
    assert (code, err) == (0, [])
    assert out[0] == "backup: removed 2 leftovers of an earlier run"
    assert out[1] == "backup: ledger version 1, 7 trips, 22 bookings"
    assert not any(p.exists() for p in old) and all(p.exists() for p in (*young, *others))
    archive = default_archive(install)

    saved = emails(install)
    stage = base / f"{backup.STAGE_PREFIX}old1"
    (stage / Path(saved[0]).parent).mkdir(parents=True)
    (stage / saved[0]).write_bytes(b"staged")
    staged = data / f"{backup.STAGE_PREFIX}old2"
    staged.mkdir()
    (staged / "ledger.db").write_bytes(b"staged")
    part = data / f".ledger-before-restore-{STAMP}.db.{hexes[0]}.part"
    part.write_bytes(b"left")
    swept = [_aged(stage, 61), _aged(staged, 90), _aged(part, 61)]
    fresh = data / f"{backup.STAGE_PREFIX}new"
    fresh.mkdir()
    (fresh / "ledger.db").write_bytes(b"staged")
    _aged(fresh, 90)
    _aged(fresh / "ledger.db", 30)  # written inside it lately: under way
    # a restore that changes nothing removes nothing
    code, out, err = run(capsys, "restore", str(archive))
    assert (code, err, out[-1]) == (0, [], NOTHING)
    assert all(p.exists() for p in (*swept, fresh))
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert out[0] == "restore: removed 3 leftovers of an earlier run"
    assert out[-1] == DONE
    assert not any(p.exists() for p in swept) and (fresh / "ledger.db").exists()
    assert emails(install) == saved
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, []) and not out[0].startswith("restore: removed")


def test_one_failure_writes_every_path_one_way(install, capsys, monkeypatch):
    """The files a failed set-aside names, those moved and the one that could not be, are all
    written as compose mounts them (`./data/...`), never one way then another."""
    data = install / "data"
    archive = taken(install, capsys=capsys)
    (data / "ledger.db").rename(install / "away.db")
    for end in ("-wal", "-shm"):
        (data / f"ledger.db{end}").write_bytes(b"left beside")
    real = backup._place

    def refuses_the_second(source, target):
        if target.name.endswith("-shm"):
            raise PermissionError(13, "Permission denied", str(target))
        return real(source, target)

    monkeypatch.setattr(backup, "_place", refuses_the_second)
    monkeypatch.setattr(backup, "mounted", lambda s, p: f"./data/{Path(p).name}")
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    aside = f"ledger-before-restore-{STAMP}.db"
    assert code == 2
    assert out == [
        f"restore: ./data/ledger.db-wal, left by a ledger no longer here, is kept as"
        f" ./data/{aside}-wal"
    ]
    assert err == [f"restore: cannot write ./data/{aside}-shm; nothing else was changed"]


def test_restore_removes_no_leftover_before_the_archive_is_proven(install, capsys):
    """A leftover of a killed run is removed only once the archive is proven: a file that is no
    backup, or one cut short, is refused with the leftover still there and nothing said of it;
    the same restore with a sound archive then removes it."""
    data = install / "data"
    archive = taken(install, capsys=capsys)
    leftover = data / f"{backup.STAGE_PREFIX}old3"
    leftover.mkdir()
    (leftover / "ledger.db").write_bytes(b"staged")
    _aged(leftover, 90)
    cut = install / "cut.tar.gz"
    cut.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])
    junk = install / "junk.tar.gz"
    junk.write_bytes(b"not a backup")
    for bad in (cut, junk):
        code, out, err = run(capsys, "restore", str(bad), "--apply")
        assert code == 2 and leftover.exists()
        assert not any("removed" in line for line in (*out, *err))
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert out[0] == "restore: removed 1 leftover of an earlier run"
    assert not leftover.exists()


def test_a_ledger_of_another_shape_is_not_called_damaged(install, capsys):
    """A backup's ledger SQLite reads whole, but that this version does not read (another
    shape of its version, an object no ledger holds, a ledger stating no version), is said to
    be so; "damaged" is for a ledger SQLite's own check fails."""
    archive = taken(install, capsys=capsys)
    work = install.parent / "shapes"
    work.mkdir()
    other = "0" * 12
    cases = {
        "trigger": with_sql(
            work, "CREATE TRIGGER t AFTER INSERT ON kv BEGIN DELETE FROM trips; END;"
        ),
        "view": with_sql(work, "CREATE VIEW v AS SELECT 1;"),
        "another schema": with_sql(work, "ALTER TABLE trips ADD COLUMN extra TEXT;"),
        "another shape stated": with_sql(
            work, f"UPDATE kv SET v='{other}' WHERE k='schema_shape';", ledger_shape=other
        ),
        "no version": with_sql(work, "DELETE FROM schema_version;"),
    }
    before = in_place(install)
    for name, edit in cases.items():
        crafted = rewrite(archive, work / f"{name}.tar.gz", edit)
        for argv in ((), ("--apply",)):
            code, out, err = run(capsys, "restore", str(crafted), *argv)
            assert (code, out) == (2, []), name
            assert err == [
                "restore: the backup's ledger is not one this version reads; nothing was changed"
            ], name
            assert in_place(install) == before, name
    crafted = rewrite(archive, work / "integrity.tar.gz", _corrupt)
    code, out, err = run(capsys, "restore", str(crafted), "--apply")
    assert (code, out, err) == (
        2,
        [],
        ["restore: the backup's ledger is damaged; nothing was changed"],
    )


def test_an_archive_cut_short_or_with_a_byte_turned_changes_nothing(install, capsys):
    """An archive whose manifest reads, then is cut short or has a byte turned (in a member's
    data, or in gzip's own checksum), is damaged: the archive is read to its end before
    anything is written, and nothing is changed."""
    archive = taken(install, "--with-mail", capsys=capsys)
    held = archive.read_bytes()
    assert len(held) <= MIB
    missing = emails(install)[0]
    (install / "archive" / missing).unlink()  # a restore would add it back

    def turned(at: int) -> bytes:
        return held[:at] + bytes([held[at] ^ 1]) + held[at + 1 :]

    work = install.parent / "damaged"
    work.mkdir()
    # the compressed byte that inflates past the manifest and the ledger's header: gzip reads
    # far ahead of the tar reader, so the damage there is met before the manifest is parsed
    inflate, out, past = zlib.decompressobj(16 + zlib.MAX_WBITS), b"", 0
    while len(out) < 4 * tarfile.BLOCKSIZE:
        out += inflate.decompress(held[past : past + 1])
        past += 1
    cases = {
        "cut at half": held[: len(held) // 2],
        "a byte just past the manifest": turned(past + 16),
        "a byte in a member's data": turned(len(held) // 2),
        "a byte of the checksum": turned(len(held) - 8),  # the first byte of gzip's CRC-32
    }
    # a member no backup writes is no backup's, but not once gzip's checksum fails after it:
    # changed bytes where no header's checksum looks read as such a member
    extra = rewrite(archive, work / "extra.tar.gz", lambda g: [*g, member("config/x", b"x")])
    whole = extra.read_bytes()
    cases["a member no backup writes, and a byte of the checksum"] = (
        whole[: len(whole) - 8] + bytes([whole[-8] ^ 1]) + whole[len(whole) - 7 :]
    )
    before = in_place(install)
    for name, data in cases.items():
        crafted = work / f"{name}.tar.gz"
        crafted.write_bytes(data)
        for argv in ((), ("--apply",)):
            code, out, err = run(capsys, "restore", str(crafted), *argv)
            assert (code, out, err) == (2, [], [f"restore: {crafted} is {CUT}"]), name
            assert in_place(install) == before, name
    assert sorted(p.name for p in (install / "data").iterdir()) == ["ledger.db"]


def test_every_printed_path_is_the_folder_compose_mounts(install, capsys, monkeypatch):
    """At the image's own folders every path backup and restore print is the one the owner
    sees on the host, in the install directory: `./backups/<name>`, `./data/<name>`."""
    import dataclasses

    from tripbox.config import KEY_BY_NAME

    for name in FOLDERS:
        key = f"TRIPBOX_{name.upper()}_DIR"
        moved = dataclasses.replace(KEY_BY_NAME[key], default=str(install / name))
        monkeypatch.setitem(KEY_BY_NAME, key, moved)
    name = f"tripbox-backup-{STAMP}.tar.gz"
    code, out, err = run(capsys, "backup")
    assert (code, err) == (0, [])
    assert re.fullmatch(rf"backup: wrote \./backups/{re.escape(name)} \([0-9,]+ bytes\)", out[-1])
    code, out, err = run(capsys, "backup")
    assert err == [f"backup: ./backups/{name} is already there"]
    archive = install / "backups" / name
    with (install / "config" / "travellers.yaml").open("a") as f:
        f.write("# changed after the backup\n")
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    kept = f"ledger-before-restore-{STAMP}.db"
    assert out[0] == f"restore: the ledger that was here is kept as ./data/{kept}"
    assert f"tar -xzf ./backups/{name} config" in out[3]
    nothing = install / "backups" / "nothing.tar.gz"
    nothing.write_bytes(b"no archive\n")
    code, out, err = run(capsys, "restore", str(nothing))
    assert err == ["restore: ./backups/nothing.tar.gz is not a tripbox backup"]
    ledger = install / "data" / "ledger.db"
    ledger.rename(install / "away.db")
    code, out, err = run(capsys, "backup", "--out", str(install / "backups" / "x.tar.gz"))
    assert err == ["backup: no ledger at ./data/ledger.db; there is nothing to back up yet"]
    (install / "data" / "ledger.db-wal").write_bytes(b"left beside")
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    assert (code, err) == (0, [])
    assert out[0] == (
        "restore: ./data/ledger.db-wal, left by a ledger no longer here, is kept as"
        f" ./data/ledger-before-restore-{STAMP}-2.db-wal"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_ledger_backup_cannot_read_is_said_so(install, capsys):
    """A ledger the backup cannot read is said so, not that the archive cannot be written."""
    ledger = install / "data" / "ledger.db"
    ledger.chmod(0o200)
    try:
        code, out, err = run(capsys, "backup")
    finally:
        ledger.chmod(0o600)
    assert (code, out, err) == (2, [], [f"backup: cannot read the ledger at {ledger}"])
    assert os.listdir(install / "backups") == []


def test_set_aside_names_what_it_moved_before_it_failed(install, capsys, monkeypatch):
    """What a ledger moved away left beside it is renamed one file at a time: when one cannot
    be, what was already moved is named, and nothing else was changed."""
    data = install / "data"
    archive = taken(install, capsys=capsys)
    (data / "ledger.db").rename(install / "away.db")
    for end in ("-wal", "-shm"):
        (data / f"ledger.db{end}").write_bytes(b"left beside")
    real = backup._place

    def refuses_the_second(source, target):
        if target.name.endswith("-shm"):
            raise PermissionError(13, "Permission denied", str(target))
        return real(source, target)

    monkeypatch.setattr(backup, "_place", refuses_the_second)
    code, out, err = run(capsys, "restore", str(archive), "--apply")
    aside = f"ledger-before-restore-{STAMP}.db"
    assert code == 2
    assert out == [
        f"restore: {data / 'ledger.db-wal'}, left by a ledger no longer here, is kept as"
        f" {data / aside}-wal"
    ]
    assert err == [f"restore: cannot write {data / aside}-shm; nothing else was changed"]
    assert not (data / "ledger.db").exists() and (data / "ledger.db-shm").exists()
