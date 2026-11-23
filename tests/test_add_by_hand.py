# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Bookings the owner types in on the page: a flight, a hotel, a car or a train, through the
edit form, filed by the trip store with no model and no message. What was typed is the owner's
edit, so an email for the same booking that comes later finds it and merges by the owner-edit
rule. Every booking is the corpus's own, read from its truth at run time; the emails filed after
the entry are the corpus's emails; the flight log is one held in memory."""

import contextlib
import dataclasses
import datetime as dt
import re
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from helpers.eml import make_eml
from helpers.web import ORIGIN, Page
from test_checks import (
    a_companion_alone,
    book,
    booking,
    bookings,
    edit,
    first,
    moved,
    names,
    owners,
    plain,
    stamps,
    the_corpus,
    validated,
)
from test_filed import SENDER, UNSURE, everything, filing, model_reads
from test_run import AR
from test_store_actions import an_hour_later, kept, owners_flights, rows, store
from test_web_uploads import SAME_SITE, alert
from test_web_views import Controls, tied

from tripbox import corpus
from tripbox.ledger import Ledger
from tripbox.pipeline import run as runmod
from tripbox.pipeline.run import Context, MessageResult
from tripbox.trips import grouping
from tripbox.trips.store import EDITABLE, STORED_AS, TripStore, editable_value
from tripbox.validate import checks, rules
from tripbox.validate.keys import is_draft
from tripbox.validate.passengers import people
from tripbox.web import views
from tripbox.web.app import create_app

ME = book().me_name
EMPTY = "fill in at least one field"
THERE = "that booking is already in your trips"
PUT_AWAY = "that booking is in Recently deleted"


def held(item: rules.Item) -> SimpleNamespace:
    """A booking the corpus files, as the page reads one it holds."""
    return SimpleNamespace(
        kind=item.kind, payload=item.payload, passengers=item.passengers, confirmation=item.pnr
    )


def typed(item: rules.Item, **over) -> dict:
    """What the owner types into the store's fields for `item`: each field the page edits, as the
    booking holds it, with `over` in place of some (None: left empty); the empty ones are not
    sent."""
    out = {f: editable_value(held(item), f) for f in EDITABLE[item.kind]}
    out.update(over)
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def form_for(item: rules.Item, **over) -> dict[str, str]:
    """The add form filled in for `item` as the edit form shows a booking, the trip left to the
    dates, with `over` typed over it."""
    return {**views._edit_values(held(item)), "trip": "", **over}


def stamp(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).isoformat(timespec="seconds")


def file_email(ctx: Context, entry: corpus.Entry) -> MessageResult:
    """The entry's own email through the pipeline, as `tripbox eval --recorded` files it."""
    ctx.model = corpus.CorpusAnswers(the_corpus(), ctx.settings.llm_model)
    return runmod.process_raw(
        ctx, entry.raw, source="eval", received=corpus.sent(entry), notify=False
    )


def under(ctx: Context, key: str) -> list[int]:
    """The bookings filed under `key`."""
    return [ti.id for ti in everything(ctx) if ti.item_key == key]


@contextlib.contextmanager
def a_page(tmp_path, base: str) -> Iterator[tuple[Page, TripStore]]:
    """The page with no login over a new ledger under the corpus's own settings, served under
    `base`, and a store over the same ledger to read what it stored."""
    s = corpus.settings(
        the_corpus(),
        data_dir=tmp_path / "data",
        archive_dir=tmp_path / "archive",
        logs_dir=tmp_path / "logs",
        environ={"TRIPBOX_BASE_URL": ORIGIN + base},
    )
    with corpus.process_as(s):
        app = create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)
        with Ledger.open(s.ledger_path) as led:
            yield Page(app, base, headers=SAME_SITE), store(led)


def add(c: Page, word: str, form: dict, query: str = ""):
    return c.post(f"/add/{word}{query}", data=form, follow_redirects=False)


def values(page: str) -> dict[str, str]:
    """Each field of a form as the page shows it: inputs and textareas by name, and each select's
    chosen option."""
    out = {
        m["name"]: m["value"]
        for m in re.finditer(r'<input [^>]*name="(?P<name>[^"]+)" value="(?P<value>[^"]*)"', page)
    }
    for m in re.finditer(r'<textarea [^>]*name="(?P<name>[^"]+)"[^>]*>(?P<value>[^<]*)<', page):
        out[m["name"]] = m["value"]
    select = re.compile(r'<select [^>]*name="(?P<name>[^"]+)"[^>]*>(?P<body>.*?)</select>', re.S)
    for m in select.finditer(page):
        chosen = re.search(r'<option value="([^"]*)" selected>', m["body"])
        out[m["name"]] = chosen.group(1) if chosen else ""
    return out


def options(page: str, name: str = "trip") -> list[tuple[str, str]]:
    """The options of the select `name`, as (value, text), in order."""
    body = re.search(rf'<select [^>]*name="{name}"[^>]*>(.*?)</select>', page, re.S).group(1)
    return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', body)


