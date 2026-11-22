# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`TripStore.settle`: every write of a booking ends in it, and it makes what is stored on the
booking follow from the booking's own fields: its derived times and its key, the checks open on
it, the trip it is in (none while it has no date) and where its sync with a flight log stands.
The bookings are the corpus's own, validated the way a fresh parse is, and spoiled or moved by
computed amounts."""

import datetime as dt
import itertools
import json
import string
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from helpers.flightlog import seeded
from test_checks import (
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

from tripbox import corpus
from tripbox.ledger import Ledger
from tripbox.parse.models import Extraction
from tripbox.pipeline.shift import DateShift
from tripbox.trips import backfill as bf
from tripbox.trips import format as fmt
from tripbox.trips import grouping, ical
from tripbox.trips import store as store_module
from tripbox.trips.store import KeyCollision, TripStore
from tripbox.util import parse_iso
from tripbox.validate import checks, keys, rules
from tripbox.validate.refdata import Airports
from tripbox.web import views
from tripbox.write.airtrail import REQUIRED_SCOPES, Profile
from tripbox.write.executor import Executor
from tripbox.write.planner import plan
from tripbox.write.sync import Syncer

AIRPORTS = Airports()
HOME = tuple(the_corpus().settings["TRIPBOX_HOME_AIRPORTS"].split(","))


def store(ledger: Ledger | None = None) -> TripStore:
    return TripStore(ledger or Ledger.memory(), HOME, book=book(), airports=AIRPORTS)


def sent_at(res: dict, when: str) -> rules.Item:
    """`res` validated as an email sent at `when` would be."""
    ex = Extraction.model_validate(
        {"document_type": "booking", "reservations": [res], "confidence": 1.0}
    )
    source = {"message_id": "<settle>", "email_date": when}
    (item,) = rules.validate_extraction(
        ex, refdata=AIRPORTS, book=book(), email_date=parse_iso(when), source=source
    )
    return item


def hours(value: str, n: int) -> str:
    """The printed local time `value` moved by `n` hours, written as the validator writes one."""
    return (rules.parse_local(value) + dt.timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M")


def row(st: TripStore, item_id: int) -> dict:
    return dict(st.led.trip_items.get(item_id))


def ids(items) -> list[int]:
    return [i.id for i in items]


def test_settle_derives_the_times_and_the_key_from_the_stored_fields():
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)
    st = store()
    ti = st.upsert_item(st.find_or_create(item), item)
    assert ti.item_key == item.key and not ti.checks
    p = item.payload
    # an instant an hour off, the day before, a duration an hour longer, and the key of that day
    wrong_key = keys.real_key(
        "flight", {**p, "dep_local": moved(p["dep_local"], -1)}, airports=AIRPORTS
    )
    spoiled = {
        **ti.payload,
        "dep_utc": (parse_iso(p["dep_utc"]) + dt.timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "date_local": (day(p["dep_local"]) - dt.timedelta(days=1)).isoformat(),
        "duration_min": 60 + (p["duration_min"] or 0),
    }
    st.led.trip_items.update(ti.id, item_key=wrong_key, payload=spoiled)
    after = st.settle(ti.id)
    for field in ("dep_local", "arr_local", "dep_utc", "arr_utc", "date_local", "duration_min"):
        assert after.payload[field] == p[field], field
    assert after.item_key == keys.real_key("flight", after.payload, airports=AIRPORTS) == item.key
    assert after.payload["alt_keys"] == [wrong_key] and st.find(wrong_key).id == ti.id
    assert after.start_at == grouping.local_str(p["dep_local"])
    # a second settle changes nothing: not the row, not its trip
    before = row(st, ti.id), dict(st.led.trips.get(ti.trip_id))
    st.settle(ti.id)
    assert (row(st, ti.id), dict(st.led.trips.get(ti.trip_id))) == before


def test_every_booking_the_corpus_files_settles_to_what_was_filed():
    """Validated as a fresh parse is (printed offsets included) and filed, each booking keeps the
    key and every derived time the validator gave it, and has no check open."""
    seen = set()
    for kind in ("flight", "lodging", "car", "train"):
        for entry, res in bookings(kind):
            item = validated(entry, res)
            if checks.codes(item):
                continue
            ti = store().upsert_item(None, item)
            label = (entry.name, kind)
            assert ti.item_key == item.key and ti.checks == () and ti.trip_id, label
            assert (ti.start_at, ti.end_at) == grouping.item_span(item)[:2], label
            for field in store_module.FLIGHT_DERIVED if kind == "flight" else ():
                assert ti.payload.get(field) == item.payload.get(field), (*label, field)
            seen.add(kind)
    assert seen == {"flight", "lodging", "car", "train"}


def test_a_replayed_older_email_never_rewinds_an_owners_edit_or_the_times_derived_from_it():
    entry, stay = booking("lodging")
    item = validated(entry, stay)
    st = store()
    ti = st.upsert_item(st.find_or_create(item), item)
    first, second, _ = stamps(entry)
    mine = moved(stay["check_in"], -1)
    st.edit_item(ti.id, {"check_in": mine}, at=first)
    again = st.upsert_item(st.get(ti.trip_id), validated(entry, stay))  # sent before the edit
    assert again.id == ti.id and again.payload["check_in"] == mine
    assert again.start_at == grouping.local_str(mine) and "check_in" in again.payload["owner_edits"]
    assert st.get(ti.trip_id).start_date == day(mine).isoformat()
    theirs = moved(stay["check_in"], -2)
    after = st.upsert_item(st.get(ti.trip_id), sent_at(edit(stay, {"check_in": theirs}), second))
    assert after.id == ti.id and after.payload["check_in"] == theirs
    assert after.start_at == grouping.local_str(theirs)
    assert "check_in" not in (after.payload.get("owner_edits") or {})
    assert st.get(ti.trip_id).start_date == day(theirs).isoformat()


def test_a_booking_with_no_date_has_no_trip_and_is_listed_as_unsorted():
    entry, flight = booking("flight", owners)
    leg = flight["segments"][0]
    undated = validated(entry, edit(flight, departure_local=None, arrival_local=None))
    a_stay, stay = booking("lodging")
    hotel = validated(a_stay, stay)
    st = store()
    st.upsert_item(st.find_or_create(hotel), hotel)
    ti = st.upsert_item(None, undated)
    assert (ti.trip_id, ti.start_at, ti.end_at) == (None, None, None)
    assert "departure" in ti.checks and ids(st.unsorted()) == [ti.id]
    assert st.find(undated.key).id == ti.id and st.item_by_key(undated.key).id == ti.id
    assert all(ti.id not in ids(st.items(t.id)) for t in st.all_trips())
    code = undated.payload["flight_code"]
    assert code not in ical.feed_ics(st, book=book(), home_airports=HOME)
    for t in st.all_trips():
        assert code not in fmt.trip_text(t, st.items(t.id), book=book(), home_airports=HOME)
    # given its date, it is placed in the trip the grouping picks and leaves the unsorted list
    when = {"dep_local": leg["departure_local"], "arr_local": leg["arrival_local"]}
    st.led.trip_items.update(ti.id, payload={**ti.payload, **when})
    placed = st.settle(ti.id)
    dated = validated(entry, flight)
    assert placed.trip_id == st.find_or_create(dated).id and st.unsorted() == []
    assert placed.start_at == grouping.local_str(leg["departure_local"])
    assert placed.id in ids(st.items(placed.trip_id)) and "departure" not in placed.checks
    assert placed.item_key == dated.key and st.find(undated.key).id == ti.id


def test_a_draft_takes_its_real_key_and_keeps_the_draft_key_as_an_alias():
    place = cities()
    entry, flight = booking("flight", lambda r: owners(r) and r["segments"][0]["origin"] in place)
    code = flight["segments"][0]["origin"]
    draft = validated(entry, edit(flight, origin=place[code]))
    real = validated(entry, flight)
    assert keys.is_draft(draft.key) and not keys.is_draft(real.key)
    st = store()
    ti = st.upsert_item(st.find_or_create(draft), draft)
    assert ti.item_key == draft.key and "origin" in ti.checks
    st.led.trip_items.update(ti.id, payload={**ti.payload, "origin": code})
    done = st.settle(ti.id)
    assert done.item_key == real.key and done.payload["alt_keys"] == [draft.key]
    assert st.find(draft.key).id == st.find(real.key).id == ti.id and "origin" not in done.checks
    # a booking under a real key whose fields come to make another booking's key: it keeps its
    # own and nothing changes
    st = store()
    held = st.upsert_item(st.find_or_create(real), real)
    p = real.payload
    leg = flight["segments"][0]
    later = edit(
        flight,
        departure_local=moved(leg["departure_local"], 1),
        arrival_local=moved(leg["arrival_local"], 1),
    )
    mine = st.upsert_item(st.get(held.trip_id), validated(entry, later))
    assert mine.id != held.id and not keys.is_draft(mine.item_key)
    back = {"dep_local": p["dep_local"], "arr_local": p["arr_local"]}
    st.led.trip_items.update(mine.id, payload={**mine.payload, **back})
    before = [row(st, held.id), row(st, mine.id)]
    with pytest.raises(KeyCollision) as caught:
        st.settle(mine.id)
    assert (caught.value.item_id, caught.value.other_id) == (mine.id, held.id)
    assert [row(st, held.id), row(st, mine.id)] == before


def test_a_key_the_booking_already_answers_to_is_taken_when_its_fields_make_it():
    """A flight moved to another day and back answers to both keys and is filed under the one its
    fields make; a draft whose real key is among its aliases takes it."""
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)
    p = item.payload
    st = store()
    ti = st.upsert_item(st.find_or_create(item), item)
    away = {"dep_local": moved(p["dep_local"], 1), "arr_local": moved(p["arr_local"], 1)}
    st.led.trip_items.update(ti.id, payload={**ti.payload, **away})
    there = st.settle(ti.id)
    assert there.item_key != item.key and there.payload["alt_keys"] == [item.key]
    home = {"dep_local": p["dep_local"], "arr_local": p["arr_local"]}
    st.led.trip_items.update(ti.id, payload={**there.payload, **home})
    back = st.settle(ti.id)
    assert back.item_key == item.key and back.payload["alt_keys"] == [there.item_key]
    assert st.find(item.key).id == st.find(there.item_key).id == ti.id
    assert st.settle(ti.id) == back
    # a draft that already answers to its real key
    place = cities()
    entry, flight = booking("flight", lambda r: owners(r) and r["segments"][0]["origin"] in place)
    code = flight["segments"][0]["origin"]
    draft = validated(entry, edit(flight, origin=place[code]))
    real = validated(entry, flight)
    st = store()
    ti = st.upsert_item(st.find_or_create(draft), draft)
    fixed = {**ti.payload, "origin": code, "alt_keys": [real.key]}
    st.led.trip_items.update(ti.id, payload=fixed)
    done = st.settle(ti.id)
    assert done.item_key == real.key and done.payload["alt_keys"] == [draft.key]
    assert st.find(draft.key).id == st.find(real.key).id == ti.id


def apart(kind: str) -> tuple[rules.Item, rules.Item]:
    """Two bookings of `kind` the corpus files cleanly, further apart than any trip's slack."""
    found = [
        validated(e, r) for e, r in bookings(kind) if not checks.codes(validated(e, r))
    ]
    far = grouping.NEARBY_DAYS + 2 * grouping.TRIP_SLACK.days
    for a in found:
        for b in found:
            gap = abs((grouping.item_dates(a)[0] - grouping.item_dates(b)[0]).days)
            if gap > far:
                return a, b
    raise AssertionError(f"the corpus files no two {kind} bookings that far apart")


