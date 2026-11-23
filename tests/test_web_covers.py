# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A trip's cover photo on the page: served from the ledger behind the login to someone who can
see its trip, kept a year by the browser and never refused for a year; an owner's photo taken by
its bytes alone and left for the worker; removed back to the tile; shown on the cards and the
trip page as decoration, with a Wikimedia photo's credit; never on a shared page or in Recently
deleted."""

import asyncio
import datetime as dt
import hashlib
import io
import re
import signal
from html import unescape

import pytest
from helpers.web import PEER, client_for
from PIL import Image
from test_coverimage import phone_jpeg
from test_web_login import as_cookie, fresh, get, session_of, sign_in
from test_web_login import page as password_page
from test_web_uploads import endless
from test_web_views import POLICY, Node, Tree, seed_past, seed_trip
from test_worker import a_worker, beat, start

from tripbox import uploads, util
from tripbox.ledger import repo
from tripbox.trips import shares
from tripbox.trips.covers import CoverStore
from tripbox.web import views
from tripbox.web.middleware import LIMIT

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


# ----------------------------------------------------------------------------- uploading
SAME = {"Sec-Fetch-Site": "same-origin"}
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"pixels " * 16
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"pixels " * 16
WEBP = b"RIFF\x40\x00\x00\x00WEBPVP8 " + b"pixels " * 16
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"pixels " * 16
GIF = b"GIF89a\x01\x00\x01\x00" + b"pixels " * 16
BOUNDARY = "sample-boundary"


def send(c, trip_id, body: bytes, name: str = "photo.jpg", declared: str = "image/jpeg", **kw):
    """The upload form posted from the trip's own page with one file in its `photo` field."""
    files = {"photo": (name, body, declared)}
    query = "" if trip_id is None else f"?trip={trip_id}"
    headers = {**SAME, **kw.pop("headers", {})}
    return c.post(f"/covers/upload{query}", files=files, headers=headers, follow_redirects=False)


def row(ctx, trip_id: int):
    return ctx.ledger.covers.get(trip_id)


def flashed(c, r) -> str:
    """The toast the page a redirect goes to shows."""
    page = c.get(c.local(r.headers["location"])).text
    return re.search(r'data-flash="([^"]*)"', page)[1]


def post_as_it_arrives(c, query: str, chunks, *, length: str | None = None):
    """POST the chunks to the upload form's address (with `query`) one receive at a time, with
    no declared length unless `length`; (status, the response's headers, how many body bytes
    the app took)."""
    taken = 0
    source = iter(chunks)
    sent: list[dict] = []

    async def receive():
        nonlocal taken
        chunk = next(source, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        taken += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def collect(message):
        sent.append(message)

    headers = [
        (b"host", b"localhost:8110"),
        (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
        (b"sec-fetch-site", b"same-origin"),
        (b"content-length", length.encode()) if length else (b"transfer-encoding", b"chunked"),
    ]
    path = c.url("/covers/upload")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": headers,
        "client": PEER,
        "server": ("localhost", 8110),
        "state": {},
    }
    asyncio.run(c.app(scope, receive, collect))
    start = next(m for m in sent if m["type"] == "http.response.start")
    named = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], named, taken


def a_part(head: bytes = b"") -> bytes:
    return (
        f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"p.jpg\""
        "\r\nContent-Type: image/jpeg\r\n\r\n"
    ).encode() + head


