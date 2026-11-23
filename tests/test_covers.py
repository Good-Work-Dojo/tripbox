# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
import json

from test_trips_store import HOME, outbound

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.trips.covers import CoverStore
from tripbox.trips.store import TripStore

NOW = dt.datetime(2027, 6, 1, 12, 0, tzinfo=dt.UTC)
CREDIT = {"title": "File:Example.jpg", "author": "A. Sample", "licence": "CC BY 4.0"}


def rig():
    led = Ledger.memory()
    return TripStore(led, HOME), CoverStore(led), led


def another(st, dep, pnr):
    item = outbound(dep=dep, arr=dep[:10] + "T18:00", pnr=pnr)
    trip = st.find_or_create(item)
    st.upsert_item(trip, item)
    return trip.id


def a_trip(st, place="Philadelphia"):
    item = outbound()
    trip = st.find_or_create(item)
    st.upsert_item(trip, item)
    if place != trip.place:
        st.set_place(trip.id, place)
    return trip.id


def found(cv, tid, place="Philadelphia", digest="d1"):
    with cv.led.transaction():
        cv.found(tid, place, b"L" + digest.encode(), b"S" + digest.encode(), digest, CREDIT)


def test_get_is_none_without_a_row():
    st, cv, _ = rig()
    assert cv.get(a_trip(st)) is None


def test_found_makes_a_ready_wikimedia_cover_with_credit_and_images():
    st, cv, _ = rig()
    tid = a_trip(st)
    found(cv, tid)
    c = cv.get(tid)
    assert (c.source, c.state, c.place, c.digest, c.credit) == (
        "wikimedia", "ready", "Philadelphia", "d1", CREDIT)
    assert cv.image("d1", "large") == b"Ld1" and cv.image("d1", "small") == b"Sd1"
    assert cv.image("nope", "large") is None


def test_owner_upload_beats_wikimedia_and_survives_place_change():
    st, cv, _ = rig()
    tid = a_trip(st)
    found(cv, tid)
    cv.upload(tid, b"raw")
    c = cv.get(tid)
    assert (c.source, c.state, c.digest) == ("owner", "pending", None)
    assert cv.image("d1", "large") is None
    assert cv.next_upload() == (tid, b"raw")
    st.set_place(tid, "Boston")
    assert cv.get(tid).source == "owner"
    # a late lookup answer never replaces the owner's photo
    found(cv, tid, "Boston", "d2")
    assert cv.get(tid).source == "owner"


def test_finish_upload_makes_it_ready_and_drops_the_raw_bytes():
    st, cv, led = rig()
    tid = a_trip(st)
    cv.upload(tid, b"raw")
    with led.transaction():
        cv.finish_upload(tid, b"big", b"sm", "dg")
    c = cv.get(tid)
    assert (c.source, c.state, c.digest) == ("owner", "ready", "dg")
    assert cv.next_upload() is None
    assert led.covers.get(tid)["upload"] is None


def test_fail_upload_records_the_code_and_leaves_the_queue():
    st, cv, led = rig()
    tid = a_trip(st)
    cv.upload(tid, b"raw")
    with led.transaction():
        cv.fail_upload(tid, "heic")
    c = cv.get(tid)
    assert (c.state, c.error) == ("failed", "heic")
    assert cv.next_upload() is None


def test_reupload_replaces_images_and_lets_the_old_digest_go():
    st, cv, _ = rig()
    tid = a_trip(st)
    cv.upload(tid, b"one")
    with cv.led.transaction():
        cv.finish_upload(tid, b"L", b"S", "old")
    cv.upload(tid, b"two")
    assert cv.image("old", "large") is None
    assert cv.get(tid).digest is None


def test_remove_suppresses_lookups_until_the_place_changes():
    st, cv, _ = rig()
    tid = a_trip(st)
    found(cv, tid)
    cv.remove(tid)
    c = cv.get(tid)
    assert (c.source, c.state, c.digest) == ("removed", "none", None)
    assert cv.image("d1", "large") is None
    assert cv.due_lookups([(tid, "Philadelphia")], NOW, 5) == []
    found(cv, tid)  # a late answer does not undo the removal
    assert cv.get(tid).source == "removed"
    st.set_place(tid, "Boston")
    assert cv.get(tid) is None
    assert cv.due_lookups([(tid, "Boston")], NOW, 5) == [(tid, "Boston")]


