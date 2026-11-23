# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The redactor: what it replaces, what it keeps on purpose, and that one value gets one
placeholder wherever it stands. Every value here is the Sample family's or the corpus's own."""

from __future__ import annotations

import base64
import email
import json
import quopri
import re
import time
import tracemalloc
from email.message import EmailMessage
from pathlib import Path

from helpers.eml import make_eml

from tripbox import redact
from tripbox.redact import KEPT_HEADERS, MAX_DEPTH, MAX_LINE, MAX_PARTS, Known, Redactor

OWNER = ("Alex Sample", "Alex Quinn Sample", "Alex Q Sample")
LISTED = frozenset({"alaskaair.com"})
MARKUP = {
    "@context": "https://schema.org",
    "@type": "FlightReservation",
    "reservationNumber": "XZS975",
    "reservationStatus": "https://schema.org/ReservationConfirmed",
    "underName": {"@type": "Person", "name": "Alex Sample"},
    "reservationFor": {
        "@type": "Flight",
        "flightNumber": "AS9203",
        "airline": {"@type": "Airline", "name": "Alaska Airlines", "iataCode": "AS"},
        "departureAirport": {"@type": "Airport", "iataCode": "MSP"},
        "departureTime": "2027-06-14T09:30:00-05:00",
        "arrivalAirport": {"@type": "Airport", "iataCode": "SEA"},
        "arrivalTime": "2027-06-14T11:18:00-07:00",
    },
}


def page(markup: dict, body: str) -> str:
    block = json.dumps(markup, indent=2)
    return (
        "<!DOCTYPE html>\n<html><head>"
        f'<script type="application/ld+json">\n{block}\n</script>'
        f"</head><body>{body}</body></html>"
    )


def read(out: str) -> tuple[email.message.Message, dict[str, str]]:
    """A redacted email read back as an email: the message, and each leaf's text by its type."""
    msg = email.message_from_string(out)
    leaves = {p.get_content_type(): p.get_payload() for p in msg.walk() if not p.is_multipart()}
    return msg, leaves


def block_of(html: str):
    found = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert found, html
    return json.loads(found.group(1))


def test_one_value_gets_one_placeholder_wherever_it_stands():
    raw = make_eml(
        from_="Alaska Airlines <no-reply@alaskaair.com>",
        to="Alex Sample <alex@example.com>",
        subject="Your trip is confirmed",
        text="Travelers\nAlex Sample\nSam Sample\n",
        html=page(MARKUP, "<p>Alex Sample</p><p>Sam Sample</p>"),
    ).as_bytes()
    r = Redactor(Known(names=("Alex Sample", "Sam Sample")))
    out = r.email(raw)
    msg, leaves = read(out)
    assert msg["To"].startswith("[NAME-1] <")
    assert leaves["text/plain"].split("\n")[:3] == ["Travelers", "[NAME-1]", "[NAME-2]"]
    assert "<p>[NAME-1]</p><p>[NAME-2]</p>" in leaves["text/html"]
    assert block_of(leaves["text/html"])["underName"]["name"] == "[NAME-1]"
    assert not re.search(r"(?i)alex|sample|\bsam\b", out)
    assert r.counts["name"] == 6


def test_a_name_is_found_in_every_form_an_airline_prints_it():
    r = Redactor(Known(names=OWNER))
    assert r.text("ALEX SAMPLE") == r.text("Sample, Alex") == r.text("sample/alex") == "[NAME-1]"
    for form in ("ALEX SAMPLE", "SAMPLE/ALEX QUINN", "Sample, Alex", "Mr Alex Q Sample"):
        out = r.text(f"Passenger: {form}, seat 32A")
        assert re.fullmatch(r"Passenger: (Mr )?\[NAME-\d\], seat 32A", out), out
    # letters run together are one word to the redactor: a gap its own notes name
    assert r.text("SAMPLEALEX") == "SAMPLEALEX"
    assert "SAMPLEALEX" in redact.__doc__


def test_a_short_part_goes_only_beside_the_rest_of_its_name():
    r = Redactor(Known(names=("Alex Q Sample",)))
    assert r.text("Gate Q, row Q. Alex Q Sample boards last") == (
        "Gate Q, row Q. [NAME-1] boards last"
    )
    assert r.text("SAMPLE/Q") == "[NAME-2]"
    # a part of three letters or more goes wherever it is a word, and never inside one
    assert r.text("Alex sampled the Sample menu") == "[NAME-3] sampled the [NAME-4] menu"
    # two short parts of a longer name are words of the language wherever its name is not
    particles = Redactor(Known(names=("Robin de la Sample", "Li Wu")))
    assert particles.text("Confirmación de la reserva de Robin de la Sample, de Li Wu y Wu") == (
        "Confirmación de la reserva de [NAME-1], de [NAME-2] y Wu"
    )
    # however the parts of two names stand side by side, none is left
    family = Redactor(Known(names=("Alex Sample", "Alex Quinn Sample", "Sam Sample")))
    for row in ("Sample Alex Quinn Sample", "Sam Sample Alex Sample", "Quinn Sam Alex Sample Sam"):
        assert not re.search(r"[a-z]", family.text(row)), row


def test_a_relatives_first_name_goes_with_the_known_family_name_beside_it():
    """The travellers file may name the owner alone: whoever shares the family name is printed
    beside it, and a middle name beside the first."""
    r = Redactor(Known(names=("Alex Sample",)))
    gone = (
        "EMERGENCY CONTACT: SAM SAMPLE 612 555 0143",
        "Travelers: Alex Sample, Robin Sample",
        "1.SAMPLE/JAMIE  2.SAMPLE, ROBIN",
        "Alex Quinn Sample",
    )
    for printed in gone:
        assert not re.search(r"(?i)sam\b|sample|robin|jamie|alex|quinn", r.text(printed)), printed
    # an initial beside a known part is a part of the name too
    assert r.text("Mr J. Sample and Alex Q. Sample") == "Mr [NAME-7] and [NAME-8] [NAME-6]"
    assert r.text("Contact: SAM SAMPLE 612 555 0143") == "Contact: [NAME-1] [PHONE-1]"
    # a word that is no name stays beside it, and the word goes nowhere else
    kept = {
        "The Sample House Inn": "The [NAME-6] House Inn",
        "Your Sample Trips itinerary": "Your [NAME-6] Trips itinerary",
        "Hello Alex, Thank you": "Hello [NAME-9], Thank you",
        "Dear Alex Your trip to Seattle, Sam": "Dear [NAME-9] Your trip to Seattle, Sam",
    }
    for printed, redacted in kept.items():
        assert r.text(printed) == redacted