def test_a_photo_is_taken_by_its_bytes_and_left_for_the_worker(tmp_path, monkeypatch, base):
    """A JPEG, a PNG and a WebP, whatever they are called and declared as, are each stored as
    they came, waiting for the worker, in the trip's one row (each replacing the last, a ready
    photo too), and the trip's page says the photo was received."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    ready(ctx, trip.id)
    for body, name, declared in (
        (JPEG, "IMG_0001.HEIC", "image/heic"),
        (PNG, "photo.jpg", "application/octet-stream"),
        (WEBP, "../../etc/cover.webp", "text/plain"),
    ):
        r = send(c, trip.id, body, name, declared)
        assert (r.status_code, r.headers["location"]) == (
            303,
            c.url(f"/trips/{trip.id}?f=cover_received"),
        ), name
        held = row(ctx, trip.id)
        assert (held["source"], held["state"], held["upload"]) == ("owner", "pending", body)
        assert (held["digest"], held["large"], held["small"]) == (None, None, None), name
        assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM covers").fetchone()[0] == 1
    assert flashed(c, r) == "Photo received — it shows in a few seconds"


def test_heic_and_what_is_no_photo_are_refused_with_their_toasts(tmp_path, monkeypatch, base):
    """A HEIC photo says so; a GIF, text, an empty file and a form with no file are each no
    JPEG, PNG or WebP. Nothing is stored, and the photo the trip had stays as it was."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 1)
    other = next(t for t in ctx.trips.all_trips() if t.id != trip.id)
    digest = ready(ctx, other.id)
    before = dict(row(ctx, other.id))
    for tid, body, toast in (
        (trip.id, HEIC, "cover_heic"),
        (other.id, HEIC, "cover_heic"),
        (trip.id, GIF, "cover_type"),
        (other.id, b"A photo of the Sample family.", "cover_type"),
        (trip.id, b"", "cover_type"),
    ):
        r = send(c, tid, body, "photo.jpg", "image/jpeg")
        assert (r.status_code, r.headers["location"]) == (
            303,
            c.url(f"/trips/{tid}?f={toast}"),
        ), (tid, body[:12])
    r = c.post(
        f"/covers/upload?trip={trip.id}", data={"note": "no file"}, headers=SAME,
        follow_redirects=False,
    )
    assert r.headers["location"] == c.url(f"/trips/{trip.id}?f=cover_type")
    assert row(ctx, trip.id) is None and dict(row(ctx, other.id)) == before
    assert c.get(f"/covers/{digest}.jpg").status_code == 200
    said = {key: flashed(c, send(c, trip.id, body)) for key, body in (
        ("cover_heic", HEIC), ("cover_type", GIF),
    )}
    assert said == {
        "cover_heic": "That photo is HEIC — upload a JPEG, PNG or WebP",
        "cover_type": "That file is not a JPEG, PNG or WebP photo",
    }


def test_a_photo_over_ten_megabytes_is_refused_and_stores_nothing(tmp_path, monkeypatch, base):
    """Ten megabytes are taken; one byte more is refused with its toast. A body counted past the
    cap as it arrives, and one declaring more, are refused once the cap is reached or before a
    byte is read; past the body limit of every form the answer is the limit's own."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    padded = JPEG + b"x" * (uploads.MAX_BYTES - len(JPEG))
    r = send(c, trip.id, padded + b"x")
    assert r.headers["location"] == c.url(f"/trips/{trip.id}?f=cover_big")
    assert row(ctx, trip.id) is None
    assert flashed(c, r) == "That photo is larger than 10 MB"
    query = f"trip={trip.id}"
    piece = b"x" * 65536
    big = c.url(f"/trips/{trip.id}?f=cover_big")
    for length in (None, str(views.BODY_CAP + 1)):
        status, said, taken = post_as_it_arrives(
            c, query, endless(a_part(JPEG), piece, 2 * views.BODY_CAP), length=length
        )
        assert (status, said["location"]) == (303, big), length
        assert taken <= views.BODY_CAP + len(piece) and (length is None or taken == 0), taken
    status, _, taken = post_as_it_arrives(
        c, query, endless(a_part(JPEG), piece, 2**20), length=str(views.BODY_CAP + LIMIT + 1)
    )
    assert (status, taken) == (413, 0)
    assert row(ctx, trip.id) is None
    r = send(c, trip.id, padded)
    assert r.headers["location"] == c.url(f"/trips/{trip.id}?f=cover_received")
    assert row(ctx, trip.id)["upload"] == padded


def test_an_upload_for_a_trip_the_owner_cannot_see_is_a_404(tmp_path, monkeypatch, base):
    """No trip, one that is no id, one that does not exist and a deleted one: the 404 page,
    answered before the body is read, and nothing is stored."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 1)
    gone = next(t for t in ctx.trips.all_trips() if t.id != trip.id)
    ctx.trips.delete_trip(gone.id)
    for tid in (None, "", "x", "²", "0", "-1", "999", str(2**70), str(gone.id)):
        assert send(c, tid, JPEG).status_code == 404, tid
        if tid is not None:
            status, _, taken = post_as_it_arrives(
                c, f"trip={tid}", endless(a_part(JPEG), b"x" * 65536, 2**20),
                length=str(views.BODY_CAP),
            )
            assert (status, taken) == (404, 0), tid
    assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM covers").fetchone()[0] == 0
    assert send(c, trip.id, JPEG).status_code == 303


