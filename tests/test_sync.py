# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The syncer (`write.sync`): for each flight that is due it compares what the flight log should
hold (`checks.desired`) with what it was last told and sends only the difference, and only for a
flight the owner is on with no check open. The flights are the corpus's own, validated the way a
fresh parse is; the flight log is an AirTrail held in memory over a ledger seeded with invented
ids for every airport and airline the corpus names, or a stand-in that fails a request on cue.
Every time is a stand-in clock passed as `now`."""

import contextlib
import dataclasses
import datetime as dt
import json
import logging
import re
import threading

import pytest
from helpers.flightlog import FIRST_AIRLINE_ID, FIRST_AIRPORT_ID, Scripted, named, seed, seeded
from test_checks import (
    a_companion_alone,
    book,
    booking,
    bookings,
    day,
    edit,
    names,
    owners,
    stamps,
    strangers_alone,
    the_corpus,
    validated,
)
from test_checks import moved as on_day
from test_settle import an_hour_later, hours, same_day_hour_later

from tripbox import corpus, util
from tripbox.ledger import Ledger
from tripbox.trips import store as store_module
from tripbox.trips.store import TripItem, TripStore
from tripbox.util import parse_iso
from tripbox.validate import checks, rules
from tripbox.write import lifecycle, sync
from tripbox.write.airtrail import (
    AIRLINES_PATH,
    AIRPORTS_PATH,
    MEMORY_USER_ID,
    REQUIRED_SCOPES,
    AirTrailRefs,
    Profile,
    flight_to_input,
)
from tripbox.write.sync import PassReport, Syncer

HOME = tuple(the_corpus().settings["TRIPBOX_HOME_AIRPORTS"].split(","))
HOUR = 3600
TIMES = {"date", "departure", "arrival", "departureScheduled", "arrivalScheduled", "duration"}


def on(led: Ledger | None = None) -> Ledger:
    """A ledger whose flight log is on."""
    led = led or Ledger.memory()
    led.kv.set("airtrail", "on")
    return led


def trip_store(led: Ledger) -> TripStore:
    return TripStore(led, HOME, book=book())


def syncer(st: TripStore, client, *, dry_run=False, alerts: list | None = None) -> Syncer:
    return Syncer(
        store=st,
        ledger=st.led,
        book=book(),
        client=client,
        refs=None if client is None else AirTrailRefs(client, st.led),
        dry_run=dry_run,
        alert=None if alerts is None else alerts.append,
    )


def clock(entry: corpus.Entry) -> dt.datetime:
    """A stand-in clock: a day after the entry's email was sent, in UTC."""
    return corpus.sent(entry).astimezone(dt.UTC) + dt.timedelta(days=1)


def seconds(n: int) -> dt.timedelta:
    return dt.timedelta(seconds=n)


def minutes_later(value: str, n: int) -> str:
    """The printed local time `value` moved by `n` minutes, written as the validator writes one."""
    return (rules.parse_local(value) + dt.timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M")


def spelled(value: str) -> str:
    """The instant `value` written another way, as a flight log may hand it back."""
    return parse_iso(value).isoformat(timespec="milliseconds")


def filed(st: TripStore, entry: corpus.Entry, res: dict) -> TripItem:
    item = validated(entry, res)
    return st.upsert_item(st.find_or_create(item), item)


def owners_flights(n: int) -> list[tuple[corpus.Entry, dict]]:
    """The first `n` of the owner's flights that validate with no finding, each its own booking."""
    found: list[tuple[corpus.Entry, dict]] = []
    taken: set[str] = set()
    for entry, res in bookings("flight"):
        item = validated(entry, res)
        if owners(res) and not checks.codes(item) and item.key not in taken:
            taken.add(item.key)
            found.append((entry, res))
            if len(found) == n:
                return found
    raise AssertionError(f"the corpus files fewer than {n} flights of the owner's")


def wanted(ti: TripItem) -> dict:
    return checks.desired(ti.payload, ti.passengers, ti.status, book())


def ref_id(led: Ledger, kind: str, code: str) -> int:
    return led.refcache.get(kind, code)["id"]


def ids(items) -> list[int]:
    return [i.id for i in items]


def test_build_flight_input_shape():
    """The body a new flight is sent as: its day, its instants and its number, the flight log's
    ids as they are given, its length in seconds, the owner by user id and each companion by
    their usual name with seat and class, and a note that names the booking, its email and
    anyone else aboard."""
    entry, flight = booking(
        "flight",
        lambda r: owners(r)
        and bool(book().classify(names(r)).guests)
        and bool(r.get("confirmation_code")),
    )
    _, strangers = booking("flight", strangers_alone)
    everyone = names(flight)
    owner = next(n for n in everyone if book().match(n).kind == "me")
    guest = next(n for n in everyone if book().match(n).kind == "guest")
    other = names(strangers)[0]
    seats = {owner: "the owner's seat", guest: "the companion's seat"}
    aboard = [owner, guest, other]
    item = validated(
        entry,
        edit(flight, {"passengers": aboard}, seat_by_passenger=seats, cabin="Main Cabin"),
    )
    assert item.classified.others == [other]
    from_id, to_id, airline_id = FIRST_AIRPORT_ID, FIRST_AIRPORT_ID + 1, FIRST_AIRLINE_ID
    inp = sync.build_flight_input(
        item, book(), user_id=MEMORY_USER_ID, from_id=from_id, to_id=to_id, airline_id=airline_id
    )
    p = item.payload
    assert (inp["date"], inp["departure"], inp["arrival"]) == (
        p["date_local"],
        p["dep_utc"],
        p["arr_utc"],
    )
    assert (inp["departureScheduled"], inp["arrivalScheduled"]) == (p["dep_utc"], p["arr_utc"])
    # the flight log keeps a flight's length in seconds
    length = parse_iso(p["arr_utc"]) - parse_iso(p["dep_utc"])
    assert inp["duration"] == int(length.total_seconds()) == p["duration_min"] * 60
    assert (inp["flightNumber"], inp["fromId"], inp["toId"], inp["airlineId"]) == (
        p["flight_code"],
        from_id,
        to_id,
        airline_id,
    )
    assert inp["passengers"] == [
        {"userId": MEMORY_USER_ID, "seatNumber": seats[owner], "seatClass": "economy"},
        {
            "guestName": book().match(guest).canonical,
            "seatNumber": seats[guest],
            "seatClass": "economy",
        },
    ]
    assert inp["note"].startswith(f"tripbox · PNR {p['pnr']}")
    assert other in inp["note"] and f"msg {p['message_id']}" in inp["note"]


class AnswersLookups(Scripted):
    """A flight log that answers a reference lookup for every airport and airline the corpus
    names, each with an id of its own; nothing is cached before it is asked."""

    def __init__(self, ledger: Ledger) -> None:
        super().__init__(ledger)
        airports, airlines = named(the_corpus())
        self.airports = {code: n for n, code in enumerate(airports, start=FIRST_AIRPORT_ID)}
        self.airlines = {code: n for n, code in enumerate(airlines, start=FIRST_AIRLINE_ID)}

    def get_json(self, path, params):
        super().get_json(path, params)  # recorded as a request
        code = params.get("query")
        if path == AIRPORTS_PATH and code in self.airports:
            return {"data": [{"id": self.airports[code], "iata": code, "type": "large_airport"}]}
        if path == AIRLINES_PATH and code in self.airlines:
            return [{"id": self.airlines[code], "iata": code}]
        return []


def test_the_flight_input_carries_airtrail_ids_looked_up_at_write_time():
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)
    assert not {"from_id", "to_id", "airline_id"} & item.payload.keys()  # no ids validated
    led = on()
    st = trip_store(led)
    log = AnswersLookups(led)
    ti = st.upsert_item(st.find_or_create(item), item)
    assert led.conn.execute("SELECT COUNT(*) FROM ref_cache").fetchone()[0] == 0
    assert syncer(st, log).run_pass(now=clock(entry)).done == {"create": 1}
    p = ti.payload
    asked = [r[1:] for r in log.requests if r[0] == "reference"]
    assert asked == [
        (AIRPORTS_PATH, p["origin"]),
        (AIRPORTS_PATH, p["destination"]),
        (AIRLINES_PATH, p["carrier"]),
    ]
    ((_, body),) = log.sent
    assert (body["fromId"], body["toId"], body["airlineId"]) == (
        log.airports[p["origin"]],
        log.airports[p["destination"]],
        log.airlines[p["carrier"]],
    )
    # each answer is kept: a second flight that shares a code with the first asks only for
    # what the first did not
    known = {p["origin"], p["destination"], p["carrier"]}

    def codes_of(e, r) -> set[str]:
        q = validated(e, r).payload
        return {q["origin"], q["destination"], q["carrier"]}

    other_entry, other = next(
        (e, r)
        for e, r in bookings("flight")
        if owners(r)
        and not checks.codes(validated(e, r))
        and validated(e, r).key != item.key
        and known & codes_of(e, r)
        and codes_of(e, r) - known
    )
    assert ref_id(led, "airport", p["origin"]) == log.airports[p["origin"]]
    log.requests.clear()
    filed(st, other_entry, other)
    assert syncer(st, log).run_pass(now=clock(entry) + seconds(60)).done == {"create": 1}
    asked_again = {r[2] for r in log.requests if r[0] == "reference"}
    assert asked_again == codes_of(other_entry, other) - known


def test_a_pending_flight_is_created_once_and_then_is_synced():
    entry, flight = booking(
        "flight", lambda r: owners(r) and bool(book().classify(names(r)).guests)
    )
    led = on()
    st = trip_store(led)
    airtrail = seeded(led, the_corpus())
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert ti.sync_state == "pending" and ids(st.due_for_sync(now=now)) == [ti.id]
    # with no flight log to send it to, the flight waits
    assert syncer(st, None).run_pass(now=now) == PassReport(done={"wait": 1}, stopped=False)
    assert st.item(ti.id).sync_state == "pending" and list(airtrail.iter_flights()) == []
    report = syncer(st, airtrail).run_pass(now=now)
    assert report == PassReport(done={"create": 1}, stopped=False)
    assert set(report.done) <= set(sync.OPERATIONS)
    after = st.item(ti.id)
    (held,) = airtrail.iter_flights()
    assert after.sync_state == "synced" and after.airtrail_flight_id == held["id"]
    assert after.told == wanted(after) and after.sync_claimed_at is None
    p = after.payload
    assert (held["date"], held["departure"], held["arrival"], held["flightNumber"]) == (
        p["date_local"],
        p["dep_utc"],
        p["arr_utc"],
        p["flight_code"],
    )
    assert (held["from"]["id"], held["to"]["id"], held["airline"]["id"]) == (
        ref_id(led, "airport", p["origin"]),
        ref_id(led, "airport", p["destination"]),
        ref_id(led, "airline", p["carrier"]),
    )
    # the flight log keeps a flight's length in seconds
    length = parse_iso(p["arr_utc"]) - parse_iso(p["dep_utc"])
    assert held["duration"] == int(length.total_seconds())
    aboard = held["passengers"]
    assert [x["user"] for x in aboard if x["user"]] == [{"id": MEMORY_USER_ID}]
    guests = sorted(x["guestName"] for x in aboard if not x["user"])
    assert guests == sorted(book().classify(after.passengers).guests)
    # a second pass finds nothing to do
    later = syncer(st, airtrail).run_pass(now=now + seconds(60))
    assert later == PassReport(done={}, stopped=False)
    assert len(list(airtrail.iter_flights())) == 1 and st.item(ti.id).sync_state == "synced"


