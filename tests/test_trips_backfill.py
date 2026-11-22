# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox airtrail backfill`: AirTrail's flight log filed into trips, as emails would."""

import copy
import datetime as dt
import itertools
import string
from zoneinfo import ZoneInfo

import pytest
from helpers.eml import make_eml
from helpers.flightlog import Scripted
from helpers.sample import TRAVELLERS
from test_run import AR, make_ctx

from tripbox.ledger import Ledger
from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.pipeline import run as runmod
from tripbox.trips import backfill as bf
from tripbox.trips.store import TripStore
from tripbox.validate import checks
from tripbox.validate.keys import is_draft, real_key
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import validate_extraction
from tripbox.write.airtrail import REQUIRED_SCOPES, AirTrailClient, Profile
from tripbox.write.sync import PassReport, Syncer

HOME = ("MSP",)
ME = "Alex Sample"


def ap(iata, icao, city, tz, aid):
    return {
        "id": aid,
        "icao": icao,
        "iata": iata,
        "name": f"{city} Airport",
        "municipality": city,
        "timezone": tz,
    }


MSP = ap("MSP", "KMSP", "Minneapolis", "America/Chicago", 1)
FAR = ap("FAR", "KFAR", "Fargo", "America/Chicago", 2)
SEA = ap("SEA", "KSEA", "Seattle", "America/Los_Angeles", 3)
ANC = ap("ANC", "PANC", "Anchorage, Alaska", "America/Anchorage", 4)
PDX = ap("PDX", "KPDX", "Portland", "America/Los_Angeles", 5)
SLC = ap("SLC", "KSLC", "Salt Lake City", "America/Denver", 6)
IND = ap("IND", "KIND", "Indianapolis", "America/New_York", 7)
BWI = ap("BWI", "KBWI", "Baltimore", "America/New_York", 8)
RDU = ap("RDU", "KRDU", "Raleigh", "America/New_York", 9)
PHL = ap("PHL", "KPHL", "Philadelphia", "America/New_York", 10)
TPA = ap("TPA", "KTPA", "Tampa", "America/New_York", 11)
ATL = ap("ATL", "KATL", "Atlanta", "America/New_York", 12)
BZN = ap("BZN", "KBZN", "Bozeman", "America/Denver", 13)
DEN = ap("DEN", "KDEN", "Denver", "America/Denver", 14)

AIRLINES = {
    "UA": {"id": 1, "name": "United Airlines", "icao": "UAL", "iata": "UA"},
    "AS": {"id": 2, "name": "Alaska Airlines", "icao": "ASA", "iata": "AS"},
    "AA": {"id": 3, "name": "American Airlines", "icao": "AAL", "iata": "AA"},
    "DL": {"id": 4, "name": "Delta Air Lines", "icao": "DAL", "iata": "DL"},
}


def fl(fid, date, code, frm, to, dep, arr, pnr, *, seat="12C"):
    carrier = (code or "")[:2]
    return {
        "id": fid,
        "date": date,
        "departure": dep,
        "arrival": arr,
        "duration": None,
        "flightNumber": code,
        "aircraftReg": "N123",
        "note": f"PNR {pnr}; added by hand" if pnr else "no record",
        "from": frm,
        "to": to,
        "airline": AIRLINES.get(carrier, {"id": 9, "name": "Other", "icao": None, "iata": carrier}),
        "aircraft": None,
        "passengers": [
            {
                "id": fid,
                "user": {"id": "u1"},
                "guestName": None,
                "seat": "aisle",
                "seatNumber": seat,
                "seatClass": "economy",
            }
        ],
    }


# whose key the flight logs here answer to: the user every record of `fl` names aboard
OWNER = Profile("u1", "apiKey", frozenset(REQUIRED_SCOPES), frozenset(), True)


class FakeAirTrail:
    def __init__(self, flights):
        self.flights = list(flights)

    def profile(self):
        return OWNER

    def iter_flights(self, *, limit=100, until_date=None):
        yield from sorted(self.flights, key=lambda f: f["date"], reverse=True)


def fresh():
    led = Ledger.memory()
    return led, TripStore(led, HOME)


def run(led, store, flights, *, since="2028-01-01", apply=True):
    return bf.backfill(led, store, FakeAirTrail(flights), since=since, me_name=ME, apply=apply)


def titles(store):
    return sorted((t.title, t.start_date, t.end_date) for t in store.all_trips())


# ----------------------------------------------------------------------------- item shape
def test_flight_item_matches_the_validator_shape_and_converts_zones():
    f = fl(
        13,
        "2028-04-06",
        "DL9271",
        PHL,
        MSP,
        "2028-04-06T23:10:00.000+00:00",
        "2028-04-07T02:18:00.000+00:00",
        "Q9TW8Z",
    )
    it = bf.flight_item(f, ME)
    assert it.key == "DL|9271|2028-04-06|PHL"
    p = it.payload
    assert p["dep_local"] == "2028-04-06T19:10" and p["arr_local"] == "2028-04-06T21:18"
    assert p["dep_utc"] == "2028-04-06T23:10:00Z" and p["arr_utc"] == "2028-04-07T02:18:00Z"
    assert p["flight_code"] == "DL9271" and p["carrier"] == "DL" and p["flight_number"] == "9271"
    assert p["origin_city"] == "Philadelphia" and p["destination_tz"] == "America/Chicago"
    assert p["duration_min"] == 188 and p["pnr"] == "Q9TW8Z" and p["cabin"] == "economy"
    assert p["seats"] == ["12C"] and p["seat_by_passenger"] == {ME: "12C"}
    assert p["source"] == bf.SOURCE and p["airtrail_flight_id"] == 13
    assert it.pnr == "Q9TW8Z" and it.passengers == [ME]