def test_an_upload_from_another_site_is_refused(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    for claims in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}):
        r = c.post(
            f"/covers/upload?trip={trip.id}",
            files={"photo": ("p.jpg", JPEG, "image/jpeg")},
            headers=claims,
            follow_redirects=False,
        )
        assert r.status_code == 403, claims
    assert row(ctx, trip.id) is None


def test_a_phone_photo_from_the_page_is_served_upright_with_nothing_but_pixels(
    tmp_path, monkeypatch, base
):
    """A phone's JPEG, turned by its EXIF and carrying a GPS position, a maker, a colour profile
    and a comment, uploaded from the page and read by the worker on one beat: both sizes the
    page serves stand upright and carry none of it."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base, airtrail=False)
    trip = seed_trip(ctx)
    sent = phone_jpeg()
    assert b"Example Phone" in sent
    r = send(c, trip.id, sent, "IMG_0001.JPG")
    assert r.headers["location"] == c.url(f"/trips/{trip.id}?f=cover_received")
    monkeypatch.setattr(signal, "signal", lambda signum, handler: None)
    w, clock, _ = a_worker(ctx, util.now())
    start(w)
    clock.tick(5)
    beat(w)
    cover = CoverStore(ctx.ledger).get(trip.id)
    assert (cover.source, cover.state) == ("owner", "ready")
    for name in (f"{cover.digest}.jpg", f"{cover.digest}-s.jpg"):
        r = c.get(f"/covers/{name}")
        assert r.status_code == 200, name
        img = Image.open(io.BytesIO(r.content))
        assert img.format == "JPEG" and img.size[1] > img.size[0], name
        assert len(img.getexif()) == 0, name
        assert not {"exif", "icc_profile", "comment", "xmp"} & set(img.info), name
        assert b"Example Phone" not in r.content and b"hello" not in r.content, name
    assert row(ctx, trip.id)["upload"] is None


# ----------------------------------------------------------------------------- removing
def test_remove_takes_the_photo_away_and_keeps_automatic_ones_off(tmp_path, monkeypatch, base):
    """Remove photo leaves a `removed` row (no picture, nothing waiting), so the photo is no
    longer served and no automatic one is looked up until the place changes; it comes back to
    the trip with its toast. A trip the owner cannot see is the 404 page, and a post from
    another site is refused; neither changes a row."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    digest = ready(ctx, trip.id)
    seed_past(ctx, 1)
    gone = next(t for t in ctx.trips.all_trips() if t.id != trip.id)
    gone_digest = ready(ctx, gone.id, LARGE + b"!")
    ctx.trips.delete_trip(gone.id)
    url = f"/trips/{trip.id}/cover/remove"
    assert c.post(url, follow_redirects=False).status_code == 403
    assert row(ctx, trip.id)["state"] == "ready"
    for tid in (gone.id, 999):
        r = c.post(f"/trips/{tid}/cover/remove", headers=SAME, follow_redirects=False)
        assert r.status_code == 404, tid
    assert row(ctx, gone.id)["digest"] == gone_digest
    r = c.post(url, headers=SAME, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (
        303,
        c.url(f"/trips/{trip.id}?f=cover_removed"),
    )
    held = row(ctx, trip.id)
    assert (held["source"], held["state"]) == ("removed", "none")
    assert (held["digest"], held["large"], held["small"], held["upload"]) == (None,) * 4
    assert c.get(f"/covers/{digest}.jpg").status_code == 404
    assert flashed(c, r) == "Photo removed"
    # a photo still waiting for the worker is taken away the same way
    send(c, trip.id, JPEG)
    assert row(ctx, trip.id)["state"] == "pending"
    c.post(url, headers=SAME)
    assert (row(ctx, trip.id)["source"], row(ctx, trip.id)["upload"]) == ("removed", None)
    assert CoverStore(ctx.ledger).next_upload() is None




# ----------------------------------------------------------------------------- showing
def within(node: Node):
    """Every element inside `node`, in page order."""
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from within(child)


def classed(tree: Tree, name: str) -> list[Node]:
    return [n for n in tree.nodes if name in n.attrs.get("class", "").split()]


def images(tree: Tree) -> list[Node]:
    return [n for n in tree.nodes if n.tag == "img"]


def decorative(img: Node) -> bool:
    """An image a screen reader skips: no text of its own, inside a cover hidden from it."""
    covers = [n for n in img.ancestors() if "cover" in n.attrs.get("class", "").split()]
    return img.attrs.get("alt") == "" and bool(covers) and covers[0].attrs["aria-hidden"] == "true"


def credit_of(page: str) -> Node:
    (line,) = classed(Tree(page), "credit")
    return line


def test_the_card_and_the_hero_show_a_ready_photo_as_decoration(tmp_path, monkeypatch, base):
    """The list's card shows the small photo and the trip's page the large one, each an image a
    screen reader skips, sized before it loads (the card's loaded only once it is near); every
    other trip keeps its tile, and so does a trip whose photo is not ready."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 2)
    others = [t for t in ctx.trips.all_trips() if t.id != trip.id]
    for path in ("/", f"/trips/{trip.id}"):
        assert images(Tree(c.get(path).text)) == [], path  # tiles alone
    digest = ready(ctx, trip.id)
    CoverStore(ctx.ledger).upload(others[0].id, JPEG)  # on its way: still the tile
    listing = Tree(c.get("/").text)
    (card,) = images(listing)
    assert card.attrs["src"] == c.url(f"/covers/{digest}-s.jpg") and decorative(card)
    assert card.attrs["loading"] == "lazy" and card.attrs["width"] == card.attrs["height"] == "64"
    link = next(n for n in card.ancestors() if n.tag == "a")
    assert link.attrs["href"] == c.url(f"/trips/{trip.id}") and trip.title in link.text()
    assert sorted(n.attrs["class"] for n in classed(listing, "tile")) == sorted(
        f"cover tile tile-{t.id % 8}" for t in others
    )
    page = c.get(f"/trips/{trip.id}").text
    (hero,) = images(Tree(page))
    assert hero.attrs["src"] == c.url(f"/covers/{digest}.jpg") and decorative(hero)
    assert "loading" not in hero.attrs and {"width", "height"} <= set(hero.attrs)
    assert 'class="hero has-photo"' in page and "tile-" not in page
    assert c.get(hero.attrs["src"].removeprefix(base)).content == LARGE
    assert images(Tree(c.get(f"/trips/{others[0].id}").text)) == []


def test_a_photo_on_its_way_says_so_and_one_that_failed_says_why(tmp_path, monkeypatch, base):
    """While the worker has not read the owner's photo the page says it is on its way (the tile
    stays); one it could not use says why, with the form to send another. A photo the worker
    is still looking for, or found none of, says nothing."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    cv = CoverStore(ctx.ledger)
    on_its_way = "Photo on its way — it shows in a few seconds."
    cv.upload(trip.id, JPEG)
    page = c.get(f"/trips/{trip.id}").text
    assert f'<p class="muted small cover-note">{on_its_way}</p>' in page
    assert images(Tree(page)) == [] and "could not be used" not in page
    for code, why in (
        ("unreadable", "it could not be read"),
        ("too_many_pixels", "it has more than 40 million pixels"),
        ("wrong_type", "it is not a JPEG, PNG or WebP"),
        ("something new", "it could not be read"),
    ):
        cv.upload(trip.id, JPEG)
        cv.fail_upload(trip.id, code)
        tree = Tree(c.get(f"/trips/{trip.id}").text)
        (note,) = classed(tree, "cover-note")
        assert note.text().startswith(f"That photo could not be used ({why}). "), code
        (form,) = [n for n in within(note) if n.tag == "form"]
        assert form.attrs["action"] == c.url(f"/covers/upload?trip={trip.id}"), code
        assert [n.attrs.get("name") for n in within(form) if n.tag == "input"] == ["photo"]
        assert on_its_way not in tree.root.text() and images(tree) == [], code
    cv.remove(trip.id)
    ctx.trips.set_place(trip.id, "Example City")
    soon = util.now() + dt.timedelta(minutes=5)
    for write in (
        lambda: cv.lookup_failed(trip.id, "Example City", "unavailable", soon),
        lambda: cv.none_found(trip.id, "Example City", soon),
    ):
        write()
        page = c.get(f"/trips/{trip.id}").text
        assert "cover-note" not in page and images(Tree(page)) == []


