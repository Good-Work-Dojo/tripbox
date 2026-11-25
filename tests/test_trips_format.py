# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
import re
import zoneinfo
from pathlib import Path

import pytest
from test_planner import BOOK, items_for
from test_trips_store import car_item

from tripbox.ledger import Ledger
from tripbox.trips import format as fmt
from tripbox.trips.store import TripStore

HOME = ("MSP",)
GOLDEN = (Path(__file__).parent / "data" / "trip_text_golden.txt").read_text(encoding="utf-8")


def sample_trip(trip_id=10):
    led = Ledger.memory()
    st = TripStore(led, HOME)
    led.trips.insert(
        title="Philadelphia",
        place="Philadelphia",
        start_date="2027-09-16",
        end_date="2027-09-16",
        id=trip_id,
    )
    trip = st.get(trip_id)
    family = ["Alex Sample", "Sam Sample", "Robin Sample", "Jamie Sample"]
    out = items_for(
        family,
        dep="2027-09-16T10:25",
        arr="2027-09-16T13:58",
        pnr="QTWZSV",
        number="9304",
        carrier="SY",
        provider="Sun Country Airlines",
        seat_by=dict(zip(family, ("8A", "8B", "8C", "8D"), strict=True)),
    )[0]
    st.upsert_item(trip, out)
    car = car_item(
        pickup="200 Example Avenue, Philadelphia PA 00000 US",
        start="2027-09-16T14:45",
        end="2027-09-21T13:15",
        code="1234567890",
    )
    car.payload["dropoff_location"] = "200 Example Avenue, Philadelphia PA 00000 US"
    st.upsert_item(st.get(trip_id), car)
    back = items_for(
        family,
        origin="PHL",
        dest="MSP",
        dep="2027-09-21T16:45",
        arr="2027-09-21T18:50",
        pnr="Q9TW8Z",
        number="9246",
        seat_by=dict(zip(family, ("21A", "21B", "21C", "21D"), strict=True)),
    )[0]
    st.upsert_item(st.get(trip_id), back)
    return st, st.get(trip_id)


def text(st, trip, **kw):
    return fmt.trip_text(trip, st.items(trip.id), book=BOOK, home_airports=HOME, **kw)


def test_trip_text_golden():
    st, trip = sample_trip()
    assert text(st, trip) == GOLDEN.rstrip("\n")


def test_toggles_drop_conf_and_names():
    st, trip = sample_trip()
    assert "conf" not in text(st, trip, include_conf=False)
    no_names = text(st, trip, include_names=False)
    assert not re.search(r"\bAlex\b", no_names) and "seats 8A" in no_names


def test_selection_renders_only_the_chosen_items():
    st, trip = sample_trip()
    back = st.item_by_key("DL|9246|2027-09-21|PHL")
    out = text(st, trip, selected={back.id})
    assert out.startswith(
        "Philadelphia · Thu Sep 16 – Tue Sep 21, 2027\n\nTue Sep 21\n✈️ Delta 9246"
    )
    assert "Sun Country" not in out and "Example Rent A Car" not in out


def test_cancelled_items_are_hidden_unless_selected():
    st, trip = sample_trip()
    st.cancel("DL|9246|2027-09-21|PHL")
    out = text(st, trip)
    assert "Delta 9246" not in out and "Tue Sep 21\n🚗 Example Rent A Car · drop off" in out
    back = st.item_by_key("DL|9246|2027-09-21|PHL")
    assert "❌ CANCELLED ✈️ Delta 9246" in text(st, trip, selected={back.id})


def test_overnight_arrival_appends_the_date():
    st, trip = sample_trip()
    red_eye = items_for(
        ["Alex Sample"],
        dep="2027-09-17T22:40",
        arr="2027-09-18T02:15",
        pnr="NIGHT1",
        number="9077",
    )[0]
    st.upsert_item(st.get(trip.id), red_eye)
    late = "✈️ Delta 9077 · MSP 10:40 PM CDT → PHL 2:15 AM EDT, Sat Sep 18"
    assert late in text(st, st.get(trip.id))


