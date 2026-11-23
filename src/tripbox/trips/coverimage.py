# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A cover photo made from an uploaded or fetched picture: decoded, turned upright, flattened onto
white and written again as two JPEGs carrying nothing but their pixels. Only the worker imports
this module; the page's container never decodes an image."""

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps

from tripbox.trips.coversniff import sniff

MAX_PIXELS = 40_000_000
LARGE_EDGE = 1600
SMALL_EDGE = 480
QUALITY = 82
FORMATS = ["JPEG", "PNG", "WEBP"]


class CoverImageError(Exception):
    """`code` is `unreadable`, `too_many_pixels` or `wrong_type`."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Processed:
    large: bytes
    small: bytes
    digest: str  # sha256 hex of `large`


def _jpeg(img: Image.Image, edge: int) -> bytes:
    copy = img.copy()
    copy.info = {}  # a JPEG save writes the source's comment back out
    copy.thumbnail((edge, edge), Image.Resampling.LANCZOS)  # never enlarges
    out = io.BytesIO()
    copy.save(out, format="JPEG", quality=QUALITY, optimize=True, progressive=True)
    return out.getvalue()


def process(data: bytes) -> Processed:
    """The large and small JPEG of `data`, or a `CoverImageError`."""
    old = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                img = Image.open(io.BytesIO(data), formats=FORMATS)
            except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise CoverImageError("too_many_pixels") from None
            except (Image.UnidentifiedImageError, OSError, ValueError, SyntaxError):
                # a picture of a kind the cover does not take (or no picture) is the wrong type;
                # one that claims a kind it takes and cannot be read is unreadable
                raise CoverImageError(
                    "unreadable" if sniff(data) in ("jpeg", "png", "webp") else "wrong_type"
                ) from None
            try:
                with img:
                    if img.width * img.height > MAX_PIXELS:
                        raise CoverImageError("too_many_pixels")
                    img.load()
                    upright = ImageOps.exif_transpose(img)
                    if upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info:
                        rgba = upright.convert("RGBA")
                        flat = Image.new("RGB", rgba.size, (255, 255, 255))
                        flat.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        flat = upright.convert("RGB")
                    large = _jpeg(flat, LARGE_EDGE)
                    small = _jpeg(flat, SMALL_EDGE)
            except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise CoverImageError("too_many_pixels") from None
            except CoverImageError:
                raise
            except Exception:
                raise CoverImageError("unreadable") from None
    finally:
        Image.MAX_IMAGE_PIXELS = old
    return Processed(large, small, hashlib.sha256(large).hexdigest())
