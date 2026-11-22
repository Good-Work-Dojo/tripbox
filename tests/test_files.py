# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The install's folders as the process that runs in them sees them: `doctor` names what is wrong
with each one, and the fix, in plain words. The suite runs as a user no account names, so a
folder's mode bites for real; who owns a folder is given by a stand-in, since only root can give
a folder to another user. Nothing here makes a folder the check looks at."""

import errno
import logging
import os
import stat

import pytest
from helpers.sample import CONFIG_DIR

from tripbox import cli, files
from tripbox.config import Settings

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root writes a folder whatever its mode")
UID, GID = os.getuid(), os.getgid()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The five folders under tmp_path, each there, and no login (this computer only)."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRIPBOX_AUTH", "none")
    for name in ("data", "archive", "logs", "backups"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"TRIPBOX_{name.upper()}_DIR", str(tmp_path / name))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    return tmp_path


def found(names=files.NAMES) -> list[str]:
    return [str(p) for p in files.problems(Settings.from_env(), names)]


def owned_by(monkeypatch, uid: int, gid: int) -> None:
    """The stand-in for who owns a folder or a file: `uid` and `gid`, with its real mode."""
    monkeypatch.setattr(
        files, "_owner", lambda path: (uid, gid, stat.S_IMODE(os.stat(path).st_mode))
    )


def who(owner: int, group: int, mode: str) -> str:
    return f"uid {UID}, gid {GID}; it belongs to uid {owner}, gid {group}, mode {mode}"


def doctor(capsys) -> tuple[int, list[str]]:
    code = cli.main(["doctor"])
    return code, capsys.readouterr().out.splitlines()


def after_the_count(out: list[str]) -> list[str]:
    (at,) = [i for i, line in enumerate(out) if line.startswith("problems: ")]
    return out[at + 1 :]


@NOT_ROOT
def test_doctor_names_a_folder_another_user_owns_and_both_fixes(env, monkeypatch, capsys):
    data = env / "data"
    data.chmod(0o555)
    owned_by(monkeypatch, 4242, 4343)
    try:
        code, out = doctor(capsys)
    finally:
        data.chmod(0o755)
    assert code == 1
    assert f"{'files':<15}{'invalid':<9}see the problems below" in out
    assert after_the_count(out) == [
        f"TRIPBOX_DATA_DIR: {data} is not writable by this user ({who(4242, 4343, '555')})"
        " — set TRIPBOX_UID=4242 and TRIPBOX_GID=4343 in .env and start again, or on the host"
        f" run: sudo chown -R {UID}:{GID} {data}"
    ]
    assert "problems: 1" in out


@NOT_ROOT
def test_doctor_names_a_root_owned_folder_and_the_chown_line(env, monkeypatch, capsys):
    archive, logs = env / "archive", env / "logs"
    archive.chmod(0o555)
    logs.chmod(0o500)
    owned_by(monkeypatch, 0, 0)
    try:
        code, out = doctor(capsys)
    finally:
        archive.chmod(0o755)
        logs.chmod(0o755)
    assert code == 1
    fix = f"on the host, in the install directory, run: sudo chown -R {UID}:{GID}"
    assert after_the_count(out) == [
        f"TRIPBOX_ARCHIVE_DIR: {archive} is not writable by this user ({who(0, 0, '555')})"
        f" — {fix} {archive}",
        f"TRIPBOX_LOGS_DIR: {logs} is not writable by this user ({who(0, 0, '500')})"
        f" — {fix} {logs}",
    ]


@NOT_ROOT
def test_a_folder_this_user_owns_but_cannot_write_is_told_apart(env, monkeypatch, capsys):
    data, backups = env / "data", env / "backups"
    data.chmod(0o555)
    backups.chmod(0o555)
    try:
        code, out = doctor(capsys)
    finally:
        data.chmod(0o755)
        backups.chmod(0o755)
    assert code == 1
    fix = "(a read-only mount or an SELinux label also does this)"
    assert after_the_count(out) == [
        f"TRIPBOX_DATA_DIR: {data} is not writable by this user ({who(UID, GID, '555')})"
        f" — on the host run: chmod u+rwx {data} {fix}",
        f"TRIPBOX_BACKUPS_DIR: {backups} is not writable by this user"
        f" ({who(UID, GID, '555')}) — on the host run: chmod u+rwx {backups} {fix}",
    ]
    # a config folder that cannot be listed
    config = env / "config"
    config.mkdir()
    (config / "travellers.yaml").write_text("")
    config.chmod(0o300)
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(config))
    try:
        assert found(("config",)) == [
            f"TRIPBOX_CONFIG_DIR: {config} cannot be read by this user"
            f" ({who(UID, GID, '300')}) — on the host run: chmod -R a+rX {config}"
        ]
    finally:
        config.chmod(0o755)


@NOT_ROOT
def test_an_unreadable_file_in_config_is_named_with_its_own_owner(env, monkeypatch):
    """A file in the config folder this user cannot read is named, with who owns it (not the
    folder's owner) and the one line that makes it readable; the readable ones are not."""
    config = env / "config"
    config.mkdir()
    (config / "travellers.yaml").write_text("")
    stray = config / "notes.txt"
    stray.write_text("")
    stray.chmod(0o200)

    def owner(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        return (4242, 4343, mode) if path.name == stray.name else (UID, GID, mode)

    monkeypatch.setattr(files, "_owner", owner)
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(config))
    try:
        assert found(("config",)) == [
            f"TRIPBOX_CONFIG_DIR: {stray} cannot be read by this user"
            f" ({who(4242, 4343, '200')}) — on the host run: chmod a+r {stray}"
        ]
        stray.chmod(0o600)
        assert found(("config",)) == []
    finally:
        stray.chmod(0o600)


def test_a_read_only_mount_and_a_full_disk_are_told_apart_from_permissions(env, monkeypatch):
    """The probe's error says what is wrong: a read-only mount and a disk with no room (or a
    quota used up) each have their own sentence and fix; anything else is told as a folder this
    user cannot write. The error is stood in for, as only root can mount or fill a disk."""
    real = os.open

    def failing(code: int):
        def opened(path, *args, **kwargs):
            if files.PROBE in os.fspath(path):
                raise OSError(code, os.strerror(code), os.fspath(path))
            return real(path, *args, **kwargs)

        return opened

    data = env / "data"
    mode = f"{stat.S_IMODE(data.stat().st_mode):o}"
    no_room = (f"{data} has no room left", "free some space on the disk it is on")
    told = {
        errno.EROFS: (
            f"{data} is on a read-only mount",
            "mount the folder read-write (in compose.yaml its line must not end :ro)",
        ),
        errno.ENOSPC: no_room,
        errno.EDQUOT: no_room,
        errno.EACCES: (
            f"{data} is not writable by this user ({who(UID, GID, mode)})",
            f"on the host run: chmod u+rwx {data} (a read-only mount or an SELinux label also"
            " does this)",
        ),
    }
    for code, (what, fix) in told.items():
        with monkeypatch.context() as m:
            m.setattr(os, "open", failing(code))
            assert found(("data",)) == [f"TRIPBOX_DATA_DIR: {what} — {fix}"], code
    assert found(("data",)) == []


def _fresh_logging():
    """Logging as a new process finds it, for one test; the suite's own put back after."""
    root = logging.getLogger()
    saved = (getattr(root, "_tripbox_configured", False), list(root.handlers), root.level)
    for h in saved[1]:  # an earlier test's terminal handler may write to a stream now closed
        root.removeHandler(h)
    root._tripbox_configured = False  # type: ignore[attr-defined]
    return root, saved


def _restore_logging(root, saved) -> None:
    configured, handlers, level = saved
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    root._tripbox_configured = configured  # type: ignore[attr-defined]


def test_a_ledger_that_cannot_be_opened_leaves_its_traceback_in_the_log_file(env, capsys):
    """The one line a command answers for a ledger it cannot open is all the terminal shows;
    the log file keeps the traceback, at DEBUG, for whoever looks."""
    (env / "data" / "ledger.db").mkdir()  # a folder where the file goes: SQLite cannot open it
    root, saved = _fresh_logging()
    try:
        assert cli.main(["upcoming"]) == 2
    finally:
        _restore_logging(root, saved)
    seen = capsys.readouterr()
    assert seen.err.splitlines() == [f"ledger: {cli.CANNOT_OPEN}"]
    written = (env / "logs" / "tripbox.log").read_text(encoding="utf-8")
    assert f" DEBUG tripbox.cli: ledger: {cli.CANNOT_OPEN}" in written
    assert "Traceback (most recent call last)" in written and "sqlite3." in written


@NOT_ROOT
def test_a_logs_folder_that_cannot_be_written_is_named_as_doctor_names_it(env, capsys):
    """A logs folder the process cannot write turns file logging off, said in one line in
    `doctor`'s own words: the folder as it is mounted, never a path of a file inside it."""
    from tripbox.log import setup_logging

    logs = env / "logs"
    logs.chmod(0o555)
    root, saved = _fresh_logging()
    try:
        setup_logging(logs)
    finally:
        _restore_logging(root, saved)
        logs.chmod(0o755)
    said = [line for line in capsys.readouterr().err.splitlines() if "file logging" in line]
    assert len(said) == 1
    assert said[0].endswith(
        f"file logging disabled: {logs} is not writable by this user ({who(UID, GID, '555')})"
        f" — on the host run: chmod u+rwx {logs} (a read-only mount or an SELinux label also"
        " does this)"
    )
    assert "tripbox.log" not in said[0]


@NOT_ROOT
def test_a_ledger_file_this_user_cannot_write_is_named(env, monkeypatch):
    data = env / "data"
    for name in ("ledger.db", "ledger.db-wal"):
        (data / name).write_bytes(b"")
    (data / "ledger.db-wal").chmod(0o444)
    assert found(("data",)) == [
        f"TRIPBOX_DATA_DIR: {data / 'ledger.db-wal'} is not writable by this user"
        f" ({who(UID, GID, '444')}) — on the host run: chmod u+rwx {data / 'ledger.db-wal'}"
        " (a read-only mount or an SELinux label also does this)"
    ]
    owned_by(monkeypatch, 4242, 4343)
    (data / "ledger.db").chmod(0o200)
    assert found(("data",))[0] == (
        f"TRIPBOX_DATA_DIR: {data / 'ledger.db'} is not writable by this user"
        f" ({who(4242, 4343, '200')}) — set TRIPBOX_UID=4242 and TRIPBOX_GID=4343 in .env and"
        f" start again, or on the host run: sudo chown -R {UID}:{GID} {data / 'ledger.db'}"
    )


def test_a_path_that_is_a_file_is_not_a_folder(env, monkeypatch, capsys):
    for name in ("logs", "backups"):
        (env / name).rmdir()
        (env / name).write_text("")
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(env / "logs"))
    code, out = doctor(capsys)
    assert code == 1
    fix = "move it away and make a folder there"
    assert after_the_count(out) == [
        f"TRIPBOX_LOGS_DIR: {env / 'logs'} is not a folder — {fix}",
        f"TRIPBOX_CONFIG_DIR: {env / 'logs'} is not a folder — {fix}",
        f"TRIPBOX_BACKUPS_DIR: {env / 'backups'} is not a folder — {fix}",
    ]


