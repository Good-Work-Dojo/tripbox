# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The sync against a real AirTrail, one scenario per test: the throwaway stock instance the
end-to-end stack starts (`helpers.e2e` says what it hands over), one instance for the module, a new
ledger per test, each test its own flight number and day. Skipped as `test_flightlog_e2e.py` is.
Users are told by id alone and no record is shown whole; every booking is the invented family's,
every number begins with 98 and every day is in 2027. The clock is held, for the whole module, on
a day before every flight here (`held_clock`), so no case turns on the day it runs."""

import contextlib
import datetime as dt
import logging
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers.e2e import (
    CLOSED,
    FULL_KEY,
    HAND_KEY,
    KEY,
    NARROW_KEY,
    NOT_HERE,
    OTHER_KEY,
    URL,
    absent,
    as_truth,
    delete_flight,
    file_corpus,
    flight_log_truth,
    held,
    ingest,
    listed,
    own_flights,
    passenger_user_ids,
    passes,
    refile_corpus,
    set_up,
    use_flight_log,
    wait_until_ready,
)
from helpers.e2e import client as new_client

from tripbox import cli, util
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.pipeline.run import build_context
from tripbox.trips.store import TripItem, TripStore
from tripbox.util import parse_iso
from tripbox.validate import checks
from tripbox.write.airtrail import (
    AirTrailClient,
    AirTrailError,
    AirTrailRefs,
    flight_to_input,
    missing_scope,
)
from tripbox.write.sync import ALERT_AFTER, BACKOFF_FIRST

pytestmark = [pytest.mark.uses_network, pytest.mark.skipif(absent(), reason=NOT_HERE)]

ME = "Alex Sample"
SAM = "Sam Sample"
ROBIN = "Robin Sample"
CORPUS_FLIGHT_LOG = {"TRIPBOX_AIRTRAIL_DRY_RUN": "false"}
DELETES = ("flight.delete.own", "flight.delete.any")
REACHES_ANY = ("flight.read.any", "flight.update.any", "flight.passengers.manage.any")
HELD = dt.datetime(2027, 1, 1, 18, 0, tzinfo=dt.UTC)  # noon in the settings' zone


@pytest.fixture(scope="module", autouse=True)
def held_clock():
    """The clock every case runs on: held on a day before every flight here, so a flight is
    never one the switch holds as past because of the day the module runs."""
    with util.clock_at(HELD):
        yield HELD


# ----------------------------------------------------------------------------- the instance
@pytest.fixture(scope="module", autouse=True)
def users():
    """Every key answers, and whose each is: the first four keys one user's, the other key
    another's. A hand key whose role may not delete its own flights, or a full-access key whose
    user's role reaches no flight of another's, fails the module here."""
    keys = {
        "app": KEY,
        "hand": HAND_KEY,
        "narrow": NARROW_KEY,
        "full": FULL_KEY,
        "other": OTHER_KEY,
    }
    # the names of the keys not given, never the keys themselves
    missing = [name for name, key in keys.items() if not key]
    assert URL and missing == [], "the address and all five keys come together"
    profiles = {}
    for name, key in keys.items():
        wait_until_ready(key)
        c = new_client(key)
        profiles[name] = c.profile()
        c.close()
    hand = profiles["hand"]
    if not (hand.is_owner or set(DELETES) & hand.permissions):
        pytest.fail(
            "the hand key's user may not delete their own flights: give the keys to a user"
            " whose role may",
            pytrace=False,
        )
    full = profiles["full"]
    if not (full.is_owner or set(REACHES_ANY) <= full.permissions):
        pytest.fail(
            "the full-access key's user may not reach other people's flights: give the keys to"
            " the instance's owner",
            pytrace=False,
        )
    owner = profiles["app"].user_id
    assert profiles["app"].is_owner, "the keys go to the instance's owner"
    assert {profiles[n].user_id for n in ("app", "hand", "narrow", "full")} == {owner}
    assert profiles["other"].user_id != owner
    return SimpleNamespace(owner=owner, other=profiles["other"].user_id)


@pytest.fixture(scope="module")
def app():
    c = new_client(KEY)
    yield c
    c.close()


@pytest.fixture(scope="module")
def hand():
    c = new_client(HAND_KEY)
    yield c
    c.close()


@pytest.fixture(scope="module")
def other():
    c = new_client(OTHER_KEY)
    yield c
    c.close()


@pytest.fixture
def answers(tmp_path, monkeypatch):
    """A new install's settings with the flight log on, through the app key."""
    return set_up(tmp_path, monkeypatch)


# ----------------------------------------------------------------------------- reading the ledger
@contextlib.contextmanager
def store():
    """The trip store of the ledger the process's settings name."""
    s = Settings.from_env()
    led = Ledger.open(s.ledger_path)
    try:
        yield TripStore(led, s.home_airports, book=s.travellers)
    finally:
        led.close()


