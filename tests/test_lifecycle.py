# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""AirTrail switched on, off and on again. Each context a command builds notes in the ledger
whether a flight log is kept (`build_context` calls `lifecycle.note_airtrail`), and a change is an
event: switched on, every flight never sent that had already left is held out of the flight log
and the upcoming flights the owner is on queue themselves, each looked for there before it is
created; switched off, nothing is sent and nothing breaks; on again, what changed meanwhile is
sent. A past flight goes only on request (`tripbox airtrail enroll`). Every booking is filed from
the corpus's emails at run time, the clock is held, and the flight log is an AirTrail held in
memory, one across every run. One ledger keeps one flight log: the cached ids go when its address
changes, and `tripbox airtrail forget` unlinks every flight so another flight log can be kept."""

import dataclasses
import datetime as dt
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from helpers.flightlog import Scripted, seed
from helpers.web import ORIGIN
from starlette.testclient import TestClient

from tripbox import cli, corpus, util
from tripbox.config import OFFLINE_ENV, Settings, off_line
from tripbox.ledger import Ledger
from tripbox.pipeline.run import Context, build_context, process_raw, sync
from tripbox.trips.store import KeyCollision, TripItem, TripStore, stamp
from tripbox.web.app import create_app
from tripbox.write import lifecycle
from tripbox.write.airtrail import MEMORY_USER_ID, REQUIRED_SCOPES, AirTrailRefs, Profile
from tripbox.write.sync import Syncer

# The corpus's emails these stories are told with; what each files is read from it at run time.
ROUND_TRIP = "06-city-names-receipt"  # the owner alone, out on one day and back on a later one
COMPANION = "24-pdf-e-ticket"  # a companion alone, out and back
STAY = "08-independent-hotel"
LATE = "23-date-line-crossing"  # the owner alone, after the round trip
BOOKED, CHANGE = "01-markup-flight", "22-schedule-change"  # the owner's flights, then one moved
LAST = "27-oversize"  # the owner's flights, in the corpus's last email that files any
# The checks only a flight log opens.
FLIGHT_LOG_ONLY = (
    "owner_unknown",
    "unverified_sender",
    "airtrail_airport",
    "airtrail_refused",
    "airtrail_still_lists",
)
FLIGHT_LOG = {
    "TRIPBOX_AIRTRAIL_URL": "http://flightlog.example:3000",
    "TRIPBOX_AIRTRAIL_API_KEY": "k",
    "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
}
NAMES_THE_FLIGHT_LOG = re.compile(r"airtrail|flight log", re.IGNORECASE)
ENROLL_USAGE = "usage: tripbox airtrail enroll --since YYYY-MM-DD [--apply]"
ELSEWHERE = "http://elsewhere.example:3000"  # another flight log's address
DAY = dt.timedelta(days=1)


class Home:
    """One installation over the corpus: its ledger file under `base`, kept from run to run, and
    one flight log held in memory that every run with AirTrail on keeps in step. A run builds its
    context as a command does (`build_context`) with the clock held, with AirTrail on (a flight
    log configured), off (none) or invalid (one configured, and a travellers file that names no
    owner)."""

    def __init__(self, base: Path) -> None:
        self.c = corpus.load(corpus.default_dir())
        nobody = base / "nobody"
        nobody.mkdir()
        travellers = yaml.safe_load((self.c.root / corpus.TRAVELLERS).read_text(encoding="utf-8"))
        del travellers["owner"]
        (nobody / "travellers.yaml").write_text(yaml.safe_dump(travellers), encoding="utf-8")
        common = {
            **self.c.settings,
            "TRIPBOX_CONFIG_DIR": str(self.c.root / corpus.CONFIG_DIR),
            "TRIPBOX_DATA_DIR": str(base / "data"),
            "TRIPBOX_ARCHIVE_DIR": str(base / "archive"),
            "TRIPBOX_LOGS_DIR": str(base / "logs"),
            "TRIPBOX_AUTH": "none",
        }
        self.environ = {
            "on": {**common, **FLIGHT_LOG},
            "dry": {**common, **FLIGHT_LOG, "TRIPBOX_AIRTRAIL_DRY_RUN": "true"},
            "off": common,
            "invalid": {**common, **FLIGHT_LOG, "TRIPBOX_CONFIG_DIR": str(nobody)},
        }
        self.settings = {k: Settings.from_env(env) for k, env in self.environ.items()}
        assert [self.settings[k].feature("airtrail").state for k in self.environ] == [
            "on",
            "on",
            "off",
            "invalid",
        ]
        self.zone = ZoneInfo(self.settings["on"].tz)
        self.led = Ledger.open(self.settings["on"].ledger_path)
        # the flight log's answers are cached as fetched the day after the corpus's last flight,
        # so none ages out of the reference cache under a clock any story here holds
        last = max(day for entry in self.c.entries for day in self.departures(entry.name))
        with util.clock_at(self.noon(last + DAY)):
            seed(self.led, self.c)
        self.log = Scripted(self.led)

    def run(self, airtrail: str, at: dt.datetime) -> Context:
        """A command's context, built at `at` with AirTrail `airtrail` (`dry`: on, in a dry
        run)."""
        s = self.settings[airtrail]
        with util.clock_at(at):
            ctx = build_context(s, ledger=self.led, with_model=False, with_notifier=False)
        ctx.model = corpus.CorpusAnswers(self.c, s.llm_model)
        if ctx.syncer is not None:
            ctx.airtrail = self.log
            ctx.syncer = Syncer(
                store=ctx.trips,
                ledger=self.led,
                book=ctx.trips.book,
                client=self.log,
                refs=None,
                dry_run=s.airtrail_dry_run,
            )
        return ctx

    def sent_at(self, name: str) -> dt.datetime:
        """When the corpus's email `name` reached the mailbox."""
        return corpus.sent(self.c.entry(name))

    def noon(self, day: dt.date) -> dt.datetime:
        """Midday of `day` where the corpus lives."""
        return dt.datetime.combine(day, dt.time(12), self.zone)

    def file(self, ctx: Context, name: str, at: dt.datetime | None = None) -> list[TripItem]:
        """The corpus's email `name` through the pipeline as the mailbox hands it over, the clock
        at `at` (when it reached the mailbox, unless given), then the pass every message ends
        with: the bookings it filed or changed, as the store then holds them."""
        received = self.sent_at(name)
        with util.clock_at(at or received):
            res = process_raw(
                ctx, self.c.entry(name).raw, source="eval", received=received, notify=False
            )
        assert res.status == "processed", name
        return [ctx.trips.item(o.trip_item_id) for o in res.outcomes]

    def departures(self, name: str) -> list[dt.date]:
        """The local day each flight leg of the email `name` departs, as the corpus states it (a
        leg it prints no day for is left out)."""
        out = []
        for res in self.c.entry(name).truth["reservations"]:
            for leg in res.get("segments") or []:
                value = first(leg.get("departure_local"))
                if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
                    out.append(dt.date.fromisoformat(value[:10]))
        return out

    def everything(self) -> list[TripItem]:
        """Every booking the ledger holds, oldest first."""
        st = TripStore(self.led, (), book=self.settings["on"].travellers)
        rows = self.led.conn.execute("SELECT id FROM trip_items ORDER BY id").fetchall()
        return [st.item(r["id"]) for r in rows]  # type: ignore[misc]


