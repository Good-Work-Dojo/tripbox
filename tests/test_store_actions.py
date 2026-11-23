# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What the owner can say about a booking, through the trip store: that it looks right
(`looks_right`), whether they were on a flight (`owner_answer`), and that a flight held back from
the flight log is to be sent again (`send_again`); and how a draft (a booking whose email lacked
a part of its key) becomes one with the booking that holds that key (`settle`, `drafts_like`).
The bookings are the corpus's own, validated the way a fresh parse is, with fields blanked to make
drafts; the flight log is an AirTrail held in memory."""

import datetime as dt
import re

import pytest
from helpers.flightlog import Scripted, seed
from test_checks import (
    FLOOR,
    a_companion_alone,
    book,
    booking,
    bookings,
    cities,
    day,
    edit,
    moved,
    names,
    owners,
    stamps,
    the_corpus,
    validated,
)

from tripbox import corpus, util
from tripbox.ledger import Ledger
from tripbox.parse.models import Extraction
from tripbox.trips import format as fmt
from tripbox.trips.store import TripItem, TripStore
from tripbox.util import parse_iso
from tripbox.validate import checks, rules
from tripbox.validate.keys import is_draft
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.refdata import Airports
from tripbox.write.executor import Executor
from tripbox.write.planner import plan
from tripbox.write.sync import Syncer

AIRPORTS = Airports()
HOME = tuple(the_corpus().settings["TRIPBOX_HOME_AIRPORTS"].split(","))
NO_OWNER = "no owner is named in travellers.yaml; add owner: names: first"
LOW = {"confidence": FLOOR / 2, "parser": "haiku"}  # read by a model, below its confidence floor


def store(ledger: Ledger | None = None) -> TripStore:
    return TripStore(ledger or Ledger.memory(), HOME, book=book(), airports=AIRPORTS)


def kept(led: Ledger) -> tuple[TripStore, Scripted, Syncer]:
    """A store over `led` that keeps a flight log: one held in memory that knows the corpus's
    airports and airlines, and the syncer that brings it in step."""
    led.kv.set("airtrail", "on")
    seed(led, the_corpus())
    log = Scripted(led)
    st = store(led)
    return st, log, Syncer(store=st, ledger=led, book=st.book, client=log, refs=None, dry_run=False)


def owners_flights(n: int) -> list[tuple[corpus.Entry, dict]]:
    """The first `n` legs of the owner's that the corpus files with no finding, each under a key
    of its own."""
    out: list[tuple[corpus.Entry, dict]] = []
    seen: set[str] = set()
    for entry, res in bookings("flight"):
        item = validated(entry, res)
        if owners(res) and not checks.codes(item) and item.key not in seen:
            seen.add(item.key)
            out.append((entry, res))
            if len(out) == n:
                return out
    raise AssertionError(f"the corpus files fewer than {n} legs of the owner's")


def nameless(res: dict) -> dict:
    return edit(res, {"passengers": []}, passengers=[])


def sent_at(res: dict, when: str, **kw) -> rules.Item:
    """`res` validated as an email whose Date header says `when` would be."""
    confidence = kw.pop("confidence", 1.0)
    ex = Extraction.model_validate(
        {"document_type": "booking", "reservations": [res], "confidence": confidence}
    )
    source = {"message_id": "<actions>", "email_date": when}
    (item,) = rules.validate_extraction(
        ex, refdata=AIRPORTS, book=book(), email_date=parse_iso(when), source=source, **kw
    )
    return item


def an_hour_later(value: str) -> str:
    return (rules.parse_local(value) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")


def rows(led: Ledger) -> list[dict]:
    return [dict(r) for r in led.conn.execute("SELECT * FROM trip_items ORDER BY id")]


def sent(log: Scripted) -> list[str]:
    return [r[0] for r in log.sent]


# ----------------------------------------------------------------------------- looks right
def test_looks_right_closes_every_soft_check_and_no_hard_one():
    (entry, flight), (other, second), (a_third, third) = owners_flights(3)
    a_stay, stay = booking("lodging")
    t0, t1 = stamps(entry, 2)
    leg, its = flight["segments"][0], second["segments"][0]
    far = (corpus.sent(entry).date() - day(leg["departure_local"])).days
    far += rules.FUTURE_LIMIT.days + 7
    past = (corpus.sent(other).date() - day(its["departure_local"])).days
    past -= rules.PAST_SLACK.days + 7
    doubtful = [
        # the owner's flight, from a sender nobody vouched for, read below the confidence floor,
        # too far out, too long, and with no flight number
        validated(
            entry,
            edit(
                flight,
                flight_number=None,
                departure_local=moved(leg["departure_local"], far),
                arrival_local=moved(leg["arrival_local"], far + 1),
            ),
            trusted_sender=False,
            **LOW,
        ),
        # a flight that names nobody, long past, and a cancellation for a booking never filed
        validated(
            other,
            edit(
                nameless(second),
                departure_local=moved(its["departure_local"], past),
                arrival_local=moved(its["arrival_local"], past),
            ),
        ),
        # a stay with no name and no check-out
        validated(a_stay, edit(stay, {"property_name": None, "check_out": None})),
    ]
    led = Ledger.memory()
    st, _, _ = kept(led)
    filed: list[TripItem] = []
    for item in doubtful:
        checks.raise_soft(item.payload, checks.codes(item), t0)
        filed.append(st.upsert_item(None, item))
    checks.raise_soft(filed[1].payload, ["unknown_cancellation"], t0)
    led.trip_items.update(filed[1].id, payload=filed[1].payload)
    st.settle(filed[1].id)
    st.sync_failed(filed[0].id, code="refused", now=parse_iso(t0), retry_at=None)
    # a flight the flight log was told the owner is on, and the owner then says they were not
    sent = st.upsert_item(None, validated(a_third, third))
    told = checks.desired(sent.payload, sent.passengers, sent.status, st.book)
    st.sync_done(sent.id, flight_id=1, told=told)
    with util.clock_at(parse_iso(t0)):
        filed.append(st.owner_answer(sent.id, False))
    before = {ti.id: st.item(ti.id).checks for ti in filed}
    assert set().union(*before.values()) >= set(checks.SOFT)  # every soft check is open on one
    for ti in filed:
        after = st.looks_right(ti.id, at=t1)
        kept_open = tuple(c for c in before[ti.id] if c in checks.HARD or c == "owner_unknown")
        assert after.checks == kept_open and after.payload["looked_at"] == t1, before[ti.id]
    open_now = {c for ti in filed for c in st.item(ti.id).checks}
    assert open_now == {"flight_number", "owner_unknown", "check_out"}


def test_looking_is_undone_only_by_a_newer_email_that_raises_a_soft_check():
    """A finding is stamped when its message was received, and it reaches the booking only with
    what the email puts into it: the same message read again, and a copy received after the
    look, are duplicates and reopen nothing, even read below the confidence floor; an email that
    changes the booking, received after the look, reopens a soft check by raising one, whatever
    its Date header says."""
    entry, flight = booking("flight", owners)
    leg = flight["segments"][0]
    later = edit(
        flight,
        departure_local=an_hour_later(leg["departure_local"]),
        arrival_local=an_hour_later(leg["arrival_local"]),
    )
    t = stamps(entry, 6)
    led = Ledger.memory()
    st = store(led)

    def read(item: rules.Item, received: str, doc: str = "booking") -> tuple[str, TripItem]:
        """`item` from a message (a `doc`) received at `received`, through the plan and the
        executor: what became of it, and the booking as it stands."""
        wp = plan([item], document_type=doc, store=st, book=book(), received_at=received)
        n = led.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        mid = led.messages.insert(sha256=f"read-{n}", source="cli", status="archived")
        (out,) = Executor(ledger=led, store=st, book=book()).execute(wp, message_row_id=mid)
        return out.result, st.item(out.trip_item_id)

    result, ti = read(validated(entry, flight, **LOW), t[0])
    assert (result, ti.checks) == ("filed", ("low_confidence",))
    looked = st.looks_right(ti.id, at=t[1])
    assert looked.checks == ()
    # copies, which put nothing into the booking: the same message read again (it keeps the
    # time it was received), one received after the look with no finding, and one received
    # after the look below the confidence floor
    for item, received in (
        (validated(entry, flight, **LOW), t[0]),
        (sent_at(flight, t[2]), t[2]),
        (sent_at(flight, t[3], **LOW), t[3]),
    ):
        assert read(item, received) == ("duplicate", looked), received
    # one that changes the booking does, and reopens the look
    result, moved_on = read(sent_at(later, t[3], **LOW), t[3], "change")
    assert (result, moved_on.checks) == ("updated", ("low_confidence",))
    assert st.looks_right(ti.id, at=t[4]).checks == ()
    # received after the look below the floor, its Date header before the look
    assert parse_iso(t[0]) < parse_iso(t[4])
    result, back = read(sent_at(flight, t[0], **LOW), t[5], "change")
    assert (result, back.checks) == ("updated", ("low_confidence",))
    assert st.item(ti.id).payload["raised"] == {"low_confidence": t[5]}
    assert len(rows(led)) == 1


# ----------------------------------------------------------------------------- the owner's answer
def test_i_was_on_this_flight_names_the_owner_and_not_me_keeps_the_flight_out():
    (entry, flight), (other, second) = owners_flights(2)
    t0, t1, t2 = stamps(entry)
    led = Ledger.memory()
    st, log, syncer = kept(led)
    yes = st.upsert_item(None, validated(entry, nameless(flight)))
    no = st.upsert_item(None, validated(other, nameless(second)))
    for ti in (yes, no):
        assert (ti.sync_state, ti.checks) == ("blocked", ("owner_unknown",))
    me = book().me_name
    on = st.owner_answer(yes.id, True, at=t0)
    assert on.passengers == [me] and on.payload["passengers"] == [me]
    assert on.payload["owner_edits"] == {"passengers": t0}
    assert checks.owner("flight", on.passengers, on.payload, book()) == "on"
    assert fmt.display_names(on.passengers, book()) == [me.split()[0]]  # as the page names them
    assert (on.sync_state, on.checks) == ("pending", ())
    off = st.owner_answer(no.id, False, at=t0)
    assert off.payload["owner_answer"] == "no"
    assert off.payload["owner_edits"] == {"owner_answer": t0}
    assert off.passengers == [] and checks.owner("flight", [], off.payload, book()) == "off"
    assert (off.sync_state, off.checks) == ("off", ())
    syncer.run_pass(now=parse_iso(t1))
    (held,) = log.flights
    assert [p["user"] is not None for p in held["passengers"]] == [True]  # the owner, aboard
    assert st.item(yes.id).airtrail_flight_id == held["id"]
    assert (st.item(yes.id).sync_state, st.item(no.id).airtrail_flight_id) == ("synced", None)
    # a yes after a no puts the owner on the flight
    again = st.owner_answer(no.id, True, at=t2)
    assert "owner_answer" not in again.payload
    assert again.payload["owner_edits"] == {"passengers": t2}
    assert (again.passengers, again.sync_state) == ([me], "pending")


def test_the_owners_answer_needs_an_owner_and_a_flight():
    entry, flight = booking("flight", owners)
    a_stay, stay = booking("lodging")
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    nobody = PassengerBook(me_aliases=(), guests=book().guests)
    st = TripStore(led, HOME, book=nobody, airports=AIRPORTS)
    leg = st.upsert_item(None, validated(entry, nameless(flight)))
    hotel = st.upsert_item(None, validated(a_stay, stay))
    before = rows(led)
    with pytest.raises(ValueError, match=re.escape(NO_OWNER)):
        st.owner_answer(leg.id, True)
    for answer in (True, False):
        with pytest.raises(ValueError, match="flight"):
            st.owner_answer(hotel.id, answer)
    assert rows(led) == before


# ----------------------------------------------------------------------------- send again
def test_send_again_releases_a_flight_deleted_there_and_a_backoff():
    (entry, flight), (other, second) = owners_flights(2)
    t0, t1, t2 = stamps(entry)
    led = Ledger.memory()
    st, log, syncer = kept(led)
    gone = st.upsert_item(None, validated(entry, flight))
    syncer.run_pass(now=parse_iso(t0))
    (there,) = log.flights
    log.deleted_by_hand(there["id"])
    detached = st.sync_detached(gone.id)  # what a pass does on finding it gone there
    assert (detached.sync_hold, detached.sync_state) == ("deleted", "off")
    log.requests.clear()
    again = st.send_again(gone.id)
    assert (again.sync_hold, again.sync_state) == (None, "pending")
    syncer.run_pass(now=parse_iso(t1))
    # looked for there before it is made again
    asked = [r[0] for r in log.requests if r[0] in ("find", "create", "update")]
    assert asked == ["find", "create"] and len(log.flights) == 1
    assert st.item(gone.id).airtrail_flight_id == log.flights[0]["id"]
    # a flight waiting out a backoff is due at once
    waits = st.upsert_item(None, validated(other, second))
    retry = parse_iso(t2) + dt.timedelta(days=1)
    st.sync_failed(waits.id, code="outage", now=parse_iso(t1), retry_at=retry)
    due = lambda: [ti.id for ti in st.due_for_sync(now=parse_iso(t2))]  # noqa: E731
    assert waits.id not in due()
    released = st.send_again(waits.id)
    assert (released.sync_after, released.sync_attempts) == (None, 0)
    assert released.sync_state == "pending"
    assert waits.id in due()
    syncer.run_pass(now=parse_iso(t2))
    assert st.item(waits.id).sync_state == "synced" and len(log.flights) == 2
    # a flight that had left when the flight log was switched on stays out
    led.trip_items.update(waits.id, sync_hold="past")
    assert (st.send_again(waits.id).sync_hold, st.item(waits.id).sync_state) == ("past", "off")


# ----------------------------------------------------------------------------- drafts
def merged(variant: str, own: rules.Item, draft: rules.Item, entry: corpus.Entry, number: str):
    """`own` filed into a ledger that keeps a flight log, then left `variant` (visible, synced,
    removed, or in a deleted trip); `draft` filed, with a row in the log of what a message did;
    and the draft given the flight number its email lacked. The booking before, the draft, the
    booking after, the log row and the store."""
    led = Ledger.memory()
    st, log, syncer = kept(led)
    b = st.upsert_item(None, own)
    if variant == "synced":
        syncer.run_pass(now=parse_iso(stamps(entry, 1)[0]))
    elif variant == "removed":
        st.remove_item(b.id)
    elif variant == "in a deleted trip":
        st.delete_trip(b.trip_id)
    b = st.item(b.id)
    d = st.upsert_item(None, draft)
    assert is_draft(d.item_key) and d.id != b.id, variant
    mid = led.messages.insert(sha256="the draft's", source="cli", status="archived")
    row = led.items.add(
        message_id=mid,
        n=0,
        kind="flight",
        item_key=d.item_key,
        outcome="filed",
        payload=draft.payload,
        trip_item_id=d.id,
    )
    led.trip_items.update(d.id, payload={**d.payload, "flight_number": number})
    return b, d, st.settle(d.id), row, st


def test_a_completed_draft_merges_into_the_booking_that_holds_its_key():
    entry, flight = booking(
        "flight",
        lambda r: owners(r) and bool(r["segments"][0].get("cabin")) and bool(r.get("provider")),
    )
    _, theirs = booking("flight", a_companion_alone)
    leg = flight["segments"][0]
    me, companion = book().me_name, names(theirs)[0]
    # the booking prints no provider, and its cabin is one the owner cleared
    mine = edit(flight, {"passengers": [me], "provider": None}, passengers=[], cabin=None)
    own = validated(entry, mine)
    draft = validated(
        entry,
        edit(
            flight,
            {"passengers": [companion]},
            passengers=[],
            flight_number=None,
            arrival_local=an_hour_later(leg["arrival_local"]),
        ),
    )
    t0, t1, t2 = stamps(entry)
    own.payload["owner_edits"] = {"cabin": t1, "seats": t1}
    draft.payload["owner_edits"] = {"cabin": t0, "notes": t2}
    for variant in ("visible", "synced", "removed", "in a deleted trip"):
        b, d, after, row, st = merged(variant, own, draft, entry, leg["flight_number"])
        led = st.led
        assert after.id == b.id and st.item(d.id) is None and len(rows(led)) == 1, variant
        assert st.find(own.key).id == st.find(d.item_key).id == b.id, variant
        assert after.item_key == own.key and d.item_key in after.payload["alt_keys"], variant
        # the travellers of both; the booking's own values, what it lacked from the draft, and
        # not what the owner cleared
        assert after.passengers == after.payload["passengers"] == [me, companion], variant
        assert after.payload["arr_local"] == own.payload["arr_local"], variant
        assert after.payload["provider"] == flight["provider"], variant
        assert after.payload["cabin"] is None, variant
        assert after.payload["owner_edits"] == {"cabin": t1, "seats": t1, "notes": t2}, variant
        assert led.items.get(row)["trip_item_id"] == b.id, variant
        # where it was, with its link to the flight log
        assert (after.trip_id, after.removed_at) == (b.trip_id, b.removed_at), variant
        assert (after.airtrail_flight_id, after.told) == (b.airtrail_flight_id, b.told), variant
        assert {t.id for t in st.all_trips()} == (
            {b.trip_id} if variant in ("visible", "synced") else set()
        ), variant
        if variant == "synced":
            assert b.sync_state == "synced" and after.airtrail_flight_id is not None
            want = checks.desired(after.payload, after.passengers, after.status, book())
            assert want != after.told and after.sync_state == "pending"  # a companion joined
        if variant == "in a deleted trip":
            assert st.get(after.trip_id) is None and led.trips.get(after.trip_id) is not None
    # a draft that adds nothing the flight log holds leaves the booking synced
    same = validated(entry, edit(mine, flight_number=None))
    b, _, after, _, _ = merged("synced", own, same, entry, leg["flight_number"])
    assert (after.id, after.told, after.sync_state) == (b.id, b.told, "synced")
    # what the draft's email raised is open on the booking until the owner looks at the
    # booking: a look at the draft is none at the booking
    doubted = validated(entry, edit(mine, flight_number=None))
    checks.raise_soft(doubted.payload, ["low_confidence"], t0)
    doubted.payload["looked_at"] = t1
    b, _, after, _, _ = merged("synced", own, doubted, entry, leg["flight_number"])
    assert after.payload["raised"] == {"low_confidence": t0} and "looked_at" not in after.payload
    assert (after.checks, after.sync_state, after.told) == (("low_confidence",), "blocked", b.told)
    # what the draft's own emails left on it stays with it; the owner's "no" about the leg holds
    flagged = validated(entry, edit(mine, flight_number=None))
    left = {
        "change_recorded": True,
        "cancelled_email_date": t0,
        "cancelled_names": {"Casey Sample": t0},
        "route_inherited_from": b.id,
        "passengers_inherited_from": b.id,
    }
    flagged.payload.update(left, owner_answer="no")
    b, _, after, _, _ = merged("synced", own, flagged, entry, leg["flight_number"])
    assert not set(left) & set(after.payload)
    assert (after.payload["owner_answer"], after.sync_state, after.told) == ("no", "off", b.told)


def test_a_draft_and_its_twin_never_both_reach_airtrail():
    entry, flight = booking("flight", owners)
    number = flight["segments"][0]["flight_number"]
    t0, t1, t2 = stamps(entry)
    for sent_first in (True, False):
        led = Ledger.memory()
        st, log, syncer = kept(led)
        twin = st.upsert_item(None, validated(entry, flight))
        if sent_first:
            syncer.run_pass(now=parse_iso(t0))
        draft = st.upsert_item(None, validated(entry, edit(flight, flight_number=None)))
        syncer.run_pass(now=parse_iso(t1))
        assert st.item(draft.id).sync_state == "blocked" and sent(log) == ["create"], sent_first
        led.trip_items.update(draft.id, payload={**draft.payload, "flight_number": number})
        assert st.settle(draft.id).id == twin.id, sent_first
        syncer.run_pass(now=parse_iso(t2))
        (held,) = log.flights
        assert sent(log) == ["create"] and st.item(twin.id).airtrail_flight_id == held["id"]
        assert st.item(draft.id) is None and len(rows(led)) == 1, sent_first


def test_drafts_like_are_the_visible_drafts_that_agree_wherever_both_say_something():
    place = cities()
    entry, flight = booking("flight", lambda r: owners(r) and r["segments"][0]["origin"] in place)
    leg = flight["segments"][0]
    proper = validated(entry, flight)
    led = Ledger.memory()
    st = store(led)
    # its origin read as a city: a value that fails its check says nothing
    city = st.upsert_item(None, validated(entry, edit(flight, origin=place[leg["origin"]])))
    assert is_draft(city.item_key) and "origin" in city.checks
    assert [ti.id for ti in st.drafts_like(proper)] == [city.id]
    # a draft that prints another departure time is not like it, nor one put away
    other = edit(flight, flight_number=None, departure_local=an_hour_later(leg["departure_local"]))
    st.upsert_item(None, sent_at(other, stamps(entry, 1)[0]))
    assert [ti.id for ti in st.drafts_like(proper)] == [city.id]
    st.remove_item(city.id)
    assert st.drafts_like(proper) == []
    # nor a booking under its real key; and an email that prints no code is like no draft
    st.restore_item(city.id)
    st.upsert_item(None, proper)
    assert [ti.id for ti in st.drafts_like(proper)] == [city.id]
    proper.pnr = None
    assert st.drafts_like(proper) == []
    # a draft that says its day and neither its number nor its time is like nothing; one that
    # names someone the email does not name is like it only when the email names them too
    st = store()
    its_day = day(leg["departure_local"]).isoformat()
    vague = edit(flight, flight_number=None, departure_local=its_day)
    st.upsert_item(None, validated(entry, vague))
    _, theirs = booking("flight", a_companion_alone)
    someone = edit(flight, {"passengers": names(theirs)[:1]}, passengers=[])
    theirs_too = st.upsert_item(
        None, sent_at(edit(someone, origin=place[leg["origin"]]), stamps(entry, 1)[0])
    )
    mine = edit(flight, {"passengers": [book().me_name]}, passengers=[])
    assert st.drafts_like(validated(entry, mine)) == []
    assert [ti.id for ti in st.drafts_like(validated(entry, someone))] == [theirs_too.id]
    # a train: its number, its day and time, and its stations
    t_entry, train = booking("train", lambda r: bool(r.get("confirmation_code")))
    t_leg = train["segments"][0]
    whole = validated(t_entry, train)
    st = store()
    stationless = st.upsert_item(None, validated(t_entry, edit(train, origin=None)))
    assert is_draft(stationless.item_key) and "stations" in stationless.checks
    assert [ti.id for ti in st.drafts_like(whole)] == [stationless.id]
    later = validated(t_entry, edit(train, departure_local=an_hour_later(t_leg["departure_local"])))
    assert st.drafts_like(later) == []
