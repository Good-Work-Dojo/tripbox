# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The worker's covers part: owner uploads are read, and with TRIPBOX_COVERS=wikimedia a few
places a beat are looked up, copied from another trip, or backed off."""

import datetime as dt
import io
import logging

import httpx2
import pytest
from PIL import Image
from test_trips_store import outbound
from test_worker import a_worker, beat, context, start

from tripbox.trips import coverimage, wikimedia
from tripbox.trips.covers import CoverStore

AT = dt.datetime(2027, 6, 1, 12, 0, tzinfo=dt.UTC)


def jpeg(size=(300, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 80, 40)).save(out, "JPEG")
    return out.getvalue()


def a_trip(ctx, place, dep="2027-07-01T09:00", pnr="ABC123"):
    item = outbound(dep=dep, arr=dep[:10] + "T18:00", pnr=pnr)
    trip = ctx.trips.find_or_create(item)
    ctx.trips.upsert_item(trip, item)
    ctx.trips.set_place(trip.id, place)
    return trip.id


class Wiki:
    """A MockTransport standing in for Wikipedia and Commons: every place has a free photo, its
    thumbnail on thumb.wikimedia.org as Commons names it, unless `status` is set (then every
    request answers it)."""

    def __init__(self, status=None, missing=()):
        self.calls: list[httpx2.Request] = []
        self.status = status
        self.missing = set(missing)

    def __call__(self, request):
        self.calls.append(request)
        if self.status:
            return httpx2.Response(self.status)
        host, q = request.url.host, request.url.params
        if host == "en.wikipedia.org":
            if q["titles"] in self.missing:
                return httpx2.Response(200, json={"query": {"pages": [{"missing": True}]}})
            return httpx2.Response(
                200, json={"query": {"pages": [{"pageimage": "X.jpg", "title": q["titles"]}]}}
            )
        if host == "commons.wikimedia.org":
            meta = {
                "License": {"value": "cc0"},
                "LicenseShortName": {"value": "CC0"},
                "LicenseUrl": {"value": "https://example.org/l"},
                "Artist": {"value": "Sample Author"},
            }
            info = {
                "thumburl": "https://thumb.wikimedia.org/x/xa/X.jpg/1600px-X.jpg",
                "descriptionurl": "https://commons.example.org/File:X.jpg",
                "extmetadata": meta,
            }
            return httpx2.Response(200, json={"query": {"pages": [{"imageinfo": [info]}]}})
        return httpx2.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})

    def lookups(self):
        return [r for r in self.calls if r.url.host == "en.wikipedia.org"]


def rig(tmp_path, wiki=None, covers="wikimedia"):
    ctx = context(tmp_path, TRIPBOX_COVERS=covers)
    w, clock, _ = a_worker(ctx, AT, transport=httpx2.MockTransport(wiki or Wiki()))
    start(w)
    return ctx, w, clock, CoverStore(ctx.ledger)


def test_a_pending_upload_is_processed_on_one_beat(tmp_path):
    ctx, w, clock, cv = rig(tmp_path, covers="off")
    tid = a_trip(ctx, "Philadelphia")
    cv.upload(tid, jpeg((400, 300)))
    clock.tick(5)
    beat(w)
    c = cv.get(tid)
    assert (c.source, c.state) == ("owner", "ready") and c.digest
    assert cv.image(c.digest, "small") and cv.image(c.digest, "large")
    assert w.last["covers"]["ok"] is True


def test_a_bad_upload_ends_failed_with_its_code(tmp_path):
    ctx, w, clock, cv = rig(tmp_path, covers="off")
    tid = a_trip(ctx, "Philadelphia")
    cv.upload(tid, b"not a picture at all")
    clock.tick(5)
    beat(w)
    c = cv.get(tid)
    assert (c.state, c.error) == ("failed", "wrong_type")
    assert cv.next_upload() is None
    assert w.last["covers"]["ok"] is False and w.last["covers"]["code"] == "wrong_type"
    good = jpeg()
    cv.upload(tid, good[: len(good) // 2])  # a picture cut short
    clock.tick(5)
    beat(w)
    assert (cv.get(tid).state, cv.get(tid).error) == ("failed", "unreadable")


@pytest.mark.parametrize("meanwhile", ["upload", "remove", "merge"])
def test_what_the_owner_does_while_the_worker_reads_a_photo_wins(
    tmp_path, monkeypatch, caplog, meanwhile
):
    """The page writes while the worker decodes: a second upload, Remove photo or a merge. The
    newest of the owner's actions stands, the beat raises nothing and says so in the log, and a
    photo still waiting (the second one, or the one the merge moved) is read on the next beat."""
    ctx, w, clock, cv = rig(tmp_path, covers="off")
    tid = a_trip(ctx, "Philadelphia")
    other = a_trip(ctx, "Boston", dep="2027-09-01T09:00", pnr="XYZ789")
    first, second = jpeg((300, 200)), jpeg((200, 300))
    acts = {
        "upload": lambda: cv.upload(tid, second),
        "remove": lambda: cv.remove(tid),
        "merge": lambda: ctx.trips.merge(tid, other),
    }
    real, pending = coverimage.process, [acts[meanwhile]]

    def reading(data):
        while pending:
            pending.pop()()
        return real(data)

    monkeypatch.setattr(coverimage, "process", reading)
    cv.upload(tid, first)
    clock.tick(5)
    with caplog.at_level(logging.INFO, logger="tripbox.worker"):
        beat(w)
    assert w.last["covers"] == {"at": w.last["covers"]["at"], "ok": True, "code": "done"}
    assert any("changed while it was read" in r.getMessage() for r in caplog.records)
    shown = ctx.ledger.conn.execute("SELECT trip_id, source, state FROM covers").fetchall()
    assert real(first).digest not in {c.digest for c in cv.all().values()}
    if meanwhile == "remove":
        assert [tuple(r) for r in shown] == [(tid, "removed", "none")]
    else:
        held = tid if meanwhile == "upload" else other
        assert [tuple(r) for r in shown] == [(held, "owner", "pending")]
        clock.tick(5)
        beat(w)
        c = cv.get(held)
        assert (c.source, c.state) == ("owner", "ready")
        assert c.digest == real(second if meanwhile == "upload" else first).digest


def test_with_covers_off_no_request_is_made(tmp_path):
    wiki = Wiki()
    ctx, w, clock, cv = rig(tmp_path, wiki, covers="off")
    a_trip(ctx, "Philadelphia")
    for _ in range(3):
        clock.tick(5)
        beat(w)
    assert wiki.calls == [] and w._http is None
    assert w.last["covers"] is None


def test_a_lookup_stores_a_cover_with_its_credit(tmp_path):
    ctx, w, clock, cv = rig(tmp_path)
    tid = a_trip(ctx, "Philadelphia")
    clock.tick(5)
    beat(w)
    c = cv.get(tid)
    assert (c.source, c.state, c.place) == ("wikimedia", "ready", "Philadelphia")
    assert c.credit["artist"] == "Sample Author"
    assert cv.image(c.digest, "large")
    assert w.last["covers"]["code"] == "done"


def test_at_most_two_lookups_a_beat(tmp_path):
    wiki = Wiki()
    ctx, w, clock, cv = rig(tmp_path, wiki)
    for i, place in enumerate(["Aaa", "Bbb", "Ccc"]):
        a_trip(ctx, place, dep=f"2027-0{i + 3}-01T09:00", pnr=f"PNR00{i}")
    clock.tick(5)
    beat(w)
    assert len(wiki.lookups()) == 2
    clock.tick(60)
    beat(w)
    assert len(wiki.lookups()) == 3


def test_lookups_run_in_a_batch_at_most_once_a_minute(tmp_path):
    wiki = Wiki()
    ctx, w, clock, cv = rig(tmp_path, wiki)
    for i, place in enumerate(["Aaa", "Bbb", "Ccc"]):
        a_trip(ctx, place, dep=f"2027-0{i + 3}-01T09:00", pnr=f"PNR00{i}")
    clock.tick(5)
    beat(w)
    assert len(wiki.lookups()) == 2
    for _ in range(11):  # 55 s later, beat by beat
        clock.tick(5)
        beat(w)
    assert len(wiki.lookups()) == 2
    clock.tick(5)  # 60 s after the batch
    beat(w)
    assert len(wiki.lookups()) == 3


def test_the_same_place_is_copied_without_network(tmp_path):
    wiki = Wiki()
    ctx, w, clock, cv = rig(tmp_path, wiki)
    one = a_trip(ctx, "Philadelphia", dep="2027-03-01T09:00", pnr="PNR001")
    clock.tick(5)
    beat(w)
    seen = len(wiki.calls)
    two = a_trip(ctx, " philadelphia ", dep="2027-05-01T09:00", pnr="PNR002")
    clock.tick(60)
    beat(w)
    assert len(wiki.calls) == seen
    a, b = cv.get(one), cv.get(two)
    assert (b.source, b.state, b.digest, b.credit) == ("wikimedia", "ready", a.digest, a.credit)
    assert cv.image(b.digest, "small")


def test_a_transient_failure_backs_off_from_five_minutes_doubling_to_a_day(tmp_path):
    wiki = Wiki(status=503)
    ctx, w, clock, cv = rig(tmp_path, wiki)
    tid = a_trip(ctx, "Philadelphia")
    waits = []
    for _ in range(12):
        clock.tick(5)
        beat(w)
        row = ctx.ledger.covers.get(tid)
        assert row["state"] == "failed" and row["error"] == "http_5xx"
        after = dt.datetime.fromisoformat(row["retry_after"])
        waits.append(int((after - clock()).total_seconds()))
        before = len(wiki.calls)
        clock.tick(waits[-1] - 1)  # not yet due
        beat(w)
        assert len(wiki.calls) == before
        clock.tick(2)
    assert waits[:4] == [300, 600, 1200, 2400]
    assert waits[-1] == 86400 and max(waits) == 86400
    assert w.last["covers"]["ok"] is False


def test_a_refused_user_agent_backs_off_instead_of_finding_none(tmp_path):
    """A 403 from Wikimedia (say, for the User-Agent) is a failure tried again in five minutes,
    never thirty days of nothing found for every trip."""
    ctx, w, clock, cv = rig(tmp_path, Wiki(status=403))
    tid = a_trip(ctx, "Philadelphia")
    clock.tick(5)
    beat(w)
    row = ctx.ledger.covers.get(tid)
    assert (row["state"], row["error"]) == ("failed", "http_4xx")
    assert dt.datetime.fromisoformat(row["retry_after"]) == clock() + dt.timedelta(minutes=5)
    assert w.last["covers"] == {"at": w.last["covers"]["at"], "ok": False, "code": "http_4xx"}


def test_none_found_is_asked_again_after_thirty_days(tmp_path):
    wiki = Wiki(missing={"Nowhere"})
    ctx, w, clock, cv = rig(tmp_path, wiki)
    tid = a_trip(ctx, "Nowhere")
    clock.tick(5)
    beat(w)
    assert cv.get(tid).state == "none"
    row = ctx.ledger.covers.get(tid)
    assert dt.datetime.fromisoformat(row["retry_after"]) == clock() + dt.timedelta(days=30)
    n = len(wiki.calls)
    clock.tick(29 * 86400)
    beat(w)
    assert len(wiki.calls) == n
    clock.tick(2 * 86400)
    beat(w)
    assert len(wiki.calls) > n


def test_deleted_trips_are_skipped(tmp_path):
    wiki = Wiki()
    ctx, w, clock, cv = rig(tmp_path, wiki)
    tid = a_trip(ctx, "Philadelphia")
    ctx.trips.delete_trip(tid)
    clock.tick(5)
    beat(w)
    assert wiki.calls == [] and cv.get(tid) is None


def test_any_exception_from_find_is_a_transient_failure(tmp_path, monkeypatch):
    def boom(place, *, http):
        raise AttributeError("unexpected")

    monkeypatch.setattr(wikimedia, "find", boom)
    ctx, w, clock, cv = rig(tmp_path)
    tid = a_trip(ctx, "Philadelphia")
    clock.tick(5)
    beat(w)
    row = ctx.ledger.covers.get(tid)
    assert (row["state"], row["error"]) == ("failed", "error")
    assert dt.datetime.fromisoformat(row["retry_after"]) == clock() + dt.timedelta(minutes=5)
    assert cv.due_lookups([(tid, "Philadelphia")], clock(), 2) == []


def test_a_photo_that_is_no_image_is_none_found(tmp_path, monkeypatch):
    monkeypatch.setattr(
        wikimedia, "find", lambda place, *, http: wikimedia.Found(b"junk", {"artist": "A"})
    )
    ctx, w, clock, cv = rig(tmp_path)
    tid = a_trip(ctx, "Philadelphia")
    clock.tick(5)
    beat(w)
    assert cv.get(tid).state == "none"