def booking(number: str, day: str) -> TripItem:
    """The booking of flight DL `number` from MSP on `day`."""
    with store() as st:
        ti = st.item_by_key(f"DL|{number}|{day}|MSP")
    assert ti is not None, (number, day)
    return ti


def note_lines(flight: dict) -> list[str]:
    return str(flight.get("note") or "").splitlines()


def without(record: dict, *fields: str) -> dict:
    return {k: v for k, v in record.items() if k not in fields}


TIMES = ("departure", "arrival", "departureScheduled", "arrivalScheduled")


def sent_for(c: Counter) -> int:
    """How many requests of a pass would change the flight log."""
    return c["POST"] + c["PUT"] + c["DELETE"]


def requests(c: Counter) -> int:
    return c["GET"] + sent_for(c)


def by_hand(c: AirTrailClient, day: str, number: str, user: str, **fields) -> int:
    """A flight logged in AirTrail by hand under the key of `c`: DL `number` MSP to BOS on `day`,
    08:00 to 11:50 local, `user` aboard; `fields` added. Its id."""
    refs = AirTrailRefs(c, None)
    dep = dt.datetime.fromisoformat(f"{day}T08:00-06:00").astimezone(dt.UTC)
    arr = dt.datetime.fromisoformat(f"{day}T11:50-05:00").astimezone(dt.UTC)
    body = {
        "date": day,
        "departure": dep.isoformat(),
        "arrival": arr.isoformat(),
        "departureScheduled": dep.isoformat(),
        "arrivalScheduled": arr.isoformat(),
        "duration": int((arr - dep).total_seconds()),
        "flightNumber": f"DL{number}",
        "fromId": refs.airport_id("MSP"),
        "toId": refs.airport_id("BOS"),
        "airlineId": refs.airline_id("DL"),
        "passengers": [{"userId": user}],
        **fields,
    }
    return c.create_flight(body)


# ----------------------------------------------------------------------------- the matrix
def test_an_update_sends_only_what_changed(tmp_path, answers, app):
    b = dict(passengers=[ME], number="9801", code="QRS987", day="2027-01-11")
    ingest(tmp_path, answers, **b, message_id="<qrs987-1@example.com>")
    ti = booking("9801", "2027-01-11")
    fid = ti.airtrail_flight_id
    assert fid and ti.sync_state == "synced"
    before = app.get_flight(fid)

    ingest(tmp_path, answers, **b, departure="09:20", message_id="<qrs987-2@example.com>")
    ti = booking("9801", "2027-01-11")
    assert (ti.airtrail_flight_id, ti.sync_state) == (fid, "synced")
    after = app.get_flight(fid)
    assert parse_iso(after["departure"]) == parse_iso(ti.payload["dep_utc"])
    assert parse_iso(after["arrival"]) == parse_iso(ti.payload["arr_utc"])
    assert parse_iso(after["departure"]) != parse_iso(before["departure"])
    # only the times moved, and the note gained one line naming them
    assert without(held(after), *TIMES) == without(held(before), *TIMES)
    added = note_lines(after)[len(note_lines(before)) :]
    assert note_lines(after)[: len(note_lines(before))] == note_lines(before)
    assert len(added) == 1 and added[0].startswith("changed ")
    assert requests(passes()) == 0


def test_what_the_owner_set_in_airtrail_by_hand_survives_an_update(
    tmp_path, answers, app, hand, users
):
    b = dict(passengers=[ME], number="9802", code="QST876", day="2027-01-12")
    ingest(tmp_path, answers, **b, message_id="<qst876-1@example.com>")
    fid = booking("9802", "2027-01-12").airtrail_flight_id
    typed = flight_to_input(hand.get_flight(fid))
    typed["aircraftReg"] = "N980SX"
    typed["departureGate"] = "B7"
    for row in typed["passengers"]:
        if row.get("userId") == users.owner:
            row["seat"] = "window"
    typed["note"] = f"{typed['note']}\nAsked for the window seat by phone"
    hand.update_flight(fid, typed)

    ingest(tmp_path, answers, **b, departure="10:05", message_id="<qst876-2@example.com>")
    ti = booking("9802", "2027-01-12")
    assert (ti.airtrail_flight_id, ti.sync_state) == (fid, "synced")
    after = app.get_flight(fid)
    assert parse_iso(after["departure"]) == parse_iso(ti.payload["dep_utc"])
    assert (after["aircraftReg"], after["departureGate"]) == ("N980SX", "B7")
    (mine,) = [p for p in after["passengers"] if (p.get("user") or {}).get("id") == users.owner]
    assert mine["seat"] == "window"
    assert "Asked for the window seat by phone" in note_lines(after)
    assert note_lines(after)[-1].startswith("changed ")