def test_an_address_of_mail_keeps_a_listed_senders_domain_and_no_other():
    r = Redactor(Known(sender_domains=LISTED))
    assert r.text("From no-reply@alaskaair.com") == "From [EMAIL-1]@alaskaair.com"
    assert r.text("news@mail.alaskaair.com") == "[EMAIL-2]@mail.alaskaair.com"
    assert r.text("alex@example.com wrote; ALEX@example.com") == "[EMAIL-3] wrote; [EMAIL-3]"
    # a domain that only ends as a listed one does is no host under it
    assert Redactor(Known(sender_domains={"rail.example"})).text(
        "billets@rail.example, x@notrail.example"
    ) == "[EMAIL-1]@rail.example, [EMAIL-2]"
    assert r.text("pat@example.net") == "[EMAIL-4]"
    # the envelope Gmail writes for a forward holds the address twice over, and goes whole
    assert r.text("<alex+caf_=trips=example.com@example.com>") == "<[EMAIL-5]>"
    # an address in any script
    assert r.text("an alex@beispiel-süd.example, bitte") == "an [EMAIL-6], bitte"
    assert r.counts["email"] == 7


def test_a_links_query_and_opaque_path_go_and_its_host_stays():
    r = Redactor(Known())
    assert r.text("See https://www.example.com/manage/trip/ab12cd34?code=XZS975&last=x#top.") == (
        "See https://www.example.com/manage/trip/[X]?[QUERY]."
    )
    assert r.counts["link"] == 1
    for kept in (
        "https://schema.org/ReservationConfirmed",
        "https://www.example.com/",
        "https://www.example.com/help/check-in.html",
    ):
        assert r.text(kept) == kept
    # a path word in capitals is the shape of a booking code; a long run of letters is no word
    assert r.text("http://www.example.com/trips/KXJQPL") == "http://www.example.com/trips/[X]"
    assert r.text("https://www.example.com/c/U2FtcGxlVG9rZW4x") == "https://www.example.com/c/[X]"
    assert r.text(f"https://www.example.com/{'a' * 24}/{'a' * 25}") == (
        f"https://www.example.com/{'a' * 24}/[X]"
    )
    assert r.counts["link"] == 1


def test_a_reference_goes_where_it_is_known_and_where_a_label_names_it_in_four_languages():
    r = Redactor(Known(references=("XZS975",)))
    assert r.text("Your trip XZS975 (xzs 975), key lodging|XZS975") == (
        "Your trip [REF-1] ([REF-1]), key lodging|[REF-1]"
    )
    labelled = {
        "Confirmation number: QWS987": "Confirmation number: [REF-2]",
        "Record locator     RWV431": "Record locator     [REF-3]",
        "Buchungsnummer: WYR420": "Buchungsnummer: [REF-4]",
        "Numéro de réservation : SXZ642": "Numéro de réservation : [REF-5]",
        "Código de reserva: YSV870": "Código de reserva: [REF-6]",
        "PNR: KXJQPL": "PNR: [REF-7]",
    }
    for printed, redacted in labelled.items():
        assert r.text(printed) == redacted
    # a code of digits or of small letters has no shape that gives it away: its label does
    assert r.text("Ref. 48213 / order no. kq7xm2") == "Ref. [REF-8] / order no. [REF-9]"
    assert r.text("To get a refund of 48213 points, order online") == (
        "To get a refund of [REF-8] points, order online"
    )
    # once a label names it, it goes wherever it stands
    assert r.text("Subject: Your stay QWS987") == "Subject: Your stay [REF-2]"
    # what follows a label and is no code stays
    for kept in (
        "Reservation status: CANCELED",
        "Booking date: 08/03/2027",
        "Your booking, flight AS9203 on 14JUN27",
        "RESERVATION CONFIRMED",
    ):
        assert r.text(kept) == kept
    assert r.counts["reference"] == 13


def test_ticket_numbers_phone_numbers_and_long_numbers_go():
    r = Redactor(Known())
    assert r.text("Ticket number 0001234500002") == "Ticket number [TICKET-1]"
    assert r.text("Ticket 000 1234500002 / 000-1234500003") == "Ticket [TICKET-1] / [TICKET-2]"
    phones = ("612-555-0142", "+1 206 555 0130", "(617) 555-0110", "01 99 00 41 27")
    for n, phone in enumerate((*phones, "089 99998-214"), start=1):
        assert r.text(f"Call {phone} today") == f"Call [PHONE-{n}] today"
    # the same digits are the same number, however they are printed
    assert r.text("(612) 555 0142, 612.555.0142") == "[PHONE-1], [PHONE-1]"
    assert r.text("206-555-0130 or 555-0130") == "[PHONE-2] or [PHONE-2]"
    assert r.text("Frequent flyer number 9000000001") == "Frequent flyer number [NUMBER-1]"
    assert r.text("Member 123 456, see ab1234567") == "Member [NUMBER-2], see ab[NUMBER-3]"
    assert (r.counts["ticket"], r.counts["phone"], r.counts["number"]) == (3, 9, 3)


def test_dates_times_flight_numbers_prices_and_seats_stay():
    r = Redactor(Known())
    kept = (
        "2027-06-14T09:30:00-05:00",
        "20270614T093000",
        "20270614",
        "14JUN27",
        "9:30 AM",
        "07.07.2027, ab 15:00 Uhr",
        "Sunday 11/07/2027",
        "à partir de 15 h",
        "Flight AS 9203",
        "Train 9907, coach 14, seat 61",
        "USD 1,592.34",
        "952,00 €",
        "1234567 KRW",
        "Winter 2027-2028 schedule",
        "Jun 14 2027 and 14-Jun-2027",
        "MSP",
        "seat 32A",
    )
    for printed in kept:
        assert r.text(f"x {printed} y") == f"x {printed} y"
    busy = "Call 612-555-0142 before 9:30 AM on 2027-06-14, flight AS 9203, USD 48.24, seat 32A"
    assert r.text(busy) == busy.replace("612-555-0142", "[PHONE-1]")
    # eight digits that are no day are a number like any other
    assert r.text("20271340") == "[NUMBER-1]"
    assert sum(r.counts.values()) == 2