# ----------------------------------------------------------------------------- the store
def test_a_booking_added_by_hand_is_filed_without_a_model_or_a_message():
    entry, res = booking("flight", owners)
    flight = validated(entry, res)
    fields = typed(flight)
    assert set(fields) == set(EDITABLE["flight"])  # every field
    at = stamp(corpus.sent(entry))
    with filing(flight_log=False) as ctx:
        ti = ctx.trips.add_by_hand("flight", fields, at=at)
        # its real key, the one the corpus files its email under
        assert ti.item_key == flight.key and not is_draft(ti.item_key)
        assert flight.key in [i["key"] for i in entry.outcome["items"]]
        # in a trip by its dates
        trip = ctx.trips.get(ti.trip_id)
        d0, d1 = grouping.item_dates(ti)
        assert (trip.start_date, trip.end_date) == (d0.isoformat(), d1.isoformat())
        assert ti.start_at[:10] == fields["dep_local"][:10]
        # what was typed, each field stamped as the owner's at the time of the entry
        stored = {STORED_AS["flight"].get(f, f): v for f, v in fields.items()}
        assert {k: ti.payload[k] for k in stored} == stored
        assert ti.payload["source"] == "hand"
        assert ti.payload["owner_edits"] == dict.fromkeys(stored, at)
        assert ti.passengers == fields["passengers"]
        assert ti.confirmation == fields["confirmation_code"]
        assert (ti.status, ti.checks, ti.source_message_id) == ("confirmed", (), None)
        assert "raised" not in ti.payload and "looked_at" not in ti.payload
        # no model was asked and no message was written
        for table in ("messages", "items", "llm_calls", "uploads"):
            assert ctx.ledger.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_too_little_to_key_is_a_draft_and_no_date_is_unsorted():
    flight = validated(*booking("flight", owners))
    stay = validated(*booking("lodging"))
    car = validated(*booking("car"))
    with filing(flight_log=False) as ctx:
        st = ctx.trips
        # no flight number: no key, but a date, so it is in a trip
        numberless = st.add_by_hand("flight", typed(flight, flight_number=None))
        assert is_draft(numberless.item_key) and numberless.checks == ("flight_number",)
        assert numberless.trip_id is not None
        assert numberless.start_at[:10] == flight.payload["dep_local"][:10]
        # a hotel's name alone: no key and no date, so it waits with no trip
        named = st.add_by_hand("lodging", {"property_name": stay.payload["property_name"]})
        assert is_draft(named.item_key) and named.item_key != numberless.item_key
        assert (named.trip_id, named.start_at) == (None, None)
        assert named.checks == ("check_in", "check_out")
        # a car's code alone: its real key, still with no date
        coded = st.add_by_hand("car", {"confirmation_code": car.pnr})
        assert coded.item_key == car.key and coded.trip_id is None
        assert [u.id for u in st.unsorted()] == [named.id, coded.id]
        assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def a_shared_leg() -> tuple[corpus.Entry, dict]:
    """A leg of the owner's that the corpus files with no finding, which names a companion and
    leaves out another companion the travellers file names, and prints its arrival."""
    everyone = {name for name, _ in book().guests}

    def fits(res: dict) -> bool:
        found = book().classify(names(res))
        leg = res["segments"][0]
        return (
            found.me_present
            and bool(found.guests)
            and bool(everyone - set(found.guests))
            and len(str(leg.get("arrival_local") or "")) > 10
        )

    return booking("flight", fits)


def test_a_later_email_for_the_same_booking_merges_by_key():
    """The owner types the leg in, naming themselves and a companion the email leaves out, with
    an arrival an hour off and no code, and then the leg's own email is filed. Either way it finds
    the booking by its key, joins the travellers of both and fills the code the booking lacked.
    An email newer than the entry wins the arrival it prints; one older than the entry leaves
    what was typed. One that names no traveller the booking lacks and moves nothing of its
    schedule still fills in what the booking lacks (its code), in either order, recording no
    change; read again, it fills nothing and is a duplicate."""
    entry, res = a_shared_leg()
    flight = validated(entry, res)
    missing = sorted({name for name, _ in book().guests} - set(book().classify(names(res)).guests))
    typed_in = typed(
        flight,
        passengers=[ME, missing[0]],
        arr_local=an_hour_later(flight.payload["arr_local"]),
        confirmation_code=None,
    )
    sent = corpus.sent(entry)
    for newer, at in ((True, sent - dt.timedelta(days=1)), (False, sent + dt.timedelta(days=1))):
        with filing(flight_log=False) as ctx:
            st = ctx.trips
            ti = st.add_by_hand("flight", typed_in, at=stamp(at))
            got = file_email(ctx, entry)
            (o,) = [o for o in got.outcomes if o.trip_item_id == ti.id]
            assert (got.status, o.result, o.action.reason) == (
                "processed",
                "updated",
                "add_passengers",
            ), newer
            after = st.item(ti.id)
            assert under(ctx, flight.key) == [ti.id] and after.item_key == flight.key
            assert people(after.passengers, book()) == people(
                [*typed_in["passengers"], *flight.passengers], book()
            )
            assert after.payload["pnr"] == after.confirmation == flight.pnr
            assert after.source_message_id == got.message_row_id
            assert after.checks == ()
            if newer:  # the email's arrival
                assert after.payload["arr_local"] == flight.payload["arr_local"]
                assert "owner_edits" not in after.payload
            else:  # what was typed holds
                assert after.payload["arr_local"] == typed_in["arr_local"]
                assert set(after.payload["owner_edits"]) == {
                    STORED_AS["flight"].get(f, f) for f in typed_in
                }
    # typed with its travellers and its schedule but not its code: nothing to add or move, and
    # the code is filled in all the same
    codeless = typed(flight, confirmation_code=None)
    for newer, at in ((True, sent - dt.timedelta(days=1)), (False, sent + dt.timedelta(days=1))):
        with filing(flight_log=False) as ctx:
            st = ctx.trips
            ti = st.add_by_hand("flight", codeless, at=stamp(at))
            got = file_email(ctx, entry)
            (o,) = [o for o in got.outcomes if o.trip_item_id == ti.id]
            assert (o.result, o.action.reason, o.action.changes) == ("updated", "fills_in", {})
            after = st.item(ti.id)
            assert after.payload["pnr"] == after.confirmation == flight.pnr, newer
            assert after.source_message_id == got.message_row_id
            assert "change_recorded" not in after.payload and after.checks == ()
            assert under(ctx, flight.key) == [ti.id]
            for field in ("dep_local", "arr_local", "destination"):
                assert after.payload[field] == codeless[field], (newer, field)
            if not newer:  # what was typed is still the owner's
                assert after.payload["owner_edits"] == ti.payload["owner_edits"]
            rows_of = ctx.ledger.items.by_message(got.message_row_id)
            assert [(r["outcome"], r["detail"]) for r in rows_of if r["trip_item_id"] == ti.id] == [
                ("updated", "fills_in")
            ]
            # read again, it has nothing left to fill in
            again = runmod.process_raw(
                ctx, entry.raw, source="eval", received=sent, notify=False, force=True
            )
            (o,) = [o for o in again.outcomes if o.trip_item_id == ti.id]
            assert o.result == "duplicate"
            assert dataclasses.asdict(st.item(ti.id)) == dataclasses.asdict(after)


