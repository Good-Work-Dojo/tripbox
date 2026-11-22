# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Against a real AirTrail: the throwaway stock instance the end-to-end stack starts with its own
database and removes again (`helpers.e2e` says what it hands over). Where neither its address nor
its key is set every test here is skipped; one without the other fails, so a stack that lost the
key never passes by skipping. The tests learn whose the key is from /api/v1/me and compare ids
alone, and no assertion here ever shows an AirTrail record whole, so no demo user's name can reach
a report. The model's answers are recorded ones written for the invented family, and kitinerary
finds nothing in a plain-text forward."""

import pytest
from helpers.e2e import (
    KEY,
    NOT_HERE,
    URL,
    absent,
    ingest,
    listed,
    passenger_user_ids,
    set_up,
    wait_until_ready,
)
from helpers.e2e import client as new_client

from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.trips.store import TripStore
from tripbox.util import parse_iso
from tripbox.write.airtrail import AirTrailRefs

pytestmark = [pytest.mark.uses_network, pytest.mark.skipif(absent(), reason=NOT_HERE)]


@pytest.fixture(scope="module", autouse=True)
def flightlog_ready():
    """Waits until AirTrail answers /api/v1/me with a 200 for the test key."""
    assert URL and KEY, "the address and the key come together"
    wait_until_ready()


@pytest.fixture
def client():
    c = new_client()
    yield c
    c.close()


@pytest.fixture
def answers(tmp_path, monkeypatch):
    """A new install's settings with AirTrail on; returns the directory of recorded answers."""
    return set_up(tmp_path, monkeypatch)


def test_an_owner_flight_reaches_airtrail_once(tmp_path, answers, client):
    booking = dict(passengers=["Alex Sample"], number="9123", code="QXR987", day="2027-03-12")
    ingest(tmp_path, answers, **booking, message_id="<qxr987@example.com>")
    s = Settings.from_env()
    assert s.on("airtrail")
    led = Ledger.open(s.ledger_path)
    # the message's pass is done once `ingest` returns: the booking holds the flight's id
    filed = TripStore(led, s.home_airports).item_by_key("DL|9123|2027-03-12|MSP")
    assert filed is not None
    (row,) = led.items.by_trip_item(filed.id)
    assert (row["outcome"], row["item_key"]) == ("filed", "DL|9123|2027-03-12|MSP")
    flight_id, state = filed.airtrail_flight_id, filed.sync_state
    assert flight_id and state == "synced"
    led.close()

    assert listed(client, "DL9123", "2027-03-12") == [flight_id]
    flight = client.get_flight(flight_id)
    # the flight log keeps a flight's length in seconds: its own departure to its own arrival
    length = parse_iso(flight["arrival"]) - parse_iso(flight["departure"])
    assert flight["duration"] == int(length.total_seconds()) > 0
    users = passenger_user_ids(flight)
    assert users == [client.user_id()]
    from_id = (flight.get("from") or {}).get("id")
    to_id = (flight.get("to") or {}).get("id")
    refs = AirTrailRefs(client, None)  # asked afresh, not read from the run's ledger
    msp, bos = refs.airport_id("MSP"), refs.airport_id("BOS")
    assert msp and bos and (from_id, to_id) == (msp, bos)

    # the same booking again under another Message-ID: filed once, still one flight in AirTrail
    ingest(tmp_path, answers, **booking, message_id="<qxr987-again@example.com>")
    assert listed(client, "DL9123", "2027-03-12") == [flight_id]


def test_a_flight_the_owner_is_not_on_never_reaches_airtrail(tmp_path, answers, client):
    ingest(
        tmp_path,
        answers,
        passengers=["Pat Example"],
        number="9124",
        code="RTW876",
        day="2027-03-19",
        message_id="<rtw876@example.com>",
    )
    s = Settings.from_env()
    assert s.on("airtrail")  # AirTrail is on, so the flight stays out of it for its passengers
    led = Ledger.open(s.ledger_path)
    store = TripStore(led, s.home_airports)
    filed = store.item_by_key("DL|9124|2027-03-19|MSP")
    assert filed is not None and filed.airtrail_flight_id is None
    assert filed.sync_state == "off" and filed.told is None
    assert [t.id for t in store.all_trips()] == [filed.trip_id]
    rows = [(r["outcome"], r["item_key"]) for r in led.items.by_trip_item(filed.id)]
    assert rows == [("filed", "DL|9124|2027-03-19|MSP")]
    led.close()
    assert listed(client, "DL9124") == []


def test_the_owner_is_the_api_keys_user(client):
    me = client.me()
    me_id = me.get("id") if isinstance(me, dict) else None
    owner = client.user_id()
    assert isinstance(owner, str) and owner and owner == me_id