@pytest.fixture
def home(tmp_path, monkeypatch):
    # an AirTrail configured is one held in memory: nothing is ever asked over the network
    monkeypatch.setenv(OFFLINE_ENV, "1")
    h = Home(tmp_path)
    try:
        yield h
    finally:
        h.led.close()


def first(value):
    """A truth value is one reading or a list of acceptable ones: the first."""
    return value[0] if isinstance(value, list) and value else value


def day(ti: TripItem) -> dt.date:
    """The local day a flight departs."""
    return dt.date.fromisoformat(ti.payload["date_local"])


def by_day(items: list[TripItem]) -> list[TripItem]:
    """Flights, the first to leave first."""
    return sorted(items, key=day)


def sent(log: Scripted) -> list[str]:
    """What was sent to the flight log, in order: `create` or `update`."""
    return [r[0] for r in log.sent]


def owner_alone(ctx: Context, ti: TripItem) -> bool:
    found = ctx.trips.book.classify(ti.passengers)
    return found.me_present and not (found.guests or found.others)


def companions_alone(ctx: Context, ti: TripItem) -> bool:
    found = ctx.trips.book.classify(ti.passengers)
    return not found.me_present and bool(found.guests) and not found.others


def state(home: Home) -> list[dict]:
    """Every booking as the ledger holds it, to compare before and after."""
    return [dataclasses.asdict(ti) for ti in home.everything()]


def test_switching_airtrail_on_queues_upcoming_owner_flights_and_holds_past_ones(home):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    theirs = home.file(ctx, COMPANION)
    (stay,) = home.file(ctx, STAY)
    assert owner_alone(ctx, out) and owner_alone(ctx, back) and day(out) < day(back)
    assert all(companions_alone(ctx, ti) for ti in theirs) and stay.kind == "lodging"
    assert {ti.sync_state for ti in home.everything()} == {"off"}
    # the owner's way out has left; the way back and the companion's flights are to come
    today = day(out) + DAY
    assert today < day(back) and all(today < day(ti) for ti in theirs)
    on = home.run("on", at=home.noon(today))
    st = on.trips
    assert (st.item(out.id).sync_state, st.item(out.id).sync_hold) == ("off", "past")
    assert (st.item(back.id).sync_state, st.item(back.id).sync_hold) == ("pending", None)
    assert [(st.item(ti.id).sync_state, st.item(ti.id).sync_hold) for ti in theirs] == [
        ("off", None)
    ] * len(theirs)
    assert (st.item(stay.id).sync_state, st.item(stay.id).sync_hold) == ("off", None)
    assert home.log.requests == []  # switching on sends nothing by itself
    # the next pass looks for the one flight to come, then creates it
    with util.clock_at(home.noon(today)):
        assert sync(on).done == {"create": 1}
    (flight,) = home.log.flights
    assert [r[0] for r in home.log.requests if r[0] in ("find", "create")] == ["find", "create"]
    sent_back = st.item(back.id)
    assert (sent_back.sync_state, sent_back.airtrail_flight_id) == ("synced", flight["id"])
    assert flight["date"] == back.payload["date_local"]
    assert st.item(out.id).sync_state == "off"


