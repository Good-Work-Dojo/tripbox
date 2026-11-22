# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A flight is written to the flight log only when the owner is on that leg, by any path. Each
test takes one path a flight can travel — an edit on the page, Looks right, a booking added by
hand, an upload, a button in Telegram, a replay, an email whose names are inherited — with a
flight that names a companion, a stranger or nobody, and asserts on the flight log itself (an
AirTrail held in memory that records every request) what it was sent. Two ways make a flight the
owner's, and only two: the owner's answer that they were on it, and the owner's own name typed
into its travellers. The last test takes every path again, each beside the owner's own flights,
and finds that every create and every update the flight log ever received carried the owner.
Every booking, airport and traveller is read from the corpus at run time."""

import contextlib
import copy
from collections.abc import Callable, Iterator

from helpers.flightlog import Scripted, named
from test_add_by_hand import typed
from test_bot import NOTHING_LEFT, bot_for, ok_of, tap
from test_checks import (
    a_companion_alone,
    book,
    booking,
    day,
    edit,
    moved,
    names,
    plain,
    stamps,
    strangers_alone,
    the_corpus,
    validated,
)
from test_filed import UNSURE, deliver, email_for, filing, model_reads, owners_flight, whole
from test_store_actions import an_hour_later, nameless, owners_flights

from tripbox import corpus, uploads, worker
from tripbox.parse import pipeline as pl
from tripbox.pipeline import run as runmod
from tripbox.pipeline.run import Context, process_raw, sync
from tripbox.trips.store import TripItem
from tripbox.validate import checks, rules
from tripbox.write.airtrail import MEMORY_USER_ID


# ----------------------------------------------------------------------------- the flight log
def owner_aboard(body: dict) -> bool:
    """Whether a create's or an update's body carries the owner's user id among its passengers."""
    return any(p.get("userId") == MEMORY_USER_ID for p in body.get("passengers") or [])


def riders(flight: dict) -> list[str]:
    """Who a flight in the flight log carries: `me` for the owner, anyone else by name."""
    return sorted("me" if p.get("user") else p["guestName"] for p in flight["passengers"])


def aboard(*printed: str) -> list[str]:
    """Who a flight naming `printed` should carry in the flight log: `me` for the owner, each
    companion by the travellers file's name for them; nobody else."""
    found = book().classify(list(printed))
    return sorted((["me"] if found.me_present else []) + list(found.guests))


class Since:
    """What the flight log of `ctx` is asked and holds from now on."""

    def __init__(self, ctx: Context) -> None:
        self.log: Scripted = ctx.airtrail
        self.start = len(self.log.requests)
        self.held = {f["id"] for f in self.log.flights}

    def sent(self) -> list[str]:
        """The creates and updates asked for since, in order."""
        return [r[0] for r in self.log.requests[self.start :] if r[0] in ("create", "update")]

    def new(self) -> list[dict]:
        """The flights it holds that it did not hold then, as they stand now."""
        return [f for f in self.log.flights if f["id"] not in self.held]


class Ledgers:
    """Fresh ledgers that keep a flight log, each with the owner's own flights filed first when
    `beside`; every flight log they kept, to read back."""

    def __init__(self, monkeypatch, *, beside: bool) -> None:
        self.monkeypatch = monkeypatch
        self.beside = beside
        self.logs: list[Scripted] = []

    @contextlib.contextmanager
    def __call__(self) -> Iterator[Context]:
        with filing() as ctx:
            self.logs.append(ctx.airtrail)
            if self.beside:
                entry, res = the_owners_own()
                got = deliver(ctx, self.monkeypatch, entry, [res], n=0)
                assert got.outcomes and {o.sync for o in got.outcomes} == {"synced"}
            yield ctx

    def only_the_owners(self) -> None:
        """Every create and update any of the flight logs was asked for carried the owner."""
        for log in self.logs:
            for request in log.sent:
                assert owner_aboard(request[-1]), request[0]


def a_story(story: Callable, monkeypatch, tmp_path) -> None:
    """One path, in ledgers of its own; then every request its flight logs received."""
    ledgers = Ledgers(monkeypatch, beside=False)
    story(ledgers, monkeypatch, tmp_path)
    ledgers.only_the_owners()


# ----------------------------------------------------------------------------- the bookings
def the_owners_own() -> tuple[corpus.Entry, dict]:
    """The last booking of flights the corpus files with no finding that names the owner, all
    its legs kept: the flights filed beside each path."""
    found = None
    for entry in the_corpus().entries:
        for res in entry.truth["reservations"]:
            legs = res.get("segments") or []
            if res["kind"] != "flight" or not legs or not book().classify(names(res)).me_present:
                continue
            filed = [i for i in entry.outcome["items"] if i["result"] == "filed"]
            if not any(i["kind"] == "flight" for i in filed):
                continue
            if not any(checks.codes(validated(entry, plain(res, n))) for n in range(len(legs))):
                found = (entry, whole(res))
    assert found is not None
    return found


def a_round_trip() -> tuple[corpus.Entry, dict, str, list[str]]:
    """The first booking of two flights or more the corpus files with no finding that names the
    owner and two companions or more: its email, the booking with every leg, the owner's name and
    the companions' names as printed."""
    for entry in the_corpus().entries:
        for res in entry.truth["reservations"]:
            legs = res.get("segments") or []
            if res["kind"] != "flight" or len(legs) < 2:
                continue
            if any(checks.codes(validated(entry, plain(res, n))) for n in range(len(legs))):
                continue
            printed = names(res)
            me = [p for p in printed if book().match(p).kind == "me"]
            them = [p for p in printed if book().match(p).kind == "guest"]
            if me and len(them) >= 2:
                return entry, whole(res), me[0], them
    raise AssertionError("the corpus files no such booking")


