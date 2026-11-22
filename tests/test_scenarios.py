# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Where a booking in doubt is filed, and what reaches the flight log. Every reason an email once
had to keep a booking out of the trips is a case here: one booking of the corpus's spoiled by one
value (a field blanked, an airport code replaced by a city the corpus reads for one, a day moved, a
confidence lowered, a sender no list vouches for), or the flight log it would be sent to spoiled
(one that refuses it, does not know one of its airports, does not answer, or is only shown it in a
dry run). Each case's email goes through the pipeline twice, into a fresh ledger each time: with a
flight log kept, an AirTrail held in memory that the pass after each message brings in step, and
with none. Then the corpus itself is filed with a flight log, and one booking is taken through
every way a draft meets its emails again. Every booking, airport, city and traveller is read from
the corpus at run time."""

import contextlib
import dataclasses
import datetime as dt
import logging
import re
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from helpers.flightlog import Scripted, named, seed
from test_checks import (
    a_companion_alone,
    book,
    booking,
    day,
    edit,
    moved,
    plain,
    stamps,
    strangers_alone,
    the_corpus,
)
from test_filed import (
    UNSURE,
    city_flight,
    deliver,
    everything,
    filing,
    owners_flight,
)
from test_store_actions import an_hour_later, nameless

from tripbox import corpus, scorecard, util
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.mail.normalize import normalize
from tripbox.pipeline import run as runmod
from tripbox.pipeline.run import Context, build_context, process_raw, sync
from tripbox.trips.store import NO_OWNER, TripItem, stamp
from tripbox.validate import checks, rules
from tripbox.validate.keys import is_draft
from tripbox.validate.refdata import Airports
from tripbox.write.airtrail import MEMORY_USER_ID
from tripbox.write.lifecycle import note_airtrail
from tripbox.write.sync import Syncer

AIRPORTS = Airports()
# What the log of a message may say became of a booking.
OUTCOMES = ("filed", "updated", "duplicate", "ignored", "failed")
# The checks only a flight log opens: with none kept they stay shut.
FLIGHT_LOG_ONLY = ("owner_unknown", "unverified_sender", "airtrail_airport", "airtrail_refused")
# A flight log set up in the settings (never reached: the travellers file names no owner).
FLIGHT_LOG = {
    "TRIPBOX_AIRTRAIL_URL": "http://flightlog.example:3000",
    "TRIPBOX_AIRTRAIL_API_KEY": "placeholder",
    "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
}
WOULD = "dry run: would "  # how a dry run logs the request it does not send


def without_a_flight_log(found) -> tuple[str, ...]:
    """The checks of `found` that are open with no flight log kept."""
    return tuple(code for code in found if code not in FLIGHT_LOG_ONLY)


def owner_aboard(body: dict) -> bool:
    """Whether a create's or an update's body carries the owner's user id among its passengers."""
    return any(p.get("userId") == MEMORY_USER_ID for p in body.get("passengers") or [])


# ----------------------------------------------------------------------------- the cases
@dataclasses.dataclass(frozen=True)
class Step:
    """What happens next to a booking filed with a flight log kept (`then`) or with none
    (`then_off`), and its open checks and sync state after it."""

    what: str
    do: Callable[[Context, TripItem], object]
    checks: tuple[str, ...]
    sync: str


@dataclasses.dataclass(frozen=True)
class Case:
    """One way a booking is in doubt: a word for it, the email about a booking of the corpus's
    (`res`, delivered as `how` says), the flight log spoiled by `spoil` before it arrives, and
    where it is filed: in a `trip` or `unsorted`, under a `real` key or a `draft`, with `checks`
    open and the sync state `on` while a flight log is kept, and `off` while none is, when the
    checks only a flight log opens stay shut. `owner`: the travellers file names an owner (with
    none, a flight log set up is not on)."""

    name: str
    entry: corpus.Entry
    res: dict
    place: str
    key: str
    checks: tuple[str, ...]
    on: str
    off: str = "off"
    how: dict = dataclasses.field(default_factory=dict)
    status: str = "confirmed"
    spoil: Callable[[Context, dict], None] | None = None
    then: tuple[Step, ...] = ()
    then_off: tuple[Step, ...] = ()
    owner: bool = True


def looked(ctx: Context, ti: TripItem) -> None:
    ctx.trips.looks_right(ti.id)


def on_it(ctx: Context, ti: TripItem) -> None:
    ctx.trips.owner_answer(ti.id, True)


def not_me(ctx: Context, ti: TripItem) -> None:
    ctx.trips.owner_answer(ti.id, False)


def a_pass(ctx: Context, ti: TripItem) -> None:
    sync(ctx)


def its_next_try(ctx: Context, ti: TripItem) -> None:
    """A pass once the backoff the flight waits out is over."""
    assert ti.sync_after is not None
    ctx.syncer.run_pass(now=util.parse_iso(ti.sync_after))


