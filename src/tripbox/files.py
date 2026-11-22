# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The install's folders, as the process that runs in them sees them: whether each is there, is a
folder, and can be written (or, the config folder, read). `doctor` lists what is wrong with them,
and `web`, `work` and `bot` refuse to start on a folder they need that is wrong, each problem in
the same words: what is wrong and the fix, by who owns the folder.

A folder is checked by doing what the process will do: an empty file is made in it and removed,
and the error that refuses it says what is wrong (a read-only mount, a full disk, or this user's
permissions). A folder that is absent is never made here (whatever makes it on first use does)."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

from tripbox.config import KEY_BY_NAME, ConfigProblem, Settings

# The folders, in the order they are checked, each with its setting
NAMES = ("data", "archive", "logs", "config", "backups")
KEYS = {name: f"TRIPBOX_{name.upper()}_DIR" for name in NAMES}
# The folders the process writes; the config folder is only read
WRITTEN = ("data", "archive", "logs", "backups")
# Absent, these are no problem whether or not they can be made: an install may have none
OPTIONAL = ("config", "backups")
# The ledger's own files in the data folder, each read and written when it is there
LEDGER_FILES = ("ledger.db", "ledger.db-wal", "ledger.db-shm")
PROBE = ".tripbox-probe-"  # the empty file a check makes and removes

MISSING = "{path} does not exist and cannot be made here"
MISSING_FIX = "make the folder, or mount one there"
NOT_A_FOLDER = "{path} is not a folder"
NOT_A_FOLDER_FIX = "move it away and make a folder there"
NOT_WRITABLE = "{path} is not writable by this user ({who})"
ANOTHER_USER_FIX = (
    "set TRIPBOX_UID={o} and TRIPBOX_GID={og} in .env and start again, or on the host run:"
    " sudo chown -R {uid}:{gid} {name}"
)
ROOT_FIX = "on the host, in the install directory, run: sudo chown -R {uid}:{gid} {name}"
THIS_USER_FIX = (
    "on the host run: chmod u+rwx {name} (a read-only mount or an SELinux label also does this)"
)
NOT_READABLE = "{path} cannot be read by this user ({who})"
NOT_READABLE_FIX = "on the host run: chmod -R a+rX {name}"
FILE_NOT_READABLE_FIX = "on the host run: chmod a+r {name}"
READ_ONLY = "{path} is on a read-only mount"
READ_ONLY_FIX = "mount the folder read-write (in compose.yaml its line must not end :ro)"
NO_ROOM = "{path} has no room left"
NO_ROOM_FIX = "free some space on the disk it is on"
# What the probe's error says of the folder, when it says more than permissions
NO_ROOM_ERRORS = (errno.ENOSPC, errno.EDQUOT)


def _owner(path: Path) -> tuple[int, int, int]:
    """Who owns `path`: its uid, its gid and its mode's permission bits."""
    st = os.stat(path)
    return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)


def _shown(key: str, path: Path) -> tuple[str, str]:
    """How a folder is named to the owner: (`path`, `name`). At the image's own default it is the
    folder compose mounts there, `./data` and `data` (the path inside the container is none the
    owner can see); set to another path, that path, both times."""
    if path == Path(KEY_BY_NAME[key].default):
        return f"./{path.name}", path.name
    return str(path), str(path)


def shown(name: str, path: Path) -> str:
    """The folder `name` (of NAMES) at `path` as the owner is told it: `./data` at the image's
    own place, else the path."""
    return _shown(KEYS[name], path)[0]


def mounted(settings: Settings, path: Path | str) -> str:
    """A path of this install as the owner names it on the host, in the install directory:
    under one of the folders at the image's own place, `./<folder>/<the rest>` (that folder is
    what compose mounts there); else the path as given."""
    absolute = Path(os.path.abspath(path))
    for name in NAMES:
        default = Path(KEY_BY_NAME[KEYS[name]].default)
        if Path(getattr(settings, f"{name}_dir")) != default:
            continue
        if absolute == default or absolute.is_relative_to(default):
            return "/".join((".", name, *absolute.relative_to(default).parts))
    return str(path)


def _who(path: Path) -> tuple[str, int, int]:
    """The `who` of a sentence (this process's ids, then the owner's and the mode), and the
    owner's uid and gid."""
    o, og, mode = _owner(path)
    who = f"uid {os.getuid()}, gid {os.getgid()}; it belongs to uid {o}, gid {og}, mode {mode:o}"
    return who, o, og