def test_a_flight_airtrail_already_holds_is_adopted_at_switch_on(home):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    # the flight log already holds the flight to come, logged there by hand
    p = back.payload
    refs = AirTrailRefs(home.log, home.led)
    there = home.log.create_flight(
        {
            "date": p["date_local"],
            "flightNumber": p["flight_code"],
            "fromId": refs.airport_id(p["origin"]),
            "toId": refs.airport_id(p["destination"]),
        }
    )
    home.log.requests.clear()
    today = day(out) + DAY
    on = home.run("on", at=home.noon(today))
    assert on.trips.item(back.id).sync_state == "pending"
    with util.clock_at(home.noon(today)):
        assert sync(on).done == {"adopt": 1}
    assert len(home.log.flights) == 1 and sent(home.log) == []
    adopted = on.trips.item(back.id)
    assert (adopted.sync_state, adopted.airtrail_flight_id) == ("synced", there)


def test_enroll_sends_past_flights_only_on_request(home):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    theirs = by_day(home.file(ctx, COMPANION))
    assert day(back) < day(theirs[0])
    # switched on once every flight has left: each is held, the companion's too
    today = day(theirs[-1]) + DAY
    on = home.run("on", at=home.noon(today))
    st = on.trips
    held = [out, back, *theirs]
    assert [(st.item(ti.id).sync_state, st.item(ti.id).sync_hold) for ti in held] == [
        ("off", "past")
    ] * len(held)
    with util.clock_at(home.noon(today)):
        assert sync(on).done == {}
    # counted, not sent: the owner's flights since a day; the companion's are never queued
    before = state(home)
    assert lifecycle.enroll(st, since=day(out), apply=False) == 2
    assert lifecycle.enroll(st, since=day(back), apply=False) == 1
    assert lifecycle.enroll(st, since=day(back) + DAY, apply=False) == 0
    assert state(home) == before and home.log.requests == []
    # on request, the way back alone is queued and the next pass sends it
    with util.clock_at(home.noon(today)):
        assert lifecycle.enroll(st, since=day(back), apply=True) == 1
    queued = st.item(back.id)
    assert (queued.sync_state, queued.sync_hold) == ("pending", None)
    assert [st.item(ti.id).sync_hold for ti in (out, *theirs)] == ["past"] * 3
    with util.clock_at(home.noon(today)):
        assert sync(on).done == {"create": 1}
    (flight,) = home.log.flights
    assert st.item(back.id).airtrail_flight_id == flight["id"]
    assert flight["date"] == back.payload["date_local"]
    # asked again, nothing is left to queue since that day
    assert lifecycle.enroll(st, since=day(back), apply=True) == 0


def test_a_late_receipt_for_a_flight_that_has_left_is_sent_when_airtrail_is_already_on(home):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    # AirTrail is switched on once the late email's flights have left too
    today = max(home.departures(LATE)) + DAY
    on = home.run("on", at=home.noon(today))
    assert [on.trips.item(ti.id).sync_hold for ti in (out, back)] == ["past", "past"]
    # their receipt is forwarded late: filed after AirTrail was on, it is sent
    late = home.file(on, LATE, at=home.noon(today))
    assert late and all(owner_alone(on, ti) and day(ti) < today for ti in late)
    assert [(ti.sync_state, ti.sync_hold) for ti in late] == [("synced", None)] * len(late)
    assert sent(home.log) == ["create"] * len(late) and len(home.log.flights) == len(late)
    # the next run holds nothing more
    again = home.run("on", at=home.noon(today + DAY))
    assert [again.trips.item(ti.id).sync_hold for ti in late] == [None] * len(late)
    assert [again.trips.item(ti.id).sync_hold for ti in (out, back)] == ["past", "past"]