def asks_nothing(ctx: Context, ti: TripItem) -> None:
    """Another pass: in a dry run a flight whose request is logged is not asked about again."""
    before = list(ctx.airtrail.requests)
    sync(ctx)
    assert ctx.airtrail.requests == before


def learns_its_airports(ctx: Context, ti: TripItem) -> None:
    """The flight log comes to know every airport the corpus names; the next try finds them."""
    seed(ctx.ledger, the_corpus())
    its_next_try(ctx, ti)


def switched_on(ctx: Context, ti: TripItem) -> None:
    """A flight log is switched on, its first day today."""
    note_airtrail(ctx.trips, ctx.ledger, on=True, today=util.now().date())


def no_answer_to_give(ctx: Context, ti: TripItem) -> None:
    """With no owner named there is no answer to offer, and the store refuses one."""
    kept = ctx.trips.airtrail_on()
    assert checks.actions(ti.checks, hold=ti.sync_hold, flight_log=kept) == frozenset()
    with pytest.raises(ValueError, match=re.escape(NO_OWNER)):
        ctx.trips.owner_answer(ti.id, True)


def destination_typed_as(code: str) -> Callable[[Context, TripItem], None]:
    def typed(ctx: Context, ti: TripItem) -> None:
        ctx.trips.edit_item(ti.id, {"destination": code})

    return typed


def unknown_to_the_flight_log(code: str) -> Callable[[Context, dict], None]:
    """The flight log knows no airport by `code`."""

    def spoil(ctx: Context, res: dict) -> None:
        ctx.ledger.refcache.put("airport", code, {})

    return spoil


def refusing(ctx: Context, res: dict) -> None:
    ctx.airtrail.fail_next(422, on="create")


def down(ctx: Context, res: dict) -> None:
    ctx.airtrail.fail_next(0)


def dry(ctx: Context, res: dict) -> None:
    ctx.syncer.dry_run = True


def a_code_no_airport_has() -> str:
    """Three capital letters that are no airport's code in the bundled table."""
    letters = [chr(c) for c in range(ord("Q"), ord("Z") + 1)]
    for a in letters:
        for b in letters:
            for c in letters:
                if AIRPORTS.airport(a + b + c) is None:
                    return a + b + c
    raise AssertionError("every code is an airport's")


def in_its_zone(code: str, *, besides: tuple[str, ...]) -> str:
    """Another airport the corpus names, in the zone of `code`, that is none of `besides`."""
    zone = AIRPORTS.airport(code)["timezone"]
    airports, _ = named(the_corpus())
    (other, *_) = [
        a
        for a in airports
        if a not in besides and (AIRPORTS.airport(a) or {}).get("timezone") == zone
    ]
    return other


def the_undated_flight() -> tuple[corpus.Entry, dict]:
    """The flight the corpus files with no date, as its email says it."""
    for entry in the_corpus().entries:
        if any(i["booking"] and i["booking"]["trip"] is None for i in entry.outcome["items"]):
            (res,) = [r for r in entry.truth["reservations"] if r["kind"] == "flight"]
            return entry, plain(res)
    raise AssertionError("the corpus files no flight with no date")