def another_owner(path: Path) -> tuple[str, int, int] | None:
    """For a file another user owns: who this process is and who owns it (the `who` of a
    sentence) and this process's uid and gid, the owner a fix names; None for a file this user
    owns or that is not there."""
    try:
        owner, _, _ = _owner(path)
    except OSError:
        return None
    if owner == os.getuid():
        return None
    return _who(path)[0], os.getuid(), os.getgid()


def _write_fix(o: int, og: int, name: str) -> str:
    """The fix for what this user cannot write, by who owns it."""
    uid, gid = os.getuid(), os.getgid()
    if o == uid:
        return THIS_USER_FIX.format(name=name)
    if o == 0:
        return ROOT_FIX.format(uid=uid, gid=gid, name=name)
    return ANOTHER_USER_FIX.format(o=o, og=og, uid=uid, gid=gid, name=name)


def _probe(folder: Path) -> int | None:
    """None when an empty file can be made in `folder` (it is removed at once); else the error
    number that refused it (0 for one that gives none)."""
    probe = folder / f"{PROBE}{secrets.token_hex(8)}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return exc.errno or 0
    os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return None


def _unreadable(folder: Path) -> list[str] | None:
    """None when `folder` cannot be listed; else the names of the files in it this user cannot
    read, sorted (none when each can be)."""
    if not os.access(folder, os.R_OK | os.X_OK):
        return None
    try:
        with os.scandir(folder) as entries:
            return sorted(e.name for e in entries if e.is_file() and not os.access(e.path, os.R_OK))
    except OSError:
        return None


def _can_be_made(path: Path) -> bool:
    """Whether the nearest folder above `path` that exists can be written."""
    parent = path.parent
    while not os.path.lexists(parent) and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def _folder(name: str, path: Path) -> list[tuple[str, str]]:
    """What is wrong with one folder, each as (what, fix)."""
    shown, label = _shown(KEYS[name], path)
    if not os.path.lexists(path):
        if name in OPTIONAL or _can_be_made(path):
            return []
        return [(MISSING.format(path=shown), MISSING_FIX)]
    if not path.is_dir():
        return [(NOT_A_FOLDER.format(path=shown), NOT_A_FOLDER_FIX)]
    if name == "config":
        stray = _unreadable(path)
        if stray is None:
            who, _, _ = _who(path)
            return [
                (NOT_READABLE.format(path=shown, who=who), NOT_READABLE_FIX.format(name=label))
            ]
        wrong = []
        for file in stray:
            who, _, _ = _who(path / file)  # the file's own owner, not the folder's
            what = NOT_READABLE.format(path=f"{shown}/{file}", who=who)
            wrong.append((what, FILE_NOT_READABLE_FIX.format(name=f"{label}/{file}")))
        return wrong
    refused = _probe(path)
    if refused == errno.EROFS:
        return [(READ_ONLY.format(path=shown), READ_ONLY_FIX)]
    if refused in NO_ROOM_ERRORS:
        return [(NO_ROOM.format(path=shown), NO_ROOM_FIX)]
    if refused is not None:
        who, o, og = _who(path)
        return [(NOT_WRITABLE.format(path=shown, who=who), _write_fix(o, og, label))]
    wrong = []
    if name == "data":
        for file in LEDGER_FILES:
            at = path / file
            if os.path.lexists(at) and not os.access(at, os.R_OK | os.W_OK):
                who, o, og = _who(at)
                what = NOT_WRITABLE.format(path=f"{shown}/{file}", who=who)
                wrong.append((what, _write_fix(o, og, f"{label}/{file}")))
    return wrong


def folder_problems(name: str, path: Path) -> list[tuple[str, str]]:
    """What is wrong with the folder `name` (of NAMES) at `path`, each as (what, fix), in the
    words `doctor` says them in."""
    return _folder(name, path)


def problems(settings: Settings, names: tuple[str, ...] | list[str]) -> list[ConfigProblem]:
    """What is wrong with the folders `names` (of NAMES, checked in NAMES' order), each a problem
    of the feature `files` under the folder's setting."""
    found = []
    for name in NAMES:
        if name in names:
            path = getattr(settings, f"{name}_dir")
            for what, fix in _folder(name, path):
                found.append(ConfigProblem(KEYS[name], what, fix, "files"))
    return found


def row(settings: Settings, found: list[ConfigProblem] | None = None) -> tuple[str, str]:
    """`doctor`'s row: `ok`, who this process is and the folders that are there; or `invalid`.
    `found` is what `problems` found of every folder, when the caller has it."""
    if found is None:
        found = problems(settings, NAMES)
    if found:
        return "invalid", "see the problems below"
    there = [n for n in NAMES if getattr(settings, f"{n}_dir").is_dir()]
    ids = f"uid {os.getuid()}, gid {os.getgid()}"
    return "ok", f"{ids} · {', '.join(there)}" if there else ids