def test_an_email_with_nothing_left_to_fill_in_is_a_duplicate():
    """A leg whose email prints the offset beside each time, typed in without its code: the
    email fills the code in; read again it is a duplicate, since a printed offset goes with its
    time, which the typed booking keeps, and is nothing the booking lacks."""
    entry, res = booking("flight", lambda r: owners(r) and r["segments"][0].get("departure_offset"))
    flight = validated(entry, res)
    assert flight.payload["dep_offset"] and flight.payload["arr_offset"]
    with filing(flight_log=False) as ctx:
        st = ctx.trips
        ti = st.add_by_hand("flight", typed(flight, confirmation_code=None))
        got = file_email(ctx, entry)
        (o,) = [o for o in got.outcomes if o.trip_item_id == ti.id]
        assert (o.result, o.action.reason) == ("updated", "fills_in")
        filled = st.item(ti.id)
        assert filled.confirmation == flight.pnr and "dep_offset" not in filled.payload
        again = runmod.process_raw(
            ctx, entry.raw, source="eval", received=corpus.sent(entry), notify=False, force=True
        )
        (o,) = [o for o in again.outcomes if o.trip_item_id == ti.id]
        assert o.result == "duplicate"
        assert dataclasses.asdict(st.item(ti.id)) == dataclasses.asdict(filled)


def mailed(
    ctx: Context,
    monkeypatch,
    entry: corpus.Entry,
    reservation: dict,
    *,
    n: int,
    date,
    confidence: float = 1.0,
    received: dt.datetime | None = None,
):
    """Email number `n` about `entry`, dated `date`, which the stand-in for the model reads as
    `reservation` with `confidence`, taken in at `received` (now when not given), through the
    pipeline."""
    model_reads(monkeypatch, [reservation], confidence=confidence)
    raw = make_eml(
        from_=SENDER,
        subject=f"Fwd: {entry.name} {n}",
        date=date,
        text=f"{entry.name} {n}",
        headers=AR,
        message_id=f"<{entry.name}-{n}@trips.example>",
    ).as_bytes()
    return runmod.process_raw(ctx, raw, source="imap", received=received, notify=False)


def test_a_doubtful_email_that_fills_in_a_looked_at_flight_holds_it_back(monkeypatch):
    """The owner's flight, typed in without its code, carries a soft stamp the owner has looked
    at, and is sent. An email read below the confidence floor then fills in its code, its cabin
    and a seat: its own stamp joins the booking's, so the check is open, the flight is held back
    and nothing it filled in is sent until the owner looks again."""
    entry, res = booking("flight", owners)
    flight = validated(entry, res)
    me = next(n for n in names(res) if book().match(n).kind == "me")
    cabin = next(
        first(leg["cabin"])
        for e in the_corpus().entries
        for r in e.truth["reservations"]
        for leg in r.get("segments") or []
        if leg.get("cabin")
    )
    doubtful = edit(res, cabin=cabin, seat_by_passenger={me: "1A"})
    raised_at, looked_at = stamps(entry, 2)
    with filing() as ctx:
        st, log = ctx.trips, ctx.airtrail
        ti = st.add_by_hand("flight", typed(flight, confirmation_code=None))
        assert st.mark(ti.id, {"unverified_sender": raised_at}).checks == ("unverified_sender",)
        looked = st.looks_right(ti.id, at=looked_at)
        assert looked.checks == () and looked.payload["raised"]  # a stamp the owner has seen
        ctx.syncer.run_pass()
        assert st.item(ti.id).sync_state == "synced"
        sent_before = list(log.sent)
        got = mailed(
            ctx,
            monkeypatch,
            entry,
            doubtful,
            n=1,
            date=corpus.sent(entry),
            confidence=UNSURE,
            received=corpus.sent(entry) + dt.timedelta(days=1),
        )
        (o,) = [o for o in got.outcomes if o.trip_item_id == ti.id]
        assert (o.result, o.action.reason) == ("updated", "fills_in")
        after = st.item(ti.id)
        assert after.confirmation == flight.pnr and after.payload["cabin"] == cabin
        assert set(after.payload["raised"]) == {"unverified_sender", "low_confidence"}
        assert (after.checks, after.sync_state) == (("low_confidence",), "blocked")
        assert log.sent == sent_before


