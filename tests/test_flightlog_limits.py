# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The known limits of matching an email to a booking, one test each, with a flight log held in
memory: what the trips page then holds, what the flight log holds, and that the page action
`docs/airtrail.md` names for it ("Known limits") leaves the flight log right. Every booking is
the invented family's (`tests/corpus/FAMILY.md`), every number begins with 98 and every day is in
2027. Each message's bookings go through the plan and the executor, then the pass every message
ends with (`test_planner.run`)."""

import datetime as dt

from helpers.airports import airports
from helpers.flightlog import Scripted, flight_log
from test_planner import BOOK, aboard, items_for, run, store

from tripbox.ledger import Ledger
from tripbox.parse.models import Extraction, Reservation
from tripbox.trips.store import TripItem
from tripbox.validate.rules import validate_extraction
from tripbox.write.sync import Syncer

ME, SAM, ROBIN = "Alex Sample", "Sam Sample", "Robin Sample"
STRANGER = "Pat Example"  # a relative the travellers file does not name
FIRST = dt.datetime(2027, 1, 4, 15, 0, tzinfo=dt.UTC)  # when the first email of a story is sent


def sent_at(n: int) -> dt.datetime:
    """When the `n`-th email of a story is sent: a day after the one before."""
    return FIRST + dt.timedelta(days=n)


def a_pass(led: Ledger, log: Scripted) -> None:
    """The pass the next message, or the worker's next beat, runs."""
    st = store(led)
    Syncer(store=st, ledger=led, book=st.book, client=log, refs=None, dry_run=False).run_pass()


def held(log: Scripted) -> list[tuple]:
    """What the flight log holds: each flight's number, day, who is aboard (the owner as "me")
    and whether its note begins with the cancellation line."""
    return [
        (f["flightNumber"], f["date"], aboard_names(f), f["note"].startswith("CANCELLED "))
        for f in log.flights
    ]


def aboard_names(flight: dict) -> list[str]:
    """Who is aboard: the owner as "me" first, then each guest in name order."""
    names = [who for who, _seat in aboard(flight)]
    return [who for who in names if who == "me"] + sorted(w for w in names if w != "me")


def live(led: Ledger) -> list[TripItem]:
    """The bookings the owner can see, oldest first."""
    st = store(led)
    rows = led.conn.execute("SELECT id FROM trip_items ORDER BY id").fetchall()
    return [ti for (iid,) in rows if (ti := st.visible(iid)) is not None]


def flight(passengers, number, day, *, n, code, dep="08:20", arr="11:55", **kw):
    """The bookings of one email, the `n`-th of its story: flight DL `number` from MSP to PHL on
    `day` under `code`, leaving at `dep` (None: the day alone is printed) and landing at `arr`
    (None: not printed)."""
    return items_for(
        passengers,
        number=number,
        pnr=code,
        dep=f"{day}T{dep}" if dep is not None else day,
        arr=f"{day}T{arr}" if arr is not None else None,
        msg=f"<{code.lower()}-{n}@example.com>",
        email=sent_at(n),
        **kw,
    )


def test_a_receipt_under_another_code_for_a_flight_the_booking_left_joins_the_moved_booking(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, flight([ME, SAM], "9861", "2027-04-05", n=0, code="QRS987"), log=log)
    # the airline moves both a day later; the flight they left still flies, and Robin, booked
    # apart under another code, is on it
    run(led, flight([ME, SAM], "9861", "2027-04-06", n=1, code="QRS987"), doc="change", log=log)
    run(led, flight([ROBIN], "9861", "2027-04-05", n=2, code="RTU654"), log=log)
    (booking,) = live(led)
    assert booking.item_key == "DL|9861|2027-04-06|MSP"
    assert sorted(booking.passengers) == [ME, ROBIN, SAM]
    assert held(log) == [("DL9861", "2027-04-06", ["me", ROBIN, SAM], False)]
    # the page: Robin is taken off the booking, and the flight log follows
    store(led).edit_item(booking.id, {"passengers": [ME, SAM]})
    a_pass(led, log)
    assert held(log) == [("DL9861", "2027-04-06", ["me", SAM], False)]


