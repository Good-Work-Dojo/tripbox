# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import dataclasses
import datetime as dt

import pytest
from helpers.airports import airports
from helpers.flightlog import flight_log
from test_checks import booking, edit, validated
from test_planner import BOOK, EMAIL, items_for
from test_planner import bookings as filed_bookings
from test_planner import run as file_mail
from test_planner import store as filing_store
from test_store_actions import an_hour_later

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.parse.models import Extraction, Reservation
from tripbox.trips import grouping, shares
from tripbox.trips.store import KeyCollision, TripStore
from tripbox.validate import keys
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import validate_extraction
from tripbox.write.sync import Syncer

HOME = ("MSP",)


def car_item(pickup="PHL", start="2027-06-22T13:00", end="2027-06-26T10:00", code="R1"):
    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            Reservation(
                kind="car",
                provider="Example Rent A Car",
                confirmation_code=code,
                pickup_location=pickup,
                pickup_time=start,
                dropoff_time=end,
                passengers=["Alex Sample"],
            )
        ],
    )
    return validate_extraction(ex, refdata=airports(), book=BOOK, email_date=EMAIL)[0]


def outbound(**kw):
    return items_for(["Alex Sample"], **kw)[0]  # DL1234 MSP→PHL 2027-06-22 08:20→11:55


def inbound(**kw):
    kw.setdefault("dep", "2027-06-26T16:45")
    kw.setdefault("arr", "2027-06-26T18:50")
    kw.setdefault("pnr", "XYZ789")
    kw.setdefault("number", "9246")
    return items_for(["Alex Sample"], origin="PHL", dest="MSP", **kw)[0]


def store():
    return TripStore(Ledger.memory(), HOME)


def test_find_or_create_titles_the_trip_after_the_city():
    st = store()
    trip = st.find_or_create(outbound())
    assert (trip.title, trip.place) == ("Philadelphia", "Philadelphia")
    assert (trip.start_date, trip.end_date) == ("2027-06-22", "2027-06-22")


def test_return_under_another_pnr_joins_and_extends_the_same_place_trip():
    st = store()
    t1 = st.find_or_create(outbound())
    st.upsert_item(t1, outbound())
    t2 = st.find_or_create(inbound())
    assert t2.id == t1.id and (t2.start_date, t2.end_date) == ("2027-06-22", "2027-06-26")
    hotel = items_for(["Alex Sample"], kind="lodging", dep="2027-06-22", arr="2027-06-26")[0]
    assert st.find_or_create(hotel).id == t1.id
    denver = items_for(
        ["Alex Sample"], dest="DEN", dep="2027-07-05T08:00", arr="2027-07-05T09:10", pnr="DEN001"
    )[0]
    t3 = st.find_or_create(denver)
    assert t3.id != t1.id and t3.title == "Denver"


def test_upsert_same_key_twice_merges_into_one_row():
    st = store()
    trip = st.find_or_create(outbound())
    a = st.upsert_item(trip, items_for(["Sam Sample"])[0], source_message_id=None)
    b = st.upsert_item(
        trip,
        items_for(["Alex Sample"], seat_by={"Alex Sample": "12A"})[0],
        airtrail_flight_id=42,
    )
    assert a.id == b.id and len(st.items(trip.id)) == 1
    assert b.passengers == ["Sam Sample", "Alex Sample"] and b.airtrail_flight_id == 42
    assert b.payload["seat_by_passenger"] == {"Alex Sample": "12A"}
    assert (
        b.confirmation == "ABC123"
        and b.start_tz == "America/Chicago"
        and b.end_tz == "America/New_York"
    )


def test_a_flight_moved_to_another_day_keeps_its_row_and_extends_the_trip():
    """The email merged into the booking under the key it has: settle files it under the key its
    new day makes, and the next email for that day finds the same row."""
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    moved = outbound(dep="2027-06-24T08:20", arr="2027-06-24T11:55")
    ti2 = st.upsert_item(st.get(trip.id), dataclasses.replace(moved, key=ti.item_key))
    assert ti2.id == ti.id and ti2.item_key == moved.key == "DL|1234|2027-06-24|MSP"
    assert ti2.payload["alt_keys"] == [ti.item_key]
    assert st.get(trip.id).end_date == "2027-06-24" and len(st.items(trip.id)) == 1
    assert st.upsert_item(st.get(trip.id), moved).id == ti.id


def test_cancelling_every_item_moves_the_trip_out_of_upcoming():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    today = dt.date(2027, 6, 11)
    assert [t.id for t in st.upcoming(today)] == [trip.id]
    assert st.cancel(ti.item_key).status == "cancelled"
    assert st.upcoming(today) == []
    past = st.past(today)
    assert [t.id for t in past] == [trip.id] and past[0].cancelled is True
    assert st.get(trip.id) is not None  # never deleted


def test_inverted_lodging_dates_do_not_invert_the_range():
    st = store()
    hotel = items_for(["Alex Sample"], kind="lodging", dep="2027-06-22", arr="2027-06-25")[0]
    hotel.payload.update(check_in="2027-06-25", check_out="2027-06-22")
    assert grouping.item_dates(hotel) == (dt.date(2027, 6, 25), dt.date(2027, 6, 25))
    trip = st.find_or_create(hotel)
    ti = st.upsert_item(trip, hotel)
    # the bad check-out never widens a range either: the trip is created on the check-in day only
    assert ti.end_at is None and (trip.start_date, trip.end_date) == ("2027-06-25", "2027-06-25")
    assert st.get(trip.id).start_date == "2027-06-25" and st.get(trip.id).end_date == "2027-06-25"


def test_move_item_to_another_trip_and_to_a_new_trip():
    st = store()
    out_trip = st.find_or_create(outbound())
    out = st.upsert_item(out_trip, outbound())
    back = st.upsert_item(out_trip, inbound())
    denver = items_for(
        ["Alex Sample"], dest="DEN", dep="2027-07-05T08:00", arr="2027-07-05T09:10", pnr="DEN001"
    )[0]
    dtrip = st.find_or_create(denver)
    st.upsert_item(dtrip, denver)
    target = st.move_item(back.id, dtrip.id)
    assert target.id == dtrip.id and target.start_date == "2027-06-26"
    assert st.get(out_trip.id).end_date == "2027-06-22"
    new = st.move_item(out.id, None)
    assert new.id not in (out_trip.id, dtrip.id) and new.title == "Philadelphia"
    assert st.get(out_trip.id) is None  # emptied → deleted


def test_merge_is_atomic(monkeypatch):
    st = store()
    out_trip = st.find_or_create(outbound())
    st.upsert_item(out_trip, outbound())
    other = st.find_or_create(inbound(dep="2027-07-19T16:45", arr="2027-07-19T18:50"))
    st.upsert_item(other, inbound(dep="2027-07-19T16:45", arr="2027-07-19T18:50"))
    real = st.recompute_range

    def boom(trip_id):
        raise RuntimeError("disk full")

    link = shares.create(st.led, other.id, shares.DEFAULT)
    monkeypatch.setattr(st, "recompute_range", boom)
    with pytest.raises(RuntimeError):
        st.merge(other.id, out_trip.id)
    assert st.get(other.id) is not None and len(st.items(other.id)) == 1
    assert len(st.items(out_trip.id)) == 1
    # a merge that did not happen leaves the source's share link working
    assert [r["id"] for r in st.led.shares.by_trip(other.id)] == [link.id]
    monkeypatch.setattr(st, "recompute_range", real)
    merged = st.merge(other.id, out_trip.id)
    assert merged.id == out_trip.id and len(st.items(out_trip.id)) == 2
    assert st.get(other.id) is None and merged.end_date == "2027-07-19"
    # the emptied source went for good, and its link with it (the ledger's cascade)
    assert st.led.shares.live() == []