def test_a_flight_already_in_airtrail_is_adopted_and_nothing_is_sent():
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)
    p = item.payload
    # the number as the booking has it, and typed there with a leading zero: the lookup
    # compares digits as the record check does, leading zeros dropped
    zeroed = p["carrier"] + "0" + p["flight_code"][len(p["carrier"]):]
    for typed in (p["flight_code"], zeroed):
        led = on()
        st = trip_store(led)
        seed(led, the_corpus())
        airtrail = Scripted(led)
        # the owner entered the flight there by hand: its day, number and airports
        fid = airtrail.create_flight(
            {
                "date": p["date_local"],
                "flightNumber": typed,
                "fromId": ref_id(led, "airport", p["origin"]),
                "toId": ref_id(led, "airport", p["destination"]),
                "passengers": [],
            }
        )
        by_hand = airtrail.flights
        airtrail.requests.clear()
        ti = st.upsert_item(st.find_or_create(item), item)
        assert syncer(st, airtrail).run_pass(now=clock(entry)).done == {"adopt": 1}, typed
        after = st.item(ti.id)
        assert (after.airtrail_flight_id, after.sync_state) == (fid, "synced"), typed
        assert after.told == wanted(after), typed
        assert airtrail.sent == [] and airtrail.flights == by_hand, typed


def test_a_record_of_the_same_flight_that_does_not_list_the_keys_user_is_not_adopted():
    """A record of the flight's day, number and origin whose passengers do not include the key's
    user (a companion's alone, logged by hand, or another user's) is not the owner's: the
    flight is created, as the backfill would neither file nor link that record."""
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)
    p = item.payload
    for aboard in (
        [{"userId": None, "guestName": "Sam Sample"}],
        [{"userId": "another-user", "guestName": None}],
    ):
        led = on()
        st = trip_store(led)
        seed(led, the_corpus())
        airtrail = Scripted(led)
        theirs = airtrail.create_flight(
            {
                "date": p["date_local"],
                "flightNumber": p["flight_code"],
                "fromId": ref_id(led, "airport", p["origin"]),
                "toId": ref_id(led, "airport", p["destination"]),
                "passengers": aboard,
            }
        )
        held = airtrail.get_flight(theirs)
        airtrail.requests.clear()
        ti = st.upsert_item(st.find_or_create(item), item)
        assert syncer(st, airtrail).run_pass(now=clock(entry)).done == {"create": 1}, aboard
        after = st.item(ti.id)
        assert after.sync_state == "synced" and after.airtrail_flight_id != theirs, aboard
        assert airtrail.get_flight(theirs) == held, aboard
        assert not sync.not_listed(airtrail.get_flight(after.airtrail_flight_id), MEMORY_USER_ID)


class StopsAfterCreating(Scripted):
    """Creates the first flight it is sent, then the process stops before the store is told."""

    stopped = False

    def create_flight(self, payload):
        fid = super().create_flight(payload)
        if not self.stopped:
            self.stopped = True
            raise RuntimeError("the process stopped")
        return fid


def test_a_crash_after_the_create_adopts_the_flight_instead_of_making_a_second():
    entry, flight = booking("flight", owners)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = StopsAfterCreating(led)
    ti = filed(st, entry, flight)
    now = clock(entry)
    with pytest.raises(RuntimeError):
        syncer(st, airtrail).sync_one(ti.id, now=now)
    left = st.item(ti.id)
    assert (left.sync_state, left.airtrail_flight_id, left.told) == ("pending", None, None)
    assert left.sync_claimed_at is None  # the claim is released with the error
    (made,) = airtrail.flights
    later = now + seconds(sync.CLAIM_SECONDS + 1)
    assert syncer(st, airtrail).run_pass(now=later).done == {"adopt": 1}
    after = st.item(ti.id)
    assert (after.sync_state, after.airtrail_flight_id) == ("synced", made["id"])
    assert [r[0] for r in airtrail.sent] == ["create"] and len(airtrail.flights) == 1


def test_a_claim_left_by_a_dead_process_expires():
    entry, flight = booking("flight", owners)
    led = on()
    st = trip_store(led)
    airtrail = seeded(led, the_corpus())
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert store_module.CLAIM_SECONDS == sync.CLAIM_SECONDS
    assert st.claim_sync(ti.id, now=now)  # a process claims the flight, then dies
    assert not st.claim_sync(ti.id, now=now)
    claim = st.item(ti.id).sync_claimed_at
    held = now + seconds(sync.CLAIM_SECONDS - 1)
    assert st.due_for_sync(now=held) == []
    assert syncer(st, airtrail).sync_one(ti.id, now=held) == "wait"
    assert st.item(ti.id).sync_claimed_at == claim and list(airtrail.iter_flights()) == []
    expired = now + seconds(sync.CLAIM_SECONDS + 1)
    assert ids(st.due_for_sync(now=expired)) == [ti.id]
    assert syncer(st, airtrail).sync_one(ti.id, now=expired) == "create"
    after = st.item(ti.id)
    assert after.sync_state == "synced" and after.sync_claimed_at is None
    assert len(list(airtrail.iter_flights())) == 1


def test_two_passes_at_once_create_one_flight(tmp_path, monkeypatch):
    """Two syncers, each on its own connection to one ledger file and in its own thread, share
    one flight log. Both find the flight due before either claims it (a barrier holds each
    claim until both are there); one of them sends it."""
    entry, flight = booking("flight", owners)
    path = tmp_path / "ledger.db"
    with Ledger.open(path) as led:
        on(led)
        seed(led, the_corpus())
        ti = filed(trip_store(led), entry, flight)
    airtrail = Scripted()  # no ledger: one instance serves both threads
    now = clock(entry)
    together = threading.Barrier(2, timeout=30)
    real = TripStore.claim_sync

    def claim_together(self, item_id, **kw):
        together.wait()
        return real(self, item_id, **kw)

    monkeypatch.setattr(TripStore, "claim_sync", claim_together)
    reports: list[PassReport] = []
    failures: list[BaseException] = []

    def a_pass():
        try:
            with Ledger.open(path) as mine:
                reports.append(syncer(trip_store(mine), airtrail).run_pass(now=now))
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            failures.append(exc)

    workers = [threading.Thread(target=a_pass) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
    assert failures == [] and not any(w.is_alive() for w in workers)
    done = sorted((r.done for r in reports), key=json.dumps)
    assert done == [{"create": 1}, {"wait": 1}]
    assert len(airtrail.flights) == 1 and [r[0] for r in airtrail.sent] == ["create"]
    with Ledger.open(path) as led:
        after = trip_store(led).item(ti.id)
    assert after.sync_state == "synced" and after.airtrail_flight_id == airtrail.flights[0]["id"]


def test_a_pass_that_outlasts_a_claim_claims_each_flight_when_it_reaches_it():
    """A pass given no time reads the clock for each flight it reaches, so a pass whose requests
    take longer than a claim lasts still holds the flight it is on: a second pass started
    meanwhile finds nothing to take."""
    flights = owners_flights(2)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    others: list[PassReport] = []
    with contextlib.ExitStack() as clocks:
        clocks.enter_context(util.clock_at(clock(flights[0][0])))
        rows = [filed(st, e, r) for e, r in flights]

        class Slow(Scripted):
            started = False

            def create_flight(self, payload):
                if not self.flights:  # the first create takes longer than a claim lasts
                    later = util.now() + seconds(sync.CLAIM_SECONDS + 1)
                    clocks.enter_context(util.clock_at(later))
                elif not self.started:  # while the second is sent, another pass starts
                    self.started = True
                    others.append(syncer(trip_store(led), self).run_pass())
                return super().create_flight(payload)

        airtrail = Slow(led)
        report = syncer(st, airtrail).run_pass()
    assert report == PassReport(done={"create": 2}, stopped=False)
    assert others == [PassReport(done={}, stopped=False)]
    assert len(airtrail.flights) == 2 and [r[0] for r in airtrail.sent] == ["create"] * 2
    assert [st.item(x.id).sync_state for x in rows] == ["synced"] * 2


def test_an_error_on_one_flight_backs_it_off_and_the_pass_goes_on(caplog):
    """An error that is not the flight log's answer, on the first of three flights: the other two
    are sent in the same pass, the first waits out a backoff, and the pass raises at its end."""
    flights = owners_flights(3)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    rows = [filed(st, e, r) for e, r in flights]
    broken = rows[0].payload["flight_code"]

    class Breaks(Scripted):
        def find_flight(self, *, flight_code, date_local, origin_iata, user_id=None):
            if flight_code == broken:
                raise RuntimeError("an error of tripbox's own")
            return super().find_flight(
                flight_code=flight_code,
                date_local=date_local,
                origin_iata=origin_iata,
                user_id=user_id,
            )

    airtrail = Breaks(led)
    now = clock(flights[0][0])
    with pytest.raises(RuntimeError):
        syncer(st, airtrail).run_pass(now=now)
    first, *rest = (st.item(x.id) for x in rows)
    assert [x.sync_state for x in rest] == ["synced"] * 2 and len(airtrail.flights) == 2
    assert (first.sync_state, first.sync_error, first.checks) == ("pending", None, ())
    assert (first.sync_attempts, first.sync_claimed_at) == (1, None)
    assert parse_iso(first.sync_after) == now + seconds(sync.BACKOFF_FIRST)
    assert st.due_for_sync(now=now + seconds(sync.BACKOFF_FIRST - 1)) == []
    assert ids(st.due_for_sync(now=now + seconds(sync.BACKOFF_FIRST))) == [first.id]
    logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(logged) == 1 and str(first.id) in logged[0].getMessage()


def test_an_outage_ends_the_pass():
    flights = owners_flights(3)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    rows = [filed(st, e, r) for e, r in flights]
    airtrail.fail_next(503)
    report = syncer(st, airtrail).run_pass(now=clock(flights[0][0]))
    assert report == PassReport(done={"outage": 1}, stopped=False, down=True)
    assert len(airtrail.requests) == 1
    first, *rest = (st.item(x.id) for x in rows)
    assert (first.sync_error, first.sync_attempts) == ("outage", 1)
    assert [(x.sync_error, x.sync_attempts, x.sync_after) for x in rest] == [(None, 0, None)] * 2


def test_a_read_the_flight_log_refuses_is_an_outage_not_a_check_on_the_booking():
    """The flight list and an airport look-up describe the flight log, not this booking: a
    refusal of one backs the flight off as an outage, with no check and no alert."""
    entry, flight = booking("flight", owners)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    alerts: list[str] = []
    syncing = syncer(st, airtrail, alerts=alerts)
    ti = filed(st, entry, flight)
    destination = ti.payload["destination"]
    known = led.refcache.get("airport", destination)
    led.conn.execute("DELETE FROM ref_cache WHERE kind='airport' AND code=?", (destination,))
    at = clock(entry)
    for what in ("find", "reference"):
        airtrail.fail_next(400, on=what)
        assert syncing.run_pass(now=at).done == {"outage": 1}, what
        assert airtrail.requests[-1][0] == what
        row = st.item(ti.id)
        assert (row.sync_state, row.sync_error, row.checks) == ("pending", "outage", ()), what
        at = parse_iso(row.sync_after)
    assert alerts == [] and airtrail.flights == []
    led.refcache.put("airport", destination, known)
    assert syncing.run_pass(now=at).done == {"create": 1}


def test_a_not_found_unlinks_a_flight_only_while_the_flight_log_answers():
    """A flight log that answers not-found to every read (a wrong address) unlinks nothing; one
    that answers otherwise has its profile read once a pass before the first unlink."""
    flights = owners_flights(2)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    rows = [filed(st, e, r) for e, r in flights]
    now = clock(flights[0][0])
    assert syncing.run_pass(now=now).done == {"create": 2}
    linked = [st.item(x.id).airtrail_flight_id for x in rows]
    for x in rows:
        st.set_status(x.id, "cancelled")
    airtrail.fail_next(404, on="get")
    airtrail.fail_next(404, on="me")
    report = syncing.run_pass(now=now + seconds(60))
    assert report == PassReport(done={"outage": 1}, stopped=False, down=True)
    assert [st.item(x.id).airtrail_flight_id for x in rows] == linked
    assert [st.item(x.id).sync_hold for x in rows] == [None, None]
    # deleted there by hand, while the flight log answers: both unlinked, one profile read
    for fid in linked:
        airtrail.deleted_by_hand(fid)
    airtrail.requests.clear()
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"detach": 2}
    assert [r[0] for r in airtrail.requests] == ["get", "me", "get"]
    assert [st.item(x.id).sync_hold for x in rows] == ["deleted"] * 2


