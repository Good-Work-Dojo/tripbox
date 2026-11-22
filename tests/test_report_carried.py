# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What a report's second redaction is handed from its first: what a label, a key or a street
made a value, never what its shape alone did. A flight's or a train's number stays in the
email, in what was read and in the booking's key, however it is printed. Every value is
invented."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tripbox import corpus
from tripbox.redact import Known, Redactor
from tripbox.report import sample

CORPUS = corpus.load(corpus.default_dir())
HEAD = "From: Example Air <bookings@air.example>\nTo: trips@example.com\nSubject: Your trip\n\n"


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    """An installation that knows nobody and holds nothing."""
    base = tmp_path_factory.mktemp("carried")
    (base / "config").mkdir()
    return corpus.settings(
        CORPUS,
        data_dir=base / "data",
        archive_dir=base / "archive",
        logs_dir=base / "logs",
        config_dir=base / "config",
    )


def report(settings, lines: str, booking: dict, key: str) -> SimpleNamespace:
    """The report of an email of these lines, read as this one booking and filed under this
    key: the key as shown, the booking as shown, the email's lines as shown, and all of it."""
    filed = [{"kind": booking["kind"], "result": "filed", "detail": None, "checks": [], "key": key}]
    held = sample._Held("processed", "kitinerary", None, filed, {"reservations": [booking]})
    made = sample._write(settings, (HEAD + lines).encode(), [], sample.FROM_FILE, held)
    did, rest = made.text.split(f"\n{sample.DID}\n", 1)[1].split(f"\n{sample.READ}\n", 1)
    read, email = rest.split(f"\n{sample.EMAIL}\n", 1)
    return SimpleNamespace(
        key=json.loads(did)["bookings"][0]["key"],
        read=json.loads(read)["reservations"][0],
        lines=email.split("\n\n", 1)[1],
        text=made.text,
    )


FLIGHTS = (
    ("B6123", "B6 123"),
    ("B61234", "B6 1234"),
    ("U28123", "U2 8123"),
    ("W61234", "W6 1234"),
    ("F91234", "F9 1234"),
    ("6E2134", "6E 2134"),
    ("G4521", "G4 521"),
    ("EZY8123", "EZY 8123"),
)


@pytest.mark.parametrize(("glued", "spaced"), FLIGHTS)
def test_a_flights_number_stays_in_the_email_in_what_was_read_and_in_the_key(
    settings, glued, spaced
):
    lines = (
        f"{glued} departs 09:30\nFlight {glued} departs 09:30\nOperated as {spaced}\n"
        "Confirmation code: KXJ4PL\nCall 612-555-0142\n"
    )
    leg = {"flight_number": glued, "carrier_iata": spaced.split()[0], "origin": "MSP"}
    booking = {"kind": "flight", "confirmation_code": "KXJ4PL", "segments": [leg]}
    shown = report(settings, lines, booking, f"flight|{glued.lower()}|2027-06-14|msp")
    assert shown.key == f"flight|{glued.lower()}|2027-06-14|msp"
    assert shown.read["segments"] == [leg] and shown.read["confirmation_code"] == "[REF-1]"
    assert shown.lines == (
        f"{glued} departs 09:30\nFlight {glued} departs 09:30\nOperated as {spaced}\n"
        "Confirmation code: [REF-1]\nCall [PHONE-1]\n"
    )


def test_a_trains_number_stays_in_the_email_in_what_was_read_and_in_the_key(settings):
    lines = "ICE9907 Exampleton 14:32\nTrain ICE9907, coach 14\nBooking number: RZ7K2Q\n"
    leg = {"train_number": "ICE9907", "origin": "Exampleton"}
    booking = {"kind": "train", "confirmation_code": "RZ7K2Q", "segments": [leg]}
    shown = report(settings, lines, booking, "train|ice9907|2027-07-07|exampleton")
    assert shown.key == "train|ice9907|2027-07-07|exampleton"
    assert shown.read["segments"] == [leg]
    assert shown.lines == (
        "ICE9907 Exampleton 14:32\nTrain ICE9907, coach 14\nBooking number: [REF-1]\n"
    )


