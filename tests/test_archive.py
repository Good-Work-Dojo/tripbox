# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt

from tripbox.mail.archive import archive, archive_path, iter_archive, sha256_hex


def test_archive_path_uses_utc_month_folders(tmp_path):
    received = dt.datetime(2031, 3, 31, 22, 30, 8, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    p = archive_path(tmp_path, received, 412, "abcdef0123456789")
    assert p == tmp_path / "2031" / "04" / "20310401T033008Z_412_abcdef01.eml"


def test_archive_writes_once_and_atomically(tmp_path):
    raw = b"Subject: hi\r\n\r\nbody"
    received = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    first = archive(tmp_path, raw, received, uid=7)
    assert first.existed is False and first.path.read_bytes() == raw
    assert first.sha256 == sha256_hex(raw)
    again = archive(tmp_path, raw, received, uid=7)
    assert again.existed is True and again.path == first.path
    assert not list(tmp_path.rglob("*.tmp"))


def test_iter_archive_orders_and_filters(tmp_path):
    for month, uid in (("2026-09", 1), ("2026-10", 2), ("2026-11", 3)):
        y, m = month.split("-")
        archive(
            tmp_path, f"uid {uid}".encode(), dt.datetime(int(y), int(m), 5, tzinfo=dt.UTC), uid=uid
        )
    assert len(iter_archive(tmp_path)) == 3
    assert [p.name.split("_")[1] for p in iter_archive(tmp_path, since_month="2026-10")] == [
        "2",
        "3",
    ]