def test_a_change_naming_some_travellers_of_a_one_code_booking_moves_them_all(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, flight([ME, SAM], "9862", "2027-04-12", n=0, code="QST876"), log=log)
    # a notice that names Sam alone moves Sam to the next day, and the owner goes too
    run(led, flight([SAM], "9862", "2027-04-13", n=1, code="QST876"), doc="change", log=log)
    (booking,) = live(led)
    assert (booking.item_key, sorted(booking.passengers)) == ("DL|9862|2027-04-13|MSP", [ME, SAM])
    assert held(log) == [("DL9862", "2027-04-13", ["me", SAM], False)]
    # the page: the booking is put back on its day, Sam taken off it; the flight log follows
    store(led).edit_item(
        booking.id,
        {"dep_local": "2027-04-12T08:20", "arr_local": "2027-04-12T11:55", "passengers": [ME]},
    )
    a_pass(led, log)
    assert held(log) == [("DL9862", "2027-04-12", ["me"], False)]


def test_a_change_mailed_per_code_files_the_new_leg_and_leaves_the_old_booking_live(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    # the owner and Sam booked apart, on one flight: one booking holding both codes
    run(led, flight([ME], "9863", "2027-04-19", n=0, code="QTU765"), log=log)
    run(led, flight([SAM], "9863", "2027-04-19", n=1, code="QTV876"), log=log)
    (both,) = live(led)
    # the airline mails the move per code, Sam's notice first
    run(led, flight([SAM], "9863", "2027-04-20", n=2, code="QTV876"), doc="change", log=log)
    run(led, flight([ME], "9863", "2027-04-20", n=3, code="QTU765"), doc="change", log=log)
    old, new = live(led)
    assert old.id == both.id and old.item_key == "DL|9863|2027-04-19|MSP"
    assert old.status == "confirmed"
    assert sorted(old.passengers) == [ME, SAM] and sorted(new.passengers) == [ME, SAM]
    assert new.item_key == "DL|9863|2027-04-20|MSP"
    assert held(log) == [
        ("DL9863", "2027-04-19", ["me", SAM], False),
        ("DL9863", "2027-04-20", ["me", SAM], False),
    ]
    # the page: the old booking is cancelled; its flight there gets the cancellation line
    store(led).set_status(old.id, "cancelled")
    a_pass(led, log)
    assert held(log) == [
        ("DL9863", "2027-04-19", ["me", SAM], True),
        ("DL9863", "2027-04-20", ["me", SAM], False),
    ]


def test_a_new_leg_naming_a_removed_traveller_and_a_stranger_moves_the_whole_booking(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, flight([ME, SAM], "9864", "2027-04-26", n=0, code="QUV987"), log=log)
    # one message: Sam's seat on the flight is cancelled, and Sam goes on a later flight that
    # day with a relative the booking does not hold
    left = flight([SAM], "9864", "2027-04-26", n=1, code="QUV987", status="cancelled")
    onward = flight(
        [SAM, STRANGER], "9865", "2027-04-26", n=1, code="QUV987", dep="13:20", arr="16:55"
    )
    run(led, left + onward, doc="change", log=log)
    (booking,) = live(led)
    assert booking.item_key == "DL|9865|2027-04-26|MSP" and booking.status == "confirmed"
    assert ME in booking.passengers
    assert [(n, d, who) for n, d, who, _ in held(log)] == [("DL9865", "2027-04-26", ["me", SAM])]
    # the page: the booking is put back on the owner's flight, Sam taken off it
    store(led).edit_item(
        booking.id,
        {
            "flight_number": "9864",
            "dep_local": "2027-04-26T08:20",
            "arr_local": "2027-04-26T11:55",
            "passengers": [ME],
        },
    )
    a_pass(led, log)
    assert held(log) == [("DL9864", "2027-04-26", ["me"], False)]


def test_everyone_cancelled_beside_a_new_leg_for_the_owner_ends_moved_and_cancelled(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, flight([ME, SAM], "9866", "2027-05-03", n=0, code="QVW876"), log=log)
    # one message: both seats on the flight are cancelled, and the owner alone goes on a later
    # flight that day
    left = flight([ME, SAM], "9866", "2027-05-03", n=1, code="QVW876", status="cancelled")
    onward = flight([ME], "9867", "2027-05-03", n=1, code="QVW876", dep="13:20", arr="16:55")
    run(led, left + onward, doc="change", log=log)
    (booking,) = live(led)
    assert (booking.item_key, booking.status) == ("DL|9867|2027-05-03|MSP", "cancelled")
    assert held(log) == [("DL9867", "2027-05-03", ["me", SAM], True)]
    # the page: the booking is marked confirmed and Sam taken off it; the flight log follows
    st = store(led)
    st.set_status(booking.id, "confirmed")
    st.edit_item(booking.id, {"passengers": [ME]})
    a_pass(led, log)
    (only,) = log.flights
    assert held(log) == [("DL9867", "2027-05-03", ["me"], False)]
    assert "reinstated " in only["note"]


def test_a_flight_typed_in_without_its_code_is_twinned_by_a_later_change(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    typed = store(led).add_by_hand(
        "flight",
        {
            "carrier": "DL",
            "flight_number": "9868",
            "origin": "MSP",
            "destination": "PHL",
            "dep_local": "2027-05-10T08:20",
            "arr_local": "2027-05-10T11:55",
            "passengers": [ME],
        },
        at=sent_at(0).isoformat(),
    )
    a_pass(led, log)
    # the airline moves it a day later, under the code the owner never typed in
    run(led, flight([ME], "9868", "2027-05-11", n=1, code="QWX765"), doc="change", log=log)
    first, second = live(led)
    assert first.id == typed.id and first.item_key == "DL|9868|2027-05-10|MSP"
    assert second.item_key == "DL|9868|2027-05-11|MSP"
    assert held(log) == [
        ("DL9868", "2027-05-10", ["me"], False),
        ("DL9868", "2027-05-11", ["me"], False),
    ]
    # the page: the booking typed in is cancelled; its flight there gets the cancellation line
    store(led).set_status(typed.id, "cancelled")
    a_pass(led, log)
    assert held(log) == [
        ("DL9868", "2027-05-10", ["me"], True),
        ("DL9868", "2027-05-11", ["me"], False),
    ]


def test_a_draft_with_no_time_is_never_completed_by_a_through_flights_other_leg(
    tmp_ledger_path,
):
    """DL 9869 flies MSP to DEN and on to PHL the same day. A draft of the owner's first leg that
    says its day and number but no time, and no airport the table knows, could be either leg: no
    email completes it, so the second leg's receipt is filed on its own and sent. A draft that
    says its time takes in a receipt that prints none: the receipt is read against it first and
    joins it, so nothing reaches the flight log until the draft is put right. Either way the
    owner puts the draft right on the page, and the flight log then holds each leg once."""
    first_leg = {
        "origin": "MSP",
        "destination": "DEN",
        "dep_local": "2027-05-17T07:10",
        "arr_local": "2027-05-17T08:30",
    }
    cities = dict(origin="Minneapolis", dest="Denver")
    for draft_time, second_time in ((None, "09:20"), ("07:10", None)):
        led = Ledger.open(tmp_ledger_path.with_name(f"{draft_time}-{second_time}.db"))
        log = flight_log(led)
        draft = flight(
            [ME], "9869", "2027-05-17", n=0, code="QXY654", dep=draft_time, arr=None, **cities
        )
        run(led, draft, log=log)
        (d,) = live(led)
        assert d.item_key.startswith("draft|") and d.sync_state == "blocked"
        second = flight(
            [ME], "9869", "2027-05-17", n=1, code="QXY654", dep=second_time, arr="11:30",
            origin="DEN", dest="PHL",
        )
        run(led, second, log=log)
        if second_time is not None:  # the second leg, filed on its own, is sent
            alone, filed = live(led)
            assert (alone.id, filed.item_key) == (d.id, "DL|9869|2027-05-17|DEN")
            assert held(log) == [("DL9869", "2027-05-17", ["me"], False)]
        else:  # the receipt joins the draft, which stays to check
            (alone,) = live(led)
            assert alone.id == d.id and alone.item_key.startswith("draft|")
            assert alone.sync_state == "blocked" and log.flights == []
        # the page: the draft is put right as the first leg; the flight log gains it
        fixed = store(led).edit_item(d.id, first_leg)
        assert fixed.item_key == "DL|9869|2027-05-17|MSP" and fixed.checks == ()
        a_pass(led, log)
        sent = [(f["flightNumber"], f["from"]["iata"], f["to"]["iata"]) for f in log.flights]
        legs = [("DL9869", "DEN", "PHL")] if second_time is not None else []
        assert sorted(sent) == sorted([*legs, ("DL9869", "MSP", "DEN")])


def test_two_stays_under_one_itinerary_number_fold_into_one(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    stays = [
        Reservation(
            kind="lodging",
            status="confirmed",
            confirmation_code="QYZ543",
            provider="Sample Trips",
            property_name=name,
            check_in=check_in,
            check_out=check_out,
            passengers=[ME],
        )
        for name, check_in, check_out in (
            ("Example Harbor Inn", "2027-05-24", "2027-05-26"),
            ("Example Mountain Lodge", "2027-05-26", "2027-05-29"),
        )
    ]
    items = validate_extraction(
        Extraction(document_type="booking", confidence=1.0, reservations=stays),
        refdata=airports(),
        book=BOOK,
        email_date=sent_at(0),
        source={"message_id": "<qyz543@example.com>", "email_date": sent_at(0).isoformat()},
        trusted_sender=True,
        parser="kitinerary",
    )
    run(led, items, log=log)
    (stay,) = live(led)
    assert stay.kind == "lodging" and stay.item_key == "lodging|QYZ543"
    assert log.requests == []  # a stay never reaches a flight log


def test_a_forced_replay_of_an_older_receipt_puts_a_companion_back_but_never_the_owner(
    tmp_ledger_path,
):
    """Only what the owner takes off on the page is remembered, so reading an older receipt
    again, forced, puts back a traveller a later email took off. No email takes the owner off:
    one that names the owner cancels the booking, and the receipt read again leaves it
    cancelled. A companion a later email took off comes back, on the page and in the flight
    log, until the owner takes them off on the page."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    booked = flight([ME, SAM], "9870", "2027-05-31", n=0, code="QZY432")
    run(led, booked, log=log)
    owner_off = flight([ME], "9870", "2027-05-31", n=1, code="QZY432", status="cancelled")
    run(led, owner_off, doc="cancellation", log=log)
    run(led, booked, log=log)  # the first receipt read again, forced
    (booking,) = live(led)
    assert (booking.status, sorted(booking.passengers)) == ("cancelled", [ME, SAM])
    assert held(log) == [("DL9870", "2027-05-31", ["me", SAM], True)]

    led = Ledger.open(tmp_ledger_path.with_name("companion.db"))
    log = flight_log(led)
    run(led, booked, log=log)
    sam_off = flight([SAM], "9870", "2027-05-31", n=1, code="QZY432", status="cancelled")
    run(led, sam_off, doc="cancellation", log=log)
    (booking,) = live(led)
    assert booking.passengers == [ME] and held(log) == [("DL9870", "2027-05-31", ["me"], False)]
    run(led, booked, log=log)  # the first receipt read again, forced: Sam is back
    (booking,) = live(led)
    assert sorted(booking.passengers) == [ME, SAM] and booking.sync_state == "synced"
    assert held(log) == [("DL9870", "2027-05-31", ["me", SAM], False)]
    # the page: Sam is taken off, which no reading undoes; the flight log follows
    store(led).edit_item(booking.id, {"passengers": [ME]})
    a_pass(led, log)
    run(led, booked, log=log)
    (booking,) = live(led)
    assert booking.passengers == [ME] and held(log) == [("DL9870", "2027-05-31", ["me"], False)]

