# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
import json
from functools import partial
from pathlib import Path

import httpx2
import pytest
from helpers.airtrail import ServesReferenceData
from test_kitinerary_map import flight_obj
from test_run import AR, D, make_ctx

from tripbox import util
from tripbox.enrich.aeroapi import AeroAPI, fields_from, pick_flight
from tripbox.enrich.enricher import candidates, enrich_recent
from tripbox.ledger.repo import Items
from tripbox.pipeline import run as runmod
from tripbox.util import now_iso, parse_iso
from tripbox.validate import checks, rules
from tripbox.write.airtrail import (
    MEMORY_USER_ID,
    REQUIRED_SCOPES,
    AirTrailError,
    AirTrailRefs,
    Profile,
)
from tripbox.write.sync import FILL_FIELDS, Syncer

ANSWERS = Path(__file__).resolve().parent / "data" / "aeroapi"


def aero_flight(origin, dest, sched_out, reg="N900ZZ", atype="B738"):
    return {
        "origin": {"code_iata": origin},
        "destination": {"code_iata": dest},
        "scheduled_out": sched_out,
        "registration": reg,
        "aircraft_type": atype,
        "actual_off": "2027-06-22T13:34:00Z",
        "actual_on": "2027-06-22T15:48:00Z",
        "gate_origin": "12",
        "gate_destination": "B7",
        "terminal_origin": None,
        "terminal_destination": "B",
    }


def test_pick_flight_by_origin_and_local_date():
    flights = [
        aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z"),
        aero_flight("PHL", "MSP", "2027-06-22T22:00:00Z"),
    ]
    f = pick_flight(
        flights,
        origin_iata="MSP",
        destination_iata="PHL",
        date_local="2027-06-22",
        origin_tz="America/Chicago",
    )
    assert f["registration"] == "N900ZZ"
    # two legs from the same origin on the same day with the same designator -> ambiguous -> None
    dup = flights + [aero_flight("MSP", "DEN", "2027-06-22T20:00:00Z")]
    assert (
        pick_flight(
            dup,
            origin_iata="MSP",
            destination_iata=None,
            date_local="2027-06-22",
            origin_tz="America/Chicago",
        )
        is None
    )
    assert (
        pick_flight(
            dup,
            origin_iata="MSP",
            destination_iata="PHL",
            date_local="2027-06-22",
            origin_tz="America/Chicago",
        )["destination"]["code_iata"]
        == "PHL"
    )
    # late-evening local departure that is already the next day in UTC
    late = [aero_flight("MSP", "PHL", "2027-06-23T03:30:00Z")]
    assert (
        pick_flight(
            late,
            origin_iata="MSP",
            destination_iata="PHL",
            date_local="2027-06-22",
            origin_tz="America/Chicago",
        )
        is not None
    )
    assert fields_from(late[0]) == {
        "aircraftReg": "N900ZZ",
        "takeoffActual": "2027-06-22T13:34:00Z",
        "landingActual": "2027-06-22T15:48:00Z",
        "departureGate": "12",
        "arrivalGate": "B7",
        "arrivalTerminal": "B",
        "_aircraft_icao": "B738",
    }


class FakeAero:
    def __init__(self, flights, cost=0.0):
        self.flights_data, self.cost, self.calls = flights, cost, []

    def usage(self):
        return {"total_cost": self.cost}

    def flights(self, ident, start, end):
        self.calls.append((ident, start, end))
        return self.flights_data


class FakeAirTrail(ServesReferenceData):
    """The flight log the flight was sent to, holding it as `flight`: answering as the owner's
    key (the user the ledger was bound to as it was sent), counting each read (`gets`) and
    recording each write (`puts`)."""

    def __init__(self):
        self.puts = []
        self.gets = []
        self.asked_profile = 0
        self.flight = {
            "id": 42,
            "date": "2027-06-22",
            "flightNumber": "DL1234",
            "aircraftReg": None,
            "departureGate": "A1",
            "from": {"id": 1, "iata": "MSP"},
            "to": {"id": 2, "iata": "PHL"},
            "airline": {"id": 9},
            "aircraft": None,
            "passengers": [{"id": 1, "user": {"id": MEMORY_USER_ID}}],
        }

    def profile(self):
        self.asked_profile += 1
        return Profile(MEMORY_USER_ID, "apiKey", frozenset(REQUIRED_SCOPES), frozenset(), True)

    def me(self):
        return {"id": MEMORY_USER_ID}

    def user_id(self):
        return MEMORY_USER_ID

    def get_flight(self, fid):
        self.gets.append(fid)
        return json.loads(json.dumps(self.flight))

    def update_flight(self, fid, payload):
        self.puts.append((fid, payload))