def test_street_addresses_in_four_languages_go_and_a_city_alone_stays():
    r = Redactor(Known())
    streets = (
        "100 Sample Lane, Minneapolis, MN 55401",
        "12 Rue de l'Exemple, 75006 Exempleville",
        "Beispielstraße 5, 80331 Beispielstadt",
        "Blvd. Ejemplo Km 9, Zona Hotelera, 77500 Ciudad Ejemplo",
    )
    for n, street in enumerate(streets, start=1):
        assert r.text(f"Address: {street}\nPhone") == f"Address: [ADDRESS-{n}]\nPhone"
    # a line that holds a British postcode goes whole
    assert r.text("The Example Inn\nExample House, Exampleton ZZ99 9ZZ\nUK") == (
        "The Example Inn\n[ADDRESS-5]\nUK"
    )
    cities = ("Arrive: Seattle (SEA) 11:18 AM", "Minneapolis-St. Paul", "Seattle, June 14 - 20")
    for kept in cities:
        assert r.text(kept) == kept
    # an address the reading gave goes whole, and so does its first line alone
    known = Redactor(Known(addresses=("300 Example Way, Seattle, WA, 98101, US", "1 Example Loop")))
    assert known.text("at 300 Example Way, Seattle, WA 98101.") == "at [ADDRESS-1]."
    assert known.text('"streetAddress": "300 example way"') == '"streetAddress": "[ADDRESS-1]"'
    assert known.text("Pick-up\n  1 Example Loop, Seattle, WA 98101 (same)") == (
        "Pick-up\n  [ADDRESS-2] (same)"
    )
    # what a label calls an address is one, whatever it looks like; and a box at a post office
    assert r.text("Lodge address: Example Point, North Shore\nPhone") == (
        "Lodge address: [ADDRESS-6]\nPhone"
    )
    assert r.text("Write to P.O. Box 1000, Minneapolis, MN 55401.") == "Write to [ADDRESS-7]."
    assert r.text("Email address: alex@example.com") == "Email address: [EMAIL-1]"
    assert r.text("Voorbeeldstraat 12, 1011 AB Voorbeeldstad, NL") == "[ADDRESS-8], NL"
    assert r.counts["address"] == 8


def test_a_code_no_label_names_goes_and_a_flight_number_stays():
    r = Redactor(Known())
    assert r.text("Your trip is booked: RTW753") == "Your trip is booked: [REF-1]"
    assert r.text("Trip RTW753, fare basis QA7NA0MC") == "Trip [REF-1], fare basis [REF-2]"
    kept = (
        "DL9140 departs gate B22, seats 32A and 41B, bag 23KG",
        "Train ICE9907, coach 14",
        "train|ICE9907|2027-07-07|parisest",
        "14JUN and JUN14, 2H05M, 1430H",
        "Rtw753 is no code as printed",
    )
    for printed in kept:
        assert r.text(printed) == printed
    # in a reading, the keys that hold a flight or a train keep theirs
    leg = {"train_number": "ICE9907", "flight_number": "9203", "origin": "MSP", "seats": ["32A"]}
    assert r.value({"confirmation_code": "RTW753", "segments": [leg]}) == {
        "confirmation_code": "[REF-1]",
        "segments": [leg],
    }
    assert r.counts["reference"] == 4


def test_a_cards_last_digits_go_behind_whatever_stands_for_the_others():
    r = Redactor(Known())
    for printed in (
        "Card: ●●●● ●●●● ●●●● 0000", "Card …0000", "Card ####0000", "Card ........0000",
        "Paid with Visa XX0000", "Paid with Visa (0000)", "Mastercard [0000] was charged",
    ):
        assert r.text(printed) == printed.replace("0000", "[NUMBER-1]"), printed
    # with no word for a card before them, such signs stand for nothing
    for kept in ("Flight XX1234 departs", "Rooms (1204) and (1206)"):
        assert r.text(kept) == kept, kept


def test_what_a_label_calls_personal_goes_whatever_it_looks_like():
    r = Redactor(Known())
    # what is printed, what of it goes, and the placeholder it goes for
    labelled = (
        ("Paid with Visa ending in 0000 on June 14, 2027", "0000", 1),
        ("Ihre Kreditkarte mit den\nEndziffern 0000 dient", "0000", 1),
        ("Card: **** **** **** 0000", "0000", 1),
        ("Date of birth: 14/06/2027", "14/06/2027", 2),
        ("Geburtsdatum: 14.06.2027", "14.06.2027", 2),
        ("Born on June 14, 2027", "June 14, 2027", 3),
        ("Passport: C01X00T47", "C01X00T47", 4),
        ("Rewards number: X7K9P", "X7K9P", 5),
        ("Known Traveler Number 98X7K", "98X7K", 6),
        ("Door code: 4821", "4821", 7),
        ("Wi-Fi password: sample4821", "sample4821", 8),
        ("Your PIN 7731 opens the gate", "7731", 9),
    )
    for printed, personal, n in labelled:
        assert r.text(printed) == printed.replace(personal, f"[NUMBER-{n}]")
    # on a page a label and its value stand in two cells
    cells = (
        "<table><tr><td>PIN code</td><td><b>4821</b></td></tr>"
        "<tr><td>Date of birth</td><td>14/06/2027</td></tr>"
        "<tr><td>Check-in date</td><td>20/06/2027</td></tr></table>"
    )
    assert r.html(cells) == cells.replace("4821", "[NUMBER-7]").replace("14/06/2027", "[NUMBER-2]")
    kept = (
        "A card is asked for at check-in, from 3:00 PM on 2027-06-14",
        "Visa rules for 2027: check before you fly",
        "Member since 2027, account manager on duty",
        "Airport code: MSP, dress code: none",
        "Check-in date: 14/06/2027",
    )
    for printed in kept:
        assert r.text(printed) == printed
    assert r.counts["number"] == len(labelled) + 2 == sum(r.counts.values())


def test_coordinates_and_network_addresses_go():
    r = Redactor(Known())
    assert r.text("geo:47.6062,-122.3321") == "geo:[NUMBER-1],[NUMBER-2]"
    assert r.text("signed in from 203.0.113.9") == "signed in from [NUMBER-3]"
    assert r.text("and from 2001:db8::7 at 10:30:45") == "and from [NUMBER-4] at 10:30:45"
    assert r.text("N 047° 36.372, W 122° 19.926") == "N [NUMBER-5], W [NUMBER-6]"
    for kept in ("Bags up to 27.5 kg, USD 33.15", "Ratio 16:9 at 09:55"):
        assert r.text(kept) == kept


