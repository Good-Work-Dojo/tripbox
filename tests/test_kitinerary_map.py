# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import shutil
from pathlib import Path

import pytest

from tripbox.parse.kitinerary import extract_file, map_jsonld, split_datetime
from tripbox.parse.models import is_complete

DATA = Path(__file__).parent / "data"


def flight_obj(
    passenger, seat, number="1234", dep="2027-06-22T08:20:00-05:00", status=None, code="ABC123"
):
    o = {
        "@context": "http://schema.org",
        "@type": "FlightReservation",
        "reservationNumber": code,
        "underName": {"@type": "Person", "name": passenger},
        "airplaneSeat": seat,
        "reservationFor": {
            "@type": "Flight",
            "flightNumber": number,
            "airline": {"@type": "Airline", "iataCode": "DL", "name": "Delta Air Lines"},
            "departureAirport": {
                "@type": "Airport",
                "iataCode": "MSP",
                "name": "Minneapolis-St Paul",
            },
            "arrivalAirport": {
                "@type": "Airport",
                "iataCode": "PHL",
                "name": "Philadelphia Intl",
            },
            "departureTime": dep,
            "arrivalTime": {
                "@type": "QDateTime",
                "@value": "2027-06-22T11:55:00-04:00",
                "timezone": "America/New_York",
            },
        },
    }
    if status:
        o["reservationStatus"] = status
    return o


def test_split_datetime_shapes():
    assert split_datetime("2027-06-22T08:20:00-05:00") == ("2027-06-22T08:20", "-05:00", None)
    assert split_datetime(
        {"@type": "QDateTime", "@value": "2027-06-22T11:55:00-0400", "timezone": "America/New_York"}
    ) == ("2027-06-22T11:55", "-04:00", "America/New_York")
    assert split_datetime("2027-06-22T08:20:00") == ("2027-06-22T08:20", None, None)
    assert split_datetime("2027-06-22T08:20Z") == ("2027-06-22T08:20", "+00:00", None)
    assert split_datetime(None) == (None, None, None)


def test_per_passenger_objects_merge_into_one_flight_reservation():
    objs = [
        flight_obj("ALEX SAMPLE", "14A"),
        flight_obj("SAM SAMPLE", "14B"),
        flight_obj("ALEX SAMPLE", "22C", number="9873", dep="2027-06-26T16:45:00-04:00"),
    ]
    ex = map_jsonld(objs)
    assert ex.document_type == "booking" and len(ex.reservations) == 1
    res = ex.reservations[0]
    assert (
        res.kind == "flight"
        and res.confirmation_code == "ABC123"
        and res.provider == "Delta Air Lines"
    )
    assert res.passengers == ["ALEX SAMPLE", "SAM SAMPLE"]
    assert len(res.segments) == 2
    s0 = res.segments[0]
    assert (s0.carrier_iata, s0.flight_number, s0.origin, s0.destination) == (
        "DL",
        "1234",
        "MSP",
        "PHL",
    )
    assert s0.departure_local == "2027-06-22T08:20" and s0.departure_offset == "-05:00"
    assert s0.arrival_local == "2027-06-22T11:55" and s0.arrival_tz == "America/New_York"
    assert s0.seats == ["14A", "14B"] and s0.seat_by_passenger == {
        "ALEX SAMPLE": "14A",
        "SAM SAMPLE": "14B",
    }
    assert is_complete(res)


def test_cancelled_status_marks_document_as_cancellation():
    ex = map_jsonld([flight_obj("X", None, status="http://schema.org/ReservationCancelled")])
    assert ex.document_type == "cancellation" and ex.reservations[0].status == "cancelled"