def leg(trip: dict, n: int, people: list[str], status: str | None = None, **over) -> dict:
    """Leg `n` of `trip` alone, naming `people`, with `over` in place of some of its fields."""
    out = copy.deepcopy(trip)
    out["segments"] = [{**out["segments"][n], **over}]
    out["passengers"] = list(people)
    if status is not None:
        out["status"] = status
    return out


def another_number(number: str) -> str:
    """Another flight number: `number` less one (one more, for the first)."""
    n = int(number)
    return str(n - 1 if n > 1 else n + 1)


def an_hour_on(segment: dict) -> dict:
    """A leg's departure and arrival an hour later."""
    return {
        "departure_local": an_hour_later(segment["departure_local"]),
        "arrival_local": an_hour_later(segment["arrival_local"]),
    }


def a_model_reading(people) -> corpus.Entry:
    """The first email the model reads whose bookings are all flights, each filed with no check
    and naming whom `people` accepts (its outcome's counts)."""
    for entry in the_corpus().entries:
        items = entry.outcome["items"]
        if entry.answer is None or entry.outcome["status"] != "processed" or not items:
            continue
        if all(
            i["kind"] == "flight"
            and i["result"] == "filed"
            and not i["booking"]["checks"]
            and people(i["people"])
            for i in items
        ):
            return entry
    raise AssertionError("the corpus holds no such email")


class Counted:
    """The model, answered from the corpus, counting how often it is asked."""

    def __init__(self) -> None:
        self.inner = corpus.CorpusAnswers(the_corpus())
        self.model = self.inner.model
        self.calls = 0

    def extract(self, nm, hint=None):
        self.calls += 1
        return self.inner.extract(nm, hint)


def theirs(ctx: Context, ti: TripItem) -> bool:
    """Whether the booking is anything but the owner's flight."""
    return checks.owner(ti.kind, ti.passengers, ti.payload, ctx.book) != "on"