def test_date_only_flight_and_missing_number_fall_back():
    f = fl(7, "2028-02-05", None, FAR, MSP, None, None, None)
    it = bf.flight_item(f, ME)
    assert it.key == "flight|airtrail|7"
    assert it.payload["dep_local"] == "2028-02-05" and it.payload["arr_local"] is None
    assert it.payload["dep_utc"] is None and it.pnr is None
    g = bf.flight_item(fl(8, "2028-02-05", "G49105", FAR, MSP, None, None, "ABC123"), ME)
    assert g.key == "G4|9105|2028-02-05|FAR" and g.payload["flight_code"] == "G49105"


# ----------------------------------------------------------------------------- grouping
def chain():
    """Four legs under one code: out through Seattle to Anchorage, home through Portland. The
    last leaves in the evening, after midnight UTC."""
    return [
        fl(
            1,
            "2028-07-08",
            "AS9302",
            MSP,
            SEA,
            "2028-07-08T15:00:00+00:00",
            "2028-07-08T18:50:00+00:00",
            "9QTWZS",
        ),
        fl(
            2,
            "2028-07-08",
            "AS9118",
            SEA,
            ANC,
            "2028-07-08T20:10:00+00:00",
            "2028-07-08T23:45:00+00:00",
            "9QTWZS",
        ),
        fl(
            3,
            "2028-07-17",
            "AS9141",
            ANC,
            PDX,
            "2028-07-17T17:30:00+00:00",
            "2028-07-17T21:05:00+00:00",
            "9QTWZS",
        ),
        fl(
            4,
            "2028-07-17",
            "AS9370",
            PDX,
            MSP,
            "2028-07-18T01:20:00+00:00",
            "2028-07-18T04:45:00+00:00",
            "9QTWZS",
        ),
    ]


def test_a_backfilled_flight_is_keyed_as_the_same_flight_from_mail():
    book = PassengerBook.from_yaml(TRAVELLERS)
    for f in chain():
        it = bf.flight_item(f, ME)
        p = it.payload
        leg = Segment(
            carrier_iata=p["carrier"],
            flight_number=p["flight_number"],
            origin=p["origin"],
            destination=p["destination"],
            departure_local=p["dep_local"],
            arrival_local=p["arr_local"],
        )
        ex = Extraction(
            document_type="booking",
            reservations=[Reservation(kind="flight", passengers=[ME], segments=[leg])],
        )
        (mail,) = validate_extraction(ex, refdata=Airports(), book=book, email_date=None)
        assert it.key == mail.key and not is_draft(mail.key), f["id"]

    # an origin the bundled table does not know makes no real key: the leg keeps AirTrail's id
    unknown = next(
        code
        for code in map("".join, itertools.product(string.ascii_uppercase[::-1], repeat=3))
        if Airports().airport(code) is None
    )
    f = chain()[0]
    f["from"] = {**f["from"], "iata": unknown}
    assert bf.flight_item(f, ME).key == f"flight|airtrail|{f['id']}"

    # the key's day is AirTrail's own date for the flight; an origin AirTrail gives no zone
    # takes the bundled table's, so an evening departure still falls on that day
    keyed = bf.flight_item(chain()[3], ME).key
    f = chain()[3]
    f["from"] = {**f["from"], "timezone": None}
    zoneless = bf.flight_item(f, ME)
    assert zoneless.key == keyed
    assert zoneless.payload["dep_local"][:10] == f["date"] == keyed.split("|")[2]


def test_one_pnr_chain_is_one_trip_at_the_longest_stay():
    led, store = fresh()
    rep = run(led, store, chain())
    assert titles(store) == [("Anchorage", "2028-07-08", "2028-07-17")]
    trip = store.all_trips()[0]
    assert [i.payload["flight_code"] for i in store.items(trip.id)] == [
        "AS9302",
        "AS9118",
        "AS9141",
        "AS9370",
    ]
    assert len(rep.created) == 1 and len(rep.filed) == 4 and not rep.skipped
    (msg,) = led.messages.all()
    assert (
        msg["source"] == bf.SOURCE and msg["status"] == "processed" and msg["archive_path"] is None
    )
    # one `filed` row per leg under the run's one message, each pointing at its booking, which
    # names no message as its source: it came from the flight log
    rows = led.items.by_message(msg["id"])
    assert [(r["n"], r["outcome"]) for r in rows] == [(n, "filed") for n in range(4)]
    filed = {r["trip_item_id"]: r["item_key"] for r in rows}
    assert {store.item(i).airtrail_flight_id for i in filed} == {1, 2, 3, 4}
    assert {(store.item(i).item_key, store.item(i).trip_id) for i in filed} == {
        (k, trip.id) for k in filed.values()
    }
    assert {store.item(i).source_message_id for i in filed} == {None}