def test_a_move_made_on_the_page_survives_a_later_settle():
    a, b = apart("flight")
    st = store()
    ta = st.upsert_item(st.find_or_create(a), a)
    tb = st.upsert_item(st.find_or_create(b), b)
    assert ta.trip_id != tb.trip_id
    st.move_item(tb.id, ta.trip_id)
    assert st.settle(tb.id).trip_id == ta.trip_id
    again = st.upsert_item(st.find_or_create(b), b)  # the email again, after the move
    assert again.id == tb.id and again.trip_id == ta.trip_id
    assert sorted(ids(st.items(ta.trip_id))) == sorted([ta.id, tb.id])
    span = sorted([grouping.item_dates(a)[0], grouping.item_dates(b)[0]])
    trip = st.get(ta.trip_id)
    assert trip.start_date == span[0].isoformat() and trip.end_date >= span[1].isoformat()


def test_the_marked_and_unsorted_readers():
    a_stay, stay = booking("lodging")
    a_car, car = booking("car")
    entry, flight = booking("flight", owners)
    marked = validated(a_stay, edit(stay, {"check_out": None}))
    gone = validated(a_car, edit(car, {"dropoff_time": None}))
    undated = validated(entry, edit(flight, departure_local=None, arrival_local=None))
    fine = validated(entry, flight)
    st = store()
    m = st.upsert_item(st.find_or_create(marked), marked)
    g = st.upsert_item(st.find_or_create(gone), gone)
    st.remove_item(g.id)
    u = st.upsert_item(None, undated)
    ok = st.upsert_item(st.find_or_create(fine), fine)
    assert "check_out" in m.checks and "dropoff_time" in st.item(g.id).checks
    assert "departure" in u.checks and ok.checks == ()
    assert ids(st.flagged()) == [m.id, u.id]
    assert ids(st.flagged(m.trip_id)) == [m.id]
    assert st.flag_counts() == {m.trip_id: 1, None: 1}
    assert ids(st.unsorted()) == [u.id]