def test_an_older_copy_fills_in_only_what_the_booking_lacks(monkeypatch):
    """Typed in without its code, the owner's flight is filled in by an email that prints a
    cabin and a seat but no price. An older copy then prints the price, another cabin and another
    seat: it fills the price in and nothing else, so the cabin and the seat stay the newer
    email's and the flight log is sent nothing for them."""
    entry, res = booking("flight", lambda r: owners(r) and bool(r.get("price")))
    flight = validated(entry, res)
    me = next(n for n in names(res) if book().match(n).kind == "me")
    cabin = next(
        first(leg["cabin"])
        for e in the_corpus().entries
        for r in e.truth["reservations"]
        for leg in r.get("segments") or []
        if leg.get("cabin")
    )
    newer = edit(res, {"price": None, "currency": None}, cabin=cabin, seat_by_passenger={me: "1A"})
    older = edit(res, cabin=f"{cabin} Plus", seat_by_passenger={me: "2B"})
    sent = corpus.sent(entry)
    with filing() as ctx:
        st, log = ctx.trips, ctx.airtrail
        ti = st.add_by_hand("flight", typed(flight, confirmation_code=None), at=stamp(sent))
        ctx.syncer.run_pass()
        got = mailed(ctx, monkeypatch, entry, newer, n=1, date=sent + dt.timedelta(days=2))
        (o,) = [o for o in got.outcomes if o.trip_item_id == ti.id]
        assert (o.result, o.action.reason) == ("updated", "fills_in")
        filled = st.item(ti.id)
        assert filled.payload["cabin"] == cabin and filled.payload["seat_by_passenger"] == {
            me: "1A"
        }
        assert filled.sync_state == "synced" and "price" not in filled.payload
        told = filled.told
        sent_before = list(log.sent)
        copy = mailed(ctx, monkeypatch, entry, older, n=2, date=sent + dt.timedelta(days=1))
        (o,) = [o for o in copy.outcomes if o.trip_item_id == ti.id]
        assert (o.result, o.action.reason) == ("updated", "fills_in")
        after = st.item(ti.id)
        assert after.payload["price"] == res["price"]
        assert after.payload["cabin"] == cabin
        assert after.payload["seat_by_passenger"] == {me: "1A"}
        assert after.payload["email_date"] == filled.payload["email_date"]  # the newer one's
        assert (after.told, after.sync_state) == (told, "synced") and log.sent == sent_before


def a_moved_leg() -> tuple[corpus.Entry, dict, corpus.Entry, rules.Item]:
    """A leg of the owner's that a later email of the corpus moves under another key (another
    number that day, or another day): the email that booked it, the leg's truth, and the later
    email with the leg it moves to, as that email is read."""
    filed: set[str] = set()
    for later in the_corpus().entries:
        moves = [
            i
            for i in later.outcome["items"]
            if i["kind"] == "flight" and i["result"] == "updated" and i["key"] not in filed
        ]
        filed |= {i["key"] for i in later.outcome["items"]}
        for res in later.truth["reservations"]:
            moved = validated(later, plain(res))
            if not any(i["key"] == moved.key for i in moves) or not owners(res):
                continue
            for entry, leg in bookings("flight"):
                item = validated(entry, leg)
                if (
                    entry.name < later.name
                    and item.pnr == moved.pnr
                    and (item.payload["origin"], item.payload["destination"])
                    == (moved.payload["origin"], moved.payload["destination"])
                ):
                    return entry, leg, later, moved
    raise AssertionError("the corpus moves no leg of the owner's")


def test_a_flight_typed_without_its_code_moves_with_its_change_email_in_the_flight_log():
    """Typed in with every field but its code, the owner's flight is sent; its confirmation
    fills the code in; a change email that later moves the flight under another key finds the
    booking by that code and moves it: one booking, one flight in the flight log, created once
    and then updated."""
    original, leg, change, moved = a_moved_leg()
    flight = validated(original, leg)
    assert moved.key != flight.key
    with filing() as ctx:
        st, log = ctx.trips, ctx.airtrail
        ti = st.add_by_hand("flight", typed(flight, confirmation_code=None))
        assert (ti.sync_state, ti.confirmation) == ("pending", None)
        ctx.syncer.run_pass()
        sent = st.item(ti.id)
        assert sent.sync_state == "synced" and [r[0] for r in log.sent] == ["create"]
        got = file_email(ctx, original)
        (o,) = [o for o in got.outcomes if o.trip_item_id == ti.id]
        assert (o.result, o.action.reason) == ("updated", "fills_in")
        assert st.item(ti.id).confirmation == flight.pnr
        later = file_email(ctx, change)
        (m,) = [o for o in later.outcomes if o.trip_item_id == ti.id]
        assert (m.result, m.action.reason) == ("updated", "rescheduled")
        after = st.item(ti.id)
        assert after.item_key == moved.key and flight.key in after.payload["alt_keys"]
        assert under(ctx, moved.key) == [ti.id] and under(ctx, flight.key) == []
        # the one flight it was sent as now says the new leg; no second one was made for it
        assert after.airtrail_flight_id == sent.airtrail_flight_id
        assert after.sync_state == "synced"
        made = [r[1]["flightNumber"] for r in log.sent if r[0] == "create"]
        assert made.count(flight.payload["flight_code"]) == 1
        assert moved.payload["flight_code"] not in made
        assert [r[0] for r in log.sent if r[0] == "update"] and all(
            r[1] == sent.airtrail_flight_id
            for r in log.sent
            if r[0] == "update" and r[2]["flightNumber"] == moved.payload["flight_code"]
        )
        (held,) = [f for f in log.flights if f["id"] == sent.airtrail_flight_id]
        assert held["flightNumber"] == moved.payload["flight_code"]