def landed_ctx(
    tmp_path, monkeypatch, people=("ALEX SAMPLE",), dep="2027-06-22T08:20:00-05:00", arr=None
):
    """A flight on the owner's receipt (with `people` on it) sent to the flight log by the pass
    its message ends with, so the flight reads `synced`; the flight log is then the stand-in
    enrichment reads the flight from and writes into. `dep` and `arr` (as printed, with their
    offsets) move its times."""
    objects = [
        flight_obj(name, f"1{seat}", dep=dep) for name, seat in zip(people, "ABCDEF", strict=False)
    ]
    if arr is not None:
        for o in objects:
            o["reservationFor"]["arrivalTime"]["@value"] = arr
    ctx, tg = make_ctx(tmp_path, monkeypatch, objects)
    from helpers.eml import make_eml

    ctx.ledger.kv.set("airtrail", "on")
    # an email read earlier that filed nothing: its row in the log comes first, so the log's ids
    # and the bookings' ids differ
    earlier = ctx.ledger.messages.insert(sha256="0" * 64, source="imap", status="ignored")
    ctx.ledger.items.add(
        message_id=earlier, n=0, kind="flight", item_key="-", outcome="ignored", payload={}
    )
    m = make_eml(from_="alex@example.com", subject="Fwd: Receipt", date=D, text="x", headers=AR)
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=1)
    assert res.status == "processed"
    use(ctx, FakeAirTrail())
    ctx.settings = ctx.settings.__class__(**{**ctx.settings.__dict__, "airtrail_dry_run": False})
    return ctx


def use(ctx, airtrail) -> None:
    """From here on `airtrail` is the context's flight log, and its syncer's."""
    ctx.airtrail = airtrail
    ctx.airtrail_refs = AirTrailRefs(airtrail, ctx.ledger)
    ctx.syncer = Syncer(
        store=ctx.trips,
        ledger=ctx.ledger,
        book=ctx.trips.book,
        client=airtrail,
        refs=ctx.airtrail_refs,
        dry_run=False,
    )


def the_flight(ctx):
    """The one flight the trip store holds as sent to AirTrail, and the log row of its email."""
    (ti,) = ctx.trips.sent_flights()
    (row,) = ctx.ledger.items.by_trip_item(ti.id)
    assert row["outcome"] == "filed" and row["id"] != ti.id
    assert ti.sync_state == "synced" and ti.told == told(ctx, ti)
    return ti, row


def told(ctx, ti):
    """What AirTrail should hold of the booking `ti` (`checks.desired`)."""
    return checks.desired(ti.payload, ti.passengers, ti.status, ctx.trips.book)


def test_enrich_fills_only_empty_fields_and_records_usage(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, row = the_flight(ctx)
    at = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)
    assert [c.id for c in candidates(ctx, at)] == [ti.id]
    aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
    lines = enrich_recent(ctx, aero, at=at)
    assert lines and "enriched" in lines[0] and "N900ZZ" in lines[0]
    fid, payload = ctx.airtrail.puts[0]
    assert (
        payload["aircraftReg"] == "N900ZZ" and payload["departureGate"] == "A1"
    )  # existing gate kept
    assert payload["takeoffActual"] == "2027-06-22T13:34:00Z" and payload["arrivalGate"] == "B7"
    assert aero.calls[0][0] == "DAL1234"  # the ICAO designator from AirTrail's airline record
    # the type AirTrail lacks fills nothing; the rest still lands (the airline's record is in
    # the reference cache since the flight was sent, so only the aircraft is asked for)
    assert ctx.airtrail.ref_calls == [("/api/v1/aircraft", "B738")]
    assert payload["aircraftId"] is None
    assert ctx.ledger.aeroapi.month_total("2027-06") == (1, 0.005)
    # the call and the enrichment are recorded against the booking, not the email's log row
    calls = ctx.ledger.conn.execute("SELECT item_id FROM aeroapi_calls").fetchall()
    assert [c["item_id"] for c in calls] == [ti.id]
    assert ctx.ledger.enrichment.get(ti.id)["enriched_at"] is not None
    assert ctx.ledger.enrichment.get(row["id"]) is None
    assert candidates(ctx, at) == []  # done, not a candidate any more
    assert enrich_recent(ctx, aero, at=at) == ["no flights to enrich"]