def cases() -> list[Case]:
    """Every case, its expectations stated here, its values read from the corpus."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    dep, arr = leg["departure_local"], leg["arrival_local"]
    city = city_flight()[2]  # a city the corpus reads for an airport: no airport code
    unknown = a_code_no_airport_has()
    ahead = (day(dep) - corpus.sent(entry).date()).days
    past = -(ahead + rules.PAST_SLACK.days + 7)
    far = rules.FUTURE_LIMIT.days + 7 - ahead
    companions = booking("flight", a_companion_alone)
    strangers = booking("flight", strangers_alone)
    stay_entry, stay = booking("lodging")
    car_entry, car = booking("car")
    train_entry, train = booking("train")
    undated = the_undated_flight()
    to = leg["destination"]
    elsewhere = in_its_zone(to, besides=(leg["origin"], to))
    lookup = Step("looks right", looked, (), "pending")
    sent = Step("the next pass", a_pass, (), "synced")
    stays_off = Step("the next pass", a_pass, (), "off")

    def unmoved(*held: str) -> Step:  # a look closes no hard check
        return Step("looks right", looked, held, "blocked")

    def flight_case(name, res, place, key, found, **kw) -> Case:
        return Case(name, entry, res, place, key, found, kw.pop("on", "blocked"), **kw)

    return [
        flight_case(
            "no airline code",
            edit(flight, {"provider": None}, carrier_iata=None),
            "trip",
            "draft",
            ("carrier",),
            then=(unmoved("carrier"),),
        ),
        flight_case(
            "no flight number",
            edit(flight, flight_number=None),
            "trip",
            "draft",
            ("flight_number",),
            then=(unmoved("flight_number"),),
        ),
        flight_case(
            "a city for the origin, its offset printed",
            edit(flight, origin=city),
            "trip",
            "draft",
            ("origin",),
            then=(unmoved("origin"),),
        ),
        flight_case(
            "a city for the origin and no offset: no zone for the departure",
            edit(flight, origin=city, departure_offset=None),
            "trip",
            "draft",
            ("origin", "departure"),
        ),
        flight_case(
            "a code no airport has for the origin",
            edit(flight, origin=unknown),
            "trip",
            "draft",
            ("origin",),
        ),
        flight_case(
            "a city for the destination",
            edit(flight, destination=city),
            "trip",
            "real",
            ("destination",),
            then=(
                unmoved("destination"),
                Step("its airport typed in", destination_typed_as(to), (), "pending"),
                sent,
            ),
        ),
        flight_case(
            "a code no airport has for the destination",
            edit(flight, destination=unknown),
            "trip",
            "real",
            ("destination",),
        ),
        flight_case(
            "no departure and no arrival: no day",
            edit(flight, departure_local=None, arrival_local=None),
            "unsorted",
            "draft",
            ("departure",),
            then=(unmoved("departure"),),
        ),
        flight_case(
            "a departure that is no time and no arrival: no day",
            edit(flight, departure_local="to be announced", arrival_local=None),
            "unsorted",
            "draft",
            ("departure",),
        ),
        flight_case(
            "a departure printed as its day alone",
            edit(flight, departure_local=dep[:10]),
            "trip",
            "real",
            ("departure",),
        ),
        flight_case(
            "long before the email",
            edit(flight, departure_local=moved(dep, past), arrival_local=moved(arr, past)),
            "trip",
            "real",
            ("date_past",),
            then=(lookup, sent),
        ),
        flight_case(
            "far after the email",
            edit(flight, departure_local=moved(dep, far), arrival_local=moved(arr, far)),
            "trip",
            "real",
            ("date_far",),
            then=(lookup, sent),
        ),
        flight_case(
            "landing before it leaves",
            edit(flight, arrival_local=moved(dep, -1)),
            "trip",
            "real",
            ("arrival_before_departure",),
            then=(unmoved("arrival_before_departure"),),
        ),
        flight_case(
            "a day too long",
            edit(flight, arrival_local=moved(arr, 1)),
            "trip",
            "real",
            ("long_leg",),
            then=(lookup, sent),
        ),
        flight_case(
            "nobody named, and the owner was on it",
            nameless(flight),
            "trip",
            "real",
            ("owner_unknown",),
            then=(
                unmoved("owner_unknown"),  # a look never answers who was on it
                Step("I was on this flight", on_it, (), "pending"),
                sent,
            ),
        ),
        flight_case(
            "nobody named, and the owner was not on it",
            nameless(flight),
            "trip",
            "real",
            ("owner_unknown",),
            then=(Step("not me", not_me, (), "off"), stays_off),
        ),
        Case(
            "only a companion named",
            *companions,
            "trip",
            "real",
            (),
            "off",
            then=(stays_off,),
        ),
        Case("only strangers named", *strangers, "trip", "real", (), "off", then=(stays_off,)),
        Case(
            "a stay with no name",
            stay_entry,
            edit(stay, {"property_name": None, "provider": None}),
            "trip",
            "real",
            ("missing_name",),
            "off",
            then=(Step("looks right", looked, (), "off"),),
        ),
        Case(
            "a stay with no name and no code",
            stay_entry,
            edit(stay, {"property_name": None, "provider": None, "confirmation_code": None}),
            "trip",
            "draft",
            ("missing_name",),
            "off",
        ),
        Case(
            "no check-in",
            stay_entry,
            edit(stay, {"check_in": None}),
            "unsorted",
            "real",
            ("check_in",),
            "off",
        ),
        Case(
            "no check-in and no code",
            stay_entry,
            edit(stay, {"check_in": None, "confirmation_code": None}),
            "unsorted",
            "draft",
            ("check_in",),
            "off",
        ),
        Case(
            "no check-out",
            stay_entry,
            edit(stay, {"check_out": None}),
            "trip",
            "real",
            ("check_out",),
            "off",
        ),
        Case(
            "a check-out before the check-in",
            stay_entry,
            edit(stay, {"check_out": moved(stay["check_in"], -1)}),
            "trip",
            "real",
            ("check_out",),
            "off",
        ),
        Case(
            "no rental company",
            car_entry,
            edit(car, {"provider": None}),
            "trip",
            "real",
            ("missing_name",),
            "off",
        ),
        Case(
            "no rental company and no code",
            car_entry,
            edit(car, {"provider": None, "confirmation_code": None}),
            "trip",
            "draft",
            ("missing_name",),
            "off",
        ),
        Case(
            "no pick-up",
            car_entry,
            edit(car, {"pickup_time": None}),
            "unsorted",
            "real",
            ("pickup_time",),
            "off",
        ),
        Case(
            "no drop-off",
            car_entry,
            edit(car, {"dropoff_time": None}),
            "trip",
            "real",
            ("dropoff_time",),
            "off",
        ),
        Case(
            "a drop-off before the pick-up",
            car_entry,
            edit(car, {"dropoff_time": moved(car["pickup_time"], -1)}),
            "trip",
            "real",
            ("dropoff_time",),
            "off",
        ),
        Case(
            "no arrival station",
            train_entry,
            edit(train, destination=None),
            "trip",
            "real",
            ("stations",),
            "off",
        ),
        Case(
            "no departure station",
            train_entry,
            edit(train, origin=None),
            "trip",
            "draft",
            ("stations",),
            "off",
        ),
        Case(
            "a train with no departure",
            train_entry,
            edit(train, departure_local=None, arrival_local=None),
            "unsorted",
            "draft",
            ("departure",),
            "off",
        ),
        flight_case(
            "a flight hard to read",
            flight,
            "trip",
            "real",
            ("low_confidence",),
            how={"confidence": UNSURE},
            then=(lookup, sent),
        ),
        Case(
            "a stay hard to read",
            stay_entry,
            stay,
            "trip",
            "real",
            ("low_confidence",),
            "off",
            how={"confidence": UNSURE},
            then=(Step("looks right", looked, (), "off"),),
        ),
        flight_case(
            "the owner's flight from a sender no list vouches for",
            flight,
            "trip",
            "real",
            ("unverified_sender",),
            how={"unvouched": True},
            then=(lookup, sent),
            # the finding is kept with no flight log, and is a check once one is switched on
            then_off=(
                Step("a flight log switched on", switched_on, ("unverified_sender",), "blocked"),
            ),
        ),
        flight_case(
            "a cancellation for a flight never filed",
            edit(flight, {"status": "cancelled"}),
            "trip",
            "real",
            ("unknown_cancellation",),
            on="off",  # a cancelled flight that was never sent is never sent
            how={"doc": "cancellation"},
            status="cancelled",
            then=(Step("looks right", looked, (), "off"), stays_off),
        ),
        Case(
            "a cancellation for a stay never filed",
            stay_entry,
            edit(stay, {"status": "cancelled"}),
            "trip",
            "real",
            ("unknown_cancellation",),
            "off",
            how={"doc": "cancellation"},
            status="cancelled",
        ),
        Case(
            "a flight the corpus files with no date",
            *undated,
            "unsorted",
            "draft",
            ("departure",),
            "blocked",
        ),
        flight_case(
            "the flight log does not know its destination",
            flight,
            "trip",
            "real",
            ("airtrail_airport",),
            spoil=unknown_to_the_flight_log(to),
            then=(
                unmoved("airtrail_airport"),
                Step(
                    "a try while it is still unknown",
                    its_next_try,
                    ("airtrail_airport",),
                    "blocked",
                ),
                Step("the flight log learns it", learns_its_airports, (), "synced"),
            ),
        ),
        flight_case(
            "the flight log does not know its destination, which is changed",
            flight,
            "trip",
            "real",
            ("airtrail_airport",),
            spoil=unknown_to_the_flight_log(to),
            then=(
                Step("another airport typed in", destination_typed_as(elsewhere), (), "pending"),
                sent,
            ),
        ),
        flight_case(
            "the flight log refuses it",
            flight,
            "trip",
            "real",
            ("airtrail_refused",),
            spoil=refusing,
            then=(lookup, sent),
        ),
        flight_case(
            "the flight log does not answer",
            flight,
            "trip",
            "real",
            (),
            on="pending",
            spoil=down,
            then=(
                Step("a pass before its backoff ends", a_pass, (), "pending"),
                Step("its next try", its_next_try, (), "synced"),
            ),
        ),
        flight_case(
            "a dry run",
            flight,
            "trip",
            "real",
            (),
            on="pending",
            spoil=dry,
            then=(Step("another pass", asks_nothing, (), "pending"),),
        ),
        flight_case(
            "no origin, no destination and no departure",
            edit(flight, origin=None, destination=None, departure_local=None, arrival_local=None),
            "unsorted",
            "draft",
            ("origin", "destination", "departure"),
        ),
        flight_case(
            "nobody named, and no owner in the travellers file",
            nameless(flight),
            "trip",
            "real",
            (),
            on="off",  # a flight log needs an owner: set up, it is not on
            owner=False,
            then=(Step("I was on this flight", no_answer_to_give, (), "off"),),
        ),
    ]


# ----------------------------------------------------------------------------- the ledgers
def no_owner_named(base: Path) -> Path:
    """A config directory whose travellers file names the corpus's companions and no owner."""
    there = base / "no-owner"
    there.mkdir()
    travellers = yaml.safe_load((the_corpus().root / corpus.TRAVELLERS).read_text("utf-8"))
    del travellers["owner"]
    (there / "travellers.yaml").write_text(yaml.safe_dump(travellers), encoding="utf-8")
    return there


