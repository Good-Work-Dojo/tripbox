# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Share links (`trips.shares`): a random token of 192 bits, of which the ledger finds a link by
its SHA-256 alone; a link works until 7 days after its trip ends, and at least 30 days after it
was made, and not once it is revoked or its trip is deleted; a trip that goes for good (a merge
empties its source) takes its links with it."""

import datetime as dt
import hashlib

import pytest
from test_trips_store import inbound, outbound, store

from tripbox import util
from tripbox.trips import shares

# When the links of these tests are made, unless a test says otherwise
MADE = dt.datetime(2027, 3, 1, 9, 0, tzinfo=dt.UTC)


def a_trip(st, **kw):
    """A trip of one flight of the Sample family's (`outbound`, with `kw`)."""
    item = outbound(**kw)
    trip = st.find_or_create(item)
    st.upsert_item(trip, item)
    return st.get(trip.id)


def made(st, trip_id, shows=shares.DEFAULT, at=MADE):
    with util.clock_at(at):
        return shares.create(st.led, trip_id, shows)


def test_a_token_is_192_bits_and_only_its_hash_finds_the_link():
    st = store()
    trip = a_trip(st)
    link = made(st, trip.id)
    assert len(link.token) == 32 and set(link.token) <= set(shares.ALPHABET)
    assert link.token not in repr(link) and link.token not in str(link)
    assert link.shows == shares.DEFAULT == ("names", "addresses", "seats", "times")
    assert shares.FIELDS == ("names", "addresses", "seats", "times", "codes", "notes")
    row = st.led.shares.by_hash(hashlib.sha256(link.token.encode()).hexdigest())
    assert row is not None and row["id"] == link.id and row["trip_id"] == trip.id
    # the ledger looks a link up by its hash: the token itself finds nothing
    assert st.led.shares.by_hash(link.token) is None
    today = MADE.date()
    found = shares.find(st.led, st, link.token, today)
    assert found is not None and found[0].id == link.id and found[1].id == trip.id
    # every link has a token of its own
    others = {made(st, trip.id).token for _ in range(20)}
    assert len(others | {link.token}) == 21
    # a token that is not 32 URL-safe characters, or that no link holds, is not found
    for wrong in (
        "",
        link.token[:-1],
        link.token + "A",
        link.token[:-1] + "=",
        link.token[:-1] + "é",
        link.token[:-1] + "/",
        "A" * 32,
        link.token.swapcase() if link.token.swapcase() != link.token else "B" * 32,
        "x" * 100_000,
    ):
        assert shares.find(st.led, st, wrong, today) is None, wrong[:40]
    # the fields a link shows are FIELDS's, in FIELDS's order; any other is refused
    every = made(st, trip.id, ("notes", "codes", "names"))
    assert every.shows == ("names", "codes", "notes")
    assert made(st, trip.id, ()).shows == ()
    count = len(st.led.shares.live())
    with pytest.raises(ValueError):
        made(st, trip.id, ("names", "passwords"))
    assert len(st.led.shares.live()) == count
    # a revoked link is not found, nor listed with its trip's
    st.led.shares.revoke(link.id)
    assert shares.find(st.led, st, link.token, today) is None
    assert link.id not in {r["id"] for r in st.led.shares.by_trip(trip.id)}


def test_a_link_works_until_7_days_after_the_trip_and_at_least_30():
    st = store()
    trip = a_trip(st)
    end = dt.date.fromisoformat(trip.end_date)
    # a trip far ahead: until a week after it ends
    ahead = made(st, trip.id, at=dt.datetime.combine(end, dt.time(9), dt.UTC) - dt.timedelta(400))
    assert shares.good_until(ahead, trip) == end + dt.timedelta(7)
    # a trip that ends tomorrow: 30 days from the day the link was made
    tomorrow = made(st, trip.id, at=dt.datetime.combine(end, dt.time(9), dt.UTC) - dt.timedelta(1))
    assert shares.good_until(tomorrow, trip) == end - dt.timedelta(1) + dt.timedelta(30)
    # a trip long past: 30 days from the day the link was made
    later = dt.datetime.combine(end, dt.time(9), dt.UTC) + dt.timedelta(400)
    past = made(st, trip.id, at=later)
    assert shares.good_until(past, trip) == later.date() + dt.timedelta(30)
    for link in (ahead, tomorrow, past):
        last = shares.good_until(link, trip)
        assert shares.find(st.led, st, link.token, last) is not None
        assert shares.find(st.led, st, link.token, last + dt.timedelta(1)) is None
    # a later booking moves the trip's end, and the day its links work until with it
    home = inbound()
    assert dt.date.fromisoformat(home.payload["dep_local"][:10]) > end
    st.upsert_item(trip, home)
    moved = st.get(trip.id)
    new_end = dt.date.fromisoformat(moved.end_date)
    assert new_end > end
    assert shares.good_until(ahead, moved) == new_end + dt.timedelta(7)
    assert shares.find(st.led, st, ahead.token, new_end + dt.timedelta(7)) is not None
    assert shares.good_until(past, moved) == later.date() + dt.timedelta(30)


def test_merging_a_trip_ends_the_source_trips_links_and_keeps_the_targets():
    st = store()
    target = a_trip(st)
    source = a_trip(st, dep="2027-09-02T08:20", arr="2027-09-02T11:55", pnr="SRC921")
    assert source.id != target.id
    kept, gone = made(st, target.id), made(st, source.id)
    today = MADE.date()
    assert shares.find(st.led, st, gone.token, today) is not None
    merged = st.merge(source.id, target.id)
    assert merged.id == target.id and st.led.trips.get(source.id) is None
    # the source's link is gone with its trip: never widened to the trip it went into
    assert st.led.shares.by_hash(hashlib.sha256(gone.token.encode()).hexdigest()) is None
    assert shares.find(st.led, st, gone.token, today) is None
    found = shares.find(st.led, st, kept.token, today)
    assert found is not None and found[1].id == target.id
    assert [r["id"] for r in st.led.shares.by_trip(target.id)] == [kept.id]


def test_a_deleted_trips_link_is_not_found_and_a_restored_trips_is():
    st = store()
    trip = a_trip(st)
    link = made(st, trip.id)
    today = MADE.date()
    st.delete_trip(trip.id)
    assert shares.find(st.led, st, link.token, today) is None
    assert link.id not in {live.id for live, _, _ in shares.live(st.led, st, today)}
    st.restore_trip(trip.id)
    found = shares.find(st.led, st, link.token, today)
    assert found is not None and found[1].id == trip.id
    assert [live.id for live, _, _ in shares.live(st.led, st, today)] == [link.id]
    # once Recently deleted is purged, the trip and its link are gone for good
    st.delete_trip(trip.id)
    st.purge_deleted(now=MADE + dt.timedelta(days=400))
    assert st.led.trips.get(trip.id) is None and st.led.shares.live() == []

