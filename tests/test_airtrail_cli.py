# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox airtrail backfill` and `tripbox airtrail status`: the owner's flights the flight log
holds filed as trips, and where the flight log stands, read from the ledger alone. The flight log
is one held in memory, answering as the Sample family's owner's key; every flight is invented and
dated in 2027."""

import copy
import datetime as dt
import json
import os

import pytest
from helpers.flightlog import Scripted
from helpers.sample import CONFIG_DIR

from tripbox import cli, util
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.pipeline import run as runmod
from tripbox.trips.store import TripStore
from tripbox.validate import checks
from tripbox.write.airtrail import REQUIRED_SCOPES, Profile

USER = "sampleowner01"
OWNER = Profile(USER, "apiKey", frozenset(REQUIRED_SCOPES), frozenset(), True)
HELD = dt.datetime(2027, 8, 2, 15, 0, tzinfo=dt.UTC)
SINCE = "2027-01-01"
AIRPORTS = {
    "MSP": {"id": 1, "iata": "MSP", "name": "MSP", "municipality": "Minneapolis",
            "timezone": "America/Chicago"},
    "DEN": {"id": 2, "iata": "DEN", "name": "DEN", "municipality": "Denver",
            "timezone": "America/Denver"},
    "PHL": {"id": 3, "iata": "PHL", "name": "PHL", "municipality": "Philadelphia",
            "timezone": "America/New_York"},
}


def record(fid: int, day: str, number: str, frm: str, to: str, dep: str, arr: str, pnr: str):
    """A flight as AirTrail's API answers it: the owner aboard, its PNR in its note."""
    return {
        "id": fid,
        "date": day,
        "departure": f"{day}T{dep}:00Z",
        "arrival": f"{day}T{arr}:00Z",
        "duration": None,
        "flightNumber": f"DL{number}",
        "aircraftReg": None,
        "note": f"PNR {pnr}",
        "from": AIRPORTS[frm],
        "to": AIRPORTS[to],
        "airline": {"id": 9, "name": "Delta Air Lines", "icao": "DAL", "iata": "DL"},
        "aircraft": None,
        "passengers": [{"id": fid, "user": {"id": USER}, "seatNumber": "14A", "seatClass": None}],
    }


TRIP = [
    record(1, "2027-03-02", "9201", "MSP", "DEN", "14:00", "16:10", "QAB123"),
    record(2, "2027-03-04", "9202", "DEN", "MSP", "18:00", "20:05", "QAB123"),
]
SIX = [  # one flight for each way a flight can stand with the flight log
    record(10 + n, f"2027-0{n + 4}-12", f"930{n}", "MSP", "PHL", "13:00", "15:35", f"QAC12{n}")
    for n in range(6)
]


class Log(Scripted):
    """The flight log, holding `flights` and answering as the owner's key."""

    def __init__(self, flights):
        super().__init__(None, profile=OWNER)
        self._flights = {int(f["id"]): copy.deepcopy(f) for f in flights}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    for name, value in {
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
        "TRIPBOX_HOME_AIRPORTS": "MSP",
        "TRIPBOX_AIRTRAIL_URL": "http://flightlog.example:3000",
        "TRIPBOX_AIRTRAIL_API_KEY": "sample-key",
        "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
        "TRIPBOX_AUTH": "none",
        "TZ": "America/Chicago",
    }.items():
        monkeypatch.setenv(name, value)
    with util.clock_at(HELD):
        yield tmp_path


def holding(monkeypatch, flights) -> Log:
    """From here on the flight log the settings name holds `flights`."""
    log = Log(flights)
    monkeypatch.setattr(runmod, "AirTrailClient", lambda url, key: log)
    return log


def run(capsys, *args: str) -> tuple[int, list[str], str]:
    code = cli.main(list(args))
    seen = capsys.readouterr()
    return code, seen.out.splitlines(), seen.err


def dump() -> list[str]:
    with Ledger.open(Settings.from_env().ledger_path) as led:
        return list(led.conn.iterdump())


def store_of(led: Ledger) -> TripStore:
    s = Settings.from_env()
    return TripStore(led, s.home_airports, book=s.travellers)


# ----------------------------------------------------------------------------- backfill
def test_backfill_prints_counts_and_changes_nothing_without_apply(env, monkeypatch, capsys):
    log = holding(monkeypatch, TRIP)
    assert run(capsys, "trips", "list")[0] == 0  # the ledger as any command leaves it
    before = dump()
    code, out, _ = run(capsys, "airtrail", "backfill", "--since", SINCE)
    assert code == 0
    assert out == [
        f"backfill: 2 flights in the flight log, 2 since {SINCE}",
        "backfill: 2 would be filed into 1 trips (1 new); 0 are already here; 0 relinked;"
        " 0 gone from the flight log",
        "backfill: nothing was changed; run again with --apply",
    ]
    assert dump() == before
    assert log.sent == []
    # with no day given it reads two years back from today
    code, out, _ = run(capsys, "airtrail", "backfill")
    assert out[0] == "backfill: 2 flights in the flight log, 2 since 2025-08-02"