def test_a_flight_in_a_deleted_trip_is_not_due_until_the_trip_is_restored():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    moved = st.upsert_item(st.get(ti.trip_id), validated(entry, an_hour_later(flight)))
    assert moved.sync_state == "pending"
    st.delete_trip(ti.trip_id)
    airtrail.requests.clear()
    later = now + seconds(HOUR)
    assert st.due_for_sync(now=later) == []
    assert syncing.run_pass(now=later).done == {}
    assert syncing.sync_one(ti.id, now=later) == "wait"
    assert airtrail.requests == []
    st.restore_trip(ti.trip_id)
    assert ids(st.due_for_sync(now=later)) == [ti.id]
    assert syncing.run_pass(now=later).done == {"update": 1}
    assert st.item(ti.id).sync_state == "synced"


def test_an_update_sends_only_what_changed_and_keeps_what_airtrail_holds_besides():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncer(st, airtrail).run_pass(now=now).done == {"create": 1}
    fid = st.item(ti.id).airtrail_flight_id
    # enrichment, or the owner, fills in there what no email said
    airtrail.by_hand(fid, aircraftReg="registration from enrichment", departureGate="gate by hand")
    before = flight_to_input(airtrail.flights[0])
    later = st.upsert_item(st.get(ti.trip_id), validated(entry, an_hour_later(flight)))
    assert later.id == ti.id and later.sync_state == "pending"
    airtrail.requests.clear()
    assert syncer(st, airtrail).run_pass(now=now + seconds(HOUR)).done == {"update": 1}
    ((_, sent_to, body),) = airtrail.sent
    assert sent_to == fid
    changed = {k for k in body if body[k] != before.get(k)}
    assert {"departure", "note"} <= changed <= TIMES | {"note"}
    (held,) = airtrail.flights
    after = st.item(ti.id)
    p = after.payload
    assert (held["departure"], held["arrival"]) == (p["dep_utc"], p["arr_utc"])
    assert held["departureScheduled"] == p["dep_utc"]
    assert held["aircraftReg"] == "registration from enrichment"
    assert held["departureGate"] == "gate by hand"
    assert held["note"].startswith(before["note"] + "\n")
    last = held["note"].splitlines()[-1]
    assert "departure" in last and "arrival" in last
    assert after.sync_state == "synced" and after.told == wanted(after)


def test_a_changed_destination_is_looked_up_and_sent_and_an_unknown_one_holds_the_flight():
    """A booking the flight log already holds changes its destination: an airport the flight log
    does not know holds the flight back with nothing sent; once it knows it, that airport's id is
    sent and the note names the end; and an update that keeps the destination asks for no
    airport, whether the flight log's answers are at hand or not."""
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    first = validated(entry, flight)
    taken = {first.payload["origin"], first.payload["destination"]}
    elsewhere = next(
        code
        for code in named(the_corpus())[0]
        if code not in taken
        and not checks.codes(validated(entry, edit(flight, destination=code)))
    )
    rerouted = edit(flight, destination=elsewhere)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    log = Scripted(led)
    syncing = syncer(st, log)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    fid = st.item(ti.id).airtrail_flight_id
    was = log.flights[0]["to"]["id"]
    known = led.refcache.get("airport", elsewhere)
    assert known["id"] != was
    # the flight log does not know the new destination: asked once, held, nothing sent
    led.conn.execute("DELETE FROM ref_cache WHERE kind='airport' AND code=?", (elsewhere,))
    moved = st.upsert_item(st.get(ti.trip_id), validated(entry, rerouted))
    assert (moved.payload["destination"], moved.sync_state) == (elsewhere, "pending")
    log.requests.clear()
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"airport": 1}
    held = st.item(ti.id)
    assert (held.checks, held.sync_state) == (("airtrail_airport",), "blocked")
    assert [r[1:] for r in log.requests if r[0] == "reference"] == [(AIRPORTS_PATH, elsewhere)]
    assert log.sent == [] and log.flights[0]["to"]["id"] == was
    # it knows the airport now: its id is sent, and the note names the end that moved
    led.refcache.put("airport", elsewhere, known)
    retry = parse_iso(held.sync_after)
    assert syncing.run_pass(now=retry).done == {"update": 1}
    ((_, sent_to, body),) = log.sent
    assert (sent_to, body["toId"]) == (fid, known["id"])
    (there,) = log.flights
    assert there["to"]["id"] == known["id"] and there["from"]["id"] == body["fromId"]
    assert "destination" in there["note"].splitlines()[-1]
    after = st.item(ti.id)
    assert (after.checks, after.sync_state, after.told) == ((), "synced", wanted(after))
    # an update that keeps the destination asks for no airport, even with none at hand
    led.conn.execute("DELETE FROM ref_cache WHERE kind='airport'")
    later = st.upsert_item(st.get(ti.trip_id), validated(entry, an_hour_later(rerouted)))
    assert later.sync_state == "pending" and later.payload["destination"] == elsewhere
    log.requests.clear()
    assert syncing.run_pass(now=retry + seconds(HOUR)).done == {"update": 1}
    assert [r for r in log.requests if r[0] == "reference"] == []
    ((_, _, body),) = log.sent
    assert body["toId"] == known["id"]
    last = log.flights[0]["note"].splitlines()[-1]
    assert "departure" in last and "destination" not in last
    assert st.item(ti.id).sync_state == "synced"


def test_a_departure_change_leaves_the_arrival_the_flight_log_holds():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    leg = flight["segments"][0]
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    fid = st.item(ti.id).airtrail_flight_id
    # the owner corrects the arrival there by hand, five minutes later than the email said
    corrected = rules.to_utc_iso(parse_iso(ti.payload["arr_utc"]) + seconds(300))
    airtrail.by_hand(fid, arrival=corrected, arrivalScheduled=corrected)
    later = edit(flight, departure_local=minutes_later(leg["departure_local"], 5))
    moved = st.upsert_item(st.get(ti.trip_id), validated(entry, later))
    assert moved.sync_state == "pending" and moved.payload["arr_utc"] == ti.payload["arr_utc"]
    airtrail.requests.clear()
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"update": 1}
    ((_, _, body),) = airtrail.sent
    assert (body["arrival"], body["arrivalScheduled"]) == (corrected, corrected)
    assert body["departure"] == body["departureScheduled"] == moved.payload["dep_utc"]
    (held,) = airtrail.flights
    assert held["arrival"] == corrected and held["departure"] == moved.payload["dep_utc"]
    assert "arrival" not in held["note"].splitlines()[-1]


def test_an_arrival_printed_as_a_day_alone_never_blanks_what_the_flight_log_holds():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    leg = flight["segments"][0]
    timed = validated(entry, flight).payload
    bare = edit(flight, arrival_local=day(leg["arrival_local"]).isoformat())
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    ti = filed(st, entry, bare)
    assert ti.payload["arr_utc"] is None and ti.sync_state == "pending"
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    (made,) = airtrail.flights
    assert (made["arrival"], made["arrivalScheduled"], made["duration"]) == (None, None, None)
    # enrichment fills in there the arrival and the length no email gave
    length = timed["duration_min"] * 60
    filled = {"arrival": timed["arr_utc"], "arrivalScheduled": timed["arr_utc"], "duration": length}
    airtrail.by_hand(made["id"], **filled)
    later = edit(bare, departure_local=hours(leg["departure_local"], 1))
    moved = st.upsert_item(st.get(ti.trip_id), validated(entry, later))
    assert moved.payload["arr_utc"] is None and moved.sync_state == "pending"
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"update": 1}
    (held,) = airtrail.flights
    assert {k: held[k] for k in filled} == filled
    assert held["departure"] == moved.payload["dep_utc"]


def test_a_change_the_flight_log_already_holds_sends_nothing():
    """The owner moved the flight there by hand before the email said so, and the flight log
    spells its times its own way: the same instants and the same day are no change."""
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    fid = st.item(ti.id).airtrail_flight_id
    after = validated(entry, an_hour_later(flight)).payload
    dep, arr = spelled(after["dep_utc"]), spelled(after["arr_utc"])
    airtrail.by_hand(
        fid,
        date=f"{after['date_local']}T00:00:00.000Z",
        departure=dep,
        departureScheduled=dep,
        arrival=arr,
        arrivalScheduled=arr,
    )
    held = airtrail.flights
    moved = st.upsert_item(st.get(ti.trip_id), validated(entry, an_hour_later(flight)))
    assert moved.sync_state == "pending" and moved.told != wanted(moved)
    airtrail.requests.clear()
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"adopt": 1}
    assert airtrail.sent == [] and airtrail.flights == held
    done = st.item(ti.id)
    assert done.sync_state == "synced" and done.told == wanted(done)


def test_a_flight_linked_with_nothing_told_that_the_log_holds_as_wanted_is_adopted():
    """A flight linked to the flight log with nothing recorded as told (as the backfill files
    one), whose record there is what the booking wants, is adopted: nothing is sent and the
    booking is recorded as told. Its class there is the flight log's own word; the booking's
    cabin is that word, or one the flight log has no word for."""
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    word = "economy+"  # one of the flight log's own classes
    cabins = (word, "a cabin the flight log has no word for")
    rows = []
    for (entry, flight), cabin in zip(owners_flights(2), cabins, strict=True):
        item = validated(entry, edit(flight, cabin=cabin))
        p = item.payload
        assert p["cabin"] == cabin
        body = sync.build_flight_input(
            item,
            book(),
            user_id=MEMORY_USER_ID,
            from_id=ref_id(led, "airport", p["origin"]),
            to_id=ref_id(led, "airport", p["destination"]),
            airline_id=ref_id(led, "airline", p["carrier"]),
        )
        body["passengers"] = [{**x, "seatClass": word} for x in body["passengers"]]
        fid = airtrail.create_flight(body)
        rows.append(st.upsert_item(st.find_or_create(item), item, airtrail_flight_id=fid))
    assert [(x.told, x.sync_state) for x in rows] == [(None, "pending")] * 2
    held = airtrail.flights
    airtrail.requests.clear()
    assert syncing.run_pass(now=clock(owners_flights(1)[0][0])).done == {"adopt": 2}
    assert airtrail.sent == [] and airtrail.flights == held
    for x in rows:
        after = st.item(x.id)
        assert (after.sync_state, after.airtrail_flight_id) == ("synced", x.airtrail_flight_id)
        assert after.told == wanted(after)


