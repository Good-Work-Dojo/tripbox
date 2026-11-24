# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
import json

import pytest
from test_trips_store import HOME, outbound

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.trips.covers import RECHECK_KV, CoverStore
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
    taken = cv.upload(tid, b"raw")
    assert taken == cv.next_upload() == (tid, b"raw")
    assert cv.finish_upload(taken, b"big", b"sm", "dg") is True
    c = cv.get(tid)
    assert (c.source, c.state, c.digest) == ("owner", "ready", "dg")
    assert cv.next_upload() is None
    assert led.covers.get(tid)["upload"] is None
    assert cv.finish_upload(taken, b"big2", b"sm2", "dg2") is False  # answered once
    assert cv.get(tid).digest == "dg"


def test_fail_upload_records_the_code_and_leaves_the_queue():
    st, cv, led = rig()
    tid = a_trip(st)
    assert cv.fail_upload(cv.upload(tid, b"raw"), "heic") is True
    c = cv.get(tid)
    assert (c.state, c.error) == ("failed", "heic")
    assert cv.next_upload() is None
    assert led.covers.get(tid)["upload"] is None


def test_an_answer_for_a_photo_the_owner_replaced_meanwhile_changes_nothing():
    """The worker reads a photo outside any transaction: a second upload sent meanwhile stays
    waiting, the first one's answer (a cover or a failure) is dropped, and the second is read
    next. The same bytes sent again are the same photo, so their answer stands."""
    st, cv, led = rig()
    tid = a_trip(st)
    cv.upload(tid, b"photo A")
    taken = cv.next_upload()
    cv.upload(tid, b"photo B")
    assert cv.finish_upload(taken, b"LA", b"SA", "dA") is False
    assert cv.fail_upload(taken, "unreadable") is False
    row = led.covers.get(tid)
    assert (row["source"], row["state"], row["upload"]) == ("owner", "pending", b"photo B")
    assert (row["digest"], row["large"], row["error"]) == (None, None, None)
    assert cv.next_upload() == (tid, b"photo B")
    assert cv.image("dA", "large") is None
    cv.upload(tid, b"photo B")
    assert cv.finish_upload(cv.next_upload(), b"LB", b"SB", "dB") is True
    assert (cv.get(tid).state, cv.get(tid).digest) == ("ready", "dB")


def test_an_answer_for_a_photo_the_owner_removed_meanwhile_changes_nothing():
    """Remove photo while the worker reads it: the trip stays removed, and the photo is never
    served."""
    st, cv, led = rig()
    tid = a_trip(st)
    taken = cv.upload(tid, b"photo A")
    cv.remove(tid)
    assert cv.finish_upload(taken, b"LA", b"SA", "dA") is False
    assert cv.fail_upload(taken, "unreadable") is False
    c = cv.get(tid)
    assert (c.source, c.state, c.digest, c.error) == ("removed", "none", None, None)
    assert cv.shown_image("dA", "large") is None and cv.image("dA", "large") is None
    assert cv.next_upload() is None


def test_an_answer_for_a_photo_whose_trip_was_merged_meanwhile_changes_nothing():
    """A merge moves the waiting photo to the trip that stays: the answer for the trip that went
    is dropped without an error, and the photo is read again for the one that stays."""
    st, cv, led = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    taken = cv.upload(a, b"photo A")
    st.merge(a, b)
    assert cv.finish_upload(taken, b"LA", b"SA", "dA") is False
    assert cv.fail_upload(taken, "unreadable") is False
    assert cv.get(a) is None and led.covers.get(a) is None
    assert cv.next_upload() == (b, b"photo A")
    assert cv.finish_upload(cv.next_upload(), b"LA", b"SA", "dA") is True
    assert (cv.get(b).source, cv.get(b).state) == ("owner", "ready")


def test_reupload_replaces_images_and_lets_the_old_digest_go():
    st, cv, _ = rig()
    tid = a_trip(st)
    cv.finish_upload(cv.upload(tid, b"one"), b"L", b"S", "old")
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