def test_upcoming_and_past_split_on_today_with_ongoing_first():
    st = store()
    june = st.find_or_create(outbound())
    st.upsert_item(june, outbound())
    st.upsert_item(st.get(june.id), inbound())
    july = st.find_or_create(
        items_for(
            ["Alex Sample"],
            dest="DEN",
            dep="2027-07-05T08:00",
            arr="2027-07-05T09:10",
            pnr="DEN001",
        )[0]
    )
    st.upsert_item(
        july,
        items_for(
            ["Alex Sample"],
            dest="DEN",
            dep="2027-07-05T08:00",
            arr="2027-07-05T09:10",
            pnr="DEN001",
        )[0],
    )
    may = st.find_or_create(
        items_for(
            ["Alex Sample"],
            dest="DEN",
            dep="2027-05-05T08:00",
            arr="2027-05-05T09:10",
            pnr="MAY001",
        )[0]
    )
    st.upsert_item(
        may,
        items_for(
            ["Alex Sample"],
            dest="DEN",
            dep="2027-05-05T08:00",
            arr="2027-05-05T09:10",
            pnr="MAY001",
        )[0],
    )
    today = dt.date(2027, 6, 25)
    assert [t.id for t in st.upcoming(today)] == [june.id, july.id]
    assert [t.id for t in st.past(today)] == [may.id]
    assert st.past(today, limit=0) == []
    assert st.counts(june.id) == {"flight": 2}
    assert st.trip_for_pnr("XYZ789") == june.id and st.trip_for_pnr("nope") is None
    kind, nxt = grouping.next_up(st.items(june.id), dt.datetime(2027, 6, 11, 12, 0))
    assert kind == "departs" and nxt.item_key.startswith("DL|1234")
    kind, nxt = grouping.next_up(st.items(june.id), dt.datetime(2027, 6, 25, 12, 0))
    assert kind == "ongoing" and nxt.item_key.startswith("DL|9246")


def test_sort_key_puts_a_date_only_hotel_after_a_timed_flight_the_same_day():
    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(
        trip, items_for(["Alex Sample"], kind="lodging", dep="2027-06-22", arr="2027-06-25")[0]
    )
    st.upsert_item(st.get(trip.id), outbound())
    assert [ti.kind for ti in st.items(trip.id)] == ["flight", "lodging"]


def test_trip_zone_and_zone_at_follow_the_flights():
    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, outbound())
    items = st.items(trip.id)
    assert grouping.trip_tz(items, HOME) == "America/New_York"
    lax = items_for(
        ["Alex Sample"],
        origin="PHL",
        dest="LAX",
        dep="2027-06-24T09:00",
        arr="2027-06-24T12:30",
        pnr="LAX001",
    )[0]
    lax.payload.update(destination_tz="America/Los_Angeles", destination_city="Los Angeles")
    st.upsert_item(st.get(trip.id), lax)
    items = st.items(trip.id)
    assert grouping.zone_at(items, "2027-06-23T15:00", HOME) == "America/New_York"
    assert grouping.zone_at(items, "2027-06-25T15:00", HOME) == "America/Los_Angeles"
    # before the first flight lands you are where the trip starts, not at the destination
    assert grouping.zone_at(items, "2027-06-20T15:00", HOME) == "America/Chicago"
    assert grouping.zone_at(items, "2027-06-22T09:00", HOME) == "America/Chicago"
    assert grouping.zone_at(items, "2027-06-22", HOME) == "America/New_York"  # landed that day


def test_rename_and_notes():
    st = store()
    trip = st.find_or_create(outbound())
    assert st.rename(trip.id, "  Philadelphia in June ").title == "Philadelphia in June"
    assert (
        st.set_notes(trip.id, "gate code 1234\n\nparking level 2").notes
        == "gate code 1234\n\nparking level 2"
    )
    assert st.set_notes(trip.id, "   ").notes is None


def ledger_row(st, ti):
    """A row of the message log pointing at a trip item, the way the executor leaves it."""
    led = st.led
    n = led.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    mid = led.messages.insert(sha256=f"s{n}", source="cli", status="processed")
    return led.items.add(
        message_id=mid,
        n=0,
        kind=ti.kind,
        item_key=ti.item_key,
        outcome="filed",
        payload={},
        trip_item_id=ti.id,
    )


def logged(st, row) -> dict:
    return dict(st.led.items.get(row))


def test_remove_item_hides_the_row_and_the_log_still_points_at_it():
    st = store()
    trip = st.find_or_create(outbound())
    out = st.upsert_item(trip, outbound())
    back = st.upsert_item(trip, inbound())
    row = ledger_row(st, back)
    before = logged(st, row)
    left = st.remove_item(back.id)
    assert left is not None and left.id == trip.id and left.end_date == "2027-06-22"
    assert [i.id for i in st.items(trip.id)] == [out.id] and st.counts(trip.id) == {"flight": 1}
    assert st.item(back.id).removed_at and st.item_by_key(back.item_key) is None
    assert st.find(back.item_key).id == back.id  # hidden, still found by its key
    # the log of what each email did is left as it was, pointing at the booking
    assert logged(st, row) == before and before["trip_item_id"] == back.id
    assert [(i.id, t.id) for i, t in st.removed_items()] == [(back.id, trip.id)]
    with pytest.raises(KeyError):
        st.remove_item(back.id)


def test_removing_the_last_item_hides_the_trip_until_restore():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    assert st.remove_item(ti.id) is None
    assert st.get(trip.id) is not None and st.all_trips() == []
    assert st.upcoming(dt.date(2027, 1, 1)) == [] and st.past(dt.date(2028, 1, 1)) == []
    back = st.restore_item(ti.id)
    assert back.id == trip.id and [t.id for t in st.all_trips()] == [trip.id]
    assert st.item(ti.id).removed_at is None and back.start_date == "2027-06-22"
    assert st.removed_items() == []
    with pytest.raises(KeyError):
        st.restore_item(ti.id)


def test_delete_trip_hides_it_everywhere_and_restore_brings_it_back():
    st = store()
    trip = st.find_or_create(outbound())
    out = st.upsert_item(trip, outbound())
    back = st.upsert_item(trip, inbound())
    st.remove_item(back.id)  # removed before the delete: stays removed throughout
    st.delete_trip(trip.id)
    assert st.get(trip.id) is None and st.all_trips() == []
    assert [t.id for t in st.deleted_trips()] == [trip.id] and st.deleted_trips()[0].deleted_at
    assert st.trip_for_pnr("ABC123") is None and st.item_by_key(out.item_key) is None
    with pytest.raises(KeyError):
        st.delete_trip(trip.id)
    restored = st.restore_trip(trip.id)
    assert restored is not None and restored.id == trip.id and st.get(trip.id).deleted_at is None
    assert [i.id for i in st.items(trip.id)] == [out.id] and st.deleted_trips() == []
    assert st.item(back.id).removed_at
    with pytest.raises(KeyError):
        st.restore_trip(trip.id)


def test_a_new_booking_never_lands_in_a_deleted_trip():
    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, outbound())
    st.delete_trip(trip.id)
    again = st.find_or_create(inbound())  # same place, a week later: nearby — but deleted
    assert again.id != trip.id
    assert st.find_or_create(outbound(pnr="NEW111"), preferred_id=trip.id).id != trip.id