def test_backfill_applied_names_trips_only_when_verbose(env, monkeypatch, capsys):
    log = holding(monkeypatch, TRIP)
    code, out, _ = run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")
    assert code == 0
    assert out == [
        f"backfill: 2 flights in the flight log, 2 since {SINCE}",
        "backfill: 2 filed into 1 trips (1 new); 0 are already here; 0 relinked;"
        " 0 gone from the flight log",
    ]
    assert log.sent == []
    # a second run files nothing; with --verbose it names no trip, as none was filed into
    code, out, _ = run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply", "--verbose")
    assert out[1] == (
        "backfill: 0 filed into 0 trips (0 new); 2 are already here; 0 relinked;"
        " 0 gone from the flight log"
    )
    assert len(out) == 2
    # into a new ledger, verbose: one line for the trip, its days and its flights
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(env / "data2"))
    code, out, _ = run(capsys, "airtrail", "backfill", "--since", SINCE, "--verbose")
    assert out[2:] == [
        "  Denver, 2027-03-02 to 2027-03-04: DL9201, DL9202",
        "backfill: nothing was changed; run again with --apply",
    ]
    for line in out:
        assert USER not in line and "flight|" not in line and "|" not in line


def test_the_old_command_is_gone(env, monkeypatch, capsys):
    holding(monkeypatch, TRIP)
    code, out, err = run(capsys, "trips", "backfill-airtrail")
    assert code == 2 and out == []
    assert err.strip() == "usage: tripbox trips list | text ID | feed-token [--rotate]"


def test_backfill_refuses_a_ledger_bound_to_another_user(env, monkeypatch, capsys):
    log = holding(monkeypatch, TRIP)
    assert run(capsys, "trips", "list")[0] == 0
    with Ledger.open(Settings.from_env().ledger_path) as led:
        led.kv.set("airtrail_user", "someone-else")
    before = dump()
    code, out, err = run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")
    assert code == 2 and out == []
    assert "tripbox airtrail forget --apply" in err
    assert dump() == before and log.sent == []


# ----------------------------------------------------------------------------- status
def six_ways(capsys) -> None:
    """The six flights filed, then each put where it stands: sent, waiting, to check, held as
    past, deleted there, and not the owner's (taken off it on the page)."""
    assert run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")[0] == 0
    with Ledger.open(Settings.from_env().ledger_path) as led:
        st = store_of(led)
        ids = [ti.id for ti in sorted(st.sent_flights(), key=lambda t: t.airtrail_flight_id)]
        synced, pending, blocked, past, deleted, theirs = ids
        assert st.item(synced).sync_state == "synced"
        st.sync_done(pending, flight_id=st.item(pending).airtrail_flight_id, told=None)
        st.sync_failed(blocked, code="refused", now=util.now(), retry_at=None)
        led.trip_items.update(past, sync_hold="past")
        st.settle(past)
        st.sync_detached(deleted)
        st.owner_answer(theirs, False)
        states = [st.item(i).sync_state for i in ids]
        assert states == ["synced", "pending", "blocked", "off", "off", "off"]


def test_status_counts_each_way_a_flight_stands(env, monkeypatch, capsys):
    holding(monkeypatch, SIX)
    six_ways(capsys)
    code, out, _ = run(capsys, "airtrail", "status")
    assert code == 0
    assert out == [
        "flight log: on",
        "flights: 1 sent, 1 waiting, 1 to check, 1 held as past, 1 deleted there,"
        " 1 not sent (not yours, or cancelled first)",
        "flights: 1 still name you in the flight log though you are off them (the trips page"
        " marks each)",
        "key: the six scopes, checked 2027-08-02 15:00 UTC",
    ]
    # the owner looked at the mark: it is not counted again
    with Ledger.open(Settings.from_env().ledger_path) as led:
        st = store_of(led)
        (marked,) = [t for t in st.sent_flights() if checks.STILL_LISTS in t.checks]
        st.looks_right(marked.id)
    key = "key: the six scopes, checked 2027-08-02 15:00 UTC"
    assert run(capsys, "airtrail", "status")[1][2] == key
    # a dry run, and each reason a pass stopped
    with Ledger.open(Settings.from_env().ledger_path) as led:
        for k, v in {
            "airtrail_dry_run": "on",
            "airtrail_down_since": "2027-08-01T09:30:00Z",
            "airtrail_key_rejected": "2027-08-01T10:00:00Z",
            "airtrail_key_problem": "scope:flight.passengers.manage.own",
        }.items():
            led.kv.set(k, v)
    out = run(capsys, "airtrail", "status")[1]
    assert out[0] == "flight log: on, dry run: nothing is sent"
    assert out[-3:] == [
        "trouble: not answering since 2027-08-01 09:30 UTC",
        "trouble: the key is refused since 2027-08-01 10:00 UTC",
        "trouble: the key lacks flight.passengers.manage.own",
    ]
    with Ledger.open(Settings.from_env().ledger_path) as led:
        led.kv.set("airtrail_key_problem", "other_log")
    assert run(capsys, "airtrail", "status")[1][-1] == (
        "trouble: another flight log is set up (tripbox airtrail forget)"
    )


