#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Builds tripbox.tar.gz, the install folder a user unpacks: `tripbox/` holding compose.yaml,
.env.example, config/travellers.example.yaml and the empty folders the services write to.

Usage: python3 tools/maintainer/install_bundle.py --out FILE

The archive is the same bytes whenever the three files are: every member is owned by 1000:1000
with no user or group name, dated 0, in a fixed order with fixed modes (the folders a user's
services write to 700, the rest readable), and the gzip header carries no time and no file
name. Standard library only. It prints `bundle: wrote FILE (6 folders, 3 files)`.
"""

from __future__ import annotations

import argparse
import gzip
import io
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TOP = "tripbox"
OWNER = 1000
# (name in the archive, the file it holds relative to the repository, or None for a folder,
# mode), in the order they are written
LAYOUT: tuple[tuple[str, str | None, int], ...] = (
    (TOP, None, 0o755),
    (f"{TOP}/compose.yaml", "compose.yaml", 0o644),
    (f"{TOP}/.env.example", ".env.example", 0o644),
    (f"{TOP}/config", None, 0o755),
    (f"{TOP}/config/travellers.example.yaml", "config/travellers.example.yaml", 0o644),
    (f"{TOP}/data", None, 0o700),
    (f"{TOP}/archive", None, 0o700),
    (f"{TOP}/logs", None, 0o700),
    (f"{TOP}/backups", None, 0o700),
)


def _info(name: str, mode: int, size: int, folder: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if folder else tarfile.REGTYPE
    info.mode = mode
    info.size = size
    info.uid = info.gid = OWNER
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def build(root: Path, out: Path) -> tuple[int, int]:
    """Writes the archive of `root`'s files to `out`; returns how many folders and files."""
    tar_bytes = io.BytesIO()
    folders = files = 0
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, source, mode in LAYOUT:
            if source is None:
                tar.addfile(_info(name, mode, 0, folder=True))
                folders += 1
                continue
            data = (root / source).read_bytes()
            tar.addfile(_info(name, mode, len(data), folder=False), io.BytesIO(data))
            files += 1
    with open(out, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, compresslevel=9, mtime=0) as gz:
            gz.write(tar_bytes.getvalue())
    return folders, files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the install folder's archive.")
    parser.add_argument("--out", required=True, help="the archive to write")
    args = parser.parse_args(argv)
    try:
        folders, files = build(ROOT, Path(args.out))
    except OSError as exc:
        print(f"bundle: cannot write {args.out}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    print(f"bundle: wrote {args.out} ({folders} folders, {files} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
