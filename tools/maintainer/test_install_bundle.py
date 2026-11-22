# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tools/maintainer/install_bundle.py: the install folder a user unpacks, as one archive whose
bytes depend on nothing but the three files it carries."""

import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BUILDER = HERE / "install_bundle.py"
sys.path.insert(0, str(HERE))
import install_bundle  # noqa: E402

FILES = {
    "tripbox/compose.yaml": "compose.yaml",
    "tripbox/.env.example": ".env.example",
    "tripbox/config/travellers.example.yaml": "config/travellers.example.yaml",
}
# name, type, mode, in this order
MEMBERS = [
    ("tripbox", "dir", 0o755),
    ("tripbox/compose.yaml", "file", 0o644),
    ("tripbox/.env.example", "file", 0o644),
    ("tripbox/config", "dir", 0o755),
    ("tripbox/config/travellers.example.yaml", "file", 0o644),
    ("tripbox/data", "dir", 0o700),
    ("tripbox/archive", "dir", 0o700),
    ("tripbox/logs", "dir", 0o700),
    ("tripbox/backups", "dir", 0o700),
]


def build(out: Path) -> subprocess.CompletedProcess:
    """The builder as a maintainer runs it, from the repository's own files."""
    return subprocess.run(
        [sys.executable, "-I", str(BUILDER), "--out", str(out)],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def members(path: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(path, "r:gz") as tar:
        return tar.getmembers()


def test_two_builds_are_the_same_bytes(tmp_path):
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    r = build(first)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"bundle: wrote {first} (6 folders, 3 files)\n"
    assert build(second).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    # a copy of the sources with other times, modes and a looser umask makes the same bytes
    copy = tmp_path / "copy"
    for rel in FILES.values():
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, copy / rel)
        os.chmod(copy / rel, 0o666)
        os.utime(copy / rel, (1_800_000_000, 1_800_000_000))
    third = tmp_path / "three.tar.gz"
    old = os.umask(0)
    try:
        install_bundle.build(copy, third)
    finally:
        os.umask(old)
    assert third.read_bytes() == first.read_bytes()
    # the gzip header holds no time and no file name
    raw = first.read_bytes()
    assert raw[:3] == b"\x1f\x8b\x08" and raw[3] == 0 and raw[4:8] == b"\0\0\0\0"


def test_the_bundle_holds_the_install_directory_and_nothing_else(tmp_path):
    out = tmp_path / "tripbox.tar.gz"
    assert build(out).returncode == 0
    found = members(out)
    assert [(m.name, "dir" if m.isdir() else "file" if m.isfile() else "other", m.mode)
            for m in found] == MEMBERS
    for m in found:
        assert (m.uid, m.gid, m.uname, m.gname, m.mtime) == (1000, 1000, "", "", 0), m.name
        assert not m.pax_headers, m.name
    with tarfile.open(out, "r:gz") as tar:
        for name, rel in FILES.items():
            assert tar.extractfile(name).read() == (REPO / rel).read_bytes(), name
    # the example settings hold no value but empty ones and examples: the one live line is the
    # password a user must choose, and it is empty
    with tarfile.open(out, "r:gz") as tar:
        example = tar.extractfile("tripbox/.env.example").read().decode()
    live = [line for line in example.splitlines() if line and not line.startswith("#")]
    assert live == ["TRIPBOX_PASSWORD="]
    # nothing outside the folder, no link, no device
    with gzip.open(out) as fh:
        data = fh.read()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        assert all(m.name == "tripbox" or m.name.startswith("tripbox/") for m in tar)
        assert all(m.isdir() or m.isfile() for m in tar)


def test_every_folder_the_compose_file_mounts_is_in_the_bundle(tmp_path):
    out = tmp_path / "tripbox.tar.gz"
    assert build(out).returncode == 0
    folders = {m.name.removeprefix("tripbox/") for m in members(out) if m.isdir()}
    services = yaml.safe_load((REPO / "compose.yaml").read_text())["services"]
    mounted = {
        vol["source"].removeprefix("./")
        for svc in services.values()
        for vol in svc["volumes"]
    }  # fmt: skip
    assert mounted == folders - {"tripbox"}
    # what the user writes is the user's alone; what the image reads is readable
    modes = {m.name.removeprefix("tripbox/"): m.mode for m in members(out) if m.isdir()}
    assert {f for f in mounted if modes[f] == 0o700} == {"data", "archive", "logs", "backups"}
    assert modes["config"] == 0o755