def test_only_the_headers_a_reader_needs_are_kept():
    msg = make_eml(
        from_="Alaska Airlines <no-reply@alaskaair.com>",
        to="alex@example.com",
        subject="Your trip is confirmed",
        text="Thank you for flying with us.\n",
        message_id="<confirm.k7q2m@mta.alaskaair.com>",
        headers={
            "Received": "from mta.example by mx.example with ESMTPS id k7q2m",
            "DKIM-Signature": "v=1; a=rsa-sha256; d=alaskaair.com; s=s1; b=k7q2m",
            "List-Unsubscribe": "<https://www.example.com/u?id=k7q2m>",
            "X-Mailer": "Sample Mailer",
            "Return-Path": "<alex+caf_=trips=example.com@example.com>",
            "Authentication-Results": "mx.google.com; dkim=pass header.i=@alaskaair.com"
            ' header.s=s1; spf=pass (google.com: domain of x designates 192.0.2.7)'
            ' smtp.mailfrom="alex+caf_=trips=example.com@example.com";'
            " dmarc=pass header.from=example.com",
        },
    )
    r = Redactor(Known(sender_domains=LISTED))
    out = r.email(msg.as_bytes())
    head = out.split("\n\n", 1)[0]
    names = [line.split(":", 1)[0] for line in head.split("\n")]
    assert set(names) <= set(KEPT_HEADERS)
    assert not re.search(r"(?i)received|dkim-signature|list-unsubscribe|x-mailer|k7q2m", out)
    assert r.removed_headers == 4
    assert "Message-ID: <[ID]@mta.alaskaair.com>" in head
    assert "From: Alaska Airlines <[EMAIL-1]@alaskaair.com>" in head
    results = next(line for line in head.split("\n") if line.startswith("Authentication-Results"))
    assert results == (
        "Authentication-Results: mx.google.com; dkim=pass header.i=@alaskaair.com;"
        " spf=pass smtp.mailfrom=[EMAIL-3]; dmarc=pass header.from=[DOMAIN]"
    )
    # every header the mail reader asks a message for is one a report keeps
    source = Path(redact.__file__).with_name("mail") / "normalize.py"
    reads = r'\b(?:msg|outer|primary|inner|part)\.get\(\s*"([A-Za-z-]+)"'
    asked = set(re.findall(reads, source.read_text()))
    assert {"From", "Authentication-Results", "Return-Path", "Message-ID"} <= asked
    kept = {name.lower() for name in KEPT_HEADERS}
    assert {name.lower() for name in asked} | {"x-forwarded-for"} <= kept


def test_an_attachment_is_never_carried():
    pdf = b"%PDF-1.4 Passenger Alex Sample, confirmation XZS975 " * 8
    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2
    raw = make_eml(
        from_="Alex Sample <alex@example.com>",
        subject="Fwd: Your e-ticket",
        text="Your e-ticket is attached.\n",
        attachments=[
            ("Alex Sample e-ticket.pdf", "application/pdf", pdf),
            ("boarding.png", "image/png", image),
        ],
    ).as_bytes()
    r = Redactor(Known(names=("Alex Sample",)))
    out = r.email(raw)
    assert f"[ATTACHMENT REMOVED: application/pdf, {len(pdf)} bytes]" in out
    assert f"[ATTACHMENT REMOVED: image/png, {len(image)} bytes]" in out
    assert r.removed_attachments == 2
    for data in (pdf, image):
        assert base64.b64encode(data).decode()[:40] not in out
    assert "PDF-1.4" not in out and "XZS975" not in out
    assert "e-ticket.pdf" not in out and "boarding" not in out
    assert 'filename="[FILE-1].pdf"' in out and 'filename="[FILE-2].png"' in out
    # a name whose last part after a dot is no file's kind keeps nothing of it
    for name in ("pass.Sample", "invoice.86420975"):
        odd = make_eml(
            from_="Alex Sample <alex@example.com>", subject="Fwd: x", text="See it.\n",
            attachments=[(name, "application/octet-stream", pdf)],
        ).as_bytes()
        shown = Redactor(Known(names=("Alex Sample",))).email(odd)
        assert 'filename="[FILE-1]"' in shown and name.split(".")[1] not in shown, name
    # an attachment of text goes whole too, whatever no rule would know in it; a calendar file
    # stays, redacted as the body is
    texts = [
        ("manifest.csv", "text/csv", b"name,seat\nLee Example,14A\nPat Example,14B\n"),
        ("host.vcf", "text/vcard", b"BEGIN:VCARD\nFN:Lee Example\nEND:VCARD\n"),
        ("notes.txt", "text/plain", b"Lee Example sits in 14A.\n"),
        ("page.html", "text/html", b"<p>Lee Example</p>\n"),
    ]
    invite = (
        b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Alex Sample to Seattle\n"
        b"END:VEVENT\nEND:VCALENDAR\n"
    )
    raw = make_eml(
        from_="Alex Sample <alex@example.com>", subject="Fwd: Manifest", text="See them.\n",
        attachments=[*texts, ("trip.ics", "text/calendar", invite)],
    ).as_bytes()
    r = Redactor(Known(names=("Alex Sample",)))
    out = r.email(raw)
    for kept in ("Lee", "Pat", "Example", "14A", "VCARD", "<p>"):
        assert kept not in out, kept
    for _, ctype, data in texts:
        assert f"[ATTACHMENT REMOVED: {ctype}, {len(data)} bytes]" in out, ctype
    assert r.removed_attachments == len(texts)
    assert "SUMMARY:[NAME-1] to Seattle" in out and "Content-Type: text/calendar" in out


def test_no_part_is_left_encoded():
    msg = EmailMessage()
    msg["From"] = "Rail Exemple <billets@rail.example>"
    msg["Subject"] = "Your tickets"
    words = "Arrivée 11:18 à Seattle, 2e classe, voiture 14. " * 6
    msg.set_content(words, cte="base64")
    page = f"<p>{words}</p><p>Réservé à Minneapolis</p>"
    msg.add_alternative(page, subtype="html", cte="quoted-printable")
    raw = msg.as_bytes()
    assert b"R=C3=A9serv=C3=A9" in raw
    assert base64.b64encode(words.encode())[:40] in raw.replace(b"\n", b"")
    out = Redactor(Known()).email(raw)
    _, leaves = read(out)
    assert "Arrivée 11:18 à Seattle, 2e classe, voiture 14." in leaves["text/plain"]
    assert "<p>Réservé à Minneapolis</p>" in leaves["text/html"]
    assert "=C3" not in out and "base64" not in out and "quoted-printable" not in out
    assert not re.search(r"[A-Za-z0-9+/=]{60,}", out)
    # text that is itself one long run of such characters is no text a reader needs
    blob = base64.b64encode(bytes(range(200))).decode()
    assert Redactor(Known()).text(f"token {blob} end") == "token [X] end"
    # bytes that are no text, in a header or a body, are replaced, never carried or choked on
    raw = b"From: Caf\xe9 <billets@rail.example>\nSubject: Billet \xe9\xff\n\nCaf\xe9 \xff\n"
    shown = Redactor(Known()).email(raw)
    assert shown.encode("utf-8").decode("utf-8") == shown and "Billet" in shown and "Caf" in shown


def sent(body: bytes, how: bytes, kind: bytes = b"text/html") -> bytes:
    """An email of one part whose body is written here as it is sent, so that a soft line break
    stands where the test puts it, under whatever its header calls the encoding."""
    return (
        b"From: Alaska Airlines <no-reply@alaskaair.com>\r\nTo: alex@example.com\r\n"
        b"Subject: Your trip is confirmed\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: " + kind + b'; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: " + how + b"\r\n\r\n" + body
    )