def test_restore_item_whose_trip_is_deleted_files_it_afresh():
    st = store()
    trip = st.find_or_create(outbound())
    out = st.upsert_item(trip, outbound())
    back = st.upsert_item(trip, inbound())
    row = ledger_row(st, back)
    st.remove_item(back.id)
    st.delete_trip(trip.id)
    target = st.restore_item(back.id)
    assert target.id != trip.id and st.item(back.id).trip_id == target.id
    assert st.item(back.id).removed_at is None
    assert st.led.items.get(row)["trip_item_id"] == back.id
    assert [t.id for t in st.deleted_trips()] == [trip.id]
    assert [i.id for i in st.items(trip.id)] == [out.id]  # the deleted trip keeps its own rows


def test_upsert_revives_a_removed_row_in_place_and_the_log_still_points_at_it():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    row = ledger_row(st, ti)
    before = logged(st, row)
    with util.clock_at(EMAIL + dt.timedelta(minutes=30)):  # after the email, before the next
        st.remove_item(ti.id)
    again = st.upsert_item(st.get(trip.id), outbound(email=EMAIL + dt.timedelta(hours=1)))
    assert again.id == ti.id and again.removed_at is None and again.trip_id == trip.id
    assert [t.id for t in st.all_trips()] == [trip.id] and logged(st, row) == before


def test_an_email_sent_before_a_booking_was_put_away_leaves_it_put_away(tmp_ledger_path):
    """The owner removes a sent booking, or deletes the trip it is in. A late copy of an email
    sent before that — a receipt that names a companion too — joins the companion to the booking
    and leaves it put away, sending nothing; an email sent after it brings the booking back."""
    both = ["Alex Sample", "Sam Sample"]
    put_away_at = EMAIL + dt.timedelta(minutes=30)
    after = EMAIL + dt.timedelta(hours=1)
    for n, how in enumerate(("removed", "trip deleted")):
        led = Ledger.open(tmp_ledger_path.with_name(f"put-away-{n}.db"))
        log = flight_log(led)
        _, (first,) = file_mail(led, items_for(["Alex Sample"]), log=log)
        st = filing_store(led)
        ti = st.item(first.trip_item_id)
        with util.clock_at(put_away_at):
            if how == "removed":
                st.remove_item(ti.id)
            else:
                st.delete_trip(ti.trip_id)
        _, (late,) = file_mail(led, items_for(both, msg="<m2>"), log=log)
        assert (late.action.reason, late.trip_item_id) == ("add_passengers", ti.id), how
        again = st.item(ti.id)
        assert again.passengers == both and st.visible(ti.id) is None, how
        assert again.trip_id == ti.trip_id and (again.removed_at is not None) is (n == 0), how
        assert [r[0] for r in log.sent] == ["create"], how
        newer = items_for([*both, "Robin Sample"], msg="<m3>", email=after)
        _, (back,) = file_mail(led, newer, log=log)
        assert back.trip_item_id == ti.id and st.visible(ti.id) is not None, how
        assert [r[0] for r in log.sent] == ["create", "update"], how


def test_upsert_moves_a_row_out_of_a_deleted_trip():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    with util.clock_at(EMAIL + dt.timedelta(minutes=30)):  # after the email, before the next
        st.delete_trip(trip.id)
    fresh = st.find_or_create(outbound())
    assert fresh.id != trip.id
    again = st.upsert_item(fresh, outbound(email=EMAIL + dt.timedelta(hours=1)))
    assert again.id == ti.id and again.trip_id == fresh.id
    assert [t.id for t in st.all_trips()] == [fresh.id]
    # nothing left to restore: the emptied deleted trip is gone for good
    assert st.deleted_trips() == [] and st.led.trips.get(trip.id) is None


def test_upsert_of_a_cancellation_leaves_a_hidden_row_hidden():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    st.delete_trip(trip.id)
    fresh = st.find_or_create(outbound())  # what the executor makes before upserting
    again = st.upsert_item(fresh, outbound(status="cancelled"))
    assert again.id == ti.id and again.trip_id == trip.id and again.status == "cancelled"
    assert st.get(fresh.id) is None  # the empty trip made for it is dropped again
    assert st.all_trips() == [] and [t.id for t in st.deleted_trips()] == [trip.id]
    # the same for an individually removed booking
    other = st.find_or_create(inbound())
    ri = st.upsert_item(other, inbound())
    st.remove_item(ri.id)
    made = st.find_or_create(inbound())
    again = st.upsert_item(made, inbound(status="cancelled"))
    assert again.id == ri.id and again.removed_at and again.status == "cancelled"
    assert st.get(made.id) is None and st.all_trips() == []


def test_purge_deleted_boundaries():
    st = store()
    led = st.led
    now = dt.datetime(2027, 3, 1, tzinfo=dt.UTC)

    def ago(days):
        return (now - dt.timedelta(days=days)).isoformat(timespec="seconds")

    trip = st.find_or_create(outbound())
    out = st.upsert_item(trip, outbound())
    back = st.upsert_item(trip, inbound())
    row = ledger_row(st, back)
    st.remove_item(back.id)
    st.remove_item(out.id)
    led.trip_items.update(back.id, removed_at=ago(30))
    led.trip_items.update(out.id, removed_at=ago(29))
    far = inbound(dep="2027-08-19T16:45", arr="2027-08-19T18:50")
    gone = st.find_or_create(far)
    gi = st.upsert_item(gone, far)
    st.delete_trip(gone.id)
    led.trips.update(gone.id, deleted_at=ago(31))
    assert st.purge_deleted(now=now) == (2, 1)  # back (30 d) + gi (in the purged trip); gone
    assert st.item(back.id) is None and led.items.get(row)["trip_item_id"] is None
    assert st.item(out.id).removed_at == ago(29) and led.trips.get(trip.id) is not None
    assert st.item(gi.id) is None and led.trips.get(gone.id) is None
    led.trip_items.update(out.id, removed_at=ago(30))
    assert st.purge_deleted(now=now) == (1, 1)  # the last removed row of a live trip takes it
    assert led.trips.get(trip.id) is None


def test_purge_sweeps_zero_row_trips_older_than_a_day():
    st = store()
    now = dt.datetime(2027, 3, 1, tzinfo=dt.UTC)
    kw = dict(title="Ghost", place=None, start_date="2027-01-01", end_date="2027-01-01")
    old, young = st.led.trips.insert(**kw), st.led.trips.insert(**kw)
    iso = lambda delta: (now - delta).isoformat(timespec="seconds")  # noqa: E731
    st.led.trips.update(old, created_at=iso(dt.timedelta(days=1, minutes=1)))
    st.led.trips.update(young, created_at=iso(dt.timedelta(minutes=1)))
    assert st.purge_deleted(now=now) == (0, 1)
    assert st.led.trips.get(old) is None and st.led.trips.get(young) is not None


def test_a_key_a_removed_booking_holds_is_still_its_own():
    """A live booking whose fields come to make the key of a booking the owner removed is
    refused, as for any booking that holds it: settle drops no row, and both stay as they were
    (an email for that day finds the removed one by its key)."""
    st = store()
    trip = st.find_or_create(outbound())
    moved = outbound(dep="2027-06-23T08:20", arr="2027-06-23T11:55")
    gone = st.upsert_item(trip, moved)
    st.remove_item(gone.id)
    live = st.upsert_item(st.get(trip.id), outbound())
    day = {k: moved.payload[k] for k in ("dep_local", "arr_local")}
    st.led.trip_items.update(live.id, payload={**live.payload, **day})
    before = [st.item(gone.id), st.item(live.id)]
    with pytest.raises(KeyCollision) as caught:
        st.settle(live.id)
    assert (caught.value.item_id, caught.value.other_id) == (live.id, gone.id)
    assert [st.item(gone.id), st.item(live.id)] == before
    assert st.find(moved.key).id == gone.id and st.item(gone.id).removed_at is not None