@contextlib.contextmanager
def set_up_with_no_owner(config_dir: Path) -> Iterator[Context]:
    """A pipeline context over an empty ledger, its settings the corpus's with a flight log set
    up, under the travellers file in `config_dir`, which names no owner."""
    c = the_corpus()
    with tempfile.TemporaryDirectory(prefix="tripbox-test-") as tmp:
        base = Path(tmp)
        s = Settings.from_env(
            {
                **c.settings,
                "TRIPBOX_CONFIG_DIR": str(config_dir),
                "TRIPBOX_DATA_DIR": str(base / "data"),
                "TRIPBOX_ARCHIVE_DIR": str(base / "archive"),
                "TRIPBOX_LOGS_DIR": str(base / "logs"),
                "TRIPBOX_AUTH": "none",
                **FLIGHT_LOG,
            }
        )
        assert s.feature("airtrail").state == "invalid"
        with corpus.process_as(s):
            led = Ledger.memory()
            try:
                yield build_context(s, ledger=led, with_model=False, with_notifier=False)
            finally:
                led.close()


def ledger_for(case: Case, kept: bool, nobody: Path):
    """The context a case is filed in: with a flight log kept or with none."""
    if case.owner:
        return filing(flight_log=kept)
    return set_up_with_no_owner(nobody) if kept else filing(flight_log=False, config_dir=nobody)