def test_an_adopted_flight_with_nothing_to_change_sends_no_request(tmp_path, answers, app):
    ingest(
        tmp_path, answers, passengers=[ME], number="9803", code="QTU765", day="2027-01-13",
        message_id="<qtu765@example.com>",
    )
    ti = booking("9803", "2027-01-13")
    fid = ti.airtrail_flight_id
    before = app.get_flight(fid)
    with store() as st:  # the row forgets what it told the flight log, and keeps the flight
        assert st.sync_done(ti.id, flight_id=fid, told=None).sync_state == "pending"
    c = passes()
    # each field read back is the one sent: dates and times come back as the same instants
    assert c["adopt"] == 1 and c["update"] == 0 and sent_for(c) == 0
    ti = booking("9803", "2027-01-13")
    assert (ti.airtrail_flight_id, ti.sync_state) == (fid, "synced")
    assert app.get_flight(fid) == before


def test_companions_arrive_as_guests_with_their_seats_and_class(tmp_path, answers, app, users):
    seats = {ME: "14A", SAM: "14B", ROBIN: "14C"}
    ingest(
        tmp_path, answers, passengers=[ME, SAM, ROBIN], number="9804", code="QUV654",
        day="2027-01-14", message_id="<quv654@example.com>", seats=seats, cabin="Economy",
    )
    ti = booking("9804", "2027-01-14")
    assert ti.sync_state == "synced"
    flight = app.get_flight(ti.airtrail_flight_id)
    rows = flight["passengers"]
    assert passenger_user_ids(flight).count(users.owner) == 1
    assert sorted(p["guestName"] for p in rows if not p.get("user")) == [ROBIN, SAM]
    by_name = {ME if p.get("user") else p["guestName"]: p for p in rows}
    assert {name: p["seatNumber"] for name, p in by_name.items()} == seats
    assert {p["seatClass"] for p in rows} == {"economy"}


def test_a_cancellation_is_a_note_and_the_flight_stays(tmp_path, answers, app):
    b = dict(passengers=[ME], number="9805", code="QVW543", day="2027-01-15")
    ingest(tmp_path, answers, **b, message_id="<qvw543-1@example.com>")
    fid = booking("9805", "2027-01-15").airtrail_flight_id
    ingest(tmp_path, answers, **b, status="cancelled", message_id="<qvw543-2@example.com>")
    ti = booking("9805", "2027-01-15")
    assert (ti.status, ti.sync_state, ti.airtrail_flight_id) == ("cancelled", "synced", fid)
    assert fid in own_flights(app)
    assert note_lines(app.get_flight(fid))[0].startswith("CANCELLED ")
    c = passes()
    assert requests(c) == 0 and "detach" not in c


def test_a_family_members_receipt_first_sends_nothing_until_the_owners_arrives(
    tmp_path, answers, app, users
):
    b = dict(number="9806", code="QWX432", day="2027-01-18")
    ingest(tmp_path, answers, passengers=[SAM], **b, message_id="<qwx432-1@example.com>")
    assert booking("9806", "2027-01-18").sync_state == "off"
    assert listed(app, "DL9806", "2027-01-18") == []
    ingest(tmp_path, answers, passengers=[ME], **b, message_id="<qwx432-2@example.com>")
    ti = booking("9806", "2027-01-18")
    assert ti.sync_state == "synced"
    assert listed(app, "DL9806", "2027-01-18") == [ti.airtrail_flight_id]
    flight = app.get_flight(ti.airtrail_flight_id)
    assert passenger_user_ids(flight).count(users.owner) == 1
    assert [p["guestName"] for p in flight["passengers"] if not p.get("user")] == [SAM]


def test_a_flight_deleted_by_hand_is_unlinked_and_sent_again_only_on_request(
    tmp_path, answers, app, hand, caplog
):
    b = dict(passengers=[ME], number="9807", code="QXY321", day="2027-01-19")
    ingest(tmp_path, answers, **b, message_id="<qxy321-1@example.com>")
    ti = booking("9807", "2027-01-19")
    fid = ti.airtrail_flight_id
    delete_flight(hand, fid)
    caplog.set_level(logging.INFO, logger="tripbox.sync")
    ingest(tmp_path, answers, **b, departure="07:45", message_id="<qxy321-2@example.com>")
    assert f"sync: trip item {ti.id}: detach" in caplog.text
    ti = booking("9807", "2027-01-19")
    assert (ti.airtrail_flight_id, ti.told, ti.sync_hold) == (None, None, "deleted")
    assert "send_again" in checks.actions(ti.checks, hold=ti.sync_hold, flight_log=True)
    c = passes(times=2)
    assert sent_for(c) == 0 and listed(app, "DL9807", "2027-01-19") == []
    with store() as st:
        st.send_again(ti.id)
    c = passes()
    assert c["create"] == 1
    (again,) = listed(app, "DL9807", "2027-01-19")
    assert again != fid and booking("9807", "2027-01-19").airtrail_flight_id == again