def test_find_by_key_falls_back_to_alt_keys():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    st.led.trip_items.update(ti.id, payload={**ti.payload, "alt_keys": ["lodging|RIGHT1"]})
    assert st.item_by_key("lodging|RIGHT1").id == ti.id
    alias = outbound()
    alias.key = "lodging|RIGHT1"
    assert st.upsert_item(st.get(trip.id), alias).id == ti.id and len(st.items(trip.id)) == 1
    assert st.item_by_key("lodging|NOPE") is None


def test_a_key_another_booking_answers_to_by_an_alias_is_not_taken():
    """Neither settle nor a relink moves a booking onto a key another booking answers to as an
    alias: each refuses as it does for a key another booking has, and both rows stay as they
    were."""
    st = store()
    trip = st.find_or_create(outbound())
    one = st.upsert_item(trip, outbound())
    two = st.upsert_item(st.get(trip.id), inbound())
    moved = outbound(dep="2027-06-24T08:20", arr="2027-06-24T11:55")
    st.led.trip_items.update(two.id, payload={**two.payload, "alt_keys": [moved.key]})
    before = [st.item(one.id), st.item(two.id)]
    with pytest.raises(RuntimeError, match="relink before filing"):
        st.relink_flight(one.item_key, moved.key, 13, {})
    day = {k: moved.payload[k] for k in ("dep_local", "arr_local")}
    st.led.trip_items.update(one.id, payload={**one.payload, **day})
    with pytest.raises(KeyCollision):
        st.settle(one.id)
    assert st.item(one.id).item_key == before[0].item_key and st.item(two.id) == before[1]
    assert st.find(moved.key).id == two.id


def test_recompute_range_keeps_a_trip_that_still_has_removed_rows():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    st.remove_item(ti.id)
    assert st.recompute_range(trip.id) is None and st.led.trips.get(trip.id) is not None
    st.led.trip_items.delete(ti.id)
    assert st.recompute_range(trip.id) is None and st.led.trips.get(trip.id) is None


def test_grouping_helpers():
    flight = outbound()
    assert grouping.item_dates(flight) == (dt.date(2027, 6, 22), dt.date(2027, 6, 22))
    assert grouping.trip_title(flight, HOME) == "Philadelphia · Jun 2027"
    assert grouping.trip_place("Philadelphia · Jun 2027") == "Philadelphia"
    city = grouping.city_from_address
    assert city("100 Example Street, Philadelphia, PA, 00000, US") == "Philadelphia"
    assert city("200 Example Avenue, Philadelphia PA 00000") == "Philadelphia"
    assert city("200 EXAMPLE AVENUE, PHILADELPHIA PA 00000 US") == "Philadelphia"
    assert city("Philadelphia, PA 00000") == "Philadelphia"
    assert city("Des Moines, IA 00000") == "Des Moines"
    assert city("just a street") is None
    # a state or country name is never the city; a street with the city glued on is unknowable
    assert city("100 Example Street Rochester, Minnesota 00000 USA") is None
    assert city("100 Example Street, Minneapolis, Minnesota 00000, United States") == "Minneapolis"
    assert city("12 Rue d'Exemple, 00000 Paris, France") == "Paris"
    assert city("Calle Ejemplo 1, 00000 Madrid, Spain") == "Madrid"
    assert city("Paris, France") == "Paris"
    assert city("Philadelphia, Pennsylvania") == "Philadelphia"
    # the part carrying the postal code is the city line; streets never are
    assert city("Sample Inn, Example Street 77, 00000 Berlin, Germany") == "Berlin"
    assert city("1-1 Example Street, Example-ku, Tokyo 000-0000, Japan") == "Tokyo"
    assert city("Calle Ejemplo Km 9, Zona Hotelera, 00000 Cancún, Q.R., Mexico") == "Cancún"
    assert city("Hotel X, 10000 Example Street, Minneapolis, MN 00000") == "Minneapolis"
    assert city("1 Example Street, London ZZ99 9ZZ, United Kingdom") == "London"
    assert city("300 Example Road, Sampleville PA 00000 US") == "Sampleville"
    # cities that are also state names, "St." cities, unit, suite and terminal lines
    assert city("100 Example Street, New York, NY 00000") == "New York"
    assert city("New York, NY 00000") == "New York"
    assert city("100 Example Street, Washington, DC 00000") == "Washington"
    assert city("100 Example Street, St. Louis, MO 00000") == "St. Louis"
    assert city("100 Example Street, Apt 4B, Minneapolis, MN 00000") == "Minneapolis"
    assert city("100 Example Street, Suite 1000, Minneapolis, MN 00000") == "Minneapolis"
    assert city("100 Example Street, Unit 1000, Chicago, IL 00000") == "Chicago"
    assert city("Example Rent A Car, Terminal 1A, San Francisco, CA 00000") == "San Francisco"
    assert city("Zona Hotelera, Cancún, Q.R. 00000, Mexico") == "Cancún"
    assert city("Hotel X, PO Box 1234, Minneapolis, MN 00000") == "Minneapolis"
    assert city("Example Inn, 2 Example Street, Toronto, ON Z9Z 9Z9, Canada") == "Toronto"
    assert grouping.item_span(flight) == (
        "2027-06-22T08:20",
        "2027-06-22T11:55",
        "America/Chicago",
        "America/New_York",
    )
    car = car_item()
    assert grouping.item_span(car) == ("2027-06-22T13:00", "2027-06-26T10:00", None, None)
    assert grouping.trip_title(car, HOME) == "Philadelphia · Jun 2027"


def test_partial_receipt_keeps_the_stored_arrival():
    st = store()
    trip = st.find_or_create(outbound())
    full = st.upsert_item(trip, outbound())
    assert full.end_at == "2027-06-22T11:55"
    partial = outbound()
    partial.payload.update(arr_local=None, arr_utc=None)
    again = st.upsert_item(st.get(trip.id), partial)
    assert again.id == full.id and again.end_at == "2027-06-22T11:55"
    assert again.payload["arr_local"] == "2027-06-22T11:55"


def test_merge_and_move_take_the_booking_along_and_the_log_still_points_at_it():
    st = store()
    a = st.find_or_create(outbound())
    ta = st.upsert_item(a, outbound())
    row_a = ledger_row(st, ta)
    b_item = inbound(dep="2027-07-19T16:45", arr="2027-07-19T18:50")
    b = st.find_or_create(b_item)
    tb = st.upsert_item(b, b_item)
    row_b = ledger_row(st, tb)
    before = [logged(st, row_a), logged(st, row_b)]
    st.set_notes(b.id, "gate code 4321")
    merged = st.merge(b.id, a.id)
    assert st.item(tb.id).trip_id == a.id and merged.notes == "gate code 4321"
    st.set_notes(a.id, "parking level 2")
    c = st.move_item(tb.id, None)
    assert st.item(tb.id).trip_id == c.id and st.item(ta.id).trip_id == a.id
    assert st.get(a.id).notes == "parking level 2"  # the source keeps its own notes
    # the log is left as it was: its rows point at the bookings wherever they are filed
    assert [logged(st, row_a), logged(st, row_b)] == before
    assert [r["id"] for r in st.led.items.by_trip_item(tb.id)] == [row_b]


def test_zone_helpers_ignore_cancelled_flights():
    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, outbound())
    lax = items_for(
        ["Alex Sample"],
        origin="PHL",
        dest="LAX",
        dep="2027-06-24T09:00",
        arr="2027-06-24T12:30",
        pnr="LAX001",
    )[0]
    lax.payload.update(destination_tz="America/Los_Angeles", destination_city="Los Angeles")
    st.upsert_item(st.get(trip.id), lax)
    st.cancel(lax.key)
    items = st.items(trip.id)
    assert grouping.zone_at(items, "2027-06-25T15:00", HOME) == "America/New_York"