def test_connection_on_the_way_home_keeps_the_destination_title():
    led, store = fresh()
    run(
        led,
        store,
        [
            fl(
                1,
                "2028-03-02",
                "DL9520",
                MSP,
                TPA,
                "2028-03-02T13:00:00+00:00",
                "2028-03-02T16:25:00+00:00",
                "RUXQTW",
            ),
            fl(
                2,
                "2028-03-05",
                "DL9533",
                TPA,
                ATL,
                "2028-03-05T15:00:00+00:00",
                "2028-03-05T16:35:00+00:00",
                "RUXQTW",
            ),
            fl(
                3,
                "2028-03-05",
                "DL9548",
                ATL,
                MSP,
                "2028-03-05T18:00:00+00:00",
                "2028-03-05T20:40:00+00:00",
                "RUXQTW",
            ),
        ],
    )
    assert titles(store) == [("Tampa", "2028-03-02", "2028-03-05")]


def test_back_to_back_trips_stay_separate():
    led, store = fresh()
    run(
        led,
        store,
        [
            fl(
                1,
                "2028-01-13",
                "DL9601",
                MSP,
                SLC,
                "2028-01-13T14:00:00+00:00",
                "2028-01-13T17:10:00+00:00",
                "SVYRUX",
            ),
            fl(
                2,
                "2028-01-16",
                "DL9602",
                SLC,
                MSP,
                "2028-01-16T18:00:00+00:00",
                "2028-01-16T20:45:00+00:00",
                "SVYRUX",
            ),
            fl(
                3,
                "2028-01-18",
                "UA9711",
                MSP,
                IND,
                "2028-01-18T13:00:00+00:00",
                "2028-01-18T14:40:00+00:00",
                "9Q8TW7",
            ),
            fl(
                4,
                "2028-01-21",
                "UA9712",
                IND,
                MSP,
                "2028-01-21T21:00:00+00:00",
                "2028-01-21T22:50:00+00:00",
                "9Q8TW7",
            ),
            fl(
                5,
                "2028-09-07",
                "AA9423",
                MSP,
                BWI,
                "2028-09-07T12:00:00+00:00",
                "2028-09-07T14:30:00+00:00",
                "QTWZ9S",
            ),
            fl(
                6,
                "2028-09-12",
                "AA9202",
                BWI,
                MSP,
                "2028-09-12T13:00:00+00:00",
                "2028-09-12T15:50:00+00:00",
                "QTWZ9S",
            ),
            fl(
                7,
                "2028-09-12",
                "DL9927",
                MSP,
                RDU,
                "2028-09-12T21:00:00+00:00",
                "2028-09-12T23:40:00+00:00",
                "QTWZS9",
            ),
            fl(
                8,
                "2028-09-15",
                "UA9678",
                RDU,
                MSP,
                "2028-09-15T15:00:00+00:00",
                "2028-09-15T17:55:00+00:00",
                "RUXQT9",
            ),
        ],
    )
    assert titles(store) == [
        ("Baltimore", "2028-09-07", "2028-09-12"),
        ("Indianapolis", "2028-01-18", "2028-01-21"),
        # the bundled table's city for the airport, as mail titles it
        (Airports().airport(RDU["iata"])["municipality"], "2028-09-12", "2028-09-15"),
        ("Salt Lake City", "2028-01-13", "2028-01-16"),
    ]


def test_return_under_another_pnr_joins_the_outbound_trip():
    led, store = fresh()
    rep = run(
        led,
        store,
        [
            fl(
                1,
                "2028-05-19",
                "DL9233",
                MSP,
                BZN,
                "2028-05-19T15:00:00+00:00",
                "2028-05-19T17:30:00+00:00",
                "TWZSVY",
            ),
            fl(
                2,
                "2028-05-21",
                "UA9245",
                BZN,
                MSP,
                "2028-05-21T22:00:00+00:00",
                "2028-05-22T00:25:00+00:00",
                "UXQTWZ",
            ),
        ],
    )
    assert titles(store) == [("Bozeman", "2028-05-19", "2028-05-21")]
    assert rep.created == [(1, "Bozeman", "2028-05-19", "2028-05-21")]  # dates after the return


def test_chain_joins_an_existing_trip_of_the_same_place_only():
    led, store = fresh()
    # a car booking made the trip to the place before any flight was filed
    car = bf.BackfillItem(
        "car",
        "car|9876543210",
        {
            "provider": "Example Rent A Car",
            "pickup_location": "Anchorage Airport",
            "pickup_city": "Anchorage",
            "pickup_time": "2028-07-08T16:30",
            "dropoff_time": "2028-07-17T08:30",
            "confirmation_code": "9876543210",
        },
        "9876543210",
        [ME],
    )
    trip = store.find_or_create(car)
    store.upsert_item(trip, car)
    assert titles(store) == [("Anchorage", "2028-07-08", "2028-07-17")]
    # and an unrelated trip covering the same dates must not swallow the flights
    other = store.new_trip(
        "Denver",
        bf._span([bf.flight_item(chain()[0], ME)])[0],
        bf._span([bf.flight_item(chain()[3], ME)])[1],
    )
    rep = run(led, store, chain())
    assert not rep.created
    assert [t.title for t in store.all_trips() if t.id == trip.id] == ["Anchorage"]
    assert len(store.items(trip.id)) == 5 and store.items(other.id) == []