@pytest.mark.parametrize(
    ("key", "numbers"),
    [(KEY, ("9808", "9809")), (FULL_KEY, ("9810", "9811"))],
    ids=["the app key", "a full-access key"],
)
def test_a_flight_the_owner_left_in_airtrail_does_not_stop_the_flights_behind_it(
    tmp_path, monkeypatch, app, hand, users, key, numbers
):
    left, behind = numbers
    answers = set_up(tmp_path, monkeypatch, key=key)
    first = dict(passengers=[ME], number=left, code="RQS210", day="2027-01-20")
    ingest(tmp_path, answers, **first, message_id=f"<{left}-1@example.com>")
    fid = booking(left, "2027-01-20").airtrail_flight_id
    typed = flight_to_input(hand.get_flight(fid))  # the owner's own row becomes a guest's
    typed["passengers"] = [{"guestName": ME}]
    hand.update_flight(fid, typed)
    full = new_client(FULL_KEY)  # the owner's role reaches the flight they left
    left_there = full.get_flight(fid)
    # a change to that flight and a new one, filed while no flight log is kept: one pass meets
    # both, the changed one first
    use_flight_log(monkeypatch, key=None)
    ingest(tmp_path, answers, **first, departure="09:35", message_id=f"<{left}-2@example.com>")
    ingest(
        tmp_path, answers, passengers=[ME], number=behind, code="RQT321", day="2027-01-21",
        message_id=f"<{behind}@example.com>",
    )
    use_flight_log(monkeypatch, key=key)
    alerts: list[str] = []
    c = passes(alerts=alerts)
    assert (c["detach"], c["create"], c["PUT"], alerts) == (1, 1, 0, [])
    try:  # nothing was written to the flight left
        after = full.get_flight(fid)
    finally:
        full.close()
    assert (held(after), after["note"]) == (held(left_there), left_there["note"])
    assert passenger_user_ids(after) == [None]
    gone = booking(left, "2027-01-20")
    assert (gone.airtrail_flight_id, gone.sync_hold) == (None, "deleted")
    sent = booking(behind, "2027-01-21")
    assert sent.sync_state == "synced"
    assert listed(app, f"DL{behind}", "2027-01-21") == [sent.airtrail_flight_id]


def test_a_full_access_key_writes_only_to_the_owners_own_flights(
    tmp_path, monkeypatch, other, users
):
    answers = set_up(tmp_path, monkeypatch, key=FULL_KEY)
    b = dict(passengers=[ME], number="9812", code="RRS432", day="2027-01-22")
    ingest(tmp_path, answers, **b, message_id="<rrs432-1@example.com>")
    ti = booking("9812", "2027-01-22")
    full = new_client(FULL_KEY)
    try:
        theirs = flight_to_input(full.get_flight(ti.airtrail_flight_id))
    finally:
        full.close()
    # the other user's flight of the same number, day and origin, whose id the ledger is made
    # to hold
    theirs["passengers"] = [{"userId": users.other}]
    theirs["note"] = "The other user's own"
    oid = other.create_flight(theirs)
    before = other.get_flight(oid)
    with store() as st:
        st.sync_done(ti.id, flight_id=oid, told=ti.told)
    use_flight_log(monkeypatch, key=None)
    ingest(tmp_path, answers, **b, departure="11:15", message_id="<rrs432-2@example.com>")
    use_flight_log(monkeypatch, key=FULL_KEY)
    # the full key reads the other user's record: the pass unlinks it by who is aboard
    # (`Syncer._off_it`), not by a refusal, which would have asked the profile once more
    full = new_client(FULL_KEY)
    try:
        assert full.get_flight(oid)["id"] == oid
    finally:
        full.close()
    c = passes()
    assert (c["detach"], sent_for(c), c["GET"]) == (1, 0, 2)
    assert booking("9812", "2027-01-22").sync_hold == "deleted"
    after = other.get_flight(oid)
    assert (held(after), after["note"]) == (held(before), before["note"])


def test_a_flight_the_owner_logged_by_hand_is_adopted_not_twinned(
    tmp_path, answers, app, hand, users
):
    hid = by_hand(hand, "2027-01-25", "9813", users.owner, note="Typed in by hand")
    typed = app.get_flight(hid)
    ingest(
        tmp_path, answers, passengers=[ME], number="9813", code="RST543", day="2027-01-25",
        message_id="<rst543@example.com>",
    )
    ti = booking("9813", "2027-01-25")
    assert (ti.airtrail_flight_id, ti.sync_state) == (hid, "synced")
    assert listed(app, "DL9813", "2027-01-25") == [hid]
    after = app.get_flight(hid)
    assert (held(after), after["note"]) == (held(typed), "Typed in by hand")
    assert requests(passes()) == 0