def test_a_flight_filed_while_airtrail_was_on_keeps_its_place_across_an_off_and_on(home):
    # the first switch on, in a dry run: the round trip is filed and waits, sent nowhere
    dry = home.run("dry", at=home.sent_at(ROUND_TRIP))
    waiting = by_day(home.file(dry, ROUND_TRIP))
    assert [(ti.sync_state, ti.sync_hold) for ti in waiting] == [("pending", None)] * 2
    assert sent(home.log) == []
    # switched off, when is noted with the flag; the late trip is filed meanwhile
    off_at = home.sent_at(LATE)
    off = home.run("off", at=off_at)
    assert home.led.kv.get("airtrail_off_at") == stamp(off_at)
    filed_off = home.file(off, LATE)
    # on again, dry run off, once every one of these flights has left: what was filed while a
    # flight log was kept keeps its place in the queue; what was filed while none was is held
    today = max(home.departures(LATE)) + DAY
    on = home.run("on", at=home.noon(today))
    st = on.trips
    assert all(day(ti) < today for ti in [*waiting, *filed_off])
    assert [(st.item(ti.id).sync_state, st.item(ti.id).sync_hold) for ti in waiting] == [
        ("pending", None)
    ] * 2
    assert [(st.item(ti.id).sync_state, st.item(ti.id).sync_hold) for ti in filed_off] == [
        ("off", "past")
    ] * len(filed_off)
    with util.clock_at(home.noon(today)):
        assert sync(on).done == {"create": len(waiting)}
    days = [ti.payload["date_local"] for ti in waiting]
    assert sorted(f["date"] for f in home.log.flights) == days
    assert [st.item(ti.id).sync_state for ti in waiting] == ["synced"] * 2


def test_switching_airtrail_off_breaks_nothing_and_sends_nothing(home):
    start = home.c.entries[0].name
    on = home.run("on", at=home.sent_at(start))
    # the flight log refuses the first flight it is sent, and knows no airport of the far trip
    home.log.fail_next(400, on="create")
    (far,) = {
        first(leg["destination"])
        for res in home.c.entry(LATE).truth["reservations"]
        for leg in res["segments"][:1]
    }
    home.led.conn.execute("DELETE FROM ref_cache WHERE kind='airport' AND code=?", (far,))
    filed = {
        entry.name: home.file(on, entry.name)
        for entry in home.c.entries
        if entry.name != LAST and entry.outcome["items"]
    }
    # nobody is named on one of the companion's flights any more
    nameless = by_day(filed[COMPANION])[0]
    with util.clock_at(home.sent_at(COMPANION)):
        on.trips.edit_item(nameless.id, {"passengers": []})
    # the owner was not on a flight the flight log was sent: it still lists it as theirs
    left = next(ti for ti in home.everything() if ti.kind == "flight" and ti.sync_state == "synced")
    with util.clock_at(home.sent_at(LAST)):
        assert "airtrail_still_lists" in on.trips.owner_answer(left.id, False).checks
    every = home.everything()
    flights = [ti for ti in every if ti.kind == "flight"]
    assert set(FLIGHT_LOG_ONLY) <= {code for ti in every for code in ti.checks}
    marked_for_it = [
        ti.id for ti in every if ti.checks and set(ti.checks) <= set(FLIGHT_LOG_ONLY)
    ]
    linked = {ti.id: (ti.airtrail_flight_id, ti.told) for ti in flights}
    assert any(flight_id for flight_id, _ in linked.values())
    assert any(NAMES_THE_FLIGHT_LOG.search(said) for said in pages(home))
    # switched off: every flight reads off and keeps its link and what was told, and nothing
    # that only a flight log explains is left open
    asked = len(home.log.requests)
    off = home.run("off", at=home.sent_at(LAST))
    every = home.everything()
    flights = [ti for ti in every if ti.kind == "flight"]
    assert [ti.sync_state for ti in flights] == ["off"] * len(flights)
    assert {ti.id: (ti.airtrail_flight_id, ti.told) for ti in flights} == linked
    assert not [ti.id for ti in every if set(ti.checks) & set(FLIGHT_LOG_ONLY)]
    assert [off.trips.item(i).checks for i in marked_for_it] == [()] * len(marked_for_it)
    # the list and every trip page render, no box, chip or menu names the flight log, and a
    # trip counts only the bookings with something of their own to check
    shown = pages(home)
    assert shown and not [said for said in shown if NAMES_THE_FLIGHT_LOG.search(said)]
    st, c = off.trips, home_client(home)
    for trip in st.all_trips():
        to_check = len(st.flagged(trip.id))
        page = c.get(f"/trips/{trip.id}").text
        assert (f"{to_check} to check" in page) is bool(to_check), trip.id
    # a new email is filed, and nothing is sent
    new = home.file(off, LAST)
    assert [(ti.sync_state, ti.sync_hold) for ti in new] == [("off", None)] * len(new)
    assert any(off.trips.book.classify(ti.passengers).me_present for ti in new)
    assert len(home.log.requests) == asked