def test_a_folder_that_is_missing_but_can_be_made_is_no_problem_and_is_not_made(
    env, monkeypatch, capsys
):
    for name in ("data", "archive", "logs", "backups"):
        (env / name).rmdir()
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(env / "new" / "data"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(env / "config"))
    code, out = doctor(capsys)
    assert code == 0 and "problems: 0" in out
    assert f"{'files':<15}{'ok':<9}uid {UID}, gid {GID}" in out
    assert sorted(p.name for p in env.iterdir()) == []
    # config and backups are never a problem when absent; the others when nothing can make them
    if os.geteuid() != 0:
        locked = env / "locked"
        locked.mkdir(mode=0o555)
        for name in ("data", "archive", "logs", "config", "backups"):
            monkeypatch.setenv(f"TRIPBOX_{name.upper()}_DIR", str(locked / name))
        fix = "make the folder, or mount one there"
        assert found() == [
            f"TRIPBOX_{name}_DIR: {locked / name.lower()} does not exist and cannot be made here"
            f" — {fix}"
            for name in ("DATA", "ARCHIVE", "LOGS")
        ]
        # the image's own folder, named as compose mounts it
        monkeypatch.delenv("TRIPBOX_ARCHIVE_DIR")
        if not os.access("/app", os.W_OK) and not os.path.lexists("/app/archive"):
            assert found(("archive",)) == [
                f"TRIPBOX_ARCHIVE_DIR: ./archive does not exist and cannot be made here — {fix}"
            ]
        locked.chmod(0o755)
        assert sorted(p.name for p in locked.iterdir()) == []


def test_the_probe_leaves_nothing_behind(env, monkeypatch, capsys):
    (env / "data" / "ledger.db").write_bytes(b"")
    before = {p: sorted(os.listdir(p)) for p in env.iterdir()}
    code, out = doctor(capsys)
    assert code == 0
    folders = "data, archive, logs, config, backups"
    assert f"{'files':<15}{'ok':<9}uid {UID}, gid {GID} · {folders}" in out
    assert {p: sorted(os.listdir(p)) for p in env.iterdir()} == before