def test_enrichment_picks_flights_by_what_the_trip_holds(tmp_path, monkeypatch):
    """A flight edited on the page since it was sent is not looked up while AirTrail has not been
    told the edit (its record there is still the unedited flight's); once it has, it is looked up
    under its edited number and day, while the log row of the email that filed it still says what
    the email said. A flight cancelled, removed or in a deleted trip is not looked up at all."""
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, row = the_flight(ctx)
    p = ti.payload
    number = str(int(p["flight_number"]) + 1)

    def next_day(local: str) -> str:
        return (rules.parse_local(local) + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

    edited = ("flight_number", "dep_local", "arr_local")
    ctx.ledger.trip_items.update(
        ti.id,
        payload={
            **p,
            "flight_number": number,
            "dep_local": next_day(p["dep_local"]),
            "arr_local": next_day(p["arr_local"]),
            "owner_edits": dict.fromkeys(edited, now_iso()),
        },
    )
    moved = ctx.trips.settle(ti.id)
    q = moved.payload
    assert q["flight_code"] == p["carrier"] + number and q["date_local"] > p["date_local"]
    assert Items.payload(row)["flight_number"] == p["flight_number"]  # the email's, as it was
    at = parse_iso(q["arr_utc"]) + dt.timedelta(hours=12)
    # AirTrail not told yet: nothing is asked, under either number
    asked = FakeAero([aero_flight(q["origin"], q["destination"], q["dep_utc"])])
    assert moved.sync_state == "pending" and candidates(ctx, at) == []
    assert enrich_recent(ctx, asked, at=at) == ["no flights to enrich"] and asked.calls == []
    # told: it is a candidate as it now stands
    st = ctx.trips
    sent = st.sync_done(ti.id, flight_id=ti.airtrail_flight_id, told=told(ctx, moved))
    assert sent.sync_state == "synced"
    ctx.airtrail.flight.update(date=q["date_local"], flightNumber=q["flight_code"])
    # cancelled, removed, or in a deleted trip, it is not looked up; put back, it is
    for away, back in (
        (partial(st.set_status, ti.id, "cancelled"), partial(st.set_status, ti.id, "confirmed")),
        (partial(st.remove_item, ti.id), partial(st.restore_item, ti.id)),
        (partial(st.delete_trip, ti.trip_id), partial(st.restore_trip, ti.trip_id)),
    ):
        away()
        assert candidates(ctx, at) == [], away
        back()
        assert [c.id for c in candidates(ctx, at)] == [ti.id], back
    aero = FakeAero([aero_flight(q["origin"], q["destination"], q["dep_utc"])])
    lines = enrich_recent(ctx, aero, at=at)
    day = dt.date.fromisoformat(q["date_local"])
    one = dt.timedelta(days=1)
    assert aero.calls == [(f"DAL{number}", day - one, day + 2 * one)]
    assert lines[0].startswith(f"🛬 DAL{number} {q['date_local']} enriched: ")
    assert ctx.airtrail.puts and ctx.ledger.enrichment.get(ti.id)["enriched_at"] is not None


def test_enrichment_leaves_flights_the_flight_log_does_not_hold_as_the_trip_does(
    tmp_path, monkeypatch
):
    """Enrichment writes into a flight's record in AirTrail, so a flight AirTrail does not hold
    as its trip does is never looked up and AeroAPI is never asked: the owner taken off the leg
    (`off`), a check open on it (`blocked`), a flight held out of the reconcile (`off`), and,
    with the flight log off, every flight (`off`)."""

    def owner_left(ctx, ti):
        return ctx.trips.remove_passengers(ti.item_key, ["ALEX SAMPLE"])

    def doubted(ctx, ti):
        payload = dict(ti.payload)
        checks.raise_soft(payload, ["low_confidence"], now_iso())
        ctx.ledger.trip_items.update(ti.id, payload=payload)
        return ctx.trips.settle(ti.id)

    def held(ctx, ti):
        ctx.ledger.trip_items.update(ti.id, sync_hold="past")
        return ctx.trips.settle(ti.id)

    def log_off(ctx, ti):
        ctx.ledger.kv.set("airtrail", "off")
        return ctx.trips.settle(ti.id)

    at = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)
    cases = {
        "owner off": (owner_left, "off"),
        "check open": (doubted, "blocked"),
        "held": (held, "off"),
        "flight log off": (log_off, "off"),
    }
    for n, (name, (change, state)) in enumerate(cases.items()):
        ctx = landed_ctx(tmp_path / str(n), monkeypatch, people=("ALEX SAMPLE", "SAM SAMPLE"))
        ti, _ = the_flight(ctx)
        assert [c.id for c in candidates(ctx, at)] == [ti.id], name
        assert change(ctx, ti).sync_state == state, name
        aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
        assert candidates(ctx, at) == [], name
        assert enrich_recent(ctx, aero, at=at) == ["no flights to enrich"], name
        assert aero.calls == [] and ctx.airtrail.puts == [], name