def test_an_unreadable_flight_log_says_where_to_look(env, monkeypatch, capsys):
    """A backfill that cannot read the flight log says what happened and where the cause is
    told: `tripbox check`."""
    from tripbox.write.airtrail import AirTrailError

    for error, said in (
        (AirTrailError(503, "down"), "HTTP 503"),
        (AirTrailError(0, "ConnectError: refused"), "no answer"),
    ):
        log = holding(monkeypatch, TRIP)

        def unreadable(*args, error=error, **kwargs):
            raise error

        monkeypatch.setattr(log, "iter_flights", unreadable)
        code, out, err = run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")
        assert (code, out) == (1, [])
        assert err == (
            f"backfill: the flight log could not be read ({said}); tripbox check says why\n"
        )


def test_status_names_what_was_not_sent_and_prints_trouble_only_while_on(
    env, monkeypatch, capsys
):
    """The count of flights not sent says why a flight is not; a `trouble:` line is printed only
    while the flight log is kept: what an old stop left says nothing once it is off."""
    holding(monkeypatch, TRIP)
    assert run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")[0] == 0
    with Ledger.open(Settings.from_env().ledger_path) as led:
        led.kv.set("airtrail_down_since", "2027-08-01T09:30:00Z")
    out = run(capsys, "airtrail", "status")[1]
    assert out[1].endswith("0 not sent (not yours, or cancelled first)")
    assert out[-1] == "trouble: not answering since 2027-08-01 09:30 UTC"
    monkeypatch.delenv("TRIPBOX_AIRTRAIL_URL")
    monkeypatch.delenv("TRIPBOX_AIRTRAIL_API_KEY")
    with util.clock_at(HELD + dt.timedelta(days=1)):
        assert run(capsys, "trips", "list")[0] == 0  # the switch is noted as a context is built
    out = run(capsys, "airtrail", "status")[1]
    assert out[0].startswith("flight log: off since ")
    assert not any(line.startswith("trouble:") for line in out)


def test_forget_promises_only_the_owners_flights(env, monkeypatch, capsys):
    """After `forget --apply` the upcoming flights are looked for only when they are the
    owner's, and the past ones go only on request: the line says each."""
    holding(monkeypatch, TRIP)
    assert run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")[0] == 0
    code, out, _ = run(capsys, "airtrail", "forget", "--apply")
    assert code == 0
    assert out == [
        "forget: 2 flights unlinked; of the 0 upcoming, yours are looked for in the flight log"
        " now set up and added there if missing; 2 past are held back (tripbox airtrail enroll"
        " --since DAY --apply sends them)"
    ]


def test_status_says_when_the_flight_log_was_switched_off(env, monkeypatch, capsys):
    holding(monkeypatch, TRIP)
    assert run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")[0] == 0
    monkeypatch.delenv("TRIPBOX_AIRTRAIL_URL")
    monkeypatch.delenv("TRIPBOX_AIRTRAIL_API_KEY")
    with util.clock_at(HELD + dt.timedelta(days=1, minutes=7)):
        assert run(capsys, "trips", "list")[0] == 0  # the switch is noted as a context is built
    code, out, _ = run(capsys, "airtrail", "status")
    assert code == 0
    assert out == [
        "flight log: off since 2027-08-03 15:07 UTC",
        "flights: 2 hold a record there from when it was kept",
        "key: the six scopes, checked 2027-08-02 15:00 UTC",
    ]


def test_status_asks_no_one_and_prints_nothing_it_cannot_read_as_a_time_or_a_scope(
    env, monkeypatch, capsys
):
    holding(monkeypatch, TRIP)
    assert run(capsys, "airtrail", "status")[1] == [
        "flight log: off",
        "flights: 0 hold a record there from when it was kept",
        "key: not checked yet (tripbox check asks AirTrail)",
    ]
    assert run(capsys, "airtrail", "backfill", "--since", SINCE, "--apply")[0] == 0

    def no_one(*args, **kwargs):
        raise AssertionError("the flight log was asked")

    monkeypatch.setattr(runmod, "AirTrailClient", no_one)
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    with Ledger.open(Settings.from_env().ledger_path) as led:
        for k, v in {
            "airtrail_key": json.dumps(
                {
                    "at": "<b>soon</b>",
                    "kind": "apiKey",
                    "missing": ["flight.read.own", "rm -rf"],
                    "denied": [],
                    "more": ["flight.delete.own", "flight.read.any", 7],
                }
            ),
            "airtrail_down_since": "yesterday",
            "airtrail_key_problem": "scope:\x1b[2Jwiped",
        }.items():
            led.kv.set(k, v)
    code, out, err = run(capsys, "airtrail", "status")
    assert code == 0 and err == ""
    assert out[2:] == [
        "key: lacks flight.read.own, ?, checked ? UTC",
        "key: it may also flight.read.any, ?; tripbox uses none of them — a narrower key is safer",
        "trouble: not answering since ? UTC",
        "trouble: the key lacks ?",
    ]