def test_eastbound_overnight_flight_keeps_its_arrival():
    from types import SimpleNamespace

    akl_sfo = SimpleNamespace(
        kind="flight",
        payload={
            "dep_local": "2027-10-15T19:45",
            "arr_local": "2027-10-15T11:55",  # earlier on the clock: it crossed the date line
            "origin_tz": "Pacific/Auckland",
            "destination_tz": "America/Los_Angeles",
        },
    )
    assert grouping.item_span(akl_sfo) == (
        "2027-10-15T19:45",
        "2027-10-15T11:55",
        "Pacific/Auckland",
        "America/Los_Angeles",
    )
    bad_hotel = SimpleNamespace(
        kind="lodging", payload={"check_in": "2027-06-25", "check_out": "2027-06-22"}
    )
    assert grouping.item_span(bad_hotel)[1] is None


def test_two_bookings_on_one_flight_keep_both_confirmation_codes():
    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, items_for(["Casey Sample"], pnr="AAA111")[0])
    ti = st.upsert_item(st.get(trip.id), items_for(["Alex Sample"], pnr="BBB222")[0])
    assert ti.confirmation == "AAA111, BBB222"
    assert st.trip_for_pnr("AAA111") == trip.id and st.trip_for_pnr("BBB222") == trip.id
    assert st.trip_for_pnr("AAA") is None
    again = st.upsert_item(st.get(trip.id), items_for(["Alex Sample"], pnr="BBB222")[0])
    assert again.confirmation == "AAA111, BBB222"


def test_a_late_original_receipt_does_not_revive_a_cancelled_booking():
    import datetime as dt

    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, outbound())
    st.cancel(outbound().key)
    late = outbound()  # items_for stamps the fixture's email date, long before the cancellation
    assert st.upsert_item(st.get(trip.id), late).status == "cancelled"
    rebooked = outbound()
    rebooked.payload["email_date"] = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)).isoformat()
    assert st.upsert_item(st.get(trip.id), rebooked).status == "confirmed"


def test_a_rebooking_sent_after_the_cancellation_revives_the_item_even_in_one_poll():
    """Cancellation sent 3 minutes ago, rebooking 2 minutes ago, both handled now: the item
    compares email dates, not the time the cancellation was processed."""
    st = store()
    now = dt.datetime.now(dt.UTC)
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, outbound())
    cancel = outbound(status="cancelled")
    cancel.payload["email_date"] = (now - dt.timedelta(minutes=3)).isoformat()
    assert st.upsert_item(st.get(trip.id), cancel).status == "cancelled"
    rebooked = outbound()
    rebooked.payload["email_date"] = (now - dt.timedelta(minutes=2)).isoformat()
    assert st.upsert_item(st.get(trip.id), rebooked).status == "confirmed"
    older = outbound()
    older.payload["email_date"] = (now - dt.timedelta(minutes=4)).isoformat()
    st.upsert_item(st.get(trip.id), cancel)
    assert st.upsert_item(st.get(trip.id), older).status == "cancelled"


def test_a_date_line_flight_never_ends_the_trip_before_its_last_item():
    from types import SimpleNamespace

    st = store()
    trip = st.find_or_create(outbound(dep="2027-10-05T08:20", arr="2027-10-05T11:55"))
    st.upsert_item(trip, outbound(dep="2027-10-05T08:20", arr="2027-10-05T11:55"))
    akl_sfo = SimpleNamespace(
        kind="flight",
        key="NZ|9008|2027-10-16|AKL",
        pnr="NZ1",
        passengers=["Alex Sample"],
        status="confirmed",
        payload={
            "dep_local": "2027-10-16T00:20",
            "arr_local": "2027-10-15T16:30",
            "origin_tz": "Pacific/Auckland",
            "destination_tz": "America/Los_Angeles",
            "origin": "AKL",
            "destination": "SFO",
        },
    )
    st.upsert_item(st.get(trip.id), akl_sfo)
    t = st.get(trip.id)
    assert (t.start_date, t.end_date) == ("2027-10-05", "2027-10-16")


def test_grouping_prefers_a_trip_for_the_same_place():
    """Denver Jun 15-25 and Philadelphia Jun 22-24 both cover Jun 23; a Denver booking on Jun 23
    joins Denver even though Philadelphia is the smaller (and otherwise preferred) span."""

    def den(day, pnr, number):
        return outbound(
            dest="DEN",
            dep=f"2027-06-{day}T08:20",
            arr=f"2027-06-{day}T09:30",
            pnr=pnr,
            number=number,
        )

    st = store()
    out_trip = st.find_or_create(outbound())
    st.upsert_item(out_trip, outbound())
    st.upsert_item(st.get(out_trip.id), inbound(dep="2027-06-24T16:45", arr="2027-06-24T18:50"))
    denver = st.find_or_create(
        den("15", "DEN1", "77"), span=(dt.date(2027, 6, 15), dt.date(2027, 6, 25))
    )
    st.upsert_item(denver, den("15", "DEN1", "77"))
    st.upsert_item(st.get(denver.id), den("25", "DEN1", "78"))
    assert denver.id != out_trip.id and st.get(denver.id).title == "Denver"
    assert (st.get(denver.id).start_date, st.get(denver.id).end_date) == (
        "2027-06-15",
        "2027-06-25",
    )
    assert st.find_or_create(den("23", "DEN2", "79")).id == denver.id
    # with only Philadelphia covering the dates, the date rule still files a Denver leg there (a
    # multi-city trip is one trip; the page's move/merge repairs cover the rare miss)
    st3 = store()
    out_trip = st3.find_or_create(outbound())
    st3.upsert_item(out_trip, outbound())
    assert st3.find_or_create(den("23", "DEN3", "80")).id == out_trip.id


def test_a_trip_whose_bookings_are_all_cancelled_is_not_reused():
    st = store()
    out_trip = st.find_or_create(outbound())
    st.upsert_item(out_trip, outbound())
    st.cancel(outbound().key)
    fresh = st.find_or_create(outbound(pnr="NEW1", number="99"))
    assert fresh.id != out_trip.id and fresh.title == "Philadelphia"
    assert st.get(out_trip.id) is not None  # the cancelled trip is kept, never deleted


def test_set_status_cancelled_blocks_older_receipts_but_not_newer():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    now = dt.datetime.now(dt.UTC)
    assert st.set_status(ti.id, "cancelled").status == "cancelled"
    assert st.get(trip.id).cancelled and st.item(ti.id).payload["cancelled_email_date"]
    older = outbound()  # items_for stamps the fixture's email date, long before the click
    assert st.upsert_item(st.get(trip.id), older).status == "cancelled"
    newer = outbound()
    newer.payload["email_date"] = (now + dt.timedelta(minutes=1)).isoformat()
    assert st.upsert_item(st.get(trip.id), newer).status == "confirmed"
    st.set_status(ti.id, "cancelled")
    back = st.set_status(ti.id, "confirmed")
    assert back.status == "confirmed" and "cancelled_email_date" not in back.payload
    assert st.upsert_item(st.get(trip.id), older).status == "confirmed"
    assert len(st.items(trip.id)) == 1


def test_set_status_rejects_other_values():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    with pytest.raises(ValueError):
        st.set_status(ti.id, "bogus")
    with pytest.raises(KeyError):
        st.set_status(999, "cancelled")
    assert st.item(ti.id).status == "confirmed"


def test_set_place_sets_the_city_and_keeps_the_title():
    st = store()
    trip = st.find_or_create(outbound())
    t = st.set_place(trip.id, "  King   of Prussia ")
    assert t.place == "King of Prussia" and t.title == "Philadelphia"
    assert st.set_place(trip.id, "   ").place is None
    assert not hasattr(st, "set_cover") and not hasattr(st, "retry_missing_covers")