def test_an_aircraft_type_airtrail_knows_fills_its_id(tmp_path, monkeypatch):
    class KnowsTheType(FakeAirTrail):
        def get_json(self, path, params):
            if path.endswith("/aircraft"):
                super().get_json(path, params)  # recorded like every lookup
                return [{"id": 77, "icao": "B738", "name": "Boeing 737-800"}]
            return super().get_json(path, params)

    ctx = landed_ctx(tmp_path, monkeypatch)
    use(ctx, KnowsTheType())
    at = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)
    aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
    lines = enrich_recent(ctx, aero, at=at)
    (_, payload) = ctx.airtrail.puts[0]
    assert payload["aircraftId"] == 77 and payload["aircraftReg"] == "N900ZZ"
    assert "enriched" in lines[0] and "aircraft B738" in lines[0]
    assert ctx.airtrail.ref_calls == [("/api/v1/aircraft", "B738")]
    assert ctx.ledger.refcache.get("aircraft", "B738")["id"] == 77


def twice() -> FakeAero:
    """AeroAPI answering with two flights that both fit the booking: no unambiguous match. (A
    flight the flight log holds as its trip does always has a destination, so the destination
    never tells two flights from its origin apart here.)"""
    return FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")] * 2)


def test_budget_guard_and_ambiguity(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, _ = the_flight(ctx)
    at = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)
    over = FakeAero([], cost=1.5)
    lines = enrich_recent(ctx, over, at=at)
    assert "budget reached" in lines[0] and over.calls == [] and ctx.airtrail.puts == []
    ambiguous = twice()
    lines = enrich_recent(ctx, ambiguous, at=at)
    assert "no unambiguous" in lines[0] and ctx.airtrail.puts == []
    assert ctx.ledger.enrichment.get(ti.id)["attempts"] == 1


def test_a_dry_run_lists_flights_and_asks_no_one(tmp_path, monkeypatch):
    """In a dry run the flights are listed by their IATA code: the flight-data service is not
    asked, nor AirTrail, not even for the airline's designator."""
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, _ = the_flight(ctx)
    ctx.ledger.refcache.clear()  # the designator would have to be asked for
    ctx.settings = ctx.settings.__class__(**{**ctx.settings.__dict__, "airtrail_dry_run": True})
    aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
    lines = enrich_recent(ctx, aero, at=dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC))
    assert lines == [f"DRY RUN: 1 flight(s) would be looked up: {ti.payload['flight_code']}"]
    assert aero.calls == [] and ctx.airtrail.gets == [] and ctx.airtrail.puts == []
    assert vars(ctx.airtrail).get("ref_calls", []) == [] and ctx.airtrail.asked_profile == 0


class FailingAero(FakeAero):
    def flights(self, ident, start, end):
        from tripbox.enrich.aeroapi import AeroAPIError

        self.calls.append((ident, start, end))
        raise AeroAPIError("503 Service Unavailable")


def test_lookup_failures_warn_only_on_the_last_attempt(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch)
    at = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)
    aero = FailingAero([])
    first = enrich_recent(ctx, aero, at=at)[0]
    second = enrich_recent(ctx, aero, at=at)[0]
    third = enrich_recent(ctx, aero, at=at)[0]
    assert first.startswith("🛬") and "attempt 1/3" in first and "503" in first
    assert second.startswith("🛬") and "attempt 2/3" in second
    assert third.startswith("⚠️") and "giving up after 3 attempts" in third and "503" in third
    assert candidates(ctx, at) == []  # out of attempts, never looked at again
    assert enrich_recent(ctx, aero, at=at) == ["no flights to enrich"]


def test_ambiguous_match_warns_only_on_the_last_attempt(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch)
    at = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)
    ambiguous = twice()
    lines = [enrich_recent(ctx, ambiguous, at=at)[0] for _ in range(3)]
    assert all("no unambiguous" in ln for ln in lines)
    assert lines[0].startswith("🛬") and lines[1].startswith("🛬")
    assert lines[2].startswith("⚠️") and "giving up after 3 attempts" in lines[2]