def test_a_change_made_while_airtrail_was_off_or_invalid_is_sent_when_it_returns(home):
    on = home.run("on", at=home.sent_at(BOOKED))
    legs = {ti.id: ti for ti in home.file(on, BOOKED)}
    assert [ti.sync_state for ti in legs.values()] == ["synced"] * len(legs)
    logged = home.log.flights
    # AirTrail off for one run, then invalid for another (the owner dropped from the travellers
    # file), and the schedule change is filed meanwhile
    off = home.run("off", at=home.sent_at(CHANGE))
    assert [off.trips.item(i).sync_state for i in legs] == ["off"] * len(legs)
    invalid = home.run("invalid", at=home.sent_at(CHANGE))
    assert invalid.syncer is None
    (moved,) = home.file(invalid, CHANGE)
    assert moved.id in legs and moved.sync_state == "off"
    assert moved.payload["dep_utc"] != legs[moved.id].payload["dep_utc"]
    assert home.log.flights == logged and sent(home.log) == ["create"] * len(legs)
    # AirTrail back: the moved flight differs from what the flight log was told, the rest not
    back = home.run("on", at=home.sent_at(CHANGE))
    st = back.trips
    assert [st.item(i).sync_state for i in legs] == [
        "pending" if i == moved.id else "synced" for i in legs
    ]
    with util.clock_at(home.sent_at(CHANGE)):
        assert sync(back).done == {"update": 1}
    assert sent(home.log) == ["create"] * len(legs) + ["update"]
    assert len(home.log.flights) == len(logged)
    now = st.item(moved.id)
    assert (now.sync_state, now.airtrail_flight_id) == ("synced", legs[moved.id].airtrail_flight_id)
    (there,) = [f for f in home.log.flights if f["id"] == now.airtrail_flight_id]
    assert util.parse_iso(there["departure"]) == util.parse_iso(now.payload["dep_utc"])


def test_a_new_ledger_started_with_airtrail_on_holds_nothing(home):
    # the first context of an empty ledger has AirTrail on, once every flight has left
    today = max(home.departures(ROUND_TRIP)) + DAY
    on = home.run("on", at=home.noon(today))
    assert home.led.kv.get("airtrail") == "on"
    filed = home.file(on, ROUND_TRIP, at=home.noon(today))
    assert [(ti.sync_state, ti.sync_hold) for ti in filed] == [("synced", None)] * len(filed)
    assert sent(home.log) == ["create"] * len(filed)
    # noted on its own, an empty ledger switched on has nothing to hold
    fresh = Ledger.memory()
    st = TripStore(fresh, (), book=home.settings["on"].travellers)
    assert lifecycle.note_airtrail(st, fresh, on=True, today=today) == "on"
    assert fresh.kv.get("airtrail") == "on" and st.hold_past(today=today) == 0


def test_the_same_state_twice_changes_nothing(home, monkeypatch):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    today = day(out) + DAY
    on = home.run("on", at=home.noon(today))
    assert on.trips.item(out.id).sync_hold == "past"
    settled: list[int] = []

    def counted(self, item_id):
        settled.append(item_id)
        raise AssertionError("settle ran")

    def twice(airtrail: str, when: dt.date) -> None:
        """A run that finds the flag as it is settles nothing and changes nothing; so does a
        second process that read it before the first switched it, once it holds the lock."""
        before = state(home)
        with monkeypatch.context() as m:
            m.setattr(TripStore, "settle", counted)
            home.run(airtrail, at=home.noon(when))
            other = Ledger.open(home.settings["on"].ledger_path)
            try:
                stale = iter(["off" if airtrail == "on" else "on"])
                real = other.kv.get
                m.setattr(other.kv, "get", lambda k, d=None: next(stale, None) or real(k, d))
                st = TripStore(other, (), book=home.settings["on"].travellers)
                assert lifecycle.note_airtrail(st, other, on=airtrail == "on", today=when) == "same"
            finally:
                other.close()
        assert settled == [] and state(home) == before

    # on twice, the next day too: the flight that leaves meanwhile is not held
    twice("on", today)
    twice("on", day(back) + DAY)
    assert on.trips.item(back.id).sync_hold is None
    # off twice
    assert home.run("off", at=home.noon(today)).trips.item(back.id).sync_state == "off"
    twice("off", today)


def test_a_flight_whose_key_is_taken_is_kept_out_of_the_flight_log_and_the_switch_goes_on(
    home, monkeypatch
):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    settle = TripStore.settle
    refused: set[int] = set()

    def refusing(self, item_id):
        if item_id in refused:
            raise KeyCollision(item_id, out.id)
        return settle(self, item_id)

    monkeypatch.setattr(TripStore, "settle", refusing)

    def held(ti: TripItem) -> tuple:
        return home.led.kv.get("airtrail"), st.item(ti.id).sync_state, st.item(ti.id).sync_hold

    st = TripStore(home.led, (), book=home.settings["on"].travellers)
    # switched on with the way out refused, a stale state on it as one settled before could
    # leave: it is held and off, so no pass sends it; the way back is settled as ever
    home.led.trip_items.update(out.id, sync_state="pending")
    refused.add(out.id)
    home.run("on", at=home.noon(day(out) + DAY))
    assert held(out) == ("on", "off", "past") and held(back) == ("on", "pending", None)
    # enrolled with the way out refused: it stays held and off, and is not counted
    assert lifecycle.enroll(st, since=day(out), apply=True) == 0
    assert held(out) == ("on", "off", "past")
    refused.clear()
    assert lifecycle.enroll(st, since=day(out), apply=True) == 1
    assert held(out) == ("on", "pending", None)
    # switched off with the way back refused: it reads off, not the pending it held
    refused.add(back.id)
    home.run("off", at=home.noon(day(out) + DAY))
    assert held(back) == ("off", "off", None) and held(out) == ("off", "off", None)