def test_switch_on_adopts_what_the_log_holds_sends_the_upcoming_and_holds_the_past(
    tmp_path, monkeypatch, app, hand, users, capsys
):
    answers = set_up(tmp_path, monkeypatch, key=None)
    today = dt.datetime(2027, 2, 15, 12, 0, tzinfo=dt.UTC)
    hid = by_hand(hand, "2027-03-03", "9817", users.owner, note="Typed in by hand")
    with util.clock_at(today):
        for number, day, who, code in (
            ("9814", "2027-02-01", [ME], "RTU654"),  # left before the switch
            ("9815", "2027-03-01", [ME], "RTV765"),  # to come
            ("9816", "2027-03-02", [SAM], "RTW876"),  # a companion's alone
            ("9817", "2027-03-03", [ME], "RTX987"),  # to come, already in the flight log
        ):
            ingest(tmp_path, answers, passengers=who, number=number, code=code, day=day,
                   message_id=f"<{code.lower()}@example.com>")
        use_flight_log(monkeypatch)
        c = passes()
        assert (c["create"], c["adopt"]) == (1, 1)
        past = booking("9814", "2027-02-01")
        assert (past.sync_hold, past.sync_state) == ("past", "off")
        assert len(listed(app, "DL9815", "2027-03-01")) == 1
        assert listed(app, "DL9817", "2027-03-03") == [hid]
        assert listed(app, "DL9814") == listed(app, "DL9816") == []
        capsys.readouterr()
        assert cli.main(["airtrail", "enroll", "--since", "2027-02-01"]) == 0
        assert capsys.readouterr().out.splitlines()[-1] == (
            "enroll: 1 past flights since 2027-02-01 would be queued; run again with --apply"
        )
        assert cli.main(["airtrail", "enroll", "--since", "2027-02-01", "--apply"]) == 0
        assert capsys.readouterr().out.splitlines()[-1] == (
            "enroll: 1 past flights since 2027-02-01 queued"
        )
        c = passes(times=2)
        assert (c["create"], sent_for(c)) == (1, 1)
    assert len(listed(app, "DL9814", "2027-02-01")) == 1
    assert len(listed(app, "DL9815", "2027-03-01")) == 1
    assert listed(app, "DL9816") == []


def test_an_unreachable_flight_log_backs_off_alerts_once_and_recovers(
    tmp_path, monkeypatch, app
):
    answers = set_up(tmp_path, monkeypatch, key=None)
    ingest(
        tmp_path, answers, passengers=[ME], number="9818", code="RUV987", day="2027-01-26",
        message_id="<ruv987@example.com>",
    )
    alerts: list[str] = []
    down = (
        "Your flight log has not answered for over an hour; flights waiting for it are sent"
        " once it answers again."
    )
    t0 = util.now().replace(microsecond=0)
    hour = dt.timedelta(seconds=ALERT_AFTER)
    # a worker that starts while the flight log is down reaches nothing, and no row is touched;
    # each worker started afresh while it stays down counts from when it stopped answering, so
    # the one started an hour after that tells the owner, once
    use_flight_log(monkeypatch, url=CLOSED)
    for at, told in (
        (t0, 0),
        (t0 + hour - dt.timedelta(seconds=1), 0),
        (t0 + hour, 1),
        (t0 + 2 * hour, 1),
    ):
        with util.clock_at(at):
            c = passes(alerts=alerts, now=at)
        assert (c["outage"], sent_for(c), len(alerts)) == (1, 0, told), at - t0
    assert alerts == [down]
    ti = booking("9818", "2027-01-26")
    assert (ti.sync_attempts, ti.sync_after, ti.sync_state) == (0, None, "pending")
    # a worker that read its key's profile while the flight log answered, which then stops: the
    # flight backs off, and the owner, told already, is told nothing more
    use_flight_log(monkeypatch)
    ctx = build_context(Settings.from_env(), with_model=False, with_notifier=False)
    assert ctx.syncer is not None and ctx.airtrail is not None
    ctx.syncer.alert = alerts.append
    ctx.airtrail.profile()
    ctx.airtrail.http.base_url = CLOSED
    waits: list[int] = []
    at = t0 + 2 * hour
    try:
        # each pass at the time the flight's backoff ends
        for _ in range(4):
            with util.clock_at(at):
                report = ctx.syncer.run_pass(now=at)
                ti = ctx.trips.item(ti.id)
            assert report.done == {"outage": 1}, report
            nxt = parse_iso(ti.sync_after)
            waits.append(int((nxt - at).total_seconds()))
            soon = at + dt.timedelta(seconds=1)
            with util.clock_at(soon):  # not due before its time: nothing is asked
                assert ctx.syncer.run_pass(now=soon).done == {}
            at = nxt
        assert waits[:2] == [BACKOFF_FIRST, 2 * BACKOFF_FIRST]
        assert alerts == [down]
        # it answers again: the flight is sent once, and nobody is told anything more
        ctx.airtrail.http.base_url = URL
        with util.clock_at(at):
            assert ctx.syncer.run_pass(now=at).done == {"create": 1}
    finally:
        ctx.airtrail.close()
        ctx.ledger.close()
    assert len(alerts) == 1
    ti = booking("9818", "2027-01-26")
    assert listed(app, "DL9818", "2027-01-26") == [ti.airtrail_flight_id]