def where(ctx: Context, ti: TripItem) -> tuple[str, str]:
    """Where a booking is filed: in a `trip` or `unsorted`; under a `real` key or a `draft`."""
    if ti.trip_id is None:
        assert ti.start_at is None and ti.id in [u.id for u in ctx.trips.unsorted()]
        place = "unsorted"
    else:
        assert ti.id in [x.id for x in ctx.trips.items(ti.trip_id)]
        place = "trip"
    return place, "draft" if is_draft(ti.item_key) else "real"


def in_step(ctx: Context, ti: TripItem, label: tuple) -> None:
    """The flight log holds the booking exactly when it reads `synced`: one flight; else it
    holds nothing. Whatever was asked of it (a refused create too) carried the owner."""
    if ti.sync_state == "synced":
        (held,) = ctx.airtrail.flights
        assert held["id"] == ti.airtrail_flight_id, label
    else:
        assert ctx.airtrail.flights == [], label
    assert all(owner_aboard(r[-1]) for r in ctx.airtrail.sent), label


# ----------------------------------------------------------------------------- the tests
def test_every_former_review_reason_is_filed_as_designed(monkeypatch, tmp_path, caplog):
    """Whatever an email gets wrong, and whatever the flight log does with it, the booking is
    filed: the message is processed, the booking is filed where its fields put it, under the key
    they make, with the checks that say what is wrong, and with a flight log kept nothing reaches
    it until those checks are cleared; with none, the checks only a flight log opens stay shut.
    Then what clears each one, step by step, and what the flight log holds after it."""
    caplog.set_level(logging.INFO, logger="tripbox.sync")
    nobody = no_owner_named(tmp_path)
    for case in cases():
        for kept in (True, False):
            label = (case.name, "with a flight log" if kept else "with none")
            with ledger_for(case, kept, nobody) as ctx:
                assert ctx.trips.airtrail_on() is (kept and case.owner), label
                if kept and case.spoil is not None:
                    case.spoil(ctx, case.res)
                got = deliver(ctx, monkeypatch, case.entry, [case.res], **case.how)
                assert got.status == "processed", label
                (o,) = got.outcomes
                assert (o.action.kind, o.result) == ("file", "filed"), label
                ti = ctx.trips.item(o.trip_item_id)
                assert ti is not None and ti.removed_at is None, label
                assert where(ctx, ti) == (case.place, case.key), label
                found = case.checks if kept else without_a_flight_log(case.checks)
                sync_state = case.on if kept else case.off
                assert (ti.status, ti.checks, ti.sync_state) == (case.status, found, sync_state), (
                    label
                )
                assert (o.checks, o.sync) == (ti.checks, ti.sync_state), label
                words = {r["outcome"] for r in ctx.ledger.conn.execute("SELECT outcome FROM items")}
                assert words <= set(OUTCOMES) and words, label
                if ctx.airtrail is not None:
                    in_step(ctx, ti, label)
                for step in case.then if kept else case.then_off:
                    step.do(ctx, ctx.trips.item(ti.id))
                    after = ctx.trips.item(ti.id)
                    assert (after.checks, after.sync_state) == (step.checks, step.sync), (
                        *label,
                        step.what,
                    )
                    if ctx.airtrail is not None:
                        in_step(ctx, after, (*label, step.what))
    # the one dry run logged the request it would have sent once, over both of its passes
    assert len([r for r in caplog.records if r.getMessage().startswith(WOULD)]) == 1


