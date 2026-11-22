# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Raw .eml archive: archive/YYYY/MM/<receivedUTC>_<uid>_<sha8>.eml; atomic, never rewritten."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Archived:
    path: Path
    sha256: str
    existed: bool


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def archive_path(root: Path, received: dt.datetime, uid: int | str | None, sha256: str) -> Path:
    received_utc = received.astimezone(dt.UTC)
    stamp = received_utc.strftime("%Y%m%dT%H%M%SZ")
    tag = str(uid) if uid is not None else "x"
    return (
        root
        / received_utc.strftime("%Y")
        / received_utc.strftime("%m")
        / f"{stamp}_{tag}_{sha256[:8]}.eml"
    )


def archive(
    root: Path, raw: bytes, received: dt.datetime, uid: int | str | None = None
) -> Archived:
    """Write raw bytes once; an identical file (same sha) is left alone (existed=True)."""
    sha = sha256_hex(raw)
    path = archive_path(root, received, uid, sha)
    if path.exists():
        return Archived(path=path, sha256=sha, existed=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".eml.tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return Archived(path=path, sha256=sha, existed=False)


def iter_archive(root: Path, since_month: str | None = None) -> list[Path]:
    """All archived .eml files in chronological (path) order, optionally from YYYY-MM onwards."""
    files = sorted(root.glob("*/*/*.eml"))
    if since_month:
        y, m = since_month.split("-")
        files = [p for p in files if (p.parent.parent.name, p.parent.name) >= (y, m)]
    return files
