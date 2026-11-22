# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Every booking an email is read for is filed at once. What is doubtful about it is a check on
the filed booking, never a reason to hold it back: a booking with findings is filed with its
checks, a cancellation for a booking never seen is filed cancelled and marked, and a booking with
no date is filed in no trip. Each email is built from the corpus's truth and read by a stand-in for
the model; the pipeline files it into an empty ledger under the corpus's own settings, with a
flight log held in memory where one is kept."""

import contextlib
import dataclasses
import datetime as dt
import tempfile
from collections.abc import Iterator
from pathlib import Path

from helpers.eml import make_eml
from helpers.flightlog import Scripted, seed
from test_checks import (
    WHOLE,
    booking,
    cities,
    edit,
    first,
    moved,
    owners,
    the_corpus,
    validated,
)
from test_normalize import AR_FWD
from test_run import AR
from test_store_actions import an_hour_later

from tripbox import corpus
from tripbox.ledger import Ledger
from tripbox.parse import pipeline as pl
from tripbox.parse.models import Extraction
from tripbox.pipeline import run as runmod
from tripbox.pipeline.run import Context, MessageResult
from tripbox.trips.store import TripItem
from tripbox.validate import checks
from tripbox.validate.keys import draft_key, is_draft
from tripbox.write.sync import Syncer

SENDER = the_corpus().settings["TRIPBOX_MAIL_ALLOWED_SENDERS"].split(",")[0]
UNSURE = 0.5  # a model's answer below its confidence floor
# Filter-forwarded by the owner's mailbox from a sender no list vouches for.
UNVOUCHED = {"Return-Path": "<alex+caf_=trips=example.com@example.com>", **AR_FWD}


@contextlib.contextmanager
def filing(
    *, flight_log: bool = True, config_dir: Path | None = None
) -> Iterator[Context]:
    """A pipeline context over an empty ledger under the corpus's own settings (its travellers
    unless `config_dir` names another directory). With `flight_log` one is kept: an AirTrail held
    in memory (`ctx.airtrail`) that knows the corpus's airports and airlines, which the pass after
    each message brings in step."""
    c = the_corpus()
    with tempfile.TemporaryDirectory(prefix="tripbox-test-") as tmp:
        base = Path(tmp)
        s = corpus.settings(
            c,
            data_dir=base / "data",
            archive_dir=base / "archive",
            logs_dir=base / "logs",
            config_dir=config_dir,
        )
        with corpus.process_as(s):
            led = Ledger.memory()
            ctx = runmod.build_context(s, ledger=led, with_model=False, with_notifier=False)
            if flight_log:
                led.kv.set("airtrail", "on")
                seed(led, c)
                log = Scripted(led)
                ctx.airtrail = log
                ctx.syncer = Syncer(
                    store=ctx.trips, ledger=led, book=ctx.trips.book, client=log, refs=None,
                    dry_run=False,
                )
            try:
                yield ctx
            finally:
                led.close()


def model_reads(monkeypatch, reservations: list[dict], *, doc="booking", confidence=1.0) -> None:
    """From here on the stand-in for the model reads every message as `reservations`."""
    answer = Extraction.model_validate(
        {"document_type": doc, "confidence": confidence, "reservations": reservations}
    )
    monkeypatch.setattr(
        runmod,
        "parse_message",
        lambda nm, model: pl.ParsedMessage(extraction=answer.model_copy(deep=True), parser="haiku"),
    )


def deliver(
    ctx: Context,
    monkeypatch,
    entry: corpus.Entry,
    reservations: list[dict],
    *,
    n: int = 0,
    doc: str = "booking",
    confidence: float = 1.0,
    unvouched: bool = False,
    received: dt.datetime | None = None,
    force: bool = False,
) -> MessageResult:
    """Email number `n` about `entry`, dated as that entry's email is, which the stand-in for the
    model reads as `reservations`, through the pipeline. `unvouched`: forwarded by the owner's
    mailbox from a sender no list vouches for; `received`: when it was taken in (now when not
    given)."""
    model_reads(monkeypatch, reservations, doc=doc, confidence=confidence)
    raw = email_for(entry, n, unvouched=unvouched)
    return runmod.process_raw(ctx, raw, source="imap", received=received, force=force)


def email_for(
    entry: corpus.Entry, n: int = 0, *, unvouched: bool = False, copy: int = 0
) -> bytes:
    """The bytes of email number `n` about `entry`, as `deliver` sends it; `copy` another copy of
    it (other bytes, the same Message-ID)."""
    again = f" copy {copy}" if copy else ""
    return make_eml(
        from_="x@evil.example" if unvouched else SENDER,
        subject=f"Fwd: {entry.name} {n}{again}",
        date=corpus.sent(entry),
        text=f"{entry.name} {n}{again}",
        headers=UNVOUCHED if unvouched else AR,
        message_id=f"<{entry.name}-{n}@trips.example>",
    ).as_bytes()


def owners_flight() -> tuple[corpus.Entry, dict]:
    """The first leg of the owner's that the corpus files with no finding."""
    return booking("flight", owners)