# ----------------------------------------------------------------------------- relink
def seed_written(led, store, *, key, fid, pnr, date, origin, dest, code):
    mid = led.messages.insert(sha256=f"s{fid}", source="imap", status="processed")
    payload = {
        "carrier": code[:2],
        "flight_number": code[2:],
        "flight_code": code,
        "origin": origin,
        "destination": dest,
        "origin_city": "x",
        "destination_city": "y",
        "origin_tz": "America/New_York",
        "destination_tz": "America/Chicago",
        "dep_local": f"{date}T16:45",
        "arr_local": f"{date}T18:50",
        "dep_utc": f"{date}T20:45:00Z",
        "arr_utc": f"{date}T23:50:00Z",
        "date_local": date,
        "pnr": pnr,
        "passengers": [ME],
    }
    item = bf.BackfillItem("flight", key, payload, pnr, [ME])
    trip = store.find_or_create(item)
    ti = store.upsert_item(trip, item, airtrail_flight_id=fid, source_message_id=mid)
    # as the email's write is recorded: the flight's id and what AirTrail was told
    ti = store.sync_done(ti.id, flight_id=fid, told=told(store, ti))
    iid = led.items.add(
        message_id=mid,
        n=0,
        kind="flight",
        item_key=key,
        outcome="filed",
        payload=payload,
        trip_item_id=ti.id,
    )
    return iid, trip, ti


def told(store, ti):
    """What a flight log should hold of the booking `ti` (`checks.desired`)."""
    return checks.desired(ti.payload, ti.passengers, ti.status, store.book)


def seed_renumbered(led, store):
    """A flight an email wrote to AirTrail as #5, whose number a later import changes."""
    return seed_written(
        led,
        store,
        key="DL|9246|2028-04-06|PHL",
        fid=5,
        pnr="Q9TW8Z",
        date="2028-04-06",
        origin="PHL",
        dest="MSP",
        code="DL9246",
    )


def seed_unmatched(led, store):
    """A flight an email wrote to AirTrail as #6, which no import matches."""
    return seed_written(
        led,
        store,
        key="UA|9330|2028-04-20|MSP",
        fid=6,
        pnr="QT9876",
        date="2028-04-20",
        origin="MSP",
        dest="DEN",
        code="UA9330",
    )


def test_relink_repoints_and_rekeys_a_renumbered_booking():
    led, store = fresh()
    iid, trip, ti = seed_renumbered(led, store)
    logged = dict(led.items.get(iid))
    # the last attempt to send it failed: the relink makes that attempt the past
    now = dt.datetime.now(dt.UTC)
    store.sync_failed(ti.id, code="refused", now=now, retry_at=now + dt.timedelta(hours=1))
    new = fl(
        13,
        "2028-04-06",
        "DL9271",
        PHL,
        MSP,
        "2028-04-06T23:10:00+00:00",
        "2028-04-07T02:18:00+00:00",
        "Q9TW8Z",
    )
    rep = run(led, store, [new])
    assert rep.relinked == [(ti.id, "DL|9246|2028-04-06|PHL", "DL|9271|2028-04-06|PHL", 13)]
    # the email's row in the log is left as it was written, pointing at the booking
    assert dict(led.items.get(iid)) == logged and logged["trip_item_id"] == ti.id
    items = store.items(trip.id)
    assert len(items) == 1 and items[0].id == ti.id
    assert items[0].item_key == "DL|9271|2028-04-06|PHL" and items[0].airtrail_flight_id == 13
    assert items[0].payload["flight_code"] == "DL9271" and items[0].start_at == "2028-04-06T19:10"
    # the flight log holds the booking as that flight, and is recorded as told so
    assert items[0].told == told(store, items[0]) and items[0].told != ti.told
    after = (items[0].sync_error, items[0].sync_error_at, items[0].sync_attempts)
    assert after == (None, None, 0) and items[0].sync_after is None
    # the flight is now filed: nothing to add, nothing duplicated
    assert rep.skipped == ["DL|9271|2028-04-06|PHL"] and not rep.filed


def test_an_unmatched_dead_id_is_reported_absent_and_its_log_row_kept():
    led, store = fresh()
    iid, _, ti = seed_unmatched(led, store)
    logged = dict(led.items.get(iid))
    rep = run(led, store, [fl(20, "2028-04-21", "UA9330", MSP, DEN, None, None, "OTHER1")])
    assert rep.absent == [(ti.id, "UA|9330|2028-04-20|MSP", 6)]
    assert dict(led.items.get(iid)) == logged