def test_a_zone_with_no_letters_reads_as_an_offset_from_utc():
    """Where a zone's own abbreviation has no letters ('+04', '-03', '+0530'), a time is shown
    with its offset from UTC instead: whole hours alone, minutes after a colon, and none for UTC
    itself. A zone with letters keeps them."""
    when = "2027-09-16T10:25"
    at = dt.datetime.fromisoformat(when)
    assert fmt.fmt_time(when, "Etc/GMT-4") == "10:25 AM UTC+4"
    assert fmt.fmt_time(when, "Etc/GMT+3") == "10:25 AM UTC-3"
    assert fmt.fmt_time(when, "America/Los_Angeles") == "10:25 AM PDT"
    names = sorted(zoneinfo.available_timezones())
    half = [n for n in names if at.replace(tzinfo=zoneinfo.ZoneInfo(n)).strftime("%Z") == "+0530"]
    assert half and {fmt.fmt_time(when, n) for n in half} == {"10:25 AM UTC+5:30"}
    for name in names:
        local = at.replace(tzinfo=zoneinfo.ZoneInfo(name))
        shown = fmt.fmt_time(when, name)
        assert shown is not None and shown.startswith("10:25 AM "), name
        zone = shown.removeprefix("10:25 AM ")
        if re.search(r"[A-Za-z]", local.strftime("%Z")):
            assert zone == local.strftime("%Z"), name
            continue
        offset = re.fullmatch(r"UTC(?:([+-])(\d{1,2})(?::(\d{2}))?)?", zone)
        assert offset, (name, zone)
        sign, hours, minutes = offset.groups()
        seconds = (int(hours or 0) * 3600 + int(minutes or 0) * 60) * (-1 if sign == "-" else 1)
        assert dt.timedelta(seconds=seconds) == local.utcoffset(), (name, zone)


def test_date_range_formats():
    assert fmt.date_range("2027-09-28", "2027-10-03") == "Tue Sep 28 – Sun Oct 3, 2027"
    assert fmt.date_range("2027-12-28", "2028-01-04") == "Tue Dec 28, 2027 – Tue Jan 4, 2028"
    assert fmt.date_range("2027-09-28", "2027-09-28") == "Tue Sep 28, 2027"
    assert fmt.short_range("2027-09-28", "2027-10-03", dt.date(2027, 2, 1)) == "Sep 28 – Oct 3"
    assert (
        fmt.short_range("2028-09-28", "2028-10-03", dt.date(2027, 2, 1)) == "Sep 28 – Oct 3, 2028"
    )


def test_date_only_hotel_shows_check_in_after_the_flight_and_check_out_on_departure_day():
    st, trip = sample_trip()
    hotel = items_for(
        ["Alex Sample", "Sam Sample"],
        kind="lodging",
        dep="2027-09-16",
        arr="2027-09-21",
        pnr="HOTEL1",
    )[0]
    hotel.payload["address"] = "100 Example Street, Philadelphia, PA 00000"
    st.upsert_item(st.get(trip.id), hotel)
    out = text(st, st.get(trip.id))
    first_day = out.split("Thu Sep 16\n", 1)[1].split("\n\nTue Sep 21", 1)[0]
    assert first_day.index("Sun Country 9304") < first_day.index("🏨 Hilton Example · check-in")
    assert re.search(
        r"🏨 Hilton Example · check-in\n   100 Example Street, Philadelphia, PA 00000"
        r"\n   conf HOTEL1 · Alex, Sam\b",
        first_day,
    )
    last_day = out.split("Tue Sep 21\n", 1)[1]
    assert "🏨 Hilton Example · check-out" in last_day and "100 Example Street" not in last_day


def test_car_in_a_second_city_uses_that_city_zone():
    st, trip = sample_trip()
    lax = items_for(
        ["Alex Sample"],
        origin="PHL",
        dest="LAX",
        dep="2027-09-18T09:00",
        arr="2027-09-18T12:10",
        pnr="LAX001",
        number="9005",
    )[0]
    lax.payload.update(destination_tz="America/Los_Angeles", destination_city="Los Angeles")
    st.upsert_item(st.get(trip.id), lax)
    st.upsert_item(
        st.get(trip.id),
        car_item(pickup="LAX", start="2027-09-18T15:00", end="2027-09-20T10:00", code="LAXCAR"),
    )
    out = text(st, st.get(trip.id))
    assert "🚗 Example Rent A Car · pick up 3:00 PM PDT" in out
    assert "🚗 Example Rent A Car · drop off 10:00 AM PDT" in out


