# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import dataclasses
import datetime as dt
import hashlib
import inspect
import json

from helpers.airports import SAMPLE_AIRPORTS, airports
from helpers.sample import TRAVELLERS

from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.util import parse_iso
from tripbox.validate import checks, rebuild, rules
from tripbox.validate.keys import real_key, segment_key
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rebuild import rebuild_item
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import (
    MAX_SEGMENT,
    city_matches,
    localize,
    parse_local,
    validate_extraction,
)

BOOK = PassengerBook.from_yaml(TRAVELLERS)
EMAIL = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)
SRC = {"message_id": "<m1>"}


def rd():
    return airports()


def seg(**kw):
    base = dict(
        carrier_iata="DL",
        flight_number="1234",
        origin="MSP",
        destination="PHL",
        departure_local="2027-06-22T08:20",
        arrival_local="2027-06-22T11:55",
    )
    base.update(kw)
    return Segment(**base)


def flight(passengers, *segs, code="ABC123", status="confirmed"):
    return Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            Reservation(
                kind="flight",
                status=status,
                confirmation_code=code,
                provider="Delta Air Lines",
                passengers=passengers,
                segments=list(segs),
            )
        ],
    )


def test_naive_local_times_resolve_through_airport_zones():
    items = validate_extraction(
        flight(["Alex Sample"], seg()), refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC
    )
    assert len(items) == 1
    it = items[0]
    assert it.ok and it.classified.me_present
    p = it.payload
    assert p["dep_utc"] == "2027-06-22T13:20:00Z" and p["arr_utc"] == "2027-06-22T15:55:00Z"
    assert p["date_local"] == "2027-06-22" and p["duration_min"] == 155
    assert "from_id" not in p and "to_id" not in p
    assert "airline_id" not in p and "airline_icao" not in p
    assert it.key == "DL|1234|2027-06-22|MSP" and p["flight_code"] == "DL1234"


def test_an_airline_no_table_knows_raises_no_check():
    s = seg(carrier_iata="QQ", flight_number="9123")
    ex = flight(["Alex Sample"], s)
    ex.reservations[0].provider = "Delta Air Lines"
    (it,) = validate_extraction(ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC)
    assert it.problems == [] and it.classified.me_present
    assert it.payload["airline_name"] == "Delta Air Lines"
    assert "airline_id" not in it.payload and "airline_icao" not in it.payload


def test_late_evening_departure_keeps_local_date_and_positive_duration():
    s = seg(departure_local="2027-06-22T22:30", arrival_local="2027-06-23T02:30")
    it = validate_extraction(
        flight(["Alex Sample"], s), refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC
    )[0]
    assert it.payload["date_local"] == "2027-06-22"  # local, even though UTC is already the 13th
    assert it.payload["dep_utc"] == "2027-06-23T03:30:00Z"
    assert it.payload["duration_min"] == 180  # 03:30Z -> 06:30Z


def test_printed_offset_wins_over_airport_zone():
    s = seg(departure_offset="-07:00")
    it = validate_extraction(
        flight(["Alex Sample"], s), refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC
    )[0]
    assert it.payload["dep_utc"] == "2027-06-22T15:20:00Z"


def leg_of(s: Segment):
    (it,) = validate_extraction(
        flight(["Alex Sample"], s), refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC
    )
    return it


def test_a_departure_printed_as_a_day_alone_gets_no_time():
    """Whatever form the day is printed in, a departure with no time keeps the day alone: no
    instant, no length, the hard finding `departure`, and the key of its day."""
    day = "2027-06-22"
    when = parse_local(day)
    printed_forms = (day, when.strftime("%a %b %d %Y"), when.strftime("%b %d, %Y"))
    # a zone or an offset beside a day is still no time
    for printed in (*printed_forms, f"{day}Z", f"{day}-05:00", f"{day} -05:00"):
        it = leg_of(seg(departure_local=printed))
        p = it.payload
        assert [str(x) for x in it.problems] == ["departure: no departure time"], printed
        assert (p["dep_local"], p["date_local"]) == (day, day), printed
        assert p["dep_utc"] is None and p["duration_min"] is None, printed
        assert "T00:00" not in json.dumps(p), printed
        assert it.key == segment_key("DL", "1234", day, "MSP"), printed
        assert real_key("flight", p, airports=rd()) == it.key, printed
        assert p["arr_utc"] == "2027-06-22T15:55:00Z", printed  # the arrival was printed
        # rebuilt from what is stored, it is the same booking
        again = rebuild_item("flight", p, refdata=rd(), book=BOOK)
        assert again.payload == p and again.problems == it.problems and again.key == it.key