def test_a_flight_gone_while_the_switch_walks_the_flights_does_not_stop_it(home, monkeypatch):
    """Settling one flight can take another booking into it (a draft it completes), so a row
    the switch listed before it began may be gone by its turn: it is passed over, and the switch
    goes on and settles the rest."""
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    settle = TripStore.settle
    gone: list[int] = []

    def taking_one_in(self, item_id):
        if not gone:  # the first flight settled takes the other one in
            gone.append(back.id if item_id == out.id else out.id)
            self.led.trip_items.delete(gone[0])
        return settle(self, item_id)

    monkeypatch.setattr(TripStore, "settle", taking_one_in)
    on = home.run("on", at=home.sent_at(ROUND_TRIP))
    (left,) = [f for f in (out, back) if f.id not in gone]
    assert home.led.kv.get("airtrail") == "on" and on.trips.item(gone[0]) is None
    assert on.trips.item(left.id).sync_state == "pending"


def test_a_switch_is_noted_in_the_log_file_alone(home, caplog):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    caplog.set_level(logging.INFO, logger="tripbox.lifecycle")

    def noted() -> list[tuple]:
        found = [
            (r.levelname, r.getMessage(), getattr(r, "file_only", False))
            for r in caplog.records
            if r.name == "tripbox.lifecycle"
        ]
        caplog.clear()
        return found

    home.run("on", at=home.noon(day(out) + DAY))
    said = "the flight log is switched on: 1 past flights held out, 2 flights settled"
    assert noted() == [("INFO", said, True)]
    home.run("on", at=home.noon(day(out) + DAY))
    assert noted() == []
    home.run("off", at=home.noon(day(out) + DAY))
    assert noted() == [("INFO", "the flight log is switched off: 2 flights settled", True)]


def cached(home: Home) -> int:
    """How many of the flight log's ids the ledger's reference cache holds."""
    return home.led.conn.execute("SELECT COUNT(*) FROM ref_cache").fetchone()[0]


def test_another_address_drops_the_cached_ids_and_the_same_one_keeps_them(home, caplog):
    """The ids the reference cache holds are one AirTrail's own. The first address a context
    notes is kept and nothing is dropped; the same address spelled another way is the same
    address; at another one the cache is emptied, noted in the log file alone."""
    url = FLIGHT_LOG["TRIPBOX_AIRTRAIL_URL"]
    scheme, host = url.split("://")
    name = host.split(":")[0]
    digest = lifecycle.address_digest
    seeded = cached(home)
    assert seeded and home.led.kv.get("airtrail_address") is None
    caplog.set_level(logging.INFO, logger="tripbox.lifecycle")
    home.run("on", at=home.sent_at(ROUND_TRIP))
    assert home.led.kv.get("airtrail_address") == digest(url) and cached(home) == seeded
    # a trailing slash, the host's case, a login and a query: the same address
    for same in (url + "/", f"{scheme}://{host.upper()}", f"{scheme}://someone@{host}/?page=1"):
        assert digest(same) == digest(url), same
        assert lifecycle.note_address(home.led, same) is False and cached(home) == seeded
    # the scheme's own port, written out or not
    assert digest(f"http://{name}") == digest(f"http://{name}:80/")
    assert digest(f"https://{name}") == digest(f"https://{name.upper()}:443")
    # another scheme, port, host or path is another address
    others = (f"https://{host}", f"{scheme}://{name}:3001", ELSEWHERE, url + "/log")
    assert len({digest(url), *map(digest, others)}) == 5
    assert digest(url + "/log/") == digest(url + "/log")
    # an address that does not read as one is still an address of its own
    unread = (f"{scheme}://{name}:port", f"{scheme}://[{name}")
    assert len({digest(url), *map(digest, unread)}) == 3
    # a context built for a flight log elsewhere: the cached ids are dropped, once
    caplog.clear()
    moved = Settings.from_env({**home.environ["on"], "TRIPBOX_AIRTRAIL_URL": ELSEWHERE})
    for _ in range(2):
        with util.clock_at(home.sent_at(ROUND_TRIP)):
            build_context(moved, ledger=home.led, with_model=False, with_notifier=False)
        assert cached(home) == 0 and home.led.kv.get("airtrail_address") == digest(ELSEWHERE)
    noted = [
        (r.levelname, r.getMessage(), getattr(r, "file_only", False))
        for r in caplog.records
        if r.name == "tripbox.lifecycle"
    ]
    said = f"the flight log's address changed: {seeded} cached ids dropped"
    assert noted == [("INFO", said, True)]
    # with no flight log kept, no address is noted and nothing is dropped
    home.led.refcache.put("airport", "one", {"id": 1})
    home.run("off", at=home.sent_at(ROUND_TRIP))
    assert cached(home) == 1 and home.led.kv.get("airtrail_address") == digest(ELSEWHERE)


