# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import pytest

from tripbox.trips.coversniff import sniff


@pytest.mark.parametrize(
    "head, kind",
    [
        (b"\xff\xd8\xff\xe0" + b"\0" * 8, "jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, "png"),
        (b"RIFF\0\0\0\0WEBPVP8 ", "webp"),
        (b"\0\0\0\x18ftypheic\0\0\0\0", "heic"),
        (b"\0\0\0\x18ftypmif1\0\0\0\0", "heic"),
        (b"\0\0\0\x18ftypheix\0\0\0\0", "heic"),
        (b"GIF89a" + b"\0" * 8, None),
        (b"BM" + b"\0" * 12, None),
        (b"RIFF\0\0\0\0WAVEfmt ", None),
        (b"\0\0\0\x18ftypmp42\0\0\0\0", None),
        (b"", None),
        (b"\xff\xd8", None),
    ],
)
def test_sniff_goes_by_magic_bytes(head, kind):
    assert sniff(head) == kind
