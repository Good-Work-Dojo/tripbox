# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The licence, the notice and the third-party texts the image carries beside the app, and what
the notices name. Reads the files at the root of the tree (the image's /app)."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# the GNU Affero General Public License, version 3, as the FSF publishes it
AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"


def test_the_image_carries_the_licence_the_notice_and_the_third_party_texts():
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        assert (ROOT / name).read_text(encoding="utf-8").strip(), name
    texts = {p.name for p in (ROOT / "LICENSES").iterdir()}
    assert {"GPL-3.0-only.txt", "OFL-1.1.txt"} <= texts
    assert all((ROOT / "LICENSES" / t).stat().st_size > 1000 for t in texts)


def test_the_notices_name_the_extractor_the_font_the_airport_table_and_airtrail():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for named in ("kitinerary-extractor", "Public Sans", "airportsdata", "AirTrail"):
        assert f"## {named}" in text, named
    # each says what its licence is, and the texts it points at are there
    for licence in ("LGPL-2.0-or-later", "OFL-1.1", "GPL-3.0-only", "MIT"):
        assert licence in text, licence
    assert "26.04.3" in text and "24.12.3" in text
    assert "<!-- licences:begin -->" in text and "<!-- licences:end -->" in text
    for kept in ("LGPL-2.0-or-later.txt", "ODbL-1.0.txt", "OFL-1.1.txt", "GPL-3.0-only.txt"):
        assert f"LICENSES/{kept}" in text, kept
        assert (ROOT / "LICENSES" / kept).is_file(), kept


def test_the_licence_is_the_affero_text_version_3():
    raw = (ROOT / "LICENSE").read_bytes()
    first = raw.decode("utf-8").splitlines()[:2]
    assert [line.strip() for line in first] == [
        "GNU AFFERO GENERAL PUBLIC LICENSE",
        "Version 3, 19 November 2007",
    ]
    assert hashlib.sha256(raw).hexdigest() == AGPL_SHA256