def test_mail_from_a_stranger_is_still_never_read_and_never_filed(monkeypatch):
    """The email the corpus has sent by nobody the mailbox trusts is rejected before it is read:
    no model is asked, nothing is filed, and a flight log hears nothing of it."""
    (entry,) = [e for e in the_corpus().entries if e.outcome["status"] == "rejected"]

    def no_reading(nm, model):
        raise AssertionError("a rejected email was read")

    monkeypatch.setattr(runmod, "parse_message", no_reading)
    for kept in (True, False):
        with filing(flight_log=kept) as ctx:
            got = process_raw(ctx, entry.raw, source="imap", received=corpus.sent(entry))
            assert (got.status, got.outcomes, got.items) == ("rejected", [], []), kept
            row = ctx.ledger.messages.get(got.message_row_id)
            assert (row["status"], row["parser"]) == ("rejected", None), kept
            assert Path(row["archive_path"]).is_file(), kept  # kept, as all mail is
            assert everything(ctx) == [] and ctx.trips.all_trips() == [], kept
            assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
            if kept:
                assert ctx.airtrail.requests == []


def test_no_draft_path_leaves_two_bookings_or_two_flights(monkeypatch):
    """A flight of the owner's whose first email prints a city for its origin is filed as a
    draft. Taken through every way its emails meet it again — that email read again, a later
    email that completes it, both read again, an edit on the page, both read again after it —
    and, in a second ledger, completed on the page before the later email arrives: after every
    step it is one booking, and the flight log holds at most one flight."""
    entry, flight, city = city_flight()
    first, later = edit(flight, origin=city), flight
    t = stamps(entry, 2)  # after the emails were sent: the owner's edits are newer than both
    leg = flight["segments"][0]

    def one(ctx: Context, step: str) -> TripItem:
        (ti,) = everything(ctx)
        assert len(ctx.airtrail.flights) <= 1, step
        assert ti.sync_state != "synced" or ctx.airtrail.flights[0]["id"] == ti.airtrail_flight_id
        return ti

    with filing() as ctx:
        deliver(ctx, monkeypatch, entry, [first], n=0)
        draft = one(ctx, "filed as a draft")
        assert is_draft(draft.item_key) and draft.sync_state == "blocked"
        assert ctx.airtrail.flights == []
        deliver(ctx, monkeypatch, entry, [first], n=0, force=True)
        assert one(ctx, "its email read again") == draft
        deliver(ctx, monkeypatch, entry, [later], n=1)
        done = one(ctx, "completed by a later email")
        assert (done.id, is_draft(done.item_key), done.sync_state) == (draft.id, False, "synced")
        for n, res in ((0, first), (1, later)):
            deliver(ctx, monkeypatch, entry, [res], n=n, force=True)
            assert one(ctx, f"email {n} read again").id == done.id
        moved_on = {
            "dep_local": an_hour_later(leg["departure_local"]),
            "arr_local": an_hour_later(leg["arrival_local"]),
        }
        edited = ctx.trips.edit_item(done.id, moved_on, at=t[0])
        sync(ctx)
        after = one(ctx, "edited on the page")
        assert (after.id, after.sync_state) == (done.id, "synced")
        assert ctx.airtrail.flights[0]["departure"] == edited.payload["dep_utc"]
        for n, res in ((0, first), (1, later)):
            deliver(ctx, monkeypatch, entry, [res], n=n, force=True)
            again = one(ctx, f"email {n} read again after the edit")
            assert again.payload["dep_local"] == moved_on["dep_local"]
            assert again.sync_state == "synced"
        assert [r[0] for r in ctx.airtrail.sent] == ["create", "update"]
    with filing() as ctx:
        deliver(ctx, monkeypatch, entry, [first], n=0)
        draft = one(ctx, "filed as a draft")
        fixed = ctx.trips.edit_item(draft.id, {"origin": leg["origin"]}, at=t[0])
        assert (fixed.id, fixed.sync_state) == (draft.id, "pending")
        assert not is_draft(fixed.item_key)
        sync(ctx)
        assert one(ctx, "completed on the page").sync_state == "synced"
        got = deliver(ctx, monkeypatch, entry, [later], n=1)
        (o,) = got.outcomes
        assert o.trip_item_id == draft.id, o.result
        assert one(ctx, "the later email after").sync_state == "synced"
        assert [r[0] for r in ctx.airtrail.sent] == ["create"]