def test_a_notice_gets_only_results_and_final_warnings():
    from tripbox.enrich.enricher import notice_lines

    assert notice_lines(["no flights to enrich"]) is None
    assert notice_lines(["🛬 DAL1234 2027-06-22: AeroAPI 503 (attempt 1/3, retrying)"]) is None
    assert notice_lines(["🛬 DAL1234 2027-06-22: nothing new to fill"]) is None
    assert notice_lines(
        [
            "🛬 DAL1234 2027-06-22: AeroAPI 503 (attempt 1/3, retrying)",
            "🛬 UAL9055 2027-06-22 enriched: aircraftReg N900ZZ",
            "⚠️ SCX9304 2027-06-21: AeroAPI 503 — giving up after 3 attempts",
        ]
    ) == (
        "🛬 UAL9055 2027-06-22 enriched: aircraftReg N900ZZ\n"
        "⚠️ SCX9304 2027-06-21: AeroAPI 503 — giving up after 3 attempts"
    )


class TimingOutAero(FakeAero):
    def flights(self, ident, start, end):
        import httpx2

        self.calls.append((ident, start, end))
        raise httpx2.ReadTimeout("read timed out")


def test_a_transport_error_on_one_flight_does_not_abort_the_run(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch)
    at = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)
    lines = enrich_recent(ctx, TimingOutAero([]), at=at)
    assert lines and lines[0].startswith("🛬") and "ReadTimeout" in lines[0]
    assert "attempt 1/3" in lines[0]

    class UsageDown(FakeAero):
        def usage(self):
            import httpx2

            raise httpx2.ConnectError("dns")

    lines = enrich_recent(ctx, UsageDown([]), at=at)
    assert "usage check failed" in lines[0]


def test_aeroapi_client_wraps_transport_errors():
    import httpx2
    import pytest

    from tripbox.enrich.aeroapi import AeroAPI, AeroAPIError

    def down(request):
        raise httpx2.ConnectError("dns")

    api = AeroAPI(
        "k", http=httpx2.Client(transport=httpx2.MockTransport(down), base_url="https://x")
    )
    with pytest.raises(AeroAPIError):
        api.usage()


def test_window_gives_every_flight_three_nightly_attempts(tmp_path, monkeypatch):
    """A flight that lands between 01:30 and 03:30 Central time is first seen the next night:
    the window is long enough to give it three nightly attempts all the same."""
    from tripbox.enrich.enricher import WINDOW_DAYS

    ctx = landed_ctx(tmp_path, monkeypatch)
    assert WINDOW_DAYS >= 4
    at = dt.datetime(2027, 6, 26, 6, 0, tzinfo=dt.UTC)  # 3.59 days after the 15:55Z landing
    assert len(candidates(ctx, at)) == 1


def test_ident_prefers_the_icao_a_backfilled_payload_carries():
    from tripbox.enrich.enricher import ident_for

    assert ident_for({"carrier": "DL", "airline_icao": "DAL", "flight_number": "9123"}) == "DAL9123"
    assert ident_for({"carrier": "DL", "flight_number": "9123"}) == "DL9123"


def test_the_ident_takes_the_icao_designator_from_airtrails_airline_record():
    from tripbox.enrich.enricher import ident_for
    from tripbox.ledger import Ledger

    airtrail = ServesReferenceData()
    refs = AirTrailRefs(airtrail, Ledger.memory())
    validated = {"carrier": "DL", "flight_number": "9123"}  # a validated payload names no ICAO
    assert ident_for(validated, refs) == "DAL9123"
    assert ident_for(validated, refs) == "DAL9123"
    assert airtrail.ref_calls == [("/api/v1/airlines", "DL")]  # the hit is kept
    # an airline AirTrail does not know: the IATA carrier
    assert ident_for({"carrier": "QX", "flight_number": "9123"}, refs) == "QX9123"
    # a backfilled payload's own ICAO code is used without asking
    backfilled = {"carrier": "WN", "airline_icao": "SWA", "flight_number": "9123"}
    assert ident_for(backfilled, refs) == "SWA9123"
    assert airtrail.ref_calls == [("/api/v1/airlines", "DL"), ("/api/v1/airlines", "QX")]


def test_an_airline_lookup_that_fails_asks_by_the_iata_carrier(caplog):
    import logging

    from tripbox.enrich.enricher import ident_for
    from tripbox.write.airtrail import AirTrailError

    class Down:
        def get_json(self, path, params):
            raise AirTrailError(503, "unavailable")

    caplog.set_level(logging.WARNING, logger="tripbox.enrich")
    refs = AirTrailRefs(Down(), None)
    assert ident_for({"carrier": "DL", "flight_number": "9123"}, refs) == "DL9123"
    assert len([r for r in caplog.records if r.name == "tripbox.enrich"]) == 1