class StopsAfterUpdating(Scripted):
    """Takes the first update it is sent, then the process stops before the store is told."""

    stopped = False

    def update_flight(self, flight_id, payload):
        out = super().update_flight(flight_id, payload)
        if not self.stopped:
            self.stopped = True
            raise RuntimeError("the process stopped")
        return out


def test_a_cancellation_sent_before_a_crash_is_noted_once():
    entry, flight = booking("flight", owners)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = StopsAfterUpdating(led)
    syncing = syncer(st, airtrail)
    ti = filed(st, entry, flight)
    noon = clock(entry).replace(hour=12, minute=0, second=0)  # every pass on one day
    assert syncing.run_pass(now=noon).done == {"create": 1}
    st.cancel(ti.item_key, email_date=stamps(entry)[0])
    with pytest.raises(RuntimeError):
        syncing.run_pass(now=noon + seconds(60))
    (sent_once,) = airtrail.flights
    assert sent_once["note"].splitlines()[0] == f"CANCELLED {noon.date().isoformat()}"
    assert st.item(ti.id).sync_state == "pending"  # the store was never told
    assert syncing.run_pass(now=noon + seconds(HOUR)).done == {"adopt": 1}
    assert airtrail.flights == [sent_once] and st.item(ti.id).sync_state == "synced"


def test_a_companion_added_later_is_added_and_one_who_left_is_removed_but_never_the_owner():
    entry, flight = booking(
        "flight", lambda r: owners(r) and len(book().classify(names(r)).guests) > 1
    )
    everyone = names(flight)
    owner = next(n for n in everyone if book().match(n).kind == "me")
    first, *others = [n for n in everyone if book().match(n).kind == "guest"]

    def usual(name: str) -> str:
        return book().match(name).canonical

    led = on()
    st = trip_store(led)
    airtrail = seeded(led, the_corpus())
    syncing = syncer(st, airtrail)
    ti = filed(st, entry, edit(flight, {"passengers": [owner, first]}))
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}

    def aboard() -> list[str]:
        (held,) = airtrail.iter_flights()
        return sorted("me" if x["user"] else x["guestName"] for x in held["passengers"])

    assert aboard() == sorted(["me", usual(first)])
    owner_row = [x for x in next(airtrail.iter_flights())["passengers"] if x["user"]]
    # the rest of the family's tickets arrive: they are added, and nobody else changes
    st.upsert_item(st.get(ti.trip_id), validated(entry, flight))
    assert syncing.run_pass(now=now + seconds(60)).done == {"update": 1}
    assert aboard() == sorted(["me", usual(first), *map(usual, others)])
    # the first companion's ticket is cancelled: their row goes, and only theirs
    st.remove_passengers(ti.item_key, [first])
    assert syncing.run_pass(now=now + seconds(120)).done == {"update": 1}
    assert aboard() == sorted(["me", *map(usual, others)])
    assert [x for x in next(airtrail.iter_flights())["passengers"] if x["user"]] == owner_row
    # the owner leaves the booking: it is no longer the owner's flight, nothing is sent, and
    # the owner's row there stays
    gone = st.remove_passengers(ti.item_key, [owner])
    assert gone.sync_state == "off" and st.due_for_sync(now=now + seconds(180)) == []
    assert syncing.run_pass(now=now + seconds(180)).done == {}
    assert aboard() == sorted(["me", *map(usual, others)])


def test_a_cancelled_flight_gets_a_note_and_is_never_deleted():
    (a_entry, a), (b_entry, b) = owners_flights(2)
    led = on()
    st = trip_store(led)
    airtrail = seeded(led, the_corpus())
    syncing = syncer(st, airtrail)
    by_email, on_the_page = filed(st, a_entry, a), filed(st, b_entry, b)
    now = clock(a_entry)
    assert syncing.run_pass(now=now).done == {"create": 2}
    sent = {f["id"]: f for f in airtrail.iter_flights()}
    st.cancel(by_email.item_key, email_date=stamps(a_entry)[0])  # a cancellation email
    st.set_status(on_the_page.id, "cancelled")  # Mark cancelled, on the page
    assert [st.item(x).sync_state for x in ids((by_email, on_the_page))] == ["pending"] * 2
    later = now + seconds(HOUR)
    assert syncing.run_pass(now=later).done == {"update": 2}
    held = {f["id"]: f for f in airtrail.iter_flights()}
    assert held.keys() == sent.keys()
    cancelled = f"CANCELLED {later.date().isoformat()}"
    for fid, f in held.items():
        first_line, *rest = f["note"].splitlines()
        assert first_line == cancelled and "\n".join(rest).startswith(sent[fid]["note"])
        assert f["passengers"] == sent[fid]["passengers"]
        assert f["departure"] == sent[fid]["departure"]
    assert [st.item(x).sync_state for x in ids((by_email, on_the_page))] == ["synced"] * 2
    # marked confirmed again, it stands, and the flight says so first
    assert st.set_status(on_the_page.id, "confirmed").sync_state == "pending"
    again = later + seconds(HOUR)
    assert syncing.run_pass(now=again).done == {"update": 1}
    flight_id = st.item(on_the_page.id).airtrail_flight_id
    note = next(f["note"] for f in airtrail.iter_flights() if f["id"] == flight_id)
    assert note.splitlines()[:2] == [f"reinstated {again.date().isoformat()}", cancelled]
    assert len(list(airtrail.iter_flights())) == 2


def test_a_cancelled_flight_never_sent_stays_off():
    entry, flight = booking("flight", owners)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    ti = filed(st, entry, flight)
    assert st.cancel(ti.item_key, email_date=stamps(entry)[0]).sync_state == "off"
    syncing = syncer(st, airtrail)
    for n in range(3):
        at = clock(entry) + seconds(n * HOUR)
        assert st.due_for_sync(now=at) == []
        assert syncing.run_pass(now=at).done == {}
        assert syncing.sync_one(ti.id, now=at) == "wait"
    assert airtrail.requests == [] and airtrail.flights == []
    assert st.item(ti.id).sync_state == "off"


def test_a_flight_deleted_in_airtrail_is_unlinked_and_never_recreated_unasked():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    airtrail.deleted_by_hand(st.item(ti.id).airtrail_flight_id)
    st.upsert_item(st.get(ti.trip_id), validated(entry, an_hour_later(flight)))
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"detach": 1}
    after = st.item(ti.id)
    assert (after.sync_state, after.sync_hold) == ("off", "deleted")
    assert (after.airtrail_flight_id, after.told) == (None, None)
    airtrail.requests.clear()
    for n in range(2, 5):
        assert syncing.run_pass(now=now + seconds(n * HOUR)).done == {}
    assert airtrail.requests == [] and airtrail.flights == []


def test_an_outage_backs_off_from_thirty_seconds_to_six_hours_and_alerts_once():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    alerts: list[str] = []
    syncing = syncer(st, airtrail, alerts=alerts)
    ti = filed(st, entry, flight)
    start = at = clock(entry)
    waits = []
    for n in range(12):
        if n:
            assert syncing.run_pass(now=at - seconds(1)).done == {}  # not yet
        airtrail.fail_next(503)
        assert syncing.run_pass(now=at).done == {"outage": 1}
        row = st.item(ti.id)
        assert (row.sync_state, row.sync_error, row.sync_attempts) == ("pending", "outage", n + 1)
        assert len(alerts) == (1 if at - start >= seconds(sync.ALERT_AFTER) else 0), n
        retry = parse_iso(row.sync_after)
        waits.append(int((retry - at).total_seconds()))
        at = retry
    assert waits == [30 * 2**n for n in range(10)] + [6 * HOUR] * 2
    assert waits == [sync.backoff(n) for n in range(1, 13)]
    assert led.kv.get("airtrail_down_since") and led.kv.get("airtrail_alerted")
    (alert,) = alerts
    assert "flight log" in alert and "AirTrail" not in alert
    # it answers again: the flight is sent, and the outage is forgotten
    assert syncing.run_pass(now=at).done == {"create": 1}
    assert not led.kv.get("airtrail_down_since") and not led.kv.get("airtrail_alerted")
    assert st.item(ti.id).sync_attempts == 0
    # a new outage alerts again, once it has lasted an hour
    st.upsert_item(st.get(ti.trip_id), validated(entry, an_hour_later(flight)))
    start = at
    while len(alerts) == 1:
        assert at - start < seconds(2 * sync.ALERT_AFTER)
        airtrail.fail_next(503)
        assert syncing.run_pass(now=at).done == {"outage": 1}
        at = parse_iso(st.item(ti.id).sync_after)
    assert len(alerts) == 2 and st.item(ti.id).sync_state == "pending"


def test_a_rejected_key_stops_the_pass_and_alerts_once():
    flights = owners_flights(3)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    alerts: list[str] = []
    syncing = syncer(st, airtrail, alerts=alerts)
    rows = [filed(st, e, r) for e, r in flights]
    now = clock(flights[0][0])
    assert ids(st.due_for_sync(now=now)) == ids(rows)
    for n in range(2):
        airtrail.fail_next(401)
        report = syncing.run_pass(now=now + seconds(n * 60))
        assert report == PassReport(done={"stop": 1}, stopped=True)
        assert len(airtrail.requests) == n + 1  # the one request refused, and nothing after it
        for x in rows:
            row = st.item(x.id)
            assert (row.sync_state, row.sync_attempts, row.sync_error) == ("pending", 0, None)
            assert (row.sync_after, row.sync_claimed_at) == (None, None)
    assert len(alerts) == 1 and led.kv.get("airtrail_key_rejected")
    assert "flight log" in alerts[0] and "AirTrail" not in alerts[0]
    # a key that works sends all three, and the rejection is forgotten
    assert syncing.run_pass(now=now + seconds(180)).done == {"create": 3}
    assert not led.kv.get("airtrail_key_rejected") and len(alerts) == 1


def test_a_refused_write_becomes_a_soft_check_that_looking_reopens_for_another_try():
    entry, flight = booking("flight", owners)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    alerts: list[str] = []
    syncing = syncer(st, airtrail, alerts=alerts)
    ti = filed(st, entry, flight)
    now = clock(entry)
    # an outage and a rejected key that ended unseen: a refusal is an answer that proves the key
    # works, so a later outage is timed afresh and a later rejection alerts again
    for key in ("airtrail_down_since", "airtrail_key_rejected"):
        led.kv.set(key, store_module.stamp(now - seconds(2 * HOUR)))

    def looked(at: dt.datetime) -> TripItem:
        payload = st.item(ti.id).payload
        checks.looked(payload, store_module.stamp(at))
        st.led.trip_items.update(ti.id, payload=payload)
        return st.settle(ti.id)

    for n in range(2):
        at = now + seconds(n * HOUR)
        airtrail.fail_next(422, on="create")
        assert syncing.run_pass(now=at).done == {"refused": 1}
        assert not led.kv.get("airtrail_down_since") and not led.kv.get("airtrail_key_rejected")
        refused = st.item(ti.id)
        assert refused.checks == ("airtrail_refused",) and refused.sync_state == "blocked"
        assert (refused.sync_error, refused.sync_after) == ("refused", None)
        assert st.due_for_sync(now=at + seconds(sync.BACKOFF_MAX)) == []
        assert airtrail.flights == []
        assert looked(at + seconds(60)).sync_state == "pending"  # the owner looks: tried again
    # one alert for the booking, naming the flight as a notification line does, and no person
    (alert,) = alerts
    assert ti.payload["flight_code"] in alert and "AirTrail" not in alert
    assert not any(name in alert for name in ti.passengers)
    assert ti.confirmation is None or ti.confirmation not in alert
    assert syncing.run_pass(now=now + seconds(3 * HOUR)).done == {"create": 1}
    after = st.item(ti.id)
    assert (after.sync_state, after.checks, after.sync_error) == ("synced", (), None)
    assert len(airtrail.flights) == 1