def test_a_refused_key_stops_the_pass_and_alerts_once(tmp_path, monkeypatch, app):
    answers = set_up(tmp_path, monkeypatch, key=None)
    ingest(
        tmp_path, answers, passengers=[ME], number="9819", code="RVW210", day="2027-01-27",
        message_id="<rvw210@example.com>",
    )
    use_flight_log(monkeypatch, key="Q" * 43)  # a key AirTrail never saw
    alerts: list[str] = []
    c = passes(alerts=alerts, times=2)
    assert (c["stop"], sent_for(c)) == (2, 0)
    assert alerts == [
        "Your flight log rejected the key tripbox uses (HTTP 401); nothing is sent to it until"
        " the key works."
    ]
    ti = booking("9819", "2027-01-27")
    untouched = (ti.airtrail_flight_id, ti.told, ti.sync_error, ti.sync_attempts, ti.sync_after)
    assert untouched == (None, None, None, 0, None) and ti.sync_state == "pending"
    use_flight_log(monkeypatch)
    assert passes(alerts=alerts)["create"] == 1 and len(alerts) == 1
    assert listed(app, "DL9819", "2027-01-27") == [booking("9819", "2027-01-27").airtrail_flight_id]


def test_a_key_without_the_passengers_scope_sends_the_owners_flight_and_stops_at_a_family_flight(
    tmp_path, monkeypatch, app
):
    answers = set_up(tmp_path, monkeypatch, key=None)
    ingest(
        tmp_path, answers, passengers=[ME], number="9820", code="RWX321", day="2027-01-28",
        message_id="<rwx321@example.com>",
    )
    ingest(
        tmp_path, answers, passengers=[ME, SAM], number="9821", code="RWY432", day="2027-01-28",
        message_id="<rwy432@example.com>",
    )
    use_flight_log(monkeypatch, key=NARROW_KEY)
    alerts: list[str] = []
    c = passes(alerts=alerts)
    assert (c["create"], c["stop"], c["POST"]) == (1, 1, 2)
    assert alerts == [
        "The key tripbox uses for your flight log lacks the scope flight.passengers.manage.own;"
        " nothing is sent until a key with all six is set (tripbox check lists what a key lacks)."
    ]
    assert listed(app, "DL9820", "2027-01-28") == [booking("9820", "2027-01-28").airtrail_flight_id]
    assert listed(app, "DL9821") == []
    family = booking("9821", "2027-01-28")
    assert (family.airtrail_flight_id, family.sync_state) == (None, "pending")


def test_another_users_key_stops_the_pass_and_writes_nothing(
    tmp_path, monkeypatch, answers, app, other
):
    b = dict(passengers=[ME], number="9822", code="RXY543", day="2027-01-29")
    ingest(tmp_path, answers, **b, message_id="<rxy543-1@example.com>")
    fid = booking("9822", "2027-01-29").airtrail_flight_id
    mine = app.get_flight(fid)
    theirs = {i: (held(f), f.get("note")) for i, f in own_flights(other).items()}
    use_flight_log(monkeypatch, key=None)
    ingest(tmp_path, answers, **b, departure="12:20", message_id="<rxy543-2@example.com>")
    use_flight_log(monkeypatch, key=OTHER_KEY)
    alerts: list[str] = []
    c = passes(alerts=alerts, times=2)
    assert (c["other"], sent_for(c)) == (2, 0)
    assert alerts == [
        "The flight log tripbox reaches now is not the one its flights were sent to (another"
        " AirTrail, or another user's key). Nothing is sent. If this is the flight log to keep,"
        " run: tripbox airtrail forget --apply"
    ]
    assert {i: (held(f), f.get("note")) for i, f in own_flights(other).items()} == theirs
    assert app.get_flight(fid) == mine
    ti = booking("9822", "2027-01-29")
    assert (ti.airtrail_flight_id, ti.sync_state) == (fid, "pending")


def test_the_apps_key_asking_for_another_users_flight_is_refused_for_flight_read_any(
    app, other, users
):
    """What the sync reads as a flight that is no longer the owner's: the app's key, which holds
    the own-flights scopes alone, asking for a flight only another user is on is refused for the
    any-flights scope, and that flight is left as it was."""
    oid = by_hand(other, "2027-01-29", "9822", users.other, note="The other user's own")
    before = other.get_flight(oid)
    with pytest.raises(AirTrailError) as refused:
        app.get_flight(oid)
    assert (refused.value.status, refused.value.code) == (403, "insufficient_scope")
    assert missing_scope(refused.value) == "flight.read.any"
    after = other.get_flight(oid)
    assert (held(after), after["note"]) == (held(before), before["note"])