# ----------------------------------------------------------------------------- the service
def answered(name: str) -> dict:
    return json.loads((ANSWERS / name).read_text())


def service(flights: dict[str, str]) -> tuple[AeroAPI, list[str]]:
    """The flight-data service answering from `tests/data/aeroapi/` through a mock transport:
    each ident it is asked by maps to an answer file; every path asked is recorded."""
    asked: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/aeroapi")
        asked.append(path)
        if path == "/account/usage":
            return httpx2.Response(200, json=answered("usage.json"))
        ident = path.removeprefix("/flights/")
        if ident not in flights:
            return httpx2.Response(404, json={"title": "Not found"})
        # as the service reads its window: a bare date is its midnight UTC, `end` is exclusive
        start, end = (
            dt.datetime.fromisoformat(request.url.params[k]).replace(tzinfo=dt.UTC)
            for k in ("start", "end")
        )
        body = answered(flights[ident])
        body["flights"] = [
            f for f in body["flights"] if start <= parse_iso(f["scheduled_out"]) < end
        ]
        return httpx2.Response(200, json=body)

    http = httpx2.Client(
        transport=httpx2.MockTransport(answer), base_url="https://flightdata.example/aeroapi"
    )
    return AeroAPI("sample-key", http=http), asked


AT = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)


def test_enrichment_asks_by_the_icao_designator_and_by_the_iata_code_when_airtrail_knows_none(  # noqa: E501
    tmp_path, monkeypatch
):
    ctx = landed_ctx(tmp_path / "icao", monkeypatch)
    aero, asked = service({"DAL1234": "flights-icao.json"})
    lines = enrich_recent(ctx, aero, at=AT)
    assert asked == ["/account/usage", "/flights/DAL1234"]
    assert lines[0].startswith("🛬 DAL1234 2027-06-22 enriched: aircraftReg N900ZZ")
    (_, put), = ctx.airtrail.puts
    want = answered("flights-icao.json")["flights"][0]
    assert put["takeoffActual"] == want["actual_off"]
    assert put["arrivalGate"] == want["gate_destination"]

    class KnowsNoAirline(FakeAirTrail):
        def get_json(self, path, params):
            super().get_json(path, params)  # recorded like every lookup
            return []

    ctx = landed_ctx(tmp_path / "iata", monkeypatch)
    ctx.ledger.refcache.clear()
    use(ctx, KnowsNoAirline())
    aero, asked = service({"DL1234": "flights-iata.json"})
    lines = enrich_recent(ctx, aero, at=AT)
    assert asked == ["/account/usage", "/flights/DL1234"]
    assert lines[0].startswith("🛬 DL1234 2027-06-22 enriched: ")
    assert ctx.airtrail.puts
    # two flights that day: no one answer; none: no match; each is retried another night
    for name in ("flights-two.json", "flights-none.json"):
        ctx = landed_ctx(tmp_path / name, monkeypatch)
        aero, asked = service({"DAL1234": name})
        (line,) = enrich_recent(ctx, aero, at=AT)
        assert "no unambiguous AeroAPI match" in line and "attempt 1/3" in line, name
        assert ctx.airtrail.puts == [] and ctx.airtrail.gets == [], name