def hotel_item(pnr="HOTEL1", dep="2027-06-22", arr="2027-06-25"):
    return items_for(["Alex Sample"], kind="lodging", dep=dep, arr=arr, pnr=pnr)[0]


def test_edit_lodging_fields_and_times():
    st = store()
    trip = st.find_or_create(hotel_item())
    ti = st.upsert_item(trip, hotel_item())
    assert (ti.start_at, ti.end_at) == ("2027-06-22", "2027-06-25")
    out = st.edit_item(
        ti.id,
        {
            "property_name": "  Hilton Example  Riverside ",
            "address": "300 Example Road, Philadelphia, PA 00000",
            "check_in": "2027-06-22T15:00",
            "check_out": "2027-06-26",
            "confirmation_code": "HOTEL1",
        },
        at="2027-01-01T00:00:00+00:00",
    )
    assert out.payload["property_name"] == "Hilton Example Riverside"
    assert out.payload["address"] == "300 Example Road, Philadelphia, PA 00000"
    assert (out.start_at, out.end_at) == ("2027-06-22T15:00", "2027-06-26")
    assert out.confirmation == "HOTEL1" and out.item_key == "lodging|HOTEL1"
    assert set(out.payload["owner_edits"]) == {"property_name", "address", "check_in", "check_out"}
    assert out.payload["owner_edits"]["address"] == "2027-01-01T00:00:00+00:00"
    assert "alt_keys" not in out.payload and st.get(trip.id).end_date == "2027-06-26"
    cleared = st.edit_item(ti.id, {"address": "", "check_out": ""})
    assert cleared.payload["address"] is None and cleared.end_at is None
    assert st.get(trip.id).end_date == "2027-06-22"


def test_edit_car_fields_and_times():
    st = store()
    car = car_item(pickup="Philadelphia Intl Airport, Philadelphia, PA", code="R1")
    trip = st.find_or_create(car)
    ti = st.upsert_item(trip, car)
    out = st.edit_item(
        ti.id,
        {
            "provider": "Hertz",
            "pickup_location": "Philadelphia Intl Airport, Philadelphia, PA",
            "dropoff_location": "400 Example Drive, Philadelphia, PA",
            "pickup_time": "2027-06-22T13:30",
            "dropoff_time": "2027-06-30T09:00",
            "confirmation_code": "R1",
        },
    )
    assert out.payload["provider"] == "Hertz"
    assert out.payload["dropoff_location"] == "400 Example Drive, Philadelphia, PA"
    assert (out.start_at, out.end_at) == ("2027-06-22T13:30", "2027-06-30T09:00")
    assert set(out.payload["owner_edits"]) == {
        "provider",
        "dropoff_location",
        "pickup_time",
        "dropoff_time",
    }
    assert st.get(trip.id).end_date == "2027-06-30"


def test_edit_rejects_removed_rows_and_inverted_stay_dates():
    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, outbound())
    ti = st.upsert_item(st.get(trip.id), hotel_item())
    with pytest.raises(ValueError, match="check-out is before check-in"):
        st.edit_item(ti.id, {"check_in": "2027-06-25", "check_out": "2027-06-22"})
    with pytest.raises(ValueError, match="check-in is not a date"):
        st.edit_item(ti.id, {"check_in": "not a date"})
    with pytest.raises(ValueError):
        st.edit_item(ti.id, {"seats": "1A"})  # not an editable field
    assert st.item(ti.id).payload["check_in"] == "2027-06-22"  # nothing changed
    st.remove_item(ti.id)
    with pytest.raises(ValueError):
        st.edit_item(ti.id, {"property_name": "X"})
    with pytest.raises(KeyError):
        st.edit_item(999, {"property_name": "X"})
    ci = st.upsert_item(st.get(trip.id), car_item())
    with pytest.raises(ValueError, match="drop-off is before pick-up"):
        st.edit_item(ci.id, {"pickup_time": "2027-06-26T10:00", "dropoff_time": "2027-06-22T13:00"})


def test_a_flights_number_day_and_airports_can_be_corrected_and_its_old_key_still_finds_it():
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    back = inbound().payload  # another number, and the route the other way
    later = outbound(dep="2027-06-24T08:20", arr="2027-06-24T11:55").payload
    out = st.edit_item(
        ti.id,
        {
            "flight_number": f"{back['carrier']} {back['flight_number']}",  # keeps its digits
            "origin": back["origin"].lower(),
            "destination": back["destination"].lower(),
            "dep_local": later["dep_local"],
            "arr_local": later["arr_local"],
        },
    )
    assert out.id == ti.id and out.checks == ()
    p = out.payload
    assert (p["flight_number"], p["origin"], p["destination"]) == (
        back["flight_number"],
        back["origin"],
        back["destination"],
    )
    assert p["flight_code"] == f"{p['carrier']}{back['flight_number']}"
    assert p["date_local"] == later["date_local"] and out.start_at == later["dep_local"]
    assert set(p["owner_edits"]) == {
        "flight_number",
        "origin",
        "destination",
        "dep_local",
        "arr_local",
    }
    assert out.item_key == keys.real_key("flight", p, airports=Airports()) != ti.item_key
    assert p["alt_keys"] == [ti.item_key] and st.find(ti.item_key).id == ti.id
    assert st.get(trip.id).end_date == later["date_local"]
    # the first receipt again: the same booking, and the owner's corrections stay
    again = st.upsert_item(st.get(trip.id), outbound())
    assert again.id == ti.id and len(st.items(trip.id)) == 1
    assert (again.item_key, again.payload["origin"]) == (out.item_key, back["origin"])


def test_saving_the_form_is_a_look():
    """A save is a look at the booking, whether it changes anything or not: the soft checks
    raised before it close, a hard one stays until its field is fixed. A save whose look time
    could not be read stamps no look."""
    st = store()
    stay = hotel_item(arr="")  # no check-out: a hard check
    ti = st.upsert_item(st.find_or_create(stay), stay)
    marked = st.mark(ti.id, {"low_confidence": EMAIL.isoformat()})
    assert marked.checks == ("check_out", "low_confidence")
    unread = st.edit_item(ti.id, {"property_name": ti.payload["property_name"]}, seen="")
    assert unread.checks == ("check_out", "low_confidence")
    assert "looked_at" not in unread.payload
    same = st.edit_item(ti.id, {"property_name": ti.payload["property_name"]})
    assert same.checks == ("check_out",) and same.payload["looked_at"]
    assert "owner_edits" not in same.payload  # nothing changed, nothing stamped
    fixed = st.edit_item(ti.id, {"check_out": "2027-06-25"})
    assert fixed.checks == () and set(fixed.payload["owner_edits"]) == {"check_out"}


def test_an_edit_that_would_make_two_real_bookings_one_is_refused_with_a_message():
    st = store()
    trip = st.find_or_create(outbound())
    first = st.upsert_item(trip, outbound())
    second = st.upsert_item(st.get(trip.id), inbound())
    before = [dataclasses.asdict(st.item(i)) for i in (first.id, second.id)]
    p = first.payload
    taken = {k: p[k] for k in ("carrier", "flight_number", "origin", "dep_local")}
    with pytest.raises(ValueError, match="^another booking already has this flight$"):
        st.edit_item(second.id, taken)
    assert [dataclasses.asdict(st.item(i)) for i in (first.id, second.id)] == before
    # two trains of the corpus's, one a number apart
    entry, res = booking("train")
    leg = res["segments"][0]
    one = validated(entry, res)
    other = validated(entry, edit(res, train_number=leg["train_number"] + "1"))
    a = st.upsert_item(st.find_or_create(one), one)
    b = st.upsert_item(st.find_or_create(other), other)
    rows = [dataclasses.asdict(st.item(i)) for i in (a.id, b.id)]
    with pytest.raises(ValueError, match="^another booking already has this train$"):
        st.edit_item(b.id, {"train_number": leg["train_number"]})
    assert [dataclasses.asdict(st.item(i)) for i in (a.id, b.id)] == rows