@pytest.mark.parametrize("how", ["none", "failed", "removed"])
def test_saving_the_same_place_to_look_again_clears_what_is_no_photo(how):
    """With automatic covers on (`look_again`), saving the place, unchanged too, clears nothing
    found, a failed lookup's backoff and a removal, so the worker looks the place up at once;
    with them off, the row stays."""
    st, cv, _ = rig()
    tid = a_trip(st)
    no_cover(st, cv, tid, how)
    st.set_place(tid, "Philadelphia")
    assert cv.get(tid) is not None
    st.set_place(tid, "Philadelphia", look_again=True)
    assert cv.get(tid) is None
    assert cv.due_lookups([(tid, "Philadelphia")], NOW, 2) == [(tid, "Philadelphia")]


def test_looking_again_keeps_a_found_photo_and_the_owners():
    st, cv, led = rig()
    tid = a_trip(st)
    found(cv, tid)
    st.set_place(tid, "Philadelphia", look_again=True)
    assert (cv.get(tid).source, cv.get(tid).state) == ("wikimedia", "ready")
    taken = cv.upload(tid, b"mine")
    st.set_place(tid, "Philadelphia", look_again=True)
    assert (cv.get(tid).source, cv.get(tid).state) == ("owner", "pending")
    cv.fail_upload(taken, "unreadable")
    st.set_place(tid, "Philadelphia", look_again=True)
    assert (cv.get(tid).source, cv.get(tid).state) == ("owner", "failed")
    other = another(st, "2027-09-01T08:20", "OTHER1")
    st.set_place(other, "", look_again=True)
    assert cv.get(other) is None and len(led.covers.all_for_lookup()) == 1


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


def test_merge_moves_a_wikimedia_cover_with_its_own_place():
    st, cv, _ = rig()
    a = a_trip(st, "Boston")
    b = another(st, "2027-09-01T08:20", "OTHER1")
    found(cv, a, "Boston", "da")
    st.merge(a, b)
    assert (cv.get(b).digest, cv.get(b).place) == ("da", "Boston")


def test_merge_an_owner_photo_replaces_an_automatic_cover_or_removal():
    st, cv, led = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    cv.upload(a, b"mine")
    found(cv, b, "Philadelphia", "db")
    st.merge(a, b)
    assert (cv.get(b).source, cv.get(b).state) == ("owner", "pending")
    assert [r["trip_id"] for r in led.covers.all_for_lookup()] == [b]
    c = another(st, "2027-10-01T08:20", "OTHER2")
    d = another(st, "2027-11-01T08:20", "OTHER3")
    cv.upload(c, b"mine")
    cv.remove(d)
    st.merge(c, d)
    assert cv.get(d).source == "owner"


def no_cover(st, cv, tid, how):
    """A row that is no cover: nothing found, a lookup that failed, or the owner's removal."""
    place = st.get(tid).place
    if how == "none":
        cv.none_found(tid, place, NOW + dt.timedelta(days=30))
    elif how == "failed":
        cv.lookup_failed(tid, place, "http_5xx", NOW + dt.timedelta(days=1))
    else:
        cv.remove(tid)
    assert cv.get(tid) is not None and cv.get(tid).state in ("none", "failed")


@pytest.mark.parametrize("how", ["none", "failed", "removed"])
def test_merge_a_destination_with_no_cover_takes_the_sources(how):
    """Only an owner's photo or a ready one is a trip's cover: a destination whose row is
    nothing found, a failed lookup or a removal takes the source's ready photo or owner's photo
    in its place."""
    for give in ("ready", "owner"):
        st, cv, led = rig()
        a = a_trip(st)
        b = another(st, "2027-09-01T08:20", "OTHER1")
        if give == "ready":
            found(cv, a, digest="da")
        else:
            cv.upload(a, b"mine")
        no_cover(st, cv, b, how)
        st.merge(a, b)
        c = cv.get(b)
        assert (c.source, c.state) == (
            ("wikimedia", "ready") if give == "ready" else ("owner", "pending")
        ), (how, give)
        assert [r["trip_id"] for r in led.covers.all_for_lookup()] == [b]