# A page as a sender's software wraps it: a soft line break inside a link's query, inside the
# address a link shows, inside a name and inside a booking reference.
WRAPPED = (
    b"<html><body><p>Hello Alex, it=E2=80=99s confirmed.</p>\r\n"
    b'<p><a href=3D"https://click.example.com/track/open?mi_fname=3DAlex&amp;mi_=\r\n'
    b'lname=3DSample&amp;id=3D7">View your trip</a></p>\r\n'
    b'<p>Write to <a href=3D"mailto:alex.sample@example.org">alex.samp=\r\n'
    b"le@example.org</a> with any question.</p>\r\n"
    b"<p>Passenger: Alex Sam=\r\n"
    b"ple</p>\r\n"
    b"<p>Confirmation code: XZS=\r\n"
    b"975</p>\r\n"
    b"</body></html>\r\n"
)
UNWRAPPED = (
    "<html><body><p>Hello [NAME-1], it’s confirmed.</p>\n"
    '<p><a href="https://click.example.com/track/open?[QUERY]">View your trip</a></p>\n'
    '<p>Write to <a href="mailto:[EMAIL-3]">[EMAIL-3]</a> with any question.</p>\n'
    "<p>Passenger: [NAME-2]</p>\n"
    "<p>Confirmation code: [REF-1]</p>\n"
    "</body></html>"
)
WRAPPED_TEXT = (
    b"Passenger: Alex Sam=\r\n"
    b"ple\r\n"
    b"Confirmation code: XZS=\r\n"
    b"975\r\n"
    b"Receipt sent to alex.samp=\r\n"
    b"le@example.org\r\n"
)
UNWRAPPED_TEXT = "Passenger: [NAME-1]\nConfirmation code: [REF-1]\nReceipt sent to [EMAIL-3]"


def test_a_part_is_redacted_as_it_reads_however_it_is_sent():
    known = Known(names=("Alex Sample",))
    # the encoding as the standard names it, as a careless sender does, and not named at all
    for how in (b"quoted-printable", b"Quoted-Printable ", b"QUOTED-PRINTABLE (text)", b"7bit"):
        out = Redactor(known).email(sent(WRAPPED, how))
        assert read(out)[1]["text/html"].strip() == UNWRAPPED, how
        for gone in ("Alex", "Sample", "alex.samp", "XZS", "975", "=3D", "=E2", "=\n", "mi_"):
            assert gone not in out, (how, gone)
        out = Redactor(known).email(sent(WRAPPED_TEXT, how, b"text/plain"))
        assert read(out)[1]["text/plain"].strip() == UNWRAPPED_TEXT, how
    for how in (b"base64", b"BASE64 "):
        body = base64.encodebytes(quopri.decodestring(WRAPPED))
        out = Redactor(known).email(sent(body, how))
        assert read(out)[1]["text/html"].strip() == UNWRAPPED, how
    # text that is sent as it reads may hold an equals sign before two digits, in a price or in
    # a link's query: it is left alone
    plain = b"Total=40.00 USD, 2 nights=20.00 each, class=AB\r\n"
    out = Redactor(known).email(sent(plain, b"8bit", b"text/plain"))
    assert read(out)[1]["text/plain"].strip() == plain.decode().strip()
    page = (
        '<p width=100 bgcolor=FFFFFF><a href="https://www.example.com/a?x=3D8F&y=3D00">'
        f"Manage</a> {'your booking ' * 8}</p>"
    )
    out = Redactor(known).email(sent(page.encode(), b"8bit"))
    assert read(out)[1]["text/html"].strip() == (
        '<p width="100" bgcolor="FFFFFF"><a href="https://www.example.com/a?[QUERY]">'
        f"Manage</a> {'your booking ' * 8}</p>"
    ).strip()


def test_an_encoded_value_goes_from_a_links_path_and_from_the_text_beside_it():
    r = Redactor(Known())
    for address in ("alex@example.com", "alex.sample@example.org"):
        token = base64.urlsafe_b64encode(address.encode()).decode().replace("=", "~")
        assert 20 <= len(token) <= 32 and token.endswith("~")
        assert r.text(f"https://click.example.com/f/a/{token}/AAAAAA~~/Xx0Xx0Xx0") == (
            "https://click.example.com/f/a/[X]/[X]/[X]"
        )
        assert r.text(f"Open click.example.com/f/a/{token} to manage it") == (
            "Open click.example.[X] to manage it"
        )
        assert r.text(f"id {token} end") == "id [X] end"
        assert r.html(f'<img src="/o/{token}/pixel.gif" alt="">') == '<img src="[X].gif" alt="">'
    # words, and what a parser reads, are no encoded value however long they run
    for kept in (
        "Seats 12A/12B/12C/12D/12E/12F",
        "non-refundable-after-departure",
        "ExampleAirlinesVacationsClub",
        "MSP-SEA-MSP/AS9203/32A/32B",
    ):
        assert r.text(kept) == kept
    assert r.counts == {}