def test_relink_refuses_to_clobber_a_row_filed_under_the_new_key():
    led, store = fresh()
    seed_renumbered(led, store)
    new = bf.flight_item(
        fl(
            13,
            "2028-04-06",
            "DL9271",
            PHL,
            MSP,
            "2028-04-06T23:10:00+00:00",
            "2028-04-07T02:18:00+00:00",
            "Q9TW8Z",
        ),
        ME,
    )
    t = store.new_trip("Philadelphia", *bf._span([new]))
    store.upsert_item(t, new, airtrail_flight_id=13)
    with pytest.raises(RuntimeError, match="relink before filing"):
        store.relink_flight("DL|9246|2028-04-06|PHL", new.key, 13, {})


def test_a_dead_flight_id_with_no_match_is_held_as_deleted():
    """A flight whose record is gone from the flight log, with none imported in its place, is
    unlinked and held out of the reconcile, so nothing makes it there again unasked; it stays on
    the page. The same for a flight an email filed and for one the backfill filed: the reader is
    the trip store. The log is left as it was written, the email's row and the backfill's. A
    cancelled flight is left alone, as before."""
    led, store = fresh()
    run(led, store, chain())
    iid, _, mailed = seed_unmatched(led, store)
    # and a flight cancelled by email: its record there carried the cancellation
    _, _, cancelled = seed_renumbered(led, store)
    cancelled = store.cancel(cancelled.item_key)
    first = bf.flight_item(chain()[0], ME)
    first_id = first.payload["airtrail_flight_id"]
    logged = store.find(first.key)
    assert {t.id for t in store.sent_flights()} >= {mailed.id, logged.id, cancelled.id}
    # the flight log lost all three: the first leg's record, the mailed and the cancelled one's
    rep = run(led, store, chain()[1:])
    assert sorted(rep.absent) == sorted(
        [(mailed.id, mailed.item_key, 6), (logged.id, first.key, first_id)]
    )
    for ti in (store.item(mailed.id), store.item(logged.id)):
        assert (ti.airtrail_flight_id, ti.told, ti.sync_hold) == (None, None, "deleted")
        assert ti.sync_state == "off" and ti.removed_at is None and ti.trip_id is not None
    assert {mailed.id, logged.id}.isdisjoint(t.id for t in store.sent_flights())
    assert store.item(cancelled.id) == cancelled  # left as it was
    assert led.items.get(iid)["trip_item_id"] == mailed.id
    (adopted,) = led.items.by_trip_item(logged.id)
    assert (adopted["outcome"], adopted["item_key"]) == ("filed", first.key)
    # a run after that holds nothing out again and files neither flight a second time
    again = run(led, store, chain()[1:])
    assert not again.absent and not again.relinked and not again.filed
    assert len(store.items(logged.trip_id)) == 4


class HeldLog(Scripted):
    """A flight log in memory holding `flights` (each as its API returns it), recording every
    request but the backfill's read of the list."""

    def __init__(self, ledger, flights):
        super().__init__(ledger, profile=OWNER)
        self._flights = {int(f["id"]): copy.deepcopy(f) for f in flights}


def test_a_backfilled_flight_reads_as_synced_and_is_never_sent_back():
    """Every leg the backfill files is recorded as told what the flight log holds of it (its own
    record), so with the flight log on it is synced and a sync pass after the backfill sends
    nothing and asks nothing; with the flight log off it is off, as every flight is."""
    book = PassengerBook.from_yaml(TRAVELLERS)
    flights = chain()
    for on in (True, False):
        led = Ledger.memory()
        if on:
            led.kv.set("airtrail", "on")
        store = TripStore(led, HOME, book=book)
        log = HeldLog(led, flights)
        since = min(f["date"] for f in flights)
        rep = bf.backfill(led, store, log, since=since, me_name=ME, apply=True)
        legs = [store.find(key) for key, *_ in rep.filed]
        assert len(legs) == len(flights), on
        for ti in legs:
            assert ti.told == told(store, ti) and ti.checks == (), (on, ti.item_key)
            assert ti.sync_state == ("synced" if on else "off"), (on, ti.item_key)
        held = log.flights
        log.requests.clear()
        syncer = Syncer(store=store, ledger=led, book=book, client=log, refs=None, dry_run=False)
        assert syncer.run_pass() == PassReport(done={}, stopped=False), on
        assert log.requests == [] and log.flights == held, on
        assert [store.item(ti.id) for ti in legs] == legs, on


def emailed(f: dict) -> dict:
    """The confirmation email of the flight log's flight `f` as kitinerary reads it: its code, its
    PNR, the owner on it, and each time printed in its airport's own zone."""
    code = f["flightNumber"]

    def airport(a: dict) -> dict:
        return {"@type": "Airport", "iataCode": a["iata"], "name": a["name"]}

    def printed(when: str, a: dict) -> str:
        return dt.datetime.fromisoformat(when).astimezone(ZoneInfo(a["timezone"])).isoformat()

    return {
        "@context": "http://schema.org",
        "@type": "FlightReservation",
        "reservationNumber": bf.PNR_RE.search(f["note"]).group(1),
        "underName": {"@type": "Person", "name": ME},
        "airplaneSeat": f["passengers"][0]["seatNumber"],
        "reservationFor": {
            "@type": "Flight",
            "flightNumber": code[2:],
            "airline": {
                "@type": "Airline",
                "iataCode": code[:2],
                "name": AIRLINES[code[:2]]["name"],
            },
            "departureAirport": airport(f["from"]),
            "arrivalAirport": airport(f["to"]),
            "departureTime": printed(f["departure"], f["from"]),
            "arrivalTime": printed(f["arrival"], f["to"]),
        },
    }