def an_hour_later(res: dict) -> dict:
    leg = res["segments"][0]
    return edit(
        res,
        departure_local=hours(leg["departure_local"], 1),
        arrival_local=hours(leg["arrival_local"], 1),
    )


def same_day_hour_later(res: dict) -> bool:
    leg = res["segments"][0]
    ends = ("departure_local", "arrival_local")
    return all(leg.get(end) and day(hours(leg[end], 1)) == day(leg[end]) for end in ends)


def test_the_sync_state_follows_the_airtrail_flag_the_owner_and_what_was_told():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    c_entry, theirs = booking("flight", lambda r: a_companion_alone(r) and same_day_hour_later(r))
    st = store()
    st.led.kv.set("airtrail", "off")
    mine = st.upsert_item(st.find_or_create(validated(entry, flight)), validated(entry, flight))
    other = st.upsert_item(
        st.find_or_create(validated(c_entry, theirs)), validated(c_entry, theirs)
    )
    assert (mine.sync_state, other.sync_state) == ("off", "off") and not st.airtrail_on()
    st.led.kv.set("airtrail", "on")
    assert st.airtrail_on() is True
    assert st.settle(mine.id).sync_state == "pending" and st.settle(other.id).sync_state == "off"
    told = checks.desired(mine.payload, mine.passengers, mine.status, book())
    done = st.sync_done(mine.id, flight_id=1, told=told)
    assert (done.sync_state, done.told, done.airtrail_flight_id) == ("synced", told, 1)
    said = checks.desired(other.payload, other.passengers, other.status, book())
    assert st.sync_done(other.id, flight_id=2, told=said).sync_state == "off"
    later = st.upsert_item(st.get(mine.trip_id), validated(entry, an_hour_later(flight)))
    assert later.id == mine.id and later.told == told and later.sync_state == "pending"
    moved_too = st.upsert_item(st.get(other.trip_id), validated(c_entry, an_hour_later(theirs)))
    assert moved_too.id == other.id and moved_too.sync_state == "off"
    st.led.kv.set("airtrail", "off")
    assert st.settle(mine.id).sync_state == "off"


def test_what_the_flight_log_is_told_after_a_message_is_recorded_on_the_booking():
    """The executor files and tells the flight log nothing; the pass a message ends with sends
    what is due, and the booking then holds what the flight log was told."""
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    c_entry, theirs = booking("flight", a_companion_alone)
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    st = store(led)
    log = seeded(led, the_corpus())
    syncer = Syncer(store=st, ledger=led, book=st.book, client=log, refs=None, dry_run=False)

    def run(items, *, document_type="booking"):
        n = led.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        mid = led.messages.insert(sha256=f"settle-{n}", source="cli", status="archived")
        wp = plan(items, document_type=document_type, store=st, book=book())
        return Executor(ledger=led, store=st, book=book()).execute(wp, message_row_id=mid)

    (out,) = run([validated(entry, flight)])
    filed = st.item(out.trip_item_id)
    assert (out.result, out.sync) == ("filed", "pending")
    assert (filed.told, filed.airtrail_flight_id) == (None, None) and not list(log.iter_flights())
    syncer.run_pass(now=parse_iso(stamps(entry)[0]))
    ti = st.item(out.trip_item_id)
    (held,) = log.iter_flights()
    assert ti.airtrail_flight_id == held["id"] and ti.sync_state == "synced"
    assert ti.told == checks.desired(ti.payload, ti.passengers, ti.status, book())
    later = sent_at(an_hour_later(flight), stamps(entry)[1])
    (changed,) = run([later], document_type="change")
    after = st.item(changed.trip_item_id)
    assert changed.result == "updated" and after.id == ti.id
    assert after.told == ti.told and after.sync_state == "pending"  # not told yet
    syncer.run_pass(now=parse_iso(stamps(entry)[2]))
    after = st.item(changed.trip_item_id)
    assert after.told != ti.told and after.sync_state == "synced"
    assert after.told == checks.desired(after.payload, after.passengers, after.status, book())
    (other,) = run([validated(c_entry, theirs)])
    mate = st.item(other.trip_item_id)
    assert other.result == "filed" and mate.told is None and mate.sync_state == "off"
    assert len(list(log.iter_flights())) == 1