def test_a_fill_puts_blanks_only_and_never_a_value_the_log_holds(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, _ = the_flight(ctx)
    ctx.airtrail.flight.update(aircraftReg="N1SAMPLE", departureGate="A1", arrivalGate="")
    fills = {"aircraftReg": "N900ZZ", "departureGate": "G12", "arrivalGate": "B7"}
    fills["landingActual"] = None
    assert ctx.syncer.fill(ti.id, fills) == ("fill", {"arrivalGate": "B7"})
    ((fid, put),) = ctx.airtrail.puts
    assert fid == ti.airtrail_flight_id
    held = (put["aircraftReg"], put["departureGate"], put["arrivalGate"])
    assert held == ("N1SAMPLE", "A1", "B7")
    assert put.get("landingActual") is None and "track" not in put
    # every blank filled: nothing is written
    ctx.airtrail.flight.update(arrivalGate="B7")
    assert ctx.syncer.fill(ti.id, {"arrivalGate": "C9", "aircraftReg": "N900ZZ"}) == (
        "nothing",
        {},
    )
    assert len(ctx.airtrail.puts) == 1
    # nor is the booking's flight touched: still synced, told as before
    assert ctx.trips.item(ti.id) == ti


def test_fill_refuses_a_field_the_booking_decides(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, _ = the_flight(ctx)
    assert "aircraftReg" in FILL_FIELDS
    for field in ("date", "departure", "flightNumber", "fromId", "passengers", "note", "track"):
        with pytest.raises(ValueError, match="not a field enrichment fills"):
            ctx.syncer.fill(ti.id, {"aircraftReg": "N900ZZ", field: "x"})
    assert ctx.airtrail.gets == [] and ctx.airtrail.puts == []


def test_a_fill_and_a_pass_never_cross_on_one_flight(tmp_path, monkeypatch):
    """A flight a sync pass has claimed waits: nothing is asked of the flight log, and the
    claim is the pass's still."""
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, _ = the_flight(ctx)
    now = util.now()
    assert ctx.trips.claim_sync(ti.id, now=now)
    assert ctx.syncer.fill(ti.id, {"aircraftReg": "N900ZZ"}, now=now) == ("wait", {})
    assert ctx.airtrail.asked_profile == 0 and ctx.airtrail.gets == [] and ctx.airtrail.puts == []
    assert not ctx.trips.claim_sync(ti.id, now=now)  # still held by the pass
    ctx.trips.release_sync(ti.id, now=now)
    # released, the fill takes the flight and gives it back
    assert ctx.syncer.fill(ti.id, {"aircraftReg": "N900ZZ"})[0] == "fill"
    assert ctx.trips.item(ti.id).sync_claimed_at is None


def test_a_fill_that_finds_the_flight_log_down_starts_no_outage_clock(tmp_path, monkeypatch):
    """The outage clock and its alert are the sync's: a fill (or enrichment asking whether it
    may write) that finds the flight log down notes nothing, so no later outage of the sync is
    timed from it."""

    class Down(FakeAirTrail):
        def __init__(self, at):
            super().__init__()
            self.at = at

        def profile(self):
            if self.at == "profile":
                raise AirTrailError(503, "Service Unavailable")
            return super().profile()

        def get_flight(self, fid):
            if self.at == "read":
                raise AirTrailError(503, "Service Unavailable")
            return super().get_flight(fid)

    for at in ("profile", "read"):
        ctx = landed_ctx(tmp_path / at, monkeypatch)
        ti, _ = the_flight(ctx)
        alerts: list[str] = []
        use(ctx, Down(at))
        ctx.syncer.alert = alerts.append
        assert ctx.syncer.fill(ti.id, {"aircraftReg": "N900ZZ"}) == ("outage", {}), at
        if at == "profile":
            assert ctx.syncer.bound() == "outage"
        assert ctx.ledger.kv.get("airtrail_down_since") in (None, ""), at
        assert ctx.ledger.kv.get("airtrail_alerted") in (None, ""), at
        assert ctx.trips.item(ti.id) == ti and alerts == [], at


def test_a_fill_on_a_flight_deleted_there_unlinks_it(tmp_path, monkeypatch):
    class Deleted(FakeAirTrail):
        def get_flight(self, fid):
            self.gets.append(fid)
            raise AirTrailError(404, "Flight not found", code="not_found")

    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, _ = the_flight(ctx)
    use(ctx, Deleted())
    aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
    (line,) = enrich_recent(ctx, aero, at=AT)
    assert "no longer yours in the flight log" in line and ctx.airtrail.puts == []
    after = ctx.trips.item(ti.id)
    assert (after.airtrail_flight_id, after.told, after.sync_hold) == (None, None, "deleted")
    assert candidates(ctx, AT) == []


def test_enrichment_writes_nothing_to_a_record_that_is_not_the_owners(tmp_path, monkeypatch):
    """A record that lists passengers, none of them the key's user, and a record that has become
    another flight (two of its day, number and origin other than told): unlinked, nothing put."""
    for case in ("not aboard", "another flight"):
        ctx = landed_ctx(tmp_path / case.replace(" ", "-"), monkeypatch)
        ti, _ = the_flight(ctx)
        p = ti.payload
        if case == "not aboard":
            there = {"passengers": [{"id": 7, "user": {"id": "another-user"}}]}
        else:
            later = dt.date.fromisoformat(p["date_local"]) + dt.timedelta(days=3)
            number = p["carrier"] + str(int(p["flight_number"]) + 1)
            there = {"date": later.isoformat(), "flightNumber": number}
        ctx.airtrail.flight.update(there)
        aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
        (line,) = enrich_recent(ctx, aero, at=AT)
        assert "no longer yours in the flight log" in line, case
        assert ctx.airtrail.puts == [], case
        assert ctx.trips.item(ti.id).sync_hold == "deleted", case


def test_enrichment_writes_nothing_while_the_ledger_is_bound_to_another_user(
    tmp_path, monkeypatch
):
    ctx = landed_ctx(tmp_path, monkeypatch)
    ti, _ = the_flight(ctx)
    ctx.ledger.kv.set("airtrail_user", "another-user")
    aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
    lines = enrich_recent(ctx, aero, at=AT)
    assert lines == [
        "the flight log takes no writes now (another flight log is set up); 1 flight(s) wait"
    ]
    assert aero.calls == [] and ctx.airtrail.gets == [] and ctx.airtrail.puts == []
    assert ctx.syncer.fill(ti.id, {"aircraftReg": "N900ZZ"}) == ("other", {})
    assert ctx.airtrail.gets == [] and ctx.airtrail.puts == []
    assert ctx.ledger.kv.get("airtrail_user") == "another-user"


def test_no_notice_line_shows_an_internal_word(tmp_path, monkeypatch):
    """What the sync answers (`stop`, `other`, `outage`, `wait`, `dry_run`) is said in words in
    the line for the run and for a flight, never as the answer's own word."""
    said = {
        "stop": "the key was refused",
        "other": "another flight log is set up",
        "outage": "the flight log did not answer",
        "wait": "not sent yet",
        "dry_run": "dry run",
    }
    ctx = landed_ctx(tmp_path, monkeypatch)
    for word in ("stop", "other", "outage"):
        monkeypatch.setattr(ctx.syncer, "bound", lambda word=word: word)
        aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
        lines = enrich_recent(ctx, aero, at=AT)
        assert lines == [f"the flight log takes no writes now ({said[word]}); 1 flight(s) wait"]
    for word, phrase in said.items():
        # a flight is tried three times, then no more: each word gets a flight of its own
        (tmp_path / word).mkdir()
        ctx = landed_ctx(tmp_path / word, monkeypatch)
        monkeypatch.setattr(ctx.syncer, "fill", lambda *a, word=word, **k: (word, {}))
        aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
        (line,) = enrich_recent(ctx, aero, at=AT)
        assert f"({phrase})" in line and f"({word})" not in line


def test_enrichment_never_asks_about_a_flight_the_owner_has_left(tmp_path, monkeypatch):
    """The owner taken off the flight while the run looks it up: the fill reads the booking
    again under its claim, finds it no longer synced, and asks the flight log nothing."""
    ctx = landed_ctx(tmp_path, monkeypatch, people=("ALEX SAMPLE", "SAM SAMPLE"))
    ti, _ = the_flight(ctx)

    class OwnerLeaves(FakeAero):
        def flights(self, ident, start, end):
            ctx.trips.remove_passengers(ti.item_key, ["ALEX SAMPLE"])
            return super().flights(ident, start, end)

    aero = OwnerLeaves([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
    (line,) = enrich_recent(ctx, aero, at=AT)
    assert "could not take it now (not sent yet)" in line
    assert ctx.airtrail.gets == [] and ctx.airtrail.puts == []
    assert ctx.trips.item(ti.id).sync_state == "off"


def test_enrichment_fills_nothing_on_a_record_the_owner_corrected_by_hand(tmp_path, monkeypatch):
    """A record whose number (or day) the owner corrected there still passes the sync's two of
    three, so it stays linked; but what the service says of the booking's flight is not that
    record's flight. Enrichment writes only to a record whose day, number and origin all agree
    with what it was told: nothing is put, the link is left, nobody is told."""
    alerts: list[str] = []
    for part in ("number", "day"):
        ctx = landed_ctx(tmp_path / part, monkeypatch)
        ti, _ = the_flight(ctx)
        ctx.syncer.alert = alerts.append
        p = ti.payload
        if part == "number":
            there = {"flightNumber": p["carrier"] + str(int(p["flight_number"]) + 1)}
        else:
            later = dt.date.fromisoformat(p["date_local"]) + dt.timedelta(days=1)
            there = {"date": later.isoformat()}
        ctx.airtrail.flight.update(there)
        aero = FakeAero([aero_flight("MSP", "PHL", "2027-06-22T13:20:00Z")])
        (line,) = enrich_recent(ctx, aero, at=AT)
        assert "nothing new to fill" in line, part
        assert ctx.airtrail.puts == [], part
        after = ctx.trips.item(ti.id)
        assert (after.airtrail_flight_id, after.sync_hold) == (ti.airtrail_flight_id, None), part
        assert after.sync_state == "synced", part
        assert ctx.syncer.fill(ti.id, {"aircraftReg": "N900ZZ"}) == ("nothing", {}), part
    assert alerts == []