def test_a_wikimedia_photo_carries_its_credit_as_text(tmp_path, monkeypatch, base):
    """A Wikimedia photo's credit line names its author and licence and Wikimedia Commons, each
    as text (an author's markup is shown, never run), the licence and the source linked to their
    pages in a new tab that is sent no address; an address that is no web page's is not linked.
    An owner's photo has no credit."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    found(ctx, trip.id)
    line = credit_of(c.get(f"/trips/{trip.id}").text)
    assert line.text() == "Photo: Sample Author · CC BY-SA 4.0 · Wikimedia Commons"
    links = {n.text(): n.attrs for n in within(line) if n.tag == "a"}
    assert {k: v["href"] for k, v in links.items()} == {
        "CC BY-SA 4.0": CREDIT["licence_url"],
        "Wikimedia Commons": CREDIT["source_url"],
    }
    assert {(v["target"], v["rel"]) for v in links.values()} == {("_blank", "noopener noreferrer")}
    hostile = {
        "artist": '<script>alert("x")</script> & <b>Sample</b>',
        "licence": "CC BY 4.0<img src=x>",
        "source_url": " //commons.example.org/wiki/File:x.jpg",
    }
    for odd in ("javascript:alert(1)", "data:text/html,x", "ftp://example.org/x", "/covers/x",
                "https://", "https:x", "https://exa mple.org/x"):
        found(ctx, trip.id, {**hostile, "licence_url": odd})
        page = c.get(f"/trips/{trip.id}").text
        line = credit_of(page)
        assert list(within(line)) == [], odd
        assert line.text() == (
            'Photo: <script>alert("x")</script> & <b>Sample</b> · CC BY 4.0<img src=x>'
            " · Wikimedia Commons"
        ), odd
        assert "<script>alert" not in page and "&lt;script&gt;" in page, odd
        assert images(Tree(page))[0].attrs["src"].startswith(c.url("/covers/")), odd
    found(ctx, trip.id, {"artist": "", "licence": "", "licence_url": "", "source_url": ""})
    assert credit_of(c.get(f"/trips/{trip.id}").text).text() == "Photo: Wikimedia Commons"
    ready(ctx, trip.id)
    assert classed(Tree(c.get(f"/trips/{trip.id}").text), "credit") == []


def test_shared_pages_and_recently_deleted_never_show_a_photo(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 1)
    found(ctx, trip.id)
    link = shares.create(ctx.ledger, trip.id, shares.FIELDS)
    shared = c.get(f"/s/{link.token}")
    assert shared.status_code == 200 and trip.title in shared.text
    assert "<img" not in shared.text and "/covers/" not in shared.text
    assert "Wikimedia" not in shared.text and "Photo:" not in shared.text
    other = next(t for t in ctx.trips.all_trips() if t.id != trip.id)
    ready(ctx, other.id, LARGE + b"?")
    ctx.trips.delete_trip(other.id)
    gone = c.get("/deleted").text
    assert f'class="cover tile tile-{other.id % 8}"' in gone and "<img" not in gone


@pytest.mark.parametrize("covers", ["off", "wikimedia"])
def test_the_sheet_offers_a_photo_and_find_photo_only_with_covers_on(
    tmp_path, monkeypatch, base, covers
):
    """The trip's ⋯ sheet holds the place and the photo under one heading: the place's form,
    which finds a photo too only with automatic covers on (and then says where it looks), the
    form to upload one (a JPEG, a PNG or a WebP, named for what it asks), and Remove photo once
    there is a photo, or one on its way or refused, to remove."""
    on = covers == "wikimedia"
    env = {"TRIPBOX_COVERS": covers}
    c, ctx = client_for(tmp_path, monkeypatch, base=base, env=env)
    trip = seed_trip(ctx)
    tree = Tree(c.get(f"/trips/{trip.id}").text)
    assert tree.by_id("photo-head").text() == "Place and photo"
    (part,) = [n for n in tree.nodes if n.attrs.get("aria-labelledby") == "photo-head"]
    menu = next(n for n in part.ancestors() if n.tag == "details")
    assert "menu" in menu.attrs["class"].split(), covers
    forms = {n.attrs["action"]: n for n in within(part) if n.tag == "form"}
    place = forms[c.url(f"/trips/{trip.id}/place")]
    (button,) = [n for n in within(place) if n.tag == "button"]
    assert button.text() == ("Save place and find photo" if on else "Save place"), covers
    assert ("Wikipedia" in tree.by_id("place-help").text()) is on, covers
    upload = forms[c.url(f"/covers/upload?trip={trip.id}")]
    assert (upload.attrs["method"], upload.attrs["enctype"]) == ("post", "multipart/form-data")
    (field,) = [n for n in within(upload) if n.tag == "input"]
    assert (field.attrs["type"], field.attrs["name"]) == ("file", "photo"), covers
    assert field.attrs["accept"] == "image/jpeg,image/png,image/webp"
    assert "required" in field.attrs
    assert tree.name_of(field) == "Choose a photo", covers
    assert [n.text() for n in within(upload) if n.tag == "button"] == ["Upload photo"]
    remove = c.url(f"/trips/{trip.id}/cover/remove")
    cv = CoverStore(ctx.ledger)

    def offered() -> bool:
        page = Tree(c.get(f"/trips/{trip.id}").text)
        (part,) = [n for n in page.nodes if n.attrs.get("aria-labelledby") == "photo-head"]
        held = [n for n in within(part) if n.attrs.get("action") == remove]
        assert [n.text() for n in held] in ([], ["Remove photo"])
        return bool(held)

    assert not offered()
    cv.upload(trip.id, JPEG)
    assert offered(), covers  # on its way
    cv.fail_upload(trip.id, "unreadable")
    assert offered(), covers  # refused by the worker
    ready(ctx, trip.id)
    assert offered(), covers
    cv.remove(trip.id)
    assert not offered(), covers
    ctx.trips.set_place(trip.id, "Elsewhere")
    found(ctx, trip.id)
    assert offered(), covers  # a Wikimedia photo
    cv.none_found(trip.id, "Elsewhere", util.now())
    assert not offered(), covers  # none was found: nothing to remove


@pytest.mark.parametrize("covers", ["off", "wikimedia"])
def test_saving_a_place_says_a_photo_is_looked_for_only_when_one_is(
    tmp_path, monkeypatch, base, covers
):
    """With automatic covers on, a place saved that changes it, and leaves the trip with no
    photo of the owner's, comes back saying a photo is looked for; the same place again, a
    place taken off, a trip with the owner's photo, and every save with covers off say only
    that the place was saved."""
    on = covers == "wikimedia"
    env = {"TRIPBOX_COVERS": covers}
    c, ctx = client_for(tmp_path, monkeypatch, base=base, env=env)
    trip = seed_trip(ctx)
    url = f"/trips/{trip.id}/place"

    def saved(place: str) -> str:
        r = c.post(url, data={"place": place}, headers=SAME, follow_redirects=False)
        assert r.status_code == 303
        return r.headers["location"].removeprefix(c.url(f"/trips/{trip.id}?f="))

    assert saved("King of Prussia") == ("place_photo" if on else "place"), covers
    assert saved("King of Prussia") == "place", covers
    found(ctx, trip.id)
    assert saved("Philadelphia") == ("place_photo" if on else "place"), covers
    assert saved("") == "place", covers
    ready(ctx, trip.id)
    assert saved("Pittsburgh") == "place", covers
    assert row(ctx, trip.id)["source"] == "owner"
    assert views.FLASH["place_photo"] == "Place saved — looking for a photo"


def test_the_pages_read_no_photo_bytes_and_one_row_for_every_card(tmp_path, monkeypatch, base):
    """The list and a trip's page read the covers without their images (the list in one pass
    for every card), and the content policy and the inline-style rule hold on a page with a
    photo and its credit."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 3)
    for t in ctx.trips.all_trips():
        ready(ctx, t.id, LARGE + str(t.id).encode())
    CoverStore(ctx.ledger).remove(trip.id)
    ctx.trips.set_place(trip.id, "Elsewhere")
    found(ctx, trip.id)
    passes = []
    whole = repo.Covers.all_for_lookup

    def refused(*args, **kw):
        raise AssertionError("a page read a cover's images")

    def counted(self):
        passes.append(1)
        return whole(self)

    monkeypatch.setattr(repo.Covers, "get", refused)
    monkeypatch.setattr(repo.Covers, "by_digest", refused)
    monkeypatch.setattr(repo.Covers, "all_for_lookup", counted)
    listing = c.get("/")
    assert listing.status_code == 200 and len(images(Tree(listing.text))) == 4
    assert passes == [1]
    page = c.get(f"/trips/{trip.id}")
    assert page.status_code == 200 and len(classed(Tree(page.text), "credit")) == 1
    for r in (listing, page):
        assert r.headers["content-security-policy"] == POLICY
        assert not re.search(r"\sstyle\s*=", r.text)