def interleaved(path, monkeypatch, first, second) -> list[BaseException]:
    """`first(store)` on one connection to the ledger at `path` and, started while its
    transaction is open, `second(store)` on another; what the second raised."""
    failures: list[BaseException] = []

    def other_side():
        try:
            with Ledger.open(path) as theirs:
                second(store(theirs))
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            failures.append(exc)

    worker = threading.Thread(target=other_side)
    real = store_module.rebuild_item

    def rebuild_meanwhile(*args, **kwargs):
        if worker.ident is None:  # the first settle, inside the first write's transaction
            worker.start()
            time.sleep(0.2)  # the other side is now waiting for the write lock
        return real(*args, **kwargs)

    monkeypatch.setattr(store_module, "rebuild_item", rebuild_meanwhile)
    with Ledger.open(path) as mine:
        first(store(mine))
    monkeypatch.setattr(store_module, "rebuild_item", real)
    worker.join(timeout=30)
    assert worker.ident is not None and not worker.is_alive()
    return failures


def edited_and_told(path, monkeypatch, *, edit_first: bool):
    """A stay filed at `path`, then edited on the page and told to a flight log at once, in the
    order given: the stay as it ends, the check-in edited in and what was told."""
    a_stay, stay = booking("lodging")
    with Ledger.open(path) as led:
        st = store(led)
        item = validated(a_stay, stay)
        ti = st.upsert_item(st.find_or_create(item), item)
    told = checks.desired(ti.payload, ti.passengers, ti.status, book())
    mine, at = moved(stay["check_in"], -1), stamps(a_stay)[0]

    def edit_it(s):
        s.edit_item(ti.id, {"check_in": mine}, at=at)

    def tell_it(s):
        s.sync_done(ti.id, flight_id=7, told=told)

    ops = (edit_it, tell_it) if edit_first else (tell_it, edit_it)
    assert interleaved(path, monkeypatch, *ops) == []
    with Ledger.open(path) as fresh:
        return store(fresh).item(ti.id), mine, told


def test_settle_and_a_sync_result_never_overwrite_each_other(tmp_path, monkeypatch):
    """`edit_item` edits a stay and `sync_done` records what a flight log was told of a booking:
    each runs while the other's transaction is open, on its own connection to one file, and the
    row ends with both."""
    for edit_first in (True, False):
        path = tmp_path / str(edit_first) / "ledger.db"
        path.parent.mkdir()
        final, mine, told = edited_and_told(path, monkeypatch, edit_first=edit_first)
        assert final.payload["check_in"] == mine, edit_first
        assert "check_in" in final.payload["owner_edits"], edit_first
        assert (final.airtrail_flight_id, final.told) == (7, told), edit_first


def test_every_store_method_that_changes_a_booking_ends_in_settle(monkeypatch):
    calls: list[int] = []
    real = TripStore.settle

    def recording(self, item_id):
        calls.append(item_id)
        return real(self, item_id)

    monkeypatch.setattr(TripStore, "settle", recording)
    a_stay, stay = booking("lodging")
    entry, flight = booking(
        "flight", lambda r: owners(r) and bool(book().classify(names(r)).guests)
    )
    hotel, leg = validated(a_stay, stay), validated(entry, flight)
    st = store()
    h = st.upsert_item(st.find_or_create(hotel), hotel).id
    assert calls == [h]
    calls.clear()
    f = st.upsert_item(st.find_or_create(leg), leg)
    assert calls == [f.id]
    companion = book().classify(f.passengers).guests[0]
    printed = next(n for n in f.passengers if book().match(n).canonical == companion)
    number = str(int(f.payload["flight_number"]) + 1)
    renumbered = {"flight_number": number, "flight_code": f.payload["carrier"] + number}
    new_key = keys.real_key("flight", {**f.payload, **renumbered}, airports=AIRPORTS)
    told = checks.desired(f.payload, f.passengers, f.status, book())
    earlier = moved(stay["check_in"], -1)

    def restore():
        st.remove_item(h)
        calls.clear()  # a removal is undone by restore_item, which settles
        st.restore_item(h)

    writes = [
        ("edit_item", h, lambda: st.edit_item(h, {"check_in": earlier}, at=stamps(a_stay)[0])),
        ("set_status cancelled", h, lambda: st.set_status(h, "cancelled")),
        ("set_status confirmed", h, lambda: st.set_status(h, "confirmed")),
        ("cancel", h, lambda: st.cancel(hotel.key)),
        ("remove_passengers", f.id, lambda: st.remove_passengers(f.item_key, [printed])),
        ("relink_flight", f.id, lambda: st.relink_flight(f.item_key, new_key, 3, renumbered)),
        ("restore_item", h, restore),
        ("move_item", h, lambda: st.move_item(h, None)),
        ("sync_done", f.id, lambda: st.sync_done(f.id, flight_id=3, told=told)),
        (
            "sync_failed",
            f.id,
            lambda: st.sync_failed(
                f.id, code="outage", now=parse_iso(stamps(entry)[0]), retry_at=None
            ),
        ),
        ("sync_detached", f.id, lambda: st.sync_detached(f.id)),
        ("send_again", f.id, lambda: st.send_again(f.id)),
        ("owner_answer", f.id, lambda: st.owner_answer(f.id, True, at=stamps(entry)[1])),
        ("looks_right", h, lambda: st.looks_right(h, at=stamps(a_stay)[1])),
    ]
    for name, item_id, write in writes:
        calls.clear()
        write()
        assert calls == [item_id], name