def city_flight() -> tuple[corpus.Entry, dict, str]:
    """The first leg of the owner's that the corpus files with no finding and whose origin the
    corpus also reads as a city, with that city."""
    places = cities()
    entry, res = booking("flight", lambda r: owners(r) and r["segments"][0]["origin"] in places)
    return entry, res, places[res["segments"][0]["origin"]]


def whole(res: dict) -> dict:
    """A truth reservation with the first reading of every field that lists several, all its
    legs kept."""
    out = {k: (v if k in WHOLE else first(v)) for k, v in res.items() if k != "segments"}
    out["segments"] = [
        {k: (v if k in WHOLE else first(v)) for k, v in leg.items()}
        for leg in res.get("segments") or []
    ]
    return out


def everything(ctx: Context) -> list[TripItem]:
    """Every booking the store holds, oldest first."""
    rows = ctx.ledger.conn.execute("SELECT id FROM trip_items ORDER BY id").fetchall()
    return [ctx.trips.item(r["id"]) for r in rows]


def test_a_booking_with_findings_is_filed_at_once_with_its_checks(monkeypatch):
    """A flight read below the confidence floor whose origin came back as a city, its offset
    not printed: no airport, so no zone for its departure either."""
    entry, flight, city = city_flight()
    printed = flight["segments"][0]["departure_local"]
    spoiled = edit(flight, origin=city, departure_offset=None)
    with filing() as ctx:
        got = deliver(ctx, monkeypatch, entry, [spoiled], confidence=UNSURE)
        (o,) = got.outcomes
        assert (got.status, o.action.kind, o.result) == ("processed", "file", "filed")
        assert o.checks == ("origin", "departure", "low_confidence")
        ti = ctx.trips.item(o.trip_item_id)
        assert is_draft(ti.item_key) and ti.checks == o.checks
        # in its trip by the day it prints, and kept from the flight log until it is checked
        trip = ctx.trips.get(ti.trip_id)
        assert ti.start_at[:10] == printed[:10] == trip.start_date
        assert (ti.sync_state, ctx.airtrail.requests) == ("blocked", [])
        rows = ctx.ledger.items.by_message(got.message_row_id)
        assert [(r["outcome"], r["trip_item_id"]) for r in rows] == [("filed", ti.id)]


def test_a_cancellation_for_a_booking_never_seen_is_filed_cancelled_and_marked(monkeypatch):
    """A stay and a flight of the owner's, each cancelled before any email about it was read:
    each is filed cancelled, in a trip of its own, with the check that says so. A cancelled
    flight that was never sent is never sent, before the owner's look and after."""
    with filing() as ctx:
        for n, (entry, res) in enumerate((booking("lodging"), owners_flight())):
            cancelled = edit(res, {"status": "cancelled"})
            got = deliver(ctx, monkeypatch, entry, [cancelled], n=n, doc="cancellation")
            (o,) = got.outcomes
            assert (got.status, o.action.kind, o.result) == ("processed", "file", "filed")
            ti = ctx.trips.item(o.trip_item_id)
            assert (ti.status, ti.checks, ti.sync_state) == (
                "cancelled",
                ("unknown_cancellation",),
                "off",
            )
            trip = ctx.trips.get(ti.trip_id)
            assert trip.cancelled and [x.id for x in ctx.trips.items(trip.id)] == [ti.id]
            looked = ctx.trips.looks_right(ti.id)
            assert (looked.status, looked.checks, looked.sync_state) == ("cancelled", (), "off")
        assert len(ctx.trips.all_trips()) == 2 and ctx.airtrail.requests == []