def test_forget_unlinks_every_flight_holds_the_past_and_binds_anew(home):
    """Another flight log is to be kept: every flight linked to the one the ledger knew is
    unlinked, the past ones are held back, the ledger is bound to nobody and its cached ids are
    dropped; the next pass binds it to the flight log then set up, and adopts what that holds."""
    on = home.run("on", at=home.sent_at(ROUND_TRIP))
    st = on.trips
    out, back = by_day(home.file(on, ROUND_TRIP))
    late = by_day(home.file(on, LATE))
    # one of the late flights was deleted in the flight log by hand, and is held out for it
    gone = late[0]
    home.log.deleted_by_hand(st.item(gone.id).airtrail_flight_id)
    st.set_status(gone.id, "cancelled")
    with util.clock_at(home.sent_at(LATE)):
        assert sync(on).done == {"detach": 1}
    assert st.item(gone.id).sync_hold == "deleted"
    linked = [ti for ti in home.everything() if ti.airtrail_flight_id is not None]
    assert {out.id, back.id} <= {ti.id for ti in linked} and gone.id not in ids(linked)
    assert all(ti.told is not None and ti.sync_state == "synced" for ti in linked)
    today = day(out) + DAY
    past = [ti for ti in linked if day(ti) < today]
    upcoming = [ti for ti in linked if day(ti) >= today]
    assert ids(past) == [out.id] and back.id in ids(upcoming)
    counts = (len(linked), len(upcoming), len(past))
    assert home.led.kv.get("airtrail_user") == MEMORY_USER_ID and cached(home)
    home.led.kv.set("airtrail_key_problem", "other_log")
    # the old flight log's outage, its alert and a rejected key: its own, not the next one's
    old_log = ("airtrail_down_since", "airtrail_alerted", "airtrail_key_rejected")
    for key in old_log:
        home.led.kv.set(key, util.now_iso())
    # counted, and nothing changes
    before, seeded = state(home), cached(home)
    assert lifecycle.forget(st, home.led, today=today, apply=False) == counts
    assert state(home) == before and cached(home) == seeded
    assert home.led.kv.get("airtrail_user") == MEMORY_USER_ID
    assert all(home.led.kv.get(key) for key in old_log)
    # applied
    held_there = home.log.flights
    home.log.requests.clear()
    with util.clock_at(home.noon(today)):
        assert lifecycle.forget(st, home.led, today=today, apply=True) == counts
    for ti in home.everything():
        assert (ti.airtrail_flight_id, ti.told, ti.sync_error, ti.sync_claimed_at) == (
            None,
            None,
            None,
            None,
        ), ti.id
    assert (st.item(out.id).sync_state, st.item(out.id).sync_hold) == ("off", "past")
    assert [(st.item(i).sync_state, st.item(i).sync_hold) for i in ids(upcoming)] == [
        ("pending", None)
    ] * len(upcoming)
    assert (st.item(gone.id).sync_state, st.item(gone.id).sync_hold) == ("off", "deleted")
    assert not home.led.kv.get("airtrail_user") and not home.led.kv.get("airtrail_key_problem")
    assert not any(home.led.kv.get(key) for key in old_log)
    assert cached(home) == 0 and home.log.requests == [] and home.log.flights == held_there
    assert lifecycle.forget(st, home.led, today=today, apply=True) == (0, 0, 0)
    # the past one goes only on request
    assert lifecycle.enroll(st, since=day(out), apply=False) == 1
    # the flight log now set up is another user's, and holds the upcoming flights: the pass
    # binds the ledger to that user, adopts them and creates nothing
    another = Profile("another user", "apiKey", frozenset(REQUIRED_SCOPES), frozenset(), True)
    home.log.answers_as(another)
    with util.clock_at(home.noon(today)):
        assert sync(on).done == {"adopt": len(upcoming)}
    assert home.led.kv.get("airtrail_user") == another.user_id
    assert sent(home.log) == [] and home.log.flights == held_there
    there = {f["id"] for f in held_there}
    assert {st.item(i).airtrail_flight_id for i in ids(upcoming)} <= there
    assert [st.item(i).sync_state for i in ids(upcoming)] == ["synced"] * len(upcoming)


def ids(items: list[TripItem]) -> list[int]:
    return [ti.id for ti in items]