# ----------------------------------------------------------------------------- flight log, offsets
class FlightLog:
    """A flight log's API as the backfill reads it."""

    def __init__(self, flights: list[dict]) -> None:
        self.flights = flights

    def profile(self) -> Profile:
        """The user every record of `logged` names aboard."""
        return Profile("owner", "apiKey", frozenset(REQUIRED_SCOPES), frozenset(), True)

    def iter_flights(self, *, limit=100, until_date=None):
        yield from self.flights


def logged(item: rules.Item, fid: int, *, timed=True, zoned=True, origin=None) -> dict:
    """The corpus flight `item` as a flight log's API returns it: `timed` False, with no times
    (a date only); `zoned` False, with no zone on its departure airport's record; `origin`, under
    another departure airport code."""
    p = item.payload

    def airport(end: str, n: int, code: str, zone: bool) -> dict:
        return {
            "id": n,
            "iata": code,
            "name": p[f"{end}_name"],
            "municipality": p[f"{end}_city"],
            "timezone": p[f"{end}_tz"] if zone else None,
        }

    return {
        "id": fid,
        "date": p["date_local"],
        "departure": p["dep_utc"] if timed else None,
        "arrival": p["arr_utc"] if timed else None,
        "duration": None,
        "flightNumber": p["flight_code"],
        "aircraftReg": None,
        "note": f"PNR {item.pnr}" if item.pnr else "",
        "from": airport("origin", 1, origin or p["origin"], zoned),
        "to": airport("destination", 2, p["destination"], True),
        "airline": {"id": 3, "name": p.get("airline_name"), "icao": None},
        "passengers": [{"user": {"id": "owner"}, "seatNumber": None, "seatClass": None}],
    }


def unknown_airport() -> str:
    """A code the bundled airport table does not hold, found at run time."""
    letters = string.ascii_uppercase[::-1]
    return next(
        code
        for code in map("".join, itertools.product(letters, repeat=3))
        if AIRPORTS.airport(code) is None
    )


def zone_utc(local: str, zone: str) -> str:
    """The local time `local` read in `zone`, in UTC as the validator writes it."""
    when = rules.parse_local(local).replace(tzinfo=ZoneInfo(zone))
    return rules.to_utc_iso(when)


def backfilled(record: dict) -> tuple[TripStore, bf.BackfillItem, store_module.TripItem]:
    """`record` filed by the backfill into a new ledger whose flight log is on."""
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    st = store(led)
    me = book().me_name
    bf.backfill(led, st, FlightLog([record]), since=record["date"], me_name=me, apply=True)
    it = bf.flight_item(record, me)
    return st, it, st.find(it.key)


def test_a_flight_the_flight_log_gave_is_kept_as_the_flight_log_said_it():
    entry, flight = booking("flight", owners)
    email = validated(entry, flight)
    cases = {
        "a date only": logged(email, 1, timed=False),
        "no zone": logged(email, 2, zoned=False),
        "an unknown origin": logged(email, 3, origin=unknown_airport()),
    }
    for case, record in cases.items():
        st, it, ti = backfilled(record)
        stored = row(st, ti.id)
        assert stored["payload_json"] == json.dumps(it.payload, default=str), case
        assert (ti.item_key, ti.start_at, ti.end_at) == (it.key, *grouping.item_span(it)[:2]), case
        # filed as told what the flight log holds of it: its own record
        assert ti.checks == () and ti.sync_state == "synced", case
        assert ti.told == checks.desired(ti.payload, ti.passengers, ti.status, book()), case
        assert st.settle(ti.id) == ti and row(st, ti.id) == stored, case
    st, it, ti = backfilled(cases["a date only"])
    assert ti.start_at == email.payload["date_local"] and it.payload["dep_utc"] is None
    assert "T00:00" not in row(st, ti.id)["payload_json"].replace(email.payload["dep_utc"], "")

    # an email for the flight: one row, derived from the email's fields from then on
    for case in ("a date only", "no zone"):
        st, it, ti = backfilled(cases[case])
        again = st.upsert_item(st.get(ti.trip_id), email)
        rows = st.led.conn.execute("SELECT id FROM trip_items").fetchall()
        assert again.id == ti.id and len(rows) == 1, case
        for field in store_module.FLIGHT_DERIVED:
            assert again.payload.get(field) == email.payload.get(field), (case, field)
        assert again.item_key == email.key and "source" not in again.payload, case

    # the owner sets its departure: derived from the edit
    st, it, ti = backfilled(cases["a date only"])
    mine = email.payload["dep_local"]
    edited = {**ti.payload, "dep_local": mine, "owner_edits": {"dep_local": stamps(entry)[0]}}
    st.led.trip_items.update(ti.id, payload=edited)
    after = st.settle(ti.id)
    assert after.payload["dep_utc"] == zone_utc(mine, email.payload["origin_tz"])
    assert after.payload["dep_local"] == mine and after.start_at == mine
    assert after.payload["date_local"] == email.payload["date_local"] and after.checks == ()