def test_a_flight_with_no_date_is_filed_unsorted_under_a_draft_key(monkeypatch):
    entry, flight = owners_flight()
    undated = edit(flight, departure_local=None, arrival_local=None)
    with filing() as ctx:
        got = deliver(ctx, monkeypatch, entry, [undated])
        (o,) = got.outcomes
        assert (got.status, o.result) == ("processed", "filed")
        ti = ctx.trips.item(o.trip_item_id)
        sha = ctx.ledger.messages.get(got.message_row_id)["sha256"]
        assert ti.item_key == draft_key(sha, 0) == o.action.item.key
        assert (ti.trip_id, ti.start_at) == (None, None) and ctx.trips.all_trips() == []
        assert [u.id for u in ctx.trips.unsorted()] == [ti.id]
        assert ctx.trips.flag_counts() == {None: 1}
        assert (ti.checks, ti.sync_state) == (("departure",), "blocked")
        assert ctx.airtrail.requests == []


def test_a_replay_after_a_fix_finds_the_booking_by_its_draft_key_and_changes_nothing(
    monkeypatch,
):
    """A stay whose email prints neither its confirmation code nor its name is filed as a draft.
    The owner gives it its name on the page, which files it under its real key, the draft key
    kept as an alias. The same email read again finds that booking by the draft key and
    changes nothing: the name stays the owner's."""
    entry, stay = booking("lodging")
    vague = edit(stay, {"confirmation_code": None, "property_name": None, "provider": None})
    with filing() as ctx:
        got = deliver(ctx, monkeypatch, entry, [vague])
        (o,) = got.outcomes
        draft = ctx.trips.item(o.trip_item_id)
        assert is_draft(draft.item_key) and o.result == "filed"
        assert draft.checks == ("missing_name",)
        fixed = ctx.trips.edit_item(draft.id, {"property_name": stay["property_name"]})
        assert not is_draft(fixed.item_key) and fixed.checks == ()
        assert fixed.payload["alt_keys"] == [draft.item_key]
        again = deliver(ctx, monkeypatch, entry, [vague], force=True)
        (o2,) = again.outcomes
        assert again.message_row_id == got.message_row_id and o2.action.item.key == draft.item_key
        assert (o2.result, o2.trip_item_id) == ("duplicate", fixed.id)
        assert everything(ctx) == [ctx.trips.item(fixed.id)]
        assert dataclasses.asdict(ctx.trips.item(fixed.id)) == dataclasses.asdict(fixed)
        rows = ctx.ledger.items.by_message(got.message_row_id)
        assert [(r["outcome"], r["detail"], r["trip_item_id"]) for r in rows] == [
            ("duplicate", None, fixed.id)
        ]


def test_a_later_email_completes_a_draft_through_the_pipeline(monkeypatch):
    entry, flight, city = city_flight()
    with filing() as ctx:
        first_read = deliver(ctx, monkeypatch, entry, [edit(flight, origin=city)], n=0)
        (o,) = first_read.outcomes
        draft = ctx.trips.item(o.trip_item_id)
        assert is_draft(draft.item_key) and o.sync == "blocked" and ctx.airtrail.flights == []
        proper = deliver(ctx, monkeypatch, entry, [flight], n=1)
        (o2,) = proper.outcomes
        assert (o2.action.kind, o2.action.reason, o2.result) == (
            "update",
            "completes_draft",
            "updated",
        )
        (one,) = everything(ctx)
        assert one.id == draft.id and not is_draft(one.item_key)
        assert one.payload["alt_keys"] == [draft.item_key]
        assert (one.checks, one.sync_state) == ((), "synced")
        (sent,) = ctx.airtrail.flights
        assert one.airtrail_flight_id == sent["id"]