def test_an_airport_airtrail_does_not_know_is_a_hard_check_until_it_knows_it():
    entry, flight = booking("flight", owners)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail)
    item = validated(entry, flight)
    destination = item.payload["destination"]
    known = led.refcache.get("airport", destination)
    led.conn.execute("DELETE FROM ref_cache WHERE kind='airport' AND code=?", (destination,))
    ti = st.upsert_item(st.find_or_create(item), item)
    now = clock(entry)
    led.kv.set("airtrail_key_rejected", store_module.stamp(now - seconds(HOUR)))
    assert syncing.run_pass(now=now).done == {"airport": 1}
    assert not led.kv.get("airtrail_key_rejected")  # the flight log answered: the key works
    unknown = st.item(ti.id)
    assert unknown.checks == ("airtrail_airport",) and unknown.sync_state == "blocked"
    assert airtrail.sent == [] and airtrail.flights == []
    retry = parse_iso(unknown.sync_after)
    assert retry - now == seconds(sync.BACKOFF_FIRST)
    assert st.due_for_sync(now=retry - seconds(1)) == []
    # looking does not close it: only the flight log knowing the airport does
    payload = unknown.payload
    checks.looked(payload, store_module.stamp(retry - seconds(1)))
    st.led.trip_items.update(ti.id, payload=payload)
    assert st.settle(ti.id).checks == ("airtrail_airport",)
    assert syncing.run_pass(now=retry).done == {"airport": 1}  # still unknown: still marked
    retry = parse_iso(st.item(ti.id).sync_after)
    led.refcache.put("airport", destination, known)
    assert ids(st.due_for_sync(now=retry)) == [ti.id]
    assert syncing.run_pass(now=retry).done == {"create": 1}
    after = st.item(ti.id)
    assert (after.checks, after.sync_state, after.sync_error) == ((), "synced", None)
    (held,) = airtrail.flights
    assert held["to"]["id"] == known["id"]


def test_a_dry_run_logs_the_request_once_and_leaves_the_flight_pending(caplog):
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    syncing = syncer(st, airtrail, dry_run=True)
    ti = filed(st, entry, flight)
    now = clock(entry)
    caplog.set_level(logging.INFO, logger="tripbox.sync")

    def logged() -> list[str]:
        lines = [r.getMessage() for r in caplog.records]
        return [line for line in lines if line.startswith("dry run: would")]

    assert syncing.run_pass(now=now).done == {"dry_run": 1}
    # logged, the flight is not due again: the flight log is not asked about it pass after pass
    asked = len(airtrail.requests)
    for n in range(1, 3):
        assert syncing.run_pass(now=now + seconds(n * 60)) == PassReport(done={}, stopped=False)
    assert len(airtrail.requests) == asked
    (line,) = logged()
    assert ti.payload["dep_utc"] in line
    assert st.item(ti.id).sync_state == "pending"
    assert airtrail.sent == [] and airtrail.flights == []
    moved = st.upsert_item(st.get(ti.trip_id), validated(entry, an_hour_later(flight)))
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"dry_run": 1}
    assert len(logged()) == 2 and moved.payload["dep_utc"] in logged()[1]
    assert st.item(ti.id).sync_state == "pending" and airtrail.sent == []


def days_later(res: dict, n: int) -> dict:
    """The flight `res` with both its times `n` days later, as an email printing it then reads."""
    leg = res["segments"][0]
    ends = ("departure_local", "arrival_local")
    return edit(res, **{end: on_day(leg[end], n) for end in ends if leg.get(end)})


def test_a_dry_run_asks_about_each_waiting_flight_once_until_what_it_would_send_changes():
    """With the dry run on, every pass would ask the flight log about the same waiting flights,
    the oldest fifty, and never reach the rest. A flight whose request is logged is not due again
    until what it would be sent for changes: two passes log all sixty, a third asks nothing, an
    owner's edit makes that one flight due again, and with the dry run off all of them are
    sent."""
    ((entry, flight),) = owners_flights(1)
    copies = [validated(entry, days_later(flight, n)) for n in range(60)]
    assert all(not checks.codes(item) for item in copies)
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    rows = [st.upsert_item(st.find_or_create(item), item) for item in copies]
    assert len({x.item_key for x in rows}) == 60
    assert {x.sync_state for x in rows} == {"pending"}
    dry = syncer(st, airtrail, dry_run=True)
    now = clock(entry)
    assert dry.run_pass(now=now).done == {"dry_run": 50}
    assert dry.run_pass(now=now + seconds(60)).done == {"dry_run": 10}
    assert all(st.item(x.id).sync_logged for x in rows)
    airtrail.requests.clear()
    assert dry.run_pass(now=now + seconds(120)) == PassReport(done={}, stopped=False)
    assert airtrail.requests == []
    # the owner moves one flight's arrival: that one is due again, and nothing else
    edited = rows[7]
    st.edit_item(edited.id, {"arr_local": minutes_later(edited.payload["arr_local"], 5)})
    assert dry.run_pass(now=now + seconds(180)).done == {"dry_run": 1}
    p = edited.payload
    asked = [r[1:] for r in airtrail.requests if r[0] == "find"]
    assert asked == [(p["flight_code"], p["date_local"], p["origin"])]
    assert dry.run_pass(now=now + seconds(240)).done == {}
    assert airtrail.sent == [] and {st.item(x.id).sync_state for x in rows} == {"pending"}
    # the dry run off: all of them are due, and they are sent
    live = syncer(st, airtrail)
    assert live.run_pass(now=now + seconds(300)).done == {"create": 50}
    assert live.run_pass(now=now + seconds(360)).done == {"create": 10}
    assert len(airtrail.flights) == 60
    assert {st.item(x.id).sync_state for x in rows} == {"synced"}


def test_a_pass_sends_nothing_for_a_flight_the_owner_is_not_on():
    c_entry, theirs = booking("flight", a_companion_alone)
    s_entry, strangers = booking("flight", strangers_alone)
    taken = {validated(c_entry, theirs).key, validated(s_entry, strangers).key}
    n_entry, mine = next(
        (e, r)
        for e, r in bookings("flight")
        if owners(r) and not checks.codes(validated(e, r)) and validated(e, r).key not in taken
    )
    a_stay, stay = booking("lodging")
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    airtrail = Scripted(led)
    nameless = edit(mine, {"passengers": []}, passengers=[])
    rows = [
        filed(st, e, r)
        for e, r in ((c_entry, theirs), (s_entry, strangers), (n_entry, nameless), (a_stay, stay))
    ]
    assert [x.sync_state for x in rows] == ["off", "off", "blocked", "off"]
    assert "owner_unknown" in rows[2].checks
    now = clock(c_entry)
    assert st.due_for_sync(now=now) == []
    syncing = syncer(st, airtrail)
    assert syncing.run_pass(now=now).done == {}
    assert [syncing.sync_one(x.id, now=now) for x in rows] == ["wait"] * 4
    assert airtrail.requests == [] and airtrail.flights == []


OTHER_LOG = (
    "The flight log tripbox reaches now is not the one its flights were sent to (another"
    " AirTrail, or another user's key). Nothing is sent. If this is the flight log to keep, run:"
    " tripbox airtrail forget --apply"
)
STILL_LISTS = (
    "Your flight log still lists this flight as yours. Remove it there if you did not fly."
)
ROLE_SAYS = "The current role does not permit this action"  # AirTrail's sentence


def lacks(scope: str) -> dict:
    """AirTrail's refusal for a scope the key does not hold, as `Scripted.fail_next` takes it."""
    return {"code": "insufficient_scope", "message": f"The credential requires {scope}"}


def a_key(user: str, **changed) -> Profile:
    """The profile of a key with exactly the scopes tripbox needs, its user the instance's own."""
    whole = Profile(user, "apiKey", frozenset(REQUIRED_SCOPES), frozenset(), True)
    return dataclasses.replace(whole, **changed)


def with_a_companion(res: dict) -> bool:
    return owners(res) and bool(book().classify(names(res)).guests)


def untouched(row: TripItem) -> bool:
    """No attempt left a trace on the flight's row."""
    return (row.sync_attempts, row.sync_error, row.sync_after, row.sync_claimed_at) == (
        0,
        None,
        None,
        None,
    )


def a_flight_log(alerts: list | None = None, **kw):
    """A ledger whose flight log is on, its store, a scripted flight log and their syncer."""
    led = on()
    st = trip_store(led)
    seed(led, the_corpus())
    log = Scripted(led, **kw)
    return led, st, log, syncer(st, log, alerts=alerts)


def test_a_key_one_scope_short_stops_the_pass_and_the_alert_names_the_scope():
    """AirTrail asks for the passengers scope only when a flight names a second passenger, so a
    key without it works until the first family booking: the pass stops there, the flight's row
    is as it was, and the owner is told the scope once, until a write goes through."""
    entry, flight = booking("flight", with_a_companion)
    alerts: list[str] = []
    led, st, airtrail, syncing = a_flight_log(alerts)
    ti = filed(st, entry, flight)
    now = clock(entry)
    scope = REQUIRED_SCOPES[-1]
    assert scope.endswith(".manage.own")
    for n in range(2):
        airtrail.fail_next(403, on="create", **lacks(scope))
        report = syncing.run_pass(now=now + seconds(n * 60))
        assert report == PassReport(done={"stop": 1}, stopped=True)
        row = st.item(ti.id)
        assert (row.sync_state, row.checks) == ("pending", ()) and untouched(row)
    assert alerts == [
        f"The key tripbox uses for your flight log lacks the scope {scope}; nothing is sent"
        " until a key with all six is set (tripbox check lists what a key lacks)."
    ]
    assert led.kv.get("airtrail_key_problem") == f"scope:{scope}"
    assert not led.kv.get("airtrail_key_rejected") and airtrail.flights == []
    # a key with the scope: the flight is sent, and the problem is over
    assert syncing.run_pass(now=now + seconds(120)).done == {"create": 1}
    assert not led.kv.get("airtrail_key_problem") and len(alerts) == 1
    # a later refusal is told again
    st.set_status(ti.id, "cancelled")
    airtrail.fail_next(403, on="update", **lacks(scope))
    assert syncing.run_pass(now=now + seconds(HOUR)) == PassReport(done={"stop": 1}, stopped=True)
    assert alerts == [alerts[0]] * 2 and st.item(ti.id).sync_state == "pending"