def test_a_reference_a_label_names_goes_from_all_three_whatever_its_shape(settings):
    """A code that reads as a flight's number is still the code its label says it is; the
    flight's own number beside it stays."""
    lines = "Confirmation code: U28123\nFlight B6123 departs 09:30\nYour code U28123 again\n"
    leg = {"flight_number": "B6123", "origin": "MSP"}
    booking = {"kind": "flight", "confirmation_code": "U28123", "segments": [leg]}
    shown = report(settings, lines, booking, "flight|b6123|2027-06-14|msp|u28123")
    assert shown.key == "flight|b6123|2027-06-14|msp|[REF-1]"
    assert shown.read == {"kind": "flight", "confirmation_code": "[REF-1]", "segments": [leg]}
    assert shown.lines == (
        "Confirmation code: [REF-1]\nFlight B6123 departs 09:30\nYour code [REF-1] again\n"
    )
    assert "U28123" not in shown.text and "u28123" not in shown.text


def test_what_only_its_shape_or_its_place_made_a_value_is_not_carried(settings):
    # a count written as one word, replaced where it stands, is no code elsewhere
    lines = "2ADULTS\nRoom for 2 adults, 2 Adults in all\nHotel address: Seattle, WA\n"
    lines += "Flight to Seattle, WA on June 14, 2027 (2027-06-14)\nNone of the rooms has a view\n"
    booking = {"kind": "lodging", "confirmation_code": "2027", "notes": "Seattle, WA. None."}
    shown = report(settings, lines, booking, "lodging|2027")
    assert shown.lines == (
        "[REF-2]\nRoom for 2 adults, 2 Adults in all\nHotel address: [ADDRESS-1]\n"
        "Flight to Seattle, WA on June 14, 2027 (2027-06-14)\nNone of the rooms has a view\n"
    )
    # a code that is a year goes from the field its key names, and from nowhere else
    assert shown.read == {
        "kind": "lodging",
        "confirmation_code": "[REF-1]",
        "notes": "Seattle, WA. None.",
    }
    assert shown.key == "lodging|2027"
    # nor is a word a model gave for a code
    booking = {"kind": "lodging", "confirmation_code": "None"}
    shown = report(settings, "None of the rooms has a view\n", booking, "draft|0123456789abcdef|0")
    assert shown.lines == "None of the rooms has a view\n"
    assert shown.read["confirmation_code"] == "[REF-1]"


def test_what_a_redaction_hands_on_is_what_a_label_a_key_or_a_street_named():
    r = Redactor(Known())
    r.text(
        "B6123 and QK7M2P4 depart. Ticket 0001234567890, order 86420975, at 192.0.2.7.\n"
        "Call 612-555-0142. Passport number: 123456789. Card ending 4242.\n"
        "Hotel address: Seattle, WA\nLodge address: Rose Cottage, Example Lane\n"
        "Example Inn, Seattle, WA 98101\n300 Example Way, Seattle, WA 98101\n"
    )
    r.value({"confirmation_code": "4821", "ticketNumber": "0009876543210", "pnr": "2027"})
    # in the order they are met: a number its label names, a phone number, a document's number,
    # an address its label names, an address with its street, and what a key names. Not the
    # code and the ticket's number that only their shape tells, the card's four digits, the
    # town under a label, the town with its ZIP code, the year under a key.
    assert r.found == (
        ("reference", "86420975"),
        ("phone", "612-555-0142"),
        ("number", "123456789"),
        ("address", "Rose Cottage, Example Lane"),
        ("address", "300 Example Way, Seattle, WA 98101"),
        ("reference", "4821"),
        ("ticket", "0009876543210"),
    )
    # nor is a value handed on that reads as a flight's or a train's number, whoever names it
    for code in ("B6123", "EZY8123", "ICE 9907", "2027"):
        assert Redactor(Known()).value({"confirmation_code": code}) == {
            "confirmation_code": "[REF-1]"
        }
    named = Redactor(Known())
    named.value([{"pnr": code} for code in ("6E2134", "ICE9907", "AS 9203", "2027", "1999")])
    assert named.found == ()
    # a code of four letters is carried no more than a known one of that length is
    again = Redactor(Known(found=(("reference", "None"), ("reference", "4821"))))
    assert again.text("None of the 4821 rooms") == "None of the [REF-1] rooms"


def unread(settings, lines: str) -> str:
    """The email's lines as the report of an email nothing read shows them."""
    held = sample._Held("failed", None, None, [], None)
    made = sample._write(settings, (HEAD + lines).encode(), [], sample.FROM_FILE, held)
    return made.text.split(f"\n{sample.EMAIL}\n", 1)[1].split("\n\n", 1)[1]