def test_an_american_address_goes_whole_whatever_its_street_is_called():
    whole = (
        "300 Example Way, Seattle, WA 98101",
        "1000 Example Hollow, Seattle, WA 98101",
        "300 Example Way NW, Suite 4, Seattle, WA 98101-1234",
        "12 Avenue of the Example, Minneapolis, MN 55401",
        "100 Sample Lane\nMinneapolis, MN 55401",
        "1000 EXAMPLE HOLLOW, SEATTLE WA 98101",
    )
    for address in whole:
        for country in ("", ", USA", ", United States"):
            for label in ("", "Address\n", "Billing address:\n"):
                r = Redactor(Known(names=("Alex Sample",)))
                shown = r.text(f"{label}{address}{country}\nPhone")
                assert shown == f"{label}[ADDRESS-1]\nPhone", (label, address, country)
    r = Redactor(Known())
    # with and without its town and its country, one address is one value
    assert r.text("1000 Example Hollow, Seattle, WA 98101, USA") == "[ADDRESS-1]"
    assert r.text("Address\n1000 Example Hollow\nPhone") == "Address\n[ADDRESS-1]\nPhone"
    # under a label on a line of its own, a line is an address whatever it looks like
    assert r.text("Hotel address:\n\nExample Point, North Shore\n") == (
        "Hotel address:\n\n[ADDRESS-2]\n"
    )
    # a town with its state and ZIP code is the end of an address wherever it stands; a count
    # or a flight's number before it is no house number
    for kept in ("Example Inn,", "2 Nights Example Inn,", "Flight 9203 Example Inn,", "AS 9203"):
        assert r.text(f"{kept} Seattle, WA 98101") == f"{kept} [ADDRESS-3]"
    assert r.text("AS 9203 Gate B, Seattle, WA 98101") == "AS 9203 Gate B, [ADDRESS-3]"
    # and on a page, where the label is the cell before and each line is a piece of its own
    page = (
        "<table><tr><td>Address</td><td>12 Example Cove<br>Seattle, WA 98101<br>USA</td></tr>"
        "<tr><td>Email address</td><td>alex@example.com</td></tr></table>"
    )
    assert r.html(page) == (
        "<table><tr><td>Address</td><td>[ADDRESS-4]<br>[ADDRESS-3]<br>USA</td></tr>"
        "<tr><td>Email address</td><td>[EMAIL-1]</td></tr></table>"
    )
    assert r.counts == {"address": 10, "email": 1}
    # a page's address is read as the reader sees it, so that its street goes from the line or
    # the cell it has to itself, whatever it is called
    r = Redactor(Known(names=("Alex Sample",)))
    page = (
        "<td>Alex Sample<br>77 Example Bayou<br>Seattle, WA 98101</td>"
        "<td><div>77 EXAMPLE BAYOU</div><div>SEATTLE WA 98101</div></td>"
        "<p>To <span>77 Example Bayou</span>, <span>Seattle</span>, <span>WA</span> 98101</p>"
    )
    assert r.html(page) == (
        "<td>[NAME-1]<br>[ADDRESS-1]<br>[ADDRESS-2]</td>"
        "<td><div>[ADDRESS-1]</div><div>[ADDRESS-2]</div></td>"
        "<p>To <span>[ADDRESS-1]</span>, <span>Seattle</span>, <span>WA</span> 98101</p>"
    )
    r = Redactor(Known())
    # what is no address: a label under a label, a town with no ZIP code, a word that is a state's
    for kept in ("Address\nCheck-in", "Pick up in Seattle, WA", "Sign IN 12345 here"):
        assert r.text(kept) == kept
    assert r.counts == {}


def test_what_a_page_hides_in_a_value_or_puts_in_a_tag_is_still_redacted():
    r = Redactor(Known(names=("Alex Sample",)))
    # a character nobody sees, written as itself or as an entity, splits no value
    assert r.html("<p>trips\u200b@example.com, Al&#8203;ex Sam&shy;ple</p>") == (
        "<p>[EMAIL-1], [NAME-1]</p>"
    )
    # what stands in angle brackets and is no tag of a page is the text it is
    assert r.html("<p>From: Pat <pat@example.net> <mailto:pat@example.net></p>") == (
        "<p>From: Pat <[EMAIL-2]> <mailto:[EMAIL-2]></p>"
    )
    assert r.html("<div>Booked by <Alex Sample></Alex></div>") == (
        "<div>Booked by <[NAME-1]></[NAME-2]></div>"
    )
    broken = r.html('<a href"https://www.example.com/x?first=Pat">Manage</a>')
    assert broken == '<a href"https://www.example.com/x?[QUERY]">Manage</a>'


def nested(depth: int) -> EmailMessage:
    """An email that carries an email that carries an email, `depth` deep; the innermost says
    which one it is."""
    inner = make_eml(
        from_="Example Desert Inn <reservations@exampledesertinn.example>",
        to="alex@example.com",
        subject="Reservation confirmed",
        text=f"Dear Alex Sample,\nConfirmation number: XTZ732\nlevel {depth}\n",
        headers={"X-Campaign": "k7q2m"},
    )
    for level in range(depth - 1, -1, -1):
        outer = make_eml(
            from_="Alex Sample <alex@example.com>",
            subject="Fwd: Reservation confirmed",
            text=f"Hotel confirmation attached, level {level}.\n",
            rfc822=inner,
        )
        inner = outer
    return inner


def test_an_attached_email_is_redacted_like_its_parent_and_depth_is_bounded():
    r = Redactor(Known(names=("Alex Sample",)))
    out = r.email(nested(1).as_bytes())
    msg, _ = read(out)
    attached = next(p for p in msg.walk() if p.get_content_type() == "message/rfc822")
    inner = attached.get_payload()[0]
    assert inner["From"] == "Example Desert Inn <[EMAIL-3]>"
    assert inner["To"] == "[EMAIL-1]"
    assert inner.get_payload().split("\n")[:2] == ["Dear [NAME-1],", "Confirmation number: [REF-1]"]
    assert "X-Campaign" not in out and "k7q2m" not in out and r.removed_headers == 1
    # each level down is redacted the same way, to a bound; past it the rest is one line
    deep = Redactor(Known()).email(nested(MAX_DEPTH + 3).as_bytes())
    assert f"level {MAX_DEPTH}." in deep and f"level {MAX_DEPTH + 1}." not in deep
    assert f"level {MAX_DEPTH + 3}\n" not in deep
    assert re.search(r"^\[REMOVED: \d+ more parts\]$", deep, re.M)
    # an attached email sent quoted-printable whole, a soft line break inside its own header
    carried = (
        b"From: Alaska Airlines <no-reply@alaskaair.com>\r\n"
        b"To: Alex Sample <alex.samp=\r\nle@example.org>\r\n"
        b"Subject: Your trip is confirmed\r\n\r\n"
        b"Passenger: Alex Sam=\r\nple, code=3DXZS975\r\n"
    )
    forward = sent(carried, b"quoted-printable", b"message/rfc822")
    out = Redactor(Known(names=("Alex Sample",))).email(forward)
    inner = read(out)[0].get_payload()[0]
    assert inner["To"] == "[NAME-1] <[EMAIL-3]>" and inner["Subject"] == "Your trip is confirmed"
    assert inner.get_payload().strip() == "Passenger: [NAME-1], code=[REF-1]"