def test_lodging_car_train_mapping():
    objs = [
        {
            "@type": "LodgingReservation",
            "reservationNumber": "H123",
            "underName": {"name": "Alex Sample"},
            "checkinTime": "2027-06-22T16:00:00-04:00",
            "checkoutTime": "2027-06-25T11:00:00-04:00",
            "totalPrice": "612.30",
            "priceCurrency": "USD",
            "reservationFor": {
                "@type": "LodgingBusiness",
                "name": "Hilton Example Central",
                "address": {
                    "streetAddress": "100 Example Street",
                    "addressLocality": "Philadelphia",
                    "addressRegion": "PA",
                    "postalCode": "00000",
                    "addressCountry": "US",
                },
            },
        },
        {
            "@type": "RentalCarReservation",
            "reservationNumber": "R9",
            "underName": {"name": "Alex Sample"},
            "pickupTime": "2027-06-22T13:00:00-04:00",
            "dropoffTime": "2027-06-25T10:00:00-04:00",
            "pickupLocation": {"name": "Philadelphia Intl Airport"},
            "dropoffLocation": {"name": "Philadelphia Intl Airport"},
            "reservationFor": {
                "@type": "RentalCar",
                "name": "Toyota Camry",
                "rentalCompany": {"name": "Hertz"},
            },
        },
        {
            "@type": "TrainReservation",
            "reservationNumber": "T5",
            "underName": {"name": "Alex Sample"},
            "reservationFor": {
                "@type": "TrainTrip",
                "trainNumber": "9031",
                "provider": {"name": "Rail Example"},
                "departureStation": {"name": "Philadelphia Example Station"},
                "arrivalStation": {"name": "New York Example Station"},
                "departureTime": "2027-06-25T10:10:00-04:00",
                "arrivalTime": "2027-06-25T11:40:00-04:00",
            },
            "reservedTicket": {"ticketedSeat": {"seatNumber": "4A"}},
        },
    ]
    ex = map_jsonld(objs)
    kinds = {r.kind: r for r in ex.reservations}
    assert set(kinds) == {"lodging", "car", "train"}
    h = kinds["lodging"]
    assert h.property_name == "Hilton Example Central" and h.check_in == "2027-06-22T16:00"
    assert (
        h.address == "100 Example Street, Philadelphia, PA, 00000, US"
        and h.price == 612.30
        and h.currency == "USD"
    )
    c = kinds["car"]
    assert (
        c.provider == "Hertz"
        and c.pickup_location == "Philadelphia Intl Airport"
        and c.notes == "Toyota Camry"
    )
    t = kinds["train"]
    assert (
        t.segments[0].train_number == "9031"
        and t.segments[0].seats == ["4A"]
        and t.provider == "Rail Example"
    )
    assert all(is_complete(r) for r in ex.reservations)


def test_every_traveller_s_train_seat_is_kept_on_the_shared_leg():
    def train(name: str, seat: str) -> dict:
        return {
            "@type": "TrainReservation",
            "reservationNumber": "T5",
            "underName": {"name": name},
            "reservationFor": {
                "@type": "TrainTrip",
                "trainNumber": "9031",
                "departureStation": {"name": "Philadelphia Example Station"},
                "arrivalStation": {"name": "New York Example Station"},
                "departureTime": "2027-06-25T10:10:00-04:00",
                "arrivalTime": "2027-06-25T11:40:00-04:00",
            },
            "reservedTicket": {"ticketedSeat": {"seatNumber": seat}},
        }

    ex = map_jsonld([train("Alex Sample", "4A"), train("Pat Sample", "4B")])
    (t,) = ex.reservations
    (leg,) = t.segments
    assert leg.seats == ["4A", "4B"]
    assert leg.passengers == ["Alex Sample", "Pat Sample"]


def test_incomplete_flight_is_flagged():
    o = flight_obj("X", None)
    del o["reservationFor"]["departureTime"]
    assert not is_complete(map_jsonld([o]).reservations[0])


def test_empty_output_is_other():
    ex = map_jsonld([])
    assert ex.document_type == "other" and ex.reservations == []


@pytest.mark.skipif(shutil.which("kitinerary-extractor") is None, reason="binary only in the image")
def test_real_binary_on_kde_sample():
    ex = extract_file(DATA / "kde-flight-booking.eml", context_date="2027-03-14")
    assert ex.reservations and ex.reservations[0].kind == "flight"
    seg = ex.reservations[0].segments[0]
    assert "9A" in seg.seats
    assert (
        seg.carrier_iata
        and seg.flight_number
        and seg.origin
        and seg.destination
        and seg.departure_local
    )


