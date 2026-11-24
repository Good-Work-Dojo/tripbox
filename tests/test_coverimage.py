# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import hashlib
import io
import struct
import zlib

import pytest
from PIL import Image

from tripbox.trips.coverimage import CoverImageError, process


def encode(img, fmt, **kw):
    out = io.BytesIO()
    img.save(out, format=fmt, **kw)
    return out.getvalue()


def photo(size=(300, 200)):
    img = Image.new("RGB", size)
    img.putdata([(x % 256, 80, 200) for x in range(size[0] * size[1])])
    return img


# What a phone writes in a JPEG's XMP packet (an APP1 segment of its own): its position again,
# and its maker
XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF'
    b' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description'
    b' xmlns:exif="http://ns.adobe.com/exif/1.0/" xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
    b' exif:GPSLatitude="30,15.0N" exif:GPSLongitude="97,44.0W"'
    b' xmp:CreatorTool="Example Phone XMP"/></rdf:RDF></x:xmpmeta>'
)
XMP_HEAD = b"http://ns.adobe.com/xap/1.0/\0"  # how an APP1 segment says it holds XMP


def phone_jpeg():
    """A phone's photo: turned by its EXIF orientation (6), with a GPS position and a maker in
    its EXIF and again in its XMP, a colour profile and a comment."""
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "Example Phone"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (30.0, 15.0, 0.0)
    return encode(
        photo((300, 200)), "JPEG", exif=exif, icc_profile=b"x" * 200, comment=b"hello", xmp=XMP
    )


def test_the_phone_photo_carries_what_must_go():
    """What the next test proves gone is there to begin with."""
    raw = phone_jpeg()
    img = Image.open(io.BytesIO(raw))
    assert img.getexif()[0x0112] == 6 and img.getexif().get_ifd(0x8825)[1] == "N"
    assert b"\xff\xe1" + struct.pack(">H", 2 + len(XMP_HEAD) + len(XMP)) + XMP_HEAD + XMP in raw
    assert img.info["xmp"] == XMP and b"GPSLatitude" in raw
    assert {"icc_profile", "comment", "exif"} <= set(img.info)


def test_a_phone_photo_comes_out_upright_with_nothing_but_pixels():
    done = process(phone_jpeg())
    for blob in (done.large, done.small):
        img = Image.open(io.BytesIO(blob))
        assert img.format == "JPEG" and img.size[1] > img.size[0]
        assert len(img.getexif()) == 0
        assert "icc_profile" not in img.info and "comment" not in img.info
        assert "xmp" not in img.info and "exif" not in img.info
        assert b"Example Phone" not in blob and b"hello" not in blob
        assert XMP_HEAD not in blob and b"GPSLatitude" not in blob and b"Exif\0\0" not in blob
    assert Image.open(io.BytesIO(done.large)).size == (200, 300)
    assert done.digest == hashlib.sha256(done.large).hexdigest()


def test_the_edges_are_capped_and_a_small_picture_is_never_enlarged():
    big = process(encode(photo((3000, 2000)), "JPEG"))
    assert Image.open(io.BytesIO(big.large)).size == (1600, 1067)
    assert max(Image.open(io.BytesIO(big.small)).size) == 480
    tiny = process(encode(photo((120, 80)), "PNG"))
    assert Image.open(io.BytesIO(tiny.large)).size == (120, 80)
    assert Image.open(io.BytesIO(tiny.small)).size == (120, 80)


def test_transparency_is_flattened_onto_white():
    clear = Image.new("RGBA", (40, 40), (255, 0, 0, 0))
    out = Image.open(io.BytesIO(process(encode(clear, "PNG")).large))
    assert out.mode == "RGB"
    assert all(c >= 250 for c in out.getpixel((20, 20)))


def test_webp_is_accepted():
    out = Image.open(io.BytesIO(process(encode(photo(), "WEBP")).large))
    assert out.format == "JPEG"


def png_claiming(width, height):
    def chunk(kind, body):
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    head = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(b"\0" * 10))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("size", [(9000, 5000), (30000, 30000)])
def test_too_many_pixels_is_refused_before_decoding(size):
    with pytest.raises(CoverImageError) as e:
        process(png_claiming(*size))
    assert e.value.code == "too_many_pixels"


def test_the_limit_is_restored_after_a_call():
    before = Image.MAX_IMAGE_PIXELS
    with pytest.raises(CoverImageError):
        process(png_claiming(30000, 30000))
    assert Image.MAX_IMAGE_PIXELS == before


def test_truncated_and_garbage_bytes_are_unreadable():
    good = encode(photo((300, 200)), "JPEG")
    for data in (good[: len(good) // 2], good[:20], b"\xff\xd8\xff" + b"\0" * 30):
        with pytest.raises(CoverImageError) as e:
            process(data)
        assert e.value.code == "unreadable"


@pytest.mark.parametrize("fmt", ["GIF", "BMP"])
def test_other_formats_are_the_wrong_type(fmt):
    with pytest.raises(CoverImageError) as e:
        process(encode(photo((20, 20)).convert("P"), fmt))
    assert e.value.code == "wrong_type"