def test_a_markup_block_is_still_json_after_redaction():
    markup = [
        MARKUP,
        {
            "@context": "https://schema.org",
            "@type": "LodgingReservation",
            "reservationNumber": 86420975,
            "underName": {"@type": "Person", "name": "Sam Sample", "email": "alex@example.com"},
            "url": "https://www.example.com/stay/ab12cd34?guest=x",
            "reservationFor": {
                "@type": "Hotel",
                "name": "Example Bay Hotel Seattle",
                "telephone": "+1 206-555-0130",
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "300 Example Way",
                    "addressLocality": "Seattle",
                    "postalCode": "98101",
                },
            },
            "checkinTime": "2027-06-14T15:00:00-07:00",
        },
    ]
    r = Redactor(Known())
    flight, stay = block_of(r.html(page(markup, "<p>Confirmation code: <b>XZS975</b></p>")))
    # nothing named these: the markup's own keys say what each is
    assert flight["reservationNumber"] == "[REF-1]" and flight["underName"]["name"] == "[NAME-1]"
    assert flight["reservationFor"] == MARKUP["reservationFor"]
    assert flight["@context"] == "https://schema.org"
    assert flight["reservationStatus"] == "https://schema.org/ReservationConfirmed"
    assert stay["reservationNumber"] == "[REF-2]"
    # what a key calls a reservation's number goes whole, even when it is no one token
    token = r.value({"ticketToken": "aztecCode:M1SAMPLE/SAM XZS975 MSPSEA", "ticketNumber": "12"})
    assert token == {"ticketToken": "[REF-3]", "ticketNumber": "12"}
    assert stay["underName"] == {"@type": "Person", "name": "[NAME-2]", "email": "[EMAIL-1]"}
    assert stay["url"] == "https://www.example.com/stay/[X]?[QUERY]"
    hotel = stay["reservationFor"]
    assert hotel["name"] == "Example Bay Hotel Seattle" and hotel["telephone"] == "[PHONE-1]"
    assert hotel["address"]["streetAddress"] == "[ADDRESS-1]"
    assert hotel["address"]["addressLocality"] == "Seattle"
    assert stay["checkinTime"] == "2027-06-14T15:00:00-07:00"
    # a block that names half a character is still written out
    odd = page({"@type": "Person", "name": "Sam \ud83d Sample"}, "")
    assert r.email(f"Content-Type: text/html\n\n{odd}".encode()).encode("utf-8")
    # a JSON-like value keeps its keys and its shape
    assert r.value({"passengers": ["SAMPLE/SAM"], "price": 1592.34, "kept": None}) == {
        "passengers": ["[NAME-2]"],
        "price": 1592.34,
        "kept": None,
    }


def test_a_long_line_and_many_parts_are_cut():
    msg = EmailMessage()
    msg["Subject"] = "Your trip"
    msg.set_content("fare rules " * 9_091 + "\nEnd of fare rules.\n")
    for n in range(299):
        msg.add_attachment(f"part {n}\n", subtype="plain", disposition="inline")
    out = Redactor(Known()).email(msg.as_bytes())
    lines = out.split("\n")
    assert max(map(len, lines)) == MAX_LINE + len("[CUT]")
    assert sum(line.endswith("[CUT]") for line in lines) == 1 and "End of fare rules." in out
    assert f"part {MAX_PARTS - 3}" in out and f"part {MAX_PARTS + 20}" not in out
    assert "[REMOVED: 101 more parts]" in out
    # a page all on one line is broken inside its tags, where a break changes nothing
    row = '<tr><td class="grid">Depart 09:30</td><td>Seattle</td></tr>'
    html = Redactor(Known()).html(f"<table>{row * 200}</table>")
    assert max(map(len, html.split("\n"))) < MAX_LINE
    assert html.replace("\n>", ">").replace("\n", " ") == f"<table>{row * 200}</table>"


