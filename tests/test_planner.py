# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The booking plan (`write.planner`) and the executor (`write.executor`) over a trip store. Each
booking read from an email is matched against the bookings the store holds and is filed, updates
the one it is, is a duplicate or is ignored; nothing is held back. The store's trip item is the one
state of a booking; the flight log is an AirTrail held in memory that the pass every message ends
with brings in step, so each story is told on the trip items and on what that flight log holds."""

import dataclasses
import datetime as dt
import re

import pytest
from helpers.airports import airports
from helpers.flightlog import Scripted, flight_log
from helpers.sample import TRAVELLERS

from tripbox import util
from tripbox.ledger import Ledger
from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.trips.store import TripItem, TripStore
from tripbox.validate import checks
from tripbox.validate.keys import is_draft
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rules import validate_extraction
from tripbox.write.executor import Executor
from tripbox.write.lifecycle import note_airtrail
from tripbox.write.planner import (
    CHANGE_FIELDS,
    Action,
    WritePlan,
    _diff,
    earlier,
    merge_items,
    moved_from,
    plan,
)
from tripbox.write.sync import Syncer

BOOK = PassengerBook.from_yaml(TRAVELLERS)
EMAIL = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)
LATER = EMAIL + dt.timedelta(hours=1)
ME = BOOK.me_name
HOME = ("MSP",)


def items_for(
    passengers,
    *,
    dep="2027-06-22T08:20",
    arr="2027-06-22T11:55",
    pnr="ABC123",
    msg="<m1>",
    status="confirmed",
    seat_by=None,
    dest="PHL",
    number="1234",
    kind="flight",
    origin="MSP",
    carrier="DL",
    provider="Delta Air Lines",
    email=EMAIL,
    trusted=True,
    confidence=1.0,
    parser="kitinerary",
):
    if kind == "flight":
        res = Reservation(
            kind="flight",
            status=status,
            confirmation_code=pnr,
            provider=provider,
            passengers=passengers,
            segments=[
                Segment(
                    carrier_iata=carrier,
                    flight_number=number,
                    origin=origin,
                    destination=dest,
                    departure_local=dep,
                    arrival_local=arr,
                    cabin="Main Cabin",
                    seat_by_passenger=seat_by or {},
                )
            ],
        )
    else:
        res = Reservation(
            kind="lodging",
            status=status,
            confirmation_code=pnr,
            provider="Hilton",
            property_name="Hilton Example",
            check_in=dep[:10],
            check_out=arr[:10],
            passengers=passengers,
        )
    ex = Extraction(document_type="booking", confidence=confidence, reservations=[res])
    return validate_extraction(
        ex,
        refdata=airports(),
        book=BOOK,
        email_date=email,
        source={"message_id": msg, "email_date": email.isoformat()},
        trusted_sender=trusted,
        parser=parser,
    )


def store(led) -> TripStore:
    return TripStore(led, HOME, book=BOOK, airports=airports())


def run(led, ex_items, doc="booking", *, log=None, dry_run=False, received=None):
    """One message's bookings through the plan and the executor, then, with a flight log `log`,
    the pass every message ends with. Each outcome's sync state and checks are read again after
    the pass, as the pipeline reads them. `received` is when the message was taken in (now when
    none is given). Returns the plan and the outcomes."""
    st = store(led)
    mid = led.messages.insert(
        sha256=f"s{led.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]}",
        source="cli",
        status="archived",
    )
    at = received.isoformat() if received is not None else None
    p = plan(ex_items, document_type=doc, store=st, book=BOOK, received_at=at)
    out = Executor(ledger=led, store=st, book=BOOK).execute(p, message_row_id=mid)
    if log is not None:
        Syncer(
            store=st, ledger=led, book=st.book, client=log, refs=None, dry_run=dry_run
        ).run_pass()
    for o in out:
        ti = st.item(o.trip_item_id) if o.trip_item_id else None
        if ti is not None:
            o.sync, o.checks = ti.sync_state, ti.checks
    return p, out


def bookings(led) -> list[TripItem]:
    """Every booking the store holds, removed or not, oldest first."""
    st = store(led)
    return [st.item(r["id"]) for r in led.conn.execute("SELECT id FROM trip_items ORDER BY id")]


def aboard(flight: dict) -> list[tuple[str, str | None]]:
    """Who the flight log holds on a flight, the owner as "me", each with their seat."""
    return [
        ("me" if x["user"] else x["guestName"], x["seatNumber"]) for x in flight["passengers"]
    ]


def sent(log: Scripted) -> list[str]:
    """What was sent to the flight log, in order: `create` or `update`."""
    return [r[0] for r in log.sent]


def test_new_flight_then_identical_email_is_duplicate(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    p1, out1 = run(led, items_for(["Alex Sample"]), log=log)
    assert [a.kind for a in p1.actions] == ["file"]
    assert (out1[0].result, out1[0].sync) == ("filed", "synced")
    # a second email for the same booking
    p2, out2 = run(led, items_for(["Alex Sample"], msg="<m2>"), log=log)
    assert [a.kind for a in p2.actions] == ["duplicate"]
    assert (out2[0].result, out2[0].sync) == ("duplicate", "synced")
    (ti,) = bookings(led)
    assert out2[0].trip_item_id == out1[0].trip_item_id == ti.id
    assert ti.item_key == "DL|1234|2027-06-22|MSP"
    assert len(log.flights) == 1 and sent(log) == ["create"]
    # both messages point at the booking
    assert [r["outcome"] for r in led.items.by_trip_item(ti.id)] == ["filed", "duplicate"]


def test_per_passenger_receipts_merge_into_one_flight(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, items_for(["SAMPLE/ALEX QUINN"], seat_by={"SAMPLE/ALEX QUINN": "14A"}), log=log)
    p2, out2 = run(
        led,
        items_for(
            ["SAM SAMPLE", "SAMPLE/ALEX QUINN"],
            msg="<m2>",
            seat_by={"SAM SAMPLE": "14B"},
        ),
        log=log,
    )
    assert p2.actions[0].kind == "update" and p2.actions[0].reason == "add_passengers"
    assert p2.actions[0].add_passengers == ["Sam Sample"]
    assert (out2[0].result, out2[0].sync) == ("updated", "synced")
    (ti,) = bookings(led)
    assert ti.passengers == ["SAMPLE/ALEX QUINN", "SAM SAMPLE"]
    (held,) = log.flights
    assert aboard(held) == [("me", "14A"), ("Sam Sample", "14B")]
    p3, _ = run(led, items_for(["SAM SAMPLE"], msg="<m3>"), log=log)  # third receipt, nothing new
    assert p3.actions[0].kind == "duplicate"
    assert sent(log) == ["create", "update"] and len(log.flights) == 1


def test_ten_minute_time_shift_is_a_change_not_a_new_flight(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, items_for(["Alex Sample"]), log=log)
    p2, out2 = run(
        led,
        items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m2>"),
        doc="change",
        log=log,
    )
    a = p2.actions[0]
    assert a.kind == "update" and a.reason == "changed"
    assert a.changes["dep_utc"] == ("2027-06-22T13:20:00Z", "2027-06-22T13:30:00Z")
    assert (out2[0].result, out2[0].sync) == ("updated", "synced")
    (ti,) = bookings(led)
    assert ti.payload["dep_utc"] == "2027-06-22T13:30:00Z" and ti.payload["change_recorded"]
    (held,) = log.flights
    assert held["departure"] == "2027-06-22T13:30:00Z" and sent(log) == ["create", "update"]


def test_same_pnr_moved_to_another_day_updates_existing(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (booked,) = items_for(["Alex Sample"])
    _, first = run(led, [booked], log=log)
    p2, out2 = run(
        led,
        items_for(["Alex Sample"], dep="2027-06-23T08:20", arr="2027-06-23T11:55", msg="<m2>"),
        log=log,
    )
    a = p2.actions[0]
    assert a.kind == "update" and a.reason == "rescheduled"
    assert a.trip_item_id == first[0].trip_item_id
    assert a.changes["date_local"] == ("2027-06-22", "2027-06-23")
    (ti,) = bookings(led)
    assert ti.item_key == "DL|1234|2027-06-23|MSP" == a.item.key
    # the key it left still finds it, so a late copy of the first receipt is no new booking
    assert ti.payload["alt_keys"] == [booked.key] and store(led).find(booked.key).id == ti.id
    (held,) = log.flights
    assert held["date"] == "2027-06-23" and out2[0].sync == "synced"


def test_a_day_printed_alone_on_the_day_of_a_held_time_changes_nothing(tmp_ledger_path):
    """A newer email for a filed flight that prints a day and no time for an end whose time is
    held on that day says nothing about it; a departure printed as another day alone is a
    change."""
    held = items_for(["Casey Sample"])[0].payload
    fields = CHANGE_FIELDS["flight"]
    both = items_for(["Casey Sample"], dep="2027-06-22", arr="2027-06-22", email=LATER)[0]
    arrival = items_for(["Casey Sample"], arr="2027-06-22", msg="<m2>", email=LATER)[0]
    assert _diff(held, both.payload, fields) == {} and _diff(held, arrival.payload, fields) == {}
    moved = items_for(["Casey Sample"], dep="2027-06-23", email=LATER)[0]
    assert _diff(held, moved.payload, fields) == {"date_local": (held["date_local"], "2027-06-23")}
    led = Ledger.open(tmp_ledger_path)
    _, (first,) = run(led, items_for(["Casey Sample"]))
    p, (out,) = run(led, [arrival])
    assert [a.kind for a in p.actions] == ["duplicate"] and out.result == "duplicate"

    def stored() -> dict:
        return store(led).item(first.trip_item_id).payload

    assert stored()["arr_local"] == held["arr_local"]
    # a change to the departure beside the arrival's day alone: the departure moves, and the
    # arrival keeps its time
    shifted = items_for(
        ["Casey Sample"], dep="2027-06-22T08:30", arr="2027-06-22", msg="<m3>", email=LATER
    )
    p, (out,) = run(led, shifted, doc="change")
    assert (p.actions[0].kind, set(p.actions[0].changes)) == ("update", {"dep_utc"})
    after = stored()
    assert after["dep_local"] == "2027-06-22T08:30"
    assert (after["arr_local"], after["arr_utc"]) == (held["arr_local"], held["arr_utc"])


def test_cancellation_annotates_known_and_files_unknown_cancelled(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, items_for(["Alex Sample"]), log=log)
    # another flight number under the same code, that day, on the same route: no booking the
    # store holds is that flight, and a cancellation acts only on the booking it names. It is
    # filed as the cancelled booking it says, marked for the owner to look at, and never sent
    p3, out3 = run(
        led, items_for(["Alex Sample"], number="9", msg="<m3>"), doc="cancellation", log=log
    )
    assert (p3.actions[0].kind, out3[0].result) == ("file", "filed")
    known, unknown = bookings(led)
    assert (unknown.status, unknown.checks, unknown.sync_state) == (
        "cancelled",
        ("unknown_cancellation",),
        "off",
    )
    (held,) = log.flights
    assert known.status == "confirmed" and "CANCELLED" not in held["note"]
    assert sent(log) == ["create"]
    p2, out2 = run(
        led,
        items_for(["Alex Sample"], msg="<m2>", status="cancelled"),
        doc="cancellation",
        log=log,
    )
    assert (p2.actions[0].kind, p2.actions[0].reason) == ("update", "cancel")
    assert (out2[0].result, out2[0].sync) == ("updated", "synced")
    known, _ = bookings(led)
    assert known.status == "cancelled"
    (held,) = log.flights  # noted there, never deleted
    assert held["note"].startswith("CANCELLED ") and len(bookings(led)) == 2


def test_adopt_when_airtrail_already_has_it(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (item,) = items_for(["Alex Sample"])
    by_hand = log.create_flight(  # the owner entered it there by hand
        {
            "date": item.payload["date_local"],
            "flightNumber": item.payload["flight_code"],
            "fromId": led.refcache.get("airport", "MSP")["id"],
            "toId": led.refcache.get("airport", "PHL")["id"],
            "passengers": [],
        }
    )
    log.requests.clear()
    p, out = run(led, [item], log=log)
    assert p.actions[0].kind == "file" and (out[0].result, out[0].sync) == ("filed", "synced")
    (ti,) = bookings(led)
    assert ti.airtrail_flight_id == by_hand and ti.told is not None
    assert log.sent == [] and len(log.flights) == 1


def test_non_owner_flight_and_hotel_go_to_trips(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    p, out = run(
        led,
        items_for(["Casey Sample"]) + items_for(["Casey Sample"], kind="lodging", pnr="H1"),
        log=log,
    )
    assert [a.kind for a in p.actions] == ["file", "file"]
    assert all((o.result, o.sync) == ("filed", "off") and o.trip_item_id for o in out)
    assert len({o.trip_id for o in out}) == 1  # one email lands in one trip
    assert log.requests == []
    p2, out2 = run(
        led,
        items_for(["Casey Sample"], kind="lodging", pnr="H1", arr="2027-06-26T11:00"),
        log=log,
    )
    assert p2.actions[0].kind == "update" and "check_out" in p2.actions[0].changes
    assert out2[0].result == "updated" and log.requests == []


def test_a_flight_that_names_nobody_and_a_dry_run_are_filed_and_kept_from_the_flight_log(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    # nobody named: filed, and kept from the flight log until someone is
    p, out = run(led, items_for([], pnr="ZZZ999", number="567"), log=log)
    assert p.actions[0].kind == "file"
    assert (out[0].result, out[0].sync, out[0].checks) == ("filed", "blocked", ("owner_unknown",))
    assert log.sent == [] and len(bookings(led)) == 1
    # a dry run files the booking and only keeps it from the flight log
    p2, out2 = run(led, items_for(["Alex Sample"], msg="<m2>"), log=log, dry_run=True)
    assert p2.actions[0].kind == "file"
    assert (out2[0].result, out2[0].sync) == ("filed", "pending") and log.sent == []
    # with the dry run off, the next pass sends it, once
    p3, out3 = run(led, items_for(["Alex Sample"], msg="<m3>"), log=log)
    assert p3.actions[0].kind == "duplicate" and out3[0].sync == "synced"
    assert sent(log) == ["create"] and len(bookings(led)) == 2


def test_same_segment_twice_in_one_email_is_merged_into_one_flight(tmp_ledger_path):
    """A parser may return one reservation per traveller for the same legs: one POST, both names."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    items = items_for(
        ["SAMPLE/ALEX QUINN"], seat_by={"SAMPLE/ALEX QUINN": "14A"}
    ) + items_for(["SAM SAMPLE"], seat_by={"SAM SAMPLE": "14B"})
    p, out = run(led, items, log=log)
    assert [a.kind for a in p.actions] == ["file"]
    item = p.actions[0].item
    assert item.classified.me_present and item.classified.guests == ["Sam Sample"]
    assert item.payload["seat_by_passenger"] == {
        "SAMPLE/ALEX QUINN": "14A",
        "SAM SAMPLE": "14B",
    }
    (held,) = log.flights
    assert sent(log) == ["create"] and aboard(held) == [("me", "14A"), ("Sam Sample", "14B")]
    (ti,) = bookings(led)
    assert ti.passengers == ["SAMPLE/ALEX QUINN", "SAM SAMPLE"] and out[0].sync == "synced"
    # a hotel with a colliding key but another kind is left alone
    p2, _ = run(led, items_for(["Casey Sample"], kind="lodging", pnr="H7", msg="<m2>"), log=log)
    assert [a.kind for a in p2.actions] == ["file"]