def test_a_later_email_completes_a_draft_added_by_hand_under_the_same_code():
    """Typed in without its airline, the leg is a draft under its code; its own email then
    completes it: one booking under the real key, the draft's key kept as an alias."""
    entry, res = booking("flight", owners)
    flight = validated(entry, res)
    with filing(flight_log=False) as ctx:
        st = ctx.trips
        draft = st.add_by_hand("flight", typed(flight, carrier=None))
        assert is_draft(draft.item_key) and draft.checks == ("carrier",)
        assert draft.confirmation == flight.pnr
        got = file_email(ctx, entry)
        (o,) = [o for o in got.outcomes if o.trip_item_id == draft.id]
        assert (o.result, o.action.reason) == ("updated", "completes_draft")
        done = st.item(draft.id)
        assert done.item_key == flight.key and done.payload["alt_keys"] == [draft.item_key]
        assert done.payload["carrier"] == flight.payload["carrier"] and done.checks == ()
        assert done.source_message_id == got.message_row_id
        assert under(ctx, flight.key) == [draft.id]
        rows_of = ctx.ledger.items.by_message(got.message_row_id)
        assert [r["detail"] for r in rows_of if r["trip_item_id"] == draft.id] == [
            "completes_draft"
        ]


def a_stay_or_car(kind: str) -> tuple[corpus.Entry, rules.Item]:
    """The corpus's first stay or car (`kind`) with no finding, as its email is read."""
    entry, res = booking(kind)
    return entry, validated(entry, res)


def reread(ctx: Context, entry: corpus.Entry) -> MessageResult:
    """The entry's email read again (`replay --force`)."""
    return runmod.process_raw(
        ctx, entry.raw, source="eval", received=corpus.sent(entry), notify=False, force=True
    )


def test_a_stay_or_a_car_typed_without_its_code_is_found_by_its_confirmation():
    """Typed in with its name and its days but no code, a stay or a car is under a key of its
    name and day, not its code's. Its confirmation, which no booking holds under its code, finds
    it by its days: it fills the code in, the code's key becomes the booking's alias, and the
    confirmation read again is a duplicate of it, not a second booking."""
    for kind in ("lodging", "car"):
        entry, item = a_stay_or_car(kind)
        with filing(flight_log=False) as ctx:
            st = ctx.trips
            ti = st.add_by_hand(kind, typed(item, confirmation_code=None))
            assert not is_draft(ti.item_key) and ti.item_key != item.key, kind
            got = file_email(ctx, entry)
            (o,) = [o for o in got.outcomes if o.action.item.key == item.key]
            assert (o.result, o.action.reason, o.trip_item_id) == ("updated", "fills_in", ti.id)
            after = st.item(ti.id)
            assert after.item_key == ti.item_key and after.payload["alt_keys"] == [item.key]
            assert after.confirmation == after.payload["confirmation_code"] == item.pnr
            assert after.source_message_id == got.message_row_id
            assert "change_recorded" not in after.payload
            assert st.find(item.key).id == ti.id and under(ctx, item.key) == []
            assert [x.id for x in everything(ctx) if x.kind == kind] == [ti.id], kind
            again = reread(ctx, entry)
            (o,) = [o for o in again.outcomes if o.action.item.key == item.key]
            assert (o.result, o.trip_item_id) == ("duplicate", ti.id), kind
            assert [x.id for x in everything(ctx) if x.kind == kind] == [ti.id], kind


def test_a_stay_typed_without_its_name_and_code_is_completed_by_its_confirmation():
    """With neither its name nor its code a stay is a draft; its confirmation fills both in and
    the draft takes the real key its code makes, keeping the draft's as an alias."""
    entry, stay = a_stay_or_car("lodging")
    with filing(flight_log=False) as ctx:
        st = ctx.trips
        draft = st.add_by_hand(
            "lodging", typed(stay, confirmation_code=None, property_name=None)
        )
        assert is_draft(draft.item_key) and draft.trip_id is not None
        got = file_email(ctx, entry)
        (o,) = [o for o in got.outcomes if o.action.item.key == stay.key]
        assert (o.result, o.action.reason, o.trip_item_id) == ("updated", "fills_in", draft.id)
        done = st.item(draft.id)
        assert done.item_key == stay.key and done.payload["alt_keys"] == [draft.item_key]
        assert done.payload["property_name"] == stay.payload["property_name"]
        assert done.confirmation == stay.pnr and done.checks == ()
        assert [x.id for x in everything(ctx) if x.kind == "lodging"] == [draft.id]