def test_a_backfilled_flight_whose_stored_day_differs_from_the_flight_logs_date_is_found_by_both_keys(  # noqa: E501
    tmp_path, monkeypatch
):
    """The backfill keys a leg by the flight log's own date and, while the booking's fields are
    the flight log's own, leaves that key alone whatever day its stored departure falls on. Once
    the flight's email is merged the booking is settled as any is; where the email's departure
    falls on another day than the flight log's date, it takes the key of its fields and keeps the
    flight log's as an alias: either finds it, a second copy of the email is a duplicate, and the
    backfill run again files nothing. One leg leaving after UTC midnight, in a record dated by its
    departure's UTC day (the email's falls the day before). (A record with no zone for an origin
    the bundled table holds takes the table's, so its departure falls on the flight log's date as
    the email's does.) The email is merged as the pipeline merges any: by its key, or, when its
    departure moved off the key's day, as the same booking moved (same code, airports and
    number)."""
    leg = chain()[3]
    record = {**leg, "date": leg["departure"][:10]}  # dated by its departure's UTC day
    departure = emailed(leg)["reservationFor"]["departureTime"]
    ctx, _ = make_ctx(tmp_path, monkeypatch, [emailed(leg)], airtrail=False)
    ctx.refdata = Airports()
    st = ctx.trips
    backfill_once(ctx, record)
    logged = bf.flight_item(record, ME).key
    ti = st.find(logged)
    stored = real_key("flight", ti.payload, airports=Airports())
    assert logged.split("|")[2] == record["date"] != ti.payload["dep_local"][:10]
    assert stored != logged and st.find(stored) is None
    assert st.settle(ti.id) == ti  # left as the flight log said it

    (out,) = mail_copy(ctx, leg, 1).outcomes
    after = st.item(ti.id)
    mailed = real_key("flight", after.payload, airports=Airports())
    assert (out.result, out.trip_item_id, bookings(ctx)) == ("updated", ti.id, 1)
    assert "source" not in after.payload and after.payload["dep_local"] == departure[:16]
    # the email's departure falls on another day than the flight log's date: the booking takes
    # the key of its fields and keeps the flight log's as an alias
    assert after.item_key == mailed != logged
    assert after.payload["dep_local"][:10] != record["date"]
    assert logged in after.payload["alt_keys"]
    assert st.find(logged).id == st.find(mailed).id == ti.id

    (copy_out,) = mail_copy(ctx, leg, 2).outcomes
    assert (copy_out.result, st.item(ti.id), bookings(ctx)) == ("duplicate", after, 1)
    again = backfill_once(ctx, record)
    assert again.skipped == [logged] and not again.filed and bookings(ctx) == 1


def backfill_once(ctx, record: dict) -> bf.BackfillReport:
    """The backfill of the flight log holding the one flight `record`, into the context's ledger."""
    return bf.backfill(
        ctx.ledger,
        ctx.trips,
        FakeAirTrail([record]),
        since=record["date"],
        me_name=ME,
        apply=True,
    )


def mail_copy(ctx, f: dict, n: int):
    """The `n`th copy of the confirmation email of the flight `f`, read by the pipeline; it was
    sent a month before the flight."""
    m = make_eml(
        from_="alex@example.com",
        subject="Fwd: Your trip",
        date=dt.datetime.fromisoformat(f["departure"]) - dt.timedelta(days=30),
        text="x",
        headers=AR,
        message_id=f"<copy{n}@example.com>",
    )
    return runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=n)


def bookings(ctx) -> int:
    return ctx.ledger.conn.execute("SELECT COUNT(*) FROM trip_items").fetchone()[0]


def log_rows(led) -> int:
    return led.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


# ----------------------------------------------------------------------------- runs
def test_second_run_changes_nothing():
    led, store = fresh()
    first = run(led, store, chain())
    before = (titles(store), log_rows(led), len(led.messages.all()))
    second = run(led, store, chain())
    assert len(first.filed) == 4 and not second.filed and not second.created
    assert sorted(second.skipped) == sorted(k for k, *_ in first.filed)
    assert (titles(store), log_rows(led), len(led.messages.all())) == before


def test_dry_run_writes_nothing_but_reports_everything():
    led, store = fresh()
    rep = run(led, store, chain(), apply=False)
    assert not rep.applied and len(rep.filed) == 4 and len(rep.created) == 1
    assert store.all_trips() == [] and log_rows(led) == 0 and led.messages.all() == []
    said = rep.lines(verbose=True)
    assert said[-1] == "backfill: nothing was changed; run again with --apply"
    assert any(line.startswith("  Anchorage, ") for line in said)


def test_window_filters_by_date_and_the_report_counts():
    led, store = fresh()
    old = fl(
        50,
        "2027-11-30",
        "UA9100",
        MSP,
        DEN,
        "2027-11-30T15:00:00+00:00",
        "2027-11-30T17:10:00+00:00",
        "OLDPNR",
    )
    rep = run(led, store, chain() + [old])
    assert rep.fetched == 5 and rep.in_window == 4 and len(rep.filed) == 4
    assert titles(store) == [("Anchorage", "2028-07-08", "2028-07-17")]