def test_notes_paragraph_and_toggle():
    st, trip = sample_trip()
    st.set_notes(trip.id, "Gate code 1234\n\nParking: level 2")
    out = text(st, st.get(trip.id))
    assert out.startswith(
        "Philadelphia · Thu Sep 16 – Tue Sep 21, 2027\n"
        "\nNotes: Gate code 1234\n\nParking: level 2\n\nThu Sep 16\n"
    )
    assert "Notes:" not in text(st, st.get(trip.id), include_notes=False)


def test_upcoming_and_list_texts():
    st, trip = sample_trip()
    today = dt.date(2027, 9, 1)
    up = fmt.upcoming_text(st, today, book=BOOK, home_airports=HOME)
    assert up == GOLDEN.rstrip("\n")
    assert (
        fmt.upcoming_text(st, dt.date(2027, 10, 1), book=BOOK, home_airports=HOME)
        == "Nothing upcoming."
    )
    assert fmt.trip_list_text(st, today) == "#10 Philadelphia · Sep 16 – Sep 21 · ✈️2 🚗1"
    assert fmt.trip_list_text(st, today, link_base="https://trips.example.com/tripbox") == (
        "#10 Philadelphia · Sep 16 – Sep 21 · ✈️2 🚗1 https://trips.example.com/tripbox/trips/10"
    )
    assert (
        fmt.trip_list_text(st, dt.date(2027, 10, 1))
        == "Nothing upcoming.\n\nPast:\n#10 Philadelphia · Sep 16 – Sep 21 · ✈️2 🚗1"
    )
    assert fmt.trip_list_text(TripStore(Ledger.memory(), HOME), today) == "No trips yet."


def test_bare_first_names_from_greetings_are_not_travellers():
    # payload["passengers"] keeps every printed name, including "Alex" from "Hi Alex,"
    assert fmt.display_names(["Alex Sample", "Sam Sample", "Alex"], BOOK) == [
        "Alex",
        "Sam",
    ]
    assert fmt.display_names(["SAMPLE/ALEX Q", "Max Smith", "Alex Quinn Sample"], BOOK) == [
        "Alex",
        "Max Smith",
    ]


def test_a_name_shown_keeps_its_accents_and_apostrophes():
    # matching folds them ('OBRIEN/RENEE' is the same person); showing keeps them
    assert fmt.display_names(["O'BRIEN/RENÉE MS", "OBRIEN/RENEE", "Zoë Example"], BOOK) == [
        "Renée O'Brien",
        "Zoë Example",
    ]


def test_name_variants_collapse_to_one_person_and_one_seat():
    st, trip = sample_trip()
    ti = st.item_by_key("DL|9246|2027-09-21|PHL")
    # a second receipt printed the same people differently and carried the same seats
    st.led.trip_items.update(
        ti.id,
        passengers=["SAMPLE/SAM", "SAM TAYLOR SAMPLE", "Alex Sample", "SAMPLE/ALEX Q"],
        payload={
            **ti.payload,
            "seat_by_passenger": {
                "SAMPLE/SAM": "14C",
                "SAM TAYLOR SAMPLE": "14C",
                "Alex Sample": "14A",
                "SAMPLE/ALEX Q": "14A",
            },
        },
    )
    out = text(st, trip, selected={ti.id})
    assert "conf Q9TW8Z · Alex, Sam · seats 14A, 14C" in out


def test_null_airport_codes_print_as_question_marks():
    st, trip = sample_trip()
    ti = st.item_by_key("DL|9246|2027-09-21|PHL")
    st.led.trip_items.update(ti.id, payload={**ti.payload, "origin": None, "destination": None})
    assert "✈️ Delta 9246 · ? 4:45 PM EDT → ? 6:50 PM CDT" in text(st, trip, selected={ti.id})


def test_zone_before_the_first_flight_is_where_the_trip_starts():
    """A stay in the home city the night before the first flight reads CDT; the stay checked into
    after landing reads EDT."""
    st, trip = sample_trip()
    night_before = items_for(
        ["Alex Sample"],
        kind="lodging",
        dep="2027-09-15T16:00",
        arr="2027-09-16T08:00",
        pnr="HOMEHT",
    )[0]
    night_before.payload["property_name"] = "Example Inn Minneapolis"
    night_before.payload["check_in"], night_before.payload["check_out"] = (
        "2027-09-15T16:00",
        "2027-09-16T08:00",
    )
    st.upsert_item(st.get(trip.id), night_before)
    city_hotel = items_for(
        ["Alex Sample"],
        kind="lodging",
        dep="2027-09-16T15:30",
        arr="2027-09-21T11:00",
        pnr="CITYHT",
    )[0]
    city_hotel.payload["property_name"] = "Hilton Example"
    city_hotel.payload["check_in"], city_hotel.payload["check_out"] = (
        "2027-09-16T15:30",
        "2027-09-21T11:00",
    )
    st.upsert_item(st.get(trip.id), city_hotel)
    out = text(st, st.get(trip.id))
    assert "Example Inn Minneapolis · check-in 4:00 PM CDT" in out
    assert "Hilton Example · check-in 3:30 PM EDT" in out