def test_a_flight_printed_as_leaving_at_midnight_keeps_its_time():
    """A time is told from what was printed, never from the time read: 00:00 is a time."""
    day = "2027-06-22"
    morning = leg_of(seg())
    twelve = parse_local(day).strftime("%Y-%m-%d %I:%M %p")  # 12:00 AM
    for printed in (f"{day}T00:00", f"{day} 00:00", f"{day}T00:00:00Z", twelve):
        it = leg_of(seg(departure_local=printed))
        p = it.payload
        assert it.ok, printed
        assert (p["dep_local"], p["date_local"]) == (f"{day}T00:00", day), printed
        gap = dt.timedelta(hours=8, minutes=20)  # the default leg leaves at 08:20
        assert parse_iso(p["dep_utc"]) == parse_iso(morning.payload["dep_utc"]) - gap, printed
        assert p["duration_min"] == morning.payload["duration_min"] + 8 * 60 + 20, printed
        assert it.key == morning.key, printed


def test_an_arrival_printed_as_a_day_alone_gets_no_time_and_raises_nothing():
    """A day alone for the arrival is no instant, and no order or length is worked out from it,
    whether it is the day of the departure or days after it."""
    day = "2027-06-22"
    later = (parse_local(day) + MAX_SEGMENT + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    timed = leg_of(seg())
    for printed in (day, later):
        it = leg_of(seg(arrival_local=printed))
        p = it.payload
        assert it.problems == [], printed
        assert p["arr_local"] == printed and p["arr_utc"] is None, printed
        assert p["duration_min"] is None and "T00:00" not in json.dumps(p), printed
        for field in ("dep_local", "dep_utc", "date_local"):
            assert p[field] == timed.payload[field], (printed, field)
        assert it.key == timed.key, printed


def test_each_problem_is_named_by_its_check():
    bad = validate_extraction(
        flight(["Alex Sample"], seg(origin="XXX")),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert checks.codes(bad) == ("origin", "departure")  # no airport, so no zone for its time
    past = validate_extraction(
        flight(
            ["Alex Sample"],
            seg(departure_local="2026-09-01T08:20", arrival_local="2026-09-01T11:55"),
        ),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert checks.codes(past) == ("date_past",)
    nonum = validate_extraction(
        flight(["Alex Sample"], seg(flight_number="12A4")),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert checks.codes(nonum) == ("flight_number",)
    backwards = validate_extraction(
        flight(["Alex Sample"], seg(arrival_local="2027-06-22T07:00")),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert checks.codes(backwards) == ("arrival_before_departure",)


def test_who_a_flight_names_is_the_owner_a_companion_someone_else_or_nobody():
    trips_only = validate_extraction(
        flight(["Casey Sample", "Pat Example"], seg()),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert checks.codes(trips_only) == () and not trips_only.classified.me_present
    assert trips_only.classified.others == ["Casey Sample", "Pat Example"]
    mixed = validate_extraction(
        flight(["SAMPLE/ALEX QUINN", "SAM SAMPLE", "Casey Sample"], seg()),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert checks.codes(mixed) == () and mixed.classified.me_present
    assert mixed.classified.guests == ["Sam Sample"]
    assert mixed.classified.others == ["Casey Sample"]
    nobody = validate_extraction(
        flight([], seg()), refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC
    )[0]
    assert checks.codes(nobody) == ("owner_unknown",)


def test_low_model_confidence_is_a_finding_of_the_model_alone():
    ex = flight(["Alex Sample"], seg())
    ex.confidence = 0.5
    it = validate_extraction(
        ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC, parser="haiku"
    )[0]
    assert checks.codes(it) == ("low_confidence",)
    ok = validate_extraction(
        ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC, parser="kitinerary"
    )[0]
    assert checks.codes(ok) == ()


def stay_and_car() -> Extraction:
    return Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            Reservation(
                kind="lodging",
                provider="Hilton",
                property_name="Hilton Example Central",
                confirmation_code="H123",
                check_in="2027-06-22",
                check_out="2027-06-25",
                passengers=["Casey Sample"],
                price=612.3,
                currency="USD",
            ),
            Reservation(
                kind="car",
                provider="Hertz",
                pickup_time="2027-06-22T13:00",
                dropoff_time="2027-06-25T10:00",
                pickup_location="Philadelphia Intl Airport",
                passengers=["Alex Sample"],
            ),
        ],
    )


def test_lodging_and_car_items_and_keys():
    items = validate_extraction(
        stay_and_car(), refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC
    )
    assert [i.kind for i in items] == ["lodging", "car"] and all(i.ok for i in items)
    assert items[0].key == "lodging|H123" == real_key("lodging", items[0].payload, airports=rd())
    assert (
        items[1].key
        == "car|hertz|2027-06-22|philadelphiaintlairport"
        == real_key("car", items[1].payload, airports=rd())
    )
    bad = Reservation(
        kind="lodging", property_name="X", check_in="2027-06-25", check_out="2027-06-22"
    )
    out = validate_extraction(
        Extraction(document_type="booking", reservations=[bad]),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert checks.codes(out) == ("check_out",)


def test_bookings_that_cannot_be_keyed_get_draft_keys_in_document_order():
    from_a_city = flight(["Alex Sample"], seg(origin="PHILADELPHIA", destination="MINNEAPOLIS"))
    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            *from_a_city.reservations,
            Reservation(kind="lodging", confirmation_code="H123"),
            Reservation(kind="lodging"),
        ],
    )
    first = validate_extraction(ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC)
    # a direct call names no message bytes: the hash of its Message-ID stands in
    digest = hashlib.sha256(SRC["message_id"].encode()).hexdigest()
    assert [i.key for i in first] == [
        f"draft|{digest[:16]}|0",
        "lodging|H123",
        f"draft|{digest[:16]}|2",
    ]
    assert all(i.payload["message_hash"] == digest for i in first)
    again = validate_extraction(ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC)
    assert [i.key for i in again] == [i.key for i in first]


def test_helpers():
    assert parse_local("2027-06-22") == dt.datetime(2027, 6, 22, 0, 0)
    assert parse_local("2027-06-22 08:20") == dt.datetime(2027, 6, 22, 8, 20)
    assert parse_local("Tue Oct 26 2027 9:00 AM") == dt.datetime(2027, 10, 26, 9, 0)
    assert parse_local("Thu, Oct 28, 2027 5:30 PM") == dt.datetime(2027, 10, 28, 17, 30)
    assert parse_local("garbage") is None
    assert localize(dt.datetime(2027, 6, 22, 8, 5), None, None, None) is None
    assert localize(
        dt.datetime(2027, 6, 22, 8, 5), "+00:00", None, None
    ).utcoffset() == dt.timedelta(0)
    assert segment_key("dl", "0012", "2027-06-22", "msp") == "DL|12|2027-06-22|MSP"


def test_carrier_code_comes_from_the_printed_airline_name_when_missing():
    from tripbox.validate.rules import carrier_from_name

    assert carrier_from_name("JetBlue") == "B6" and carrier_from_name("Southwest Airlines") == "WN"
    assert (
        carrier_from_name("Air France-KLM") == "AF" and carrier_from_name("Delta Air Lines") == "DL"
    )
    assert carrier_from_name("Acme Air") is None and carrier_from_name(None) is None
    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            Reservation(
                kind="flight",
                provider="Southwest Airlines",
                confirmation_code="VYRUXQ",
                passengers=["Casey Sample"],
                segments=[
                    Segment(
                        flight_number="9571",
                        origin="MSP",
                        destination="DEN",
                        departure_local="2027-11-09T07:00",
                        arrival_local="2027-11-09T08:15",
                    )
                ],
            )
        ],
    )
    items = validate_extraction(
        ex, refdata=rd(), book=BOOK, email_date=EMAIL
    )
    assert items[0].ok and items[0].key == "WN|9571|2027-11-09|MSP"
    assert items[0].payload["carrier"] == "WN"
    assert items[0].payload["airline_name"] == "Southwest Airlines"


def test_parse_local_handles_single_digit_hours_and_impossible_dates():
    assert parse_local("2027-06-22 6:00 AM") == dt.datetime(2027, 6, 22, 6, 0)
    assert parse_local("2027-06-22 9:30 PM") == dt.datetime(2027, 6, 22, 21, 30)
    assert parse_local("2027-06-22T8:05") == dt.datetime(2027, 6, 22, 8, 5)
    assert parse_local("2027-06-22T08:20:00Z") == dt.datetime(2027, 6, 22, 8, 20)
    assert parse_local("2027-06-22T08:20:00-05:00") == dt.datetime(2027, 6, 22, 8, 20)
    assert parse_local("2027-02-29") is None
    assert parse_local("2027-13-01T08:20") is None
    assert parse_local("2027-06-22 garbage") is None


def test_a_year_at_either_end_of_the_calendar_is_no_usable_time():
    """A year no booking has (out of 1900-2200) reads as no date, so a leg whose instant would
    leave the calendar is filed with the hard check `departure` instead of raising."""
    for printed in ("0001-01-01T00:00", "9999-12-31T23:00", "Dec 31, 9999", "1899-12-31T08:00"):
        assert parse_local(printed) is None, printed
        assert rules.day_alone(printed) is None, printed
    tokyo = leg_of(seg(departure_tz="Asia/Tokyo", departure_local="0001-01-01T00:00"))
    lax = leg_of(seg(departure_tz="America/Los_Angeles", departure_local="9999-12-31T23:00"))
    for it in (tokyo, lax):
        assert [p.code for p in it.problems] == ["departure"]
        assert it.payload["dep_utc"] is None
        again = rebuild_item("flight", it.payload, refdata=rd(), book=BOOK)
        assert [p.code for p in again.problems] == ["departure"]
    late_arrival = leg_of(seg(arrival_local="9999-12-31T23:00"))
    assert late_arrival.payload["arr_utc"] is None
    assert parse_local("2200-12-31T23:59") is not None
    assert parse_local("1900-01-01T00:00") is not None


def test_passengers_are_classified_per_segment():
    """One PNR, Alex on the outbound only and Sam on the return only (kitinerary lists the
    travellers of each leg): the return is Sam's flight, not Alex's."""
    ex = flight(
        ["Alex Sample", "Sam Sample"],
        seg(passengers=["Alex Sample"]),
        seg(
            flight_number="200",
            origin="PHL",
            destination="MSP",
            departure_local="2027-06-26T16:45",
            arrival_local="2027-06-26T18:50",
            passengers=["Sam Sample"],
        ),
    )
    out, back = validate_extraction(ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC)
    assert out.classified.me_present and out.ok and out.classified.guests == []
    assert not back.classified.me_present and back.ok
    assert back.classified.guests == ["Sam Sample"] and back.passengers == ["Sam Sample"]
    assert out.classified is not back.classified


def test_segment_status_overrides_the_reservation_status():
    ex = flight(
        ["Alex Sample"],
        seg(status="confirmed"),
        seg(
            flight_number="200",
            departure_local="2027-06-26T16:45",
            arrival_local="2027-06-26T18:50",
            status="cancelled",
        ),
    )
    items = validate_extraction(ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC)
    assert [i.status for i in items] == ["confirmed", "cancelled"]


# A receipt that prints only city names next to each flight and the airport codes only as pairs
# in a baggage table further down; a model reading it may return the cities.
RECEIPT_OUT = dict(departure_local="2027-10-05T09:10", arrival_local="2027-10-05T11:15")
RECEIPT_TEXT = """Receipt
Flight DL 1234
PHILADELPHIA 09:10 AM  MINNEAPOLIS 11:15 AM
...
Baggage
Tue 5 Oct 2027  PHL-MSP
Fri 8 Oct 2027  MSP-PHL
"""


def test_city_names_resolve_through_a_code_pair_printed_elsewhere_in_the_email():
    out = seg(origin="PHILADELPHIA", destination="MINNEAPOLIS", **RECEIPT_OUT)
    back = seg(
        origin="MINNEAPOLIS",
        destination="PHILADELPHIA",
        flight_number="9318",
        departure_local="2027-10-08T15:30",
        arrival_local="2027-10-08T19:05",
    )
    items = validate_extraction(
        flight(["Lee Example"], out, back),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
        text=RECEIPT_TEXT,
    )
    assert [(i.payload["origin"], i.payload["destination"]) for i in items] == [
        ("PHL", "MSP"),
        ("MSP", "PHL"),
    ]
    assert all(i.ok for i in items)
    first = items[0]
    assert first.key == "DL|1234|2027-10-05|PHL" and first.payload["origin_city"] == "Philadelphia"
    assert first.payload["dep_utc"] == "2027-10-05T13:10:00Z"  # Philadelphia's zone, now known
    assert first.payload["airports_from_text"] == "PHL-MSP"


def test_a_printed_code_pair_is_used_only_when_its_cities_match_the_leg():
    it = validate_extraction(
        flight(
            ["Lee Example"], seg(origin="PHILADELPHIA", destination="MINNEAPOLIS", **RECEIPT_OUT)
        ),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
        text="Tue 5 Oct 2027  DEN-MSP",
    )[0]
    assert "origin" in checks.codes(it) and it.payload["origin"] == "PHILADELPHIA"
    assert "airports_from_text" not in it.payload


def test_two_printed_pairs_that_fit_the_same_city_leave_the_airport_unread():
    chicago = {
        code: {
            "iata": code,
            "icao": f"K{code}",
            "name": name,
            "city": "Chicago",
            "country": "US",
            "tz": "America/Chicago",
        }
        for code, name in (("ORD", "Chicago O'Hare International"), ("MDW", "Chicago Midway"))
    }
    it = validate_extraction(
        flight(["Lee Example"], seg(origin="CHICAGO", destination="MINNEAPOLIS")),
        refdata=Airports({**SAMPLE_AIRPORTS, **chicago}),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
        text="ORD-MSP  MDW-MSP",
    )[0]
    assert "origin" in checks.codes(it) and it.payload["origin"] == "CHICAGO"


def test_city_matching_needs_the_whole_municipality_name():
    def ap(municipality):
        return {"municipality": municipality}

    assert city_matches("KANSAS CITY", ap("Kansas City"))
    assert city_matches("CHICAGO-OHARE", ap("Chicago"))
    assert city_matches("ANCHORAGE", ap("Anchorage, Alaska"))
    assert city_matches("CANCUN", ap("Cancún"))
    assert city_matches("ST. PAUL", ap("St Paul"))
    assert not city_matches("KANSAS", ap("Kansas City"))
    assert not city_matches("SAN", ap("San Francisco"))
    assert not city_matches("MINNEAPOLIS", ap(None))
    assert not city_matches("MINNEAPOLIS", None)


def test_an_unresolved_airport_is_named_as_the_reason_the_time_has_no_zone():
    it = validate_extraction(
        flight(["Lee Example"], seg(origin="PHILADELPHIA", destination="MINNEAPOLIS")),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    dep = [str(p) for p in it.problems if p.code == "departure"]
    assert dep == ["departure: time zone unknown until the origin airport is fixed (08:20)"]
    garbled = validate_extraction(
        flight(["Lee Example"], seg(departure_local="soon")),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert [str(p) for p in garbled.problems if p.code == "departure"] == [
        "departure: no usable departure time ('soon')"
    ]


def test_a_journey_pair_never_resolves_one_leg_of_a_connection():
    """Baggage tables can be per journey: PHL-MSP printed for Philadelphia -> Denver -> Minneapolis
    must not turn the Philadelphia -> Denver leg into a Philadelphia -> Minneapolis flight."""
    it = validate_extraction(
        flight(["Lee Example"], seg(origin="PHILADELPHIA", destination="DENVER", **RECEIPT_OUT)),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
        text="Tue 5 Oct 2027  PHL-MSP",
    )[0]
    assert "destination" in checks.codes(it) and it.payload["destination"] == "DENVER"


def test_one_printed_code_and_one_city_resolve_only_through_a_pair_with_that_code():
    ok = validate_extraction(
        flight(["Lee Example"], seg(origin="PHL", destination="MINNEAPOLIS")),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
        text="DEN-MSP ... PHL-MSP",
    )[0]
    assert ok.ok and (ok.payload["origin"], ok.payload["destination"]) == ("PHL", "MSP")
    other = validate_extraction(
        flight(["Lee Example"], seg(origin="DEN", destination="PHILADELPHIA")),
        refdata=rd(),
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
        text="MSP-PHL",
    )[0]
    assert "destination" in checks.codes(other) and other.payload["destination"] == "PHILADELPHIA"


def test_validation_does_not_depend_on_a_flight_log():
    """What is found in a booking is the same whether or not a flight log is kept: the validator
    is not told, and names no place a booking goes. The owner's flight from a known sender has no
    finding; from a sender nobody vouched for, every flight carries the sender finding, whoever
    it names, and nothing but a flight does."""
    for fn in (rules.validate_extraction, rules.validate_flight, rebuild.rebuild_item):
        assert "airtrail" not in inspect.signature(fn).parameters, fn.__name__
    assert "route" not in {f.name for f in dataclasses.fields(rules.Item)}
    owners = flight(["Alex Sample"], seg(flight_number="9123"), code="QXR987")
    (mine,) = validate_extraction(owners, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC)
    assert checks.codes(mine) == () and mine.classified.me_present
    src = {**SRC, "from": "deals@unlisted.example"}
    guests = flight(["Sam Sample"], seg(flight_number="9123"), code="QXR987")
    nobody = flight([], seg(flight_number="9123"), code="QXR987")
    for ex, found in (
        (owners, ("unverified_sender",)),
        (guests, ("unverified_sender",)),
        (nobody, ("unverified_sender", "owner_unknown")),
    ):
        (it,) = validate_extraction(
            ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=src, trusted_sender=False
        )
        assert checks.codes(it) == found
        assert "deals@unlisted.example" in str(it.problems[-1])
    others = validate_extraction(
        stay_and_car(), refdata=rd(), book=BOOK, email_date=EMAIL, source=src, trusted_sender=False
    )
    assert [checks.codes(it) for it in others] == [(), ()]


def test_a_known_airport_without_a_zone_says_the_zone_is_missing():
    zoneless = Airports({**SAMPLE_AIRPORTS, "MSP": {**SAMPLE_AIRPORTS["MSP"], "tz": ""}})
    it = validate_extraction(
        flight(["Alex Sample"], seg(flight_number="9123"), code="QXR987"),
        refdata=zoneless,
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert [str(p) for p in it.problems if p.code == "departure"] == [
        "departure: no time zone known for MSP and the email prints none (08:20)"
    ]
    assert checks.codes(it) == ("departure",) and it.payload["dep_utc"] is None
    # an offset the email prints is a zone: the same flight then needs no look
    printed = validate_extraction(
        flight(["Alex Sample"], seg(flight_number="9123", departure_offset="-05:00")),
        refdata=zoneless,
        book=BOOK,
        email_date=EMAIL,
        source=SRC,
    )[0]
    assert printed.ok and printed.payload["dep_utc"] == "2027-06-22T13:20:00Z"


def test_a_flight_is_named_from_the_email_then_the_shipped_list():
    def named(provider):
        ex = flight(["Alex Sample"], seg(flight_number="9123"))
        ex.reservations[0].provider = provider
        (it,) = validate_extraction(ex, refdata=rd(), book=BOOK, email_date=EMAIL, source=SRC)
        return it.payload["airline_name"]

    assert named(None) == "Delta Air Lines"
    assert named("Example Air") == "Example Air"