def test_enroll_from_the_command_line(home, monkeypatch, capsys):
    ctx = home.run("off", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(ctx, ROUND_TRIP))
    for k, v in home.environ["on"].items():
        monkeypatch.setenv(k, v)
    since = day(out).isoformat()
    today = home.noon(day(out) + DAY)
    st = TripStore(home.led, (), book=home.settings["on"].travellers)
    # a --since that is not a day (a word, a month, a day no month has) or none: usage, before
    # anything else
    for bad in (["--since", "soon"], ["--since", since[:7]], ["--since", since[:8] + "32"], []):
        assert cli.main(["airtrail", "enroll", *bad]) == 2
        assert ENROLL_USAGE in capsys.readouterr().err.splitlines()
    assert home.led.kv.get("airtrail") is None
    # counted: the flight that had left when AirTrail was switched on
    with util.clock_at(today):
        assert cli.main(["airtrail", "enroll", "--since", since]) == 0
    said = f"enroll: 1 past flights since {since} would be queued; run again with --apply"
    assert capsys.readouterr().out.splitlines() == [said]
    assert (st.item(out.id).sync_hold, st.item(back.id).sync_state) == ("past", "pending")
    # queued
    with util.clock_at(today):
        assert cli.main(["airtrail", "enroll", "--since", since, "--apply"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"enroll: 1 past flights since {since} queued"]
    assert (st.item(out.id).sync_hold, st.item(out.id).sync_state) == (None, "pending")
    # AirTrail off: its off line
    for k in FLIGHT_LOG:
        monkeypatch.delenv(k)
    with util.clock_at(today):
        assert cli.main(["airtrail", "enroll", "--since", since]) == 2
    err = capsys.readouterr().err.splitlines()
    assert off_line(Settings.from_env().feature("airtrail")) in err


def test_forget_from_the_command_line(home, monkeypatch, capsys):
    on = home.run("on", at=home.sent_at(ROUND_TRIP))
    out, back = by_day(home.file(on, ROUND_TRIP))
    for k, v in home.environ["on"].items():
        monkeypatch.setenv(k, v)
    today = home.noon(day(out) + DAY)
    st = TripStore(home.led, (), book=home.settings["on"].travellers)
    before = state(home)
    with util.clock_at(today):
        assert cli.main(["airtrail", "forget"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "forget: 2 flights are linked to the flight log tripbox knew: 1 upcoming, 1 past;"
        " run again with --apply"
    ]
    assert state(home) == before and home.led.kv.get("airtrail_user") == MEMORY_USER_ID
    with util.clock_at(today):
        assert cli.main(["airtrail", "forget", "--apply"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "forget: 2 flights unlinked; of the 1 upcoming, yours are looked for in the flight log"
        " now set up and added there if missing; 1 past are held back (tripbox airtrail enroll"
        " --since DAY --apply sends them)"
    ]
    assert (st.item(out.id).sync_hold, st.item(back.id).sync_state) == ("past", "pending")
    assert [st.item(i).airtrail_flight_id for i in (out.id, back.id)] == [None, None]
    assert not home.led.kv.get("airtrail_user")
    # AirTrail off: its off line
    for k in FLIGHT_LOG:
        monkeypatch.delenv(k)
    with util.clock_at(today):
        assert cli.main(["airtrail", "forget", "--apply"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert off_line(Settings.from_env().feature("airtrail")) in err


def pages(home: Home) -> list[str]:
    """What the list and every trip page say in their check boxes, amber chips, flight log lines
    and menus (`Shown`), each page asserted to render."""
    c = home_client(home)
    st = TripStore(home.led, (), book=home.settings["on"].travellers)
    out: list[str] = []
    for path in ["/?past=all", *(f"/trips/{t.id}" for t in st.all_trips())]:
        r = c.get(path)
        assert r.status_code == 200, path
        out += Shown.of(r.text).said
    return out


def home_client(home: Home) -> TestClient:
    """The page over the installation's ledger, as its own container serves it (at the
    default address, on localhost, at the host's root)."""
    s = dataclasses.replace(home.settings["off"], tripbox_auth="none")
    app = create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)
    return TestClient(app, base_url=ORIGIN)


# Elements that never close.
VOID = frozenset(
    ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr")
)


class Shown(HTMLParser):
    """What a page shows in its check boxes (`div.check`), its amber chips, its lines about the
    flight log (`.sync`) and its menus (`details.menu`, `details.item-menu`): the text, and each
    attribute a reader is shown there (a confirmation's question, a label, a title)."""

    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []
        self.open: list[str] = []  # the tags open, outermost first
        self.inside: list[int] = []  # how deep each region read here starts

    @classmethod
    def of(cls, page: str) -> Shown:
        found = cls()
        found.feed(page)
        return found

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        starts = (
            (tag == "div" and "check" in classes)
            or "sync" in classes
            or (tag == "span" and "chip-amber" in classes)
            or (tag == "details" and classes & {"menu", "item-menu"})
        )
        if tag not in VOID:
            self.open.append(tag)
            if starts:
                self.inside.append(len(self.open))
        if self.inside or starts:
            self.said += [v for k, v in attrs if k in ("data-confirm", "aria-label", "title") and v]

    def handle_endtag(self, tag):
        if tag not in self.open:
            return
        while self.open:
            closed = self.open.pop()
            while self.inside and self.inside[-1] > len(self.open):
                self.inside.pop()
            if closed == tag:
                break

    def handle_data(self, data):
        if self.inside and data.strip():
            self.said.append(" ".join(data.split()))