def test_a_scope_problem_is_not_told_again_for_an_answer_that_only_proves_the_key_is_accepted():
    """An older flight whose airport the flight log does not know is asked about pass after
    pass; each answer proves the key is accepted, not that it gained the scope the next flight
    needs, so the owner is told of the scope once."""
    a_entry, a = booking("flight", owners)
    first = validated(a_entry, a)
    unknown = first.payload["destination"]

    def elsewhere(e, r) -> bool:
        q = validated(e, r)
        apart = unknown not in (q.payload["origin"], q.payload["destination"])
        return owners(r) and not checks.codes(q) and q.key != first.key and apart

    b_entry, b = next((e, r) for e, r in bookings("flight") if elsewhere(e, r))
    alerts: list[str] = []
    led, st, airtrail, syncing = a_flight_log(alerts)
    led.conn.execute("DELETE FROM ref_cache WHERE kind='airport' AND code=?", (unknown,))
    older, newer = filed(st, a_entry, a), filed(st, b_entry, b)
    at = clock(a_entry)
    for _ in range(3):
        airtrail.fail_next(403, on="create", **lacks(REQUIRED_SCOPES[3]))
        report = syncing.run_pass(now=at)
        assert report == PassReport(done={"airport": 1, "stop": 1}, stopped=True)
        at = parse_iso(st.item(older.id).sync_after)
    assert len(alerts) == 1 and led.kv.get("airtrail_key_problem").startswith("scope:")
    assert st.item(newer.id).sync_state == "pending" and airtrail.flights == []


def test_a_role_that_forbids_stops_the_pass_with_its_own_sentence():
    entry, flight = booking("flight", owners)
    alerts: list[str] = []
    led, st, airtrail, syncing = a_flight_log(alerts)
    ti = filed(st, entry, flight)
    now = clock(entry)
    for n in range(2):
        airtrail.fail_next(403, on="create", code="forbidden", message=ROLE_SAYS)
        report = syncing.run_pass(now=now + seconds(n * 60))
        assert report == PassReport(done={"stop": 1}, stopped=True)
        row = st.item(ti.id)
        assert (row.sync_state, row.checks) == ("pending", ()) and untouched(row)
    assert alerts == [
        f"Your role in your flight log does not permit what tripbox needs ({ROLE_SAYS});"
        " nothing is sent until it does."
    ]
    assert led.kv.get("airtrail_key_problem") == "role"
    assert not led.kv.get("airtrail_key_rejected")
    # a refusal that says nothing more is the key's, told in a rejected key's words
    airtrail.fail_next(403)
    assert syncing.run_pass(now=now + seconds(120)).stopped
    assert len(alerts) == 2 and "rejected the key" in alerts[1] and "HTTP 403" in alerts[1]
    assert led.kv.get("airtrail_key_problem") == "unauthorized"
    assert led.kv.get("airtrail_key_rejected")
    # and so is one whose scope cannot be read: the same word, so it is not told again
    airtrail.fail_next(403, code="insufficient_scope", message="The credential requires $(x)")
    assert syncing.run_pass(now=now + seconds(180)).stopped and len(alerts) == 2
    assert syncing.run_pass(now=now + seconds(240)).done == {"create": 1}
    assert not led.kv.get("airtrail_key_problem") and not led.kv.get("airtrail_key_rejected")


class DeletedMeanwhile(Scripted):
    """The owner deletes a flight there after tripbox read it and before tripbox writes it."""

    deleted = False

    def update_flight(self, flight_id, payload):
        if not self.deleted:
            self.deleted = True
            self.deleted_by_hand(flight_id)
        return super().update_flight(flight_id, payload)


def test_a_flight_that_is_no_longer_the_owners_there_is_unlinked_and_the_pass_goes_on(caplog):
    """The owner took themself off a flight in the flight log, or deleted it: AirTrail then asks
    an own-flights key for an any-flights scope, or says the flight is not found. That one flight
    is unlinked, the next is sent in the same pass, nobody is alerted and the key is not said to
    be refused; nothing is written to the flight."""
    flights = owners_flights(2)
    caplog.set_level(logging.INFO, logger="tripbox.sync")
    for how in ("read", "write", "gone"):
        alerts: list[str] = []
        led = on()
        st = trip_store(led)
        seed(led, the_corpus())
        airtrail = (DeletedMeanwhile if how == "gone" else Scripted)(led)
        syncing = syncer(st, airtrail, alerts=alerts)
        rows = [filed(st, e, r) for e, r in flights]
        now = clock(flights[0][0])
        assert syncing.run_pass(now=now).done == {"create": 2}
        linked = [st.item(x.id).airtrail_flight_id for x in rows]
        for x in rows:
            st.set_status(x.id, "cancelled")  # both are due again
        held = {f["id"]: f for f in airtrail.flights}
        airtrail.requests.clear()
        caplog.clear()
        if how == "read":
            airtrail.fail_next(403, on="get", **lacks("flight.read.any"))
        elif how == "write":
            airtrail.fail_next(403, on="update", **lacks("flight.update.any"))
        report = syncing.run_pass(now=now + seconds(HOUR))
        assert report == PassReport(done={"detach": 1, "update": 1}, stopped=False), how
        first, second = (st.item(x.id) for x in rows)
        assert (first.airtrail_flight_id, first.told) == (None, None), how
        assert (first.sync_hold, first.sync_state, first.checks) == ("deleted", "off", ()), how
        assert (second.sync_state, second.airtrail_flight_id) == ("synced", linked[1]), how
        assert alerts == [] and not led.kv.get("airtrail_key_rejected"), how
        assert not led.kv.get("airtrail_key_problem"), how
        # the one update that went through is the second flight's; the first is as it was there
        after = {f["id"]: f for f in airtrail.flights}
        assert after[linked[1]] != held[linked[1]], how
        assert after.get(linked[0]) == (None if how == "gone" else held[linked[0]]), how
        said = [
            r.getMessage()
            for r in caplog.records
            if r.name == "tripbox.sync" and f"trip item {first.id}:" in r.getMessage()
        ]
        assert len(said) == 1 and "detach" in said[0], how


def test_an_update_answered_not_found_for_a_flight_that_is_there_is_a_refusal_not_an_unlink():
    """AirTrail also answers not-found to a write its role refuses on a flight that is there (a
    role that may not change who is on a flight): the flight is read again before it is
    unlinked, and one that is there stays linked, marked as refused."""
    entry, flight = booking("flight", owners)
    alerts: list[str] = []
    led, st, airtrail, syncing = a_flight_log(alerts)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    fid = st.item(ti.id).airtrail_flight_id
    st.set_status(ti.id, "cancelled")
    held = airtrail.flights
    airtrail.requests.clear()
    airtrail.fail_next(404, on="update", code="not_found", message="Flight not found")
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"refused": 1}
    assert [r[0] for r in airtrail.requests] == ["get", "update", "get"]
    row = st.item(ti.id)
    assert (row.airtrail_flight_id, row.sync_hold, row.sync_error) == (fid, None, "refused")
    assert row.checks == ("airtrail_refused",) and airtrail.flights == held
    assert len(alerts) == 1 and ti.payload["flight_code"] in alerts[0]


def test_a_not_found_is_not_believed_while_the_role_may_not_read_the_owners_flights():
    """AirTrail says not found of every flight a role may not read, the owner's own too. A
    flight is unlinked for a not-found only while the profile, read afresh, says the role may
    read the owner's flights; otherwise the pass stops for the role and every flight stays
    linked."""
    flights = owners_flights(2)
    alerts: list[str] = []
    key = a_key("the first user")
    led, st, airtrail, syncing = a_flight_log(alerts, profile=key)
    rows = [filed(st, e, r) for e, r in flights]
    now = clock(flights[0][0])
    assert syncing.run_pass(now=now).done == {"create": 2}
    linked = [st.item(x.id).airtrail_flight_id for x in rows]
    for x in rows:
        st.set_status(x.id, "cancelled")
    # the user's role is changed to one that may not read flights; the key is as it was
    lesser = a_key(key.user_id, is_owner=False, permissions=frozenset({REQUIRED_SCOPES[3]}))
    airtrail.answers_as(lesser)
    for n in range(1, 3):
        airtrail.fail_next(404, on="get", code="not_found", message="Flight not found")
        report = syncing.run_pass(now=now + seconds(n * HOUR))
        assert report == PassReport(done={"stop": 1}, stopped=True)
        assert [st.item(x.id).airtrail_flight_id for x in rows] == linked
        assert [st.item(x.id).sync_hold for x in rows] == [None, None]
    assert led.kv.get("airtrail_key_problem") == "role"
    assert len(alerts) == 1 and "role" in alerts[0] and airtrail.sent[2:] == []
    # the role may read them again: the flights are found, and updated
    airtrail.answers_as(key)
    assert syncing.run_pass(now=now + seconds(3 * HOUR)).done == {"update": 2}
    assert not led.kv.get("airtrail_key_problem")
    # a profile that names nothing the role grants says nothing of it: a flight deleted there
    # is unlinked as ever
    airtrail.answers_as(a_key(key.user_id, is_owner=False))
    airtrail.deleted_by_hand(linked[0])
    st.set_status(rows[0].id, "confirmed")
    assert syncing.run_pass(now=now + seconds(4 * HOUR)).done == {"detach": 1}
    assert st.item(rows[0].id).sync_hold == "deleted" and len(alerts) == 1


