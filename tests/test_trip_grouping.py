# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""End-to-end trip grouping through the booking plan and the executor, with trips in the ledger
and, where a flight log is kept, an AirTrail held in memory that the pass after each message
brings in step."""

import datetime as dt

from helpers.flightlog import flight_log
from test_planner import EMAIL, aboard, items_for, run, sent, store

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.trips import grouping


def run_plan(led, items, airtrail=None, doc="booking"):
    """One message's bookings planned and filed (`test_planner.run`), then, with a flight log
    `airtrail`, the pass a message ends with. Returns the outcomes and the store."""
    _, outcomes = run(led, items, doc, log=airtrail)
    return outcomes, store(led)


def relative_round_trip():
    out = items_for(
        ["Casey Sample"], dep="2027-07-03T07:00", arr="2027-07-03T10:35", pnr="RU9X7Q"
    )
    back = items_for(
        ["Casey Sample"],
        origin="PHL",
        dest="MSP",
        dep="2027-07-06T13:00",
        arr="2027-07-06T15:05",
        pnr="RU9X7Q",
        number="9848",
    )
    hotel = items_for(
        ["Casey Sample"], kind="lodging", dep="2027-07-03", arr="2027-07-06", pnr="97531975"
    )
    return out + back + hotel


def test_round_trip_and_hotel_land_in_one_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    outcomes, st = run_plan(led, relative_round_trip())
    assert all(o.result == "filed" for o in outcomes)
    assert len({o.trip_id for o in outcomes}) == 1 and outcomes[0].trip_title == "Philadelphia"
    trip = st.get(outcomes[0].trip_id)
    assert (trip.title, trip.start_date, trip.end_date) == (
        "Philadelphia",
        "2027-07-03",
        "2027-07-06",
    )
    items = st.items(trip.id)
    assert [i.kind for i in items] == ["flight", "lodging", "flight"]
    assert all(i.airtrail_flight_id is None for i in items)
    assert all(o.trip_item_id for o in outcomes)
    rows = led.items.by_message(led.items.get(outcomes[0].item_id)["message_id"])
    assert [r["outcome"] for r in rows] == ["filed"] * 3
    assert [r["trip_item_id"] for r in rows] == [o.trip_item_id for o in outcomes]


def test_later_email_with_same_pnr_reuses_and_extends_the_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    first, st = run_plan(led, relative_round_trip()[:1])
    assert st.get(first[0].trip_id).end_date == "2027-07-03"
    back = items_for(
        ["Casey Sample"],
        origin="PHL",
        dest="MSP",
        dep="2027-07-09T13:00",
        arr="2027-07-09T15:05",
        pnr="RU9X7Q",
        number="9848",
    )
    outcomes, st = run_plan(led, back)
    assert outcomes[0].trip_id == first[0].trip_id
    assert st.get(first[0].trip_id).end_date == "2027-07-09" and len(st.all_trips()) == 1


def test_owner_round_trip_links_both_airtrail_flights_into_one_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    out = items_for(
        ["Alex Sample", "Sam Sample"],
        dep="2027-10-26T09:00",
        arr="2027-10-26T10:05",
        pnr="QT9W8Z",
        number="9120",
        dest="DEN",
    )
    back = items_for(
        ["Alex Sample", "Sam Sample"],
        origin="DEN",
        dest="MSP",
        dep="2027-10-28T17:30",
        arr="2027-10-28T20:25",
        pnr="QT9W8Z",
        number="9121",
    )
    outcomes, st = run_plan(led, out + back, airtrail=at)
    assert [o.sync for o in outcomes] == ["synced", "synced"]
    trip = st.get(outcomes[0].trip_id)
    assert (trip.title, trip.start_date, trip.end_date) == ("Denver", "2027-10-26", "2027-10-28")
    assert [i.airtrail_flight_id for i in st.items(trip.id)] == [f["id"] for f in at.flights]
    assert len(at.flights) == 2


def test_return_leg_under_another_pnr_joins_the_outbound_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    out = items_for(
        ["Alex Sample"], dep="2027-09-16T10:25", arr="2027-09-16T13:58", pnr="QTWZSV"
    )
    first, st = run_plan(led, out, airtrail=at)
    assert st.get(first[0].trip_id).title == "Philadelphia"
    back = items_for(
        ["Alex Sample"],
        origin="PHL",
        dest="MSP",
        dep="2027-09-21T16:45",
        arr="2027-09-21T18:50",
        pnr="Q9TW8Z",
        number="9246",
    )
    outcomes, st = run_plan(led, back, airtrail=at)
    assert outcomes[0].trip_id == first[0].trip_id and len(st.all_trips()) == 1
    assert st.get(first[0].trip_id).end_date == "2027-09-21"


def test_family_receipt_first_then_owners_share_one_trip_item(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    kw = dict(dep="2027-09-21T08:20", arr="2027-09-21T11:55", pnr="Q9TW8Z", number="9246")
    first, st = run_plan(
        led, items_for(["SAM SAMPLE"], seat_by={"SAM SAMPLE": "16B"}, **kw), airtrail=at
    )
    assert (first[0].result, first[0].sync) == ("filed", "off") and at.flights == []
    ti1 = st.item(first[0].trip_item_id)
    assert ti1.passengers == ["SAM SAMPLE"] and ti1.airtrail_flight_id is None
    second, st = run_plan(
        led, items_for(["ALEX SAMPLE"], seat_by={"ALEX SAMPLE": "16A"}, **kw), airtrail=at
    )
    assert (second[0].result, second[0].sync) == ("updated", "synced")
    assert second[0].trip_id == first[0].trip_id and second[0].trip_item_id == ti1.id
    ti2 = st.item(ti1.id)
    (held,) = at.flights
    assert ti2.airtrail_flight_id == held["id"]
    assert ti2.passengers == ["SAM SAMPLE", "ALEX SAMPLE"]
    assert ti2.payload["seat_by_passenger"] == {"SAM SAMPLE": "16B", "ALEX SAMPLE": "16A"}
    assert [r["outcome"] for r in led.items.by_trip_item(ti1.id)] == ["filed", "updated"]
    assert len(st.all_trips()) == 1 and len(st.items(ti2.trip_id)) == 1


def test_reschedule_across_the_range_rekeys_the_item_and_extends_the_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    first, st = run_plan(led, items_for(["Alex Sample"]), airtrail=at)
    trip = st.get(first[0].trip_id)
    assert (trip.start_date, trip.end_date) == ("2027-06-22", "2027-06-22")
    moved = items_for(
        ["Alex Sample"], dep="2027-06-30T08:20", arr="2027-06-30T11:55", msg="<m2>"
    )
    outcomes, st = run_plan(led, moved, airtrail=at, doc="change")
    assert (outcomes[0].result, outcomes[0].action.reason) == ("updated", "rescheduled")
    assert sent(at) == ["create", "update"] and at.sent[1][1] == at.flights[0]["id"]
    items = st.items(trip.id)
    assert len(items) == 1 and items[0].id == first[0].trip_item_id
    assert items[0].item_key == "DL|1234|2027-06-30|MSP" and items[0].start_at == "2027-06-30T08:20"
    assert st.get(trip.id).end_date == "2027-06-30"


def test_cancellation_marks_the_trip_item_cancelled_but_keeps_it(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    first, st = run_plan(led, items_for(["Alex Sample"]), airtrail=at)
    outcomes, st = run_plan(
        led, items_for(["Alex Sample"], msg="<m2>"), airtrail=at, doc="cancellation"
    )
    assert (outcomes[0].result, outcomes[0].action.reason) == ("updated", "cancel")
    items = st.items(first[0].trip_id)
    assert [i.status for i in items] == ["cancelled"] and st.get(first[0].trip_id).cancelled
    hotel, st = run_plan(led, items_for(["Casey Sample"], kind="lodging", pnr="H1"))
    cancel, st = run_plan(
        led,
        items_for(["Casey Sample"], kind="lodging", pnr="H1", msg="<m3>"),
        doc="cancellation",
    )
    assert cancel[0].action.reason == "cancel"
    assert st.item(hotel[0].trip_item_id).status == "cancelled"


def test_adopted_flight_joins_a_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    (item,) = items_for(["Alex Sample"])
    by_hand = at.create_flight(  # entered there by hand before the email came
        {"date": item.payload["date_local"], "flightNumber": item.payload["flight_code"]}
    )
    at.requests.clear()
    outcomes, st = run_plan(led, [item], airtrail=at)
    assert (outcomes[0].result, outcomes[0].sync) == ("filed", "synced") and outcomes[0].trip_id
    assert st.items(outcomes[0].trip_id)[0].airtrail_flight_id == by_hand
    assert at.sent == [] and len(at.flights) == 1


def test_rebooking_a_cancelled_code_never_leaves_an_empty_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    first, st = run_plan(
        led,
        items_for(
            ["Casey Sample"], kind="lodging", pnr="H1", dep="2027-06-22", arr="2027-06-25"
        ),
    )
    hotel_trip = first[0].trip_id
    run_plan(
        led,
        items_for(
            ["Casey Sample"],
            kind="lodging",
            pnr="H1",
            dep="2027-06-22",
            arr="2027-06-25",
            msg="<m2>",
        ),
        doc="cancellation",
    )
    # the same confirmation code comes back, re-booked four months later: no trip covers it
    import datetime as dt

    rebooked = items_for(
        ["Casey Sample"],
        kind="lodging",
        pnr="H1",
        dep="2027-10-12",
        arr="2027-10-15",
        msg="<m3>",
    )
    # a re-booking email is newer than the cancellation (an older receipt would not revive it)
    rebooked[0].payload["email_date"] = (
        dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)
    ).isoformat()
    outcomes, st = run_plan(led, rebooked)
    assert outcomes[0].result == "updated"
    assert all(len(st.items(t.id)) >= 1 for t in st.all_trips())
    ti = st.item_by_key("lodging|H1")
    assert ti.trip_id == hotel_trip and ti.start_at == "2027-10-12" and ti.status == "confirmed"
    assert st.get(hotel_trip).start_date == "2027-10-12" and outcomes[0].trip_id == hotel_trip


def test_a_booking_stays_in_the_trip_that_holds_it(tmp_ledger_path):
    """A booking stays in the trip that holds it, whatever trip the rest of its email goes to."""
    led = Ledger.open(tmp_ledger_path)
    hotel = items_for(
        ["Casey Sample"], kind="lodging", pnr="H1", dep="2027-06-22", arr="2027-06-25"
    )[0]
    first, st = run_plan(led, [hotel])
    a_id = first[0].trip_id
    other = st.new_trip("Other", *grouping.item_dates(hotel))
    st.move_item(first[0].trip_item_id, other.id)  # the owner files it elsewhere on the page
    assert st.get(a_id) is None
    # the same stay again, changed, in an email whose flight lands in a trip of its own
    flight = items_for(
        ["Casey Sample"], pnr="JQ1111", dep="2027-07-03T07:00", arr="2027-07-03T10:35", msg="<m2>"
    )
    longer = items_for(
        ["Casey Sample"], kind="lodging", pnr="H1", dep="2027-06-22", arr="2027-06-30", msg="<m2>"
    )
    outcomes, st = run_plan(led, flight + longer)
    assert [o.result for o in outcomes] == ["filed", "updated"]
    assert outcomes[1].trip_id == other.id != outcomes[0].trip_id
    assert st.item(first[0].trip_item_id).trip_id == other.id
    assert led.items.get(outcomes[1].item_id)["trip_item_id"] == first[0].trip_item_id


def test_one_travellers_cancellation_drops_only_that_traveller(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    booked = items_for(["Alex Sample", "Sam Sample"])
    outcomes, st = run_plan(led, booked, airtrail=at)
    assert (outcomes[0].result, outcomes[0].sync) == ("filed", "synced")
    assert aboard(at.flights[0]) == [("me", None), ("Sam Sample", None)]
    outcomes, st = run_plan(
        led, items_for(["Sam Sample"], status="cancelled"), airtrail=at, doc="cancellation"
    )
    o = outcomes[0]
    assert (o.action.kind, o.action.reason, o.action.remove_passengers) == (
        "update",
        "remove_passengers",
        ["Sam Sample"],
    )
    assert (o.result, o.sync) == ("updated", "synced")  # Alex still flies
    (held,) = at.flights
    assert aboard(held) == [("me", None)]
    ti = st.item_by_key(booked[0].key)
    assert ti.status == "confirmed" and ti.passengers == ["Alex Sample"]
    # the whole party cancelled -> the flight is cancelled
    outcomes, st = run_plan(
        led,
        items_for(["Alex Sample", "Sam Sample"], status="cancelled"),
        airtrail=at,
        doc="cancellation",
    )
    assert outcomes[0].action.reason == "cancel" and outcomes[0].sync == "synced"
    assert st.item_by_key(booked[0].key).status == "cancelled"
    assert at.flights[0]["note"].startswith("CANCELLED ") and len(at.flights) == 1


def test_a_late_original_receipt_after_a_change_does_not_rewind_it(tmp_ledger_path):
    import datetime as dt

    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    e0 = EMAIL
    booked = items_for(["Alex Sample"], email=e0)
    run_plan(led, booked, airtrail=at)
    change = items_for(
        ["Alex Sample"],
        dep="2027-06-22T07:40",
        arr="2027-06-22T11:15",
        email=e0 + dt.timedelta(hours=1),
    )
    outcomes, st = run_plan(led, change, airtrail=at, doc="change")
    assert (outcomes[0].result, outcomes[0].action.reason) == ("updated", "changed")
    sam = items_for(
        ["Sam Sample"],
        dep="2027-06-22T07:40",
        arr="2027-06-22T11:15",
        email=e0 + dt.timedelta(hours=2),
    )
    outcomes, _ = run_plan(led, sam, airtrail=at)
    assert outcomes[0].action.reason == "add_passengers"
    # a new name does not make a changed booking one a late copy may rewind
    assert st.item_by_key(booked[0].key).payload["change_recorded"] is True
    late = items_for(["Jamie Sample"], email=e0)  # the original 08:20 receipt, forwarded late
    outcomes, _ = run_plan(led, late, airtrail=at)
    assert outcomes[0].action.reason == "add_passengers" and outcomes[0].action.changes == {}
    ti = st.item_by_key(booked[0].key)
    assert ti.payload["dep_local"] == "2027-06-22T07:40" and outcomes[0].sync == "synced"
    assert sorted(ti.passengers) == ["Alex Sample", "Jamie Sample", "Sam Sample"]
    (held,) = at.flights
    assert held["departure"] == ti.payload["dep_utc"] != booked[0].payload["dep_utc"]
    updates = [r[2] for r in at.sent if r[0] == "update"]
    assert len(updates) == 3 and all(
        u.get("departure") != booked[0].payload["dep_utc"] for u in updates
    )


def test_a_rescheduled_flight_the_owner_is_not_on_moves_its_booking_instead_of_adding_one(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    first = items_for(["Casey Sample"], pnr="JQ1111")
    outcomes, st = run_plan(led, first)
    assert outcomes[0].action.kind == "file"
    moved = items_for(
        ["Casey Sample"], pnr="JQ1111", dep="2027-06-23T08:20", arr="2027-06-23T11:55"
    )
    outcomes, st = run_plan(led, moved, doc="change")
    o = outcomes[0]
    assert (o.action.kind, o.action.reason, o.result) == ("update", "rescheduled", "updated")
    items = [i for i in st.items(o.trip_id) if i.status != "cancelled"]
    assert [i.item_key for i in items] == [moved[0].key]
    assert items[0].start_at.startswith("2027-06-23")
    # the one booking answers to the key it left too
    assert st.find(first[0].key).id == items[0].id
    assert led.conn.execute("SELECT COUNT(*) FROM trip_items").fetchone()[0] == 1


def test_a_duplicate_receipt_refiles_a_flight_whose_trip_link_was_lost(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    booked = items_for(["Alex Sample"])
    outcomes, st = run_plan(led, booked, airtrail=at)
    ti = st.item_by_key(booked[0].key)
    led.trip_items.delete(ti.id)  # the booking's row is lost
    st.recompute_range(ti.trip_id)
    outcomes, st = run_plan(led, items_for(["Alex Sample"]), airtrail=at)
    # its next copy files it again, and takes up the flight the flight log already holds
    assert (outcomes[0].result, outcomes[0].sync) == ("filed", "synced") and outcomes[0].trip_id
    again = st.item_by_key(booked[0].key)
    assert again is not None and len(at.flights) == 1 and sent(at) == ["create"]
    assert again.airtrail_flight_id == at.flights[0]["id"]
    # an item the owner removed on the page stays removed
    st.remove_item(again.id)
    outcomes, st = run_plan(led, items_for(["Alex Sample"]), airtrail=at)
    assert outcomes[0].result == "duplicate" and st.item_by_key(booked[0].key) is None


def test_a_change_email_revives_a_removed_booking_in_its_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    first = items_for(["Casey Sample"], pnr="JQ2222")
    outcomes, st = run_plan(led, first)
    ti = st.item_by_key(first[0].key)
    with util.clock_at(EMAIL + dt.timedelta(minutes=30)):  # after the email, before the change
        st.remove_item(ti.id)
    assert st.all_trips() == []
    moved = items_for(
        ["Casey Sample"],
        pnr="JQ2222",
        dep="2027-06-23T08:20",
        arr="2027-06-23T11:55",
        email=EMAIL + dt.timedelta(hours=1),
    )
    outcomes, st = run_plan(led, moved, doc="change")
    o = outcomes[0]
    assert o.result == "updated" and o.trip_id == ti.trip_id and o.trip_title == "Philadelphia"
    back = st.item_by_key(moved[0].key)
    assert back is not None and back.id == ti.id and back.removed_at is None
    assert [t.id for t in st.all_trips()] == [ti.trip_id] and st.removed_items() == []
    assert led.conn.execute("SELECT COUNT(*) FROM trip_items").fetchone()[0] == 1


def test_a_re_forward_never_resurrects_a_deleted_trip(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    at = flight_log(led)
    booked = items_for(["Alex Sample"])
    outcomes, st = run_plan(led, booked, airtrail=at)
    st.delete_trip(outcomes[0].trip_id)
    outcomes, st = run_plan(led, items_for(["Alex Sample"]), airtrail=at)
    o = outcomes[0]
    assert o.result == "duplicate" and o.trip_id is None and o.trip_title is None
    assert st.item_by_key(booked[0].key) is None and st.all_trips() == []
    assert len(st.deleted_trips()) == 1 and len(at.flights) == 1 and sent(at) == ["create"]


def test_a_cancellation_for_a_deleted_trip_cancels_in_place(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    first = items_for(["Casey Sample"], pnr="JQ3333")
    outcomes, st = run_plan(led, first)
    tid = outcomes[0].trip_id
    st.delete_trip(tid)
    outcomes, st = run_plan(
        led, items_for(["Casey Sample"], pnr="JQ3333", status="cancelled"), doc="cancellation"
    )
    o = outcomes[0]
    assert (o.action.kind, o.action.reason, o.result) == ("update", "cancel", "updated")
    assert st.all_trips() == [] and [t.id for t in st.deleted_trips()] == [tid]
    ti = st.item(led.trip_items.by_key(first[0].key)["id"])
    assert ti.trip_id == tid and ti.status == "cancelled"
    # named for Telegram, never linked (the page would 404); the log points at the booking
    assert o.trip_id is None and o.trip_title == "Philadelphia" and led.trips.get(tid) is not None
    assert led.items.get(o.item_id)["trip_item_id"] == ti.id
    restored = st.restore_trip(tid)
    assert restored is not None and restored.id == tid
    assert [i.status for i in st.items(tid)] == ["cancelled"]


def test_with_no_home_airports_a_trip_is_titled_by_its_first_destination():
    (it,) = items_for(
        ["Alex Sample"], dest="BOS", dep="2027-06-22T08:20", arr="2027-06-22T12:10", pnr="QXR987"
    )
    assert grouping.trip_title(it, ()) == "Boston · Jun 2027"