def test_where_photos_come_from_is_named_only_in_a_credit_or_with_covers_on(
    tmp_path, monkeypatch, base
):
    """With automatic covers off (the default), a Wikimedia photo found while they were on still
    shows with its credit, and that line is the only part of any page that names Wikimedia or
    Wikipedia or offers to find a photo. With them on, the place's form and its help say so."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    assert ctx.settings.covers == "off"
    trip = seed_trip(ctx)
    found(ctx, trip.id)
    for path in ("/", f"/trips/{trip.id}", f"/trips/{trip.id}/share", "/deleted", "/settings"):
        r = c.get(path)
        assert r.status_code == 200, path
        rest = unescape(re.sub(r'<p class="credit">.*?</p>', "", r.text, flags=re.S)).lower()
        for word in ("wikipedia", "wikimedia", "find photo"):
            assert word not in rest, (path, word)
    assert credit_of(c.get(f"/trips/{trip.id}").text).text().endswith("· Wikimedia Commons")
    on, ctx = client_for(
        tmp_path / "on", monkeypatch, base=base, env={"TRIPBOX_COVERS": "wikimedia"}
    )
    trip = seed_trip(ctx)
    page = on.get(f"/trips/{trip.id}").text
    assert ">Save place and find photo</button>" in page
    assert "A free photo of it is looked up on Wikipedia." in page