def test_set_place_to_the_same_place_keeps_the_cover():
    st, cv, _ = rig()
    tid = a_trip(st)
    found(cv, tid)
    st.set_place(tid, "  Philadelphia ")
    assert cv.get(tid) is not None
    st.set_place(tid, "Boston")
    assert cv.get(tid) is None


def test_merge_destination_keeps_its_own_cover():
    st, cv, led = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    found(cv, a, digest="da")
    found(cv, b, "Philadelphia", "db")
    st.merge(a, b)
    assert cv.get(b).digest == "db"
    assert cv.get(a) is None and led.covers.all_for_lookup()[0]["trip_id"] == b
    assert len(led.covers.all_for_lookup()) == 1


def test_merge_moves_the_sources_cover_when_the_destination_has_none():
    st, cv, led = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    found(cv, a, digest="da")
    st.merge(a, b)
    assert cv.get(b).digest == "da" and cv.get(a) is None
    assert [r["trip_id"] for r in led.covers.all_for_lookup()] == [b]


def test_merge_with_no_covers_is_a_no_op():
    st, cv, led = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    st.merge(a, b)
    assert led.covers.all_for_lookup() == []


def test_same_place_ready_folds_case_and_spacing_and_skips_own_trip():
    st, cv, _ = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    found(cv, a, "Philadelphia", "da")
    assert cv.same_place_ready("  philadelphia ").digest == "da"
    assert cv.same_place_ready("PHILADELPHIA", exclude=a) is None
    assert cv.same_place_ready("Boston") is None
    cv.upload(b, b"x")  # an owner photo is not shared by place
    assert cv.same_place_ready("Philadelphia", exclude=a) is None


def test_due_lookups_respects_retry_after_and_limit():
    st, cv, led = rig()
    ids = [a_trip(st)]
    for n in range(4):
        ids.append(another(st, f"2027-{n + 7:02d}-10T08:20", f"P{n}"))
    past, future = NOW - dt.timedelta(hours=1), NOW + dt.timedelta(hours=1)
    with led.transaction():
        cv.none_found(ids[1], "Philadelphia", past)
        cv.none_found(ids[2], "Philadelphia", future)
        cv.lookup_failed(ids[3], "Philadelphia", "http_429", past)
        cv.lookup_failed(ids[4], "Philadelphia", "http_429", future)
    want = [(i, "Philadelphia") for i in ids]
    got = cv.due_lookups(want, NOW, 10)
    assert got == [(ids[0], "Philadelphia"), (ids[1], "Philadelphia"), (ids[3], "Philadelphia")]
    assert cv.due_lookups(want, NOW, 2) == got[:2]
    assert cv.due_lookups([(ids[0], "  ")], NOW, 2) == []
    assert cv.get(ids[3]).error == "http_429"
    assert led.covers.get(ids[3])["attempts"] == 1
    with led.transaction():
        cv.lookup_failed(ids[3], "Philadelphia", "http_429", future)
    assert led.covers.get(ids[3])["attempts"] == 2


def test_due_lookups_skips_ready_owner_and_deleted_trips():
    st, cv, _ = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    c = another(st, "2027-10-01T08:20", "OTHER2")
    found(cv, a)
    cv.upload(b, b"x")
    st.delete_trip(c)
    assert cv.due_lookups([(a, "P"), (b, "P"), (c, "P")], NOW, 5) == []


def test_found_for_a_vanished_or_moved_place_is_dropped():
    st, cv, _ = rig()
    tid = a_trip(st)
    found(cv, tid, "Elsewhere")
    assert cv.get(tid) is None


def test_purge_removes_the_cover_and_restore_keeps_it():
    st, cv, led = rig()
    tid = a_trip(st)
    found(cv, tid)
    st.delete_trip(tid)
    assert cv.get(tid) is not None
    st.restore_trip(tid)
    assert cv.get(tid) is not None
    st.delete_trip(tid)
    st.purge_deleted(now=util.now() + dt.timedelta(days=400))
    assert led.trips.get(tid) is None
    assert cv.get(tid) is None and led.covers.all_for_lookup() == []


def test_credit_round_trips_as_json_and_none_when_absent():
    st, cv, led = rig()
    tid = a_trip(st)
    found(cv, tid)
    assert json.loads(led.covers.get(tid)["credit_json"]) == CREDIT
    cv.remove(tid)
    assert cv.get(tid).credit is None