def test_a_confirmation_that_fits_no_single_stay_typed_in_is_filed_on_its_own():
    """Two stays typed in that both fit the email's nights, one for other nights, one that checks
    in on the email's day but out on another, one under another property's name, and one the
    owner cancelled: the confirmation finds none of them and is filed as a booking of its own,
    and they are left as they were. A car typed in with its pick-up day alone says nothing
    beyond its start, and is not taken either."""
    entry, stay = a_stay_or_car("lodging")
    other = next(
        validated(e, r)
        for e, r in bookings("lodging")
        if validated(e, r).payload["property_name"] != stay.payload["property_name"]
    )
    alone = typed(stay, confirmation_code=None)
    nameless = typed(stay, confirmation_code=None, property_name=None)
    elsewhere = {**alone, "property_name": other.payload["property_name"]}
    nights = ("check_in", "check_out")
    later = {**alone, **{f: moved(stay.payload[f], 1) for f in nights if stay.payload.get(f)}}
    longer = {**alone, "check_out": moved(stay.payload["check_out"], 1)}
    car_entry, car = a_stay_or_car("car")
    pickup_only = {"pickup_time": car.payload["pickup_time"]}
    for case, kind, email, key, typed_in, cancel in (
        ("two that fit", "lodging", entry, stay.key, [alone, nameless], False),
        ("other nights", "lodging", entry, stay.key, [later], False),
        ("another check-out", "lodging", entry, stay.key, [longer], False),
        ("another property", "lodging", entry, stay.key, [elsewhere], False),
        ("cancelled", "lodging", entry, stay.key, [alone], True),
        ("pick-up day alone", "car", car_entry, car.key, [pickup_only], False),
    ):
        with filing(flight_log=False) as ctx:
            st = ctx.trips
            hand = [st.add_by_hand(kind, fields) for fields in typed_in]
            if cancel:
                hand = [st.set_status(ti.id, "cancelled") for ti in hand]
            got = file_email(ctx, email)
            (o,) = [o for o in got.outcomes if o.action.item.key == key]
            assert o.result == "filed" and o.trip_item_id not in {ti.id for ti in hand}, case
            assert under(ctx, key) == [o.trip_item_id], case
            for ti in hand:
                assert dataclasses.asdict(st.item(ti.id)) == dataclasses.asdict(ti), case


def test_a_stay_typed_in_under_part_of_its_name_is_found_by_its_confirmation():
    """A name agrees by its words, whatever their case, when one's words are all in the other's:
    a stay typed in under its name's first words, in capitals, is the stay its confirmation is
    for."""
    entry, stay = a_stay_or_car("lodging")
    words = stay.payload["property_name"].split()
    assert len(words) > 1
    short = typed(stay, confirmation_code=None, property_name=" ".join(words[:-1]).upper())
    with filing(flight_log=False) as ctx:
        st = ctx.trips
        ti = st.add_by_hand("lodging", short)
        got = file_email(ctx, entry)
        (o,) = [o for o in got.outcomes if o.action.item.key == stay.key]
        assert (o.result, o.action.reason, o.trip_item_id) == ("updated", "fills_in", ti.id)
        after = st.item(ti.id)
        assert after.payload["property_name"] == short["property_name"]  # as typed
        assert after.confirmation == stay.pnr and st.find(stay.key).id == ti.id


def test_adding_what_is_already_there_is_refused_with_a_message(tmp_path, base):
    """A second add of a booking that holds the same key, a double submit included, stores
    nothing and says why: in your trips, or in Recently deleted once it was removed."""
    flight = validated(*booking("flight", owners))
    stay = validated(*booking("lodging"))
    with a_page(tmp_path, base) as (c, st):
        for item in (flight, stay):
            word = views.EDIT_WHAT[item.kind]
            form = form_for(item)
            assert add(c, word, form).status_code == 303
            before = rows(st.led)
            again = add(c, word, form)
            assert again.status_code == 400 and alert(again.text) == ("edit-error", THERE)
            assert values(again.text)["confirmation_code"] == form["confirmation_code"]
            assert rows(st.led) == before
            with pytest.raises(ValueError, match=THERE):
                st.add_by_hand(item.kind, typed(item))
            st.remove_item(st.item_by_key(item.key).id)
            before = rows(st.led)
            hidden = add(c, word, form)
            assert hidden.status_code == 400 and alert(hidden.text) == ("edit-error", PUT_AWAY)
            assert rows(st.led) == before