def test_update_keeps_what_earlier_emails_established(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    run(led, items_for(["SAMPLE/ALEX QUINN"], seat_by={"SAMPLE/ALEX QUINN": "14A"}))
    run(led, items_for(["SAM SAMPLE"], seat_by={"SAM SAMPLE": "14B"}, msg="<m2>"))
    (ti,) = bookings(led)
    p = ti.payload
    assert p["seat_by_passenger"] == {"SAMPLE/ALEX QUINN": "14A", "SAM SAMPLE": "14B"}
    assert p["passengers"] == ["SAMPLE/ALEX QUINN", "SAM SAMPLE"]
    assert p["message_id"] == "<m2>"  # the latest email wins where it says something
    # a cancellation that prints no arrival time does not erase the stored one
    cancel = items_for(["Alex Sample"], arr=None, msg="<m3>", status="cancelled")
    run(led, cancel, doc="cancellation")
    (ti,) = bookings(led)
    assert ti.status == "cancelled" and ti.payload["arr_utc"] == "2027-06-22T15:55:00Z"


def test_same_pnr_same_day_new_flight_number_is_a_reschedule(tmp_ledger_path):
    """A change under the same code: the flight has another number on the same day."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (booked,) = items_for(["Alex Sample"], number="567")
    run(led, [booked], log=log)
    p2, out2 = run(
        led,
        items_for(["Alex Sample"], number="0678", dep="2027-06-22T07:10", msg="<m2>"),
        doc="change",
        log=log,
    )
    a = p2.actions[0]
    assert a.kind == "update" and a.reason == "rescheduled"
    assert a.changes["flight_code"] == ("DL567", "DL678") and "dep_utc" in a.changes
    assert (out2[0].result, out2[0].sync) == ("updated", "synced")
    (ti,) = bookings(led)
    assert ti.item_key == "DL|678|2027-06-22|MSP" and booked.key == "DL|567|2027-06-22|MSP"
    (held,) = log.flights
    assert held["flightNumber"] == "DL678" and sent(log) == ["create", "update"]


def test_change_email_without_names_inherits_them_from_the_known_pnr(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    run(led, items_for(["SAMPLE/ALEX QUINN", "SAM SAMPLE"]))
    nameless = items_for([], dep="2027-06-22T09:05", msg="<m2>")  # schedule change, no names
    assert checks.codes(nameless[0]) == ("owner_unknown",)
    p = plan(nameless, document_type="change", store=store(led), book=BOOK)
    a = p.actions[0]
    assert a.kind == "update" and a.reason == "changed" and "dep_utc" in a.changes
    assert a.item.classified.me_present and a.item.classified.guests == ["Sam Sample"]
    assert a.item.payload["passengers_inherited_from"] and a.add_passengers == []
    # a new leg under the code that names nobody takes the names of the one booking there
    back = dict(
        number="9246", origin="PHL", dest="MSP", dep="2027-06-26T16:45", arr="2027-06-26T18:50"
    )
    (leg,) = items_for([], msg="<m4>", **back)
    a = plan([leg], document_type="booking", store=store(led), book=BOOK).actions[0]
    assert a.kind == "file" and a.item.classified.me_present
    assert a.item.classified.guests == ["Sam Sample"]
    # and under a code whose two legs carry the same travellers, the names they agree on
    run(led, items_for(["SAM SAMPLE", "SAMPLE/ALEX QUINN"], msg="<m5>", **back))
    (third,) = items_for(
        [], number="567", dep="2027-06-23T08:20", arr="2027-06-23T11:55", msg="<m6>"
    )
    a = plan([third], document_type="booking", store=store(led), book=BOOK).actions[0]
    assert a.kind == "file" and a.item.classified.me_present
    assert a.item.classified.guests == ["Sam Sample"]
    # a code the booking under its key does not hold lends it no names: it prints nothing new
    # of that booking, so it is a copy, and still names nobody
    p2 = plan(
        items_for([], pnr="ZZZ999", msg="<m3>"),
        document_type="change",
        store=store(led),
        book=BOOK,
    )
    assert p2.actions[0].kind == "duplicate"
    assert checks.codes(p2.actions[0].item) == ("owner_unknown",)


def city_receipt(name, msg, **leg):
    """One traveller's receipt for the fixture's flight, its airports read as city names."""
    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[ticket(name, origin="MINNEAPOLIS", destination="PHILADELPHIA", **leg)],
    )
    return validate_extraction(
        ex,
        refdata=airports(),
        book=BOOK,
        email_date=EMAIL,
        source={"message_id": msg, "email_date": EMAIL.isoformat()},
    )


def test_receipt_with_city_names_inherits_the_known_segment_route(tmp_ledger_path):
    """A receipt read with city names where an earlier one gave the airport codes: the same
    code, flight, date and printed time as the booking already filed, so its traveller is added
    to that flight."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, items_for(["SAMPLE/ALEX QUINN"], seat_by={"SAMPLE/ALEX QUINN": "16A"}), log=log)
    items = city_receipt("ROBIN SAMPLE", "<m3>", seat_by_passenger={"ROBIN SAMPLE": "16C"})
    assert "origin" in checks.codes(items[0]) and is_draft(items[0].key)  # no key from a city
    p, out = run(led, items, log=log)
    a = p.actions[0]
    assert a.kind == "update" and a.reason == "add_passengers"
    assert a.add_passengers == ["Robin Sample"] and a.item.key == "DL|1234|2027-06-22|MSP"
    assert a.item.payload["origin"] == "MSP" and a.item.payload["route_inherited_from"]
    assert (out[0].result, out[0].sync) == ("updated", "synced")
    (held,) = log.flights
    assert aboard(held) == [("me", "16A"), ("Robin Sample", "16C")]
    # a different printed time is not the same segment: a draft of its own, with its checks
    p2, out2 = run(
        led, city_receipt("ROBIN SAMPLE", "<m4>", departure_local="2027-06-22T09:05"), log=log
    )
    assert p2.actions[0].kind == "file" and is_draft(p2.actions[0].item.key)
    assert "origin" in out2[0].checks and len(bookings(led)) == 2
    assert sent(log) == ["create", "update"]


def test_a_departure_printed_as_a_day_alone_inherits_only_from_a_time_held_on_that_day(
    tmp_ledger_path,
):
    """A receipt read with city names and its departure as a day alone is the booking the store
    holds a time for on that day, and takes that time; held against another day alone it is not
    known to be that booking, and keeps its finding."""
    led = Ledger.open(tmp_ledger_path)
    (timed,) = items_for(["SAMPLE/ALEX QUINN"])
    run(led, [timed])
    bare = city_receipt("ROBIN SAMPLE", "<m3>", departure_local="2027-06-22")
    assert {"origin", "destination", "departure"} <= set(checks.codes(bare[0]))
    p, out = run(led, bare)
    a = p.actions[0]
    assert (a.kind, a.reason, a.add_passengers) == ("update", "add_passengers", ["Robin Sample"])
    assert a.item.key == timed.key and a.item.payload["dep_local"] == timed.payload["dep_local"]
    (ti,) = bookings(led)
    assert ti.payload["dep_utc"] == timed.payload["dep_utc"] and ti.checks == ()
    # the same booking held with its day alone: two days alone say nothing of each other
    other = Ledger.open(tmp_ledger_path.with_name("other.db"))
    (dayless,) = items_for(["SAMPLE/ALEX QUINN"], dep="2027-06-22")
    held = store(other).upsert_item(None, dayless)
    assert held.checks == ("departure",)
    again = city_receipt("ROBIN SAMPLE", "<m3>", departure_local="2027-06-22")
    p2, _ = run(other, again)
    assert p2.actions[0].kind == "file" and "departure" in checks.codes(p2.actions[0].item)
    assert is_draft(p2.actions[0].item.key) and len(bookings(other)) == 2
    # and a receipt with a time is not the booking held with its day alone
    p3, _ = run(other, city_receipt("ROBIN SAMPLE", "<m4>"))
    assert p3.actions[0].kind == "file" and len(bookings(other)) == 3


def test_a_family_receipt_first_then_the_owners_is_one_booking_and_one_flight_carrying_the_guest(
    tmp_ledger_path,
):
    """An airline may send one receipt per traveller, in any order. Sam's first is a flight the
    owner is not on; Alex's after it makes that same booking the owner's flight, and the flight
    log gets one flight with both aboard."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    p1, out1 = run(led, items_for(["SAM SAMPLE"], seat_by={"SAM SAMPLE": "16B"}), log=log)
    assert p1.actions[0].kind == "file" and (out1[0].result, out1[0].sync) == ("filed", "off")
    assert log.flights == [] and log.requests == []
    p2, out2 = run(
        led,
        items_for(["SAMPLE/ALEX QUINN"], seat_by={"SAMPLE/ALEX QUINN": "16A"}, msg="<m2>"),
        log=log,
    )
    a = p2.actions[0]
    assert (a.kind, a.reason, a.add_passengers) == ("update", "add_passengers", [ME])
    assert (out2[0].result, out2[0].sync) == ("updated", "synced")
    (ti,) = bookings(led)
    assert ti.id == out1[0].trip_item_id == out2[0].trip_item_id
    (held,) = log.flights
    assert aboard(held) == [("me", "16A"), ("Sam Sample", "16B")] and sent(log) == ["create"]
    # Sam's receipt again is a duplicate of the one booking
    p3, _ = run(led, items_for(["SAM SAMPLE"], msg="<m3>"), log=log)
    assert p3.actions[0].kind == "duplicate" and len(bookings(led)) == 1


def test_booking_copy_never_rewinds_a_recorded_change(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    # the change's itinerary is processed first (both arrive in the same second)
    (changed,) = items_for(
        ["Alex Sample"], number="678", status="changed", dep="2027-06-22T07:10"
    )
    run(led, [changed], doc="change", log=log)
    p, out = run(led, items_for(["Alex Sample"], number="567", msg="<m2>"), doc="booking", log=log)
    assert (p.actions[0].kind, p.actions[0].reason) == ("ignore", "older_copy")
    assert (out[0].result, out[0].sync) == ("ignored", "synced")
    (ti,) = bookings(led)
    assert ti.item_key == changed.key and ti.payload["flight_code"] == "DL678"
    assert sent(log) == ["create"]


def test_each_travellers_receipt_joins_the_one_booking_and_rides_on_the_owners_flight(
    tmp_ledger_path,
):
    """Receipts for Jamie, then Sam, then Alex are one booking, and both ride on Alex's flight."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, items_for(["JAMIE SAMPLE"], seat_by={"JAMIE SAMPLE": "16C"}), log=log)
    p_sam, _ = run(
        led, items_for(["SAM SAMPLE"], seat_by={"SAM SAMPLE": "16B"}, msg="<m2>"), log=log
    )
    assert (p_sam.actions[0].kind, p_sam.actions[0].reason) == ("update", "add_passengers")
    assert log.flights == []
    p, out = run(led, items_for(["SAMPLE/ALEX QUINN"], msg="<m3>"), log=log)
    a = p.actions[0]
    assert (a.kind, a.add_passengers) == ("update", [ME]) and out[0].sync == "synced"
    (held,) = log.flights
    assert aboard(held) == [("me", None), ("Jamie Sample", "16C"), ("Sam Sample", "16B")]
    for who in ("JAMIE SAMPLE", "SAM SAMPLE"):  # already aboard: nothing to do
        again, _ = run(led, items_for([who], msg=f"<again-{who}>"), log=log)
        assert again.actions[0].kind == "duplicate"
    p_robin, out_robin = run(led, items_for(["ROBIN SAMPLE"], msg="<robin>"), log=log)
    assert p_robin.actions[0].add_passengers == ["Robin Sample"]  # a fourth receipt joins
    assert out_robin[0].sync == "synced" and len(bookings(led)) == 1 and len(log.flights) == 1
    assert sent(log) == ["create", "update"]


def test_booking_copy_on_the_same_key_does_not_rewind_a_change(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    run(led, items_for(["Alex Sample"]))  # booking: 08:20
    run(
        led,
        items_for(["Alex Sample"], dep="2027-06-22T07:40", msg="<m2>", status="changed"),
        doc="change",
    )
    p, out = run(led, items_for(["Alex Sample"], msg="<m3>"), doc="booking")  # the booking again
    assert p.actions[0].kind == "duplicate" and out[0].result == "duplicate"
    (ti,) = bookings(led)
    assert ti.payload["dep_local"] == "2027-06-22T07:40"


def three_traveller_receipt(out_only="Alex Sample", back_only="Robin Sample"):
    """One receipt split per traveller: Sam on both legs, Alex on the outbound only, Robin on
    the return only (or whoever is named for each). kitinerary and the model both hand this back as
    three reservations."""

    def res(names, *segs):
        return Reservation(
            kind="flight",
            status="confirmed",
            confirmation_code="FAM123",
            provider="Delta Air Lines",
            passengers=names,
            segments=list(segs),
        )

    def out():
        return Segment(
            carrier_iata="DL",
            flight_number="1234",
            origin="MSP",
            destination="PHL",
            departure_local="2027-06-22T08:20",
            arrival_local="2027-06-22T11:55",
        )

    def back():
        return Segment(
            carrier_iata="DL",
            flight_number="9246",
            origin="PHL",
            destination="MSP",
            departure_local="2027-06-26T16:45",
            arrival_local="2027-06-26T18:50",
        )

    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            res(["Sam Sample"], out(), back()),
            res([out_only], out()),
            res([back_only], back()),
        ],
    )
    return validate_extraction(
        ex,
        refdata=airports(),
        book=BOOK,
        email_date=EMAIL,
        source={"message_id": "<fam>", "email_date": EMAIL.isoformat()},
    )