# A reference whose label ends in the word for a flight, then a bare copy of it near and far.
LABELLED_FOR_A_FLIGHT = (
    ("Booking reference for flight: XZS975", "Your trip XZS975 is confirmed", "XZS975"),
    ("Booking reference - Flight: XZS975", "Trip XZS975", "XZS975"),
    ("Código de reserva del vuelo: XZS975", "Su viaje XZS975 está confirmado", "XZS975"),
    ("Référence de réservation du vol : XZS975", "Votre voyage XZS975", "XZS975"),
    ("Buchungscode Flug: XZS975", "Ihre Reise XZS975", "XZS975"),
    ("Reservation number for flight: ABC12D", "Trip ABC12D", "ABC12D"),
    ("Booking reference for your return flight: XZS975", "Trip XZS975", "XZS975"),
    ("Booking reference for the outbound flight: XZS975", "Trip XZS975", "XZS975"),
    ("Booking reference, flight: XZS975", "Trip XZS975", "XZS975"),
    ("Booking reference / Flight: XZS975", "Trip XZS975", "XZS975"),
    ("Confirmation number for your Example Air flight: XZS975", "Trip XZS975", "XZS975"),
    ("Buchungsnummer für den Flug: XZS975", "Ihre Reise XZS975", "XZS975"),
    ("Buchungsnummer Ihres Flugs / Flug: XZS975", "Ihre Reise XZS975", "XZS975"),
    ("Código de reserva de su vuelo: XZS975", "Su viaje XZS975", "XZS975"),
    ("Localizador de tu vuelo: XZS975", "Su viaje XZS975", "XZS975"),
    ("Référence de votre vol : XZS975", "Votre voyage XZS975", "XZS975"),
    ("Numéro de réservation de votre vol : XZS975", "Votre voyage XZS975", "XZS975"),
)


@pytest.mark.parametrize(("label", "near", "code"), LABELLED_FOR_A_FLIGHT)
def test_a_reference_whose_label_ends_in_a_transport_word_goes_wherever_it_stands(
    settings, label, near, code
):
    # the line between puts the bare copies past where the label looks for its code
    lines = f"{label}\nWe look forward to welcoming you on board soon.\n{near}\nAgain: {code}\n"
    assert unread(settings, lines) == lines.replace(code, "[REF-1]")


LONG_LABELS_FOR_A_FLIGHT = (
    "Confirmation code for your Example Air Lines return flight: XZS975",
    "Booking reference for your Example Airways outbound flight: XZS975",
    "Your confirmation code (please keep it safe) for this flight: XZS975",
)


@pytest.mark.parametrize("label", LONG_LABELS_FOR_A_FLIGHT)
def test_a_reference_whose_long_label_ends_in_a_transport_word_goes_from_every_bare_copy(
    settings, label
):
    """A label's words may run past its window up to the word for a flight: the code is still
    the label's, so no bare copy of it is kept as a flight's number."""
    lines = f"{label}\nWe look forward to welcoming you on board soon.\n"
    lines += "Your trip XZS975 is confirmed\nAgain: XZS975\n"
    assert unread(settings, lines) == lines.replace("XZS975", "[REF-1]")


def test_a_flights_number_after_a_label_that_stands_apart_is_still_a_flights_number(settings):
    """A label whose colon and code stand before the word for a flight leaves that flight's
    number alone."""
    lines = "Booking reference: KXJ4PL, Flight: EZY8123\nEZY8123 departs 09:30\n"
    assert unread(settings, lines) == lines.replace("KXJ4PL", "[REF-1]")


def test_a_label_word_before_the_word_for_a_flight_on_its_line_makes_the_number_a_reference(
    settings,
):
    """A label owns the word for a flight on its line when no colon, digit or line break
    stands between them, whatever else does: the flight's number then goes as a reference
    wherever it stands, the price of never keeping a reference whose label ends that way."""
    lines = "Your booking is confirmed. Flight: EZY8123\nEZY8123 departs 09:30\n"
    assert unread(settings, lines) == lines.replace("EZY8123", "[REF-1]")


def test_only_a_flights_or_a_trains_key_makes_its_value_a_number_kept_in_the_email(settings):
    lines = "Member AB12345 thanks you\nConfirmation code: KXJ4PL\n"
    leg = {"flight_number": "B6123", "seat": "AB12345", "origin": "MSP"}
    booking = {"kind": "flight", "confirmation_code": "KXJ4PL", "segments": [leg]}
    shown = report(settings, lines, booking, "flight|b6123|2027-06-14|msp")
    assert "AB12345" not in shown.lines
    assert shown.lines == "Member [REF-2] thanks you\nConfirmation code: [REF-1]\n"