def test_an_edit_on_the_page_ends_a_flight_log_flights_exemption():
    """The owner edits a flight the flight log gave, which no email has changed: what the owner
    typed is stamped under the payload's own fields, so the flight is derived from it from then
    on, as an email's would be. One that holds only a day and is given only an arrival on the
    form is not given a midnight: its departure is the check that says so."""
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    email = validated(entry, flight)
    p = email.payload
    # its departure airport's record has no zone there: the bundled table's stands in
    st, _, ti = backfilled(logged(email, 1, zoned=False))
    assert ti.payload["origin_tz"] == p["origin_tz"] and ti.payload["source"] == bf.SOURCE
    mine = hours(p["dep_local"], 1)
    after = st.edit_item(ti.id, {"dep_local": mine, "arr_local": hours(p["arr_local"], 1)})
    assert set(after.payload["owner_edits"]) == {"dep_local", "arr_local"}
    assert after.payload["origin_tz"] == p["origin_tz"]
    assert after.payload["dep_utc"] == zone_utc(mine, p["origin_tz"])
    assert (after.start_at, after.checks, after.sync_state) == (mine, (), "pending")
    # a day alone, and an arrival typed in beside it
    st, _, ti = backfilled(logged(email, 2, timed=False))
    day_alone = ti.payload["dep_local"]
    assert rules.day_alone(day_alone) == day_alone and ti.payload["arr_local"] is None
    lands = grouping.local_str(p["arr_local"])
    form = {**views._edit_values(ti), "arrives_date": lands[:10], "arrives_time": lands[11:]}
    fields = views._edit_fields("flight", form)
    assert fields["dep_local"] == day_alone  # as the form shows it: a day
    after = st.edit_item(ti.id, fields)
    assert set(after.payload["owner_edits"]) == {"arr_local"}
    assert after.payload["dep_local"] == day_alone and after.payload["dep_utc"] is None
    assert after.payload["arr_utc"] == zone_utc(lands, p["destination_tz"])
    assert (after.checks, after.sync_state) == (("departure",), "blocked")