# ----------------------------------------------------------------------------- the paths
def a_page_edit(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """A companion's flight, strangers' flight and a flight that names nobody, each with its
    times, its number and its airports edited on the page in turn, a pass after each: none
    becomes the owner's and nothing is sent."""
    with ledgers() as ctx:
        since = Since(ctx)
        nobody = owners_flights(2)[1]
        flights = (
            booking("flight", a_companion_alone),
            booking("flight", strangers_alone),
            (nobody[0], nameless(nobody[1])),
        )
        for n, (entry, res) in enumerate(flights):
            (o,) = deliver(ctx, monkeypatch, entry, [res], n=n).outcomes
            filed = ctx.trips.item(o.trip_item_id)
            segment = res["segments"][0]
            at = stamps(entry, 3)  # after the email: what the owner types wins
            edits = (
                {
                    "dep_local": an_hour_on(segment)["departure_local"],
                    "arr_local": an_hour_on(segment)["arrival_local"],
                },
                {"flight_number": another_number(segment["flight_number"])},
                {"origin": segment["destination"], "destination": segment["origin"]},
            )
            for when, fields in zip(at, edits, strict=True):
                ctx.trips.edit_item(filed.id, fields, at=when)
                sync(ctx)
                after = ctx.trips.item(filed.id)
                assert theirs(ctx, after), fields
                assert after.passengers == filed.passengers, fields
                want = "blocked" if not filed.passengers else "off"  # nobody named waits
                assert after.sync_state == want and after.airtrail_flight_id is None, fields
        assert since.sent() == [] and since.new() == []


def a_look(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Looks right on a companion's flight read below the confidence floor, on strangers' flight
    too far out, and on a flight that names nobody read below the floor: their doubts close and
    none is sent; on the last the question who was on it stays open."""
    with ledgers() as ctx:
        since = Since(ctx)
        entry, mine = owners_flight()
        strangers_entry, strangers = booking("flight", strangers_alone)
        far = too_far(strangers_entry, strangers["segments"][0])
        flights = (
            (*booking("flight", a_companion_alone), {"confidence": UNSURE}, ("low_confidence",)),
            (
                strangers_entry,
                edit(
                    strangers,
                    departure_local=moved(strangers["segments"][0]["departure_local"], far),
                    arrival_local=moved(strangers["segments"][0]["arrival_local"], far),
                ),
                {},
                ("date_far",),
            ),
            (entry, nameless(mine), {"confidence": UNSURE}, ("low_confidence", "owner_unknown")),
        )
        for n, (e, res, how, found) in enumerate(flights):
            (o,) = deliver(ctx, monkeypatch, e, [res], n=n, **how).outcomes
            assert o.checks == found, found
            ctx.trips.looks_right(o.trip_item_id)
            sync(ctx)
            after = ctx.trips.item(o.trip_item_id)
            assert theirs(ctx, after), found
            held = ("owner_unknown",) if "owner_unknown" in found else ()
            assert after.checks == held, found
            assert after.sync_state == ("blocked" if held else "off"), found
        assert since.sent() == [] and since.new() == []


def too_far(entry: corpus.Entry, segment: dict) -> int:
    """Days to move a leg of `entry`'s by for it to be too far after its email."""
    ahead = (day(segment["departure_local"]) - corpus.sent(entry).date()).days
    return rules.FUTURE_LIMIT.days + 7 - ahead


def added_by_hand(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Flights typed in on the page naming a companion, strangers, and nobody: none is sent; the
    one naming nobody waits for the owner's answer."""
    with ledgers() as ctx:
        since = Since(ctx)
        st = ctx.trips
        companion = booking("flight", a_companion_alone)
        strangers = booking("flight", strangers_alone)
        nobody = owners_flights(2)[1]
        added = [
            st.add_by_hand("flight", typed(validated(*companion))),
            st.add_by_hand("flight", typed(validated(*strangers))),
            st.add_by_hand("flight", typed(validated(*nobody), passengers=None)),
        ]
        assert [(ti.sync_state, ti.checks) for ti in added] == [
            ("off", ()),
            ("off", ()),
            ("blocked", ("owner_unknown",)),
        ]
        sync(ctx)
        assert [st.item(ti.id).sync_state for ti in added] == ["off", "off", "blocked"]
        assert all(theirs(ctx, st.item(ti.id)) for ti in added)
        assert since.sent() == [] and since.new() == []


def an_upload(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """An email handed in on the page, read as a flight naming only a companion, and a paste read
    as strangers' flight: the owner's own input settles who sent it, not who flew, so neither is
    sent."""
    with ledgers() as ctx:
        since = Since(ctx)
        companion = booking("flight", a_companion_alone)
        strangers = booking("flight", strangers_alone)
        handed_in = (  # an email naming a companion alone; a paste naming strangers alone
            (companion[1], email_for(companion[0], 7), "booking.eml"),
            (strangers[1], f"{strangers[0].name} pasted".encode(), None),
        )
        for res, body, name in handed_in:
            model_reads(monkeypatch, [res])
            token, _ = uploads.submit(ctx.ledger, body, name=name, submitted_by=None)
            assert worker.take_upload(ctx) == "filed", name
            row = ctx.ledger.uploads.get(token)
            (item,) = ctx.ledger.items.by_message(row["message_id"])
            ti = ctx.trips.item(item["trip_item_id"])
            assert theirs(ctx, ti) and (ti.sync_state, ti.checks) == ("off", ()), name
        sync(ctx)
        assert since.sent() == [] and since.new() == []


def a_telegram_button(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Looks right tapped on a companion's flight read below the confidence floor, I was on this
    flight tapped on it too (a flight that names someone is not asked), and Send again tapped on
    a flight the owner was taken off after it was deleted there by hand: nothing is sent."""
    entry, trip, me, (companion, *_) = a_round_trip()
    with ledgers() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        since = Since(ctx)
        theirs_entry, their_flight = booking("flight", a_companion_alone)
        (o,) = deliver(ctx, monkeypatch, theirs_entry, [their_flight], confidence=UNSURE).outcomes
        card, keyboard = tg.sent[-1]
        assert tap(bot, tg, ok_of(keyboard), text=card)[0] == "Marked as right"
        assert tap(bot, tg, f"ck:{o.trip_item_id}:me")[0] == NOTHING_LEFT
        after = ctx.trips.item(o.trip_item_id)
        assert theirs(ctx, after) and (after.checks, after.sync_state) == ((), "off")
        assert since.sent() == []
        # the owner's flight with a companion, sent, then deleted there by hand
        (o,) = deliver(ctx, monkeypatch, entry, [leg(trip, 0, [me, companion])], n=1).outcomes
        (held,) = since.new()
        ctx.airtrail.deleted_by_hand(held["id"])
        at = stamps(entry, 2)
        ctx.trips.edit_item(o.trip_item_id, an_hour_dep(ctx, o.trip_item_id), at=at[0])
        sync(ctx)  # the pass finds it gone there and lets it go
        assert ctx.trips.item(o.trip_item_id).sync_hold == "deleted"
        # the owner takes themselves off it, then taps Send again
        ctx.trips.edit_item(o.trip_item_id, {"passengers": [companion]}, at=at[1])
        tap(bot, tg, f"ck:{o.trip_item_id}:again")
        after = ctx.trips.item(o.trip_item_id)
        assert theirs(ctx, after) and (after.sync_hold, after.sync_state) == (None, "off")
        sync(ctx)
        assert since.sent() == ["create"] and since.new() == []  # the one the owner deleted


def an_hour_dep(ctx: Context, item_id: int) -> dict:
    """A flight's departure and arrival as the store holds them, an hour later."""
    p = ctx.trips.item(item_id).payload
    return {"dep_local": an_hour_later(p["dep_local"]), "arr_local": an_hour_later(p["arr_local"])}


def a_replay(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """The corpus's email of a companion's flights and the one of strangers' flights, filed and
    read again by force, once from the answer stored with each and once with the model asked
    again: none of their flights is sent."""
    with ledgers() as ctx:
        monkeypatch.setattr(runmod, "parse_message", pl.parse_message)  # the model is asked
        model = Counted()
        ctx.model = model
        since = Since(ctx)
        companions = a_model_reading(lambda p: p["me"] == 0 and p["guests"] and not p["others"])
        strangers = a_model_reading(lambda p: p["me"] == 0 and not p["guests"] and p["others"])
        for entry in (companions, strangers):
            first = process_raw(ctx, entry.raw, source="imap", received=corpus.sent(entry))
            assert first.status == "processed" and first.outcomes, entry.name
            for reread in (False, True):
                asked = model.calls
                again = process_raw(ctx, entry.raw, source="replay", force=True, reread=reread)
                assert again.message_row_id == first.message_row_id, entry.name
                assert model.calls == asked + reread, entry.name
                assert len(again.outcomes) == len(first.outcomes), entry.name
                for o in again.outcomes:
                    ti = ctx.trips.item(o.trip_item_id)
                    assert theirs(ctx, ti) and ti.sync_state == "off", entry.name
        assert since.sent() == [] and since.new() == []


def inherited_from_an_unverified_sender(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """The owner's flight with companions, sent. A change notice that names nobody, forwarded
    from a sender no list vouches for, takes its names from the booking it is for: the booking is
    the owner's all the same, and its change waits for the owner's look; a notice that names the
    owner waits the same. Strangers' flight from that sender is no check: it is not sent at all.
    Once the owner has looked, the change goes, with the owner aboard."""
    entry, flight = owners_flight()
    segment = flight["segments"][0]
    with ledgers() as ctx:
        since = Since(ctx)
        (o,) = deliver(ctx, monkeypatch, entry, [flight], n=0).outcomes
        (held,) = since.new()
        notice = nameless(edit(flight, **an_hour_on(segment)))
        how = {"doc": "change", "unvouched": True}
        (o2,) = deliver(ctx, monkeypatch, entry, [notice], n=1, **how).outcomes
        assert (o2.trip_item_id, o2.result) == (o.trip_item_id, "updated")
        ti = ctx.trips.item(o.trip_item_id)
        assert not theirs(ctx, ti) and aboard(*ti.passengers) == aboard(*flight["passengers"])
        assert (ti.checks, ti.sync_state) == (("unverified_sender",), "blocked")
        assert since.new() == [held] and since.sent() == ["create"]
        named = edit(flight, **{k: moved(v, 1) for k, v in an_hour_on(segment).items()})
        (o3,) = deliver(ctx, monkeypatch, entry, [named], n=2, **how).outcomes
        assert (o3.sync, o3.checks) == ("blocked", ("unverified_sender",))
        assert since.new() == [held] and since.sent() == ["create"]
        strangers_entry, strangers = booking("flight", strangers_alone)
        (o4,) = deliver(
            ctx, monkeypatch, strangers_entry, [strangers], n=3, unvouched=True
        ).outcomes
        assert (o4.result, o4.sync, o4.checks) == ("filed", "off", ())
        assert since.sent() == ["create"]
        assert ctx.trips.looks_right(o.trip_item_id).sync_state == "pending"
        sync(ctx)
        (moved_on,) = since.new()
        assert moved_on["departure"] == ctx.trips.item(o.trip_item_id).payload["dep_utc"]
        assert riders(moved_on) == aboard(*flight["passengers"])
        assert since.sent() == ["create", "update"]


def a_nameless_notice_for_a_leg_that_moved(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Two legs under one code, carrying different travellers. A notice that names nobody, for
    the outbound moved a day on or given another number that day, is for that leg's travellers,
    not the return's (filed last): with the owner on the outbound, the flight log's flight moves
    with the two it held; with the owner on the return only, the outbound that moves is not the
    owner's and is never sent."""
    entry, trip, me, (sam, robin, *_) = a_round_trip()
    out = trip["segments"][0]
    moves = (
        {"departure_local": moved(out["departure_local"], 1)}
        | {"arrival_local": moved(out["arrival_local"], 1)},
        {"flight_number": another_number(out["flight_number"])},
    )
    for outbound, back in (([me, sam], [sam, robin]), ([robin, sam], [me, sam])):
        for move in moves:
            label = (outbound, sorted(move))
            with ledgers() as ctx:
                since = Since(ctx)
                legs = [leg(trip, 0, outbound), leg(trip, 1, back)]
                got = deliver(ctx, monkeypatch, entry, legs)
                first, second = (ctx.trips.item(o.trip_item_id) for o in got.outcomes)
                (held,) = since.new()
                assert riders(held) == aboard(me, sam), label
                notice = leg(trip, 0, [], **move)
                (o,) = deliver(ctx, monkeypatch, entry, [notice], n=1, doc="change").outcomes
                assert (o.trip_item_id, o.result) == (first.id, "updated"), label
                now = ctx.trips.item(first.id)
                assert now.item_key == o.action.item.key != first.item_key, label
                assert sorted(now.passengers) == sorted(first.passengers), label
                assert ctx.trips.item(second.id).passengers == second.passengers, label
                if me in outbound:  # the one flight, moved, with the two it held
                    (held,) = since.new()
                    assert riders(held) == aboard(me, sam), label
                    assert held["flightNumber"] == now.payload["flight_code"], label
                    assert held["date"] == now.payload["date_local"], label
                    assert now.sync_state == "synced", label
                    assert since.sent() == ["create", "update"], label
                else:  # the return alone, as it was
                    assert (now.sync_state, now.airtrail_flight_id) == ("off", None), label
                    assert since.new() == [held] and since.sent() == ["create"], label


def a_new_leg_under_a_code_whose_legs_differ(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Two legs under one code carry different travellers, the owner on the return only. A notice
    that names nobody, for a flight no booking is (the outbound's route a day on, under another
    number: no booking it could have moved from), takes nobody's names, since the legs under its
    code carry different travellers: it is filed naming nobody, waits for the owner's answer and
    is not sent."""
    entry, trip, me, (sam, robin, *_) = a_round_trip()
    out = trip["segments"][0]
    with ledgers() as ctx:
        since = Since(ctx)
        deliver(ctx, monkeypatch, entry, [leg(trip, 0, [robin, sam]), leg(trip, 1, [me, sam])])
        (held,) = since.new()
        other = leg(
            trip,
            0,
            [],
            flight_number=another_number(out["flight_number"]),
            departure_local=moved(out["departure_local"], 1),
            arrival_local=moved(out["arrival_local"], 1),
        )
        (o,) = deliver(ctx, monkeypatch, entry, [other], n=1).outcomes
        assert (o.action.kind, o.result) == ("file", "filed")
        ti = ctx.trips.item(o.trip_item_id)
        assert (ti.passengers, ti.checks, ti.sync_state) == ([], ("owner_unknown",), "blocked")
        sync(ctx)
        assert since.new() == [held] and since.sent() == ["create"]


def a_cancelled_leg_beside_a_new_leg(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """The owner and a companion on a sent flight. One message lists that flight cancelled beside
    another number that day, in either order, the new leg naming everyone, only the owner, or
    only the companion. Everyone: the booking moves, both aboard. Only the owner: the companion
    leaves, the owner moves. Only the companion: the cancellation names the owner, which cancels
    the booking rather than taking a traveller off it, so the new leg still moves it and the
    booking is cancelled where it moved — never a live flight with the owner aboard that the
    email took them off. The flight log holds the one flight, moved, as the booking is."""
    entry, trip, me, (sam, *_) = a_round_trip()
    out = trip["segments"][0]
    onward_fields = {"flight_number": another_number(out["flight_number"]), **an_hour_on(out)}
    stories = (  # the cancelled leg names, the new leg names, the booking's status, who rides
        ([me, sam], [me, sam], "confirmed", aboard(me, sam)),
        ([sam], [me], "confirmed", aboard(me)),
        ([me], [sam], "cancelled", aboard(me, sam)),
    )
    for cancelled, onward, status, riding in stories:
        for old_first in (True, False):
            label = (cancelled, onward, old_first)
            with ledgers() as ctx:
                since = Since(ctx)
                (o,) = deliver(ctx, monkeypatch, entry, [leg(trip, 0, [me, sam])]).outcomes
                left = leg(trip, 0, cancelled, status="cancelled")
                new = leg(trip, 0, onward, **onward_fields)
                pair = [left, new] if old_first else [new, left]
                deliver(ctx, monkeypatch, entry, pair, n=1, doc="change")
                ti = ctx.trips.item(o.trip_item_id)
                assert (ti.status, aboard(*ti.passengers)) == (status, riding), label
                assert ti.payload["flight_code"].endswith(onward_fields["flight_number"]), label
                (held,) = since.new()
                assert held["flightNumber"] == ti.payload["flight_code"], label
                assert held["note"].startswith("CANCELLED ") is (status == "cancelled"), label
                assert riders(held) == riding, label
                assert since.sent() == ["create", "update"], label


def a_companions_new_leg_beside_their_cancelled_leg(
    ledgers: Ledgers, monkeypatch, tmp_path
) -> None:
    """The owner and a companion on a sent flight. One message cancels the companion's seat on it
    and lists the companion alone on another number that day, in either order. The new leg names
    only the traveller the cancellation takes off the booking, so it does not move the booking:
    the companion leaves it, the owner stays on the flight as it was, and the companion's new
    flight is a booking of its own, not the owner's and never sent. The flight log's flight
    stays where it was, with the owner alone."""
    entry, trip, me, (sam, *_) = a_round_trip()
    out = trip["segments"][0]
    onward_fields = {"flight_number": another_number(out["flight_number"]), **an_hour_on(out)}
    for old_first in (True, False):
        with ledgers() as ctx:
            since = Since(ctx)
            (o,) = deliver(ctx, monkeypatch, entry, [leg(trip, 0, [me, sam])]).outcomes
            before = ctx.trips.item(o.trip_item_id)
            left = leg(trip, 0, [sam], status="cancelled")
            new = leg(trip, 0, [sam], **onward_fields)
            pair = [left, new] if old_first else [new, left]
            got = deliver(ctx, monkeypatch, entry, pair, n=1, doc="change")
            took = {o.action.item.status: (o.action.kind, o.action.reason) for o in got.outcomes}
            assert took == {
                "cancelled": ("update", "remove_passengers"),
                "confirmed": ("file", ""),
            }, old_first
            ti = ctx.trips.item(o.trip_item_id)
            assert ti.status == "confirmed" and aboard(*ti.passengers) == aboard(me), old_first
            assert ti.item_key == before.item_key, old_first
            assert ti.payload["flight_code"] == before.payload["flight_code"], old_first
            assert not theirs(ctx, ti) and ti.sync_state == "synced", old_first
            (onward,) = [x for x in got.outcomes if x.trip_item_id != o.trip_item_id]
            theirs_now = ctx.trips.item(onward.trip_item_id)
            assert theirs_now.payload["flight_code"].endswith(onward_fields["flight_number"])
            assert aboard(*theirs_now.passengers) == aboard(sam), old_first
            assert theirs(ctx, theirs_now) and theirs_now.sync_state == "off", old_first
            (held,) = since.new()
            assert held["flightNumber"] == before.payload["flight_code"], old_first
            assert riders(held) == aboard(me), old_first
            assert since.sent() == ["create", "update"], old_first


def seats_booked_apart(ctx: Context, monkeypatch, trip: dict, entry: corpus.Entry) -> TripItem:
    """The owner and a companion book the first leg of `trip` apart, each under a code of their
    own (the companion's is the trip's code reversed): the store keeps one booking holding both
    codes, sent with both aboard. The booking."""
    me, sam = trip_people(trip)
    theirs_code = trip["confirmation_code"][::-1]
    (o,) = deliver(ctx, monkeypatch, entry, [leg(trip, 0, [me])]).outcomes
    apart = {**leg(trip, 0, [sam]), "confirmation_code": theirs_code}
    (joined,) = deliver(ctx, monkeypatch, entry, [apart], n=1).outcomes
    assert (joined.trip_item_id, joined.result) == (o.trip_item_id, "updated")
    shared = ctx.trips.item(o.trip_item_id)
    assert shared.confirmation.split(", ") == [trip["confirmation_code"], theirs_code]
    assert aboard(*shared.passengers) == aboard(me, sam) and shared.sync_state == "synced"
    return shared


def trip_people(trip: dict) -> tuple[str, str]:
    """The owner's name and the first companion's, as `trip` prints them."""
    printed = names(trip)
    me = next(p for p in printed if book().match(p).kind == "me")
    sam = next(p for p in printed if book().match(p).kind == "guest")
    return me, sam


def a_day_on(trip: dict) -> dict:
    """The first leg's departure and arrival a day later."""
    out = trip["segments"][0]
    return {k: moved(out[k], 1) for k in ("departure_local", "arrival_local")}


def a_change_under_one_of_two_codes(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Seats booked apart on one flight, one booking holding both codes. A change under the
    companion's code that names only the companion, a day on, says nothing of the owner's seat:
    the booking, and its flight in the flight log, stay on their day, and the companion's new
    flight is a booking of its own, not the owner's and never sent."""
    entry, trip, *_ = a_round_trip()
    me, sam = trip_people(trip)
    with ledgers() as ctx:
        since = Since(ctx)
        shared = seats_booked_apart(ctx, monkeypatch, trip, entry)
        (held,) = since.new()
        assert riders(held) == aboard(me, sam) and since.sent() == ["create", "update"]
        later = a_day_on(trip)
        theirs_code = trip["confirmation_code"][::-1]
        change = {**leg(trip, 0, [sam], **later), "confirmation_code": theirs_code}
        (o3,) = deliver(ctx, monkeypatch, entry, [change], n=2, doc="change").outcomes
        assert (o3.action.kind, o3.result) == ("file", "filed")
        assert o3.trip_item_id != shared.id
        now = ctx.trips.item(shared.id)
        assert (now.item_key, now.payload["dep_local"]) == (
            shared.item_key,
            shared.payload["dep_local"],
        )
        assert aboard(*now.passengers) == aboard(me, sam) and now.sync_state == "synced"
        new = ctx.trips.item(o3.trip_item_id)
        assert day(new.payload["dep_local"]) == day(later["departure_local"])
        assert aboard(*new.passengers) == aboard(sam)
        assert theirs(ctx, new) and new.sync_state == "off"
        sync(ctx)
        assert since.new() == [held] and held["date"] == now.payload["date_local"]
        assert since.sent() == ["create", "update"]


def the_owners_change_under_one_of_two_codes(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Seats booked apart on one flight, one booking holding both codes. The owner's own change
    under the owner's code, naming the owner, a day on, moves the booking: one booking and one
    flight in the flight log, moved there, never a second for the same flight."""
    entry, trip, *_ = a_round_trip()
    me, sam = trip_people(trip)
    with ledgers() as ctx:
        since = Since(ctx)
        shared = seats_booked_apart(ctx, monkeypatch, trip, entry)
        count = ctx.ledger.conn.execute("SELECT COUNT(*) FROM trip_items").fetchone()[0]
        later = a_day_on(trip)
        change = leg(trip, 0, [me], **later)
        (o3,) = deliver(ctx, monkeypatch, entry, [change], n=2, doc="change").outcomes
        assert (o3.action.reason, o3.trip_item_id) == ("rescheduled", shared.id)
        assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM trip_items").fetchone()[0] == count
        now = ctx.trips.item(shared.id)
        assert day(now.payload["dep_local"]) == day(later["departure_local"])
        assert not theirs(ctx, now) and now.sync_state == "synced"
        (held,) = since.new()
        assert held["date"] == now.payload["date_local"] and held["id"] == now.airtrail_flight_id
        assert since.sent() == ["create", "update", "update"]


def a_nameless_change_under_one_of_two_codes(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """Seats booked apart on one flight, one booking holding both codes. A change under the
    companion's code that names nobody, a day on, moves nothing, and does not say whose seat it
    is, so it borrows nobody's names: the new leg is filed naming nobody and waits for the
    owner's answer, nothing is sent, and the booking is as it was."""
    entry, trip, *_ = a_round_trip()
    with ledgers() as ctx:
        since = Since(ctx)
        shared = seats_booked_apart(ctx, monkeypatch, trip, entry)
        (held,) = since.new()
        change = {
            **leg(trip, 0, [], **a_day_on(trip)),
            "confirmation_code": trip["confirmation_code"][::-1],
        }
        (o3,) = deliver(ctx, monkeypatch, entry, [change], n=2, doc="change").outcomes
        assert (o3.action.kind, o3.result) == ("file", "filed")
        new = ctx.trips.item(o3.trip_item_id)
        assert (new.passengers, new.checks, new.sync_state) == ([], ("owner_unknown",), "blocked")
        assert ctx.trips.item(shared.id) == shared
        sync(ctx)
        assert since.new() == [held] and since.sent() == ["create", "update"]


def a_replay_after_a_fix(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """The corpus's email of the owner's flights is filed and sent; the owner moves one on the
    page and the change is sent. Read again by force, from the stored answer and with the model
    asked again, it makes no second flight and leaves the flight log in step with the fix."""
    with ledgers() as ctx:
        monkeypatch.setattr(runmod, "parse_message", pl.parse_message)  # the model is asked
        model = Counted()
        ctx.model = model
        since = Since(ctx)
        entry = a_model_reading(lambda p: p["me"] >= 1)
        first = process_raw(ctx, entry.raw, source="imap", received=corpus.sent(entry))
        ids = [o.trip_item_id for o in first.outcomes]
        assert [o.sync for o in first.outcomes] == ["synced"] * len(ids)
        fixed = ctx.trips.edit_item(ids[0], an_hour_dep(ctx, ids[0]), at=stamps(entry, 1)[0])
        sync(ctx)
        assert since.sent() == ["create"] * len(ids) + ["update"]
        for reread in (False, True):
            asked = model.calls
            again = process_raw(ctx, entry.raw, source="replay", force=True, reread=reread)
            assert model.calls == asked + reread
            assert sorted(o.trip_item_id for o in again.outcomes) == sorted(ids), reread
            ti = ctx.trips.item(ids[0])
            assert ti.payload["dep_local"] == fixed.payload["dep_local"], reread
            assert ti.sync_state == "synced", reread
            assert ti.told == checks.desired(ti.payload, ti.passengers, ti.status, ctx.book)
            there = {f["id"]: f for f in since.new()}
            assert len(there) == len(ids), reread
            assert there[ti.airtrail_flight_id]["departure"] == ti.payload["dep_utc"], reread
        assert since.sent() == ["create"] * len(ids) + ["update"]


def a_doubtful_flight_that_names_nobody(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """A flight that names nobody, read below the confidence floor, from a sender no list vouches
    for. Looks right then I was on this flight, and the other way round: after the first it is
    still held (a look does not say who flew; a yes opens the sender's check, which guards the
    owner's flights), and it is sent only after both, the owner aboard."""
    entry, flight = owners_flight()
    for order in (("look", "yes"), ("yes", "look")):
        with ledgers() as ctx:
            since = Since(ctx)
            how = {"confidence": UNSURE, "unvouched": True}
            (o,) = deliver(ctx, monkeypatch, entry, [nameless(flight)], **how).outcomes
            assert (o.checks, o.sync) == (("low_confidence", "owner_unknown"), "blocked")
            held_by = {"look": ("owner_unknown",), "yes": ("low_confidence", "unverified_sender")}
            for k, what in enumerate(order):
                if what == "look":
                    ctx.trips.looks_right(o.trip_item_id)
                else:
                    ctx.trips.owner_answer(o.trip_item_id, True)
                sync(ctx)
                ti = ctx.trips.item(o.trip_item_id)
                if k == 0:
                    assert (ti.checks, ti.sync_state) == (held_by[what], "blocked"), order
                    assert since.sent() == [], order
            assert (ti.checks, ti.sync_state) == ((), "synced"), order
            (held,) = since.new()
            assert riders(held) == ["me"] and since.sent() == ["create"], order


def the_owner_taken_off(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """The owner and a companion on a sent flight. The owner takes themselves off it on the page:
    it leaves the reconcile and nothing is sent (nothing is ever deleted there). A receipt sent
    before the edit that names the owner again leaves the travellers as the owner set them:
    still nothing is sent."""
    entry, trip, me, (sam, *_) = a_round_trip()
    booked = leg(trip, 0, [me, sam])
    with ledgers() as ctx:
        since = Since(ctx)
        (o,) = deliver(ctx, monkeypatch, entry, [booked]).outcomes
        (held,) = since.new()
        off = ctx.trips.edit_item(o.trip_item_id, {"passengers": [sam]}, at=stamps(entry, 1)[0])
        assert theirs(ctx, off) and off.sync_state == "off"
        sync(ctx)
        (o2,) = deliver(ctx, monkeypatch, entry, [booked], n=1).outcomes
        ti = ctx.trips.item(o.trip_item_id)
        assert o2.trip_item_id == ti.id and theirs(ctx, ti)
        assert aboard(*ti.passengers) == aboard(sam) and ti.sync_state == "off"
        assert since.new() == [held] and since.sent() == ["create"]


def everything_else(ctx: Context, bot, tg, item_id: int, entry: corpus.Entry, res: dict) -> None:
    """Every other thing the owner can do to a flight on the page or from Telegram, a pass after
    each: the flight stays someone else's."""
    st = ctx.trips
    segment = res["segments"][0]
    at = iter(stamps(entry, 5))  # after the email: what the owner types wins
    _, airlines = named(the_corpus())
    other_airline = next(a for a in airlines if a != segment["carrier_iata"])
    edits = (
        {"flight_number": another_number(segment["flight_number"])},
        {"carrier": other_airline},
        {"origin": segment["destination"], "destination": segment["origin"]},
        {"confirmation_code": res["confirmation_code"][::-1]},
    )
    st.looks_right(item_id)
    tap(bot, tg, f"ck:{item_id}:again")
    st.send_again(item_id)
    st.remove_item(item_id)
    st.restore_item(item_id)
    st.set_status(item_id, "cancelled")
    st.set_status(item_id, "confirmed")
    st.edit_item(item_id, an_hour_dep(ctx, item_id), at=next(at))
    for fields in edits:
        st.edit_item(item_id, fields, at=next(at))
        sync(ctx)
        assert theirs(ctx, st.item(item_id)), fields
    sync(ctx)
    assert theirs(ctx, st.item(item_id))


def the_only_two_ways(ledgers: Ledgers, monkeypatch, tmp_path) -> None:
    """A companion's flight and two flights that name nobody. Everything else the owner can do
    on the page or from Telegram leaves each someone else's and sends nothing: Looks right, Send
    again, a removal and its restore, a status set and set back, an edit of every flight field
    but the travellers, a companion's name typed in, the button that would add the owner to a
    flight that names someone, and Not me. Then I was on this flight on one that names nobody,
    and the owner's name typed into the companion's flight: each is the owner's and is sent,
    the owner aboard."""
    entry, trip, me, (sam, robin, *_) = a_round_trip()
    companion = booking("flight", a_companion_alone)
    (_, first), (_, second) = owners_flights(3)[1:]
    nobody = (owners_flights(3)[1][0], nameless(first))
    not_me = (owners_flights(3)[2][0], nameless(second))
    with ledgers() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        since = Since(ctx)
        st = ctx.trips
        filed = [
            deliver(ctx, monkeypatch, e, [res], n=n).outcomes[0].trip_item_id
            for n, (e, res) in enumerate((companion, nobody, not_me))
        ]
        theirs_id, nobody_id, not_me_id = filed
        for item_id, (e, res) in zip(filed[:2], (companion, nobody), strict=True):
            everything_else(ctx, bot, tg, item_id, e, res)
        # a companion typed into the companion's flight; the button that would add the owner
        st.edit_item(theirs_id, {"passengers": [*st.item(theirs_id).passengers, robin]})
        assert tap(bot, tg, f"ck:{theirs_id}:me")[0] == NOTHING_LEFT
        assert tap(bot, tg, f"ck:{not_me_id}:no")[0] == "Noted: not your flight"
        sync(ctx)
        assert all(theirs(ctx, st.item(i)) for i in filed)
        assert since.sent() == [] and since.new() == []
        # the two ways: I was on this flight, and the owner's name typed in
        assert tap(bot, tg, f"ck:{nobody_id}:me")[0] == "Added you to this flight"
        st.edit_item(theirs_id, {"passengers": [*st.item(theirs_id).passengers, me]})
        sync(ctx)
        for item_id in (theirs_id, nobody_id):
            ti = st.item(item_id)
            assert not theirs(ctx, ti) and ti.sync_state == "synced", item_id
        assert theirs(ctx, st.item(not_me_id))
        assert since.sent() == ["create", "create"] and len(since.new()) == 2
        assert all("me" in riders(f) for f in since.new())


PATHS = (
    a_page_edit,
    a_look,
    added_by_hand,
    an_upload,
    a_telegram_button,
    a_replay,
    inherited_from_an_unverified_sender,
    a_nameless_notice_for_a_leg_that_moved,
    a_new_leg_under_a_code_whose_legs_differ,
    a_cancelled_leg_beside_a_new_leg,
    a_companions_new_leg_beside_their_cancelled_leg,
    a_change_under_one_of_two_codes,
    the_owners_change_under_one_of_two_codes,
    a_nameless_change_under_one_of_two_codes,
    a_replay_after_a_fix,
    a_doubtful_flight_that_names_nobody,
    the_owner_taken_off,
    the_only_two_ways,
)


# ----------------------------------------------------------------------------- the tests
def test_a_page_edit_cannot_send_someone_elses_flight(monkeypatch, tmp_path):
    a_story(a_page_edit, monkeypatch, tmp_path)


def test_looks_right_cannot_send_someone_elses_flight(monkeypatch, tmp_path):
    a_story(a_look, monkeypatch, tmp_path)


def test_a_booking_added_by_hand_without_the_owner_is_never_sent(monkeypatch, tmp_path):
    a_story(added_by_hand, monkeypatch, tmp_path)


def test_an_upload_without_the_owner_is_never_sent(monkeypatch, tmp_path):
    a_story(an_upload, monkeypatch, tmp_path)


def test_a_telegram_button_cannot_send_someone_elses_flight(monkeypatch, tmp_path):
    a_story(a_telegram_button, monkeypatch, tmp_path)


def test_a_replay_cannot_send_someone_elses_flight(monkeypatch, tmp_path):
    a_story(a_replay, monkeypatch, tmp_path)


def test_names_inherited_from_an_unverified_sender_do_not_reach_the_flight_log_unchecked(
    monkeypatch, tmp_path
):
    a_story(inherited_from_an_unverified_sender, monkeypatch, tmp_path)


def test_a_nameless_notice_for_a_leg_that_moved_is_for_that_legs_travellers(monkeypatch, tmp_path):
    a_story(a_nameless_notice_for_a_leg_that_moved, monkeypatch, tmp_path)


def test_a_new_leg_under_a_code_whose_legs_differ_names_nobody_and_is_not_sent(
    monkeypatch, tmp_path
):
    a_story(a_new_leg_under_a_code_whose_legs_differ, monkeypatch, tmp_path)


def test_a_cancelled_leg_beside_a_new_leg_leaves_the_owner_on_no_flight_the_email_took_them_off(
    monkeypatch, tmp_path
):
    a_story(a_cancelled_leg_beside_a_new_leg, monkeypatch, tmp_path)


def test_a_companions_new_leg_beside_their_cancelled_leg_leaves_the_owner_where_they_are(
    monkeypatch, tmp_path
):
    a_story(a_companions_new_leg_beside_their_cancelled_leg, monkeypatch, tmp_path)


def test_a_change_under_one_of_two_codes_leaves_the_owners_flight_on_its_day(
    monkeypatch, tmp_path
):
    a_story(a_change_under_one_of_two_codes, monkeypatch, tmp_path)


def test_the_owners_change_under_one_of_two_codes_moves_the_one_booking(monkeypatch, tmp_path):
    a_story(the_owners_change_under_one_of_two_codes, monkeypatch, tmp_path)


def test_a_nameless_change_under_one_of_two_codes_names_nobody_and_is_not_sent(
    monkeypatch, tmp_path
):
    a_story(a_nameless_change_under_one_of_two_codes, monkeypatch, tmp_path)


def test_a_replay_after_a_fix_sends_no_second_flight_and_keeps_the_fix(monkeypatch, tmp_path):
    a_story(a_replay_after_a_fix, monkeypatch, tmp_path)


def test_a_doubtful_flight_that_names_nobody_is_sent_only_after_a_look_and_a_yes(
    monkeypatch, tmp_path
):
    a_story(a_doubtful_flight_that_names_nobody, monkeypatch, tmp_path)


def test_taking_the_owner_off_a_sent_flight_holds_against_an_older_receipt(monkeypatch, tmp_path):
    a_story(the_owner_taken_off, monkeypatch, tmp_path)


def test_the_owners_answer_and_name_are_the_only_ways_a_flight_becomes_theirs(
    monkeypatch, tmp_path
):
    a_story(the_only_two_ways, monkeypatch, tmp_path)


def test_every_request_the_flight_log_ever_received_named_the_owner(monkeypatch, tmp_path):
    """Every path above again, each in its own ledgers beside the owner's own flights: every
    create and every update any flight log was asked for carried the owner's user id, and each
    ledger's own flights were created there."""
    ledgers = Ledgers(monkeypatch, beside=True)
    for path in PATHS:
        path(ledgers, monkeypatch, tmp_path)
    ledgers.only_the_owners()
    entry, res = the_owners_own()
    for log in ledgers.logs:
        creates = [r for r in log.sent if r[0] == "create"]
        assert len(creates) >= len(res["segments"])