def test_a_flight_added_by_hand_reaches_the_flight_log_only_with_the_owner_on_it():
    """With a flight log kept: the owner named, it waits and one pass sends it; only a companion
    named, it is off and nothing is sent; nobody named, it is held for the owner's two answers
    and sent only after a yes, never after a no."""
    (e1, mine), (e2, yes), (e3, no) = owners_flights(3)
    e4, theirs = booking("flight", a_companion_alone)
    st, log, syncer = kept(Ledger.memory())
    owners_leg = st.add_by_hand("flight", typed(validated(e1, mine)))
    assert (owners_leg.sync_state, owners_leg.checks) == ("pending", ())
    companions = st.add_by_hand("flight", typed(validated(e4, theirs)))
    assert (companions.sync_state, companions.checks) == ("off", ())
    nameless = [
        st.add_by_hand("flight", typed(validated(e, res), passengers=None))
        for e, res in ((e2, yes), (e3, no))
    ]
    for ti in nameless:
        assert (ti.sync_state, ti.checks) == ("blocked", ("owner_unknown",))
        assert checks.actions(ti.checks, hold=ti.sync_hold, flight_log=True) == {"on_it"}
    syncer.run_pass()
    assert [r[0] for r in log.sent] == ["create"] and len(log.flights) == 1
    assert st.item(owners_leg.id).sync_state == "synced"
    assert st.item(companions.id).sync_state == "off"
    assert [st.item(ti.id).sync_state for ti in nameless] == ["blocked", "blocked"]
    # the owner's answers: a yes sends it at the next pass, a no keeps it out
    assert st.owner_answer(nameless[0].id, True).sync_state == "pending"
    assert st.owner_answer(nameless[1].id, False).sync_state == "off"
    syncer.run_pass()
    assert [r[0] for r in log.sent] == ["create", "create"] and len(log.flights) == 2
    sent = {ti.airtrail_flight_id for ti in (st.item(owners_leg.id), st.item(nameless[0].id))}
    assert sent == {f["id"] for f in log.flights}
    assert st.item(companions.id).airtrail_flight_id is None
    assert st.item(nameless[1].id).airtrail_flight_id is None


# ----------------------------------------------------------------------------- the page
def test_the_four_forms_render_post_and_come_back_to_the_booking(tmp_path, base):
    with a_page(tmp_path, base) as (c, st):
        for word in ("lodging", "boat", "flights"):  # any other word is no form
            assert c.get(f"/add/{word}").status_code == 404
            assert add(c, word, {}).status_code == 404
        assert rows(st.led) == []
        for kind, word in views.EDIT_WHAT.items():
            item = validated(*booking(kind))
            page = c.get(f"/add/{word}")
            assert page.status_code == 200, word
            assert f"<title>Add a {word} · Trips</title>" in page.text
            assert f"<h1>Add a {word}</h1>" in page.text and ">Add</button>" in page.text
            assert f'href="{c.url("/add")}">Cancel</a>' in page.text
            assert 'name="shown"' not in page.text and 'name="seen"' not in page.text
            assert options(page.text)[0] == ("", "Decide by the dates")
            shown = values(page.text)
            assert shown["trip"] == ""
            assert not "".join(v for k, v in shown.items() if k != "passengers")
            # the travellers start with the owner's name, on the forms that list them
            assert shown.get("passengers") == (ME if kind in views.EDIT_NAMES else None)
            r = add(c, word, form_for(item))
            ti = st.item_by_key(item.key)
            assert r.status_code == 303, word
            assert r.headers["location"] == c.url(f"/trips/{ti.trip_id}?f=added#item-{ti.id}")
            back = c.get(c.local(r.headers["location"]))
            assert back.status_code == 200 and f'id="item-{ti.id}"' in back.text
            assert 'data-flash="Added"' in back.text
            assert (ti.kind, ti.payload["source"], ti.source_message_id) == (kind, "hand", None)
        # with no date it waits in the list's unsorted group
        stay = validated(*booking("lodging"))
        bare = {"property_name": stay.payload["property_name"], "trip": ""}
        r = add(c, "hotel", bare)
        assert (r.status_code, r.headers["location"]) == (303, c.url("/?f=added#unsorted"))
        (waiting,) = st.unsorted()
        assert waiting.payload["property_name"] == bare["property_name"]
        assert f'id="item-{waiting.id}"' in c.get("/").text


def test_adding_from_a_trip_page_lands_in_that_trip(tmp_path, base):
    """From a trip's menu, or its empty-trip line, the chooser and the form carry the trip, which
    a booking whose dates the grouping would put elsewhere then lands in."""
    entry, res = booking("flight", owners)
    flight = validated(entry, res)
    day = rules.parse_local(flight.payload["dep_local"]).date()
    far = grouping.NEARBY_DAYS + grouping.TRIP_SLACK.days

    def away(r: dict) -> bool:
        return abs((rules.parse_local(r["check_in"]).date() - day).days) > far

    stay = validated(*booking("lodging", away))
    with a_page(tmp_path, base) as (c, st):
        filed = st.upsert_item(st.find_or_create(flight), flight)
        trip_id = filed.trip_id
        page = c.get(f"/trips/{trip_id}").text
        assert f'href="{c.url(f"/add?trip={trip_id}")}">Add a booking</a>' in page
        chooser = c.get(f"/add?trip={trip_id}").text
        links = re.findall(r'<a class="choice" href="([^"]+)"', chooser)
        assert links[-4:] == [
            c.url(f"/add/{word}?trip={trip_id}") for word in ("flight", "hotel", "car", "train")
        ]
        assert f'<a class="back" href="{c.url(f"/trips/{trip_id}")}"' in chooser
        form = c.get(f"/add/hotel?trip={trip_id}").text
        assert values(form)["trip"] == str(trip_id)
        assert f'href="{c.url(f"/add?trip={trip_id}")}">Cancel</a>' in form
        # a trip that is no trip is not chosen
        assert st.get(trip_id + 1) is None
        assert values(c.get(f"/add/hotel?trip={trip_id + 1}").text)["trip"] == ""
        r = add(c, "hotel", form_for(stay, trip=str(trip_id)), f"?trip={trip_id}")
        ti = st.item_by_key(stay.key)
        assert ti.trip_id == trip_id
        assert r.headers["location"] == c.url(f"/trips/{trip_id}?f=added#item-{ti.id}")
        trip = st.get(trip_id)
        assert trip.start_date <= ti.start_at[:10] <= trip.end_date
        # emptied, the trip says how to add to it, and is offered on the form all the same
        st.remove_item(filed.id)
        st.remove_item(ti.id)
        empty = c.get(f"/trips/{trip_id}").text
        line = re.search(r'<p class="empty">(.*?)</p>', empty, re.S).group(1)
        assert f'href="{c.url(f"/add?trip={trip_id}")}"' in line
        form = c.get(f"/add/car?trip={trip_id}").text
        assert values(form)["trip"] == str(trip_id)