def printed_offset(local: str, utc: str, hours_off: int) -> str:
    """The UTC offset of `local` at the instant `utc`, `hours_off` hours further east, written
    the way an extractor writes a printed one."""
    gap = rules.parse_local(local) - parse_iso(utc).replace(tzinfo=None)
    minutes = int(gap.total_seconds() // 60) + 60 * hours_off
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def test_a_printed_offset_keeps_the_instants_first_filed():
    entry, flight = booking("flight", lambda r: owners(r) and same_day_hour_later(r))
    p = validated(entry, flight).payload  # each time read in its airport's zone
    dep_offset = printed_offset(p["dep_local"], p["dep_utc"], 1)
    arr_offset = printed_offset(p["arr_local"], p["arr_utc"], 1)
    odd = edit(
        flight,
        departure_offset=dep_offset,
        arrival_offset=arr_offset,
        departure_tz=None,
        arrival_tz=None,
    )
    item = validated(entry, odd)
    assert (item.payload["dep_offset"], item.payload["arr_offset"]) == (dep_offset, arr_offset)
    assert item.payload["dep_utc"] != p["dep_utc"] and item.payload["arr_utc"] != p["arr_utc"]

    def instants(t) -> tuple:
        return t.payload["dep_utc"], t.payload["arr_utc"]

    st = store()
    ti = st.upsert_item(st.find_or_create(item), item)
    assert instants(ti) == instants(st.settle(ti.id)) == instants(item)
    again = st.upsert_item(st.get(ti.trip_id), validated(entry, odd))  # the same email again
    assert again.id == ti.id and instants(again) == instants(item)
    # an email printing the departure with no offset: that end is read in its airport's zone
    plain = st.upsert_item(st.get(ti.trip_id), validated(entry, edit(odd, departure_offset=None)))
    assert "dep_offset" not in plain.payload and plain.payload["arr_offset"] == arr_offset
    assert instants(plain) == (p["dep_utc"], item.payload["arr_utc"])
    st.upsert_item(st.get(ti.trip_id), validated(entry, odd))
    # the owner moves the departure an hour: that end is read in its airport's zone too
    later = hours(p["dep_local"], 1)
    edited = {
        **st.item(ti.id).payload,
        "dep_local": later,
        "owner_edits": {"dep_local": stamps(entry)[0]},
    }
    st.led.trip_items.update(ti.id, payload=edited)
    after = st.settle(ti.id)
    assert "dep_offset" not in after.payload and after.payload["arr_offset"] == arr_offset
    assert instants(after) == (zone_utc(later, p["origin_tz"]), item.payload["arr_utc"])


def test_a_printed_zone_keeps_the_instants_first_filed():
    """A zone printed beside each time, other than its airport's, is the zone each is read in:
    through settle and through the same email filed again."""

    def other_zones(entry, res) -> dict | None:
        """`res` with each time's zone printed as the other airport's and no offset, when that
        reads both times as other instants than their airports' zones do and the flight still
        validates with no finding."""
        p = validated(entry, res).payload
        here, there = p["origin_tz"], p["destination_tz"]
        if zone_utc(p["dep_local"], there) == zone_utc(p["dep_local"], here):
            return None
        if zone_utc(p["arr_local"], here) == zone_utc(p["arr_local"], there):
            return None
        odd = edit(
            res,
            departure_tz=there,
            arrival_tz=here,
            departure_offset=None,
            arrival_offset=None,
        )
        return None if checks.codes(validated(entry, odd)) else odd

    entry, flight, odd = next(
        (e, r, o)
        for e, r in bookings("flight")
        if owners(r) and not checks.codes(validated(e, r)) and (o := other_zones(e, r))
    )
    p = validated(entry, flight).payload
    item = validated(entry, odd)
    assert (item.payload["dep_zone"], item.payload["arr_zone"]) == (
        p["destination_tz"],
        p["origin_tz"],
    )
    assert item.payload["dep_utc"] == zone_utc(p["dep_local"], p["destination_tz"])
    assert item.payload["arr_utc"] == zone_utc(p["arr_local"], p["origin_tz"])

    def instants(t) -> tuple:
        return t.payload["dep_utc"], t.payload["arr_utc"]

    st = store()
    ti = st.upsert_item(st.find_or_create(item), item)
    assert instants(ti) == instants(st.settle(ti.id)) == instants(item)
    again = st.upsert_item(st.get(ti.trip_id), validated(entry, odd))  # the same email again
    assert again.id == ti.id and instants(again) == instants(item)
    assert instants(st.settle(again.id)) == instants(item)


def test_a_moved_date_keeps_no_printed_offset():
    entry, flight = booking(
        "flight", lambda r: bool(r["segments"][0].get("departure_offset")) and owners(r)
    )
    ex = Extraction.model_validate(
        {"document_type": "booking", "reservations": [flight], "confidence": 1.0}
    )
    shift = DateShift(days=7)
    shift.extraction(ex)
    sent = corpus.sent(entry) + dt.timedelta(days=shift.days)
    (item,) = rules.validate_extraction(
        ex, refdata=AIRPORTS, book=book(), email_date=sent, source={"message_id": "<settle>"}
    )
    assert not {"dep_offset", "arr_offset"} & set(item.payload)
    assert item.payload["dep_utc"] == zone_utc(item.payload["dep_local"], item.payload["origin_tz"])


def test_a_relinked_flight_takes_the_flight_logs_schedule_as_it_said_it():
    entry, flight = booking(
        "flight", lambda r: bool(r["segments"][0].get("departure_offset")) and owners(r)
    )
    email = validated(entry, flight)
    st = store()
    ti = st.upsert_item(st.find_or_create(email), email)
    assert {"dep_offset", "arr_offset"} <= set(ti.payload)
    it = bf.flight_item(logged(email, 5, timed=False), book().me_name)  # a date only
    schedule = {k: it.payload.get(k) for k in bf.SCHEDULE_FIELDS}
    after = st.relink_flight(ti.item_key, it.key, 5, schedule)
    assert after.id == ti.id and after.payload["source"] == bf.SOURCE
    assert not {"dep_offset", "dep_zone", "arr_offset", "arr_zone"} & set(after.payload)
    assert after.payload["dep_local"] == after.start_at == it.payload["dep_local"]
    assert after.payload["dep_utc"] is None and after.item_key == it.key


# ----------------------------------------------------------------------------- a day and no time
def same_day(res: dict) -> bool:
    leg = res["segments"][0]
    return day(leg["departure_local"]) == day(leg["arrival_local"])


def days_alone(res: dict) -> dict:
    """`res` with the day of each of its leg's times printed and no time."""
    leg = res["segments"][0]
    ends = ("departure_local", "arrival_local")
    return edit(res, **{end: day(leg[end]).isoformat() for end in ends})


def test_a_flight_with_a_day_and_no_time_is_in_its_trip_by_its_day_and_marked():
    entry, flight = booking("flight", owners)
    leg = flight["segments"][0]
    the_day = day(leg["departure_local"]).isoformat()
    bare, dated = validated(entry, days_alone(flight)), validated(entry, flight)
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    st = store(led)
    ti = st.upsert_item(st.find_or_create(bare), bare)
    assert ti.checks == ("departure",) and ti.sync_state == "blocked"
    assert ti.item_key == dated.key and st.unsorted() == []
    assert ti.trip_id == st.find_or_create(dated).id and ti.id in ids(st.items(ti.trip_id))
    assert ti.start_at == ti.payload["dep_local"] == ti.payload["date_local"] == the_day
    assert ti.payload["dep_utc"] is None and ti.payload["arr_utc"] is None
    assert "T00:00" not in row(st, ti.id)["payload_json"].replace(ti.payload["email_date"], "")
    # nothing is invented by a second settle, nor by the same email filed again
    before = row(st, ti.id), dict(st.led.trips.get(ti.trip_id))
    st.settle(ti.id)
    assert (row(st, ti.id), dict(st.led.trips.get(ti.trip_id))) == before
    again = st.upsert_item(st.get(ti.trip_id), validated(entry, days_alone(flight)))
    assert again.id == ti.id and again.checks == ("departure",)
    assert again.payload["dep_utc"] is None and again.start_at == the_day
    # given its time, the check goes and the instant appears
    st.led.trip_items.update(ti.id, payload={**again.payload, "dep_local": leg["departure_local"]})
    timed = st.settle(ti.id)
    assert timed.checks == () and timed.sync_state == "pending"
    assert timed.payload["dep_utc"] == dated.payload["dep_utc"]
    assert timed.payload["dep_local"] == dated.payload["dep_local"] == timed.start_at
    assert (timed.item_key, timed.trip_id) == (ti.item_key, ti.trip_id)


def shown(st: TripStore, ti: store_module.TripItem) -> tuple:
    """How the booking `ti` is shown: the day the text lists it on and its first line there, its
    time, zone and lines on the trip page, and the dates of its event in the calendar feed."""
    items = st.items(ti.trip_id)
    ((when, line),) = [
        (d, dl)
        for d, lines in fmt.group_by_day(items, book=book(), home_airports=HOME)
        for dl in lines
        if dl.item.id == ti.id
    ]
    shows = SimpleNamespace(home=HOME, book=book(), flight_log=False, dry_run=False)
    page = views.present(line, items, shows)
    event = ical.event(st.item(ti.id), items, book=book(), home=HOME)
    dates = [ln for ln in event if ln.startswith(("DTSTART", "DTEND"))]
    return when, line.lines[0], (page["time"], page["tz"], page["lines"]), dates


def test_a_flight_with_a_day_and_no_time_is_shown_as_the_flight_logs_date_only_flight():
    """On the page, in the text and in the feed: listed on its day with no time, exactly as a
    flight the flight log gave with a date only."""
    entry, flight = booking("flight", lambda r: owners(r) and same_day(r))
    the_day = day(flight["segments"][0]["departure_local"])
    bare = validated(entry, days_alone(flight))
    st = store()
    ti = st.upsert_item(st.find_or_create(bare), bare)
    log_st, _, log_ti = backfilled(logged(validated(entry, flight), 1, timed=False))
    mailed = shown(st, ti)
    assert mailed == shown(log_st, log_ti)
    when, line, page, dates = mailed
    assert when == the_day and page == ("", "", [])
    after = the_day + dt.timedelta(days=1)
    assert dates == [f"DTSTART;VALUE=DATE:{the_day:%Y%m%d}", f"DTEND;VALUE=DATE:{after:%Y%m%d}"]
    text = fmt.trip_text(st.get(ti.trip_id), st.items(ti.trip_id), book=book(), home_airports=HOME)
    assert f"{fmt.day_label(the_day)}\n{line}" in text and "12:00 AM" not in text


def test_an_arrival_set_on_a_date_only_flight_log_flight_leaves_the_departure_a_day_alone():
    """The owner gives a backfilled flight with a date only its arrival: the flight is derived
    from its fields from then on, and its departure stays the day with no time."""
    entry, flight = booking("flight", owners)
    email = validated(entry, flight)
    st, _, ti = backfilled(logged(email, 1, timed=False))
    the_day = email.payload["date_local"]
    assert ti.payload["dep_local"] == the_day and ti.payload["arr_local"] is None
    mine, at = email.payload["arr_local"], stamps(entry)[0]
    edited = {**ti.payload, "arr_local": mine, "owner_edits": {"arr_local": at}}
    st.led.trip_items.update(ti.id, payload=edited)
    after = st.settle(ti.id)
    p = after.payload
    assert p["dep_local"] == p["date_local"] == after.start_at == the_day
    assert p["dep_utc"] is None and p["duration_min"] is None
    assert p["arr_local"] == mine and p["arr_utc"] == zone_utc(mine, p["destination_tz"])
    assert after.checks == ("departure",) and after.sync_state == "blocked"
    assert after.item_key == ti.item_key
    stored = row(st, ti.id)["payload_json"]
    assert "T00:00" not in stored.replace(mine, "").replace(p["arr_utc"], "").replace(at, "")
    st.settle(ti.id)
    assert row(st, ti.id)["payload_json"] == stored


PRINTED = ("dep_offset", "dep_zone", "arr_offset", "arr_zone")


def test_a_newer_email_printing_a_held_times_day_alone_leaves_the_time():
    """A day printed alone says nothing about the time: a newer email that prints the day of a
    time the booking holds, and no time, leaves that time with its instant and what it was read
    by, and never wins over the owner's own time; a day alone on another day moves that end."""
    entry, flight = booking(
        "flight",
        lambda r: owners(r)
        and bool(r["segments"][0].get("departure_offset"))
        and same_day_hour_later(r),
    )
    leg = flight["segments"][0]
    item = validated(entry, flight)
    st = store()
    ti = st.upsert_item(st.find_or_create(item), item)
    first, second, third = stamps(entry)

    def held(t) -> tuple:
        fields = {k: t.payload.get(k) for k in (*store_module.FLIGHT_DERIVED, *PRINTED)}
        return fields, t.checks, t.item_key, t.start_at, t.end_at, t.trip_id

    before = held(ti)
    assert before[0]["dep_offset"] and before[1] == ()
    for ends in (("departure_local", "arrival_local"), ("arrival_local",)):
        bare = sent_at(edit(flight, **{end: day(leg[end]).isoformat() for end in ends}), first)
        assert held(st.upsert_item(st.get(ti.trip_id), bare)) == before, ends
    # the owner sets the departure an hour later; a newer email printing its day alone keeps it
    mine = hours(item.payload["dep_local"], 1)
    owned = {**st.item(ti.id).payload, "dep_local": mine, "owner_edits": {"dep_local": first}}
    st.led.trip_items.update(ti.id, payload=owned)
    edited = st.settle(ti.id)
    alone = edit(flight, departure_local=day(leg["departure_local"]).isoformat())
    later = st.upsert_item(st.get(ti.trip_id), sent_at(alone, second))
    assert later.id == ti.id and later.payload["dep_local"] == mine == later.start_at
    held_utc = zone_utc(mine, later.payload["origin_tz"])  # read in its airport's zone
    assert later.payload["dep_utc"] == edited.payload["dep_utc"] == held_utc
    assert "dep_local" in later.payload["owner_edits"] and later.checks == ()
    assert not {"dep_offset", "dep_zone"} & set(later.payload)
    # an arrival printed as a day alone on another day: that end moves, its instant goes
    other = (day(leg["arrival_local"]) + dt.timedelta(days=1)).isoformat()
    moved_on = st.upsert_item(st.get(ti.trip_id), sent_at(edit(alone, arrival_local=other), third))
    p = moved_on.payload
    assert (p["arr_local"], p["arr_utc"], p["duration_min"]) == (other, None, None)
    assert moved_on.end_at == other and moved_on.checks == ()
    assert p["dep_local"] == mine and p["dep_utc"] == later.payload["dep_utc"]


def test_a_newer_email_with_the_time_completes_a_flight_that_held_only_its_day():
    entry, flight = booking(
        "flight", lambda r: owners(r) and bool(r["segments"][0].get("departure_offset"))
    )
    bare = validated(entry, days_alone(flight))
    st = store()
    ti = st.upsert_item(st.find_or_create(bare), bare)
    assert ti.checks == ("departure",) and ti.payload["dep_utc"] is None
    timed = st.upsert_item(st.get(ti.trip_id), sent_at(flight, stamps(entry)[0]))
    want = validated(entry, flight).payload
    assert timed.id == ti.id and timed.checks == () and timed.item_key == ti.item_key
    for field in (*store_module.FLIGHT_DERIVED, *PRINTED):
        assert timed.payload.get(field) == want.get(field), field
