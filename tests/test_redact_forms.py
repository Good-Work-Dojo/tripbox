# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The forms a personal value takes that no one email of the corpus shows together: a label told
by its words in four languages, an address outside the United States, the signs between a
number's groups, a display name in an encoded word, a long reference, a calendar's folded lines.
Every value is invented."""

from __future__ import annotations

import base64
import email
import sys
import unicodedata

from tripbox import redact
from tripbox.redact import Known, Redactor


def test_an_address_label_is_told_by_its_words():
    lines = (
        "Rechnungsadresse:\nAm Beispielring 12\n80331 Beispielstadt",
        "Lieferadresse: Am Beispielhof 12, 80331 Beispielstadt",
        "Adresse de facturation : 4 Clos des Exemples, 75006 Exempleville",
        "Dirección de facturación: Urbanización Ejemplo 4, 28001 Ciudad Ejemplo",
        "Address line 1: 77 Example Bayou",
        "Address 1: 77 Example Bayou",
        "Billing address (line 2): Example House",
        "Indirizzo di fatturazione: Casa Esempio",
    )
    for line in lines:
        label = line[: line.index(":") + 1]
        between = line[len(label)][0]
        assert Redactor(Known()).text(f"{line}\nTel") == f"{label}{between}[ADDRESS-1]\nTel", line
    # under a label that ends its line, and in the cell after one on a page
    r = Redactor(Known())
    assert r.text("Adresse de livraison\nMaison Exemple, Hameau des Exemples\n") == (
        "Adresse de livraison\n[ADDRESS-1]\n"
    )
    assert r.html("<td>Lieferadresse</td><td>Haus Beispiel, Beispielhöhe</td>") == (
        "<td>Lieferadresse</td><td>[ADDRESS-2]</td>"
    )
    # an address of mail or of a page under such a label is what it is, and a sentence is none
    for kept, shown in (
        ("E-Mail-Adresse: trips@example.com", "E-Mail-Adresse: [EMAIL-1]"),
        ("Web address: https://www.example.com/", "Web address: https://www.example.com/"),
        ("Your address is on file and stays as it is", None),
    ):
        assert Redactor(Known()).text(kept) == (shown or kept)


def test_a_name_label_is_told_by_its_words():
    named = (
        ("Nom du passager : Pat Example", "Pat Example"),
        ("Nombre del pasajero: Lee Example", "Lee Example"),
        ("Name des Reisenden: Robin Sample", "Robin Sample"),
        ("Cardholder name: Robin Sample", "Robin Sample"),
        ("Name on card: Robin Sample", "Robin Sample"),
        ("Reservation holder: Robin Sample", "Robin Sample"),
        ("Adult 1 (lead): Robin Sample", "Robin Sample"),
        ("Nom et prénom du voyageur : Pat Example", "Pat Example"),
        ("  Traveller 2 name:   Lee Example   Seat 32A", "Lee Example"),
    )
    for line, name in named:
        assert redact.harvest_text(f"Your booking\n{line}\nSee you soon\n") == (name,), line
    # a label that names a thing, or says something else of a person, names nobody
    for line in (
        "Nom de l'hôtel : Exemple Palace",
        "Trip name: Summer Escape",
        "Passenger type: Adult",
        "Guest count: Two",
        "Customer service: Example Travel Desk",
        "Name der Unterkunft: Haus Beispiel",
        "Station name: Exampleton Central",
        "Guest parking: Available",
        "Guest Wi-Fi: Included",
        "Room name: Garden Suite",
    ):
        assert redact.harvest_text(f"{line}\n") == (), line


def test_an_address_outside_the_united_states_goes_by_its_postcode_and_town():
    whole = (
        "77 Example Bayou, Exampleton, ON Z9Z 9Z9",
        "77 Example Bayou\nExampleton ON  Z9Z 9Z9, Canada",
        "Beispielring 8, 80331 Beispielstadt",
        "Unter den Beispielen 77\n10117 Beispielstadt",
        "Via Esempio 12, 00100 Città Esempio",
        "12 Example Parade, Exampleton NSW 2000",
        "Voorbeeldhof 12, 1011 AB Voorbeeldstad",
        "4 Clos des Exemples, 75006 Exempleville",
        "Urbanización Ejemplo 4\n28001 Ciudad Ejemplo",
        "Am Beispielhof 12a, D-80331 Beispielstadt",
    )
    for address in whole:
        shown = Redactor(Known()).text(f"Hotel Beispiel\n{address}\nTel")
        assert shown == "Hotel Beispiel\n[ADDRESS-1]\nTel", address
    r = Redactor(Known())
    # a town with its province and postcode is the end of an address wherever it stands, and
    # the line of a Canadian postcode goes as the line of a British one does
    assert r.text("Example Inn, Exampleton, ON Z9Z 9Z9") == "Example Inn, [ADDRESS-1]"
    assert r.text("Example Inn\nBox 4, Z9Z 9Z9\n") == "Example Inn\n[ADDRESS-2]\n"
    # on a page the street and the postcode's line are pieces of their own
    page = "<p>Haus Beispiel<br>Beispielring 8<br>80331 Beispielstadt</p>"
    assert r.html(page) == "<p>Haus Beispiel<br>[ADDRESS-3]<br>[ADDRESS-4]</p>"
    # an address whose postcode and town another one gave away is still one address
    r = Redactor(Known())
    both = "Beispielring 8\n80331 Beispielstadt\n\nAm Beispielhof 12, 80331 Beispielstadt\n"
    assert r.text(both) == "[ADDRESS-1]\n\n[ADDRESS-2]\n"
    # what is no address: a year, a count, a flight's number before a town, a state's letters
    # before a year
    for kept in (
        "Monday 14\n2027 Seattle sailings",
        "Total nights 2\n1200 Points earned",
        "Flight AS 9203, 55401 Minneapolis",
        "Seattle, WA 2027 schedule",
        "2 Nächte, 80331 Beispielstadt",
        "Zimmer 204, 10117 Beispielstadt",
        "Sat, May 1, 2027   AA 9630   MSP 9:10 AM",
    ):
        assert Redactor(Known()).text(kept) == kept, kept


def test_a_phone_number_goes_whatever_stands_between_its_groups():
    r = Redactor(Known())
    written = (
        ("612‑555‑0142", 1),  # the hyphen that does not break a line
        ("612–555–0142", 1),  # an en dash
        ("612·555·0142", 1),  # a middle dot
        ("+33 1 99 00 41 27", 2),  # a narrow space that does not break
        ("+49 30 555 0142", 3),  # a thin space
        ("000‑1234567890", None),  # a ticket number
    )
    for phone, n in written:
        shown = "[TICKET-1]" if n is None else f"[PHONE-{n}]"
        assert r.text(f"Call {phone} now") == f"Call {shown} now", phone
    # a range of small numbers is no phone number
    kept = "Open 9–17, rows 12–14"
    assert r.text(kept) == kept


def test_a_phone_number_goes_with_any_space_or_dash_of_unicode_between_its_groups():
    r = Redactor(Known())
    for gap in ("\u2212", " \u2013 ", " - ", "\u2022", "\u2003", "\u2014", "\u3000", "\uff0d"):
        phone = gap.join(("612", "555", "0142"))
        assert r.text(f"Call {phone} now") == "Call [PHONE-1] now", repr(gap)


def test_the_gaps_between_digits_hold_every_space_and_dash_unicode_names():
    held = set(redact._SPACES) | set(redact._DASHES.replace("\\-", "-"))
    for code in range(sys.maxunicode + 1):
        if unicodedata.category(chr(code)) in ("Zs", "Pd"):
            assert chr(code) in held, f"U+{code:04X}"


def test_a_display_name_is_decoded_after_its_address_list_is_split():
    pat = base64.b64encode(b"Pat Sample").decode()
    raw = (
        "From: Alaska Airlines <no-reply@alaskaair.com>\n"
        "To: =?UTF-8?Q?S=C3=A4mple=2C_Robin?= <robin@example.net>,\n"
        f" =?UTF-8?B?{pat}?= <pat@example.net>\n"
        "Subject: Your trip\n\nHello\n"
    ).encode()
    assert redact.harvest(raw) == ("Sämple, Robin", "Pat Sample")
    out = Redactor(Known(names=redact.harvest(raw))).email(raw)
    assert email.message_from_string(out)["To"] == "[NAME-1] <[EMAIL-2]>, [NAME-2] <[EMAIL-3]>"


def test_a_reference_a_label_names_goes_up_to_forty_characters():
    named = (
        ("Booking reference: GYGA1B2C3D4E5", "GYGA1B2C3D4E5"),
        ("Confirmation number: ABCDEFGHJKLM", "ABCDEFGHJKLM"),
        ("Order number: AB12-CD34-EF56-GH78", "AB12-CD34-EF56-GH78"),
        ("Voucher: 7H2K9-QWXZ4-M3N8P-L6T5R-B1C0D-9F8G7", "7H2K9-QWXZ4-M3N8P-L6T5R-B1C0D-9F8G7"),
    )
    for line, code in named:
        r = Redactor(Known())
        shown = r.text(f"{line}\nQuote {code} when you call.")
        assert shown == f"{line[: -len(code)]}[REF-1]\nQuote [REF-1] when you call.", line
    # a day, a time and a word after such a label are no reference
    for kept in (
        "Booking date: 2027-06-14",
        "Booking made: 2027-06-14T09:30:00",
        "Reservation: non-refundable",
        "Booking: check-in from 15:00",
    ):
        assert Redactor(Known()).text(kept) == kept, kept


def test_a_calendar_is_redacted_as_it_reads_and_stays_a_calendar():
    """A calendar's lines are folded at 75 octets and its commas escaped: a value is found
    across a fold, and the part written back is still a calendar."""
    event = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:k7q2m0001x@calendar.example\r\n"
        "DTSTART:20270614T143000Z\r\n"
        "SUMMARY:Flight AS 9203 to Seattle\r\n"
        "DESCRIPTION:Confirmation code: XZS\r\n 975\\nPassenger: Alex Sample\\nCall 612-55\r\n"
        " 5-0142 or write to alex.sam\r\n\tple@example.org\\, any day\r\n"
        "LOCATION:77 Example Bayou\\, Seattle\\, WA 98101\r\n"
        'ATTENDEE;CN="Sample, Alex";RSVP=TRUE:mailto:alex@example.com\r\n'
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    head = (
        "From: Alaska Airlines <no-reply@alaskaair.com>\nSubject: Your trip\n"
        'MIME-Version: 1.0\nContent-Type: text/calendar; charset="utf-8"; method=REQUEST\n\n'
    )
    raw = (head + event).encode()
    # (the attendee's name and the passenger's are one name, taken once)
    assert redact.harvest(raw) == ("Sample, Alex",)
    out = Redactor(Known(names=("Alex Sample",))).email(raw)
    shown = email.message_from_string(out)
    assert shown.get_content_type() == "text/calendar"
    assert shown.get_payload().strip().split("\n") == [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:[EMAIL-2]",
        "DTSTART:20270614T143000Z",
        "SUMMARY:Flight AS 9203 to Seattle",
        "DESCRIPTION:Confirmation code: [REF-1]\\nPassenger: [NAME-1]\\nCall [PHONE-1] or"
        " write to [EMAIL-3]\\, any day",
        "LOCATION:[ADDRESS-1]",
        'ATTENDEE;CN="[NAME-1]";RSVP=TRUE:mailto:[EMAIL-4]',
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # a line that grows long is folded again, never inside a placeholder
    long = "DESCRIPTION:" + "Passenger Alex Sample\\, seat 32A\\; " * 20
    longer = raw.replace(b"SUMMARY:", long.encode() + b"\r\nSUMMARY:")
    out = Redactor(Known(names=("Alex Sample",))).email(longer)
    lines = email.message_from_string(out).get_payload().split("\n")
    assert max(map(len, lines)) <= 200 and out.count("[NAME-1]") == 22
    unfolded = "\n".join(lines).replace("\n ", "")
    assert "DESCRIPTION:" + "Passenger [NAME-1]\\, seat 32A\\; " * 20 in unfolded


def test_a_part_in_latin_letters_said_to_be_utf8_or_said_nothing_of_reads_as_written():
    """Windows-1252 bytes in a part that names UTF-8, or no charset, read as the letters they
    are, so a known name in them goes; what is UTF-8 beside them stays as it is."""
    r = Redactor(Known(names=("Robin Sämple",)))
    for header in ("Content-Type: text/plain; charset=utf-8\n", ""):
        raw = (
            f"Subject: Your stay\n{header}\n".encode()
            + "Café confirmed.\n".encode()
            + b"Guest: Robin S\xe4mple\nPrice: 120 \x80\n"
        )
        shown = r.email(raw)
        assert "Robin" not in shown and "mple" not in shown, header
        assert "Café confirmed." in shown and "120 €" in shown and "�" not in shown, header
    assert redact.pasted(b"Guest: Robin S\xe4mple\n") == "Guest: Robin Sämple\n"