def test_an_empty_form_is_refused_with_a_message(tmp_path, base):
    """Nothing filled in, or only the travellers (the owner's name the flight and train forms
    start with), is no booking."""
    with a_page(tmp_path, base) as (c, st):
        for word in ("flight", "hotel", "car", "train"):
            shown = values(c.get(f"/add/{word}").text)
            blank = {**shown, "passengers": ""} if "passengers" in shown else shown
            for form in (shown, blank, {**blank, "confirmation_code": "   "}):
                r = add(c, word, form)
                assert r.status_code == 400, word
                assert alert(r.text) == ("edit-error", EMPTY), word
                assert r.text.index('role="alert"') < r.text.index("<form")
        assert rows(st.led) == []
        with pytest.raises(ValueError, match=EMPTY):
            st.add_by_hand("lodging", {"address": " ", "check_in": ""})
        for kind in views.EDIT_NAMES:
            with pytest.raises(ValueError, match=EMPTY):
                st.add_by_hand(kind, {"passengers": [ME, *(name for name, _ in book().guests)]})
        assert rows(st.led) == []


def test_the_add_form_refuses_what_the_edit_form_refuses(tmp_path, base):
    """A time with no date, a flight number that is not one, and a stay that ends before it
    starts are refused, tied to their field, with what was typed kept; an airline designator
    typed before the number is dropped."""
    flight = validated(*booking("flight", owners))
    stay = validated(*booking("lodging"))
    p = flight.payload
    with a_page(tmp_path, base) as (c, st):
        for over, field, sentence in (
            ({"departs_date": ""}, "departs_date", "departure is not a date"),
            ({"flight_number": "soon"}, "flight_number", "Flight number is not a number"),
        ):
            r = add(c, "flight", form_for(flight, **over))
            assert r.status_code == 400 and alert(r.text) == ("edit-error", sentence), field
            assert tied(r.text, field) == [field]
            assert values(r.text)["origin"] == p["origin"]
        shown = views._edit_values(held(stay))
        swapped = {
            "check_in_date": shown["check_out_date"],
            "check_out_date": shown["check_in_date"],
            "check_in_time": "",
            "check_out_time": "",
        }
        r = add(c, "hotel", form_for(stay, **swapped))
        assert r.status_code == 400
        assert alert(r.text) == ("edit-error", "check-out is before check-in")
        assert tied(r.text, "check_out_date") == ["check_out_date"]
        assert rows(st.led) == []
        designated = f"{p['carrier']} {p['flight_number']}"
        assert add(c, "flight", form_for(flight, flight_number=designated)).status_code == 303
        assert st.item_by_key(flight.key).payload["flight_number"] == p["flight_number"]


def test_every_control_of_the_add_forms_has_a_label(tmp_path, base):
    with a_page(tmp_path, base) as (c, st):
        for path in ("/add", *(f"/add/{w}" for w in views.EDIT_WHAT.values())):
            r = c.get(path)
            controls = Controls()
            controls.feed(r.text)
            assert controls.unlabelled == [], path
            if path != "/add":
                assert "trip" in controls.found and len(controls.found) >= 6, path
            csp = r.headers.get("content-security-policy", "")
            assert "script-src 'self'" in csp and "form-action 'self'" in csp, path
            (script,) = re.findall(r"<script\b[^>]*>", r.text, re.I)
            assert script.startswith(f'<script src="{c.url("/static/app.js")}?v='), path
            assert " style=" not in r.text and "<style" not in r.text, path
            assert "data-refresh" not in r.text, path  # a reload would drop what was typed
        # refused, the form is labelled the same
        r = add(c, "train", {"trip": ""})
        controls = Controls()
        controls.feed(r.text)
        assert r.status_code == 400 and controls.unlabelled == [] and "trip" in controls.found


def test_a_booking_added_by_hand_is_the_owners_edit_dated_now_by_default():
    """With no time given, the entry is stamped now; a field the page does not edit, or a kind it
    does not add, is refused and stores nothing."""
    flight = validated(*booking("flight", owners))
    st = store()
    before = dt.datetime.now(dt.UTC).replace(microsecond=0)
    ti = st.add_by_hand("flight", typed(flight, passengers=None))
    stamps = set(ti.payload["owner_edits"].values())
    assert len(stamps) == 1 and dt.datetime.fromisoformat(stamps.pop()) >= before
    for kind, fields in (("flight", {"price": "1"}), ("boat", {"provider": "x"})):
        with pytest.raises(ValueError):
            st.add_by_hand(kind, fields)
    assert [r["id"] for r in rows(st.led)] == [ti.id]
    assert dataclasses.asdict(st.item(ti.id)) == dataclasses.asdict(ti)