def test_owner_is_not_put_on_a_leg_they_are_not_booked_on(tmp_ledger_path):
    """Merging Alex's outbound into Sam's must not put the owner on Sam's return."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    wp, outcomes = run(led, three_traveller_receipt(), log=log)
    by_key = {a.item.key: a for a in wp.actions}
    out, back = by_key["DL|1234|2027-06-22|MSP"], by_key["DL|9246|2027-06-26|PHL"]
    assert out.kind == "file" and out.item.classified.me_present
    assert out.item.classified.guests == ["Sam Sample"]  # Robin is not on this leg
    assert back.kind == "file" and not back.item.classified.me_present
    assert sorted(back.item.classified.guests) == ["Robin Sample", "Sam Sample"]
    assert not re.search(r"\bAlex\b", " ".join(back.item.passengers))
    # only the leg the owner is on reaches the flight log
    assert {o.action.item.key: o.sync for o in outcomes} == {
        out.item.key: "synced",
        back.item.key: "off",
    }
    (held,) = log.flights
    assert held["flightNumber"] == out.item.payload["flight_code"]
    assert aboard(held) == [("me", None), ("Sam Sample", None)]
    # a notice for the outbound that names nobody takes the names of that leg, not of the
    # return, which was filed last under the same code
    nameless = items_for([], pnr="FAM123", msg="<m2>", email=LATER)
    p, (o,) = run(led, nameless, log=log)
    assert p.actions[0].kind == "duplicate" and p.actions[0].item.classified.me_present
    assert p.actions[0].item.classified.guests == ["Sam Sample"]
    assert o.sync == "synced" and sent(log) == ["create"]


def test_a_nameless_notice_for_a_leg_that_moved_takes_that_legs_travellers(tmp_ledger_path):
    """A notice that names nobody, for a leg that moved to another day, is for the travellers of
    that leg, not of the leg filed last under the code. Whichever leg the owner is on, nobody
    joins the leg that moved, and the flight log holds who it held."""
    day_later = dict(dep="2027-06-23T08:20", arr="2027-06-23T11:55", msg="<m2>", email=LATER)
    # the owner on the outbound: the return, filed last, names a companion the outbound lacks
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, three_traveller_receipt(), log=log)
    (held,) = log.flights
    assert aboard(held) == [("me", None), ("Sam Sample", None)]
    (notice,) = items_for([], pnr="FAM123", **day_later)
    p, (o,) = run(led, [notice], doc="change", log=log)
    a = p.actions[0]
    assert (a.kind, a.reason, a.add_passengers) == ("update", "rescheduled", [])
    out_leg, back = bookings(led)
    assert out_leg.item_key == notice.key and o.trip_item_id == out_leg.id
    assert sorted(out_leg.passengers) == ["Alex Sample", "Sam Sample"]
    assert sorted(back.passengers) == ["Robin Sample", "Sam Sample"]
    (held,) = log.flights  # the one flight, moved, with the two it had aboard
    assert aboard(held) == [("me", None), ("Sam Sample", None)]
    assert (held["date"], o.sync) == (notice.payload["date_local"], "synced")
    # the owner on the return only, filed last: the outbound that moves is not the owner's
    led = Ledger.open(tmp_ledger_path.with_name("return.db"))
    log = flight_log(led)
    run(led, three_traveller_receipt(out_only="Robin Sample", back_only="Alex Sample"), log=log)
    (held,) = log.flights
    assert aboard(held) == [("me", None), ("Sam Sample", None)]  # the return
    (notice,) = items_for([], pnr="FAM123", **day_later)
    p, (o,) = run(led, [notice], doc="change", log=log)
    a = p.actions[0]
    assert (a.kind, a.reason, a.add_passengers) == ("update", "rescheduled", [])
    assert not a.item.classified.me_present
    out_leg, back = bookings(led)
    assert out_leg.item_key == notice.key
    assert sorted(out_leg.passengers) == ["Robin Sample", "Sam Sample"]
    assert (out_leg.sync_state, out_leg.airtrail_flight_id, o.sync) == ("off", None, "off")
    assert sorted(back.passengers) == ["Alex Sample", "Sam Sample"]
    assert log.flights == [held] and sent(log) == ["create"]
    # a notice that names nobody, for a flight no booking is: the legs under its code carry
    # different travellers, so nobody's names are taken. It is filed like any flight that names
    # nobody, never as the owner's, and kept from the flight log
    (other,) = items_for([], pnr="FAM123", number="567", msg="<m3>", email=LATER)
    p, (o,) = run(led, [other], log=log)
    assert p.actions[0].kind == "file" and checks.codes(other) == ("owner_unknown",)
    assert not other.classified.me_present and other.passengers == []
    assert (o.result, o.sync, o.checks) == ("filed", "blocked", ("owner_unknown",))
    assert len(bookings(led)) == 3 and log.flights == [held] and sent(log) == ["create"]


def test_a_nameless_notice_under_another_code_takes_no_names_from_the_booking(tmp_ledger_path):
    """The booking under the notice's key holds another confirmation code: its travellers are
    not the notice's, and nothing says who is. A cancellation that names nobody cancels nobody's
    seat and marks the booking, which keeps its own code alone; what a change says of the flight
    changes the booking."""
    led = Ledger.open(tmp_ledger_path)
    run(led, items_for(["Alex Sample"]))
    plans = []
    for doc in ("cancellation", "change"):
        (notice,) = items_for([], pnr="ZZZ999", dep="2027-06-22T08:30", msg="<m2>", email=LATER)
        p, (o,) = run(led, [notice], doc=doc)
        assert checks.codes(notice) == ("owner_unknown",), doc
        assert not notice.classified.me_present and notice.passengers == [], doc
        plans.append((p.actions[0].kind, p.actions[0].reason, o.result))
        if doc == "cancellation":
            (ti,) = bookings(led)
            assert (ti.status, ti.confirmation) == ("confirmed", "ABC123")
            assert ti.payload["dep_local"] == "2027-06-22T08:20"
            assert ti.checks == ("unknown_cancellation",)
    assert plans == [
        ("ignore", "unknown_cancellation", "ignored"),
        ("update", "changed", "updated"),
    ]
    (ti,) = bookings(led)
    assert (ti.status, ti.passengers) == ("confirmed", ["Alex Sample"])
    assert ti.payload["dep_local"] == "2027-06-22T08:30"
    assert ti.checks == ("unknown_cancellation",)


def test_unresolvable_flights_are_never_folded_together(tmp_ledger_path):
    """Two legs on an airline whose code is not printed must stay two drafts."""
    led = Ledger.open(tmp_ledger_path)

    def res(seg_kw):
        return Reservation(
            kind="flight",
            status="confirmed",
            confirmation_code="EA1",
            provider="Example Air",
            passengers=["Alex Sample"],
            segments=[Segment(flight_number="9051", **seg_kw)],
        )

    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            res(dict(origin="MSP", destination="PHL", departure_local="2027-06-22T08:20")),
            res(dict(origin="PHL", destination="MSP", departure_local="2027-06-26T16:45")),
        ],
    )
    items = validate_extraction(
        ex,
        refdata=airports(),
        book=BOOK,
        email_date=EMAIL,
        source={"message_id": "<sa>"},
    )
    assert len(items) == 2 and all(is_draft(i.key) for i in items)
    assert items[0].key != items[1].key
    wp = plan(items, document_type="booking", store=store(led), book=BOOK)
    assert [a.kind for a in wp.actions] == ["file", "file"]
    # legs without a carrier code never fold, not even under one key
    twin = dataclasses.replace(items[1], key=items[0].key)
    assert merge_items([items[0], twin]) == [items[0], twin]

    # two copies of a leg from a city: apart when they differ, or when either lacks its carrier,
    # its number or its day
    for first, second in (
        ({}, {"flight_number": "567"}),
        ({}, {"departure_local": "2027-06-23T08:20"}),
        ({"provider": "Example Air", "carrier_iata": None},) * 2,
        ({"flight_number": None},) * 2,
        ({"departure_local": None},) * 2,
        ({"origin": None},) * 2,
    ):
        ex = Extraction(
            document_type="booking",
            confidence=1.0,
            reservations=[ticket("ROBIN SAMPLE", **first), ticket("SAM SAMPLE", **second)],
        )
        apart = validate_extraction(
            ex, refdata=airports(), book=BOOK, email_date=EMAIL, source={"message_id": "<m3>"}
        )
        assert all(is_draft(i.key) for i in apart)
        assert len(merge_items(apart)) == 2, (first, second)


def ticket(name, provider="Delta", **leg):
    """One traveller's ticket for a leg whose airports came back as city names."""
    seg = dict(
        carrier_iata="DL",
        flight_number="1234",
        origin="PHILADELPHIA",
        destination="MINNEAPOLIS",
        departure_local="2027-06-22T08:20",
        arrival_local="2027-06-22T11:55",
    )
    seg.update(leg)
    return Reservation(
        kind="flight",
        status="confirmed",
        confirmation_code="ABC123",
        provider=provider,
        passengers=[name],
        segments=[Segment(**seg)],
    )


def test_the_same_unresolved_leg_twice_in_one_email_is_one_draft():
    """A receipt that lists each ticket gives a leg once per traveller. Read with a city for its
    origin the leg has no real key, and its copies are still one booking, under the first's
    draft key."""
    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[ticket("ROBIN SAMPLE"), ticket("SAM SAMPLE")],
    )

    def merged():
        items = validate_extraction(
            ex, refdata=airports(), book=BOOK, email_date=EMAIL, source={"message_id": "<m3>"}
        )
        assert len(items) == 2 and all(is_draft(i.key) for i in items)
        first_key = items[0].key
        return first_key, merge_items(items)

    first_key, (one,) = merged()
    assert one.key == first_key
    assert one.passengers == ["ROBIN SAMPLE", "SAM SAMPLE"]
    assert one.payload["passengers"] == ["ROBIN SAMPLE", "SAM SAMPLE"]
    assert one.classified.guests == ["Robin Sample", "Sam Sample"]
    again_key, (again,) = merged()
    assert again.key == again_key == first_key


def test_a_draft_folds_only_into_a_draft_of_the_same_flight_leg():
    """A stay, a car or a train without a real key never folds with its copy, and a leg read with a
    city never folds into the same leg read with its code (one has a real key, the other not)."""
    for kind, extra in (
        ("lodging", {}),
        ("car", {}),
        ("train", {"segments": [Segment()]}),
    ):
        ex = Extraction(
            document_type="booking",
            confidence=1.0,
            reservations=[Reservation(kind=kind, passengers=["SAM SAMPLE"], **extra)] * 2,
        )
        copies = validate_extraction(
            ex, refdata=airports(), book=BOOK, email_date=EMAIL, source={"message_id": "<m3>"}
        )
        assert [i.kind for i in copies] == [kind, kind] and all(is_draft(i.key) for i in copies)
        assert len(merge_items(copies)) == 2, kind
    for pair in (
        [ticket("ROBIN SAMPLE"), ticket("SAM SAMPLE", origin="PHL")],
        [ticket("ROBIN SAMPLE", origin="PHL"), ticket("SAM SAMPLE")],
    ):
        ex = Extraction(document_type="booking", confidence=1.0, reservations=pair)
        legs = validate_extraction(
            ex, refdata=airports(), book=BOOK, email_date=EMAIL, source={"message_id": "<m3>"}
        )
        assert sorted(is_draft(i.key) for i in legs) == [False, True]
        assert len(merge_items(legs)) == 2


def test_a_later_email_with_the_same_code_completes_an_unfixed_draft(tmp_ledger_path):
    """A receipt read with city names is filed as a draft. The proper receipt for its flight,
    under the same code and carrying its traveller, completes it: one booking under the real key
    that answers to the draft's key too, with the travellers of both, and one flight in the
    flight log. Completing it records no change, so a newer receipt still moves the flight."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (draft,) = city_receipt("ROBIN SAMPLE", "<m3>")
    held = store(led).upsert_item(None, draft)
    assert is_draft(held.item_key) and "origin" in held.checks
    # a cancellation completes no draft: it is filed as the cancelled booking it says
    notice = items_for(["Alex Sample", "ROBIN SAMPLE"], status="cancelled", msg="<m4>")
    wp = plan(notice, document_type="cancellation", store=store(led), book=BOOK)
    assert (wp.actions[0].kind, wp.actions[0].item.status) == ("file", "cancelled")
    p, (out,) = run(led, items_for(["Alex Sample", "ROBIN SAMPLE"]), log=log)
    a = p.actions[0]
    assert (a.kind, a.reason, a.trip_item_id) == ("update", "completes_draft", held.id)
    (ti,) = bookings(led)
    assert ti.id == held.id and ti.item_key == a.item.key == "DL|1234|2027-06-22|MSP"
    assert ti.payload["alt_keys"] == [held.item_key] and store(led).find(held.item_key).id == ti.id
    assert ti.passengers == ["ROBIN SAMPLE", "Alex Sample"]
    assert (out.result, out.sync, out.checks) == ("updated", "synced", ())
    assert [(r["detail"], r["item_key"]) for r in led.items.by_trip_item(ti.id)] == [
        ("completes_draft", ti.item_key)
    ]
    (flight,) = log.flights
    assert aboard(flight) == [("me", None), ("Robin Sample", None)]
    assert "change_recorded" not in ti.payload
    newer = items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m5>", email=LATER)
    p, _ = run(led, newer, log=log)
    assert (p.actions[0].kind, p.actions[0].reason) == ("update", "changed")
    assert bookings(led)[0].payload["dep_local"] == "2027-06-22T08:30"


def test_a_later_email_that_disagrees_with_a_draft_is_its_own_booking(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    (draft,) = city_receipt("ROBIN SAMPLE", "<m3>")
    held = store(led).upsert_item(None, draft)
    p, (out,) = run(led, items_for(["ROBIN SAMPLE"], number="567"))
    assert p.actions[0].kind == "file" and out.result == "filed"
    assert [ti.item_key for ti in bookings(led)] == [held.item_key, p.actions[0].item.key]
    assert bookings(led)[0].passengers == ["ROBIN SAMPLE"]


def test_two_drafts_that_fit_complete_neither(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    st = store(led)
    drafts = [
        st.upsert_item(None, city_receipt(name, msg)[0])
        for name, msg in (("ROBIN SAMPLE", "<m3>"), ("SAM SAMPLE", "<m4>"))
    ]
    assert len({d.item_key for d in drafts}) == 2 and all(is_draft(d.item_key) for d in drafts)
    both = items_for(["ROBIN SAMPLE", "SAM SAMPLE"])
    assert [ti.id for ti in st.drafts_like(both[0])] == [d.id for d in drafts]
    p, (out,) = run(led, both)
    assert p.actions[0].kind == "file" and out.result == "filed"
    keys = [ti.item_key for ti in bookings(led)]
    assert keys == [*(d.item_key for d in drafts), "DL|1234|2027-06-22|MSP"]


def test_a_draft_two_bookings_of_one_message_fit_is_completed_by_neither(tmp_ledger_path):
    """A draft that says its day and departure time and no number fits two flights of one
    message under its code that each carry its traveller: each is filed as its own booking.
    So is a booking whose one draft another booking of the message fits beside a second one."""
    led = Ledger.open(tmp_ledger_path)
    (timed,) = city_receipt("ROBIN SAMPLE", "<m3>", flight_number=None)
    held = store(led).upsert_item(None, timed)
    both = items_for(["ROBIN SAMPLE"]) + items_for(["ROBIN SAMPLE"], number="567")
    assert [len(store(led).drafts_like(i)) for i in both] == [1, 1]
    p, _ = run(led, both)
    assert [a.kind for a in p.actions] == ["file", "file"]
    filed = [a.item.key for a in p.actions]
    assert [ti.item_key for ti in bookings(led)] == [held.item_key, *filed]
    # the first fits one draft, the second fits that one and another
    other = Ledger.open(tmp_ledger_path.with_name("other.db"))
    st = store(other)
    drafts = [
        st.upsert_item(None, city_receipt("ROBIN SAMPLE", "<m3>")[0]),
        st.upsert_item(None, city_receipt("SAM SAMPLE", "<m4>", flight_number=None)[0]),
    ]
    both = items_for(["ROBIN SAMPLE"]) + items_for(
        ["ROBIN SAMPLE", "SAM SAMPLE"], origin="PHL", dest="MSP"
    )
    assert [[ti.id for ti in st.drafts_like(i)] for i in both] == [
        [drafts[0].id],
        [d.id for d in drafts],
    ]
    p, _ = run(other, both)
    assert [a.kind for a in p.actions] == ["file", "file"]
    assert [ti.item_key for ti in bookings(other)][:2] == [d.item_key for d in drafts]


def test_a_companions_receipt_under_a_shared_code_never_completes_the_owners_draft_of_another_leg(
    tmp_ledger_path,
):
    """The owner's draft says its day and nothing else that tells a leg apart (no number, no
    time, its airports as cities): a companion's receipt for another flight that day, under the
    same code, is filed as its own booking, and the owner is put on no flight."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (vague,) = city_receipt(
        "SAMPLE/ALEX QUINN", "<m3>", flight_number=None, departure_local="2027-06-22"
    )
    held = store(led).upsert_item(None, vague)
    assert is_draft(held.item_key)
    p, (out,) = run(led, items_for(["ROBIN SAMPLE"], number="567"), log=log)
    assert (p.actions[0].kind, out.result) == ("file", "filed")
    assert store(led).item(held.id) == held
    assert [ti.item_key for ti in bookings(led)] == [held.item_key, p.actions[0].item.key]
    assert not [x for f in log.flights for x in f["passengers"] if x["user"]]