def test_a_value_a_later_email_cannot_have_right_leaves_the_booking_as_it_was(monkeypatch):
    """A later email for a filed flight prints a city where its arrival airport belongs, and a
    departure and an arrival an hour later. The city says nothing: the booking keeps its airport
    and carries no check for it, while the new times apply and reach the flight log."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    city = city_flight()[2]  # a city the corpus reads for an airport: no airport code
    later = edit(
        flight,
        destination=city,
        departure_local=an_hour_later(leg["departure_local"]),
        arrival_local=an_hour_later(leg["arrival_local"]),
    )
    assert "destination" in checks.codes(validated(entry, later))
    with filing() as ctx:
        (o,) = deliver(ctx, monkeypatch, entry, [flight], n=0).outcomes
        before = ctx.trips.item(o.trip_item_id)
        assert (before.sync_state, before.checks) == ("synced", ())
        (o2,) = deliver(ctx, monkeypatch, entry, [later], n=1, doc="change").outcomes
        assert (o2.result, o2.trip_item_id) == ("updated", before.id)
        assert "dep_utc" in o2.action.changes and "destination" not in o2.action.changes
        ti = ctx.trips.item(before.id)
        assert ti.payload["destination"] == before.payload["destination"]
        assert ti.payload["destination_tz"] == before.payload["destination_tz"]
        assert ti.payload["dep_local"] == an_hour_later(before.payload["dep_local"])
        assert ti.payload["dep_utc"] != before.payload["dep_utc"]
        assert (ti.checks, ti.sync_state) == ((), "synced")
        (held,) = ctx.airtrail.flights
        assert held["departure"] == ti.payload["dep_utc"]
        assert [r[0] for r in ctx.airtrail.sent] == ["create", "update"]


def inverted_ends_say_nothing(monkeypatch, entry, res: dict, wrong: dict, ends: tuple) -> None:
    """`res` filed, then a later copy of it whose own end comes before its start (`wrong`): the
    copy says nothing about either end, so it is a duplicate and the booking keeps its ends,
    raises no check and, for a flight, stays in step with the flight log."""
    with filing() as ctx:
        (o,) = deliver(ctx, monkeypatch, entry, [res], n=0).outcomes
        before = ctx.trips.item(o.trip_item_id)
        assert before.checks == ()
        (o2,) = deliver(ctx, monkeypatch, entry, [wrong], n=1, doc="change").outcomes
        assert (o2.result, o2.trip_item_id) == ("duplicate", before.id)
        after = ctx.trips.item(before.id)
        assert [after.payload[k] for k in ends] == [before.payload[k] for k in ends]
        assert (after.checks, after.sync_state) == ((), before.sync_state)
        assert [r[0] for r in ctx.airtrail.sent] == (["create"] if res["kind"] == "flight" else [])


def test_a_flights_arrival_printed_before_its_departure_says_nothing(monkeypatch):
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    wrong = edit(flight, arrival_local=moved(leg["arrival_local"], -1))  # its day left out
    assert "arrival_before_departure" in checks.codes(validated(entry, wrong))
    ends = ("dep_local", "arr_local", "dep_utc", "arr_utc")
    inverted_ends_say_nothing(monkeypatch, entry, flight, wrong, ends)


def test_a_stays_check_out_printed_before_its_check_in_says_nothing(monkeypatch):
    entry, stay = booking("lodging")
    wrong = edit(stay, {"check_out": moved(stay["check_in"], -1)})
    assert "check_out" in checks.codes(validated(entry, wrong))
    inverted_ends_say_nothing(monkeypatch, entry, stay, wrong, ("check_in", "check_out"))


def test_a_cars_drop_off_printed_before_its_pick_up_says_nothing(monkeypatch):
    entry, car = booking("car")
    wrong = edit(car, {"dropoff_time": moved(car["pickup_time"], -1)})
    assert "dropoff_time" in checks.codes(validated(entry, wrong))
    inverted_ends_say_nothing(monkeypatch, entry, car, wrong, ("pickup_time", "dropoff_time"))


def test_an_email_that_prints_only_days_for_a_booking_that_holds_the_times_is_a_duplicate(
    monkeypatch,
):
    """A day printed alone says nothing about the time a booking holds on that day: an email
    that prints only the days of a flight already filed with its times changes nothing, and the
    finding it raises about the times it lacks is not the booking's."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    days = edit(
        flight,
        departure_local=leg["departure_local"][:10],
        arrival_local=leg["arrival_local"][:10],
    )
    with filing() as ctx:
        (o,) = deliver(ctx, monkeypatch, entry, [flight], n=0).outcomes
        before = ctx.trips.item(o.trip_item_id)
        assert (before.sync_state, before.checks) == ("synced", ())
        assert "departure" in checks.codes(validated(entry, days))  # it prints no time
        again = deliver(ctx, monkeypatch, entry, [days], n=1)
        (o2,) = again.outcomes
        assert (again.status, o2.action.kind, o2.result) == ("ignored", "duplicate", "duplicate")
        assert dataclasses.asdict(ctx.trips.item(o.trip_item_id)) == dataclasses.asdict(before)
        assert [r[0] for r in ctx.airtrail.sent] == ["create"]