def test_the_corpus_with_a_flight_log_sends_only_the_owners_checked_flights():
    """The thirty emails go through the pipeline, in order, into an empty ledger, as `tripbox
    eval --recorded` files them, but with a flight log kept. What each email does to its bookings
    is the same as with none (the plan never asks whether one is kept). After each message's pass
    no flight is left waiting, and the flight log then holds exactly the flights whose booking,
    as the corpus's outcomes leave it, has the owner aboard and no check open — none under a
    renumbered flight's old number — less the owner's flights from the email forwarded from a
    sender no list vouches for: those are held by that check alone, through every later email
    for them, and sent by the next pass once the owner has looked. The owner's flight with no
    date is held by its own check, which a look does not clear. Filing the thirty again adds
    nothing."""
    c = the_corpus()
    # the booking with no date, as the corpus states it: its checks with no flight log
    (undated,) = [
        tuple(i["booking"]["checks"])
        for e in c.entries
        for i in e.outcome["items"]
        if i["booking"] is not None and i["booking"]["trip"] is None
    ]
    # each flight as the corpus's outcomes leave it, by key; a booking an email moves to a key
    # never filed (a renumbered flight) leaves the key it had
    flights: dict[str, dict] = {}
    left: list[str] = []
    for e in c.entries:
        for i in e.outcome["items"]:
            if i["kind"] != "flight" or i["booking"] is None:
                continue
            if i["key"] not in flights and i["result"] != "filed":
                (old,) = [k for k in flights if renumbered(k, i["key"])]
                left.append(code_and_day(old)[0])
                del flights[old]
            flights[i["key"]] = i["booking"]
    assert left, "the corpus renumbers a flight"
    seen: list[tuple[str, str, str | None]] = []
    with tempfile.TemporaryDirectory(prefix="tripbox-test-") as tmp:
        base = Path(tmp)
        s = corpus.settings(
            c, data_dir=base / "data", archive_dir=base / "archive", logs_dir=base / "logs"
        )
        # the emails the gate reads but whose sender no list vouches for, and their flights
        unvouched = []
        for e in c.entries:
            nm = normalize(
                e.raw,
                s.allowed_senders,
                s.tz,
                require_auth=s.require_google_auth,
                trusted_domains=s.mail_trusted_sender_domains,
            )
            if nm.origin_ok and not nm.sender_trusted:
                unvouched.append(e.name)
        # of their flights, the owner's: the flight log hears of these only once the owner looks
        by_sender = sorted(
            {
                i["key"]
                for e in c.entries
                if e.name in unvouched
                for i in e.outcome["items"]
                if i["kind"] == "flight"
                and i["booking"] is not None
                and book().classify(flights[i["key"]]["travellers"]).me_present
            }
        )
        assert len(by_sender) == 2
        # every later email that is for them, after the one that files them
        for_them = [
            e.name for e in c.entries if any(i["key"] in by_sender for i in e.outcome["items"])
        ]
        assert len(for_them) >= 2 and for_them[0] in unvouched
        wanted = {
            code_and_day(k)
            for k, b in flights.items()
            if book().classify(b["travellers"]).me_present and not b["checks"]
            and k not in by_sender
        }
        with corpus.process_as(s):
            led = Ledger.memory()
            ctx = build_context(s, ledger=led, with_model=False, with_notifier=False)
            assert ctx.syncer is None  # the corpus's settings keep no flight log: switch one on
            led.kv.set("airtrail", "on")
            seed(led, c)
            log = Scripted(led)
            st = ctx.trips
            ctx.airtrail = log
            ctx.syncer = Syncer(
                store=st, ledger=led, book=st.book, client=log, refs=None, dry_run=False
            )
            ctx.model = corpus.CorpusAnswers(c, s.llm_model)
            for entry in c.entries:
                res = process_raw(
                    ctx, entry.raw, source="eval", received=corpus.sent(entry), notify=False
                )
                # what the email files is the same as without a flight log; the sync state and
                # the flight log's own checks are asserted below
                diffs = scorecard.compare_outcome(
                    outcome_without_a_flight_log(entry.outcome),
                    outcome_without_a_flight_log(scorecard.outcome_view(res, ctx)),
                )
                assert diffs == (), entry.name
                # the checks the corpus states for each booking, all open with no flight log
                stated = {
                    (i["kind"], i["key"]): tuple(i["booking"]["checks"])
                    for i in entry.outcome["items"]
                    if i["booking"] is not None
                }
                for o in res.outcomes:
                    label = (entry.name, o.action.item.kind, o.result)
                    ti = st.item(o.trip_item_id)
                    assert (o.sync, o.checks) == (ti.sync_state, ti.checks), label
                    mine = checks.owner(ti.kind, ti.passengers, ti.payload, st.book) == "on"
                    from_them = "unverified_sender" in (ti.payload.get("raised") or {})
                    marked = stated[(o.action.item.kind, o.action.item.key)]
                    if not mine:
                        want = ("off", marked)
                    elif from_them:
                        opened = {"unverified_sender", *marked}
                        want = ("blocked", tuple(code for code in checks.ALL if code in opened))
                    elif marked:
                        want = ("blocked", marked)
                    else:
                        want = ("synced", ())
                    assert (o.sync, o.checks) == want, label
                    assert (ti.airtrail_flight_id is not None) is (o.sync == "synced"), label
                    seen.append((o.action.item.kind, o.result, o.sync))
                # the unvouched sender's flights, from the email that files them on
                if entry.name in for_them:
                    held = [st.find(k) for k in by_sender]
                    assert [(ti.checks, ti.sync_state) for ti in held] == [
                        (("unverified_sender",), "blocked")
                    ] * 2, entry.name
            # nothing waits, and the flight log holds each of the owner's flights once; the
            # booking with no date, in no trip, is scanned with the rest
            unsorted = st.unsorted()
            assert [ti.kind for ti in unsorted] == ["flight"]
            everything_ = [ti for trip in st.all_trips() for ti in st.items(trip.id)] + unsorted
            assert not [ti for ti in everything_ if ti.sync_state == "pending"]
            assert st.due_for_sync(now=util.now()) == []
            sent = [ti for ti in everything_ if ti.sync_state == "synced"]
            in_log = {f["id"] for f in log.flights}
            assert {ti.airtrail_flight_id for ti in sent} == in_log and len(sent) == len(in_log)
            assert all(ti.kind == "flight" for ti in sent) and sent
            for ti in sent:
                assert ti.told == checks.desired(ti.payload, ti.passengers, ti.status, st.book)
            # exactly the owner's checked flights, as the corpus's outcomes leave them
            assert {(f["flightNumber"], f["date"]) for f in log.flights} == wanted
            assert not {f["flightNumber"] for f in log.flights} & set(left)
            assert all(owner_aboard(r[-1]) for r in log.sent)
            # the flights held back are exactly the owner's two from the unvouched sender, held
            # by that check alone, and the owner's flight with no date, held by its own checks
            held = [ti for ti in everything_ if ti.sync_state == "blocked"]
            by_check = [ti for ti in held if "unverified_sender" in ti.checks]
            assert sorted(ti.item_key for ti in by_check) == by_sender
            rest = [ti for ti in held if ti not in by_check]
            assert [(ti.id, ti.checks) for ti in rest] == [(unsorted[0].id, undated)]
            before = len(log.flights)
            for ti in held:  # the owner looks at each, after the email that raised its check
                stamps_ = [util.parse_iso(at) for at in (ti.payload.get("raised") or {}).values()]
                raised = max(stamps_, default=util.now())
                st.looks_right(ti.id, at=stamp(raised + dt.timedelta(hours=1)))
            assert [st.item(ti.id).sync_state for ti in by_check] == ["pending"] * 2
            assert st.item(rest[0].id).sync_state == "blocked"  # a look fixes no field
            assert sync(ctx).done == {"create": len(by_check)}
            assert len(log.flights) == before + len(by_check)
            assert [st.item(ti.id).sync_state for ti in by_check] == ["synced"] * 2
            assert st.item(rest[0].id).sync_state == "blocked"
            assert all(owner_aboard(r[-1]) for r in log.sent)
            # filing the thirty again, each read anew, adds no booking and no flight, and leaves
            # every flight there as it was (an update may come and go on the way: the package's
            # booking email puts back the traveller its later cancellation takes off again)
            items = len(everything(ctx))
            holds = [as_held(f) for f in log.flights]
            log.requests.clear()
            for entry in c.entries:
                process_raw(
                    ctx,
                    entry.raw,
                    source="eval",
                    received=corpus.sent(entry),
                    notify=False,
                    force=True,
                )
            assert len(everything(ctx)) == items
            assert [as_held(f) for f in log.flights] == holds
            assert "create" not in [r[0] for r in log.sent]
            assert all(owner_aboard(r[-1]) for r in log.sent)
    # each way a booking can end was met on the way
    assert {(kind, sync_) for kind, _, sync_ in seen} >= {
        ("flight", "synced"),
        ("flight", "off"),
        ("flight", "blocked"),
        ("lodging", "off"),
        ("car", "off"),
        ("train", "off"),
    }
    assert {result for _, result, _ in seen} == {"filed", "updated"}