def test_a_draft_is_completed_only_by_an_email_that_shares_a_traveller(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    # its day and its number, and another traveller
    (numbered,) = city_receipt("ROBIN SAMPLE", "<m3>", departure_local="2027-06-22")
    held = store(led).upsert_item(None, numbered)
    p, (out,) = run(led, items_for(["SAM SAMPLE"]))
    assert (p.actions[0].kind, out.result) == ("file", "filed")
    assert store(led).item(held.id) == held and len(bookings(led)) == 2
    # its day and its departure time, and a traveller in common
    other = Ledger.open(tmp_ledger_path.with_name("other.db"))
    (timed,) = city_receipt("ROBIN SAMPLE", "<m3>", flight_number=None)
    held = store(other).upsert_item(None, timed)
    p, _ = run(other, items_for(["Alex Sample", "ROBIN SAMPLE"]))
    a = p.actions[0]
    assert (a.kind, a.reason, a.trip_item_id) == ("update", "completes_draft", held.id)
    (ti,) = bookings(other)
    assert ti.item_key == a.item.key and ti.payload["alt_keys"] == [held.item_key]


def test_an_email_under_a_draft_key_completes_no_draft(tmp_ledger_path):
    """Only an email with a real key completes a draft: one under a draft key beside it in the
    message, which fits the same draft, takes no part in it."""
    led = Ledger.open(tmp_ledger_path)
    (draft,) = city_receipt("ROBIN SAMPLE", "<m3>")
    held = store(led).upsert_item(None, draft)
    (proper,) = items_for(["ROBIN SAMPLE"], msg="<m4>")
    (numberless,) = items_for(["ROBIN SAMPLE"], number=None, msg="<m4>")
    assert is_draft(numberless.key) and not is_draft(proper.key)
    p, _ = run(led, [proper, numberless])
    assert [(a.kind, a.reason) for a in p.actions][0] == ("update", "completes_draft")
    assert p.actions[0].trip_item_id == held.id and p.actions[1].kind == "file"


def test_a_draft_that_does_not_say_where_it_leaves_from_fits_only_on_its_departure_time(
    tmp_ledger_path,
):
    """A through-flight keeps one number over two legs in one day. A draft of the owner's that
    prints its day and its number, but neither a departure time nor an airport the table knows,
    could be either leg, so no email completes it, nor lends its travellers: the other leg's
    receipt that names nobody is filed on its own, names nobody, and nothing with the owner
    aboard reaches the flight log."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (vague,) = city_receipt("SAMPLE/ALEX QUINN", "<m3>", departure_local="2027-06-22")
    _, (drafted,) = run(led, [vague], log=log)
    assert is_draft(drafted.action.item.key) and drafted.sync == "blocked"
    (onward,) = items_for(
        [], origin="PHL", dest="MSP", dep="2027-06-22T13:15:00", arr="2027-06-22T15:20:00"
    )
    assert store(led).drafts_like(onward) == []
    p, (o,) = run(led, [onward], log=log)
    a = p.actions[0]
    assert (a.kind, o.result) == ("file", "filed") and not a.item.classified.me_present
    assert a.item.passengers == [] and checks.codes(a.item) == ("owner_unknown",)
    assert (o.sync, o.checks) == ("blocked", ("owner_unknown",))
    draft, filed = bookings(led)
    assert (draft.id, filed.id) == (drafted.trip_item_id, o.trip_item_id)
    assert is_draft(draft.item_key) and draft.passengers == ["SAMPLE/ALEX QUINN"]
    assert log.flights == [] and log.sent == []
    # the same draft that says its departure time is the leg that leaves then
    timed = Ledger.open(tmp_ledger_path.with_name("timed.db"))
    (draft,) = city_receipt("SAMPLE/ALEX QUINN", "<m3>")
    store(timed).upsert_item(None, draft)
    (leg,) = items_for(["SAMPLE/ALEX QUINN"], msg="<m4>")
    assert [ti.item_key for ti in store(timed).drafts_like(leg)] == [draft.key]


def test_a_cancellation_for_an_unknown_booking_is_filed_cancelled(tmp_ledger_path):
    """A cancellation for no booking the store holds is filed as the cancelled booking it says,
    whether it prints the booking as cancelled or only comes as a cancellation, with the check
    that it cancels a booking never filed, stamped when its message was received. A cancelled
    flight that was never sent is never sent, before the owner's look and after."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    for printed in ("cancelled", "confirmed"):
        number = "1234" if printed == "cancelled" else "567"
        notice = items_for(["Alex Sample"], number=number, status=printed, msg=f"<{printed}>")
        wp, (o,) = run(led, notice, doc="cancellation", log=log, received=LATER)
        a = wp.actions[0]
        assert (a.kind, a.item.status, o.result) == ("file", "cancelled", "filed"), printed
        assert a.item.payload["raised"] == {"unknown_cancellation": LATER.isoformat()}
        ti = store(led).item(o.trip_item_id)
        assert (ti.status, ti.checks, ti.sync_state) == (
            "cancelled",
            ("unknown_cancellation",),
            "off",
        ), printed
        looked = store(led).looks_right(ti.id)
        assert (looked.checks, looked.sync_state) == ((), "off"), printed
    assert log.requests == []


def owner_flight(**kw):
    """The owner's flight DL 9123 MSP-PHL on 2027-06-22 (booking code QXR987 unless given)."""
    return items_for(["Alex Sample"], **{"pnr": "QXR987", "number": "9123", **kw})


def test_with_airtrail_off_nothing_is_sent_and_every_flight_is_filed(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    led.kv.set("airtrail", "off")  # the flight log is there, and none is kept
    p, out = run(
        led,
        owner_flight(pnr="RTV876", number="9124", msg="<new>") + items_for(["Casey Sample"]),
        log=log,
    )
    assert [a.kind for a in p.actions] == ["file", "file"]
    assert [(o.result, o.sync) for o in out] == [("filed", "off")] * 2
    assert all(o.trip_item_id for o in out)
    _, first = run(led, owner_flight(), log=log)
    held = first[0].trip_item_id
    plans = []
    for items, doc in (
        (owner_flight(dep="2027-06-22T08:30", msg="<m2>", email=LATER), "change"),
        (owner_flight(msg="<m4>"), "booking"),
        (owner_flight(msg="<m3>", status="cancelled", email=LATER), "cancellation"),
    ):
        wp, (o,) = run(led, items, doc=doc, log=log)
        plans.append((wp.actions[0].kind, wp.actions[0].reason))
        assert (o.trip_item_id, o.sync) == (held, "off")
    assert plans == [("update", "changed"), ("duplicate", ""), ("update", "cancel")]
    assert [ti.sync_state for ti in bookings(led)] == ["off"] * 3
    assert log.requests == [] and log.flights == []


def test_airtrail_added_after_flights_were_filed_in_trips_keeps_each_booking_one_item(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)  # no flight log is kept
    (booked,) = owner_flight()
    _, first = run(led, [booked])
    assert (first[0].result, first[0].sync) == ("filed", "off")
    log = flight_log(led)  # one is switched on
    moved = owner_flight(
        dep="2027-06-23T08:20", arr="2027-06-23T11:55", msg="<m2>", email=LATER
    )
    p, out = run(led, moved, doc="change", log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "rescheduled")]
    (ti,) = bookings(led)
    assert ti.id == first[0].trip_item_id and ti.item_key == moved[0].key
    (held,) = log.flights
    assert out[0].sync == "synced" and held["date"] == moved[0].payload["date_local"]
    # an older copy of the first receipt, forwarded late, does not rewind it
    p, out = run(led, owner_flight(msg="<m3>"), log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("ignore", "older_copy")]
    (ti,) = bookings(led)
    assert ti.item_key == moved[0].key and ti.payload["date_local"] == held["date"]
    # a cancellation cancels it
    cancelled = owner_flight(
        dep="2027-06-23T08:20",
        arr="2027-06-23T11:55",
        msg="<m4>",
        status="cancelled",
        email=LATER + dt.timedelta(hours=1),
    )
    p, out = run(led, cancelled, doc="cancellation", log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "cancel")]
    (ti,) = bookings(led)
    assert ti.status == "cancelled" and out[0].sync == "synced"
    assert len(log.flights) == 1 and sent(log) == ["create", "update"]


def test_airtrail_off_then_on_again_does_not_twin_a_flight(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    p, out = run(led, owner_flight(), log=log)
    assert [a.kind for a in p.actions] == ["file"] and len(log.flights) == 1
    trip_item_id = out[0].trip_item_id
    # the flight log off: the same email is a duplicate, a changed one updates the same booking
    assert note_airtrail(store(led), led, on=False, today=EMAIL.date()) == "off"
    p, _ = run(led, owner_flight(msg="<m2>"), log=log)
    assert [a.kind for a in p.actions] == ["duplicate"]
    p, out = run(
        led,
        owner_flight(dep="2027-06-22T08:30", msg="<m3>", email=LATER),
        doc="change",
        log=log,
    )
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "changed")]
    assert (out[0].result, out[0].sync) == ("updated", "off")
    assert out[0].trip_item_id == trip_item_id and sent(log) == ["create"]
    # on again: the booking is still the one flight the flight log holds, and the change made
    # while it was off is sent to that flight
    assert note_airtrail(store(led), led, on=True, today=EMAIL.date()) == "on"
    p, _ = run(led, owner_flight(msg="<m4>"), log=log)
    assert [a.kind for a in p.actions] == ["duplicate"]
    assert len(log.flights) == 1 and sent(log) == ["create", "update"]
    (ti,) = bookings(led)
    assert (ti.item_key, ti.airtrail_flight_id) == ("DL|9123|2027-06-22|MSP", log.flights[0]["id"])
    assert ti.sync_state == "synced" and log.flights[0]["departure"] == ti.payload["dep_utc"]
    # and the next email that changes it updates that flight
    later = owner_flight(msg="<m5>", status="cancelled", email=LATER + dt.timedelta(hours=1))
    _, out = run(led, later, doc="cancellation", log=log)
    assert out[0].sync == "synced" and len(log.flights) == 1
    assert sent(log) == ["create", "update", "update"]


def test_another_travellers_receipt_on_a_split_booking_never_rekeys_the_owners_flight(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (mine,) = items_for(["Alex Sample"])
    _, first = run(led, [mine], log=log)
    day_later = dict(dep="2027-06-23T08:20", arr="2027-06-23T11:55", msg="<m2>", email=LATER)
    (own,) = items_for(["Alex Sample"], **day_later)
    (theirs,) = items_for(["Casey Sample"], **day_later)
    # the owner's own notice is the booking that moved; another traveller's is not
    assert moved_from(own, store(led), BOOK).id == first[0].trip_item_id
    assert moved_from(theirs, store(led), BOOK) is None
    p, out = run(led, [theirs], doc="change", log=log)
    assert [a.kind for a in p.actions] == ["file"] and out[0].sync == "off"
    kept, split = bookings(led)
    assert (kept.item_key, kept.passengers, kept.sync_state) == (
        mine.key,
        ["Alex Sample"],
        "synced",
    )
    assert (split.item_key, split.passengers) == (theirs.key, ["Casey Sample"])
    (held,) = log.flights
    assert held["date"] == mine.payload["date_local"] and sent(log) == ["create"]


def test_two_bookings_that_could_have_moved_move_neither(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    day_later = dict(dep="2027-06-23T08:20", arr="2027-06-23T11:55")
    (first,) = items_for(["Alex Sample"])
    (second,) = items_for(["Alex Sample"], number="567", msg="<m2>", **day_later)
    run(led, [first])
    # with one booking under the code, the same number a day later is that booking, moved
    (notice,) = items_for(["Alex Sample"], msg="<m3>", email=LATER, **day_later)
    assert moved_from(notice, store(led), BOOK).item_key == first.key
    run(led, [second])
    # with two (the first a day earlier, the second under another number that day), neither
    assert moved_from(notice, store(led), BOOK) is None
    p, out = run(led, [notice], doc="change")
    assert [a.kind for a in p.actions] == ["file"] and out[0].result == "filed"
    assert sorted(ti.item_key for ti in bookings(led)) == sorted(
        [first.key, second.key, notice.key]
    )


def train(
    number,
    dep="2027-06-22T08:20",
    msg="<m1>",
    email=EMAIL,
    names=("Casey Sample",),
    status="confirmed",
):
    """One traveller's train between two cities, under one booking code."""
    res = Reservation(
        kind="train",
        status=status,
        confirmation_code="RE1",
        provider="Rail Example",
        passengers=list(names),
        segments=[
            Segment(
                train_number=number,
                origin="MINNEAPOLIS",
                destination="PHILADELPHIA",
                departure_local=dep,
            )
        ],
    )
    return validate_extraction(
        Extraction(document_type="booking", confidence=1.0, reservations=[res]),
        refdata=airports(),
        book=BOOK,
        email_date=email,
        source={"message_id": msg, "email_date": email.isoformat()},
    )


def test_a_train_given_another_number_or_another_day_is_the_same_booking(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    (booked,) = train("9051")
    _, first = run(led, [booked])
    assert first[0].result == "filed" and booked.key.startswith("train|9051|")
    # another number that day: its times say nothing new, and it is still the booking, moved
    (renumbered,) = train("9052", msg="<m2>", email=LATER)
    p, out = run(led, [renumbered], doc="change")
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "rescheduled")]
    assert p.actions[0].changes == {"train_number": ("9051", "9052")}
    (ti,) = bookings(led)
    assert ti.item_key == renumbered.key and ti.payload["train_number"] == "9052"
    assert ti.payload["alt_keys"] == [booked.key] and ti.payload["change_recorded"] is True
    # the same number on another day
    (later,) = train("9052", dep="2027-06-23T08:20", msg="<m3>", email=LATER)
    p, out = run(led, [later], doc="change")
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "rescheduled")]
    assert p.actions[0].changes == {"dep_local": ("2027-06-22T08:20", "2027-06-23T08:20")}
    (ti,) = bookings(led)
    assert ti.item_key == later.key and ti.start_at == "2027-06-23T08:20"
    # the first receipt again, forwarded late, is an older copy of it
    p, out = run(led, train("9051", msg="<m4>"))
    assert [(a.kind, a.reason) for a in p.actions] == [("ignore", "older_copy")]
    assert len(bookings(led)) == 1 and bookings(led)[0].item_key == later.key
    # a cancellation that names nobody, for the train it has left, is for that train
    p, out = run(led, train("9051", msg="<m5>", email=LATER, names=()), doc="cancellation")
    assert [(a.kind, a.reason) for a in p.actions] == [("ignore", "older_copy")]
    assert bookings(led)[0].status == "confirmed"
    # and the same for the train it is on cancels it
    gone = train("9052", dep="2027-06-23T08:20", msg="<m6>", email=LATER, names=())
    p, out = run(led, gone, doc="cancellation")
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "cancel")]
    assert bookings(led)[0].status == "cancelled"