def test_a_date_only_check_out_comes_before_the_flight_home():
    st, trip = sample_trip()
    hotel = items_for(
        ["Alex Sample"], kind="lodging", dep="2027-09-16", arr="2027-09-21", pnr="HOTEL1"
    )[0]
    hotel.payload["property_name"] = "Hilton Example"
    st.upsert_item(st.get(trip.id), hotel)
    out = text(st, st.get(trip.id))
    last_day = out.split("Tue Sep 21")[-1]
    assert last_day.index("check-out") < last_day.index("Delta 9246")


@pytest.mark.parametrize(
    ("carrier", "number", "url"),
    [
        # FlightAware names a flight by its airline's ICAO code: SY 9304 is SCX9304
        ("SY", "9304", "https://flightaware.com/live/flight/SCX9304"),
        ("sy", " 0440 ", "https://flightaware.com/live/flight/SCX440"),
        ("DL", 692, "https://flightaware.com/live/flight/DAL692"),
        # no ICAO code known, or no number it can be asked by: no link
        ("ZZ", "9304", None),
        ("SY", "9304A", None),
        ("SY", "９３０４", None),
        ("SY", "93041", None),
        ("SY", "", None),
        (None, "9304", None),
    ],
)
def test_a_flight_is_tracked_by_its_airlines_icao_code(carrier, number, url):
    assert fmt.track_url({"carrier": carrier, "flight_number": number}) == url


def test_a_number_past_what_int_reads_is_no_link_and_no_error():
    """int() refuses a string of over 4300 digits; a typed number that long shows no link."""
    assert fmt.track_url({"carrier": "SY", "flight_number": "9" * 5000}) is None


def test_the_airline_list_in_the_config_directory_gives_a_tracker_code(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(tmp_path))
    (tmp_path / "airlines.yaml").write_text("- iata: ZZ\n  name: Example Air\n  icao: exa\n")
    url = fmt.track_url({"carrier": "ZZ", "flight_number": "12"})
    assert url == "https://flightaware.com/live/flight/EXA12"


HOUR = dt.timedelta(hours=1)
DEP = dt.datetime(2027, 9, 16, 15, 25, tzinfo=dt.UTC)
ARR = dt.datetime(2027, 9, 16, 17, 58, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("at", "shown"),
    [
        (DEP - fmt.TRACK_FROM - HOUR, False),  # over a day before it leaves
        (DEP - fmt.TRACK_FROM, True),
        (DEP - 2 * HOUR, True),
        (DEP + HOUR, True),  # in the air
        (ARR + fmt.TRACK_UNTIL, True),
        (ARR + fmt.TRACK_UNTIL + HOUR, False),  # hours after it landed
    ],
)
def test_a_flight_is_tracked_on_its_card_from_a_day_before_until_after_it_lands(at, shown):
    p = {"dep_utc": DEP.isoformat(), "arr_utc": ARR.isoformat().replace("+00:00", "Z")}
    assert fmt.tracking_now(p, at) is shown


def test_a_flight_with_no_departure_instant_or_arrival_has_its_own_tracking_time():
    # no departure instant (no time, or a day alone): never
    assert not fmt.tracking_now({}, DEP)
    assert not fmt.tracking_now({"dep_utc": "soon", "arr_utc": ARR.isoformat()}, DEP)
    # no arrival, or one before the departure: from the departure on
    for arr in (None, "later", (DEP - 3 * HOUR).isoformat()):
        p = {"dep_utc": DEP.isoformat(), "arr_utc": arr}
        assert fmt.tracking_now(p, DEP + fmt.TRACK_UNTIL), arr
        assert not fmt.tracking_now(p, DEP + fmt.TRACK_UNTIL + HOUR), arr