def test_clearing_the_date_sends_a_booking_to_unsorted_and_a_date_brings_it_back():
    st = store()
    trip = st.find_or_create(outbound())
    flight = st.upsert_item(trip, outbound())
    stay = st.upsert_item(st.get(trip.id), hotel_item())
    p = flight.payload
    undated = st.edit_item(flight.id, {"dep_local": "", "arr_local": ""})
    assert (undated.trip_id, undated.start_at) == (None, None) and "departure" in undated.checks
    # its fields make no key now: a draft key, the key it leaves an alias
    assert keys.is_draft(undated.item_key) and undated.payload["alt_keys"] == [flight.item_key]
    away = st.edit_item(stay.id, {"check_in": ""})
    assert (away.trip_id, away.start_at) == (None, None) and "check_in" in away.checks
    assert [u.id for u in st.unsorted()] == [flight.id, stay.id]
    assert st.get(trip.id) is None  # emptied, so gone
    dated = st.edit_item(flight.id, {"dep_local": p["dep_local"], "arr_local": p["arr_local"]})
    assert dated.trip_id is not None and dated.start_at == p["dep_local"]
    assert dated.checks == () and dated.item_key == flight.item_key
    back = st.edit_item(stay.id, {"check_in": stay.payload["check_in"]})
    assert (back.trip_id is not None, back.start_at) == (True, stay.start_at)
    assert st.unsorted() == []


def test_a_draft_is_not_fixed_into_a_booking_in_recently_deleted():
    """Fixing a draft on the page into a booking the owner has put away is refused, and nothing
    is stored: the draft would vanish into Recently deleted behind a Saved."""
    st = store()
    trip = st.find_or_create(outbound())
    flight = st.upsert_item(trip, outbound())
    draft = st.upsert_item(st.get(trip.id), outbound(number=""))
    assert keys.is_draft(draft.item_key)
    st.remove_item(flight.id)
    before = dataclasses.asdict(st.item(draft.id))
    number = flight.payload["flight_number"]
    with pytest.raises(ValueError) as refused:
        st.edit_item(draft.id, {"flight_number": number})
    assert str(refused.value) == (
        "another booking already has this flight; it is in Recently deleted — restore it first"
    )
    assert dataclasses.asdict(st.item(draft.id)) == before
    st.restore_item(flight.id)
    merged = st.edit_item(draft.id, {"flight_number": number})
    assert merged.id == flight.id and st.item(draft.id) is None


def test_a_draft_and_a_booking_holding_two_flight_log_records_are_never_merged():
    """Merging would leave one of the two records behind in the flight log: settle refuses with
    KeyCollision and changes neither row."""
    st = store()
    trip = st.find_or_create(outbound())
    flight = st.upsert_item(trip, outbound())
    draft = st.upsert_item(st.get(trip.id), outbound(number=""))
    st.sync_done(flight.id, flight_id=1, told=None)
    st.sync_done(draft.id, flight_id=2, told=None)
    payload = {**st.item(draft.id).payload, "flight_number": flight.payload["flight_number"]}
    st.led.trip_items.update(draft.id, payload=payload)  # its fields now make the flight's key
    rows = [dataclasses.asdict(st.item(i)) for i in (flight.id, draft.id)]
    with pytest.raises(KeyCollision):
        st.settle(draft.id)
    assert [dataclasses.asdict(st.item(i)) for i in (flight.id, draft.id)] == rows
    st.sync_done(draft.id, flight_id=1, told=None)  # the same record: one flight, merged
    assert st.item(draft.id) is None and st.item(flight.id).airtrail_flight_id == 1


def test_a_time_typed_with_no_date_is_refused():
    st = store()
    trip = st.find_or_create(outbound())
    flight = st.upsert_item(trip, outbound())
    stay = st.upsert_item(st.get(trip.id), hotel_item(arr=""))  # no check-out held
    p = flight.payload
    for item_id, field, sentence, time in (
        (flight.id, "arr_local", "arrival is not a date", p["arr_local"][10:]),
        (stay.id, "check_out", "check-out is not a date", p["dep_local"][10:]),
    ):
        before = dataclasses.asdict(st.item(item_id))
        with pytest.raises(ValueError, match=f"^{sentence}$") as refused:
            st.edit_item(item_id, {field: time})
        assert refused.value.field == field
        assert dataclasses.asdict(st.item(item_id)) == before


def test_a_typed_flight_number_loses_its_airline_designator_and_must_be_digits():
    carrier = outbound().payload["carrier"]
    number = inbound().payload["flight_number"]
    lettered = number[-1] + carrier[0]  # a designator of a digit and a letter

    def edited(fields: dict):
        st = store()
        ti = st.upsert_item(st.find_or_create(outbound()), outbound())
        return st, ti, lambda: st.edit_item(ti.id, fields)

    for typed, also in (
        (f"{carrier} {number}", {}),
        (f"{carrier}{number}", {}),
        (f"{carrier}{number}", {"carrier": carrier}),
        (f"{lettered} {number}", {}),
        (f"{lettered}{number}", {}),
        (number, {}),
    ):
        _, _, save = edited({"flight_number": typed, **also})
        assert save().payload["flight_number"] == number, typed
    for typed in (number[:-1] + carrier[0], f"{carrier} {number}{carrier}"):
        st, ti, save = edited({"flight_number": typed})
        with pytest.raises(ValueError, match="^Flight number is not a number$") as refused:
            save()
        assert refused.value.field == "flight_number"
        assert st.item(ti.id).payload["flight_number"] == ti.payload["flight_number"]
    # a number an email printed with its designator stays as shown until the owner retypes it
    st = store()
    misread = outbound(number=f"{carrier}{number}")
    ti = st.upsert_item(st.find_or_create(misread), misread)
    assert "flight_number" in ti.checks
    left = st.edit_item(ti.id, {"flight_number": ti.payload["flight_number"]})
    assert left.payload["flight_number"] == f"{carrier}{number}"
    assert "owner_edits" not in left.payload
    fixed = st.edit_item(ti.id, {"flight_number": number})
    assert fixed.payload["flight_number"] == number and "flight_number" not in fixed.checks


def test_an_edited_flight_is_sent_again_and_the_next_receipt_does_not_twin_it():
    """With a flight log kept, the owner moves a sent flight's times by an hour: it waits to be
    sent, a pass updates the one flight there, and the first receipt read again leaves one
    booking with the owner's times and one flight."""
    led = Ledger.memory()
    log = flight_log(led)
    _, (filed,) = file_mail(led, items_for(["Alex Sample"]), log=log)
    st = filing_store(led)
    ti = st.item(filed.trip_item_id)
    assert ti.sync_state == "synced" and len(log.flights) == 1
    p = ti.payload
    times = {"dep_local": an_hour_later(p["dep_local"]), "arr_local": an_hour_later(p["arr_local"])}
    edited = st.edit_item(ti.id, times)
    assert (edited.sync_state, edited.item_key) == ("pending", ti.item_key)
    Syncer(store=st, ledger=led, book=st.book, client=log, refs=None, dry_run=False).run_pass()
    (held,) = log.flights
    assert held["departure"] == edited.payload["dep_utc"] != p["dep_utc"]
    assert [r[0] for r in log.sent] == ["create", "update"]
    assert st.item(ti.id).sync_state == "synced"
    _, (again,) = file_mail(led, items_for(["Alex Sample"], msg="<m2>"), log=log)
    (one,) = filed_bookings(led)
    assert one.id == ti.id == again.trip_item_id and one.sync_state == "synced"
    assert (one.payload["dep_local"], one.payload["dep_utc"]) == (
        times["dep_local"],
        edited.payload["dep_utc"],
    )
    assert len(log.flights) == 1 and [r[0] for r in log.sent] == ["create", "update"]