def test_inherited_names_cannot_carry_an_unverified_senders_flight_past_its_check(
    tmp_ledger_path,
):
    """A change notice that names nobody takes its names from the booking it is for; when it was
    auto-forwarded from a sender no list vouches for, the booking it changes is the owner's
    all the same, and nothing of it reaches the flight log until the owner has looked."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    _, first = run(led, items_for(["Alex Sample", "Sam Sample"]), log=log)
    assert first[0].sync == "synced"
    (held,) = log.flights
    notice = items_for([], dep="2027-06-22T08:30", msg="<m2>", email=LATER, trusted=False)
    assert checks.codes(notice[0]) == ("unverified_sender", "owner_unknown")
    p, out = run(led, notice, doc="change", log=log)
    a = p.actions[0]
    assert (a.kind, a.reason) == ("update", "changed") and a.item.classified.me_present
    assert (out[0].result, out[0].sync) == ("updated", "blocked")
    assert out[0].checks == ("unverified_sender",)
    (ti,) = bookings(led)
    assert ti.payload["dep_local"] == "2027-06-22T08:30"  # filed as the email says
    assert log.flights == [held] and sent(log) == ["create"]  # and nothing is sent
    # a notice that names its travellers is held back in the same way
    named = items_for(
        ["Alex Sample"], dep="2027-06-22T07:40", msg="<m3>", email=LATER, trusted=False
    )
    _, out = run(led, named, doc="change", log=log)
    assert (out[0].sync, out[0].checks) == ("blocked", ("unverified_sender",))
    assert log.flights == [held]
    # on a flight the owner is not on, the sender is no check: there is no flight log to guard
    _, out = run(
        led, items_for(["Casey Sample"], number="567", msg="<m4>", trusted=False), log=log
    )
    assert (out[0].result, out[0].sync, out[0].checks) == ("filed", "off", ())


def test_an_unverified_senders_cancellation_is_filed_and_nothing_of_it_is_sent(tmp_ledger_path):
    """A cancellation, whole or of one traveller, from a sender no list vouches for is filed on
    the booking like any other; the flight log hears nothing of it until the owner has looked."""
    for names, reason in ((["Sam Sample"], "remove_passengers"), ([], "cancel")):
        led = Ledger.open(tmp_ledger_path.with_name(f"{reason}.db"))
        log = flight_log(led)
        run(led, items_for(["Alex Sample", "Sam Sample"]), log=log)
        (held,) = log.flights
        notice = items_for(names, msg="<m2>", status="cancelled", email=LATER, trusted=False)
        p, out = run(led, notice, doc="cancellation", log=log)
        assert (p.actions[0].kind, p.actions[0].reason) == ("update", reason)
        assert (out[0].result, out[0].sync) == ("updated", "blocked")
        assert out[0].checks == ("unverified_sender",)
        (ti,) = bookings(led)
        gone = reason == "cancel"
        assert (ti.status == "cancelled") is gone and ("Sam Sample" in ti.passengers) is gone
        assert log.flights == [held] and sent(log) == ["create"]
        # the same from a sender that is vouched for goes through
        led = Ledger.open(tmp_ledger_path.with_name(f"{reason}-vouched.db"))
        log = flight_log(led)
        run(led, items_for(["Alex Sample", "Sam Sample"]), log=log)
        notice = items_for(names, msg="<m2>", status="cancelled", email=LATER)
        _, out = run(led, notice, doc="cancellation", log=log)
        assert (out[0].sync, out[0].checks) == ("synced", ()) and sent(log) == ["create", "update"]


def test_a_partial_cancellation_read_twice_leaves_the_booking_as_the_first_reading_left_it(
    tmp_ledger_path,
):
    """A cancellation acts on the travellers the booking holds. Read a second time, it names
    nobody the booking still has: nothing is cancelled, in the trip or in the flight log."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, items_for(["Alex Sample", "Sam Sample", "Robin Sample"]), log=log)

    def cancelled(names, msg, ledger=led, flight_log=log):
        notice = items_for(names, msg=msg, status="cancelled", email=LATER)
        p, (o,) = run(ledger, notice, "cancellation", log=flight_log)
        return p.actions[0], o

    # one traveller the booking holds and one it does not: the one it holds leaves
    a, o = cancelled(["SAM SAMPLE", "Casey Sample"], "<m2>")
    assert (a.kind, a.reason, a.remove_passengers) == (
        "update",
        "remove_passengers",
        ["Sam Sample"],
    )
    assert (o.result, o.sync) == ("updated", "synced")
    # the same again, and one naming only a stranger: they cancel nobody this booking has
    for names in (["SAM SAMPLE", "Casey Sample"], ["Sam Sample"], ["Casey Sample"]):
        a, o = cancelled(names, f"<again-{len(names)}-{names[0]}>")
        assert a.kind == "duplicate" and (o.result, o.sync) == ("duplicate", "synced")
    (ti,) = bookings(led)
    assert ti.status == "confirmed" and ti.passengers == ["Alex Sample", "Robin Sample"]
    (held,) = log.flights
    assert "CANCELLED" not in held["note"]
    assert aboard(held) == [("me", None), ("Robin Sample", None)]
    assert sent(log) == ["create", "update"]
    # the owner named: the booking is cancelled
    a, o = cancelled(["SAMPLE/ALEX QUINN"], "<m5>")
    assert (a.kind, a.reason) == ("update", "cancel")
    assert bookings(led)[0].status == "cancelled" and o.sync == "synced"
    assert log.flights[0]["note"].startswith("CANCELLED ")
    # every traveller a booking holds, however the email prints them, and nobody at all
    for n, names in enumerate((["ROBIN SAMPLE", "SAMPLE/SAM"], [])):
        other = Ledger.open(tmp_ledger_path.with_name(f"whole-{n}.db"))
        run(other, items_for(["Sam Sample", "Robin Sample"]))
        a, o = cancelled(names, "<m6>", other, None)
        assert (a.kind, a.reason, a.remove_passengers) == ("update", "cancel", []), names
        assert bookings(other)[0].status == "cancelled"


def test_the_first_receipt_read_after_a_partial_cancellation_puts_nobody_back(tmp_ledger_path):
    """A partial cancellation takes a companion off; the first receipt, sent before it and read
    after it (forwarded late, a mail backfill, an upload), is a duplicate: the companion stays
    off the booking and off the flight log's flight. An older email that changes the flight or
    adds someone else still does that, without the companion; a newer one (a rebooking) puts
    the companion back."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    both = ["Alex Sample", "Sam Sample"]
    run(led, items_for(both), log=log)
    notice = items_for(["Sam Sample"], msg="<m2>", status="cancelled", email=LATER)
    p, (o,) = run(led, notice, "cancellation", log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "remove_passengers")]
    (ti,) = bookings(led)
    assert ti.payload["cancelled_names"] == {"Sam Sample": LATER.isoformat()}
    assert sent(log) == ["create", "update"]
    p, (o,) = run(led, items_for(both, msg="<m3>"), log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("duplicate", "")]
    (ti,) = bookings(led)
    assert ti.passengers == ti.payload["passengers"] == ["Alex Sample"]
    assert o.sync == "synced" and [aboard(f) for f in log.flights] == [[("me", None)]]
    assert sent(log) == ["create", "update"]
    # sent between the receipt and the cancellation: Robin joins, Sam does not
    between = EMAIL + dt.timedelta(minutes=30)
    trio = [*both, "Robin Sample"]
    p, (o,) = run(led, items_for(trio, msg="<m4>", email=between), log=log)
    assert [(a.kind, a.reason, a.add_passengers) for a in p.actions] == [
        ("update", "add_passengers", ["Robin Sample"])
    ]
    (ti,) = bookings(led)
    assert ti.passengers == ti.payload["passengers"] == ["Alex Sample", "Robin Sample"]
    assert ti.payload["cancelled_names"] == {"Sam Sample": LATER.isoformat()}
    assert [aboard(f) for f in log.flights] == [[("me", None), ("Robin Sample", None)]]
    # a rebooking sent after the cancellation puts Sam back
    p, (o,) = run(led, items_for(both, msg="<m5>", email=LATER + dt.timedelta(hours=1)), log=log)
    assert [(a.kind, a.reason, a.add_passengers) for a in p.actions] == [
        ("update", "add_passengers", ["Sam Sample"])
    ]
    (ti,) = bookings(led)
    assert ti.passengers == ["Alex Sample", "Robin Sample", "Sam Sample"]
    assert [aboard(f) for f in log.flights] == [
        [("me", None), ("Robin Sample", None), ("Sam Sample", None)]
    ]


def test_a_cancellation_naming_the_owner_cancels_only_a_booking_the_owner_is_on(tmp_ledger_path):
    """A companion's or a relative's flight that the owner's own receipt never reached is not
    cancelled because the owner cancelled a seat of their own: on a booking that holds
    travellers and not the owner, the owner's is one more name the booking does not hold."""
    cases = (
        (["Alex Sample"], ("duplicate", "", []), "confirmed", ["Sam Sample", "Robin Sample"]),
        (
            ["SAMPLE/ALEX QUINN", "SAM SAMPLE"],
            ("update", "remove_passengers", ["Sam Sample"]),
            "confirmed",
            ["Robin Sample"],
        ),
        (
            ["Alex Sample", "Sam Sample", "Robin Sample"],
            ("update", "cancel", []),
            "cancelled",
            ["Sam Sample", "Robin Sample"],
        ),
    )
    for n, (names, want, status, left) in enumerate(cases):
        led = Ledger.open(tmp_ledger_path.with_name(f"owner-{n}.db"))
        log = flight_log(led)
        run(led, items_for(["Sam Sample", "Robin Sample"]), log=log)
        notice = items_for(names, msg="<m2>", status="cancelled", email=LATER)
        assert notice[0].classified.me_present
        p, (o,) = run(led, notice, doc="cancellation", log=log)
        a = p.actions[0]
        assert (a.kind, a.reason, a.remove_passengers) == want, names
        (ti,) = bookings(led)
        assert (ti.status, ti.passengers) == (status, left), names
        assert o.sync == "off" and log.requests == [], names  # never the owner's flight
    # a booking that holds no name at all is cancelled by a cancellation that names the owner
    led = Ledger.open(tmp_ledger_path.with_name("nameless-stay.db"))
    run(led, items_for([], kind="lodging", pnr="H1"))
    notice = items_for(
        ["Alex Sample"], kind="lodging", pnr="H1", msg="<m2>", status="cancelled", email=LATER
    )
    p, _ = run(led, notice, doc="cancellation")
    assert (p.actions[0].kind, p.actions[0].reason) == ("update", "cancel")
    assert bookings(led)[0].status == "cancelled"


def test_a_cancellation_for_the_flight_a_booking_has_left_is_an_older_copy(tmp_ledger_path):
    """Cancelled and rebooked: the booking has moved, by a recorded change, off the flight the
    cancellation names. A cancellation for that flight that names everyone the booking holds,
    or nobody, changes nothing of the booking, and the flight log hears nothing. One that
    singles out a traveller may be for that traveller's seat, so it is decided like any
    cancellation of the booking: the owner named cancels it, a companion named leaves it."""
    both = ["Alex Sample", "Sam Sample"]
    me, sam = ("me", None), ("Sam Sample", None)
    cases = (
        (both, ("ignore", "older_copy"), "confirmed", both, [me, sam]),
        ([*both, "Casey Sample"], ("ignore", "older_copy"), "confirmed", both, [me, sam]),
        ([], ("ignore", "older_copy"), "confirmed", both, [me, sam]),
        (["Casey Sample"], ("duplicate", ""), "confirmed", both, [me, sam]),
        (["Sam Sample"], ("update", "remove_passengers"), "confirmed", ["Alex Sample"], [me]),
        (["Alex Sample"], ("update", "cancel"), "cancelled", both, [me, sam]),
        (["Alex Sample", "Casey Sample"], ("update", "cancel"), "cancelled", both, [me, sam]),
    )
    for n, (names, want, status, left, riding) in enumerate(cases):
        led = Ledger.open(tmp_ledger_path.with_name(f"left-{n}.db"))
        log = flight_log(led)
        (booked,) = items_for(both, number="567")
        run(led, [booked], log=log)
        rebooked = items_for(both, number="0678", dep="2027-06-22T07:10", msg="<m2>", email=LATER)
        run(led, rebooked, doc="change", log=log)
        (ti,) = bookings(led)
        assert ti.item_key == rebooked[0].key and store(led).find(booked.key).id == ti.id
        (held,) = log.flights
        late = items_for(
            names,
            number="567",
            msg="<late>",
            status="cancelled",
            email=LATER + dt.timedelta(hours=1),
        )
        p, (o,) = run(led, late, doc="cancellation", log=log)
        assert [(a.kind, a.reason) for a in p.actions] == [want], names
        assert (o.trip_item_id, o.sync) == (ti.id, "synced"), names
        (after,) = bookings(led)
        assert (after.item_key, after.status, after.passengers) == (ti.item_key, status, left)
        (there,) = log.flights  # the one flight, moved; never a second
        assert there["flightNumber"] == rebooked[0].payload["flight_code"]
        assert aboard(there) == riding, names
        assert there["note"].startswith("CANCELLED ") is (status == "cancelled"), names
        if want[0] != "update":
            assert there == held and sent(log) == ["create", "update"], names
    # the flight it is on now is cancelled by a cancellation that names that flight
    current = items_for(["Alex Sample"], number="678", msg="<m9>", status="cancelled", email=LATER)
    p, (o,) = run(led, current, doc="cancellation", log=log)
    assert (p.actions[0].kind, p.actions[0].reason) == ("update", "cancel")
    assert bookings(led)[0].status == "cancelled"


def test_a_cancellation_under_a_key_the_owner_corrected_cancels_that_booking(tmp_ledger_path):
    """A stay answers to two keys once the owner has corrected its code, and its key never
    moves: a cancellation printing either code is for that stay, whether or not a change to it
    was recorded before."""
    for changed in (False, True):
        for code in ("H1", "H7"):
            led = Ledger.open(tmp_ledger_path.with_name(f"{code}-{changed}.db"))
            _, (first,) = run(led, items_for(["Casey Sample"], kind="lodging", pnr="H1"))
            if changed:  # a later email moves the check-out
                longer = items_for(
                    ["Casey Sample"],
                    kind="lodging",
                    pnr="H1",
                    arr="2027-06-26T11:00",
                    msg="<m2>",
                    email=LATER,
                )
                p, _ = run(led, longer, doc="change")
                assert (p.actions[0].kind, p.actions[0].reason) == ("update", "changed")
            st = store(led)
            st.edit_item(first.trip_item_id, {"confirmation_code": "H7"})
            (stay,) = bookings(led)
            assert stay.item_key == "lodging|H1" and stay.payload["alt_keys"] == ["lodging|H7"]
            assert bool(stay.payload.get("change_recorded")) is changed
            cancel = items_for(
                ["Casey Sample"],
                kind="lodging",
                pnr=code,
                msg="<m3>",
                status="cancelled",
                email=LATER + dt.timedelta(hours=1),
            )
            p, (o,) = run(led, cancel, doc="cancellation")
            assert (p.actions[0].kind, p.actions[0].reason) == ("update", "cancel"), (code, changed)
            assert o.result == "updated" and bookings(led)[0].status == "cancelled"