def test_a_newer_copy_that_is_a_duplicate_raises_nothing(monkeypatch):
    """A copy of what a booking already holds puts nothing into it, so it raises no check and
    does not undo the owner's look, even received after that look, read below the confidence
    floor or forwarded from a sender no list vouches for."""
    entry, flight = owners_flight()
    with filing() as ctx:
        (o,) = deliver(ctx, monkeypatch, entry, [flight], n=0, confidence=UNSURE).outcomes
        assert (o.checks, o.sync) == (("low_confidence",), "blocked")
        looked = ctx.trips.looks_right(o.trip_item_id)
        assert (looked.checks, looked.sync_state) == ((), "pending")
        runmod.sync(ctx)
        settled = ctx.trips.item(o.trip_item_id)
        assert settled.sync_state == "synced"
        later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        for n, how in enumerate(({"confidence": UNSURE}, {"unvouched": True}), start=1):
            again = deliver(ctx, monkeypatch, entry, [flight], n=n, received=later, **how)
            (o2,) = again.outcomes
            assert o2.action.item.problems, how
            assert (o2.result, o2.checks, o2.sync) == ("duplicate", (), "synced"), how
            assert dataclasses.asdict(ctx.trips.item(o.trip_item_id)) == dataclasses.asdict(
                settled
            ), how
        assert [r[0] for r in ctx.airtrail.sent] == ["create"]


def test_a_finding_is_dated_by_the_reading_that_files_it_when_the_first_never_finished(
    monkeypatch,
):
    """A change read below the confidence floor is taken in before the owner looks at the
    booking it changes, and that first reading fails. Read again after the look, what it raises
    is dated by this reading: the look does not hide it, and the change waits for the owner."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    later = edit(
        flight,
        departure_local=an_hour_later(leg["departure_local"]),
        arrival_local=an_hour_later(leg["arrival_local"]),
    )
    now = dt.datetime.now(dt.UTC)
    with filing() as ctx:
        (o,) = deliver(ctx, monkeypatch, entry, [flight], n=0).outcomes
        assert o.sync == "synced"

        def broken(*args, **kw):
            raise RuntimeError("the disk is full")

        with monkeypatch.context() as m:
            m.setattr(ctx.executor, "execute", broken)
            taken_in = now - dt.timedelta(hours=1)
            failed = deliver(
                ctx, monkeypatch, entry, [later], n=1, doc="change", confidence=UNSURE,
                received=taken_in,
            )
        assert failed.status == "failed"
        look = (now - dt.timedelta(minutes=30)).isoformat(timespec="seconds")
        assert ctx.trips.looks_right(o.trip_item_id, at=look).checks == ()
        again = runmod.process_raw(ctx, email_for(entry, 1), source="replay")
        assert again.message_row_id == failed.message_row_id
        (o2,) = again.outcomes
        assert (again.status, o2.result) == ("processed", "updated")
        ti = ctx.trips.item(o.trip_item_id)
        assert (ti.checks, ti.sync_state) == (("low_confidence",), "blocked")
        assert ti.payload["raised"]["low_confidence"] > look
        assert [r[0] for r in ctx.airtrail.sent] == ["create"]


def test_a_copy_set_aside_unread_dates_its_findings_by_the_reading_that_files_it(monkeypatch):
    """A second copy of an email is set aside unread, since its twin was filed. Read later by
    force, after the owner looked at the booking, it is read for the first time: what it raises
    is dated by this reading, so the look does not hide it and its change waits."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    later = edit(
        flight,
        departure_local=an_hour_later(leg["departure_local"]),
        arrival_local=an_hour_later(leg["arrival_local"]),
    )
    now = dt.datetime.now(dt.UTC)
    with filing() as ctx:
        (o,) = deliver(
            ctx, monkeypatch, entry, [flight], n=0, received=now - dt.timedelta(hours=3)
        ).outcomes
        assert o.sync == "synced"
        twin = email_for(entry, 0, copy=1)
        model_reads(monkeypatch, [later], doc="change", confidence=UNSURE)
        aside = runmod.process_raw(ctx, twin, source="imap", received=now - dt.timedelta(hours=2))
        row = ctx.ledger.messages.get(aside.message_row_id)
        assert (aside.status, aside.outcomes, row["parser"]) == ("ignored", [], None)
        look = (now - dt.timedelta(minutes=90)).isoformat(timespec="seconds")
        assert ctx.trips.looks_right(o.trip_item_id, at=look).checks == ()
        forced = runmod.process_raw(ctx, twin, source="replay", force=True)
        assert forced.message_row_id == aside.message_row_id
        assert [x.result for x in forced.outcomes] == ["updated"]
        ti = ctx.trips.item(o.trip_item_id)
        assert (ti.checks, ti.sync_state) == (("low_confidence",), "blocked")
        assert ti.payload["raised"]["low_confidence"] > look
        assert [r[0] for r in ctx.airtrail.sent] == ["create"]
