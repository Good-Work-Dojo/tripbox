# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
from test_planner import BOOK
from test_trips_format import sample_trip

from tripbox.ledger import Ledger
from tripbox.trips import ical
from tripbox.trips.store import TripStore

HOME = ("MSP",)


def unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_trip_calendar_structure():
    st, trip = sample_trip()
    hotel = __import__("test_planner").items_for(
        ["Alex Sample"], kind="lodging", dep="2027-09-16", arr="2027-09-21", pnr="HOTEL1"
    )[0]
    hotel.payload["address"] = "100 Example Street, Philadelphia, PA 00000"
    st.upsert_item(st.get(trip.id), hotel)
    st.cancel("DL|9246|2027-09-21|PHL")
    ics = ical.trip_ics(st.get(trip.id), st.items(trip.id), book=BOOK, home_airports=HOME)
    assert ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//tripbox//trips//EN\r\n")
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    lines = unfold(ics.rstrip("\r\n"))
    assert "X-WR-CALNAME:Philadelphia" in lines and lines[-1] == "END:VCALENDAR"
    assert lines.count("BEGIN:VEVENT") == 4
    events = ics.split("BEGIN:VEVENT")[1:]
    assert ical.UID_DOMAIN == "tripbox.invalid"
    flight = st.item_by_key("SY|9304|2027-09-16|MSP")
    out = next(
        e
        for e in events
        if f"UID:tripbox-item-{flight.id}@{ical.UID_DOMAIN}" in e.replace("\r\n ", "")
    )
    out_lines = unfold(out)
    assert "DTSTART:20270916T152500Z" in out_lines and "DTEND:20270916T175800Z" in out_lines
    assert "SUMMARY:Sun Country 9304 · MSP 10:25 AM CDT → PHL 1:58 PM EDT" in out_lines
    assert "LOCATION:MSP → PHL" in out_lines
    assert any(
        ln.startswith("DESCRIPTION:✈️ Sun Country 9304 · MSP 10:25 AM CDT → PHL 1:58 PM EDT\\n")
        and "conf QTWZSV" in ln
        for ln in out_lines
    )
    assert any(ln.startswith("DTSTAMP:") and ln.endswith("Z") for ln in out_lines)
    hot = next(e for e in events if "HOTEL1" in e.replace("\r\n ", ""))
    hot_lines = unfold(hot)
    assert "DTSTART;VALUE=DATE:20270916" in hot_lines and "DTEND;VALUE=DATE:20270922" in hot_lines
    assert "LOCATION:100 Example Street\\, Philadelphia\\, PA 00000" in hot_lines
    car = next(e for e in events if "1234567890" in e.replace("\r\n ", ""))
    car_lines = unfold(car)
    assert (
        "DTSTART:20270916T184500Z" in car_lines and "DTEND:20270921T171500Z" in car_lines
    )  # EDT -> UTC via zone_at
    back = next(e for e in events if "Q9TW8Z" in e.replace("\r\n ", ""))
    assert "STATUS:CANCELLED" in unfold(back)


def test_description_escaping_and_folding():
    st, trip = sample_trip()
    st.set_notes(trip.id, "Gate code 1234\n\nBEGIN:VEVENT is just text 🎉; really, it is")
    ics = ical.trip_ics(st.get(trip.id), st.items(trip.id), book=BOOK, home_airports=HOME)
    lines = unfold(ics)
    assert any(
        ln.startswith(
            "X-WR-CALDESC:Gate code 1234\\n\\nBEGIN:VEVENT is just text 🎉\\; really\\, it is"
        )
        for ln in lines
    )
    assert "BEGIN:VEVENT is just text" not in ics.split("\r\n")  # never a bare BEGIN: line
    long = "x" * 300
    folded = ical.fold(f"DESCRIPTION:{long}")
    assert (
        all(len(p.encode()) <= 75 for p in folded.split("\r\n"))
        and folded.replace("\r\n ", "") == f"DESCRIPTION:{long}"
    )
    folded_emoji = ical.fold("SUMMARY:" + "🎉" * 40)
    assert (
        folded_emoji.replace("\r\n ", "").encode("utf-8").decode("utf-8") == "SUMMARY:" + "🎉" * 40
    )


