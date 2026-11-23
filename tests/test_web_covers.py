# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A trip's cover photo on the page: served from the ledger behind the login to someone who can
see its trip, kept a year by the browser and never refused for a year; an owner's photo taken by
its bytes alone and left for the worker; removed back to the tile; shown on the cards and the
trip page as decoration, with a Wikimedia photo's credit; never on a shared page or in Recently
deleted."""

import hashlib

from helpers.web import client_for
from test_web_login import as_cookie, fresh, get, session_of, sign_in
from test_web_login import page as password_page
from test_web_views import POLICY, seed_past, seed_trip

from tripbox.trips.covers import CoverStore

# What a cover's 200 carries, and every other answer under /covers/
KEPT = "private, max-age=31536000, immutable"
# Bytes that stand for the two sizes the worker makes: the page never decodes a picture, so
# they need be nothing but bytes
LARGE = b"\xff\xd8\xff\xe0" + b"large picture " * 8
SMALL = b"\xff\xd8\xff\xe0" + b"small picture " * 4
CREDIT = {
    "artist": "Sample Author",
    "licence": "CC BY-SA 4.0",
    "licence_url": "https://licences.example.org/by-sa/4.0",
    "source_url": "https://commons.example.org/wiki/File:Philadelphia.jpg",
}


def ready(ctx, trip_id: int, large: bytes = LARGE, small: bytes = SMALL) -> str:
    """An owner's photo the worker has made into a cover; its digest."""
    digest = hashlib.sha256(large).hexdigest()
    cv = CoverStore(ctx.ledger)
    cv.upload(trip_id, large)
    cv.finish_upload(trip_id, large, small, digest)
    return digest


def found(ctx, trip_id: int, credit=None) -> str:
    """A Wikimedia photo of the trip's place, with its credit; its digest."""
    digest = hashlib.sha256(LARGE).hexdigest()
    place = ctx.trips.get(trip_id).place
    CoverStore(ctx.ledger).found(trip_id, place, LARGE, SMALL, digest, credit or CREDIT)
    return digest


# ----------------------------------------------------------------------------- serving
def test_a_ready_cover_is_served_as_a_jpeg_the_browser_keeps(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    digest = ready(ctx, trip.id)
    for name, body in ((f"{digest}.jpg", LARGE), (f"{digest}-s.jpg", SMALL)):
        r = c.get(f"/covers/{name}")
        assert (r.status_code, r.content) == (200, body), name
        assert r.headers["content-type"] == "image/jpeg", name
        assert r.headers.get_list("x-content-type-options") == ["nosniff"], name
        assert r.headers.get_list("cache-control") == [KEPT], name
        assert r.headers["content-security-policy"] == POLICY, name
        assert c.head(c.url(f"/covers/{name}")).status_code == 200, name


def test_a_photo_is_served_only_to_someone_signed_in(tmp_path, monkeypatch, base):
    """Signed out, a photo's address is the login's challenge (a page load is sent to sign in),
    never the picture, and never kept: once signed in, the same address is the photo."""
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    path = f"/covers/{ready(ctx, trip.id)}.jpg"
    page_load = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}
    for headers in ({}, {"Accept": "image/avif,image/webp,*/*;q=0.8"}, page_load):
        r = fresh(c).get(path, headers=headers, follow_redirects=False)
        assert r.status_code in (401, 303) and LARGE not in r.content, headers
        assert r.headers.get_list("cache-control") == ["no-store"], headers
        if r.status_code == 303:
            assert r.headers["location"].startswith(c.url("/login")), headers
    session = as_cookie("tripbox_session", session_of(sign_in(c)))
    r = get(c, path, session)
    assert (r.status_code, r.content) == (200, LARGE)
    assert r.headers.get_list("cache-control") == [KEPT]


def test_a_name_that_is_no_photo_shown_is_a_404_never_kept(tmp_path, monkeypatch, base):
    """A name of any other shape, a digest no cover holds, a photo of a deleted trip and one the
    owner removed are each a 404 the browser does not keep; restored, the trip's photo is served
    again, and a photo two trips hold is served while either is not deleted."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    digest = ready(ctx, trip.id)
    st = ctx.trips

    def answer(name: str) -> int:
        r = c.get(f"/covers/{name}")
        if r.status_code != 200:
            assert LARGE not in r.content and SMALL not in r.content, name
            assert r.headers.get_list("cache-control") == ["no-store"], name
        return r.status_code

    for name in (
        "0" * 64 + ".jpg",
        digest.upper() + ".jpg",
        digest + ".png",
        digest + ".JPG",
        digest + "-m.jpg",
        digest + "-s-s.jpg",
        digest[:-1] + ".jpg",
        digest + "0.jpg",
        digest + ".jpg.jpg",
        digest,
        "upload",
        "x.jpg",
        f"{digest}.jpg/",
    ):
        assert answer(name) == 404, name
    st.delete_trip(trip.id)
    assert answer(f"{digest}.jpg") == answer(f"{digest}-s.jpg") == 404
    st.restore_trip(trip.id)
    assert answer(f"{digest}.jpg") == 200
    # the same picture on another trip (a same-place copy): served while either trip is shown
    seed_past(ctx, 1)
    other = next(t for t in st.all_trips() if t.id != trip.id)
    assert ready(ctx, other.id) == digest
    st.delete_trip(trip.id)
    assert answer(f"{digest}.jpg") == 200
    st.delete_trip(other.id)
    assert answer(f"{digest}.jpg") == 404
    st.restore_trip(other.id)
    CoverStore(ctx.ledger).remove(other.id)
    assert answer(f"{digest}-s.jpg") == 404