def test_text_made_to_be_slow_is_redacted_in_the_time_its_length_takes():
    """No pattern reads a run of one character again from every place in it: forty thousand
    characters of each kind take about a second, where reading them so takes ten or more. Each
    case is timed on its own, so the bound holds on a slow machine and a total says nothing."""
    size = 40_000
    units = ("A", "x", "1", "a@", "Aa ", "<", "1 ", "(1)", "Reservation ", "x.", "-", "A1", "Q. ")
    units += (" ", "\t", "=\n", "=3D", "Aa, WA ", "Address\n", "<x@y>", "aB1", "\u200b")
    known = Known(names=("Alex Q Sample",), references=("XZS975",), addresses=("300 Example Way",))
    slowest = [0.0, ""]

    def timed(case: str, started: float) -> None:
        took = time.monotonic() - started
        if took > slowest[0]:
            slowest[:] = [took, case]

    for unit in units:
        started = time.monotonic()
        text = unit * (size // len(unit))
        r = Redactor(known)
        assert len(r.text(text)) <= len(text) + 16
        r.html(text)
        r.email(f"Subject: {text[:2000]}\n\n{text}".encode())
        redact.harvest(f"Subject: x\n\n{text}".encode())
        redact.pasted(text.encode())
        timed(repr(unit), started)
    # nor a run of white space after the start of what a pattern looks for
    starts = ("12 Example Way,", "12 Example Way, a", "Seattle,", "Seattle, WA", "Address")
    starts += ("Date of birth", "card ending", "prepared for", "Calle Ejemplo", "Passenger name")
    starts += ("Flight number",)
    for start in starts:
        started = time.monotonic()
        text = start + " " * size + ", x"
        Redactor(known).text(text)
        Redactor(known).html(text)
        redact.harvest_text(text)
        timed(start, started)
    # nor many numbers each held against every other, nor a label asked for once a token
    apart = "\n".join(f"000-{n // 1000:03d}-{n % 1000:04d}" for n in range(17_000))
    alike = "\n".join(f"{n:05d}-555-0142" for n in range(17_000))
    for name, text in (("apart", apart), ("alike", alike)):
        started = time.monotonic()
        assert Redactor(known).text(text).count("[PHONE-") == 17_000
        timed(name, started)
    started = time.monotonic()
    redact.harvest_text(" " * 20_000 + "ABCD/EFGH " * 2_000)
    timed("slash tokens", started)
    assert slowest[0] < 8, f"{slowest[1]} took {slowest[0]:.1f} s"


def test_learning_the_labels_of_a_text_made_of_them_holds_less_than_two_copies_of_it():
    """The labels of a text are read one at a time, never all held at once: a text of twenty
    thousand labels, each ending in the word for a flight, peaks at less than two copies of it
    (holding every label's match took over ten)."""
    text = "ref flight: A1 " * 20_000
    r = Redactor(Known())
    tracemalloc.start()
    try:
        r.learn_text(text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * len(text)


def test_a_header_or_a_nesting_made_too_deep_is_still_redacted():
    """Neither a name behind thousands of titles nor parts nested thousands deep stops a report,
    and what lies too deep to read is not carried."""
    titled = b'From: "' + b"Mr " * 3000 + b'Alex Sample" <alex@example.com>\nSubject: Fwd: x\n\nx\n'
    assert redact.harvest(titled) == ("Alex Sample",)
    depth = 1500
    shells = "".join(
        f'Content-Type: multipart/mixed; boundary="b{n}"\n\n--b{n}\n' for n in range(depth)
    )
    nested = f"Subject: Fwd: x\n{shells}Content-Type: text/plain\n\nGuest: Alex Sample\n".encode()
    assert redact.harvest(nested) in ((), ("Alex Sample",))
    r = Redactor(Known(names=("Alex Sample",)))
    shown = r.email(nested)
    assert "Alex" not in shown and "Sample" not in shown
    assert "more parts]" in shown or "[ATTACHMENT REMOVED: " in shown


def test_the_same_email_gives_the_same_report_twice():
    raw = make_eml(
        from_="Alaska Airlines <no-reply@alaskaair.com>",
        to="Alex Sample <alex@example.com>",
        subject="Your trip is confirmed: XZS975",
        text="Confirmation code: XZS975\nCall 206-555-0161\nTicket 0001234500001\n",
        html=page(MARKUP, '<a href="https://www.example.com/t/ab12?x=1">Alex Sample</a>'),
        attachments=[("e-ticket.pdf", "application/pdf", b"%PDF-1.4 x")],
    ).as_bytes()
    known = Known(names=OWNER, sender_domains=LISTED)
    one, two = Redactor(known), Redactor(known)
    first = one.email(raw)
    assert two.email(raw) == first
    assert one.counts == two.counts and one.removed_headers == two.removed_headers
    # and a second pass by the same redactor names every value as the first did
    assert one.email(raw) == first


def test_the_emails_own_names_are_harvested_and_a_senders_name_is_not():
    forward = (
        "---------- Forwarded message ---------\n"
        "From: Example Bay Hotel Seattle <stay@examplebayhotel.example>\n"
        "Date: Mon, Jan 18, 2027 at 10:31 AM\n"
        "Subject: Your stay is confirmed\n"
        "To: Pat Example <pat@example.net>\n\n\n"
        "Dear Guest,\n\nGuest name: Lee Example\nRoom: Two Queen Beds\n\n"
        "Passengers\n  Robin Sample        child\n  Jamie Sample (child)\n\n"
        "  From:  MINNEAPOLIS/ST PAUL    7:15 AM\n"
        "  Name                SAMPLE/SAM\n"
    )
    cues = (
        ("Adult 2: Pat Example\n", ("Pat Example",)),
        ("Itinerary prepared for LEE EXAMPLE and one guest.\n", ("LEE EXAMPLE",)),
        ("Thank you, Robin! Thank you for flying with us.\n", ("Robin",)),
        ("The room is held for Mrs Jamie Sample until 6:00 PM.\n", ("Jamie Sample",)),
        ('ATTENDEE;CN="Sam Sample":mailto:alex@example.com\n', ("Sam Sample",)),
        ("Your host: Lee Example\n", ("Lee Example",)),
        ("Your host Robin will meet you at the door.\n", ("Robin",)),
        ("Emergency contact: Sam Sample\n", ("Sam Sample",)),
        # several names behind one label, a name in two columns, a list under a table's head
        (
            "Passengers: MR ALEX SAMPLE, MRS SAM SAMPLE and Robin Sample (child)\n",
            ("ALEX SAMPLE", "SAM SAMPLE", "Robin Sample"),
        ),
        ("Travelers: 2 adults (Pat Example; Lee Example)\n", ("Pat Example", "Lee Example")),
        ("First name  Jamie    Last name  Sample\n", ("Jamie", "Sample")),
        (
            "Passenger name     Seat\n1. Alex Sample     32A\n- Sam Sample      32B\n\nTotal\n",
            ("Alex Sample", "Sam Sample"),
        ),
        # a name before an address of mail that reads as its owner's
        ("On Jan 18, 2027 at 10:02 AM Lee Example <lee@example.net> wrote:\n", ("Lee Example",)),
    )
    for line, names in cues:
        mail = make_eml(from_="Rail Exemple <billets@rail.example>", subject="x", text=line)
        assert redact.harvest(mail.as_bytes()) == names, line
    raw = make_eml(
        from_="Alex Sample <alex@example.com>",
        subject="Fwd: Your stay is confirmed",
        text=forward,
        html=page(MARKUP, "<p>Hello Alex,</p>"),
    ).as_bytes()
    names = redact.harvest(raw)
    assert set(names) == {
        "Alex Sample",
        "Pat Example",
        "Lee Example",
        "Robin Sample",
        "Jamie Sample",
        "SAMPLE/SAM",
        "Alex",
    }
    # a vendor writing from its own address is no traveller, and neither is a route
    vendor = make_eml(
        from_="Alaska Airlines <no-reply@alaskaair.com>",
        to="alex@example.com",
        subject="Your trip is confirmed",
        text="Hello,\n\n  From:  MINNEAPOLIS/ST PAUL    7:15 AM\n",
    ).as_bytes()
    assert redact.harvest(vendor) == ()


def test_what_is_known_comes_from_the_travellers_the_reading_and_what_was_typed():
    class Book:
        me_aliases = OWNER
        guests = (("Sam Sample", ("Sam Sample", "SAMPLE/SAM")),)

    reading = {
        "document_type": "booking",
        "reservations": [
            {
                "kind": "lodging",
                "confirmation_code": "YTR864",
                "passengers": ["Robin Sample"],
                "address": "300 Example Way, Seattle, WA 98101",
                "pickup_location": "MSP",
                "dropoff_location": "1 Example Loop, Seattle, WA 98101",
                "segments": [
                    {"passengers": ["Jamie Sample"], "seat_by_passenger": {"Lee Example": "32A"}}
                ],
            }
        ],
    }
    known = redact.known_from(Book(), reading, ["Pat Example"], ("Alex",), LISTED)
    assert known.names == (
        *OWNER,
        "Sam Sample",
        "SAMPLE/SAM",
        "Robin Sample",
        "Jamie Sample",
        "Lee Example",
        "Pat Example",
        "Alex",
    )
    assert known.references == ("YTR864",)
    assert known.addresses == (
        "300 Example Way, Seattle, WA 98101",
        "1 Example Loop, Seattle, WA 98101",
    )
    assert known.sender_domains == LISTED
    assert redact.known_from(None, None, (), (), ()) == Known()


def test_a_person_markup_names_by_its_key_alone_is_harvested_and_replaced():
    """`underName` and `customer` are often a plain string or an object with no type; the
    person is harvested all the same, and an organisation is not."""
    forms = (
        ("underName", "Robin Sample", ("Robin Sample",)),
        ("underName", {"name": "Robin Sample"}, ("Robin Sample",)),
        ("customer", {"name": "Robin Sample"}, ("Robin Sample",)),
        ("customer", [{"name": "Robin Sample"}, "Pat Example"], ("Robin Sample", "Pat Example")),
        ("customer", {"@type": "Organization", "name": "Sample Holdings"}, ()),
    )
    for key, value, names in forms:
        markup = {k: v for k, v in MARKUP.items() if k != "underName"} | {key: value}
        raw = (
            "Subject: Your flight\nContent-Type: text/html; charset=utf-8\n\n"
            + page(markup, "<p>Your flight is booked.</p>")
        ).encode()
        harvested = redact.harvest(raw)
        assert sorted(harvested) == sorted(names), (key, value)
        shown = Redactor(Known(names=harvested)).email(raw)
        assert "Robin" not in shown and "Pat Example" not in shown, (key, value)