def test_feed_contains_every_trip_and_token_is_stable_until_rotated():
    st, trip = sample_trip()
    led = st.led
    t1 = ical.feed_token(led)
    assert len(t1) >= 32 and ical.feed_token(led) == t1
    t2 = ical.feed_token(led, rotate=True)
    assert t2 != t1 and ical.feed_token(led) == t2
    feed = ical.feed_ics(st, book=BOOK, home_airports=HOME)
    assert "X-WR-CALNAME:Trips" in unfold(feed) and unfold(feed).count("BEGIN:VEVENT") == 3
    empty = ical.feed_ics(TripStore(Ledger.memory(), HOME), book=BOOK, home_airports=HOME)
    assert "BEGIN:VEVENT" not in empty and empty.endswith("END:VCALENDAR\r\n")


def test_date_only_car_rental_is_an_all_day_span_and_control_characters_are_dropped():
    from test_planner import items_for
    from test_trips_store import car_item

    st, trip = sample_trip()
    car = car_item(pickup="DEN", start="2027-09-17", end="2027-09-19", code="DAYCAR")
    st.upsert_item(st.get(trip.id), car)
    hotel = items_for(
        ["Alex Sample"], kind="lodging", dep="2027-09-16", arr="2027-09-17", pnr="CRLF1"
    )[0]
    hotel.payload["address"] = "100 Example Street\r\nPhiladelphia, PA\x07"
    st.upsert_item(st.get(trip.id), hotel)
    ics = ical.trip_ics(st.get(trip.id), st.items(trip.id), book=BOOK, home_airports=HOME)
    lines = unfold(ics.rstrip("\r\n"))
    assert "DTSTART;VALUE=DATE:20270917" in lines and "DTEND;VALUE=DATE:20270920" in lines
    assert not any("\r" in ln or "\x07" in ln for ln in lines)
    assert any(ln.startswith("LOCATION:100 Example Street\\nPhiladelphia\\, PA") for ln in lines)


def test_cancelled_items_say_so_in_the_summary():
    st, trip = sample_trip()
    st.cancel("DL|9246|2027-09-21|PHL")
    ics = ical.trip_ics(st.get(trip.id), st.items(trip.id), book=BOOK, home_airports=HOME)
    lines = unfold(ics.rstrip("\r\n"))
    assert any(ln.startswith("SUMMARY:Cancelled: Delta 9246") for ln in lines)
    assert "STATUS:CANCELLED" in lines


def test_feed_omits_removed_items_and_deleted_trips():
    st, trip = sample_trip()
    gone = st.items(trip.id)[0]
    st.remove_item(gone.id)
    feed = ical.feed_ics(st, book=BOOK, home_airports=HOME)
    assert unfold(feed).count("BEGIN:VEVENT") == 2 and gone.item_key not in feed
    assert f"UID:tripbox-item-{gone.id}@" not in "\n".join(unfold(feed))
    st.delete_trip(trip.id)
    assert "BEGIN:VEVENT" not in ical.feed_ics(st, book=BOOK, home_airports=HOME)


def test_a_flights_uid_stays_when_its_day_changes_its_key():
    st, trip = sample_trip()
    out = next(ti for ti in st.items(trip.id) if ti.payload.get("pnr") == "QTWZSV")

    def uid():
        ics = ical.trip_ics(st.get(trip.id), st.items(trip.id), book=BOOK, home_airports=HOME)
        event = next(e for e in ics.split("BEGIN:VEVENT")[1:] if "QTWZSV" in e.replace("\r\n ", ""))
        return next(ln for ln in unfold(event) if ln.startswith("UID:"))

    before = uid()
    moved = st.edit_item(out.id, {"dep_local": "2027-09-17T10:25", "arr_local": "2027-09-17T13:58"})
    assert moved.id == out.id and moved.item_key != out.item_key
    assert uid() == before == f"UID:tripbox-item-{out.id}@{ical.UID_DOMAIN}"