def test_a_record_that_is_another_flight_is_never_written_to():
    """A flight log put back from an older copy hands out again the ids its newest flights
    held, so the id a booking keeps may name another flight, the owner's own included. A record
    is written only while at least two of its day, the digits of its number and its origin's
    code agree with what the flight log was last told of it (with nothing told: with what the
    booking itself says); a part either side does not say agrees with nothing. Otherwise it is
    another flight: the booking is unlinked and nothing is sent."""
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)
    p = item.payload
    elsewhere = next(
        code for code in named(the_corpus())[0] if code not in (p["origin"], p["destination"])
    )
    other_day = (dt.date.fromisoformat(p["date_local"]) + dt.timedelta(days=3)).isoformat()
    digits = re.sub(r"\D", "", p["flight_code"])
    other_number = p["flight_code"].replace(digits, str(int(digits) % 9000 + 1))
    assert re.sub(r"\D", "", other_number) != digits
    now = clock(entry)

    def another(led: Ledger, *parts: str) -> dict:
        """The fields of a record that is another flight in `parts`."""
        fields = {
            "day": {"date": other_day},
            "number": {"flightNumber": other_number},
            "origin": {"from": led.refcache.get("airport", elsewhere)},
            "destination": {"to": led.refcache.get("airport", elsewhere)},
        }
        return {k: v for part in parts for k, v in fields[part].items()}

    def sent_then_changed_there(*parts: str, **more):
        """The flight sent, its record then changed by other hands in `parts`, and the booking
        changed since, so it is due."""
        led, st, log, syncing = a_flight_log()
        ti = st.upsert_item(st.find_or_create(item), item)
        assert syncing.run_pass(now=now).done == {"create": 1}
        fid = st.item(ti.id).airtrail_flight_id
        log.by_hand(fid, **another(led, *parts), **more)
        st.set_status(ti.id, "cancelled")
        log.requests.clear()
        return st, log, syncing, ti, fid

    def unlinked(st: TripStore, log: Scripted, syncing: Syncer, ti: TripItem) -> bool:
        """The pass unlinks the booking, sends nothing, and never takes it up again."""
        held = log.flights
        took = syncing.run_pass(now=now + seconds(HOUR)).done
        after = st.item(ti.id)
        left = (after.airtrail_flight_id, after.told, after.sync_hold, after.sync_state)
        again = syncing.run_pass(now=now + seconds(2 * HOUR)).done
        asked = [r[0] for r in log.requests]
        return (
            (took, again, left) == ({"detach": 1}, {}, (None, None, "deleted", "off"))
            and (asked, log.sent) == (["get"], [])
            and log.flights == held
        )

    def updated(st: TripStore, log: Scripted, syncing: Syncer, ti: TripItem, fid: int) -> bool:
        took = syncing.run_pass(now=now + seconds(HOUR)).done
        after = st.item(ti.id)
        return (
            took == {"update": 1}
            and [r[1] for r in log.sent] == [fid]
            and (after.airtrail_flight_id, after.sync_state) == (fid, "synced")
        )

    # two of the three other, or all three: another flight
    pairs = (("day", "number"), ("day", "origin"), ("number", "origin"))
    for parts in (("day", "number", "origin"), *pairs):
        st, log, syncing, ti, fid = sent_then_changed_there(*parts)
        assert unlinked(st, log, syncing, ti), parts
    # one other: the owner changed the flight there by hand, and it is updated as ever
    for part in ("day", "number", "origin"):
        st, log, syncing, ti, fid = sent_then_changed_there(part)
        assert updated(st, log, syncing, ti, fid), part
    # a part the record does not say agrees with nothing: with no number there, a record on
    # the day it was told is still the flight, and one on another day is not
    st, log, syncing, ti, fid = sent_then_changed_there(flightNumber=None)
    assert updated(st, log, syncing, ti, fid)
    st, log, syncing, ti, fid = sent_then_changed_there("day", flightNumber=None)
    assert unlinked(st, log, syncing, ti)
    # a part neither side says (told with no number, as a flight filed from the flight log's
    # own record may be) has the destination stand in for it: a numberless record on another
    # day between the same airports is still the flight, and one to another airport is not
    st, log, syncing, ti, fid = sent_then_changed_there("day", flightNumber=None)
    st.sync_done(ti.id, flight_id=fid, told={**st.item(ti.id).told, "flight": None})
    assert st.item(ti.id).sync_state == "pending" and updated(st, log, syncing, ti, fid)
    st, log, syncing, ti, fid = sent_then_changed_there("day", "destination", flightNumber=None)
    st.sync_done(ti.id, flight_id=fid, told={**st.item(ti.id).told, "flight": None})
    assert st.item(ti.id).sync_state == "pending" and unlinked(st, log, syncing, ti)

    def linked_with_nothing_told(*parts: str):
        """A booking linked to a record of the owner's with nothing recorded as told (a link
        older than what was told), the record another flight in `parts`."""
        led, st, log, syncing = a_flight_log()
        record = {
            "date": p["date_local"],
            "flightNumber": p["flight_code"],
            "fromId": ref_id(led, "airport", p["origin"]),
            "toId": ref_id(led, "airport", p["destination"]),
            "passengers": [{"userId": MEMORY_USER_ID}],
        }
        fid = log.create_flight(record)
        log.by_hand(fid, **another(led, *parts))
        ti = st.upsert_item(st.find_or_create(item), item, airtrail_flight_id=fid)
        assert (ti.told, ti.sync_state) == (None, "pending")
        log.requests.clear()
        return st, log, syncing, ti, fid

    # nothing told: the record is held against what the booking itself says
    for part in ("day", "number", "origin"):
        st, log, syncing, ti, fid = linked_with_nothing_told(part)
        assert updated(st, log, syncing, ti, fid), part
    for parts in (("day", "number", "origin"), *pairs):
        st, log, syncing, ti, fid = linked_with_nothing_told(*parts)
        assert unlinked(st, log, syncing, ti), parts


def test_a_record_with_only_its_scheduled_departure_has_that_stand_in_for_its_departure():
    """A record with no number and no origin code has its departure's instant stand in for the
    origin; a record that holds only its scheduled departure (as the backfill reads one) has
    that one stand in, so it is the flight as told when the scheduled instant is the one told."""
    entry, flight = booking("flight", owners)
    p = validated(entry, flight).payload
    known = {"date": p["date_local"], "flight": None, "from": None, "to": p["destination"],
             "departure": p["dep_utc"]}
    record = {"date": p["date_local"], "flightNumber": None, "from": {"iata": None},
              "to": {"iata": p["destination"]}, "departure": None,
              "departureScheduled": p["dep_utc"]}
    assert sync.as_told(record, known)
    assert sync.as_told({**record, "departure": p["dep_utc"], "departureScheduled": None}, known)
    later = (parse_iso(p["dep_utc"]) + dt.timedelta(hours=1)).isoformat()
    assert not sync.as_told({**record, "departureScheduled": later}, known)
    # the record's departure, when it holds one, is compared before its scheduled one
    assert not sync.as_told({**record, "departure": later}, known)


def test_a_flight_the_owner_is_off_there_is_unlinked_though_the_key_could_write_to_it():
    """A key that holds any-flights scopes, under a role that may see every flight, is refused
    nothing: it reads, and could write, a flight its user is not on. So whose flight a record is
    is read from the record itself: one that lists passengers, none of them the key's user, is
    no longer the owner's there. It is unlinked, nothing is written to it, and the next flight
    is sent in the same pass."""
    c_entry, with_others = booking("flight", with_a_companion)
    taken = validated(c_entry, with_others).key
    entry, alone = next(
        (e, r)
        for e, r in bookings("flight")
        if owners(r) and not checks.codes(validated(e, r)) and validated(e, r).key != taken
    )
    wide = {f"flight.{what}.any" for what in ("read", "update", "passengers.manage")}
    full_access = a_key("the first user", scopes=frozenset(REQUIRED_SCOPES) | wide)
    alerts: list[str] = []
    led, st, airtrail, syncing = a_flight_log(alerts, profile=full_access)
    rows = [filed(st, c_entry, with_others), filed(st, entry, alone)]
    now = clock(c_entry)
    assert syncing.run_pass(now=now).done == {"create": 2}
    linked = [st.item(x.id).airtrail_flight_id for x in rows]
    there = {f["id"]: f for f in airtrail.flights}
    # the owner takes themself off the first flight in the flight log; the companions stay
    staying = [x for x in there[linked[0]]["passengers"] if not x["user"]]
    assert staying and len(staying) < len(there[linked[0]]["passengers"])
    airtrail.by_hand(linked[0], passengers=staying)
    for x in rows:
        st.set_status(x.id, "cancelled")  # both are due again
    held = {f["id"]: f for f in airtrail.flights}
    airtrail.requests.clear()
    report = syncing.run_pass(now=now + seconds(HOUR))
    assert report == PassReport(done={"detach": 1, "update": 1}, stopped=False)
    first, second = (st.item(x.id) for x in rows)
    assert (first.airtrail_flight_id, first.told) == (None, None)
    assert (first.sync_hold, first.sync_state, first.checks) == ("deleted", "off", ())
    assert (second.sync_state, second.airtrail_flight_id) == ("synced", linked[1])
    asked = [r[:2] for r in airtrail.requests]
    assert asked == [("get", linked[0]), ("get", linked[1]), ("update", linked[1])]
    after = {f["id"]: f for f in airtrail.flights}
    assert after[linked[0]] == held[linked[0]] and after[linked[1]] != held[linked[1]]
    assert alerts == [] and not led.kv.get("airtrail_key_problem")
    # the record under the second id becomes somebody else's flight, another user aboard
    theirs = [{**x, "user": {"id": "another user"}} for x in after[linked[1]]["passengers"]]
    airtrail.by_hand(linked[1], passengers=theirs)
    st.set_status(rows[1].id, "confirmed")
    held = airtrail.flights
    airtrail.requests.clear()
    assert syncing.run_pass(now=now + seconds(2 * HOUR)).done == {"detach": 1}
    assert [r[:2] for r in airtrail.requests] == [("get", linked[1])]
    assert airtrail.flights == held and st.item(rows[1].id).sync_hold == "deleted"
    assert alerts == []


def test_a_key_of_another_user_stops_the_pass_and_sends_nothing():
    """The ids a ledger holds are one AirTrail user's flights. A key that answers as another
    user (another AirTrail at the address, or another user's key) is asked nothing more: no
    flight is claimed, looked for or sent, and the owner is told once what to run."""
    entry, flight = booking("flight", owners)
    alerts: list[str] = []
    first, other = a_key("the first user"), a_key("another user")
    led, st, airtrail, syncing = a_flight_log(alerts, profile=first)
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    assert led.kv.get("airtrail_user") == first.user_id
    st.set_status(ti.id, "cancelled")  # due again
    held = airtrail.flights
    airtrail.answers_as(other)
    airtrail.requests.clear()
    for n in range(1, 3):
        report = syncing.run_pass(now=now + seconds(n * HOUR))
        assert report == PassReport(done={"other": 1}, stopped=True)
        assert syncing.sync_one(ti.id, now=now + seconds(n * HOUR)) == "other"
        row = st.item(ti.id)
        assert row.sync_state == "pending" and untouched(row)
    assert airtrail.requests == [("profile",)] and airtrail.flights == held
    assert alerts == [OTHER_LOG]
    # a flight that becomes due only once it is claimed is not sent either: nothing is asked for
    # a flight unless the pass began with the ledger bound to the key's user
    real = syncing._due
    turns = iter([False])
    syncing._due = lambda ti, now: next(turns, None) is None and real(ti, now)
    assert syncing.sync_one(ti.id, now=now + seconds(3 * HOUR)) == "wait"
    syncing._due = real
    assert airtrail.requests == [("profile",)] and untouched(st.item(ti.id))
    assert led.kv.get("airtrail_user") == first.user_id
    assert led.kv.get("airtrail_key_problem") == "other_log"
    assert set(report.done) <= set(sync.OPERATIONS)
    # the first user's key again: the pass sends, and the next such key is told again
    airtrail.answers_as(first)
    assert syncing.run_pass(now=now + seconds(3 * HOUR)).done == {"update": 1}
    assert not led.kv.get("airtrail_key_problem") and alerts == [OTHER_LOG]
    st.set_status(ti.id, "confirmed")
    airtrail.answers_as(other)
    assert syncing.run_pass(now=now + seconds(4 * HOUR)).stopped and alerts == [OTHER_LOG] * 2


def test_the_first_pass_binds_the_ledger_and_keeps_what_the_key_may_do():
    entry, flight = booking("flight", owners)
    last = REQUIRED_SCOPES[-1]
    key = a_key(
        "the first user",
        scopes=(frozenset(REQUIRED_SCOPES) - {last}) | {"flight.delete.own"},
        permissions=frozenset({"flight.read.own", "flight.create.any"}),
        is_owner=False,
    )
    led, st, airtrail, syncing = a_flight_log(profile=key)
    now = clock(entry)
    # with no flight due nothing is asked and nothing is bound
    assert syncing.run_pass(now=now).done == {} and airtrail.requests == []
    assert led.kv.get("airtrail_user") is None and led.kv.get("airtrail_key") is None
    ti = filed(st, entry, flight)
    with util.clock_at(now):
        assert syncing.run_pass(now=now).done == {"create": 1}
    assert [r[0] for r in airtrail.requests].count("profile") == 1
    assert airtrail.requests[0] == ("profile",)
    assert led.kv.get("airtrail_user") == key.user_id
    kept = {
        "at": store_module.stamp(now),
        "kind": "apiKey",
        "missing": [last],
        "denied": ["flight.update.own", last],
        "more": ["flight.delete.own"],
    }
    assert json.loads(led.kv.get("airtrail_key")) == kept
    ((_, body),) = airtrail.sent
    assert key.user_id in [x.get("userId") for x in body["passengers"]]
    # the same key at a later pass: what is kept stays, its time too
    st.set_status(ti.id, "cancelled")
    with util.clock_at(now + seconds(HOUR)):
        assert syncing.run_pass(now=now + seconds(HOUR)).done == {"update": 1}
    assert json.loads(led.kv.get("airtrail_key")) == kept
    # the user's key with every scope, the user now the instance's owner: kept anew
    airtrail.answers_as(a_key(key.user_id))
    st.set_status(ti.id, "confirmed")
    later = now + seconds(2 * HOUR)
    with util.clock_at(later):
        assert syncing.run_pass(now=later).done == {"update": 1}
    whole = {"at": store_module.stamp(later), "kind": "apiKey"}
    whole.update(missing=[], denied=[], more=[])
    assert json.loads(led.kv.get("airtrail_key")) == whole