def test_a_cancellation_under_the_day_the_owner_corrected_cancels_the_flight(tmp_ledger_path):
    """The owner corrects a flight's day on the page: its key moves, the key it leaves stays an
    alias, and no change is recorded. A cancellation printed with the old day is for that
    flight, not an older copy of one it left, so it cancels it, and the one flight in the flight
    log is marked cancelled."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (booked,) = items_for(["Alex Sample"])
    _, (first,) = run(led, [booked], log=log)
    fixed = store(led).edit_item(
        first.trip_item_id, {"dep_local": "2027-06-23T08:20", "arr_local": "2027-06-23T11:55"}
    )
    assert fixed.item_key != booked.key and fixed.payload["alt_keys"] == [booked.key]
    assert not fixed.payload.get("change_recorded")
    cancel = items_for(["Alex Sample"], msg="<m2>", status="cancelled", email=LATER)
    assert cancel[0].key == booked.key
    p, (o,) = run(led, cancel, doc="cancellation", log=log)
    assert (p.actions[0].kind, p.actions[0].reason) == ("update", "cancel")
    (ti,) = bookings(led)
    assert (ti.id, ti.status, ti.item_key) == (first.trip_item_id, "cancelled", fixed.item_key)
    assert o.sync == "synced"
    (held,) = log.flights
    assert held["date"] == fixed.payload["date_local"] and held["note"].startswith("CANCELLED ")
    assert sent(log) == ["create", "update"]


def test_an_older_receipt_never_puts_back_a_traveller_the_owner_took_off(tmp_ledger_path):
    """The owner takes themselves off a sent flight on the page: it leaves the reconcile, and a
    receipt sent before the edit that names the owner again leaves the travellers as the owner
    set them, so nothing is sent for a flight the owner is not on."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    both = ["Alex Sample", "Sam Sample"]
    _, (first,) = run(led, items_for(both), log=log)
    (held,) = log.flights
    off = store(led).edit_item(first.trip_item_id, {"passengers": ["Sam Sample"]})
    assert (off.passengers, off.sync_state) == (["Sam Sample"], "off")
    _, (o,) = run(led, items_for(both, msg="<m2>"), log=log)
    (ti,) = bookings(led)
    assert (o.trip_item_id, o.sync) == (ti.id, "off")
    assert ti.passengers == ti.payload["passengers"] == ["Sam Sample"]
    assert log.flights == [held] and sent(log) == ["create"]


def test_an_older_receipt_never_puts_back_a_companion_the_owner_took_off(tmp_ledger_path):
    """The owner takes a companion off a sent flight on the page; the first receipt read again
    names them and changes nothing: they stay off the booking and off the flight log's flight."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    both = ["Alex Sample", "Sam Sample"]
    _, (first,) = run(led, items_for(both), log=log)
    store(led).edit_item(first.trip_item_id, {"passengers": ["Alex Sample"]})
    p, (o,) = run(led, items_for(both, msg="<m2>"), log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("duplicate", "")]
    (ti,) = bookings(led)
    assert ti.passengers == ti.payload["passengers"] == ["Alex Sample"]
    assert o.sync == "synced" and [aboard(f) for f in log.flights] == [[("me", None)]]
    assert sent(log) == ["create", "update"]  # the companion taken off there by the edit


def test_a_companions_older_receipt_after_the_owners_answer_joins_the_flight(tmp_ledger_path):
    """A flight that names nobody; the owner answers that they were on it; then a companion's
    receipt, sent before the answer, arrives: the companion joins the booking and the flight
    log's flight, since the answer took nobody off."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    _, (first,) = run(led, items_for([]), log=log)
    assert first.sync == "blocked"
    store(led).owner_answer(first.trip_item_id, True)
    p, (o,) = run(led, items_for(["Sam Sample"], msg="<m2>"), log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "add_passengers")]
    (ti,) = bookings(led)
    assert ti.passengers == ["Alex Sample", "Sam Sample"] and o.sync == "synced"
    (held,) = log.flights
    assert aboard(held) == [("me", None), ("Sam Sample", None)]


def test_an_older_copy_after_the_owner_corrects_the_day_changes_nothing(tmp_ledger_path):
    """The owner corrects a flight's day on the page; the first receipt, read again, is a
    duplicate: it records no change, so a cancellation printed with the old day is still for
    the flight and cancels it."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    (booked,) = items_for(["Alex Sample"])
    _, (first,) = run(led, [booked], log=log)
    store(led).edit_item(
        first.trip_item_id, {"dep_local": "2027-06-23T08:20", "arr_local": "2027-06-23T11:55"}
    )
    p, (o,) = run(led, items_for(["Alex Sample"], msg="<m2>"), log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("duplicate", "")]
    assert o.result == "duplicate"
    (ti,) = bookings(led)
    assert not ti.payload.get("change_recorded") and ti.payload["dep_local"] == "2027-06-23T08:20"
    cancel = items_for(["Alex Sample"], msg="<m3>", status="cancelled", email=LATER)
    p, _ = run(led, cancel, doc="cancellation", log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "cancel")]
    (ti,) = bookings(led)
    assert ti.status == "cancelled" and log.flights[0]["note"].startswith("CANCELLED ")


def test_an_email_still_moves_what_the_owner_did_not_edit(tmp_ledger_path):
    """The owner corrects a flight's arrival on the page; a later booking email moves its
    departure: the departure moves, the owner's arrival stays, and the flight log follows."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    _, (first,) = run(led, items_for(["Alex Sample"]), log=log)
    held = store(led).item(first.trip_item_id).payload
    lands = dt.datetime.fromisoformat(held["arr_local"]) + dt.timedelta(hours=1)
    lands = lands.strftime("%Y-%m-%dT%H:%M")
    mine = store(led).edit_item(first.trip_item_id, {"arr_local": lands})
    moved = items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m2>", email=LATER)
    p, (o,) = run(led, moved, log=log)
    (a,) = p.actions
    assert (a.kind, a.reason, set(a.changes)) == ("update", "changed", {"dep_utc"})
    (ti,) = bookings(led)
    assert ti.payload["dep_local"] == moved[0].payload["dep_local"]
    assert (ti.payload["arr_local"], ti.payload["arr_utc"]) == (lands, mine.payload["arr_utc"])
    (flight,) = log.flights
    assert flight["departure"] == ti.payload["dep_utc"] and o.sync == "synced"
    assert flight["arrival"] == ti.payload["arr_utc"]


def test_a_flight_that_loses_its_key_on_the_page_is_one_flight_when_it_is_fixed(tmp_ledger_path):
    """The owner moves a sent flight to the next day and types a city where its origin belongs:
    its fields make no key, so it takes a draft key, keeps its flight-log record and waits on
    the check. Whether the airline's email for the new day completes it or the owner fixes the
    origin on the page, it takes the new day's key with both earlier keys as aliases, and the
    one flight in the flight log is moved."""
    for fixed_by in ("email", "page"):
        led = Ledger.open(tmp_ledger_path.with_name(f"lost-{fixed_by}.db"))
        log = flight_log(led)
        (booked,) = items_for(["Alex Sample"])
        _, (first,) = run(led, [booked], log=log)
        (held,) = log.flights
        later = items_for(
            ["Alex Sample"],
            dep="2027-06-23T08:20",
            arr="2027-06-23T11:55",
            msg="<m2>",
            email=LATER,
        )[0]
        lost = store(led).edit_item(
            first.trip_item_id,
            {
                "dep_local": later.payload["dep_local"],
                "arr_local": later.payload["arr_local"],
                "origin": "MINNEAPOLIS",
            },
            at=EMAIL.isoformat(),
        )
        assert is_draft(lost.item_key) and lost.payload["alt_keys"] == [booked.key], fixed_by
        assert (lost.checks, lost.sync_state) == (("origin", "departure"), "blocked"), fixed_by
        assert lost.airtrail_flight_id == held["id"], fixed_by
        if fixed_by == "email":
            p, _ = run(led, [later], log=log)
            assert [(a.kind, a.reason) for a in p.actions] == [("update", "completes_draft")]
        else:
            store(led).edit_item(first.trip_item_id, {"origin": booked.payload["origin"]})
            run(led, [later], log=log)
        (ti,) = bookings(led)
        assert (ti.id, ti.item_key) == (first.trip_item_id, later.key), fixed_by
        assert ti.payload["alt_keys"] == [booked.key, lost.item_key], fixed_by
        assert (ti.checks, ti.sync_state) == ((), "synced"), fixed_by
        (flight,) = log.flights
        assert (flight["id"], flight["date"]) == (held["id"], "2027-06-23"), fixed_by
        assert sent(log) == ["create", "update"], fixed_by


def keyless(led, log, fields: dict):
    """The owner's flight filed and sent, then edited on the page (`fields`, stamped at the
    email's own time) so that its fields make no key: the flight under its own draft key, and
    the key it left."""
    (booked,) = items_for(["Alex Sample"])
    _, (first,) = run(led, [booked], log=log)
    lost = store(led).edit_item(first.trip_item_id, fields, at=EMAIL.isoformat())
    assert is_draft(lost.item_key) and lost.payload["alt_keys"] == [booked.key]
    assert lost.airtrail_flight_id == log.flights[0]["id"]
    return lost, booked


def test_a_flight_whose_date_is_cleared_on_the_page_is_moved_by_the_next_change(tmp_ledger_path):
    """The owner clears a sent flight's date on the page; a change email under its code moves
    it to another day: it is the same booking, rescheduled, and the one flight in the flight
    log moves with it."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    lost, booked = keyless(led, log, {"dep_local": "", "arr_local": ""})
    assert (lost.trip_id, lost.start_at) == (None, None)
    later = items_for(
        ["Alex Sample"], dep="2027-06-23T08:20", arr="2027-06-23T11:55", msg="<m2>", email=LATER
    )
    p, (o,) = run(led, later, doc="change", log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "rescheduled")]
    (ti,) = bookings(led)
    assert (ti.id, ti.item_key) == (lost.id, later[0].key) and o.sync == "synced"
    assert ti.payload["alt_keys"] == [booked.key, lost.item_key]
    (flight,) = log.flights
    assert (flight["id"], flight["date"]) == (lost.airtrail_flight_id, "2027-06-23")
    assert sent(log) == ["create", "update"]


def test_a_flight_whose_number_is_cleared_on_the_page_is_moved_by_the_next_change(
    tmp_ledger_path,
):
    """The owner clears a sent flight's number and leaves its departure a day alone; a change
    email under its code gives it another number on that day: the same booking, one flight."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    lost, _ = keyless(led, log, {"flight_number": "", "dep_local": "2027-06-22"})
    renumbered = items_for(["Alex Sample"], number="9246", msg="<m2>", email=LATER)
    p, (o,) = run(led, renumbered, doc="change", log=log)
    assert [(a.kind, a.reason) for a in p.actions] == [("update", "rescheduled")]
    (ti,) = bookings(led)
    assert (ti.id, ti.item_key, o.sync) == (lost.id, renumbered[0].key, "synced")
    (flight,) = log.flights
    assert flight["flightNumber"] == renumbered[0].payload["flight_code"]
    assert sent(log) == ["create", "update"]


def test_a_draft_an_email_filed_is_never_the_booking_a_flight_moved_from(tmp_ledger_path):
    """A flight filed from an email that printed no date is that email's draft. A later email
    for the flight on a day is no move of it (only a booking an edit left keyless is one): it
    is a booking of its own, and the draft waits as it was, to be completed on the page."""
    led = Ledger.open(tmp_ledger_path)
    _, (undated,) = run(led, items_for(["Alex Sample"], dep="", arr=""))
    draft = store(led).item(undated.trip_item_id)
    assert is_draft(draft.item_key) and draft.start_at is None
    p, (o,) = run(led, items_for(["Alex Sample"], msg="<m2>", email=LATER), doc="change")
    assert [a.kind for a in p.actions] == ["file"]
    assert o.trip_item_id != draft.id and store(led).item(draft.id) == draft