@pytest.mark.parametrize("how", ["none", "failed", "removed"])
def test_merge_a_source_row_that_is_no_cover_is_dropped(how):
    """A source's row that is no cover never moves: the destination keeps its own cover, or with
    none is looked up for its own place, not kept from it by the source's backoff or removal."""
    st, cv, led = rig()
    a = a_trip(st, "Boston")
    b = another(st, "2027-09-01T08:20", "OTHER1")
    no_cover(st, cv, a, how)
    st.merge(a, b)
    assert led.covers.all_for_lookup() == []
    place = st.get(b).place
    assert place and cv.due_lookups([(b, place)], NOW, 2) == [(b, place)]
    st, cv, led = rig()
    a = a_trip(st, "Boston")
    b = another(st, "2027-09-01T08:20", "OTHER1")
    no_cover(st, cv, a, how)
    found(cv, b, st.get(b).place, "db")
    st.merge(a, b)
    assert cv.get(a) is None and (cv.get(b).state, cv.get(b).digest) == ("ready", "db")
    assert [r["trip_id"] for r in led.covers.all_for_lookup()] == [b]


def test_merge_a_destination_keeps_its_own_row_when_neither_is_a_cover():
    st, cv, led = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    no_cover(st, cv, a, "failed")
    no_cover(st, cv, b, "removed")
    st.merge(a, b)
    assert [(r["trip_id"], r["source"]) for r in led.covers.all_for_lookup()] == [(b, "removed")]


def test_merge_an_owner_destination_keeps_its_photo_over_a_source_owner_photo():
    st, cv, _ = rig()
    a = a_trip(st)
    b = another(st, "2027-09-01T08:20", "OTHER1")
    cv.upload(a, b"src")
    cv.upload(b, b"dst")
    st.merge(a, b)
    assert cv.next_upload() == (b, b"dst")


def test_a_rolled_back_upload_or_remove_leaves_no_row():
    st, cv, led = rig()
    tid = a_trip(st)
    for write in (lambda: cv.upload(tid, b"x"), lambda: cv.remove(tid)):
        try:
            with led.transaction():
                write()
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert cv.get(tid) is None


def test_a_lookup_answer_never_overwrites_an_owner_row_written_just_before():
    st, cv, _ = rig()
    tid = a_trip(st)
    cv.upload(tid, b"mine")
    cv.none_found(tid, "Philadelphia", NOW)
    cv.lookup_failed(tid, "Philadelphia", "http_500", NOW)
    assert (cv.get(tid).source, cv.get(tid).state) == ("owner", "pending")


def test_the_places_found_with_no_photo_are_looked_up_again_once():
    """1.1.0 refused every thumbnail on thumb.wikimedia.org and kept its place as having no
    photo for NONE_DAYS. The recheck deletes each Wikimedia row that found none, so its place is
    due again; it leaves an owner's photo, a found one, a failed lookup's backoff and a removal
    as they are, and once it has run (`RECHECK_KV`) it deletes nothing again."""
    st, cv, led = rig()
    kinds = ("none", "ready", "owner", "pending", "failed", "removed")
    ids = {how: another(st, f"2027-{n + 7:02d}-10T08:20", f"P{n}") for n, how in enumerate(kinds)}
    for how, tid in ids.items():
        st.set_place(tid, f"Place {how}")
    later = NOW + dt.timedelta(days=29)
    cv.none_found(ids["none"], "Place none", later)
    found(cv, ids["ready"], "Place ready")
    cv.finish_upload(cv.upload(ids["owner"], b"mine"), b"big", b"sm", "do")
    cv.upload(ids["pending"], b"waiting")
    cv.lookup_failed(ids["failed"], "Place failed", "http_5xx", later)
    cv.remove(ids["removed"])
    rows = "SELECT * FROM covers ORDER BY trip_id"
    kept = [tuple(r) for r in led.conn.execute(rows) if r["trip_id"] != ids["none"]]
    assert led.kv.get(RECHECK_KV) is None
    assert cv.recheck_none_found() == 1
    assert [tuple(r) for r in led.conn.execute(rows)] == kept
    shown = [(tid, f"Place {how}") for how, tid in ids.items()]
    assert cv.due_lookups(shown, NOW, 10) == [(ids["none"], "Place none")]
    assert led.kv.get(RECHECK_KV)
    cv.none_found(ids["none"], "Place none", later)  # found with no photo since the recheck
    assert cv.recheck_none_found() == 0
    assert cv.get(ids["none"]).state == "none" and cv.due_lookups(shown, NOW, 10) == []