def test_backfill_refuses_without_home_airports():
    led = Ledger.memory()
    client = FakeAirTrail(chain())
    client.iter_flights = lambda **kw: pytest.fail("AirTrail was read")
    with pytest.raises(ValueError, match="TRIPBOX_HOME_AIRPORTS is empty"):
        bf.backfill(led, TripStore(led, ()), client, since="2028-01-01", me_name=ME, apply=True)
    assert led.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_backfill_needs_an_owner_name():
    led = Ledger.memory()
    client = FakeAirTrail(chain())
    client.iter_flights = lambda **kw: pytest.fail("AirTrail was read")
    with pytest.raises(ValueError, match="names no owner"):
        bf.backfill(led, TripStore(led, HOME), client, since="2028-01-01", me_name="", apply=True)
    assert led.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_backfill_names_the_home_airports_first_when_both_are_missing():
    led = Ledger.memory()
    with pytest.raises(bf.BackfillRefused, match="TRIPBOX_HOME_AIRPORTS is empty"):
        bf.backfill(
            led,
            TripStore(led, ()),
            FakeAirTrail(chain()),
            since="2028-01-01",
            me_name="",
            apply=True,
        )


# ----------------------------------------------------------------------------- the bundled table
def unknown_code() -> str:
    """A code the bundled airport table does not hold, found at run time."""
    return next(
        code
        for code in map("".join, itertools.product(string.ascii_uppercase[::-1], repeat=3))
        if Airports().airport(code) is None
    )


def respelt(f: dict) -> dict:
    """`f` with every city and airport name AirTrail gives spelt otherwise, and no zone."""
    ends = {}
    for end in ("from", "to"):
        a = f[end]
        ends[end] = {**a, "municipality": a["municipality"][::-1], "name": a["name"][::-1]}
        ends[end]["timezone"] = None
    return {**f, **ends}


def test_a_backfilled_flight_takes_its_cities_and_zones_from_the_bundled_table(
    tmp_path, monkeypatch
):
    """AirTrail spells a city otherwise than the bundled table: the backfilled flight takes the
    table's city, zone and airport name by its code, as mail does, so the chain joins the trip
    an email made for the same place instead of making one of its own."""
    for f in chain():
        p = bf.flight_item(respelt(f), ME).payload
        for end, field in (("from", "origin"), ("to", "destination")):
            known = Airports().airport(f[end]["iata"])
            assert p[f"{field}_city"] == known["municipality"], (f["id"], end)
            assert p[f"{field}_tz"] == known["timezone"], (f["id"], end)
            assert p[f"{field}_name"] == known["name"], (f["id"], end)
        assert p == bf.flight_item(f, ME).payload, f["id"]
    outbound = chain()[1]
    ctx, _ = make_ctx(tmp_path, monkeypatch, [emailed(outbound)], airtrail=False)
    ctx.refdata = Airports()
    (mailed,) = mail_copy(ctx, outbound, 1).outcomes
    trip = ctx.trips.get(ctx.trips.item(mailed.trip_item_id).trip_id)
    rep = bf.backfill(
        ctx.ledger,
        ctx.trips,
        FakeAirTrail([respelt(f) for f in chain()]),
        since=min(f["date"] for f in chain()),
        me_name=ME,
        apply=True,
    )
    assert not rep.created and {t for _, t, _ in rep.filed} == {trip.id}
    assert [t.id for t in ctx.trips.all_trips()] == [trip.id]
    assert len(ctx.trips.items(trip.id)) == len(chain())


def test_an_airport_the_table_lacks_keeps_the_flight_logs_own_words():
    f = chain()[1]
    code = unknown_code()
    f["from"] = {**f["from"], "iata": code, "municipality": f["from"]["municipality"][::-1]}
    p = bf.flight_item(f, ME).payload
    assert p["origin"] == code
    assert (p["origin_city"], p["origin_tz"], p["origin_name"]) == (
        f["from"]["municipality"],
        f["from"]["timezone"],
        f["from"]["name"],
    )
    # the local time is AirTrail's instant read in the zone AirTrail gives that airport
    when = dt.datetime.fromisoformat(f["departure"]).astimezone(ZoneInfo(f["from"]["timezone"]))
    assert p["dep_local"] == when.strftime("%Y-%m-%dT%H:%M")
    # the other end is in the table: its words are the table's
    assert p["destination_city"] == Airports().airport(f["to"]["iata"])["municipality"]


def test_a_backfill_then_a_pass_sends_nothing():
    """A flight the backfill files from a record whose cities and zones AirTrail spells
    otherwise is recorded as told what that record holds: the pass that follows sends no create
    and no update, and the flight log is as it was."""
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    book = PassengerBook.from_yaml(TRAVELLERS)
    store = TripStore(led, HOME, book=book)
    flights = [respelt(f) for f in chain()]
    log = HeldLog(led, flights)
    held = log.flights
    rep = bf.backfill(
        led, store, log, since=min(f["date"] for f in flights), me_name=ME, apply=True
    )
    assert len(rep.filed) == len(flights)
    syncer = Syncer(store=store, ledger=led, book=book, client=log, refs=None, dry_run=False)
    report = syncer.run_pass()
    assert not {"create", "update"} & set(report.done)
    assert [r for r in log.requests if r[0] in ("create", "update")] == []
    assert log.flights == held