def test_a_change_naming_only_a_stranger_does_not_move_a_flight_left_keyless(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    lost, _ = keyless(led, log, {"dep_local": "", "arr_local": ""})
    theirs = items_for(
        ["Casey Sample"], dep="2027-06-23T08:20", arr="2027-06-23T11:55", msg="<m2>", email=LATER
    )
    p, (o,) = run(led, theirs, doc="change", log=log)
    assert [a.kind for a in p.actions] == ["file"] and o.sync == "off"
    assert store(led).item(lost.id) == lost and len(bookings(led)) == 2
    assert sent(log) == ["create"]


def test_a_draft_with_a_record_in_the_flight_log_is_not_merged_into_another_on_the_page(
    tmp_ledger_path,
):
    """A sent flight that lost its key on the page holds its record in the flight log. Fixing it
    on the page into a flight another booking already is would leave that record behind on a
    day it no longer has, so it is refused as for any booking, and nothing is stored."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    _, (first,) = run(led, items_for(["Alex Sample"]), log=log)
    _, (other,) = run(
        led,
        items_for(
            ["Alex Sample"],
            dep="2027-06-23T08:20",
            arr="2027-06-23T11:55",
            pnr="FAM123",  # booked apart: the same flight the next day
            msg="<m2>",
        ),
        log=log,
    )
    st = store(led)
    assert other.trip_item_id != first.trip_item_id
    taken = st.item(other.trip_item_id).payload
    lost = st.edit_item(
        first.trip_item_id,
        {"dep_local": taken["dep_local"], "arr_local": taken["arr_local"], "origin": "MINNEAPOLIS"},
    )
    assert is_draft(lost.item_key) and lost.airtrail_flight_id is not None
    with pytest.raises(ValueError, match="^another booking already has this flight$"):
        st.edit_item(first.trip_item_id, {"origin": taken["origin"]})
    assert st.item(first.trip_item_id) == lost and len(bookings(led)) == 2
    assert sent(log) == ["create", "create"]


def test_a_message_that_lists_the_cancelled_leg_beside_the_new_one_moves_the_booking(
    tmp_ledger_path,
):
    """One message may print the flight a booking leaves, as cancelled, beside the flight it
    moves to. In either order the booking moves, and the new leg says who rides on. When it
    names everyone the cancellation takes off (the travellers it names; all the booking holds
    when it names nobody), the cancellation is for the flight being left and the booking stays
    live. When it leaves one of them out, the cancellation is decided like any other: the owner
    taken off cancels the booking (no live flight with the owner aboard), a companion taken off
    leaves it and does not ride to the new flight. Another booking the same message cancels is
    cancelled as it says."""
    both = ["Alex Sample", "Sam Sample"]
    me, sam = ("me", None), ("Sam Sample", None)
    whole, moved_on = ("ignore", "older_copy"), ("update", "rescheduled")
    cancel, remove = ("update", "cancel"), ("update", "remove_passengers")
    cases = (  # the cancelled leg names, the new leg names, the cancelled leg's plan, what is left
        (both, both, whole, "confirmed", both, [me, sam]),
        ([], both, whole, "confirmed", both, [me, sam]),
        (["Sam Sample"], both, whole, "confirmed", both, [me, sam]),
        (["Alex Sample"], both, whole, "confirmed", both, [me, sam]),
        (["Casey Sample"], both, whole, "confirmed", both, [me, sam]),  # it takes nobody off
        (["Alex Sample"], ["Sam Sample"], cancel, "cancelled", both, [me, sam]),
        (both, ["Sam Sample"], cancel, "cancelled", both, [me, sam]),
        ([], ["Sam Sample"], cancel, "cancelled", both, [me, sam]),
        (["Sam Sample"], ["Alex Sample"], remove, "confirmed", ["Alex Sample"], [me]),
    )
    runs = [(old_first, case) for old_first in (True, False) for case in cases]
    for n, (old_first, (cancelled, onward, want, status, left_on, riding)) in enumerate(runs):
        led = Ledger.open(tmp_ledger_path.with_name(f"pair-{n}.db"))
        log = flight_log(led)
        _, (first,) = run(led, items_for(both, number="567"), log=log)
        _, (hotel,) = run(led, items_for(["Alex Sample"], kind="lodging", pnr="H1", msg="<h>"))
        left = items_for(cancelled, number="567", msg="<m2>", status="cancelled", email=LATER)
        new = items_for(onward, number="0678", dep="2027-06-22T07:10", msg="<m2>", email=LATER)
        stay = items_for(
            ["Alex Sample"], kind="lodging", pnr="H1", msg="<m2>", status="cancelled", email=LATER
        )
        pair = left + new if old_first else new + left
        p, out = run(led, pair + stay, doc="change", log=log)
        label = (old_first, cancelled, onward)
        assert {a.item.key: (a.kind, a.reason) for a in p.actions} == {
            left[0].key: want,
            new[0].key: moved_on,
            stay[0].key: ("update", "cancel"),
        }, label
        results = ["ignored" if want == whole else "updated", "updated", "updated"]
        assert sorted(o.result for o in out) == results, label
        st = store(led)
        flight = st.item(first.trip_item_id)
        assert (flight.item_key, flight.status) == (new[0].key, status), label
        assert flight.passengers == left_on and st.find(left[0].key).id == flight.id, label
        assert st.item(hotel.trip_item_id).status == "cancelled"
        (held,) = log.flights  # the one flight, moved
        assert held["flightNumber"] == new[0].payload["flight_code"]
        assert held["note"].startswith("CANCELLED ") is (status == "cancelled"), label
        assert aboard(held) == riding and sent(log) == ["create", "update"], label


def test_a_new_leg_read_below_the_confidence_floor_moves_the_booking_the_same_in_either_order(
    tmp_ledger_path,
):
    """Nothing is held back, so a new leg read below the model's confidence floor is decided
    like any other: it moves the booking, and the cancelled leg beside it is for the flight the
    booking leaves, whichever the message lists first. Its finding is a check on the moved
    booking, which keeps the move from the flight log until the owner has looked."""
    both = ["Alex Sample", "Sam Sample"]
    for n, old_first in enumerate((True, False)):
        led = Ledger.open(tmp_ledger_path.with_name(f"unsure-{n}.db"))
        log = flight_log(led)
        _, (first,) = run(led, items_for(both, number="567"), log=log)
        left = items_for(both, number="567", msg="<m2>", status="cancelled", email=LATER)
        new = items_for(
            both,
            number="0678",
            dep="2027-06-22T07:10",
            msg="<m2>",
            email=LATER,
            confidence=0.5,
            parser="haiku",
        )
        assert checks.codes(new[0]) == ("low_confidence",)
        p, _ = run(led, left + new if old_first else new + left, doc="change", log=log)
        assert {a.item.key: (a.kind, a.reason) for a in p.actions} == {
            left[0].key: ("ignore", "older_copy"),
            new[0].key: ("update", "rescheduled"),
        }, old_first
        (ti,) = bookings(led)
        assert (ti.id, ti.item_key, ti.status, ti.passengers) == (
            first.trip_item_id,
            new[0].key,
            "confirmed",
            both,
        ), old_first
        assert (ti.checks, ti.sync_state) == (("low_confidence",), "blocked"), old_first
        (held,) = log.flights  # the flight log still holds the flight it left
        assert held["flightNumber"] == left[0].payload["flight_code"] and sent(log) == ["create"]
        st = store(led)
        assert st.looks_right(ti.id).sync_state == "pending"
        Syncer(store=st, ledger=led, book=st.book, client=log, refs=None, dry_run=False).run_pass()
        (held,) = log.flights
        assert held["flightNumber"] == new[0].payload["flight_code"], old_first
        assert sent(log) == ["create", "update"]
        assert aboard(held) == [("me", None), ("Sam Sample", None)], old_first


def test_a_traveller_the_new_leg_names_is_not_removed_by_the_cancelled_leg_beside_it(
    tmp_ledger_path,
):
    """Three travellers: the cancelled leg names two of them, and the new leg names one of those
    two beside the owner. The one the new leg names rides on and the other leaves, whichever
    leg the message lists first."""
    onward = ["Alex Sample", "Sam Sample"]
    for n, old_first in enumerate((True, False)):
        led = Ledger.open(tmp_ledger_path.with_name(f"three-{n}.db"))
        log = flight_log(led)
        run(led, items_for([*onward, "Casey Sample"], number="567"), log=log)
        left = items_for(
            ["Sam Sample", "Casey Sample"],
            number="567",
            msg="<m2>",
            status="cancelled",
            email=LATER,
        )
        new = items_for(onward, number="0678", dep="2027-06-22T07:10", msg="<m2>", email=LATER)
        p, _ = run(led, left + new if old_first else new + left, doc="change", log=log)
        took = {a.item.key: (a.kind, a.reason, a.remove_passengers) for a in p.actions}
        assert took[left[0].key] == ("update", "remove_passengers", ["Casey Sample"]), old_first
        (ti,) = bookings(led)
        assert (ti.item_key, ti.status, ti.passengers) == (new[0].key, "confirmed", onward)
        (held,) = log.flights
        assert aboard(held) == [("me", None), ("Sam Sample", None)], old_first


def test_a_message_that_moves_a_booking_again_goes_by_its_own_new_leg(tmp_ledger_path):
    """A booking a recorded change has moved once is moved again by a message that also cancels
    the flight it first left. That message's new leg says who rides on, so naming everyone does
    not make the cancellation an older copy: a new leg without the owner leaves the booking
    cancelled."""
    both = ["Alex Sample", "Sam Sample"]
    later = LATER + dt.timedelta(hours=1)
    cases = (
        (both, ("ignore", "older_copy"), "confirmed"),
        (["Sam Sample"], ("update", "cancel"), "cancelled"),
    )
    for n, (onward, want, status) in enumerate(cases):
        led = Ledger.open(tmp_ledger_path.with_name(f"again-{n}.db"))
        log = flight_log(led)
        run(led, items_for(both, number="567"), log=log)
        once = items_for(both, number="0678", dep="2027-06-22T07:10", msg="<m2>", email=LATER)
        run(led, once, doc="change", log=log)
        left = items_for(both, number="567", msg="<m3>", status="cancelled", email=later)
        new = items_for(onward, number="9246", msg="<m3>", email=later)
        p, _ = run(led, left + new, doc="change", log=log)
        assert {a.item.key: (a.kind, a.reason) for a in p.actions} == {
            left[0].key: want,
            new[0].key: ("update", "rescheduled"),
        }, onward
        (ti,) = bookings(led)
        assert (ti.item_key, ti.status, ti.passengers) == (new[0].key, status, both), onward
        (held,) = log.flights
        assert held["flightNumber"] == new[0].payload["flight_code"]
        assert held["note"].startswith("CANCELLED ") is (status == "cancelled"), onward


def test_an_older_copy_beside_a_cancellation_moves_nothing_and_the_cancellation_stands(
    tmp_ledger_path,
):
    """A message cancels the flight a booking is on and lists another number that day as an
    older copy would: it adds a traveller and moves nothing. The booking stays on the flight
    the email cancels, so that cancellation is the booking's."""
    for n, old_first in enumerate((True, False)):
        led = Ledger.open(tmp_ledger_path.with_name(f"stale-{n}.db"))
        log = flight_log(led)
        (booked,) = items_for(["Alex Sample"], number="567", email=LATER + dt.timedelta(hours=1))
        run(led, [booked], log=log)
        left = items_for(["Alex Sample"], number="567", msg="<m2>", status="cancelled")
        copy = items_for(
            ["Alex Sample", "Sam Sample"], number="0678", dep="2027-06-22T07:10", msg="<m2>"
        )
        p, out = run(led, left + copy if old_first else copy + left, log=log)
        assert {a.item.key: (a.kind, a.reason, a.changes) for a in p.actions} == {
            left[0].key: ("update", "cancel", {}),
            copy[0].key: ("update", "add_passengers", {}),
        }, old_first
        (ti,) = bookings(led)
        assert (ti.item_key, ti.status) == (booked.key, "cancelled"), old_first
        (held,) = log.flights
        assert held["flightNumber"] == booked.payload["flight_code"]
        assert held["note"].startswith("CANCELLED "), old_first


def test_a_train_given_another_number_beside_its_cancellation_moves_and_stays_live(
    tmp_ledger_path,
):
    """A message gives a train another number that day and adds a traveller: the booking moves
    with everyone it names, and the cancellation the message lists beside it is for the train
    the booking leaves."""
    for n, old_first in enumerate((True, False)):
        led = Ledger.open(tmp_ledger_path.with_name(f"train-{n}.db"))
        run(led, train("9051"))
        left = train("9051", msg="<m2>", email=LATER, status="cancelled")
        new = train("9052", msg="<m2>", email=LATER, names=("Casey Sample", "Sam Sample"))
        p, _ = run(led, left + new if old_first else new + left, doc="change")
        assert {a.item.key: (a.kind, a.reason, a.changes) for a in p.actions} == {
            left[0].key: ("ignore", "older_copy", {}),
            new[0].key: ("update", "add_passengers", {"train_number": ("9051", "9052")}),
        }, old_first
        (ti,) = bookings(led)
        assert (ti.item_key, ti.status) == (new[0].key, "confirmed"), old_first
        assert ti.passengers == ["Casey Sample", "Sam Sample"]


def test_a_trains_new_leg_that_names_nobody_carries_everyone_and_a_nameless_cancellation_all(
    tmp_ledger_path,
):
    """A train may print no name. A new leg that names nobody carries everyone the booking
    holds, so the cancellation beside it is for the train being left; a cancellation that names
    nobody takes everyone off, so a new leg that names only one of them leaves the booking
    cancelled."""
    pair = ("Casey Sample", "Sam Sample")
    cases = (
        (("Casey Sample",), ("Casey Sample",), (), ("ignore", "older_copy"), "confirmed"),
        (pair, (), ("Sam Sample",), ("update", "cancel"), "cancelled"),
    )
    runs = [(old_first, case) for old_first in (True, False) for case in cases]
    for n, (old_first, (aboard_it, cancelled, onward, want, status)) in enumerate(runs):
        led = Ledger.open(tmp_ledger_path.with_name(f"rail-{n}.db"))
        run(led, train("9051", names=aboard_it))
        left = train("9051", msg="<m2>", email=LATER, names=cancelled, status="cancelled")
        new = train("9052", msg="<m2>", email=LATER, names=onward)
        p, _ = run(led, left + new if old_first else new + left, doc="change")
        label = (old_first, cancelled, onward)
        assert {a.item.key: (a.kind, a.reason) for a in p.actions} == {
            left[0].key: want,
            new[0].key: ("update", "rescheduled"),
        }, label
        (ti,) = bookings(led)
        assert (ti.item_key, ti.status) == (new[0].key, status), label
        assert ti.passengers == list(aboard_it), label


def test_an_older_copy_of_a_train_that_adds_a_traveller_leaves_its_number(tmp_ledger_path):
    """An older copy never rewinds a booking, and a train's number is part of what it would
    rewind: a copy older than the booking, under another number that day, adds the traveller
    it names and leaves the number, the key and the times as they were."""
    led = Ledger.open(tmp_ledger_path)
    (booked,) = train("9051", email=LATER)
    run(led, [booked])
    (before,) = bookings(led)
    p, _ = run(led, train("9052", msg="<m2>", names=("Casey Sample", "Sam Sample")))
    assert [(a.kind, a.reason, a.changes) for a in p.actions] == [("update", "add_passengers", {})]
    (ti,) = bookings(led)
    assert (ti.item_key, ti.payload["train_number"]) == (booked.key, "9051")
    assert ti.passengers == ["Casey Sample", "Sam Sample"]
    assert ti.start_at == before.start_at and not ti.payload.get("alt_keys")
    assert not ti.payload.get("change_recorded")


def test_a_nameless_cancellation_under_a_code_the_booking_shares_cancels_nobody(tmp_ledger_path):
    """A relative booked the owner's flight under a code of their own, so the booking holds both
    codes. A cancellation under either that names nobody does not say whose seat it cancels: it
    takes no names from the booking, files no booking of its own and leaves the booking as it
    is, marked for the owner to look at (it is kept from the flight log until then). Looks
    right clears the mark, and the flight is as the flight log holds it again."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    run(led, items_for(["Alex Sample"]), log=log)
    _, (joined,) = run(led, items_for(["Casey Sample"], pnr="ZZZ999", msg="<m2>"), log=log)
    (ti,) = bookings(led)
    assert joined.result == "updated" and ti.confirmation == "ABC123, ZZZ999"
    for n, code in enumerate(("ZZZ999", "ABC123")):
        (notice,) = items_for([], pnr=code, msg=f"<c-{code}>", status="cancelled", email=LATER)
        received = LATER + dt.timedelta(hours=n)
        p, (o,) = run(led, [notice], doc="cancellation", log=log, received=received)
        a = p.actions[0]
        assert (a.kind, a.reason, a.trip_item_id) == ("ignore", "unknown_cancellation", ti.id)
        assert checks.codes(notice) == ("owner_unknown",) and notice.passengers == [], code
        assert (o.result, o.trip_item_id, o.sync, o.checks) == (
            "ignored",
            ti.id,
            "blocked",
            ("unknown_cancellation",),
        ), code
        row = led.items.get(o.item_id)
        assert (row["outcome"], row["detail"], row["trip_item_id"]) == (
            "ignored",
            "unknown_cancellation",
            ti.id,
        ), code
        marked = store(led).item(ti.id)
        assert marked.payload["raised"]["unknown_cancellation"] == received.isoformat(), code
    (marked,) = bookings(led)
    assert marked.status == "confirmed" and marked.passengers == ["Alex Sample", "Casey Sample"]
    assert marked.source_message_id == ti.source_message_id
    (held,) = log.flights
    assert "CANCELLED" not in held["note"] and sent(log) == ["create"]
    looked = store(led).looks_right(ti.id)
    assert (looked.checks, looked.sync_state) == ((), "synced") and sent(log) == ["create"]
    # a notice that is no cancellation still takes the booking's names under either code
    (change,) = items_for([], pnr="ZZZ999", dep="2027-06-22T08:30", msg="<m5>", email=LATER)
    p, _ = run(led, [change], doc="change", log=log)
    assert (p.actions[0].kind, p.actions[0].reason) == ("update", "changed")
    assert change.classified.me_present and change.classified.others == ["Casey Sample"]
    # on a booking with that one code, the same nameless cancellation cancels it
    other = Ledger.open(tmp_ledger_path.with_name("one-code.db"))
    run(other, items_for(["Casey Sample"], pnr="ZZZ999"))
    (notice,) = items_for([], pnr="ZZZ999", msg="<c>", status="cancelled", email=LATER)
    p, _ = run(other, [notice], doc="cancellation")
    assert (p.actions[0].kind, p.actions[0].reason) == ("update", "cancel")
    assert bookings(other)[0].status == "cancelled"


def test_a_finding_is_stamped_when_its_message_was_received_never_at_the_emails_date(
    tmp_ledger_path,
):
    """The Date an email prints is the sender's to write. An unvouched sender's change that says
    it was sent before the owner last looked, and is received after, opens the check and holds
    the flight back; one received before that look stays cleared when it is read again."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    _, (first,) = run(led, items_for(["Alex Sample"]), log=log, received=EMAIL)
    st = store(led)

    def looks(at: dt.datetime):
        payload = st.item(first.trip_item_id).payload
        checks.looked(payload, at.isoformat())
        led.trip_items.update(first.trip_item_id, payload=payload)
        return st.settle(first.trip_item_id)

    hour = dt.timedelta(hours=1)
    looks(EMAIL + 2 * hour)
    # dated an hour after the booking, before the look; received an hour after the look
    notice = items_for(
        ["Alex Sample"], dep="2027-06-22T08:30", msg="<m2>", email=LATER, trusted=False
    )
    received = EMAIL + 3 * hour
    p, (o,) = run(led, notice, doc="change", log=log, received=received)
    assert (o.result, o.sync, o.checks) == ("updated", "blocked", ("unverified_sender",))
    ti = st.item(first.trip_item_id)
    assert ti.payload["raised"] == {"unverified_sender": received.isoformat()}
    assert ti.payload["email_date"] == LATER.isoformat() and sent(log) == ["create"]
    # the owner looks; the change goes to the flight log
    assert looks(EMAIL + 4 * hour).sync_state == "pending"
    _, (o,) = run(led, items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m3>"), log=log)
    assert (o.result, o.sync) == ("duplicate", "synced") and sent(log) == ["create", "update"]
    # a later change from a vouched sender, then the unvouched one read again: it is stamped
    # with the time it was received, before the look, and reopens nothing
    run(
        led,
        items_for(["Alex Sample"], dep="2027-06-22T07:40", msg="<m4>", email=EMAIL + 5 * hour),
        doc="change",
        log=log,
        received=EMAIL + 5 * hour,
    )
    again = items_for(
        ["Alex Sample"], dep="2027-06-22T08:30", msg="<m2>", email=LATER, trusted=False
    )
    p, (o,) = run(led, again, doc="change", log=log, received=received)
    assert (p.actions[0].kind, o.result) == ("update", "updated")
    assert (o.sync, o.checks) == ("synced", ())
    assert st.item(first.trip_item_id).payload["raised"] == {
        "unverified_sender": received.isoformat()
    }
    # with no received time given, a finding is stamped now
    (plain,) = items_for(["Casey Sample"], number="567", msg="<m5>", trusted=False)
    plan([plain], document_type="booking", store=st, book=BOOK)
    stamped = dt.datetime.fromisoformat(plain.payload["raised"]["unverified_sender"])
    assert abs(dt.datetime.now(dt.UTC) - stamped) < dt.timedelta(minutes=5)


def test_a_stale_copy_that_adds_a_traveller_leaves_the_bookings_date_the_latest(tmp_ledger_path):
    """The newest plain receipt is filed first. An older copy that adds a traveller keeps the
    booking's schedule and its date, so a copy dated between the two is still an older copy and
    rewinds nothing."""
    led = Ledger.open(tmp_ledger_path)
    newest = EMAIL + dt.timedelta(hours=2)
    run(led, items_for(["Alex Sample"], email=newest))
    p, _ = run(led, items_for(["Sam Sample"], dep="2027-06-22T08:30", msg="<m2>", email=EMAIL))
    a = p.actions[0]
    assert (a.kind, a.reason, a.changes) == ("update", "add_passengers", {})
    (ti,) = bookings(led)
    assert ti.payload["email_date"] == newest.isoformat()
    assert ti.passengers == ["Alex Sample", "Sam Sample"]
    p, _ = run(led, items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m3>", email=LATER))
    assert [a.kind for a in p.actions] == ["duplicate"]
    (ti,) = bookings(led)
    assert ti.payload["dep_local"] == "2027-06-22T08:20"
    assert ti.payload["email_date"] == newest.isoformat()
    # a newer email does move the date on
    newer = newest + dt.timedelta(hours=1)
    run(led, items_for(["Robin Sample"], msg="<m4>", email=newer))
    assert bookings(led)[0].payload["email_date"] == newer.isoformat()


def test_an_older_copy_is_told_by_the_instant_not_by_how_its_date_is_printed(tmp_ledger_path):
    """Two dates printed with different offsets compare as instants: a copy sent half an hour
    before the booking's email, in a zone five hours ahead, is the older one."""
    ahead = dt.timezone(dt.timedelta(hours=5))
    before = (EMAIL - dt.timedelta(minutes=30)).astimezone(ahead)
    assert before.isoformat() > EMAIL.isoformat() and before < EMAIL  # the text says otherwise
    assert earlier(before.isoformat(), EMAIL.isoformat())
    assert not earlier(EMAIL.isoformat(), before.isoformat())
    assert not earlier("not a date", EMAIL.isoformat()) and not earlier(None, EMAIL.isoformat())
    led = Ledger.open(tmp_ledger_path)
    run(led, items_for(["Alex Sample"]))
    p, _ = run(led, items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m2>", email=before))
    assert [a.kind for a in p.actions] == ["duplicate"]
    assert bookings(led)[0].payload["dep_local"] == "2027-06-22T08:20"


def test_an_identical_re_forward_of_a_removed_booking_stays_removed(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    _, first = run(led, items_for(["Alex Sample"]), log=log)
    st = store(led)
    with util.clock_at(EMAIL + dt.timedelta(minutes=30)):  # between the email and a later one
        st.remove_item(first[0].trip_item_id)
    p, out = run(led, items_for(["Alex Sample"], msg="<m2>"), log=log)
    assert [a.kind for a in p.actions] == ["duplicate"] and out[0].result == "duplicate"
    (ti,) = bookings(led)
    assert ti.removed_at is not None and st.all_trips() == []
    assert sent(log) == ["create"] and len(log.flights) == 1
    # an email sent after the removal that changes it brings the same booking back
    p, out = run(
        led,
        items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m3>", email=LATER),
        doc="change",
        log=log,
    )
    (ti,) = bookings(led)
    assert out[0].result == "updated" and ti.removed_at is None
    assert ti.id == first[0].trip_item_id and len(st.all_trips()) == 1


def test_an_email_for_a_flight_held_as_deleted_updates_the_booking_and_sends_nothing(
    tmp_ledger_path,
):
    """A flight the owner deleted by hand in the flight log is held out of the reconcile, and a
    flight that had left when the flight log was switched on is too: a later email updates the
    booking, and the flight log gains nothing."""
    led = Ledger.open(tmp_ledger_path)
    log = flight_log(led)
    _, first = run(led, items_for(["Alex Sample"]), log=log)
    log.deleted_by_hand(bookings(led)[0].airtrail_flight_id)
    _, out = run(
        led,
        items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m2>", email=LATER),
        doc="change",
        log=log,
    )
    (ti,) = bookings(led)  # the pass found it gone there and unlinked it
    assert (ti.sync_hold, out[0].sync, log.flights) == ("deleted", "off", [])
    for n, hold in enumerate(("deleted", "past")):
        if ti.sync_hold != hold:
            led.trip_items.update(ti.id, sync_hold=hold)
            assert store(led).settle(ti.id).sync_state == "off"
        log.requests.clear()
        at = LATER + dt.timedelta(hours=n + 1)
        dep = ("2027-06-22T07:40", "2027-06-22T07:10")[n]
        _, out = run(
            led,
            items_for(["Alex Sample"], dep=dep, msg=f"<m{n + 3}>", email=at),
            doc="change",
            log=log,
        )
        (ti,) = bookings(led)
        assert out[0].result == "updated" and ti.payload["dep_local"] == dep
        assert (ti.sync_hold, ti.sync_state, ti.airtrail_flight_id) == (hold, "off", None)
        assert log.requests == [] and log.flights == []


def test_the_executor_logs_one_row_per_booking_and_never_asks_the_flight_log(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    flight_log(led)  # a flight log is kept; the executor is given none
    st = store(led)
    ex = Executor(ledger=led, store=st, book=BOOK)

    def execute(items, doc="booking"):
        mid = led.messages.insert(
            sha256=f"x{led.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]}",
            source="cli",
            status="archived",
        )
        out = ex.execute(plan(items, document_type=doc, store=st, book=BOOK), message_row_id=mid)
        return out, led.items.by_message(mid)

    out, rows = execute(items_for(["Alex Sample"]) + items_for([], number="567"))
    assert [o.result for o in out] == ["filed", "filed"]
    assert [(r["n"], r["outcome"], r["detail"]) for r in rows] == [
        (0, "filed", None),
        (1, "filed", None),
    ]
    assert [r["id"] for r in rows] == [o.item_id for o in out]
    assert [r["item_key"] for r in rows] == [o.action.item.key for o in out]
    filed = out[0].trip_item_id
    assert out[1].trip_item_id is not None
    assert [r["trip_item_id"] for r in rows] == [filed, out[1].trip_item_id]
    assert (out[0].sync, out[0].checks, out[0].trip_title) == ("pending", (), "Philadelphia")
    assert (out[1].sync, out[1].checks) == ("blocked", ("owner_unknown",))
    assert st.item(filed).source_message_id == rows[0]["message_id"]
    for items, doc, result, detail in (
        (items_for(["Alex Sample"], msg="<m2>"), "booking", "duplicate", None),
        (items_for(["Sam Sample"], msg="<m3>"), "booking", "updated", "add_passengers"),
        (
            items_for(["Alex Sample"], dep="2027-06-22T08:30", msg="<m4>"),
            "change",
            "updated",
            "changed",
        ),
        (
            items_for(["Sam Sample"], msg="<m5>", status="cancelled"),
            "cancellation",
            "updated",
            "remove_passengers",
        ),
        (
            items_for(["Alex Sample"], msg="<m6>", status="cancelled"),
            "cancellation",
            "updated",
            "cancel",
        ),
    ):
        (o,), (row,) = execute(items, doc)
        assert (o.result, row["outcome"], row["detail"]) == (result, result, detail), items[0].key
        assert (o.trip_item_id, row["trip_item_id"]) == (filed, filed)
        assert row["payload_json"] and led.items.payload(row) == items[0].payload
        # the booking names the last message that changed it; a copy changes nothing
        changed = result == "updated"
        assert (st.item(filed).source_message_id == row["message_id"]) is changed
    last = st.item(filed)
    assert last.status == "cancelled" and last.passengers == ["Alex Sample"]
    # the executor files and tells the flight log nothing: only a sync pass records that
    assert (last.told, last.airtrail_flight_id, last.sync_claimed_at) == (None, None, None)


def test_a_key_another_booking_holds_fails_that_booking_alone_with_a_plain_sentence(
    tmp_ledger_path,
):
    led = Ledger.open(tmp_ledger_path)
    st = store(led)
    day_later = dict(dep="2027-06-23T08:20", arr="2027-06-23T11:55")
    _, (mine,) = run(led, items_for(["Alex Sample"]))
    _, (other,) = run(led, items_for(["Casey Sample"], pnr="ZZZ999", msg="<m2>", **day_later))
    before = [dataclasses.asdict(ti) for ti in bookings(led)]
    # an update that would move the first booking onto the key the second holds
    (onto,) = items_for(["Alex Sample"], msg="<m3>", email=LATER, **day_later)
    clash = Action(
        "update",
        onto,
        reason="rescheduled",
        trip_item_id=mine.trip_item_id,
        changes={"date_local": ("2027-06-22", "2027-06-23")},
    )
    hotel = Action("file", items_for(["Alex Sample"], kind="lodging", pnr="H1", msg="<m3>")[0])
    mid = led.messages.insert(sha256="clash", source="cli", status="archived")
    failed, filed = Executor(ledger=led, store=st, book=BOOK).execute(
        WritePlan([clash, hotel]), message_row_id=mid
    )
    assert failed.result == "failed" and filed.result == "filed"
    assert failed.error == "another booking is already filed under the key these details make"
    assert failed.summary == f"update failed: {failed.error}"  # the sentence, and nothing else
    assert [dataclasses.asdict(ti) for ti in bookings(led)][:2] == before
    rows = led.items.by_message(mid)
    assert [(r["n"], r["outcome"], r["detail"]) for r in rows] == [
        (0, "failed", failed.error),
        (1, "filed", None),
    ]
    assert [r["trip_item_id"] for r in rows] == [None, filed.trip_item_id]


def test_what_belongs_to_the_booking_survives_a_merge(tmp_ledger_path):
    """An email's payload never overwrites what the booking holds about itself: each stamp in
    `raised` keeps the later of the two times, and the owner's look, edits and answer, the keys
    it also answers to and its recorded change stay as stored."""
    led = Ledger.open(tmp_ledger_path)
    st = store(led)
    _, (first,) = run(led, items_for(["Alex Sample"]))
    t = [(EMAIL + dt.timedelta(hours=n)).isoformat() for n in range(4)]
    own = {
        "raised": {"unverified_sender": t[2], "low_confidence": t[1]},
        "looked_at": t[1],
        "owner_edits": {"cabin": t[1]},
        "owner_answer": "no",
        "alt_keys": ["an-earlier-key"],
        "change_recorded": True,
        "cancelled_names": {"Casey Sample": t[1]},
    }
    row = st.item(first.trip_item_id)
    led.trip_items.update(row.id, payload={**row.payload, **own})
    st.settle(row.id)
    (item,) = items_for(["Alex Sample", "Sam Sample"], msg="<m2>", email=LATER)
    item.payload.update(
        raised={"unverified_sender": t[0], "low_confidence": t[3], "date_far": t[3]},
        looked_at=t[3],
        owner_edits={"cabin": t[3], "seats": t[3]},
        owner_answer="yes",
        alt_keys=["another-key"],
        change_recorded=False,
        cancelled_names={"Casey Sample": t[3], "Jamie Sample": t[3]},
    )
    update = Action("update", item, reason="add_passengers", trip_item_id=row.id)
    mid = led.messages.insert(sha256="merge", source="cli", status="archived")
    (out,) = Executor(ledger=led, store=st, book=BOOK).execute(
        WritePlan([update]), message_row_id=mid
    )
    after = st.item(row.id)
    assert out.result == "updated" and after.passengers == ["Alex Sample", "Sam Sample"]
    assert after.payload["raised"] == {
        "unverified_sender": t[2],
        "low_confidence": t[3],
        "date_far": t[3],
    }
    for key in (
        "looked_at",
        "owner_edits",
        "owner_answer",
        "alt_keys",
        "change_recorded",
        "cancelled_names",
    ):
        assert after.payload[key] == own[key], key