def test_the_corpus_reaches_a_real_airtrail_as_its_truth_says_and_twice_adds_nothing(
    tmp_path, app, users
):
    before = set(own_flights(app))
    environ = {"TRIPBOX_AIRTRAIL_URL": URL, "TRIPBOX_AIRTRAIL_API_KEY": KEY, **CORPUS_FLIGHT_LOG}
    ctx = file_corpus(tmp_path, environ)
    try:
        new = {i: f for i, f in own_flights(app).items() if i not in before}
        got = sorted((as_truth(f, users.owner) for f in new.values()),
                     key=lambda f: (f["date"], f["flight"]))
        assert got == flight_log_truth()
        assert all(users.owner in passenger_user_ids(f) for f in new.values())
        kept = {i: held(f) for i, f in new.items()}
        refile_corpus(ctx)
    finally:
        if ctx.airtrail is not None:
            ctx.airtrail.close()
        ctx.ledger.close()
    again = {i: f for i, f in own_flights(app).items() if i not in before}
    assert {i: held(f) for i, f in again.items()} == kept


# ----------------------------------------------------------------------------- two known limits
def test_a_change_that_names_only_a_companion_moves_the_owners_flight_there_too(
    tmp_path, answers, app, users
):
    """A change under the booking's one code that names Sam alone moves the whole booking, the
    owner's flight in the flight log with it (docs/airtrail.md, "Known limits"). Put back on its
    day with Sam taken off on the page, the flight log follows."""
    b = dict(number="9823", code="RYZ654")
    ingest(tmp_path, answers, passengers=[ME, SAM], **b, day="2027-02-08",
           message_id="<ryz654-1@example.com>")
    fid = booking("9823", "2027-02-08").airtrail_flight_id
    ingest(tmp_path, answers, passengers=[SAM], **b, day="2027-02-09", document_type="change",
           message_id="<ryz654-2@example.com>")
    moved = booking("9823", "2027-02-09")
    assert sorted(moved.passengers) == [ME, SAM] and moved.airtrail_flight_id == fid
    there = app.get_flight(fid)
    assert as_truth(there, users.owner)["date"] == "2027-02-09"
    assert as_truth(there, users.owner)["people"] == ["me", SAM]
    with store() as st:
        st.edit_item(
            moved.id,
            {"dep_local": "2027-02-08T08:20", "arr_local": "2027-02-08T12:10", "passengers": [ME]},
        )
    assert passes()["update"] == 1
    back = as_truth(app.get_flight(fid), users.owner)
    assert (back["date"], back["people"], back["cancelled"]) == ("2027-02-08", ["me"], False)
    assert listed(app, "DL9823") == [fid]


def test_a_change_mailed_per_code_leaves_the_old_flight_until_it_is_cancelled_on_the_page(
    tmp_path, answers, app, users
):
    """The owner and Sam booked apart on one flight (one booking holding both codes); the
    airline mails the move per code, Sam's notice first. Sam's is filed on its own, the owner's
    joins it there, and the old booking stays live beside it: the flight log holds both flights
    (docs/airtrail.md, "Known limits"). Cancelled on the page, the old one gets the cancellation
    line there."""
    ingest(tmp_path, answers, passengers=[ME], number="9824", code="RZY765", day="2027-02-15",
           message_id="<rzy765-1@example.com>")
    ingest(tmp_path, answers, passengers=[SAM], number="9824", code="RZW876", day="2027-02-15",
           message_id="<rzw876-1@example.com>")
    old = booking("9824", "2027-02-15")
    assert sorted(old.passengers) == [ME, SAM] and old.sync_state == "synced"
    ingest(tmp_path, answers, passengers=[SAM], number="9824", code="RZW876", day="2027-02-16",
           document_type="change", message_id="<rzw876-2@example.com>")
    ingest(tmp_path, answers, passengers=[ME], number="9824", code="RZY765", day="2027-02-16",
           document_type="change", message_id="<rzy765-2@example.com>")
    new = booking("9824", "2027-02-16")
    assert new.id != old.id and sorted(new.passengers) == [ME, SAM]
    assert booking("9824", "2027-02-15").status == "confirmed"
    both = {as_truth(app.get_flight(i), users.owner)["date"]: i for i in listed(app, "DL9824")}
    assert both == {"2027-02-15": old.airtrail_flight_id, "2027-02-16": new.airtrail_flight_id}
    with store() as st:
        st.set_status(old.id, "cancelled")
    assert passes()["update"] == 1
    gone = as_truth(app.get_flight(old.airtrail_flight_id), users.owner)
    kept = as_truth(app.get_flight(new.airtrail_flight_id), users.owner)
    assert (gone["cancelled"], kept["cancelled"]) == (True, False)
    assert gone["people"] == kept["people"] == ["me", SAM]