def test_a_profile_that_cannot_be_read_ends_the_pass_and_touches_no_flight():
    """The profile is read before the first flight of a pass. A refusal of it is the key's and
    stops the pass in the words of its kind; anything else is an outage; either way no flight's
    row is touched, and nothing is bound."""
    entry, flight = booking("flight", owners)
    alerts: list[str] = []
    led, st, airtrail, syncing = a_flight_log(alerts, profile=a_key("the first user"))
    ti = filed(st, entry, flight)
    now = clock(entry)
    stopped, down = PassReport(done={"stop": 1}, stopped=True), PassReport(
        done={"outage": 1}, stopped=False, down=True
    )
    refusals = (
        ({"status": 401}, stopped, "unauthorized"),
        ({"status": 403, **lacks(REQUIRED_SCOPES[0])}, stopped, f"scope:{REQUIRED_SCOPES[0]}"),
        ({"status": 403, "code": "forbidden", "message": ROLE_SAYS}, stopped, "role"),
        ({"status": 503}, down, "role"),
        ({"status": 404}, down, "role"),
    )
    for n, (failure, report, word) in enumerate(refusals):
        airtrail.fail_next(failure.pop("status"), on="profile", **failure)
        assert syncing.run_pass(now=now + seconds(n * 60)) == report, word
        row = st.item(ti.id)
        assert row.sync_state == "pending" and untouched(row), word
        assert led.kv.get("airtrail_key_problem") == word
        assert airtrail.requests == [("profile",)] * (n + 1)
    assert len(alerts) == 3 and REQUIRED_SCOPES[0] in alerts[1] and ROLE_SAYS in alerts[2]
    assert led.kv.get("airtrail_user") is None and led.kv.get("airtrail_down_since")
    assert syncing.run_pass(now=now + seconds(HOUR)).done == {"create": 1}
    assert led.kv.get("airtrail_user") == "the first user"
    assert not led.kv.get("airtrail_key_problem") and not led.kv.get("airtrail_down_since")


def test_the_owner_taken_off_a_sent_flight_is_never_written_to_and_is_marked():
    """The owner leaves a flight the flight log already holds as theirs (their seat cancelled
    while the family still goes). Nothing is sent, ever: the flight reads off, and the booking is
    marked so the owner can tidy the flight log by hand. Looking closes the mark and a replay
    does not reopen it; the owner back on the flight, it is gone and what changed meanwhile is
    sent as for any flight."""
    entry, flight = booking("flight", with_a_companion)
    owner = next(n for n in names(flight) if book().match(n).kind == "me")
    led, st, airtrail, syncing = a_flight_log()
    ti = filed(st, entry, flight)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    (held,) = airtrail.flights
    airtrail.requests.clear()

    def nothing_is_sent(at: dt.datetime) -> bool:
        idle = st.due_for_sync(now=at) == [] and syncing.run_pass(now=at).done == {}
        return idle and syncing.sync_one(ti.id, now=at) == "wait" and airtrail.requests == []

    with util.clock_at(now + seconds(HOUR)):
        gone = st.remove_passengers(ti.item_key, [owner])
    assert (gone.sync_state, gone.checks) == ("off", ("airtrail_still_lists",))
    assert checks.LABELS["airtrail_still_lists"] == STILL_LISTS
    assert checks.still_listed("off", gone.told) and gone.airtrail_flight_id == held["id"]
    assert nothing_is_sent(now + seconds(HOUR))
    # Looks right closes it, and the same email read again does not reopen it
    with util.clock_at(now + seconds(2 * HOUR)):
        looked = st.looks_right(ti.id)
        assert (looked.sync_state, looked.checks) == ("off", ())
        again = st.remove_passengers(ti.item_key, [owner])
        assert (again.sync_state, again.checks) == ("off", ())
        # the flight changes while the owner is off it: still nothing is sent
        edited = st.edit_item(ti.id, {"arr_local": minutes_later(ti.payload["arr_local"], 5)})
    assert (edited.sync_state, edited.checks) == ("off", ())
    assert nothing_is_sent(now + seconds(2 * HOUR)) and airtrail.flights == [held]
    # the owner is on the flight again: the mark is gone, and the change is sent as any other
    with util.clock_at(now + seconds(3 * HOUR)):
        back = st.owner_answer(ti.id, True)
    assert (back.sync_state, back.checks) == ("pending", ())
    assert "airtrail_still_lists" not in (back.payload.get("raised") or {})
    assert not checks.still_listed("on", back.told)
    assert syncing.run_pass(now=now + seconds(3 * HOUR)).done == {"update": 1}
    (there,) = airtrail.flights
    assert there["arrival"] == back.payload["arr_utc"] != held["arrival"]
    assert [x for x in there["passengers"] if x["user"]] == [
        x for x in held["passengers"] if x["user"]
    ]
    # off it once more, after that look: it is marked again
    with util.clock_at(now + seconds(4 * HOUR)):
        assert st.remove_passengers(ti.item_key, [owner]).checks == ("airtrail_still_lists",)


def test_no_path_that_makes_a_flight_due_sends_anything_for_a_flight_the_owner_has_left():
    """A flight the flight log holds, whose booking no longer holds the owner — by an email, by
    an edit on the page or by the owner's own answer: whatever then touches the booking or the
    flight log's switch, no request is made for it."""
    entry, flight = booking("flight", with_a_companion)
    everyone = names(flight)
    owner = next(n for n in everyone if book().match(n).kind == "me")
    others = [n for n in everyone if n != owner]
    booked = validated(entry, flight)
    now = clock(entry)
    today = now.date()

    def by_an_email(st, ti):
        st.remove_passengers(ti.item_key, [owner])

    def on_the_page(st, ti):
        st.edit_item(ti.id, {"passengers": others})

    def by_an_answer(st, ti):
        st.owner_answer(ti.id, False)

    for leave in (by_an_email, on_the_page, by_an_answer):
        led, st, airtrail, syncing = a_flight_log()
        ti = st.upsert_item(st.find_or_create(booked), booked)
        assert syncing.run_pass(now=now).done == {"create": 1}
        held = airtrail.flights
        airtrail.requests.clear()
        leave(st, ti)
        paths = {
            "left": lambda: None,
            "a page edit": lambda st=st, ti=ti: st.edit_item(
                ti.id, {"arr_local": minutes_later(ti.payload["arr_local"], 5)}
            ),
            "looks right": lambda st=st, ti=ti: st.looks_right(ti.id),
            "a replay": lambda st=st, ti=ti, leave=leave: (
                leave(st, ti)
                if leave is by_an_email
                else st.upsert_item(st.get(ti.trip_id), validated(entry, flight))
            ),
            "send again": lambda st=st, ti=ti: st.send_again(ti.id),
            "cancelled": lambda st=st, ti=ti: st.set_status(ti.id, "cancelled"),
            "confirmed": lambda st=st, ti=ti: st.set_status(ti.id, "confirmed"),
            "switched off": lambda st=st, led=led: lifecycle.note_airtrail(
                st, led, on=False, today=today
            ),
            "switched on": lambda st=st, led=led: lifecycle.note_airtrail(
                st, led, on=True, today=today
            ),
            "enrolled": lambda st=st: lifecycle.enroll(st, since=dt.date.min, apply=True),
            "removed": lambda st=st, ti=ti: st.remove_item(ti.id),
            "restored": lambda st=st, ti=ti: st.restore_item(ti.id),
            "trip deleted": lambda st=st, ti=ti: st.delete_trip(st.item(ti.id).trip_id),
            "trip restored": lambda st=st, ti=ti: st.restore_trip(st.item(ti.id).trip_id),
            "settled": lambda st=st: st.settle_flights(),
        }
        for n, (path, take) in enumerate(paths.items(), start=1):
            take()
            at = now + seconds(n * HOUR)
            row = st.item(ti.id)
            assert row.sync_state == "off", (leave.__name__, path)
            assert st.due_for_sync(now=at) == [], (leave.__name__, path)
            if path != "switched off":
                assert syncing.run_pass(now=at).done == {}, (leave.__name__, path)
            assert syncing.sync_one(ti.id, now=at) == "wait", (leave.__name__, path)
            assert airtrail.requests == [], (leave.__name__, path)
        # even a row left saying it is due (a state settled before the owner left): the pass
        # reads who is on the flight before it asks the flight log anything
        led.trip_items.update(ti.id, sync_state="pending")
        at = now + seconds((len(paths) + 1) * HOUR)
        assert ids(st.due_for_sync(now=at)) == [ti.id]
        assert syncing.run_pass(now=at).done == {"wait": 1}, leave.__name__
        assert syncing.sync_one(ti.id, now=at) == "wait" and airtrail.requests == []
        assert airtrail.flights == held, leave.__name__
        assert st.item(ti.id).airtrail_flight_id == held[0]["id"]
        # the flight log forgotten (another to be kept), then a pass: unlinked, nothing sent
        lifecycle.forget(st, led, today=today, apply=True)
        at = now + seconds((len(paths) + 2) * HOUR)
        row = st.item(ti.id)
        assert (row.sync_state, row.airtrail_flight_id) == ("off", None), leave.__name__
        assert st.due_for_sync(now=at) == [] and syncing.run_pass(now=at).done == {}
        assert syncing.sync_one(ti.id, now=at) == "wait", leave.__name__
        assert airtrail.requests == [] and airtrail.flights == held, leave.__name__


def test_an_airline_the_flight_log_does_not_know_is_left_empty_and_not_sent_again():
    entry, flight = booking("flight", owners)
    led, st, airtrail, syncing = a_flight_log()
    item = validated(entry, flight)
    carrier = item.payload["carrier"]
    led.conn.execute("DELETE FROM ref_cache WHERE kind='airline' AND code=?", (carrier,))
    ti = st.upsert_item(st.find_or_create(item), item)
    now = clock(entry)
    assert syncing.run_pass(now=now).done == {"create": 1}
    assert [r[1:] for r in airtrail.requests if r[0] == "reference"] == [(AIRLINES_PATH, carrier)]
    ((_, body),) = airtrail.sent
    (made,) = airtrail.flights
    assert body["airlineId"] is None and made["airline"] is None
    after = st.item(ti.id)
    assert (after.sync_state, after.checks, after.told) == ("synced", (), wanted(after))
    # nothing is due, so the airline is not asked for again and nothing is sent again
    airtrail.requests.clear()
    for n in range(1, 3):
        assert syncing.run_pass(now=now + seconds(n * HOUR)).done == {}
    assert airtrail.requests == [] and airtrail.flights == [made]
    # a later change leaves the airline as it is there
    st.set_status(ti.id, "cancelled")
    assert syncing.run_pass(now=now + seconds(3 * HOUR)).done == {"update": 1}
    assert airtrail.flights[0]["airline"] is None