def test_a_backfilled_flight_with_no_time_raises_no_check_and_stays_as_the_log_gave_it():
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    store = TripStore(led, HOME, book=PassengerBook.from_yaml(TRAVELLERS))
    f = fl(7, "2028-02-05", None, FAR, MSP, None, None, None)
    rep = bf.backfill(led, store, FakeAirTrail([f]), since=f["date"], me_name=ME, apply=True)
    (key,) = [k for k, *_ in rep.filed]
    ti = store.find(key)
    assert ti.checks == () and ti.sync_state == "synced"
    assert ti.payload["dep_local"] == f["date"] and ti.payload["dep_utc"] is None
    assert ti.payload["source"] == bf.SOURCE and ti.item_key == key
    assert store.settle(ti.id) == ti


def test_a_numberless_backfilled_flight_cancelled_on_the_page_is_updated_not_unlinked():
    """A flight the flight log holds with no number, from an airport with no code: neither part
    can agree, so the destination stands in for one of them. Cancelled on the page, the flight
    is updated there (its note gains the line), not unlinked."""
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    book = PassengerBook.from_yaml(TRAVELLERS)
    store = TripStore(led, HOME, book=book)
    f = fl(7, "2028-02-05", None, {**FAR, "iata": None}, MSP, None, None, None)
    log = HeldLog(led, [f])
    rep = bf.backfill(led, store, log, since=f["date"], me_name=ME, apply=True)
    ti = store.find(rep.filed[0][0])
    assert (ti.payload["flight_code"], ti.payload["origin"]) == ("", "")
    cancelled = store.set_status(ti.id, "cancelled")
    assert cancelled.sync_state == "pending"
    syncer = Syncer(store=store, ledger=led, book=book, client=log, refs=None, dry_run=False)
    assert syncer.run_pass().done == {"update": 1}
    after = store.item(ti.id)
    assert (after.sync_hold, after.airtrail_flight_id, after.sync_state) == (None, 7, "synced")
    (record,) = log.flights
    assert record["note"].startswith("CANCELLED ")


def test_a_backfill_of_300_flights_asks_for_three_pages():
    """The flight log's list is read a page of 100 at a time, following its cursor, through the
    client itself. (How long it takes is printed, not asserted.)"""
    import json
    import time

    import httpx2

    base = chain()[0]
    first = dt.date.fromisoformat(base["date"])
    flights = []
    for n in range(300):
        day = (first - dt.timedelta(days=n)).isoformat()
        number = str(int(base["flightNumber"][2:]) - n)
        flights.append(
            fl(n + 1, day, base["flightNumber"][:2] + number, MSP, SEA, None, None, f"P{n:05d}")
        )
    pages: list[str | None] = []

    def answer(request):
        if request.url.path == "/api/v1/me":
            body = {"data": {"id": OWNER.user_id, "isOwner": True, "permissions": []}}
            return httpx2.Response(200, json=body)
        assert request.url.path == "/api/v1/flights"
        assert request.url.params["limit"] == "100"
        cursor = request.url.params.get("cursor")
        pages.append(cursor)
        at = int(cursor or 0)
        nxt = str(at + 100) if at + 100 < len(flights) else None
        body = {"data": flights[at : at + 100], "page": {"nextCursor": nxt}}
        return httpx2.Response(200, content=json.dumps(body))

    client = AirTrailClient(
        "http://flightlog.example", "k", transport=httpx2.MockTransport(answer)
    )
    led, store = fresh()
    started = time.perf_counter()
    rep = bf.backfill(led, store, client, since=flights[-1]["date"], me_name=ME, apply=True)
    took = time.perf_counter() - started
    print(f"backfill of {len(flights)} flights: {took:.2f} s")
    assert pages == [None, "100", "200"]
    assert (rep.fetched, rep.in_window, len(rep.filed)) == (300, 300, 300)


def test_a_record_that_is_not_the_owners_is_never_filed_or_linked():
    """The backfill reads only the key's user's flights: a record that lists passengers, none of
    them that user, is not filed; and a booking linked to a record that no longer lists them is
    unlinked, not relinked to it."""
    led, store = fresh()
    theirs = chain()[0]
    theirs["passengers"] = [{**theirs["passengers"][0], "user": {"id": "another-user"}}]
    rep = run(led, store, [theirs, *chain()[1:]])
    assert rep.fetched == 4 and rep.in_window == 3 and len(rep.filed) == 3
    assert store.find(bf.flight_item(chain()[0], ME).key) is None
    # the owner's flight there, filed and linked, then someone else's record under its id
    led, store = fresh()
    run(led, store, chain())
    first = store.find(bf.flight_item(chain()[0], ME).key)
    rep = run(led, store, [theirs, *chain()[1:]])
    assert rep.absent == [(first.id, first.item_key, theirs["id"])] and not rep.relinked
    after = store.item(first.id)
    assert (after.airtrail_flight_id, after.sync_hold) == (None, "deleted")