def test_a_backfill_of_the_demo_world_files_trips_and_writes_nothing_back(
    tmp_path, monkeypatch, app, users, capsys
):
    """The owner's own flights there (the demo world's, and every case's before this one) filed
    into an empty ledger whose home airport is the one they leave from most: the three lines; a
    pass after it asks the flight log nothing; every flight there is as it was; and a second
    backfill files nothing."""
    from tripbox.validate.refdata import Airports
    from tripbox.write.sync import not_listed

    set_up(tmp_path, monkeypatch)
    before = {fid: (held(f), f.get("note")) for fid, f in own_flights(app).items()}
    assert before, "the owner has flights there"
    flights = list(own_flights(app).values())
    origins = Counter(
        code
        for f in flights
        if (code := str((f.get("from") or {}).get("iata") or "").upper())
        and Airports().airport(code) is not None
    )
    home, _ = origins.most_common(1)[0]
    monkeypatch.setenv("TRIPBOX_HOME_AIRPORTS", home)
    since = min(str(f.get("date"))[:10] for f in flights)
    window = [f for f in flights if not not_listed(f, users.owner)]
    shape = re.compile(
        r"backfill: (\d+) (would be filed|filed) into (\d+) trips \((\d+) new\);"
        r" (\d+) are already here; 0 relinked; 0 gone from the flight log"
    )
    capsys.readouterr()
    assert cli.main(["airtrail", "backfill", "--since", since]) == 0
    out = capsys.readouterr().out.splitlines()
    listed = f"{len(flights)} flights in the flight log, {len(window)} since {since}"
    assert out[0] == f"backfill: {listed}"
    m = shape.fullmatch(out[1])
    assert m and m[2] == "would be filed", out[1]
    filed, trips, new, here = int(m[1]), int(m[3]), int(m[4]), int(m[5])
    assert filed + here == len(window) and filed and 0 < new == trips <= filed
    assert out[2:] == ["backfill: nothing was changed; run again with --apply"]
    assert cli.main(["airtrail", "backfill", "--since", since, "--apply"]) == 0
    out = capsys.readouterr().out.splitlines()
    m = shape.fullmatch(out[1])
    assert len(out) == 2 and m and (m[2], int(m[1])) == ("filed", filed)
    assert requests(passes()) == 0
    after = {fid: (held(f), f.get("note")) for fid, f in own_flights(app).items()}
    assert after == before
    assert cli.main(["airtrail", "backfill", "--since", since, "--apply"]) == 0
    m = shape.fullmatch(capsys.readouterr().out.splitlines()[1])
    assert m and (int(m[1]), int(m[3]), int(m[5])) == (0, 0, len(window))


def test_a_fill_reaches_a_real_flight_and_leaves_what_it_holds(tmp_path, answers, app, hand):
    """Enrichment's write, through the syncer: on a flight sent there, the blanks are filled
    and what the record holds (a gate the owner set by hand, its times, who is aboard) stays; a
    second fill finds no blank and writes nothing."""
    when = HELD + dt.timedelta(days=70)  # a day, and a number, no other case here uses
    day, number = when.date().isoformat(), str(9800 + when.timetuple().tm_yday)
    ingest(
        tmp_path, answers, passengers=[ME], number=number, code="RFL432", day=day,
        message_id="<rfl432@example.com>",
    )
    ti = booking(number, day)
    fid = ti.airtrail_flight_id
    typed = flight_to_input(hand.get_flight(fid))
    typed["departureGate"] = "B7"
    hand.update_flight(fid, typed)
    before = app.get_flight(fid)
    assert not before.get("aircraftReg") and not before.get("arrivalGate")
    ctx = build_context(Settings.from_env(), with_model=False, with_notifier=False)
    try:
        assert ctx.syncer is not None
        fills = {"aircraftReg": "N98FL", "departureGate": "C1", "arrivalGate": "A3"}
        assert ctx.syncer.fill(ti.id, fills) == (
            "fill",
            {"aircraftReg": "N98FL", "arrivalGate": "A3"},
        )
        assert ctx.syncer.fill(ti.id, fills) == ("nothing", {})
    finally:
        ctx.airtrail.close()
        ctx.ledger.close()
    after = app.get_flight(fid)
    assert (after["aircraftReg"], after["departureGate"], after["arrivalGate"]) == (
        "N98FL",
        "B7",
        "A3",
    )
    kept = ("aircraftReg", "arrivalGate")
    assert without(held(after), *kept) == without(held(before), *kept)
    assert after["note"] == before["note"]
    assert booking(number, day).sync_state == "synced"


def test_every_case_runs_on_a_clock_held_before_its_flights(held_clock):
    assert util.now() == held_clock
    days = re.findall(r'"(2027-\d\d-\d\d)"', Path(__file__).read_text(encoding="utf-8"))
    assert days and all(dt.date.fromisoformat(d) > held_clock.date() for d in days)
