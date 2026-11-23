# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What kind of picture some bytes are, from their first bytes alone.

Nothing here decodes: the page's container may import this module, and only the worker imports an
image library."""

from typing import Literal

Kind = Literal["jpeg", "png", "webp", "heic"]

_HEIC_BRANDS = (b"heic", b"mif1", b"heix")


def sniff(data: bytes) -> Kind | None:
    """`jpeg`, `png`, `webp` or `heic` by magic bytes, else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "heic"
    return None
