# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The flight-data service's window: it reads a bare date as its midnight UTC and `end` as
exclusive, so a flight leaving in the evening west of UTC (after midnight UTC on its own local
day) is asked for with a window that reaches the day after. The answers are the hand-written
ones in `tests/data/aeroapi/`; the Sample family's DL 1234 leaves MSP at 19:30 Central time."""

import datetime as dt

from test_enrich import answered, enrich_recent, landed_ctx, service, the_flight

DEP = "2027-06-22T19:30:00-05:00"
ARR = "2027-06-22T23:05:00-04:00"
AT = dt.datetime(2027, 6, 23, 12, 0, tzinfo=dt.UTC)


def test_an_evening_departure_west_of_utc_is_filled_on_the_first_lookup(tmp_path, monkeypatch):
    ctx = landed_ctx(tmp_path, monkeypatch, dep=DEP, arr=ARR)
    ti, _ = the_flight(ctx)
    assert ti.payload["dep_utc"].startswith("2027-06-23T00:30")  # the next day in UTC
    aero, asked = service({"DAL1234": "flights-evening.json"})
    (line,) = enrich_recent(ctx, aero, at=AT)
    assert asked == ["/account/usage", "/flights/DAL1234"]
    evening = answered("flights-evening.json")["flights"]
    booked = next(f for f in evening if f["scheduled_out"] == "2027-06-23T00:30:00Z")
    assert line.startswith(f"🛬 DAL1234 2027-06-22 enriched: aircraftReg {booked['registration']}")
    ((_, put),) = ctx.airtrail.puts
    assert put["aircraftReg"] == booked["registration"]
    assert ctx.ledger.enrichment.get(ti.id)["attempts"] == 1


def test_a_flight_outside_the_window_or_on_another_local_day_is_not_picked(tmp_path, monkeypatch):
    """The window runs from the day before to the day after the flight's local day: the same
    number the evening before is asked for but is on another local day, and the evening after
    falls outside the window; neither is ever the one filled in."""
    ctx = landed_ctx(tmp_path, monkeypatch, dep=DEP, arr=ARR)
    aero, _ = service({"DAL1234": "flights-evening.json"})
    start, end = dt.date(2027, 6, 21), dt.date(2027, 6, 24)
    returned = [f["scheduled_out"] for f in aero.flights("DAL1234", start, end)]
    assert returned == ["2027-06-22T00:30:00Z", "2027-06-23T00:30:00Z"]
    enrich_recent(ctx, aero, at=AT)
    ((_, put),) = ctx.airtrail.puts
    others = [
        f["registration"]
        for f in answered("flights-evening.json")["flights"]
        if f["scheduled_out"] != "2027-06-23T00:30:00Z"
    ]
    assert put["aircraftReg"] not in others