def as_held(flight: dict) -> tuple:
    """What the flight log holds of a flight, its notes left out: its id, day, times, number,
    airports, airline and who is aboard (the owner by user id, anyone else by name)."""
    riders = sorted(
        (p["user"] or {}).get("id") or p.get("guestName") or "" for p in flight["passengers"]
    )
    return (
        flight["id"],
        flight["date"],
        flight["departure"],
        flight["arrival"],
        flight["flightNumber"],
        (flight["from"] or {}).get("id"),
        (flight["to"] or {}).get("id"),
        (flight["airline"] or {}).get("id"),
        riders,
    )


def code_and_day(key: str) -> tuple[str, str]:
    """A flight's code and day, from its real key, as the flight log holds them."""
    carrier, number, when, _ = key.split("|")
    return f"{carrier}{int(number)}", when


def renumbered(old: str, new: str) -> bool:
    """Whether the real flight key `new` is `old` given another number that day, or moved to
    another day under its number: the same carrier and origin."""
    if is_draft(old) or is_draft(new):
        return False
    a, b = old.split("|"), new.split("|")
    return a[0] == b[0] and a[3] == b[3] and (a[1] == b[1]) != (a[2] == b[2])


def outcome_without_a_flight_log(view: dict) -> dict:
    """An outcome view without what only a flight log decides: each booking's sync state and the
    checks only a flight log opens."""
    items = []
    for item in view["items"]:
        found = item["booking"]
        if found is not None:
            found = {k: v for k, v in found.items() if k != "sync"}
            found["checks"] = list(without_a_flight_log(found["checks"]))
        items.append({**item, "booking": found})
    return {**view, "items": items}