def test_the_wire_schema_maps_to_extraction():
    from tripbox.parse.models import WireExtraction, WireLeg, WireReservation, to_extraction

    h = WireExtraction(
        document_type="booking",
        confidence=1.4,
        reservations=[
            WireReservation(
                kind="flight",
                status="confirmed",
                code="QT9W8Z",
                provider="United",
                passengers=["SAMPLE/ALEX QUINN", "SAMPLE/SAM"],
                legs=[
                    WireLeg(
                        carrier="UA",
                        number="9120",
                        origin="MSP",
                        destination="DEN",
                        departure="2027-10-26T09:00",
                        arrival="2027-10-26T10:05",
                        seats=["SAMPLE/ALEX QUINN: 14A", "SAMPLE/SAM: 14B"],
                    )
                ],
                price=512.0,
                currency="USD",
            ),
            WireReservation(
                kind="lodging",
                code="H1",
                place="Example Inn Denver",
                address="100 Example Street, Denver, CO",
                start="2027-10-26",
                end="2027-10-28",
            ),
            WireReservation(
                kind="car",
                provider="Example Rent A Car",
                place="DEN airport",
                address="DEN airport",
                start="2027-10-26T10:45",
                end="2027-10-28T16:00",
            ),
            WireReservation(
                kind="train",
                provider="Rail Example",
                legs=[
                    WireLeg(
                        carrier="Rail Example",
                        number="9031",
                        origin="Philadelphia Example Station",
                        destination="New York Example Station",
                        departure="2027-06-25T10:10",
                    )
                ],
            ),
        ],
    )
    ex = to_extraction(h)
    assert ex.confidence == 1.0 and len(ex.reservations) == 4
    f = ex.reservations[0]
    assert f.confirmation_code == "QT9W8Z" and f.segments[0].carrier_iata == "UA"
    assert f.segments[0].seats == ["14A", "14B"]
    assert f.segments[0].seat_by_passenger == {
        "SAMPLE/ALEX QUINN": "14A",
        "SAMPLE/SAM": "14B",
    }
    assert is_complete(f)
    h2 = ex.reservations[1]
    assert (
        h2.property_name == "Example Inn Denver"
        and h2.check_in == "2027-10-26"
        and is_complete(h2)
    )
    c = ex.reservations[2]
    assert (
        c.pickup_location == "DEN airport"
        and c.pickup_time == "2027-10-26T10:45"
        and is_complete(c)
    )
    t = ex.reservations[3]
    assert (
        t.segments[0].train_number == "9031"
        and t.segments[0].operator == "Rail Example"
        and is_complete(t)
    )


def test_passengers_are_kept_per_leg():
    objs = [
        flight_obj("ALEX SAMPLE", "1A", number="100"),
        flight_obj("SAM SAMPLE", "2B", number="200", dep="2027-06-26T16:45:00-04:00"),
    ]
    res = map_jsonld(objs).reservations[0]
    assert len(res.segments) == 2 and res.passengers == ["ALEX SAMPLE", "SAM SAMPLE"]
    assert [s.passengers for s in res.segments] == [["ALEX SAMPLE"], ["SAM SAMPLE"]]


def test_a_cancelled_leg_does_not_cancel_the_confirmed_ones():
    ok, no = "http://schema.org/ReservationConfirmed", "http://schema.org/ReservationCancelled"
    objs = [
        flight_obj("ALEX SAMPLE", "1A", number="100", status=ok),
        flight_obj(
            "ALEX SAMPLE", "2B", number="200", dep="2027-06-26T16:45:00-04:00", status=no
        ),
        flight_obj(
            "ALEX SAMPLE", "3C", number="300", dep="2027-06-26T19:30:00-04:00", status=ok
        ),
    ]
    ex = map_jsonld(objs)
    assert ex.document_type == "booking" and ex.reservations[0].status == "confirmed"
    assert [s.status for s in ex.reservations[0].segments] == [
        "confirmed",
        "cancelled",
        "confirmed",
    ]
    only = map_jsonld([objs[1]])
    assert only.document_type == "cancellation" and only.reservations[0].status == "cancelled"


def test_flight_number_keeps_only_the_number():
    cases = [
        ("B6", "B6123", "123"),
        ("G4", "G4 123", "123"),
        ("F9", "F9 9010", "9010"),
        ("DL", "DL 1234", "1234"),
        ("DL", "1234", "1234"),
        ("DL", "9653", "9653"),
        ("DL", 42, "42"),
    ]
    for carrier, printed, want in cases:
        o = flight_obj("ALEX SAMPLE", "1A", number=printed)
        o["reservationFor"]["airline"]["iataCode"] = carrier
        seg = map_jsonld([o]).reservations[0].segments[0]
        assert (seg.carrier_iata, seg.flight_number) == (carrier, want), printed