def test_owner_edit_survives_an_older_email_and_yields_to_a_newer_one():
    st = store()
    first = hotel_item()
    first.payload["address"] = "100 Example Street, Philadelphia, PA 00000"
    trip = st.find_or_create(first)
    ti = st.upsert_item(trip, first)
    edited = st.edit_item(
        ti.id,
        {"address": "", "property_name": "Hilton Example Riverside"},
        at="2027-01-10T00:00:00+00:00",
    )
    assert edited.payload["address"] is None
    older = hotel_item()
    older.payload.update(
        address="100 Example Street, Philadelphia, PA 00000", email_date="2027-01-09T00:00:00+00:00"
    )
    after = st.upsert_item(st.get(trip.id), older)
    assert after.payload["address"] is None
    assert after.payload["property_name"] == "Hilton Example Riverside"
    assert set(after.payload["owner_edits"]) == {"address", "property_name"}
    newer = hotel_item()
    newer.payload.update(
        address="1 New Address, Philadelphia, PA", email_date="2027-01-11T00:00:00+00:00"
    )
    after = st.upsert_item(st.get(trip.id), newer)
    assert after.payload["address"] == "1 New Address, Philadelphia, PA"  # the real change lands
    assert after.payload["property_name"] == "Hilton Example"  # printed by the newer email too
    assert "owner_edits" not in after.payload and len(st.items(trip.id)) == 1


def test_editing_the_confirmation_code_records_an_alias_unless_it_collides():
    st = store()
    trip = st.find_or_create(hotel_item())
    ti = st.upsert_item(trip, hotel_item())
    out = st.edit_item(ti.id, {"confirmation_code": "right-1"})
    assert out.confirmation == "right-1" and out.item_key == "lodging|HOTEL1"
    assert out.payload["alt_keys"] == ["lodging|RIGHT1"]
    fixed = hotel_item(pnr="RIGHT1")  # a later email printing the corrected code: same row
    assert fixed.key == "lodging|RIGHT1"
    again = st.upsert_item(st.get(trip.id), fixed)
    assert again.id == ti.id and len(st.items(trip.id)) == 1
    oi = st.upsert_item(
        st.get(trip.id), hotel_item(pnr="OTHER9", dep="2027-06-23", arr="2027-06-24")
    )
    out = st.edit_item(ti.id, {"confirmation_code": "OTHER9"})  # another booking's code: no alias
    assert out.confirmation == "OTHER9" and out.payload["alt_keys"] == ["lodging|RIGHT1"]
    assert st.item_by_key("lodging|OTHER9").id == oi.id
    out = st.edit_item(ti.id, {"confirmation_code": "hotel1"})  # the row's own key: no alias
    assert out.payload["alt_keys"] == ["lodging|RIGHT1"]
    out = st.edit_item(ti.id, {"confirmation_code": "RIGHT"})  # a second correction replaces it
    assert out.payload["alt_keys"] == ["lodging|RIGHT"] and st.item_by_key("lodging|RIGHT1") is None


def test_edit_keeps_check_in_only_when_check_out_is_the_same_day():
    st = store()
    trip = st.find_or_create(hotel_item())
    ti = st.upsert_item(trip, hotel_item())
    out = st.edit_item(ti.id, {"check_in": "2027-06-22T15:00", "check_out": "2027-06-22"})
    assert out.start_at == "2027-06-22T15:00" and out.end_at is None
    assert (st.get(trip.id).start_date, st.get(trip.id).end_date) == ("2027-06-22", "2027-06-22")


def test_edit_leaves_untouched_times_unstamped():
    st = store()
    trip = st.find_or_create(hotel_item())
    ti = st.upsert_item(trip, hotel_item())
    st.led.trip_items.update(ti.id, payload={**ti.payload, "check_in": "2027-06-22 15:00"})
    out = st.edit_item(ti.id, {"check_in": "2027-06-22T15:00", "address": "1 Example Street"})
    assert set(out.payload["owner_edits"]) == {"address"}  # same instant, different spelling
    assert out.start_at == "2027-06-22T15:00"


def test_text_fields_are_clipped_to_the_form_limits():
    st = store()
    trip = st.find_or_create(hotel_item())
    ti = st.upsert_item(trip, hotel_item())
    assert len(st.rename(trip.id, "x" * 300).title) == 80
    assert len(st.set_place(trip.id, "y" * 300).place) == 80
    out = st.edit_item(
        ti.id, {"property_name": "n" * 300, "address": "a" * 300, "confirmation_code": "c" * 300}
    )
    assert len(out.payload["property_name"]) == 120 and len(out.payload["address"]) == 200
    assert len(out.payload["confirmation_code"]) == 40 and len(out.confirmation) == 40


def test_deleted_counts_without_loading_rows():
    st = store()
    trip = st.find_or_create(outbound())
    a = st.upsert_item(trip, outbound())
    st.upsert_item(trip, inbound())
    assert st.deleted_counts() == (0, 0)
    st.remove_item(a.id)
    st.delete_trip(trip.id)
    assert st.deleted_counts() == (1, 1)


def test_upsert_revive_is_atomic(monkeypatch):
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    st.delete_trip(trip.id)
    fresh = st.find_or_create(outbound())

    def boom(*a, **k):
        raise RuntimeError("disk full")

    # the booking is written back into the fresh trip, then the trips' dates break
    monkeypatch.setattr(st, "recompute_range", boom)
    with pytest.raises(RuntimeError):
        st.upsert_item(fresh, outbound())
    kept = st.item(ti.id)
    assert kept.trip_id == trip.id  # rolled back
    assert [t.id for t in st.deleted_trips()] == [trip.id]


def test_restore_item_falls_back_to_a_new_trip_when_grouping_cannot_date_it(monkeypatch):
    st = store()
    trip = st.find_or_create(outbound())
    ti = st.upsert_item(trip, outbound())
    st.remove_item(ti.id)
    st.delete_trip(trip.id)
    monkeypatch.setattr(
        st, "find_or_create", lambda *a, **k: (_ for _ in ()).throw(ValueError("no dates"))
    )
    target = st.restore_item(ti.id)
    assert target.id != trip.id and target.title == "Philadelphia"
    assert (target.start_date, target.end_date) == ("2027-06-22", "2027-06-22")
    assert st.item(ti.id).trip_id == target.id and st.item(ti.id).removed_at is None


def test_alias_lookup_survives_a_malformed_payload():
    st = store()
    trip = st.find_or_create(outbound())
    st.upsert_item(trip, outbound())
    st.led.conn.execute("UPDATE trip_items SET payload_json='not json'")
    assert st.item_by_key("lodging|NOPE") is None


def test_purge_reads_the_held_clock():
    st = store()
    held = dt.datetime(2027, 3, 1, tzinfo=dt.UTC)
    trip = st.find_or_create(outbound())
    out = st.upsert_item(trip, outbound())
    with util.clock_at(held):
        st.remove_item(out.id)
    with util.clock_at(held + dt.timedelta(days=29)):
        assert st.purge_deleted() == (0, 0)
    with util.clock_at(held + dt.timedelta(days=30)):
        assert st.purge_deleted() == (1, 1)
