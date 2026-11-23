# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A copy of an email that can be shown to strangers: names, addresses, codes and numbers are
replaced by words in square brackets, and what a parser reads is kept.

`Redactor` is given what is already known of an email (`Known`: the travellers' names, the codes
and addresses a reading found) and then looks for more by rule. One original value gets one
placeholder (`[NAME-1]`, `[EMAIL-1]`, `[PHONE-1]`, `[REF-1]`, `[TICKET-1]`, `[ADDRESS-1]`,
`[NUMBER-1]`) wherever it stands, numbered by first appearance. The rules run in this order, and
a later rule never reads inside what an earlier one replaced:

1. known values: each name as a whole, in any case and order, with white space, `/` or `,`
   between its parts; each part of three or more letters wherever it is a word; a part of one or
   two letters only beside another part of its name; each known code, address and number;
2. addresses of mail, whole, except that the domain of a listed travel sender stays;
3. links: scheme and host stay, the query and fragment become `?[QUERY]`, and a path segment
   stays only when it is a plain word (any other becomes `[X]`, so an encoded value in a path
   goes whatever its length);
4. booking references a label names (confirmation, record locator, PNR, reservation, booking,
   itinerary and their German, French and Spanish forms): 5 to 40 letters and digits, hyphens
   between a code's groups, which then go wherever they stand; and a code no label names: 5 to
   10 capitals and digits holding both, unless it has the shape of a flight number (a
   carrier's two characters, `AS`, `B6` or `6E`, and up to four digits), a day or a weight, or a
   word such as `Flight` or `Seat` stands before it here or anywhere in the email;
5. ticket numbers; 6. phone numbers, whatever stands between their groups (a space or a hyphen
   of any kind, a full stop, a middle dot);
7. street addresses: a house number with a street word; a box at a post office; what a label
   calls an address, on the label's line, on the line under it or in the next cell of a page (a
   label is told by its words, in four languages: `Rechnungsadresse`, `Adresse de
   facturation`, `Address line 1`); an address told by its end, whatever its street is called:
   a town, a state and a postcode of the United States, Canada or Australia, with the country
   after them and what stands before them from the house number on, or a postcode and its town
   with a street before them, on the line or the line above (on a page, where a street has a
   line or a cell to itself, it is known from the page as it reads); the line of a British or
   a Canadian postcode;
8. any other run of six or more digits, a position on the map, an address on a network.

What a label calls personal goes whatever it looks like, before any of it could be kept as a
date: a date of birth, a passport's, a licence's or a member's number, the last digits of a
card, a door's code, a password. An encoded value becomes `[X]` wherever it stands: a run of
20 or more letters, digits and the signs of base64 (`+ / _ - = ~`) that holds a piece of 12 or
more mixing letters with digits, or capitals with small letters as no word does; and any such
run of 32 or more that holds a letter and a digit, or of 60 or more.

An email's text is redacted as it reads, never as it was sent: each text part is decoded first
(base64, quoted-printable, its charset), whatever its header calls the encoding, and written
out as UTF-8. Text that still reads as quoted-printable after that (a part that names no
encoding, the source of an email pasted without its headers) is decoded once more. A calendar
part's lines are put together again from where they were folded and its escaped commas read as
commas; it is written back as the calendar it is. A character nobody sees (a zero-width space,
a soft hyphen) between two characters of a value is dropped, and what stands in angle brackets
on a page and is no tag of one is redacted as text.

One redaction can hand the next what it found (`Redactor.found`, `Known.found`): a report
redacts its sections twice, so that a value a label names in one of them goes from the others.
Only what a label, a key, a street or a phone number's shape made a value is handed on: never a
code told by its shape alone, a value that reads as a flight's or a train's number, a year, or
a town with no more than its state.

Kept on purpose, because they are what a parser reads: a date or a time in any printed form, a
flight or train number, an airport code, a city, an airline's or a hotel's name, a price, a
seat. Kept because nothing tells them from those: a postcode that stands alone, a number of
five digits or fewer that no label names.

What it cannot do, so that nobody takes its output for more than it is:

- letters run together are one word: `SAMPLEALEX` stays, and so does a name inside a longer
  word or token (a key built from a property's name);
- a name nothing gave it (no travellers file, no reading, no greeting, no label, no title)
  stays, and so does anything personal said in a sentence: a request, a reason, a relation;
- a booking reference of letters alone stays unless a label names it or the reading held it;
  one that reads as a flight number (a carrier's two characters and up to four digits) stays
  likewise;
- a `SURNAME/GIVEN` token is taken for a name only beside a label, a title or a relative on the
  same booking: a pair of place names is printed the same way;
- a sender's display name is taken for a person's only when the address reads as a person's;
- a street stays when it has no street word the rules know, no label, and no postcode and
  town beside it; so does a town, or a postcode, that stands alone;
- a value a page's markup splits (`alex@<b>example.com</b>`) is read as its pieces;
- every attachment, one of text too (a list, a card, a page), is removed whole, nothing inside
  it read, except a calendar file, which is redacted like the body.

The module is pure: it imports nothing of the pipeline, the ledger or the settings."""

from __future__ import annotations

import base64
import binascii
import codecs
import datetime as dt
import email
import email.errors
import email.header
import email.message
import email.policy
import email.utils
import html as htmllib
import json
import quopri
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# What is counted: one per value replaced, and one per link whose query went.
KINDS = ("name", "email", "phone", "reference", "ticket", "address", "number", "link")
_WORD = {
    "name": "NAME",
    "email": "EMAIL",
    "phone": "PHONE",
    "reference": "REF",
    "ticket": "TICKET",
    "address": "ADDRESS",
    "number": "NUMBER",
}
QUERY, OPAQUE, MESSAGE_ID, CUT = "[QUERY]", "[X]", "[ID]", "[CUT]"
DOMAIN, HOST = "[DOMAIN]", "[HOST]"
# The headers a redacted email keeps, each value redacted; every other header is dropped.
KEPT_HEADERS = (
    "From",
    "To",
    "Cc",
    "Reply-To",
    "Sender",
    "Subject",
    "Date",
    "Message-ID",
    "MIME-Version",
    "Content-Type",
    "Content-Transfer-Encoding",
    "Content-Disposition",
    "Content-Language",
    "Return-Path",
    "X-Forwarded-To",
    "X-Forwarded-For",
    "Authentication-Results",
)
_KEPT = {name.lower(): name for name in KEPT_HEADERS}
# written anew for every part, from what the part is: never copied
_REWRITTEN = ("content-type", "content-transfer-encoding", "content-disposition")
MAX_DEPTH = 5  # emails attached to emails that are still redacted
MAX_PARTS = 200  # parts of one email that are still redacted
MAX_LINE = 2000  # characters of a line of a redacted email
_MAX_HEADER = 16 * 1024  # characters of a header's value that are read
_MAX_HEADERS = 400  # headers of one message that are read
_MAX_LEARNT = 500  # values an email's own labels and markup may add
_MAX_STREETS = 50  # streets an email's own addresses may add
# What one redaction of an email hands the next (`Redactor.found`), and how many of each kind.
_FOUND_KINDS = ("name", "reference", "address", "phone", "ticket", "number")
_MAX_FOUND = 100
_WRAP = 400  # a page's line is broken inside the next tag once it is this long
# The authentication server whose name says nothing of the mailbox's owner.
_PUBLIC_AUTHSERV = "mx.google.com"

# Booking-reference labels, as the start of a word (a German label goes on: Buchungsnummer).
REFERENCE_LABELS = (
    # English
    "confirmation",
    "record locator",
    "locator",
    "pnr",
    "reservation",
    "booking",
    "itinerary",
    "reference",
    "order",
    "voucher",
    "trip id",
    # German
    "bestätigung",
    "bestaetigung",
    "buchung",
    "reservierung",
    "vorgang",
    "auftrag",
    # French
    "réservation",
    "dossier",
    "itinéraire",
    "référence",
    # Spanish
    "confirmación",
    "confirmacion",
    "reserva",
    "reservación",
    "reservacion",
    "localizador",
    "itinerario",
    "referencia",
)


def _any_of(phrases: Iterable[str]) -> str:
    """A pattern for any of `phrases`, the longest first, the white space inside one of any
    width."""
    longest_first = sorted(phrases, key=len, reverse=True)
    return "|".join(re.escape(p).replace(r"\ ", r"[ \t]+") for p in longest_first)


# (`ref` alone is one too, as a whole word: `refund` is none)
_LABEL = re.compile(
    r"(?<![^\W\d_])(?:(?:" + _any_of(REFERENCE_LABELS) + r")[^\W\d_]{0,24}|ref(?![^\W\d_]))",
    re.I,
)
# What may stand between a label and the code it names: its number, code, # and no. forms.
_LABEL_TAIL = re.compile(
    r"(?:[\s:#=\-–—()\[\].*|]|\b(?:number|numbers|num|no|nr|n[°ºo]|code|id|ref|reference|locator"
    r"|numéro|numero|número|nummer|código|codigo|de|du|la|le|el|der|die|is|your|ihre|votre|su)\b)*",
    re.I,
)
_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
_WINDOW = 40  # characters after a label in which its code is looked for
_MAX_CODE = 40  # letters and digits of the longest code a label names
# Words in capitals that follow a label and are no code.
_NOT_A_CODE = frozenset(
    """
    confirmed canceled cancelled pending details detail number status change changed update
    updated summary receipt hotel hotels flight flights train trains ticket tickets total guest
    guests passenger traveler travelers traveller dates check rooms nights adult adults child
    children economy business first premium class policy terms notice reminder request record
    locator reference booking bookings itinerary reserved booked issued payment amount price
    monday tuesday wednesday thursday friday saturday sunday january february march april june
    july august september october november december montag dienstag mittwoch donnerstag freitag
    samstag sonntag januar februar märz maerz juni juli oktober dezember lundi mardi mercredi
    jeudi vendredi samedi dimanche janvier février fevrier avril juillet août aout septembre
    octobre novembre décembre decembre lunes martes miércoles miercoles jueves viernes sábado
    sabado domingo enero febrero marzo abril junio julio agosto septiembre octubre noviembre
    diciembre confirmada confirmado cancelada cancelado pendiente bestätigt bestaetigt
    storniert confirmée confirmee annulée annulee détails datos detalles estado statut
    """.split()
)
# A word before a token that says it is a flight, a train, a seat, a room: kept.
_KEPT_BEFORE = re.compile(
    r"(?:flight|flug|vol|vuelo|train|zug|tren|seat|sitz|platz|siège|asiento|gate|terminal|room"
    r"|zimmer|chambre|habitación|coach|wagen|voiture|coche|row)[\s.:#|]*$",
    re.I,
)
# A code no label names: 5 to 10 capitals and digits, holding both. Two letters and up to four
# digits are a flight number; digits before a few letters are a day, a weight or a time.
_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Z0-9]{0,9}[A-Z])(?=[A-Z0-9]{0,9}\d)[A-Z0-9]{5,10}(?![A-Za-z0-9])"
)
# A flight's number as an airline prints it glued: its two characters (two letters, or a letter
# and a digit either way round: `B6`, `6E`), up to four digits, a letter after them at most.
_FLIGHT_SHAPE = r"(?:[A-Z][A-Z0-9]|\d[A-Z])\d{1,4}[A-Z]?"
_NOT_A_CODE_SHAPE = re.compile(
    rf"{_FLIGHT_SHAPE}|\d+[A-Z]{{1,3}}|\d{{1,2}}H\d{{1,2}}M?"
    r"|(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{1,4}"
)
# What reads as a flight's or a train's number, glued or not: a carrier's or a train's letters
# (`AS`, `B6`, `6E`, `EZY`, `ICE`), then digits. One redaction never hands such a value to the
# next as a reference, nor a year.
_TRANSPORT = re.compile(r"(?:[A-Z]{1,4}|[A-Z]\d|\d[A-Z])[ \-]?\d{1,5}[A-Z]?", re.I)
_YEAR = re.compile(r"(?:19|20)\d{2}")
# A number the email itself calls a flight's or a train's, which is then one wherever it stands.
_NAMED_TRANSPORT = re.compile(
    r"(?<![^\W\d_])(?i:flight|flug|vol|vuelo|train|zug|tren)(?:[ \t]+(?i:number|no\.?|nr\.?|#))?"
    r"[ \t.:#]{1,3}((?:[A-Z]{1,4}|[A-Z]\d|\d[A-Z])\d{1,5}[A-Z]?)(?![A-Za-z0-9])"
)
_MAX_TRANSPORT = 200  # such numbers that one email may name
# The word for a flight or a train that ends a reference's own label (`Booking reference for
# your return flight:`, `Código de reserva de su vuelo:`, `Buchungscode Flug:`), matched on what
# stands between the label and its code: anything but a colon, a digit or a line break, then the
# word and a colon. A code after such a label is the label's, never a flight's or a train's.
_LABEL_ENDS_IN_TRANSPORT = re.compile(
    r"[^\n\d:]*?(?<![^\W\d_])(?:flight|flug|vol|vuelo|train|zug|tren)"
    r"(?:[ \t]+(?:number|no\.?|nr\.?))?[ \t]*:[ \t]*",
    re.I,
)
# how far a label's own words may run on its line before the word for a flight or a train that
# ends it (`Confirmation code for your Example Air Lines return flight:`), and how far before a
# code such a label may begin: that, and the longest a label match runs
_OWNED_STRETCH = 160
_LABEL_REACH = _OWNED_STRETCH + 64
# What a label calls personal. A document's number follows its label; a member's or a
# customer's number only a label that says `number`, `no.`, `#` or `:`; a door's code or a
# password only after `:`, `=` or `is`.
_DOCUMENTS = (
    r"passport|passeport|reisepass|pasaporte|driver['’]?s?[ \t]+licen[cs]e|licen[cs]e"
    r"|führerschein|fuehrerschein|permis[ \t]+de[ \t]+conduire|licencia|known[ \t]+travell?er"
    r"|ktn|redress|national[ \t]+id|personalausweis|ausweis|dni|tax[ \t]+id"
)
_MEMBERSHIPS = (
    r"member(?:ship)?|rewards?|loyalty|frequent[ \t]+fl[yi]er|mileage(?:[ \t]+plan)?|skymiles"
    r"|aadvantage|mileageplus|rapid[ \t]+rewards|bonvoy|honors|trueblue|account|customer"
    r"|kunden|client|fidélité|fidelite|adhérent|socio|vielflieger|avios"
)
_SECRETS = (
    r"door[ \t]+code|gate[ \t]+code|access[ \t]+code|entry[ \t]+code|key[ \t]*code"
    r"|lock[ \t]?box(?:[ \t]+code)?|pin(?:[ \t]+code)?|passcode|password|passwort|kennwort"
    r"|mot[ \t]+de[ \t]+passe|contraseña|code[ \t]+d['’]accès|türcode|zugangscode"
    r"|código[ \t]+de[ \t]+acceso|wi-?fi[ \t]+(?:password|key|code|passwort|schlüssel)"
    r"|wlan-?(?:passwort|schlüssel|code)"
)
_ID_LABEL = re.compile(
    rf"(?<![^\W\d_])(?:(?P<document>{_DOCUMENTS})(?![^\W\d_])"
    rf"|(?P<member>{_MEMBERSHIPS})[^\W\d_]{{0,24}}|(?P<secret>{_SECRETS})(?![^\W\d_]))",
    re.I,
)
_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{2,22}[A-Za-z0-9]")
_SECRET_TOKEN = re.compile(r"\S{3,40}")
_SAYS_NUMBER = re.compile(r"[:#=\t]|num|n[°º]|\bno\b|\bnr\b|\bid\b", re.I)
_BIRTH = re.compile(
    r"(?i:(?<![^\W\d_])(?:date[ \t]+of[ \t]+birth|d\.o\.b\.?|dob|birth[ \t]?date|birthday"
    r"|born(?:[ \t]+on)?|geburtsdatum|geboren(?:[ \t]+am)?|date[ \t]+de[ \t]+naissance"
    r"|n[ée]\(?e?\)?[ \t]+le|fecha[ \t]+de[ \t]+nacimiento|nacid[oa](?:/a)?[ \t]+el))"
    r"[ \t]*(?:[:=][ \t]*)?"
    r"(\d{1,4}[./\-]\d{1,2}[./\-]\d{1,4}|\d{1,2}\.?[ \t]?[^\W\d_]{3,10}\.?,?[ \t]?\d{2,4}"
    r"|[^\W\d_]{3,10}\.?[ \t]\d{1,2}(?:st|nd|rd|th)?,?[ \t]\d{4}|\d{8})"
)
# The last digits of a card: after a word for `ending` with a word for a card shortly before
# it, or after the signs that stand for the digits left out.
_CARD_END = re.compile(
    r"(?i:ending(?:[ \t]+(?:in|with))?|ends[ \t]+in|terminaci[oó]n|terminada[ \t]+en"
    r"|se[ \t]+terminant[ \t]+par|finissant[ \t]+par|endziffern|endet[ \t]+auf"
    r"|last[ \t]+(?:4|four)(?:[ \t]+digits)?)[ \t:]*(?:\n[ \t]*)?(?<!\d)(\d{4})(?!\d)"
)
_CARD_MASK = re.compile(
    r"(?<![*•·●○◦∙#…xX.])(?:[*•·●○◦∙#]{2,40}|…{1,4}|\.{3,40}|[xX]{4,40})[ \t\-]?"
    r"(?<!\d)(\d{4})(?!\d)"
)
# Signs too few or too common to stand for a card's digits alone (`XX1234`, `(1234)`): they do
# only shortly after a word for a card.
_CARD_NEAR = re.compile(r"(?<![\w*])(?:[xX]{2,3}[ \t\-]?|[(\[][ \t]?)(\d{4})(?!\d)")
_CARD_WORD = re.compile(
    r"\b(?:card|carte|karte|kreditkarte|tarjeta|visa|mastercard|amex|american[ \t]+express)\b",
    re.I,
)
# A placeholder in a string being worked on is one opaque run of private-use characters, so that
# no later pattern reads inside it; `_Work` holds what each stands for.
_OPEN, _CLOSE, _ZERO = "\ue000", "\ue001", 0xE100
_MARK = "\ue000[\ue100-\ue109]+\ue001"
_MARK_RE = re.compile(_MARK)
_PRIVATE = re.compile("[\ue000-\uf8ff]")
_OUT_OF_SIGHT = "\ue002"  # stands for one character of what no rule may read
# A position on the map, an address on a network.
_PLACES = re.compile(
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"
    r"|(?<![\w.])-?\d{1,3}\.\d{4,8}(?![\w.])"
    r"|(?<![\w.])\d{1,3}[ \t]?°[ \t]?\d{1,2}(?:[.,]\d+)?[ \t]?['′]?"
    r"(?:[ \t]?\d{1,2}(?:[.,]\d+)?[ \t]?[\"″])?"
    r"|(?<![\w:.])(?=[0-9A-Fa-f:]{0,45}::|(?:[0-9A-Fa-f]{1,4}:){4})"
    r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"
)
# What a label calls an address is one, whatever it looks like: the rest of the label's line
# after its colon; and, when it holds a digit or a comma (so that the next label is no address),
# the line under a label that ends its own line, or the cell after one on a page. A label is
# told by its words: a word for an address (alone, or the end of a German one:
# `Rechnungsadresse`), up to three words before it and what a label goes on with after it
# (`de facturation`, `line 1`, `(line 2)`). Before a colon that may be any few words; with no
# colon, only a number, words in brackets or `de …`, so that a sentence is no label.
_ADDRESS_WORD = (
    r"(?:[^\W\d_]{0,24}(?:adresse|anschrift)|address|direcci[oó]n|domicilio|indirizzo|adres)"
)
_ADDRESS_LABEL = rf"[ \t>*•\-]*(?:[^\W\d_][\w'’\-]{{0,24}}[ \t]+){{0,3}}{_ADDRESS_WORD}"
_ADDRESS_GOES_ON = r"(?:[ \t]+[\w'’()\-]{1,20}){0,4}"
_ADDRESS_NUMBERED = (
    r"(?:[ \t]+(?:(?:line|ligne|zeile|l[ií]nea|riga)[ \t]*)?\d|[ \t]+\([^)\n]{1,20}\)"
    r"|[ \t]+d(?:e|u|es|el|i|ella)[ \t]+[^\W\d_]{2,20}|[ \t]+post(?:al|ale))?"
)
_ADDRESS_LINE = re.compile(
    rf"^({_ADDRESS_LABEL}{_ADDRESS_GOES_ON}[ \t]*:[ \t]*)(\S[^\n]{{2,120}})$", re.I | re.M
)
_ADDRESS_BELOW = re.compile(
    rf"^({_ADDRESS_LABEL}(?:{_ADDRESS_GOES_ON}[ \t]*:|{_ADDRESS_NUMBERED})[ \t]*\n(?:[ \t]*\n)?"
    r"[ \t>*•\-]*)(\S[^\n]{2,120})$",
    re.I | re.M,
)
_ADDRESS_CELL = re.compile(rf"{_ADDRESS_LABEL}{_ADDRESS_GOES_ON}[ \t]*:?", re.I)
_CELL_LINE = re.compile(r"\S[^\n]{2,120}")
_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
# how a month's name starts, the four languages
_MONTH_WORDS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|mär|mrz|mai|okt|dez|fév|fev|avr|juin|juil"
    "|août|aout|déc|ene|abr|ago|dic"
)
_CURRENCY = (
    r"(?:USD|EUR|GBP|CAD|AUD|NZD|CHF|JPY|CNY|MXN|SEK|NOK|DKK|PLN|CZK|HUF|BRL|INR|SGD|HKD|KRW|THB"
    r"|ZAR|AED|US\$|CA\$|A\$|[$€£¥])"
)
_AMOUNT = r"(?:\d{1,3}(?:[.,'\xa0\u202f]\d{3}){1,3}(?:[.,]\d{1,2})?|\d{1,9}(?:[.,]\d{1,2})?)"

# Street words, the four languages. English and German ones are read as written (a capital
# first); French and Spanish ones in any case, since a number or a name stands beside them.
STREET_WORDS = {
    "en": (
        "Street St Avenue Ave Road Rd Boulevard Blvd Drive Dr Lane Ln Way Parkway Pkwy Court Ct"
        " Place Pl Square Sq Terrace Ter Highway Hwy Circle Cir Trail Trl Close Crescent Row"
        " Walk Gardens Plaza Loop Alley Pike Turnpike Route Expressway Freeway Quay Wharf Mews"
        " Cove Cv Bend Bnd Hollow Holw Ridge Rdg Crossing Xing Canyon Cyn Creek Crk Glen Gln"
        " Vista Heights Hts Landing Lndg Grove Grv Bluff Blf Trace Trce Meadow Meadows Valley"
        " Vly Knoll Pointe Parade Esplanade Promenade"
    ).split(),
    # (with the Dutch words that are built the same way)
    "de": (
        "straße strasse str. weg platz allee gasse damm ufer chaussee markt straat laan plein"
        " gracht kade singel dijk"
    ).split(),
    "fr": "rue avenue av. boulevard bd bd. chemin allée impasse quai faubourg esplanade".split(),
    # French words that are English ones too: a street only with `de`, `du`, `des` after them
    "fr_de": "place route cours passage square".split(),
    "es": (
        "calle c/ avenida avda avda. av. paseo camino carrera carretera ctra ctra. ronda"
        " travesía rambla calzada callejón prolongación privada vía rua viale piazza corso"
        " praça travessa"
    ).split(),
    # Spanish words a hotel's name may hold: a street only at the start of what is printed
    "es_loose": "plaza pza pza. blvd blvd. boulevard bulevar".split(),
}
_UK_POSTCODE = r"[A-Z]{1,2}\d[A-Z\d]?[ \t]?\d[A-Z]{2}"
_TOWN = r"[^\W\d_][\w'’.\-]{0,30}(?:[ \t][^\W\d_][\w'’.\-]{0,30}){0,2}"
# After a street: at most one more piece of the address, then its postcode and town. The white
# space between two pieces is a few characters, or a line end and the next line's indent: a run
# of it without end is read once, never again from each place in it.
_GAP = r"[ \t]{0,8}(?:,|\n)[ \t]{0,40}"
# the United States, as an address names the country after its ZIP code
_US_COUNTRY = (
    r"(?:,?[ \t]{1,4}(?:USA|U\.S\.A\.?|US|U\.S\.|United[ \t]States(?:[ \t]of[ \t]America)?)"
    r"(?!\w))?"
)
_CA_PROVINCES = "AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT"
_CA_POSTCODE = r"[A-Z]\d[A-Z][ \-]?\d[A-Z]\d"
_CANADA = r"(?:,?[ \t]{1,4}Canada(?!\w))?"
_AU_STATES = "NSW|VIC|QLD|TAS|ACT|SA|WA|NT"
_AUSTRALIA = r"(?:,?[ \t]{1,4}Australia(?!\w))?"
_TAIL = (
    rf"(?:{_GAP}[^\n,]{{1,40}})??{_GAP}(?:"
    rf"{_TOWN},?(?:[ \t]{{0,8}}|[ \t]{{0,8}}\n[ \t]{{0,40}})[A-Z]{{2}}[ \t]{{1,8}}\d{{5}}"
    rf"(?:-\d{{4}})?(?![\w-]){_US_COUNTRY}"
    rf"|{_TOWN},?[ \t]{{1,8}}{_UK_POSTCODE}(?!\w)"
    rf"|{_TOWN},?[ \t]{{1,8}}(?:{_CA_PROVINCES}),?[ \t]{{1,8}}{_CA_POSTCODE}(?!\w){_CANADA}"
    rf"|{_TOWN},?[ \t]{{1,8}}(?:{_AU_STATES}),?[ \t]{{1,8}}\d{{4}}(?![\w-]){_AUSTRALIA}"
    rf"|(?:[A-Z]{{1,2}}-)?\d{{4,5}}[ \t]{{1,8}}{_TOWN}"
    r")"
)


# An address of the United States is told by its end, whatever its street is called: a town, a
# state's two letters and a ZIP code, and the country when it follows on the line.
_US_STATES = frozenset(
    (
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ"
        " NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP AA AE AP"
    ).split()
)
# The states whose two letters are a word as well: a town's only after a comma.
_US_WORDS = frozenset("AS HI ID IN ME OK OR".split())
_US_CITY = (
    r"[A-Z][\w'’.\-]{0,30}(?:[ \t](?:[A-Z][\w'’.\-]{0,30}|d[eu]|del|la|of|the)(?![\w'’.\-]))"
    r"{0,3}"
)
# Canada and Australia write an address as the United States do: a town, a province's or a
# state's letters, a postcode. An Australian postcode is four digits, which a year is too, so
# it is one only when it starts as its state's postcodes do.
_TOWN_STATE = re.compile(
    rf"(?<![\w'’.\-])(?:{_MARK}|{_US_CITY})"
    r"(?P<comma>,)?(?:[ \t]{1,4}|[ \t]{0,4}\n[ \t]{0,40}|(?<=,))(?P<state>[A-Z]{2,3}),?[ \t]{1,4}"
    rf"(?:(?P<us>\d{{5}})(?:-\d{{4}})?(?![\w\-]){_US_COUNTRY}"
    rf"|(?P<ca>{_CA_POSTCODE})(?![\w\-]){_CANADA}|(?P<au>\d{{4}})(?![\w\-]){_AUSTRALIA})"
)
_CA_STATES = frozenset(_CA_PROVINCES.split("|"))
_AU_STARTS = {
    "NSW": "12", "ACT": "02", "VIC": "38", "QLD": "49", "SA": "5", "WA": "6", "TAS": "7", "NT": "0"
}
# Elsewhere the postcode comes first: four or five digits (a country's letters before them, two
# letters after a Dutch one), then the town. So many things are written that way that it is
# the end of an address only with a street beside it: before it on the line, or on the line
# above.
_CAPITAL = "A-ZÀ-ÖØ-Þ"
_POSTCODE_TOWN = re.compile(
    r"(?<![\w.\-/])(?P<code>(?:[A-Z]{1,2}-)?\d{4,5}(?:[ ]?[A-Z]{2}(?![A-Za-z]))?)[ \t]{1,4}"
    rf"(?P<town>[{_CAPITAL}][\w'’.\-]{{2,30}}(?:[ \t](?:[{_CAPITAL}][\w'’.\-]{{0,30}}"
    r"|am|an|der|de|del|di|la|le|les|sur|sous|en|im|in|bei|ob|vor|aan|den|op)(?![\w'’.\-])){0,3})"
)
# A street as it is written there: its number last (`Beispielring 8`, `Via Esempio 12a`) or
# first (`4 Clos des Exemples`).
_NUMBER_LAST = re.compile(r"(?<![\w.,\-/#])\d{1,4}(?:[ ]?[a-zA-Z])?(?:[\-/]\d{1,4})?$")
_NUMBER_FIRST = re.compile(r"\d{1,4}(?:[ ]?(?:bis|ter|[a-zA-Z]))?,?[ \t]+(?=[^\W\d_])")
# A house number: digits (12, 12B, 12-14, 12 1/2) before a word that starts as a street's name does.
_HOUSE = re.compile(
    rf"(?<![\w.,\-/#])\d{{1,6}}(?:[A-Za-z]|[\-/]\w{{1,4}}|[ \t]\d/\d)?[ \t]+(?={_MARK}|[A-Z0-9])"
)
# After a number, a word that says it counts something or is a day of a month: no house number.
_COUNTED = re.compile(
    r"(?i:nights?|rooms?|guests?|adults?|child(?:ren)?|kids?|infants?|passengers?|travell?ers?"
    r"|people|persons?|bags?|pieces?|items?|tickets?|seats?|days?|hours?|hrs?|minutes?|mins?"
    r"|miles|points|stars?|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    # and in German, French and Spanish
    r"|n[aä]chte?|zimmer|personen|erwachsener?|kind(?:er)?|g[aä]ste?|tage?|stunden?|uhr"
    r"|januar|februar|m[aä]rz|mai|juni|juli|oktober|dezember"
    r"|nuits?|chambres?|personnes?|adultes?|enfants?|jours?|heures?|janvier|f[eé]vrier|mars"
    r"|avril|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre"
    r"|noches?|habitaci[oó]n(?:es)?|personas?|adultos?|ni[nñ]os?|d[ií]as?|horas?|enero|febrero"
    r"|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b"
)
# An airline's two characters before a number: a flight's, not a house's.
_CARRIER_BEFORE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9][ \t]?$")
_STREET_SPAN = 160  # characters before a town in which its street is looked for


def _street_words(lang: str, *, cased: bool) -> str:
    words = sorted(STREET_WORDS[lang], key=len, reverse=True)
    if not cased:
        return "|".join(re.escape(w) for w in words)
    forms = {form for w in words for form in (w.title(), w.upper())}
    return "|".join(re.escape(w) for w in sorted(forms, key=len, reverse=True))


_EN_STREET = _street_words("en", cased=False)
_STREETS = re.compile(
    "|".join(
        (
            # 300 Example Way; 100 N Example St, Suite 4
            rf"(?<![\w.,\-/#])\d{{1,6}}[A-Za-z]?[ \t]+(?:(?:{_MARK}|[A-Z0-9][\w'’.\-]{{0,30}})"
            rf"[ \t]+){{1,4}}?(?:{_EN_STREET}|{_EN_STREET.upper()})\b\.?"
            r"(?:,?[ \t]*(?:Apt|Apartment|Suite|Ste|Unit|Floor|Fl|#)\.?[ \t]*[\w\-]{1,8})?",
            # 12 rue de l'Exemple; 3 place de l'Exemple
            r"(?<![\w.,\-/#])\d{1,4}(?:[ \t]?(?:bis|ter))?,?[ \t]+(?i:(?:"
            + _street_words("fr", cased=False)
            + r")|(?:"
            + _street_words("fr_de", cased=False)
            + r")(?=[ \t]+(?:de|du|des|d['’])))[ \t]+[^\n,]{2,40}",
            # Beispielstraße 5; Berliner Straße 12a (a seat or a parking place is no street)
            r"(?<![\w/#])(?:[A-ZÄÖÜ][\wäöüß.\-]{0,40}[ \t]+){0,3}?"
            r"(?!(?i:sitz|steh|stell|park|liege|schlaf|fenster|gang|flug|arbeits)platz)"
            r"[A-ZÄÖÜ][\wäöüß.\-]{0,40}?(?i:"
            + _street_words("de", cased=False)
            + r")[ \t]+\d{1,4}(?:[ \t]?[a-zA-Z])?(?![\w.:,]\d|\w)",
            r"(?<![\w/#])(?:[A-ZÄÖÜ][\wäöüß.\-]{0,40}[ \t]+){1,3}(?:"
            + _street_words("de", cased=True)
            + r")[ \t]+\d{1,4}(?:[ \t]?[a-zA-Z])?(?![\w.:,]\d|\w)",
            # Calle del Ejemplo, 12; Blvd. Ejemplo Km 9
            r"(?<![\w/#])(?i:"
            + _street_words("es", cased=False)
            + r")[ \t]+[^\n,\d]{2,40}?(?:,?[ \t]*(?:(?:[Nn][ºo°]\.?|[Nn]úm\.?|#)[ \t]*)?\d{1,5}"
            r"|[ \t]+[Kk][Mm]\.?[ \t]*\d{1,4}(?:[.,]\d)?|[ \t]+s/n)(?![\w.:,]\d|\w)",
            r"(?<![\w/#])(?<![^\W\d_][ \t])(?i:"
            + _street_words("es_loose", cased=False)
            + r")[ \t]+[^\n,\d]{2,40}?(?:[ \t]*(?:(?:[Nn][ºo°]\.?|[Nn]úm\.?|#)[ \t]*)?\d{1,5}"
            r"|[ \t]+[Kk][Mm]\.?[ \t]*\d{1,4}(?:[.,]\d)?|[ \t]+s/n)(?![\w.:,]\d|\w)",
            # P.O. Box 1234; Postfach 1234
            r"(?<![\w/#])(?i:P\.?[ \t]?O\.?[ \t]?Box|Postfach|Apartado(?:[ \t]+de[ \t]+Correos)?"
            r"|Bo[îi]te[ \t]+postale|B\.P\.)[ \t]+\d{1,7}(?!\d)",
        )
    )
)
_TAIL_RE = re.compile(_TAIL)
_TOWN_FOUND = re.compile(rf"(?:{_GAP}[^\n,\ue000-\uf8ff]{{1,40}})??{_GAP}({_MARK})")
# A British postcode, or a Canadian one: its line is an address.
_POSTCODE = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_UK_POSTCODE}|{_CA_POSTCODE})(?![A-Za-z0-9])"
)

_LETTERS = re.compile(r"[^\W\d_]+")
_ALNUM = re.compile(r"[^\W_]+")
# what may stand between two parts of one name, on one line
_JOIN = re.compile(r"[ \t\xa0]{0,2}[/,.\-'’_]?[ \t\xa0]{0,2}")
_ATEXT = r"[\w!#$%&'*+=?^`{|}~.\-]"
_EMAIL = re.compile(
    rf"(?:{_MARK}|[^\W_])(?:{_MARK}|{_ATEXT}){{0,63}}@"
    rf"(?:{_MARK}|[\w\-]){{1,63}}(?:\.(?:{_MARK}|[\w\-]){{1,63}}){{1,8}}"
)
_LINK = re.compile(
    rf"(?:(?:https?|ftp)://|(?<![\w.@/])www\.)(?:{_MARK}|[^\s<>\"'`\\^{{}}|\[\]\ue000-\uf8ff])+",
    re.I,
)
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?")
# A path segment that is a plain word: lower-case words, or words that each start with one
# capital, joined by nothing or hyphens, with a file's suffix or none.
_SEGMENT = re.compile(
    r"(?:[a-z]+|[A-Z][a-z]{2,})(?:-?(?:[a-z]+|[A-Z][a-z]{2,}))*(?:\.[A-Za-z][A-Za-z0-9]{0,4})?"
)
_KEEP = re.compile(
    "|".join(
        (
            r"(?P<iso>(?<![\d\-])\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,6})?)?"
            r"(?:Z|[+\-]\d{2}:?\d{2})?)?(?!-?\d))",
            r"(?P<stamp>(?<![0-9A-Za-z])\d{8}T\d{4,6}Z?(?![0-9A-Za-z]))",
            r"(?P<day>(?<!\d)\d{8}(?!\d))",
            r"(?P<dmy>(?<![\d./\-])\d{1,2}(?P<s1>[./\-])\d{1,2}(?P=s1)(?:\d{4}|\d{2})"
            r"(?![./\-]?\d))",
            r"(?P<ymd>(?<![\d./\-])\d{4}(?P<s2>[./])\d{1,2}(?P=s2)\d{1,2}(?![./\-]?\d))",
            r"(?P<years>(?<![\d./\-])(?:19|20)\d{2}[\-/–](?:19|20)\d{2}(?![./\-]?\d))",
            rf"(?P<mon>(?<![0-9A-Za-z])\d{{1,2}}(?:{_MONTHS})\d{{2}}(?:\d{{2}})?(?![0-9A-Za-z]))",
            rf"(?P<mdy>(?<![^\W\d_])(?:{_MONTH_WORDS})[^\W\d_]{{0,7}}\.?[ \t]+\d{{1,2}}"
            r"(?:st|nd|rd|th)?,?[ \t]+(?:19|20)\d{2}(?!\d))",
            r"(?P<time>(?<![\d:.,])\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]?[AP]\.?M\b\.?)?(?![:.]?\d))",
            r"(?P<ampm>(?<![\d:.,])\d{1,2}[ \t]?[AP]\.?M\b)",
            r"(?P<hour>(?<![\d:.,])\d{1,2}[ \t]?h(?:[ \t]?\d{2})?(?![0-9A-Za-z]))",
            r"(?P<dec>(?<![\d.,\-/])\d{1,3}(?:,\d{3})*\.\d{2}(?!\d|[.,\-/]\d))",
            r"(?P<dec2>(?<![\d.,\-/])\d{1,3}(?:\.\d{3})*,\d{2}(?!\d|[.,\-/]\d))",
        )
    ),
    re.I,
)
# a price: a currency's code is read in capitals only (`try` is a word)
_PRICE = re.compile(
    rf"{_CURRENCY}[ \t\xa0]{{0,2}}{_AMOUNT}(?!\d|[.,]\d)"
    rf"|(?<![\d.,])(?<!\d[ \-]){_AMOUNT}[ \t\xa0]?{_CURRENCY}"
)
_CSS_COLOUR = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
_CSS_ATTRS = frozenset({"style", "bgcolor", "color", "text", "link", "vlink", "alink"})
# A run of the characters an encoded value is written in (base64's, a link's, and the `~` some
# senders pad with), and the pieces of one that `_encoded` reads.
_LONG_RUN = re.compile(r"(?<![A-Za-z0-9+/_=~\-])[A-Za-z0-9+/_=~\-]{20,}(?![A-Za-z0-9+/_=~\-])")
_PIECE = re.compile(r"[A-Za-z0-9]{12,}")
_CASED = re.compile(r"[A-Z]+[a-z]*|[a-z]+")
# Characters nobody sees, which a page puts inside an address or a word so that a reader's
# software does not link it: between two characters of a value they are no part of it.
_UNSEEN = re.compile(
    "(?<=[0-9A-Za-z\u00c0-\u024f@._+\\-])[\u00ad\u200b\u200c\u200d\u2060\ufeff]+"
    "(?=[0-9A-Za-z\u00c0-\u024f@._+\\-])"
)
# Quoted-printable text: a line that ends in the `=` of a soft line break, an attribute's equals
# sign before its quote, and an escaped byte.
_SOFT_BREAK = re.compile(r"[^=\s]=\n(?=\S)")
_QUOTED_ATTRIBUTE = re.compile(r"\w=3D[\"']")
_ESCAPE = re.compile(r"=[0-9A-F]{2}")
_QUOTED_LINE = 76  # the longest line quoted-printable text holds
# Every space of Unicode's class Zs and every dash of its class Pd (a page writes the ones that
# do not break a line, of any width), each as it stands inside a character class.
_SPACES = (
    " \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f"
    "\u205f\u3000"
)
_DASHES = (
    "\\-\u058a\u05be\u1400\u1806\u2010\u2011\u2012\u2013\u2014\u2015\u2e17\u2e1a\u2e3a"
    "\u2e3b\u2e40\u2e5d\u301c\u3030\u30a0\ufe31\ufe32\ufe58\ufe63\uff0d\U00010d6e\U00010ead"
    "\u2212"  # and the minus sign
)
# What stands between the groups of digits of one number, inside a character class: a space or
# a dash, and with them a full stop, a middle dot or a bullet.
_GAP_IN_DIGITS = _SPACES + _DASHES
_BETWEEN_DIGITS = _GAP_IN_DIGITS + ".\u00b7\u2022\u2219"
_TICKET = re.compile(rf"(?<!\d)\d{{13}}(?!\d)|(?<!\d)\d{{3}}[{_GAP_IN_DIGITS}]\d{{10}}(?!\d)")
# a phone number's groups, apart by one of those, or by a dash with a space on each side
_PHONE = re.compile(
    r"(?<![\w+])(?:\+|00)?(?:\(\d{1,5}\)|\d{1,5})"
    rf"(?:(?:[{_BETWEEN_DIGITS}/]|[{_SPACES}][{_DASHES}][{_SPACES}])"
    r"(?:\(\d{1,5}\)|\d{1,6})){1,7}(?!\w)"
    r"|(?<![\w+])\+\d{7,15}(?!\w)"
)
# groups of three or more digits: a flight number beside a day of the month is no number
_NUMBER = re.compile(
    rf"\d{{6,}}|(?<![A-Za-z\d])\d{{3,}}(?:[{_GAP_IN_DIGITS}]\d{{3,}}){{1,20}}(?![A-Za-z\d])"
)

# Not parts of a name: titles and what a passenger list prints beside a name.
_TITLES = frozenset(
    "mr mrs ms miss mstr dr prof jr sr ii iii iv herr frau mme mlle sra srta"
    " adult child infant chd inf adt".split()
)
# Words a harvested name never holds: what stands where a name could (after a greeting, a
# label, under a heading) and is none.
_COMMON = frozenset(
    """
    guest guests customer customers traveler travelers traveller travellers passenger passengers
    member sir madam madame monsieur valued client kunde kundin gast cliente huésped pasajero
    team all there friend everyone name names ticket tickets seat seats coach class economy
    business first premium cabin adult adults child children infant flight flights total fare
    room rooms night nights check date dates hotel airport terminal gate departure arrival
    confirmation booking reservation number status code reference details and or the with your
    you our for from this that one two queen king beds bed view frequent flyer aboard back
    again none
    """.split()
)
# Words that make what stands there a company's name, never a person's.
_COMPANY = frozenset(
    """
    airlines airline airways air hotel hotels inn suites resort lodge rail railways trips
    travel tours rent rental car cars group team services service desk support center centre
    department reservations inc llc ltd gmbh
    """.split()
)
# A word beside a known name that is no part of anyone's: what opens a sentence or a line.
_NOT_BESIDE = frozenset(
    """
    the a an dear hello hi hallo bonjour hola guten tag sehr geehrte geehrter estimado estimada
    liebe lieber cher chère thanks thank welcome to for from with and or of by in on at is as
    if no not yes we it my re fw fwd subject sent cc bcc contact emergency attn family via per
    i
    """.split()
)
# (a word, or one letter with or without its full stop: an initial)
_WORD_BEFORE = re.compile(r"(?<![^\W\d_])(?:([^\W\d_]{2,20})|([^\W\d_])\.?)[ \t]$")
_WORD_AFTER = re.compile(r"[ \t](?:([^\W\d_]{2,20})|([^\W\d_])\.?)(?![^\W\d_])")
_WORD_AFTER_FAMILY = re.compile(r"[/,][ \t]?([^\W\d_]{2,20})(?![^\W\d_])")
_PARTICLES = frozenset(
    "de la del van von der den da di le du bin al dos das el los las ten ter".split()
)
GREETINGS = (
    "Dear",
    "Hello",
    "Hi",
    "Hallo",
    "Guten Tag",
    "Sehr geehrte",
    "Sehr geehrter",
    "Bonjour",
    "Hola",
    "Estimado",
    "Estimada",
    "Cher",
    "Chère",
    "Liebe",
    "Lieber",
)
_GREETING = re.compile(
    r"^[ \t>]*(?:"
    + _any_of(GREETINGS)
    + r")(?:\(a\))?[ \t]+([^\n,.!:;]{1,60})",
    re.I | re.M,
)
NAME_LABELS = (
    "Passenger",
    "Traveler",
    "Traveller",
    "Guest name",
    "Name",
    "Driver",
    "Reisender",
    "Voyageur",
    "Pasajero",
    # and more of the same, since a label that names a person is a name worth taking
    "Passenger name",
    "Traveler name",
    "Traveller name",
    "Guest",
    "Lead guest",
    "Lead passenger",
    "Reserved under",
    "Booked by",
    "Renter",
    "Gast",
    "Name des Gastes",
    "Fahrgast",
    "Fahrer",
    "Nom",
    "Nom du client",
    "Client",
    "Passager",
    "Conducteur",
    "Nombre",
    "Nombre del huésped",
    "Huésped",
    "Titular",
    "Viajero",
    "Conductor",
    "Adult",
    "Child",
    "Infant",
    "Host",
    "Your host",
    "Hosted by",
    "Booked for",
    "Reserved for",
    "Issued to",
    "Ticket holder",
    "Cardholder",
    "Card holder",
    "Account holder",
    "Bill to",
    "Billed to",
    "Customer",
    "Customer name",
    "Contact name",
    "Primary guest",
    "Primary traveler",
    "Primary traveller",
    "Kunde",
    "Mieter",
    "Titulaire",
    "Cliente",
    "Anfitrión",
    "Attn",
    "First name",
    "Last name",
    "Given name",
    "Given names",
    "Family name",
    "Middle name",
    "Full name",
    "Surname",
    "Vorname",
    "Nachname",
    "Familienname",
    "Prénom",
    "Nom de famille",
    "Apellido",
    "Apellidos",
    "Emergency contact",
    "Contact person",
    "Next of kin",
)
# A label stands at the start of a line or of a column (two spaces or a tab from what is before
# it: looked for behind the label, so that a run of white space is not read from each place in
# it), and its value ends where the next column starts.
_LABELLED = re.compile(
    r"(?:^[ \t>*•\-]*|(?<=[ \t][ \t])|(?<=\t))(?:"
    + _any_of(NAME_LABELS)
    + r")s?(?:\(s\))?(?:[ \t]*#?\d{1,2})?(?:[ \t]*:[ \t]*|[ \t]{2,}|\t[ \t]*)"
    r"(\S[^\n\t]{0,80}?)(?=[ \t]{2,}|\t|[ \t]*$)",
    re.I | re.M,
)
# Any label before a colon, at the start of a line or of a column, and its value; which labels
# name a person is told by their words (`_names_a_person`), so `Nom du passager` and
# `Cardholder name` are ones though no list holds them.
_COLON_LABEL = re.compile(
    r"(?:^[ \t>*•\-]*|(?<=[ \t][ \t])|(?<=\t))([^\W\d_](?:[^\n\t: ]|[ ](?![ ])){0,58}?)"
    r"[ \t]*:[ \t]*(\S[^\n\t]{0,80}?)(?=[ \t]{2,}|\t|[ \t]*$)",
    re.M,
)
# Words of a label, without their marks. A label with a word for a name names a person, unless
# another of its words says whose name it is (`Hotel name`, `Rate name`); one with a word for a
# person does when its other words only say which one (`Lead passenger`, `Reservation holder`,
# `Adult 1`), so `Passenger type` and `Guest parking` name nobody.
_NAME_WORDS = frozenset(
    "name names nom noms nombre nombres vorname nachname familienname prenom prenoms apellido"
    " apellidos surname".split()
)
_PERSON_WORDS = frozenset(
    """
    passenger passengers traveler travelers traveller travellers guest guests driver drivers
    renter customer client cardholder holder host adult adults child children infant infants
    reisender reisende reisenden fahrgast fluggast passagier passagiere gast gastes gaste kunde
    kundin kunden mieter fahrer inhaber karteninhaber voyageur voyageurs passager passagers
    conducteur titulaire cliente clients invite hote pasajero pasajeros pasajera viajero
    viajeros huesped huespedes titular clientes conductor anfitrion
    """.split()
)
_WHICH_PERSON = frozenset(
    """
    lead primary main principal first second third other additional reservation booking ticket
    card account policy the of on for by du de del des la le el der die das den dem des fur
    pour por para reserva reservierung buchung tarjeta carte karte billet billete hauptreisender
    """.split()
)
_THING_WORDS = frozenset(
    """
    hotel hotels hostel property lodging accommodation airline airlines carrier company
    station airport tour product event ship vessel file plan rate room fare cabin class tariff
    bed meal offer promotion deal option service package restaurant venue car vehicle operator
    agency supplier vendor merchant business brand bank trip itinerary network wifi ssid group
    team list campaign domain route destination place street unterkunft firma gesellschaft
    fluggesellschaft bahnhof flughafen reise zimmer tarif etablissement compagnie societe gare
    aeroport voyage sejour chambre empresa aerolinea alojamiento establecimiento estacion
    aeropuerto viaje habitacion tarifa
    """.split()
)
# what stands between two names behind one label
_BETWEEN_NAMES = re.compile(r"[;&,]|\b(?:and|und|et|y)\b")
# A title before a name, and words in a sentence that a name follows. After a word of thanks or
# of welcome a name counts only behind a comma: `Welcome Aboard` names nobody.
_TITLED = re.compile(
    r"(?<![^\W\d_])(?i:Mr|Mrs|Ms|Miss|Mx|Herr|Frau|Mme|Mlle|Monsieur|Madame|Señor|Señora|Sra"
    r"|Srta)\.?[ \t]+([A-Z][^\n]{0,60})"
)
# words before an address of mail in angle brackets: whose address it is
_NAMED_ADDRESS = re.compile(
    r"(?<![\w'’.\-])((?:[A-Z][\w'’.\-]{0,40}[ \t]+){1,4})<([^<>\s@]{1,64}@[^<>\s]{1,255})>"
)
_CUE = re.compile(
    r"(?<![^\W\d_])(?:(?i:prepared[ \t]+for|reserved[ \t]+for|booked[ \t]+for|issued[ \t]+to"
    r"|in[ \t]+the[ \t]+name[ \t]+of|under[ \t]+the[ \t]+name(?:[ \t]+of)?|on[ \t]+behalf[ \t]+of"
    r"|au[ \t]+nom[ \t]+de|auf[ \t]+den[ \t]+namen|a[ \t]+nombre[ \t]+de"
    r"|your[ \t]+(?:host|driver|guide)(?:[ \t]+is)?)[ \t]*(?::[ \t]*)?"
    r"|(?i:welcome(?:[ \t]+back)?|bienvenue|willkommen|bienvenid[oa]|thank[ \t]+you|thanks|merci"
    r"|danke|gracias)[ \t]*,[ \t]*|CN=[\"']?)([A-Z][^\n]{0,60})"
)
# A heading over a list of names: alone on its line, or the first cell of a table's head.
_HEADING = re.compile(
    r"^[ \t]*(?:passengers?|travell?ers?|guests?|names?|passagers?|voyageurs?|reisende|fahrgäste"
    r"|pasajeros?|viajeros?)(?:[ \t]+names?)?(?:[ \t]+(?:and|und|et|y|&)[ \t]+\w+)?[ \t]*:?"
    r"(?:[ \t]{2,}[^\n]*)?[ \t]*$",
    re.I | re.M,
)
_BULLET = re.compile(r"^[\s>*•\-–]*(?:\d{1,2}[.)])?[ \t]*")
_SLASH_NAME = re.compile(
    r"(?<![A-Za-z/])([A-Z][A-Z'\-]{1,24})/"
    r"([A-Z][A-Z'\-]{0,24}(?: [A-Z][A-Z'\-]{0,24}){0,2})(?![a-z/])"
)
# What a line holds that says a `WORD/WORD` token on it is a place, not a person.
_ROUTE_LINE = re.compile(
    r"\d{1,2}:\d{2}|\(\s*[A-Z]{3}\s*\)|^[ \t]*(?:from|to|depart\w*|arriv\w*|origin|destination"
    r"|von|nach|de|desde|hasta)\b",
    re.I,
)
_BLOCK_LINE = re.compile(
    r"^[ \t>]*(From|To|Cc|Von|An|De|À|Para|Reply-To)[ \t]*:[ \t]*([^\n]{3,200})$", re.I | re.M
)
_FORWARD = re.compile(
    r"^[ \t>]*(?:-{3,}[ \t]*(?:Forwarded message|Original Message)[ \t]*-{3,}"
    r"|Begin forwarded message:)[ \t]*$",
    re.I | re.M,
)
_FORWARD_SUBJECT = re.compile(r"^[ \t]*(?:fwd?|fw|tr|wg|rv|i)[ \t]*:", re.I)
# Mail hosts where an address is a person's, never a company's.
_PERSONAL_HOSTS = frozenset(
    """
    gmail.com googlemail.com outlook.com hotmail.com live.com msn.com yahoo.com ymail.com
    icloud.com me.com mac.com aol.com proton.me protonmail.com pm.me gmx.de gmx.net gmx.com
    web.de t-online.de mail.com fastmail.com hey.com zoho.com yandex.com yahoo.co.uk yahoo.fr
    yahoo.de hotmail.co.uk hotmail.fr orange.fr free.fr laposte.net wanadoo.fr btinternet.com
    comcast.net verizon.net att.net sbcglobal.net cox.net bellsouth.net
    """.split()
)
_TO_HEADERS = ("to", "cc", "bcc", "delivered-to", "x-forwarded-to")
_FROM_HEADERS = ("from", "sender", "reply-to")

# What a key of structured markup, or of a reading, says its value is.
_REFERENCE_KEYS = frozenset(
    """
    reservationnumber reservationid confirmationnumber confirmationcode confirmation_code
    bookingnumber bookingreference bookingcode pnr recordlocator tickettoken membershipnumber
    ordernumber itinerarynumber
    """.split()
)
_TICKET_KEYS = frozenset({"ticketnumber"})
_PHONE_KEYS = frozenset({"telephone", "phone", "faxnumber", "mobile"})
_ADDRESS_KEYS = frozenset({"streetaddress", "address", "pickup_location", "dropoff_location"})
_NAME_KEYS = frozenset(
    """
    passengers passenger travellers travelers traveller traveler guests guest guestname
    drivername driver passengername
    """.split()
)
_PERSON_KEYS = frozenset({"name", "givenname", "familyname", "additionalname", "alternatename"})
# a mapping whose keys are travellers' names
_NAMED_KEYS = frozenset({"seat_by_passenger"})
# keys whose value is a flight's or a train's number, a seat, a code of a place: never a code
# that no label names
_KEPT_KEYS = frozenset(
    """
    flightnumber trainnumber busnumber iatacode icaocode flight_number train_number
    carrier_iata flight_code seats seat seatnumber seatrow departuregate arrivalgate
    departureterminal arrivalterminal departureplatform arrivalplatform cabin origin
    destination
    """.split()
)
# the kept keys whose value is a flight's or a train's number, which the email may then print bare
_TRANSPORT_KEYS = frozenset(
    "flightnumber trainnumber busnumber flight_number train_number flight_code".split()
)


def _fold(text: str) -> str:
    """One spelling of a value: NFKC, case folded, runs of white space one space."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _bare(text: str) -> str:
    """The letters and digits of a value alone, folded."""
    return "".join(ch for ch in _fold(text) if ch.isalnum())


def _plain(text: str) -> str:
    """A word as a ticket prints it: its marks dropped, case folded."""
    split = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in split if not unicodedata.combining(ch)).casefold()


def _spellings(word: str) -> set[str]:
    """How a part of a name may be printed: without its marks, and, for a German one, with
    `ae`, `oe` and `ue` written out."""
    out = {_plain(word)}
    spelt = word.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    out.add(_plain(spelt))
    return out


def _name_parts(name: str) -> list[str]:
    """The parts of a name that are replaced: its words, without titles."""
    return [p for p in (_plain(w) for w in _LETTERS.findall(name)) if p not in _TITLES]


@dataclass(frozen=True)
class Known:
    """What is known of an email before any rule looks at it."""

    names: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    numbers: tuple[str, ...] = ()
    sender_domains: frozenset[str] = frozenset()
    # What a first redaction of the same email replaced (`Redactor.found`), as (kind, value):
    # each goes wherever it stands, whatever it looks like there.
    found: tuple[tuple[str, str], ...] = ()


class _Work:
    """One string while it is redacted: each replacement is a mark in `s`, and `slots` holds, for
    each mark, its kind (None: kept as it is), the value it stands for (`key`; None: nothing is
    numbered), the text shown for it (after its placeholder, when it has a key), what stood
    there, and whether what made it a value is evidence enough to hand it to the next redaction
    (`carried`: a label, a key, a street, a phone number's shape; never a code's shape alone).
    A placeholder is numbered only when its mark is shown, so a value that a later rule took
    into a larger one (a name inside an address of mail) uses up no number."""

    def __init__(self, text: str) -> None:
        self.s = _PRIVATE.sub("\ufffd", text)
        self.slots: list[tuple[str | None, str | None, str, str, bool]] = []

    def mark(
        self, kind: str | None, key: str | None, shown: str, original: str, carried: bool = False
    ) -> str:
        self.slots.append((kind, key, shown, original, carried))
        digits = "".join(chr(_ZERO + int(d)) for d in str(len(self.slots) - 1))
        return f"{_OPEN}{digits}{_CLOSE}"

    def _slot(self, mark: str) -> tuple[str | None, str | None, str, str, bool]:
        return self.slots[int("".join(str(ord(ch) - _ZERO) for ch in mark[1:-1]))]

    def kind(self, mark: str) -> str | None:
        """What a mark stands for: one of KINDS, or None for what is kept or not counted."""
        return self._slot(mark)[0]

    def original(self, text: str) -> str:
        """`text` as it stood before any mark was put in it."""
        return _MARK_RE.sub(lambda m: self._slot(m.group())[3], text)

    def keep(self, pattern: re.Pattern[str], ok=None) -> None:
        """What `pattern` finds stays as it is, out of reach of every later rule."""

        def kept(m: re.Match[str]) -> str:
            if ok is not None and not ok(m):
                return m.group()
            return self.mark(None, None, m.group(), m.group())

        self.s = pattern.sub(kept, self.s)

    def reveal(self, redactor: Redactor) -> str:
        def show(m: re.Match[str]) -> str:
            kind, key, shown, original, carried = self._slot(m.group())
            if kind is None:
                return shown
            redactor.counts[kind] += 1
            if key is None:
                return shown
            return redactor._placeholder(kind, key, original if carried else "") + shown

        return _MARK_RE.sub(show, self.s)


def _is_day(digits: str) -> bool:
    """Eight digits that are a day from 1990 to 2100, written year first."""
    try:
        day = dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return False
    return 1990 <= day.year <= 2100


def _kept(m: re.Match[str]) -> bool:
    """Whether what `_KEEP` found is the date it looks like."""
    if m.group("day"):
        return _is_day(m.group("day"))
    if m.group("stamp"):
        return _is_day(m.group("stamp")[:8])
    if m.group("dmy"):
        a, b = (int(n) for n in re.split(r"[./\-]", m.group("dmy"))[:2])
        return (1 <= a <= 31 and 1 <= b <= 12) or (1 <= a <= 12 and 1 <= b <= 31)
    if m.group("ymd"):
        _, month, day = (int(n) for n in re.split(r"[./]", m.group("ymd")))
        return 1 <= month <= 12 and 1 <= day <= 31
    return True


def _tokens(window: str) -> Iterator[tuple[int, int, str]]:
    """What a label may name in the text after it, as (where it starts, where it ends, the
    token): a run of 5 to 40 letters and digits (of digits alone, to 10: more are a ticket's or
    a phone's, which rules of their own find). Groups joined by hyphens are one token when they
    are a code's, capitals and digits and 8 or more of them (`AB12-CD34-EF56`); else each group
    is a token of its own (`check-in`, a day written with hyphens)."""
    for found in _TOKEN.finditer(window):
        whole = found.group()
        chars = whole.replace("-", "")
        if len(chars) < len(whole) and 8 <= len(chars) <= _MAX_CODE and chars.isupper():
            if any(ch.isdigit() for ch in chars):
                yield found.start(), found.end(), whole
                continue
        at = found.start()
        for group in whole.split("-"):
            if 5 <= len(group) <= (10 if group.isdigit() else _MAX_CODE):
                yield at, at + len(group), group
            at += len(group) + 1


# A town with its state's letters, and the country after them or none: `Seattle, WA`.
_TOWN_AND_STATE = re.compile(
    rf"{_US_CITY},?[ \t]+[A-Z]{{2,3}}(?:,?[ \t]+[^\W\d_][\w.'’ \-]{{1,30}})?[ \t.]*"
)


def _label_ends_in_transport(stretch: str, reach: int = _WINDOW) -> bool:
    """Whether `stretch`, all that stands between a label and a code, is the rest of that label
    ending in the word for a flight or a train and a colon (`_LABEL_ENDS_IN_TRANSPORT`), within
    `reach` characters (the label's window unless said)."""
    return len(stretch) <= reach and _LABEL_ENDS_IN_TRANSPORT.fullmatch(stretch) is not None


def _owned_by_a_label(text: str, start: int, code: int) -> bool:
    """Whether a label earlier on the line ends in the transport word at `start`, so that the
    code at `code` is that label's, however long the label's words run up to
    `_OWNED_STRETCH`. Only the line, and at most `_LABEL_REACH` characters before the word, are
    read, so each word costs the same however many labels the text holds."""
    lo = max(0, code - _LABEL_REACH)
    lo = text.rfind("\n", lo, start) + 1 or lo
    return any(
        label.end() <= start
        and _label_ends_in_transport(text[label.end() : code], _OWNED_STRETCH)
        for label in _LABEL.finditer(text, lo, start)
    )


def _town_alone(address: str) -> bool:
    """Whether what a label called an address is a town and no more: it holds no digit, and
    is one or two words or a town with its state. So much else is written `Seattle, WA` that
    it is an address only where its label stands."""
    if any(ch.isdigit() for ch in address):
        return False
    address = address.strip()
    return len(_ALNUM.findall(address)) <= 2 or _TOWN_AND_STATE.fullmatch(address) is not None


def _encoded(run: str) -> bool:
    """Whether a run of 20 or more of an encoded value's characters is one, and no word: 60 or
    more that hold a letter; 32 or more that hold a letter and a digit; or any that hold a piece
    of 12 or more letters and digits which mixes the two, or mixes capitals and small letters as
    no word does (`YWxleEBl`)."""
    if not any(ch.isalpha() for ch in run):
        return False
    if len(run) >= 60 or (len(run) >= 32 and any(ch.isdigit() for ch in run)):
        return True
    for piece in _PIECE.findall(run):
        if piece.isdigit():
            continue
        if any(ch.isdigit() for ch in piece):
            return True
        # a word is small letters, capitals, or one capital and two or more small letters
        odd = 0
        for part in _CASED.findall(piece):
            capitals = sum(1 for ch in part if ch.isupper())
            small = len(part) - capitals
            word = capitals == 0 or (small == 0 and capitals > 1) or (capitals == 1 and small > 1)
            odd += not word
        if odd >= 2:
            return True
    return False


def _house_before(s: str, floor: int, town: int) -> int | None:
    """Where the address that ends in the town at `town` starts: at a house number before it
    on its line, with at most one more piece (a suite, a floor) between the street and the
    town; when the town starts its own line, on the line above, a blank line or two apart at
    most. A number that counts something, or that a word such as `Flight` or `Room` or an
    airline's two letters stand before, is no house number, nor is one with nothing between
    it and the town. Never before `floor`; None when no house number stands there."""
    reach = max(floor, town - _STREET_SPAN)
    cut = s.rfind("\n", reach, town)
    if not s[max(cut + 1, reach) : town].strip(" \t,>*•-"):
        if cut < 0:
            return None
        above = s[max(floor, cut - _STREET_SPAN) : cut + 1]
        gap = len(above) - len(above.rstrip(" \t\n"))
        if above.count("\n", len(above) - gap) > 3:
            return None
        town = cut + 1 - gap
        reach = max(floor, town - _STREET_SPAN)
        cut = s.rfind("\n", reach, town)
    start = max(cut + 1, reach)
    for m in _HOUSE.finditer(s, start, town):
        if _COUNTED.match(s, m.end()) or _KEPT_BEFORE.search(s, start, m.start()):
            continue
        if _CARRIER_BEFORE.search(s, max(start, m.start() - 4), m.start()):
            continue
        rest = s[m.start() : town].rstrip(" \t,")
        if rest.count(",") <= 1 and not re.search(r"[:;!?|]", rest):
            return m.start()
    return None


def _town_first(s: str) -> Iterator[tuple[int, int, int]]:
    """Each end of an address in `s` that is a town, a state and a postcode (the United
    States, Canada, Australia): (where it starts, where it ends with its country, where its
    postcode ends)."""
    for m in _TOWN_STATE.finditer(s):
        state = m.group("state")
        if m.group("us"):
            named = state in _US_STATES and not (m.group("comma") is None and state in _US_WORDS)
            country = "us"
        elif m.group("ca"):
            named, country = state in _CA_STATES, "ca"
        else:
            named, country = m.group("au")[0] in _AU_STARTS.get(state, ""), "au"
        if named:
            yield m.start(), m.end(), m.end(country)


def _street_in(s: str, start: int, end: int) -> int | None:
    """Where a street written with its number last or first starts in `s[start:end]`, which is
    one piece of a line: after a label's colon, at a street's words. None when the piece is no
    street: it holds no word, its number counts something or is a flight's, a seat's, a room's."""
    colon = s.rfind(": ", start, end)
    start = colon + 2 if colon >= 0 else start
    while start < end and s[start] in " \t>*•-":
        start += 1
    while end > start and s[end - 1] in " \t":
        end -= 1
    piece = s[start:end]
    if not 4 <= len(piece) <= 60 or not re.search(r"[^\W\d_]{3}", _MARK_RE.sub("", piece)):
        return None
    last = _NUMBER_LAST.search(piece)
    if last is not None and last.start() > 0:
        before = piece[: last.start()]
        words = _LETTERS.findall(before)
        counted = bool(words) and (
            words[-1].casefold() in _NOT_A_CODE or _COUNTED.fullmatch(words[-1]) is not None
        )
        if not (counted or _KEPT_BEFORE.search(before) or _CARRIER_BEFORE.search(before)):
            return start
    first = _NUMBER_FIRST.match(piece)
    if first is not None and not _COUNTED.match(piece, first.end()):
        return start
    return None


def _postcode_first(s: str) -> Iterator[tuple[int, int, int]]:
    """Each address of `s` that ends in a postcode and its town, with its street before them on
    the line or on the line above (a blank line or two apart at most): (where the street
    starts, where the postcode starts, where the town ends). A postcode that could be a year
    is none on a line of its own."""
    floor = 0
    for m in _POSTCODE_TOWN.finditer(s):
        if m.start() < floor or _COUNTED.fullmatch(m.group("town").split()[0]):
            continue
        reach = max(floor, m.start() - _STREET_SPAN)
        cut = s.rfind("\n", reach, m.start())
        left = max(cut + 1, reach)
        head = s[left : m.start()].rstrip(" \t")
        street = None
        if head.endswith(","):
            comma = left + len(head) - 1
            street = _street_in(s, max(s.rfind(",", left, comma) + 1, left), comma)
        elif not head.strip(">*•-") and cut >= floor:
            code = m.group("code")
            above = s[max(floor, cut - _STREET_SPAN) : cut + 1]
            gap = len(above) - len(above.rstrip(" \t\n"))
            a_year = code.isdigit() and len(code) == 4 and code[:2] in ("19", "20")
            if not a_year and above.count("\n", len(above) - gap) <= 3:
                line_end = cut + 1 - gap
                reach = max(floor, line_end - _STREET_SPAN)
                street = _street_in(s, max(s.rfind("\n", reach, line_end) + 1, reach), line_end)
        if street is not None:
            yield street, m.start(), m.end()
            floor = m.end()


def _ended_addresses(s: str) -> Iterator[tuple[int, int, int, int, bool]]:
    """Each address of `s` that is told by its end, in the order they stand, none inside
    another: (where it starts, where its end starts, where it ends, where the value of an end
    that stands alone stops, whether the end is one with its town first). A town, state and
    postcode is an address with or without a street before it; a postcode and town only with
    one."""
    ends = [(town, end, stop, None) for town, end, stop in _town_first(s)]
    ends += [(code, end, end, street) for street, code, end in _postcode_first(s)]
    at = 0
    for town, end, stop, street in sorted(ends, key=lambda found: found[:2]):
        if town < at or (street is not None and street < at):
            continue
        if street is None:
            start = _house_before(s, at, town)
            yield (town if start is None else start), town, end, stop, True
        else:
            yield street, town, end, stop, False
        at = end


def _still_quoted(text: str) -> bool:
    """Text that is quoted-printable whatever its header calls it: more than one attribute
    written `=3D"`; or more than one line ending in the `=` of a soft line break beside more
    than one escaped byte; or three such lines, or two `=3D`, in text none of whose lines is
    longer than that encoding writes. (A link's query may hold `=3D` or `=AB` as it reads, so
    neither alone says anything.)"""
    if len(_QUOTED_ATTRIBUTE.findall(text)) >= 2:
        return True
    soft = len(_SOFT_BREAK.findall(text))
    if soft >= 2 and len(_ESCAPE.findall(text)) >= 2:
        return True
    if soft < 3 and text.count("=3D") < 2:
        return False
    return max(map(len, text.split("\n"))) <= _QUOTED_LINE


def _as_1252(error: UnicodeError) -> tuple[str, int]:
    """Bytes that are not UTF-8 in text said to be, read as Windows-1252, which is what such
    mail is most often written in, so a name in it stays the name it is."""
    if not isinstance(error, UnicodeDecodeError):
        raise error
    return error.object[error.start : error.end].decode("cp1252", "replace"), error.end


codecs.register_error("tripbox-redact-1252", _as_1252)


def _decoded(data: bytes, charset: str) -> str:
    """`data` in `charset`; where that is UTF-8 or ASCII (also when nothing named one), a byte
    that does not fit is read as Windows-1252, never left as a replacement character."""
    try:
        name = codecs.lookup(charset).name
    except LookupError:
        charset = name = "utf-8"
    return data.decode(charset, "tripbox-redact-1252" if name in ("utf-8", "ascii") else "replace")


def _unquoted(text: str, charset: str = "utf-8") -> str:
    """`text` with its line ends as one character, decoded once for each time it still reads
    as quoted-printable (a part whose header names no encoding, or the wrong one; a part
    encoded twice; the source of an email pasted without its headers): at most twice."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for _ in range(2):
        if not _still_quoted(text):
            break
        try:
            data = quopri.decodestring(text.encode(charset, "replace"))
            text = _decoded(data, charset).replace("\r\n", "\n").replace("\r", "\n")
        except LookupError, ValueError:
            break
    return text


def _readable(text: str) -> str:
    """Text that can be written out and read: no byte that was no text, no control character
    but a tab and a line end, nothing unseen inside a value."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", _as_text(text))
    return _UNSEEN.sub("", text)


def pasted(raw: bytes) -> str:
    """Bytes that are no email, as the text they are: UTF-8, decoded where it is still
    quoted-printable."""
    return _readable(_unquoted(_decoded(raw, "utf-8")))


def _date_like(token: str) -> bool:
    return bool(
        re.fullmatch(rf"\d{{1,2}}(?:{_MONTHS})\d{{2,4}}", token, re.I)
        or (len(token) == 8 and token.isdigit() and _is_day(token))
        or re.fullmatch(r"\d{1,2}h\d{2}", token, re.I)
    )


class Redactor:
    """Redacts strings, pages, JSON-like values and whole emails with one memory of what stood
    for what: `counts` says how many values of each of `KINDS` were replaced, `removed_headers`
    and `removed_attachments` what `email` dropped."""

    def __init__(self, known: Known) -> None:
        self.known = known
        self.counts: Counter = Counter()
        self.removed_headers = 0
        self.removed_attachments = 0
        self._placeholders: dict[tuple[str, str], str] = {}
        self._numbered: Counter = Counter()
        self._files: dict[str, int] = {}
        # names: each one's parts as a multiset, and the names each spelling of a part is in
        self._names: list[Counter] = []
        self._canon: dict[str, str] = {}
        self._in: dict[str, set[int]] = {}
        self._seen_names: set[tuple[str, ...]] = set()
        # the parts that are a first name and those that are a family name
        self._given: set[str] = set()
        self._family: set[str] = set()
        self._refs: dict[str, re.Pattern[str]] = {}
        self._addresses: dict[str, tuple[re.Pattern[str], str]] = {}
        self._ends: set[str] = set()
        self._digits: dict[str, tuple[re.Pattern[str], str]] = {}
        self._found: dict[tuple[str, str], str] = {}
        self._found_of: Counter = Counter()
        # the phone numbers placed so far, by their last seven digits
        self._phones: dict[str, list[str]] = {}
        # the numbers the email or a key of its reading calls a flight's or a train's
        self._transport: set[str] = set()
        self._learnt = 0
        self._streets_learnt = 0
        self._boundaries = 0
        self._domains = frozenset(d.lower().strip(".") for d in known.sender_domains)
        for name in known.names:
            self._add_name(name)
        for ref in known.references:
            self._add_reference(ref)
        for address in known.addresses:
            self._add_address(address)
        for number in known.numbers:
            self._add_number(number)
        for kind, value in known.found:
            if not self._handed_on(kind, value):
                continue
            if kind == "name":
                self._add_name(value)
            elif kind == "reference":
                self._add_reference(value, sure=True)
            elif kind == "address":
                self._add_address(value, sure=True)
            elif kind in ("phone", "ticket", "number"):
                self._add_number(value, kind)

    @property
    def found(self) -> tuple[tuple[str, str], ...]:
        """What was replaced so far on evidence enough to replace it wherever it stands, as
        (kind, the value as it stood), for the next redaction of the same email
        (`Known.found`): what a label or a key named (a reference, a number of five digits or
        more, an address, a name), an address with its street, a phone number, and what was
        known already; at most _MAX_FOUND of a kind. Never what its shape alone made a value
        (a code no label names, a ticket's or any long number, a town with its postcode), nor
        what `_handed_on` refuses: a value that reads as a flight's or a train's number, a
        year, a town alone under an address's label. A name found beside a known one, a
        number with a letter in it and one of fewer digits are what their place made them, and
        stay so."""
        return tuple((kind, value) for (kind, _), value in self._found.items())

    def _handed_on(self, kind: str, value: str) -> bool:
        """Whether a value of this kind may be known wherever it stands: a number is of five
        digits or more and no letter; a reference is no year, reads as no flight's or train's
        number and is none the email calls one; an address is more than a town."""
        if kind == "number":
            return not any(ch.isalpha() for ch in value) and sum(ch.isdigit() for ch in value) >= 5
        if kind == "reference":
            code = value.strip()
            if _YEAR.fullmatch(code) or _TRANSPORT.fullmatch(code):
                return False
            return _bare(code) not in self._transport
        return kind != "address" or not _town_alone(value)

    def _note(self, kind: str, key: str, original: str) -> None:
        if (kind, key) in self._found or not original or self._found_of[kind] >= _MAX_FOUND:
            return
        if self._handed_on(kind, original):
            self._found[kind, key] = original
            self._found_of[kind] += 1

    # ------------------------------------------------------------------ what is known
    def _add_name(self, name: str) -> bool:
        parts = _name_parts(name)
        key = tuple(sorted(parts))
        if not parts or len(parts) > 8 or key in self._seen_names:
            return False
        if len(parts) == 1 and len(parts[0]) < 3:
            return False
        self._seen_names.add(key)
        self._names.append(Counter(parts))
        n = len(self._names) - 1
        if len(parts) == 1:
            self._given.add(parts[0])  # one word alone is what a greeting calls someone
        else:
            # `SAMPLE/ALEX` and `Sample, Alex` put the family name first
            first_is_family = bool(re.search(r"[/,]", name))
            self._family.add(parts[0] if first_is_family else parts[-1])
            self._given.add(parts[1] if first_is_family else parts[0])
        for word in _LETTERS.findall(name):
            part = _plain(word)
            if part in _TITLES:
                continue
            self._in.setdefault(part, set()).add(n)
            for spelling in _spellings(word):
                self._canon.setdefault(spelling, part)
        return True

    def _add_reference(self, ref: str, sure: bool = False) -> bool:
        """`sure`: a first redaction found it under a label or a key, so four digits are
        enough (four letters are a word as often as a code, and never are)."""
        chars = [c for c in unicodedata.normalize("NFKC", ref) if c.isalnum()]
        key = "".join(chars).casefold()
        letters = any(c.isalpha() for c in chars)
        digits = any(c.isdigit() for c in chars)
        least = 4 if digits and (sure or letters) else 5
        if key in self._refs or len(chars) > 40 or len(chars) < least:
            return False
        between = rf"[{_GAP_IN_DIGITS}]?"
        if letters and digits:
            # a code of letters and digits is nothing else, wherever it stands
            pattern = between.join(re.escape(c) for c in chars)
        elif digits:
            pattern = r"(?<![A-Za-z0-9])" + between.join(chars) + r"(?![A-Za-z0-9])"
        else:
            # letters alone could be a word: only as a whole word, written as one
            pattern = r"(?<![^\W_])" + "".join(re.escape(c) for c in chars) + r"(?![^\W_])"
        self._refs[key] = re.compile(pattern, re.I)
        return True

    def _add_address(self, address: str, sure: bool = False, last: bool = False) -> bool:
        """`sure`: a first redaction found it, so it needs no digit to be an address. `last`:
        it is the end of an address (a postcode and its town), looked for after every street,
        so that a street takes its own end with it."""
        whole = _ALNUM.findall(address)
        first = _ALNUM.findall(re.split(r"[,\n]", address.strip(), maxsplit=1)[0])
        if len(first) < 2 or not any(ch.isdigit() for ch in "".join(first)):
            first = []
        if not first and (len(whole) < 2 or not (sure or any(ch.isdigit() for ch in address))):
            return False
        key = _bare(" ".join(first or whole))
        if key in self._addresses or len(whole) > 40:
            return False
        between = r"[\W_]{0,6}"
        forms = [between.join(re.escape(t) for t in whole)]
        if first and first != whole:
            forms.append(between.join(re.escape(t) for t in first))
        # with the postcode and town that follow it, as a street found by rule is
        pattern = r"(?<![^\W_])(?:" + "|".join(forms) + rf")(?![^\W_])(?:{_TAIL})?"
        self._addresses[key] = (re.compile(pattern, re.I), key)
        if last:
            self._ends.add(key)
        return True

    def _add_number(self, number: str, kind: str = "number") -> bool:
        """`kind`: what a first redaction found it to be (a phone's, a ticket's number), which
        says more than `number` and takes its place."""
        digits = [c for c in number if c.isdigit()]
        key = "".join(digits)
        held = self._digits.get(key)
        if len(digits) < 5 or (held is not None and (kind == "number" or held[1] != "number")):
            return False
        pattern = re.compile(r"(?<!\d)" + rf"[{_BETWEEN_DIGITS}]?".join(digits) + r"(?!\d)")
        self._digits[key] = (pattern, kind)
        return True

    def _placeholder(self, kind: str, key: str, original: str = "") -> str:
        """The placeholder of one original value: the same wherever it stands. `original` is
        the value as it stood, noted for `found`."""
        found = self._placeholders.get((kind, key))
        if found is None and kind == "phone" and len(key) >= 7:
            # one number with and without its country's or its area's code is one number: it
            # ends as one placed before it does (those are kept by their last seven digits, a
            # few to each, so that a new number is never held against every other)
            near = self._phones.setdefault(key[-7:], [])
            for other in near:
                if other.endswith(key) or key.endswith(other):
                    found = self._placeholders[kind, key] = self._placeholders[kind, other]
                    break
            if found is None and len(near) < 8:
                near.append(key)
        if found is None:
            self._numbered[kind] += 1
            found = self._placeholders[kind, key] = f"[{_WORD[kind]}-{self._numbered[kind]}]"
        if kind in _FOUND_KINDS and kind != "name":
            self._note(kind, key, original)
        return found

    def _listed(self, domain: str) -> bool:
        """A listed sender's domain, or a host under one."""
        domain = domain.lower().strip(".")
        return domain in self._domains or any(domain.endswith("." + d) for d in self._domains)

    # ------------------------------------------------------------------ what an email gives away
    def _learn(self, text: str) -> None:
        """Note each booking reference a label in `text` names, so that it is replaced wherever
        it stands: the first token (`_tokens`: 5 to 40 letters and digits) within `_WINDOW`
        characters after the label that holds a digit, or one written all in capitals that the
        label names directly."""
        text = _UNSEEN.sub("", text)
        self._learn_streets(text)
        for named in _NAMED_TRANSPORT.finditer(text):
            if len(self._transport) >= _MAX_TRANSPORT:
                break
            # a label on the same line that ends in this word makes the code its own, also
            # when the label's words run past its window
            if _owned_by_a_label(text, named.start(), named.start(1)):
                if self._learnt < _MAX_LEARNT and self._add_reference(named.group(1)):
                    self._learnt += 1
                continue
            self._transport.add(named.group(1).casefold())
        for label in _LABEL.finditer(text):
            if self._learnt >= _MAX_LEARNT:
                return
            start = label.end()
            window = text[start : start + _WINDOW + _MAX_CODE + 8]
            direct = _LABEL_TAIL.match(window).end()
            kept: list[tuple[int, int]] | None = None  # the days and times of the window
            for at, end, token in _tokens(window):
                if at >= _WINDOW:
                    break
                before = window[:at]
                # a label whose last word is a flight's or a train's names the code after it
                owned = _label_ends_in_transport(before)
                named = at <= direct or owned
                if any(ch.isdigit() for ch in token):
                    if _date_like(token) or re.fullmatch(r"\d+[a-z]{2,}", token):
                        continue
                    if kept is None:
                        kept = [m.span() for m in _KEEP.finditer(window) if _kept(m)]
                    if any(a < end and at < b for a, b in kept):
                        continue  # a piece of a day or a time is no code
                    if re.search(rf"{_CURRENCY}[ \t]*$", before) or re.match(
                        rf"[ \t]?{_CURRENCY}", window[end:]
                    ):
                        continue
                    if not owned and _KEPT_BEFORE.search(before):
                        continue
                    if not named and (
                        re.fullmatch(_FLIGHT_SHAPE, token) or token.casefold() in self._transport
                    ):
                        continue  # a flight's or a train's number, and no label names it
                elif not (named and self._capitals_are_a_code(token, text, start, at, window)):
                    continue
                if self._add_reference(token):
                    self._learnt += 1
                break

    def _learn_streets(self, text: str) -> None:
        """Note the street of each address in `text` that is told by its end
        (`_ended_addresses`), so that it is replaced where it stands alone: on a page each line
        of an address is a piece of its own, and only what the reader sees puts the street
        beside its town."""
        if not any(start < town for start, town, *_ in _ended_addresses(text)):
            return
        # (a day and a time are kept before any address is looked for: here they are put out
        # of sight, each character for one, so that no number of theirs is a house's)
        seen = _KEEP.sub(lambda m: _OUT_OF_SIGHT * len(m.group()) if _kept(m) else m.group(), text)
        for start, town, end, _, town_first in _ended_addresses(seen):
            if self._streets_learnt >= _MAX_STREETS:
                return
            if start < town:
                street = re.split(r"[,\n]", text[start:town], maxsplit=1)[0]
                self._streets_learnt += self._add_address(street)
                if not town_first:
                    # a postcode and its town are an address only beside their street: known
                    # with it, they go from a line of their own too
                    self._streets_learnt += self._add_address(text[town:end], last=True)

    @staticmethod
    def _capitals_are_a_code(token: str, text: str, start: int, found: int, window: str) -> bool:
        """Whether a word in capitals right after a label is the code it names: a sign such as
        `:` or `#` stands between them, or the label names a number or a code, or the word
        stands in a line that is not all capitals, or alone on its own line."""
        if not token.isupper() or token.casefold() in _NOT_A_CODE:
            return False
        between = window[:found]
        if re.search(r"[:#=]", between) or re.search(r"[^\W\d_]", between):
            return True
        at = start + found
        line_start = text.rfind("\n", 0, at) + 1
        line_end = text.find("\n", at)
        line = text[line_start : line_end if line_end >= 0 else len(text)]
        if line.strip() == token:
            return True
        return "\n" not in between and any(ch.islower() for ch in line)

    def _learn_value(self, obj: Any, hint: str | None = None, depth: int = 0) -> None:
        """Note what the keys of structured markup say their values are: a person's name, a
        reservation's number, a street."""
        if depth > 40 or self._learnt >= _MAX_LEARNT:
            return
        if isinstance(obj, dict):
            person = "person" in str(obj.get("@type", "")).casefold()
            for key, value in obj.items():
                self._learn_value(value, _says(key, person), depth + 1)
        elif isinstance(obj, list):
            for value in obj:
                self._learn_value(value, hint, depth + 1)
        elif isinstance(obj, str) and hint is not None:
            added = False
            if hint == "name":
                added = self._add_name(obj)
            elif hint == "reference" and re.fullmatch(r"[A-Za-z0-9\-]{4,40}", obj.strip()):
                added = self._add_reference(obj)
            elif hint == "address":
                added = self._add_address(obj)
            self._learnt += added
        elif isinstance(obj, int) and not isinstance(obj, bool) and hint == "reference":
            self._learnt += self._add_reference(str(obj))

    def learn(self, raw: bytes) -> None:
        """Note what the email's own labels and markup give away, before anything of it is
        redacted: `email` does this itself, and a caller that redacts other values first (what
        was read from the email) calls it so that they name each value as the email will."""
        self._study(_tree(raw))

    def learn_text(self, text: str) -> None:
        """`learn`, of text that is no email."""
        self._learn(text)

    def _study(self, node: _Node) -> None:
        for part in _walk(node):
            if part.kind == "mail":
                for name, value in part.headers:
                    if name.lower() == "subject":
                        self._learn(_words(value))
            elif part.kind == "text":
                self._learn(part.text)
            elif part.kind == "calendar":
                self._learn(_calendar_reads(part.text))
            elif part.kind == "html":
                seen = _rendering(part.text)
                self._learn(seen.text())
                for block in seen.blocks:
                    self._learn_value(block)

    # ------------------------------------------------------------------ strings
    def text(self, s: str) -> str:
        """`s` with every rule applied."""
        if not isinstance(s, str) or not s:
            return s
        self._learn(s)
        return self._redact(s)

    def _redact(
        self,
        s: str,
        *,
        css: bool = False,
        known_only: bool = False,
        codes: bool = True,
        before: str = "",
    ) -> str:
        """`css`: `s` is a style, whose colours are no numbers. `known_only`: rule 1 alone (a
        key of a JSON value). `codes` False: `s` is a flight's or a train's number, which no
        rule takes for a code no label names. `before`: the text of the cell before `s` on a
        page, where the label of what `s` holds may stand."""
        if not any(ch.isalnum() for ch in s):
            return s
        before = _PRIVATE.sub(" ", before)
        work = _Work(_UNSEEN.sub("", s))
        self._known_values(work)
        if not known_only:
            self._emails(work)
            self._links(work)
            self._labelled(work, before)
            if css:
                work.keep(_CSS_COLOUR)
            work.keep(_KEEP, _kept)
            work.keep(_PRICE)
            self._by_their_end(work)
            self._long_runs(work)
            if codes:
                self._codes(work)
            self._digits_of(work)
            self._streets(work, before)
            self._numbers(work)
        return work.reveal(self)

    def _replace(self, work: _Work, spans: list[tuple[int, int, str, str]]) -> None:
        """Each of `spans` (start, end, kind, key) of the working string replaced, the first of
        two that overlap alone. A label named each, which is evidence to hand it on."""
        out: list[str] = []
        at = 0
        for start, end, kind, key in sorted(spans):
            if start < at or not key:
                continue
            original = work.original(work.s[start:end])
            out += [work.s[at:start], self._put(work, kind, key, original, True)]
            at = end
        out.append(work.s[at:])
        work.s = "".join(out)

    def _labelled(self, work: _Work, before: str = "") -> None:
        """What a label calls personal, whatever it looks like: a card's last digits, a date of
        birth (which no date rule may keep), a document's or a member's number, a door's code
        or a password. Each is a number to the report. `before` is read as standing a tab
        before the string, so a label in the cell before names what this one holds."""
        lead = f"{before}\t" if before else ""
        s = lead + work.s
        spans: list[tuple[int, int, str, str]] = []
        for m in _CARD_END.finditer(s):
            if _CARD_WORD.search(s, max(0, m.start() - 60), m.start()):
                spans.append((m.start(1), m.end(1), "number", "card" + m.group(1)))
        for m in _CARD_MASK.finditer(s):
            spans.append((m.start(1), m.end(1), "number", "card" + m.group(1)))
        for m in _CARD_NEAR.finditer(s):
            if _CARD_WORD.search(s, max(0, m.start() - 30), m.start()):
                spans.append((m.start(1), m.end(1), "number", "card" + m.group(1)))
        for m in _BIRTH.finditer(s):
            spans.append((m.start(1), m.end(1), "number", _bare(m.group(1))))
        for m in _ID_LABEL.finditer(s):
            tail = _LABEL_TAIL.match(s, m.end())
            if m.group("secret"):
                token = _SECRET_TOKEN.match(s, tail.end())
                signed = re.search(r"[:=\t]|\bis\b", s[m.end() : tail.end()])
                if token is not None and not signed:
                    # with no sign between them, only digits are the code the label names
                    if not re.fullmatch(r"\d{3,10}[#*]?[.,;]?", token.group()):
                        continue
            else:
                if m.group("member") and not _SAYS_NUMBER.search(s[m.start() : tail.end()]):
                    continue
                token = _ID_TOKEN.match(s, tail.end())
                if token is not None and not any(ch.isdigit() for ch in token.group()):
                    token = None
            if token is not None:
                key = _bare(work.original(token.group())) or "secret"
                spans.append((token.start(), token.end(), "number", key))
        # what was found in the cell before is no part of this string
        here = [(a - len(lead), b - len(lead), kind, key) for a, b, kind, key in spans]
        self._replace(work, [span for span in here if span[0] >= 0])

    def _codes(self, work: _Work) -> None:
        """A code no label names (`_CODE`), which a booking reference is more often than not:
        replaced as one, unless its shape or the word before it says what else it is."""

        def code(m: re.Match[str]) -> str:
            token = m.group()
            if _NOT_A_CODE_SHAPE.fullmatch(token) or _date_like(token):
                return token
            if token.casefold() in self._transport:
                return token  # the email calls it a flight's or a train's number elsewhere
            if _KEPT_BEFORE.search(work.s, max(0, m.start() - 24), m.start()):
                return token
            return self._put(work, "reference", token.casefold(), token)

        work.s = _CODE.sub(code, work.s)

    def _put(
        self, work: _Work, kind: str, key: str, original: str, carried: bool = False
    ) -> str:
        """A mark for a value replaced. `carried`: what made it one is evidence enough to
        replace it wherever it stands (`found`)."""
        return work.mark(kind, key, "", original, carried)

    def _known_values(self, work: _Work) -> None:
        self._known_names(work)
        for key, pattern in self._refs.items():
            work.s = pattern.sub(
                lambda m, key=key: self._put(work, "reference", key, m.group(), True), work.s
            )
        for ends in (False, True):
            for pattern, key in self._addresses.values():
                if (key in self._ends) is ends:
                    work.s = pattern.sub(
                        lambda m, key=key: self._put(work, "address", key, m.group(), True),
                        work.s,
                    )
        for key, (pattern, kind) in self._digits.items():
            work.s = pattern.sub(
                lambda m, key=key, kind=kind: self._put(work, kind, key, m.group(), True), work.s
            )

    def _known_names(self, work: _Work) -> None:
        """Rule 1 for names. Every word that is a part of a known name is a candidate; candidates
        that stand side by side and are all parts of one name are one span, replaced whole. A
        span of one part of one or two letters is left: such a part goes only beside the rest
        of its name."""
        if not self._names:
            return
        s = work.s
        candidates: list[tuple[int, int, str]] = []
        for m in _LETTERS.finditer(s):
            part = self._canon.get(_plain(m.group()))
            if part is not None:
                candidates.append((m.start(), m.end(), part))
        if not candidates:
            return
        # from each candidate, the longest run of neighbours that are parts of one name
        reach: list[int] = []
        for i, (_, end, part) in enumerate(candidates):
            names, used = set(self._in[part]), Counter([part])
            j = i + 1
            while j < len(candidates):
                next_start, next_end, next_part = candidates[j]
                if not _JOIN.fullmatch(s, end, next_start):
                    break
                fit = {n for n in names if self._names[n][next_part] > used[next_part]}
                if not fit:
                    break
                names, end = fit, next_end
                used[next_part] += 1
                j += 1
            reach.append(j)
        # the longest spans are taken first, so `Q. Alex Q Sample` is a gate and then a name;
        # a span that runs into one already taken is cut there (what is left of it is still
        # parts of one name)
        taken = [False] * len(candidates)
        spans: list[tuple[int, int]] = []
        for i in sorted(range(len(candidates)), key=lambda i: (i - reach[i], i)):
            j = i
            while j < reach[i] and not taken[j]:
                j += 1
            parts = Counter(candidates[k][2] for k in range(i, j))
            # parts of one or two letters alone are a name only when they are all of one
            if not any(len(part) >= 3 for part in parts) and parts not in self._names:
                continue
            taken[i:j] = [True] * (j - i)
            spans.append((i, j))
        out: list[str] = []
        at = 0
        for i, j in sorted(spans):
            start, end = candidates[i][0], candidates[j - 1][1]
            parts = [candidates[k][2] for k in range(i, j)]
            if j == i + 1:
                start, end, beside = self._beside(s, start, end, parts[0], at)
                parts += beside
            out.append(s[at:start])
            out.append(self._put(work, "name", " ".join(sorted(parts)), s[start:end]))
            at = end
        out.append(s[at:])
        work.s = "".join(out)

    def _beside(self, s: str, start: int, end: int, part: str, floor: int):
        """A known part that stands alone, with the capitalised word beside it that makes it a
        name: before a family name or after it behind `/` or `,` (a relative's first name),
        after a first name (a middle or a family name nobody listed). The span as it then is,
        and the word, which is replaced here and learnt nowhere."""

        def name_like(word: str) -> bool:
            plain = _plain(word)
            if not word[0].isupper() or plain in self._canon:
                return False
            return not any(
                plain in words for words in (_NOT_BESIDE, _COMMON, _COMPANY, _TITLES, _NOT_A_CODE)
            )

        if part in self._family:
            before = _WORD_BEFORE.search(s, max(0, start - 24), start)
            word = (before.group(1) or before.group(2)) if before else ""
            at = before.start(1 if before.group(1) else 2) if before else -1
            if at >= floor and name_like(word):
                return at, end, [_plain(word)]
            after = _WORD_AFTER_FAMILY.match(s, end)
            if after and name_like(after.group(1)):
                return start, after.end(1), [_plain(after.group(1))]
        if part in self._given:
            after = _WORD_AFTER.match(s, end)
            word = (after.group(1) or after.group(2)) if after else ""
            if word and name_like(word):
                return start, after.end(), [_plain(word)]
        return start, end, []

    def _emails(self, work: _Work) -> None:
        """Rule 2: an address of mail, whole; the domain of a listed sender stays."""

        def address(m: re.Match[str]) -> str:
            original = work.original(m.group())
            domain = original.rsplit("@", 1)[1]
            kept = f"@{domain.lower()}" if self._listed(domain) else ""
            return work.mark("email", _fold(original), kept, original)

        work.s = _EMAIL.sub(address, work.s)

    def _links(self, work: _Work) -> None:
        """Rule 3: scheme and host stay; the query and the fragment become `?[QUERY]`; a path
        segment stays only when it is a plain word."""

        def link(m: re.Match[str]) -> str:
            url = m.group()
            cut = len(url)
            while cut and url[cut - 1] in ".,;:!?)'\"":
                if url[cut - 1] == ")" and url.count("(", 0, cut) >= url.count(")", 0, cut):
                    break
                cut -= 1
            url, after = url[:cut], url[cut:]
            scheme, sep, rest = url.partition("://")
            if not sep:
                scheme, rest = "", url
            found = re.search(r"[/?#]", rest)
            cut = found.start() if found else len(rest)
            authority, tail = rest[:cut], rest[cut:]
            host = authority if _HOSTNAME.fullmatch(authority) else OPAQUE
            ask = re.search(r"[?#]", tail)
            path = tail[: ask.start()] if ask else tail
            asked = ask is not None and len(tail) > ask.start() + 1
            segments = [
                seg if not seg or self._plain_segment(seg) else OPAQUE for seg in path.split("/")
            ]
            shown = f"{scheme}{sep}{host}{'/'.join(segments)}"
            if asked:
                shown += f"?{QUERY}"
            original = work.original(url)
            return work.mark("link" if asked else None, None, shown, original) + after

        work.s = _LINK.sub(link, work.s)

    @staticmethod
    def _plain_segment(segment: str) -> bool:
        """A path segment that is a plain word of at most 24 letters and hyphens, with a file's
        suffix or none."""
        word = segment.rsplit(".", 1)[0]
        return len(word) <= 24 and bool(_SEGMENT.fullmatch(segment))

    def _long_runs(self, work: _Work) -> None:
        """A run of 20 or more letters, digits and the signs encoded values are written with
        that is one (`_encoded`), wherever it stands."""

        def run(m: re.Match[str]) -> str:
            text = m.group()
            return work.mark(None, None, OPAQUE, text) if _encoded(text) else text

        work.s = _LONG_RUN.sub(run, work.s)

    def _digits_of(self, work: _Work) -> None:
        """Rules 5 and 6: ticket numbers, then phone numbers; before them a position on the map
        and an address on a network, which are printed as some phone numbers are."""
        work.s = _PLACES.sub(
            lambda m: self._put(work, "number", _bare(m.group()), m.group()), work.s
        )
        work.s = _TICKET.sub(
            lambda m: self._put(work, "ticket", re.sub(r"\D", "", m.group()), m.group()), work.s
        )

        def phone(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group())
            if len(digits) < 7:
                return m.group()
            kind = "phone" if len(digits) <= 15 else "number"
            return self._put(work, kind, digits, m.group(), kind == "phone")

        work.s = _PHONE.sub(phone, work.s)

    def _by_their_end(self, work: _Work) -> None:
        """Rule 7, by an address's end, whatever its street is called: a town, a state and a
        postcode (the United States, Canada, Australia) with the country after them and what
        stands before them from the house number on; or a postcode and its town with the street
        before them. The street stands on the same line or the line above. It runs before the
        rules for codes and numbers, which would take a piece of an address for something
        else."""
        out: list[str] = []
        at = 0
        for start, town, end, stop, _ in _ended_addresses(work.s):
            if start == town:  # a town alone is one value with its state and postcode
                key = _bare(work.original(work.s[town:stop]))
            else:  # an address is its first line, as it is when that line stands alone
                street = work.original(work.s[start:town])
                key = _bare(re.split(r"[,\n]", street, maxsplit=1)[0])
            out.append(work.s[at:start])
            whole = work.original(work.s[start:end])
            # (with its street it is an address wherever it stands; a town is found again)
            out.append(self._put(work, "address", key, whole, start < town))
            at = end
        out.append(work.s[at:])
        work.s = "".join(out)

    def _streets(self, work: _Work, before: str = "") -> None:
        """Rule 7: what a label calls an address (on its own line, on the line above, or in the
        cell `before` on a page); a house number with a street word, or a box at a post office,
        and, when the same or the next line gives one, its postcode and town; the line of a
        British postcode."""

        def put(value: str, loose: bool) -> str | None:
            """The placeholder of what a label calls an address; `loose`: the label stands
            apart from it, so it is one only when it holds a digit or a comma."""
            left = _MARK_RE.sub(" ", value)
            if not re.search(r"[^\W_]{3}", left) or (loose and not re.search(r"[\d,]", left)):
                return None  # nothing is left but what other rules replaced, or it is a label
            original = work.original(value)
            key = _bare(re.split(r"[,\n]", original, maxsplit=1)[0]) or _bare(original)
            return self._put(work, "address", key, original, True)

        def labelled(m: re.Match[str], loose: bool = False) -> str:
            shown = put(m.group(2), loose)
            return m.group() if shown is None else m.group(1) + shown

        work.s = _ADDRESS_LINE.sub(labelled, work.s)
        work.s = _ADDRESS_BELOW.sub(lambda m: labelled(m, True), work.s)
        if before and _ADDRESS_CELL.fullmatch(before.strip()):
            line = _CELL_LINE.search(work.s)
            shown = put(line.group().rstrip(), True) if line else None
            if shown is not None:
                end = line.start() + len(line.group().rstrip())
                work.s = work.s[: line.start()] + shown + work.s[end:]

        out: list[str] = []
        at = 0
        for m in _STREETS.finditer(work.s):
            if m.start() < at:
                continue
            tail = _TAIL_RE.match(work.s, m.end())
            if tail is None:  # its town may be an address already, found by its ZIP code
                tail = _TOWN_FOUND.match(work.s, m.end())
                if tail is not None and work.kind(tail.group(1)) != "address":
                    tail = None
            end = tail.end() if tail is not None else m.end()
            original = work.original(work.s[m.start() : end])
            out.append(work.s[at : m.start()])
            out.append(self._put(work, "address", _bare(m.group()), original, True))
            at = end
        out.append(work.s[at:])
        work.s = "".join(out)

        def line(m: re.Match[str]) -> tuple[int, int]:
            start = work.s.rfind("\n", 0, m.start()) + 1
            end = work.s.find("\n", m.end())
            end = len(work.s) if end < 0 else end
            if end - start > 100:
                start = max(start, m.start() - 40)
                end = m.end()
            while start < m.start() and work.s[start] in " \t":
                start += 1
            while end > m.end() and work.s[end - 1] in " \t":
                end -= 1
            return start, end

        out, at = [], 0
        for m in _POSTCODE.finditer(work.s):
            if m.start() < at:
                continue
            start, end = line(m)
            start = max(start, at)
            original = work.original(work.s[start:end])
            out.append(work.s[at:start])
            out.append(self._put(work, "address", _bare(original), original))
            at = end
        out.append(work.s[at:])
        work.s = "".join(out)

    def _numbers(self, work: _Work) -> None:
        """Rule 8: any other run of six or more digits, single spaces or hyphens inside it."""

        def number(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group())
            if len(digits) < 6:
                return m.group()
            return self._put(work, "number", digits, m.group())

        work.s = _NUMBER.sub(number, work.s)

    # ------------------------------------------------------------------ JSON-like values
    def value(self, obj: Any, hint: str | None = None, _depth: int = 0) -> Any:
        """Every string of a JSON-like value redacted; its shape and its keys stay (a key that
        is itself a known value, a traveller's name, is replaced). A key that says what its
        value is (a person's name, a reservation's number, a telephone, a street) makes the
        value one, whatever it looks like."""
        if _depth > 60:
            return None
        if isinstance(obj, dict):
            person = "person" in str(obj.get("@type", "")).casefold()
            out = {}
            for key, value in obj.items():
                named = isinstance(key, str) and key.casefold() in _NAMED_KEYS
                if named and isinstance(value, dict):
                    out[key] = {
                        self._hinted(str(k), "name"): self.value(v, None, _depth + 1)
                        for k, v in value.items()
                    }
                    continue
                shown = self._redact(key, known_only=True) if isinstance(key, str) else key
                out[shown] = self.value(value, _says(key, person), _depth + 1)
            return out
        if isinstance(obj, list):
            return [self.value(v, hint, _depth + 1) for v in obj]
        if isinstance(obj, str):
            return self._hinted(obj, hint)
        if isinstance(obj, int) and not isinstance(obj, bool):
            # a number that is a reservation's, or long enough to be one, is read as its digits
            if hint in ("reference", "ticket", "phone") or len(str(abs(obj))) >= 6:
                shown = self._hinted(str(obj), hint)
                return obj if shown == str(obj) else shown
        return obj

    def _hinted(self, s: str, hint: str | None) -> str:
        """A string whose key says what it is: redacted by rule, and when no rule finds it,
        replaced whole as what the key says."""
        if hint in ("kept", "transport"):
            transport = hint == "transport" and _TRANSPORT.fullmatch(s.strip())
            if transport and len(self._transport) < _MAX_TRANSPORT:
                self._transport.add(_bare(s))
            self._learn(s)
            return self._redact(s, codes=False)
        if hint in ("reference", "ticket", "phone"):
            # a reservation's number that is one token is learnt, so the email's text names it
            # the same; anything else under such a key goes whole, never in part
            if hint == "reference" and re.fullmatch(r"[A-Za-z0-9\-]{4,40}", s.strip()):
                self._add_reference(s)
                out = self.text(s)
                if out != s:
                    return out
            key = _bare(s) if hint == "reference" else re.sub(r"\D", "", s)
            if len(key) < 4:
                return self.text(s)
            self.counts[hint] += 1
            return self._placeholder(hint, key, s)
        if hint == "name":
            if self._add_name(s):
                self._note("name", _fold(s), s)
        elif hint == "address":
            self._add_address(s)
        out = self.text(s)
        if hint != "address" or out != s or not any(ch.isdigit() for ch in s):
            return out  # a place with no number in it is a city or an airport
        self.counts["address"] += 1
        return self._placeholder("address", _bare(s), s)

    # ------------------------------------------------------------------ pages
    def html(self, s: str) -> str:
        """A page with its text and its attribute values redacted, its tags as they were; a
        block of structured markup stays JSON, its strings redacted; any other script is
        removed. A line that grows long is broken inside the next tag, where a line break
        changes nothing a reader or a parser sees."""
        seen = _rendering(s)
        self._learn(seen.text())
        for block in seen.blocks:
            self._learn_value(block)
        return self._page(s)

    def _page(self, s: str) -> str:
        page = _Page(self)
        try:
            page.feed(s)
            page.close()
        except Exception:  # noqa: BLE001 - a page the parser gives up on is redacted as text
            return self.text(s)
        return "".join(page.out)

    # ------------------------------------------------------------------ emails
    def email(self, raw: bytes) -> str:
        """The email as text that is still an email: the kept headers, the parts in their
        order under boundaries numbered anew, each text part decoded and redacted, every other
        part one line saying what was removed."""
        tree = _tree(raw)
        self._study(tree)
        self._boundaries = 0
        lines = _as_text("\n".join(self._mail(tree))).split("\n")
        return "\n".join(ln if len(ln) <= MAX_LINE else ln[:MAX_LINE] + CUT for ln in lines) + "\n"

    def _mail(self, node: _Node) -> list[str]:
        lines = []
        for name, value in node.headers:
            kept = _KEPT.get(name.lower())
            if kept is None:
                self.removed_headers += 1
            elif kept.lower() not in _REWRITTEN:
                lines.append(f"{kept}: {self._header(kept, value)}")
        self.removed_headers += node.dropped
        head, body = self._part(node.children[0])
        return [*lines, *head, "", *body]

    def _header(self, name: str, value: str) -> str:
        if name == "MIME-Version":
            return "1.0"
        if name == "Message-ID":
            host = value.strip().strip("<>").rpartition("@")[2].strip().lower()
            listed = bool(_HOSTNAME.fullmatch(host)) and self._listed(host)
            return f"<{MESSAGE_ID}@{host}>" if listed else f"<{MESSAGE_ID}>"
        if name == "Authentication-Results":
            return self._authentication(value)
        return " ".join(self.text(_words(value)).split())

    def _authentication(self, value: str) -> str:
        """Each `method=result` pair, and of its properties the domain of `header.from`,
        `header.i` and `smtp.mailfrom`, kept or replaced as an address's is."""
        chunks = _chunks(" ".join(value.split()))
        if not chunks:
            return ""
        server = (chunks[0].split() or [""])[0].lower()
        listed = bool(_HOSTNAME.fullmatch(server)) and self._listed(server)
        out = [server if listed or server == _PUBLIC_AUTHSERV else HOST]
        for chunk in chunks[1:]:
            words = chunk.split() or [""]
            pair = re.fullmatch(r"([A-Za-z][A-Za-z0-9.\-]{0,20})=([A-Za-z]{1,20})", words[0])
            if pair is None:
                continue
            said = [f"{pair.group(1).lower()}={pair.group(2).lower()}"]
            for word in words[1:]:
                key, sep, of = word.partition("=")
                if sep and key.lower() in ("header.from", "header.i", "smtp.mailfrom"):
                    said.append(f"{key.lower()}={self._sender(of)}")
            out.append(" ".join(said))
        return "; ".join(out)

    def _sender(self, value: str) -> str:
        value = value.strip().strip("<>\"'")
        local, at, domain = value.rpartition("@")
        if at and local:
            return self._redact(value)
        domain = domain.lower()
        shown = domain if _HOSTNAME.fullmatch(domain) and self._listed(domain) else DOMAIN
        return f"{at}{shown}"

    def _calendar(self, text: str) -> str:
        """A calendar redacted as it reads and written as the calendar it is: each line put
        together again from where it was folded, its value read with its escaped commas, line
        ends and semicolons as themselves, redacted, escaped as it was and folded anew."""
        out = []
        for line in _calendar_lines(text):
            head, value = _calendar_parts(line)
            if "\\" in value:
                shown = self._redact(_ESCAPED.sub(_unescaped, value))
                for sign, written in (("\\", "\\\\"), (";", "\\;"), (",", "\\,"), ("\n", "\\n")):
                    shown = shown.replace(sign, written)
            else:
                shown = self._redact(value)
            out.append(_folded(self._redact(head) + shown))
        return "\n".join(out)

    def _file(self, node: _Node) -> list[str]:
        """The `Content-Disposition` line of a part that had a file name: the name is `[FILE-n]`
        with its suffix when that is one of a file's kinds (`_FILE_KINDS`), else none, so a word
        or a number after a dot is never shown."""
        if node.filename is None:
            return []
        n = self._files.setdefault(_fold(node.filename), len(self._files) + 1)
        found = re.search(r"\.([A-Za-z0-9]{1,8})$", node.filename.strip())
        kind = found.group(1).lower() if found else ""
        suffix = f".{kind}" if kind in _FILE_KINDS else ""
        return [f'Content-Disposition: attachment; filename="[FILE-{n}]{suffix}"']

    def _part(self, node: _Node) -> tuple[list[str], list[str]]:
        """A part's own header lines and its body's lines."""
        self.removed_headers += node.dropped if node.kind != "mail" else 0
        if node.kind == "multi":
            self._boundaries += 1
            boundary = f"part-{self._boundaries}"
            body: list[str] = []
            for child in node.children:
                head, lines = self._part(child)
                body += [f"--{boundary}", *head, "", *lines, ""]
            head = [f'Content-Type: {node.ctype}; boundary="{boundary}"']
            return head, [*body, f"--{boundary}--"]
        if node.kind in ("text", "html", "calendar"):
            if node.kind == "html":
                shown = self._page(node.text)
            elif node.kind == "calendar":
                shown = self._calendar(node.text)
            else:
                shown = self.text(node.text)
            head = [f'Content-Type: {node.ctype}; charset="utf-8"', *self._file(node)]
            return [*head, "Content-Transfer-Encoding: 8bit"], shown.strip("\n").split("\n")
        if node.kind == "attached":
            return ["Content-Type: message/rfc822", *self._file(node)], self._mail(node.children[0])
        if node.kind == "more":
            head = ['Content-Type: text/plain; charset="utf-8"']
            return head, [f"[REMOVED: {node.more} more parts]"]
        self.removed_attachments += 1
        return [f"Content-Type: {node.ctype}", *self._file(node)], [
            f"[ATTACHMENT REMOVED: {node.ctype}, {node.size} bytes]"
        ]


def _says(key: Any, person: bool) -> str | None:
    """What a key of structured markup, or of a reading, says its value is."""
    if not isinstance(key, str):
        return None
    k = key.casefold()
    if k in _REFERENCE_KEYS:
        return "reference"
    if k in _TICKET_KEYS:
        return "ticket"
    if k in _PHONE_KEYS:
        return "phone"
    if k in _ADDRESS_KEYS:
        return "address"
    if k in _NAME_KEYS or (person and k in _PERSON_KEYS):
        return "name"
    if k in _TRANSPORT_KEYS:
        return "transport"
    if k in _KEPT_KEYS:
        return "kept"
    return None


def _markup(text: str) -> Any:
    """A block of structured markup read as JSON; a page may wrap it in a comment or a CDATA
    section, which is no part of it. ValueError when it is no JSON."""
    inner = text.strip()
    for opening, closing in (("<!--", "-->"), ("//<![CDATA[", "//]]>"), ("<![CDATA[", "]]>")):
        if inner.startswith(opening) and inner.endswith(closing):
            inner = inner[len(opening) : -len(closing)].strip()
    try:
        return json.loads(inner)
    except RecursionError:
        raise ValueError("nested too deep") from None


# The elements a page is built of, and the attribute names a tag holds; a name with a prefix
# (`o:p`, `v:roundrect`) or `amp-` before it is an element as well.
_ELEMENTS = frozenset(
    (
        "a abbr acronym address area article aside audio b base basefont bdi bdo bgsound big"
        " blink blockquote body br button canvas caption center cite code col colgroup data"
        " datalist dd del details dfn dialog dir div dl dt em embed fieldset figcaption figure"
        " font footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe"
        " image img input ins kbd label legend li link main map mark marquee menu meta meter nav"
        " nobr noframes noscript object ol optgroup option output p param picture pre progress q"
        " rp rt ruby s samp script section select small source spacer span strike strong style"
        " sub summary sup svg table tbody td template textarea tfoot th thead time title tr"
        " track tt u ul var video wbr xml xmp"
        # what a drawing is built of
        " circle clippath defs desc ellipse g line lineargradient mask path pattern polygon"
        " polyline radialgradient rect stop symbol text tspan use"
    ).split()
)
_PREFIXED = re.compile(r"[a-z][a-z0-9]{0,15}:[a-z][a-z0-9\-]{0,40}|amp-[a-z0-9\-]{1,40}")
_ATTRIBUTE = re.compile(r"[a-z_:][a-z0-9_:.\-]{0,60}")


def _is_element(tag: str) -> bool:
    return tag in _ELEMENTS or _PREFIXED.fullmatch(tag) is not None


class _Page(HTMLParser):
    """A page written out again as it is read, its text and attribute values redacted."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__(convert_charrefs=True)
        self.r = redactor
        self.out: list[str] = []
        self._col = 0
        self._raw: str | None = None  # the script or style element being read
        self._json = False
        self._held: list[str] = []
        self._before = ""  # the end of the last text met: the cell before the next one

    def _write(self, text: str) -> None:
        self.out.append(text)
        cut = text.rfind("\n")
        self._col = len(text) - cut - 1 if cut >= 0 else self._col + len(text)

    def _tag(self, name: str, attrs: list[str], end: str) -> None:
        gap = "\n" if self._col > _WRAP else " "
        if attrs:
            self._write(f"<{name}{gap}{' '.join(attrs)}{end}")
        else:
            self._write(f"<{name}{gap if gap == chr(10) else ''}{end}")

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], end: str) -> None:
        if not (_is_element(tag) and all(_ATTRIBUTE.fullmatch(name) for name, _ in attrs)):
            # what stands in angle brackets and is no tag of a page: the text it is
            self._write(self.r._redact(self.get_starttag_text() or f"<{tag}>"))
            return
        shown = []
        for name, value in attrs:
            if value is None:
                shown.append(name)
            else:
                redacted = self.r._redact(value, css=name in _CSS_ATTRS)
                shown.append(f'{name}="{htmllib.escape(redacted, quote=True)}"')
        self._tag(tag, shown, end)

    def handle_starttag(self, tag: str, attrs) -> None:
        self._open(tag, attrs, ">")
        if tag in ("script", "style"):
            kind = next((v for k, v in attrs if k == "type"), None) or ""
            self._raw, self._json, self._held = tag, "ld+json" in kind.lower(), []

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._open(tag, attrs, " />")

    def handle_endtag(self, tag: str) -> None:
        if self._raw is not None:
            self._flush()
        if _is_element(tag):
            self._tag(f"/{tag}", [], ">")
        else:
            self._write(self.r._redact(f"</{tag}>"))

    def _flush(self) -> None:
        held, self._held = "".join(self._held), []
        raw, self._raw = self._raw, None
        if raw == "style":
            self._write(self.r._redact(held, css=True))
        elif self._json:
            try:
                block = json.dumps(self.r.value(_markup(held)), ensure_ascii=False, indent=1)
            except ValueError:
                block = self.r.text(held)  # no JSON: redacted as the text it is
            self._write("\n" + block.replace("</", "<\\/") + "\n")

    def handle_data(self, data: str) -> None:
        if self._raw is not None:
            self._held.append(data)
            return
        self.r._learn(data)
        self._write(htmllib.escape(self.r._redact(data, before=self._before), quote=False))
        if data.strip():
            self._before = data.strip()[-80:]

    def handle_comment(self, data: str) -> None:
        self._write("<!--" + self.r.text(data).replace("--", "- -") + "-->")

    def handle_decl(self, decl: str) -> None:
        self._write("<!" + self.r.text(decl) + ">")

    def handle_pi(self, data: str) -> None:
        self._write("<?" + self.r.text(data) + ">")

    def unknown_decl(self, data: str) -> None:
        self._write("<![" + self.r.text(data) + "]>")

    def close(self) -> None:
        super().close()
        if self._raw is not None:
            self._flush()


class _Rendering(HTMLParser):
    """A page as the text a reader sees, a cell two spaces from the one before it and a block on
    its own line, and its blocks of structured markup, read."""

    BLOCK = frozenset(
        "p br div tr li h1 h2 h3 h4 h5 h6 table section ul ol title td th dd dt blockquote".split()
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocks: list[Any] = []
        self._raw: str | None = None
        self._json = False
        self._held: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            kind = next((v for k, v in attrs if k == "type"), None) or ""
            self._raw, self._json, self._held = tag, "ld+json" in kind.lower(), []
        elif tag in ("td", "th"):
            self.parts.append("  ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._raw is not None:
            if self._raw == "script" and self._json:
                try:
                    self.blocks.append(_markup("".join(self._held)))
                except ValueError:
                    pass
            self._raw = None
        elif tag in self.BLOCK and tag not in ("td", "th", "br"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._raw is not None:
            self._held.append(data)
        else:
            self.parts.append(_UNSEEN.sub("", data).replace("\xa0", " "))

    def text(self) -> str:
        return "".join(self.parts)


def _rendering(page: str) -> _Rendering:
    seen = _Rendering()
    try:
        seen.feed(page)
        seen.close()
    except Exception:  # noqa: BLE001 - what the parser read before it gave up still counts
        pass
    return seen


# ----------------------------------------------------------------------------- an email's parts
@dataclass
class _Node:
    """One piece of an email as it is redacted: `mail` (a message: its headers and one child, its
    body), `multi` (a multipart and its parts), `text` and `html` (a text part, decoded),
    `attached` (an attached email: one child, the `mail`), `gone` (any other part: its type and
    size alone) and `more` (the parts past a bound, counted)."""

    kind: str  # (and `calendar`: a `text/calendar` part, whose lines are folded and escaped)
    ctype: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    dropped: int = 0  # headers that are neither kept nor written anew
    text: str = ""
    size: int = 0
    filename: str | None = None
    children: list[_Node] = field(default_factory=list)
    more: int = 0


def _walk(node: _Node) -> Iterator[_Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _words(value: Any) -> str:
    """A header's value as its text: unfolded, read as a body is (`_header_text`), its encoded
    words decoded, cut at a bound. Each encoded word is decoded on its own, as a body is
    (`_decoded`), so one whose bytes are not the charset it names, or whose charset is unknown,
    reads as well as it can and leaves the words beside it decoded; the white space between two
    encoded words goes, as RFC 2047 says."""
    text = _header_text(re.sub(r"\r?\n[ \t]+", " ", str(value))[:_MAX_HEADER])
    out: list[str] = []
    at = 0
    for m in email.header.ecre.finditer(text):
        between = text[at : m.start()]
        if not out or between.strip():
            out.append(between)
        try:
            [(data, charset)] = email.header.decode_header(m.group())
        except Exception:  # noqa: BLE001 - an encoded word that does not decode is read as written
            out.append(m.group())
        else:
            if isinstance(data, bytes):
                out.append(_decoded(data, charset or "utf-8"))
            else:
                out.append(data)
        at = m.end()
    out.append(text[at:])
    return re.sub(r"[\x00-\x1f\x7f]", " ", _as_text("".join(out))).strip()


def _header_text(text: str) -> str:
    """A header's text with the bytes the mail parser kept as they were sent (each a lone
    surrogate) read as a body's are: as UTF-8, a byte that does not fit read as Windows-1252."""
    try:
        sent = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text
    return sent.decode("utf-8", "tripbox-redact-1252")


def _as_text(text: str) -> str:
    """`text` with every byte that was no text (kept by the mail parser as a lone surrogate)
    replaced, so that what is redacted can always be written out."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _type_of(part: email.message.Message) -> str:
    ctype = part.get_content_type()
    if re.fullmatch(r"[a-z0-9][a-z0-9.+\-]{0,40}/[a-z0-9][a-z0-9.+\-]{0,60}", ctype):
        return ctype
    return "application/octet-stream"


def _name_of(part: email.message.Message) -> str | None:
    try:
        name = part.get_filename()
    except Exception:  # noqa: BLE001 - a file name that cannot be read is no name
        return None
    return _as_text(str(name)) if name else None


def _encoding_of(part: email.message.Message) -> str:
    """How a part's header says it is encoded: the word alone, in small letters."""
    said = str(part.get("Content-Transfer-Encoding") or "")
    named = re.match(r"[\s\"']*([A-Za-z0-9\-]{1,40})", said)
    return named.group(1).lower() if named else ""


def _bytes_of(part: email.message.Message) -> bytes:
    """A part's body, decoded as its header says it is encoded. The mail parser decodes an
    encoding it is given by its exact name; one it left as it was sent, because the name stands
    in quotes or has a comment after it, is decoded here."""
    raw = part.get_payload()
    try:
        sent = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else b""
    except UnicodeEncodeError:
        sent = raw.encode("utf-8", "replace")
    try:
        data = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - a body that does not decode is read as it stands
        data = None
    if isinstance(data, bytes) and data != sent:
        return data
    how = _encoding_of(part)
    if how == "quoted-printable":
        return quopri.decodestring(sent)
    if how == "base64":
        try:
            packed = b"".join(sent.split())
            return binascii.a2b_base64(packed + b"=" * (-len(packed) % 4))
        except binascii.Error, ValueError:
            return sent
    return sent


def _text_of(part: email.message.Message) -> str:
    """A text part as it reads: decoded as its header says, in its charset, and decoded again
    where it is still quoted-printable after that."""
    data = _bytes_of(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        codecs.lookup(charset)
        text = _decoded(data, charset)
    except LookupError, ValueError:
        charset = "utf-8"
        text = _decoded(data, charset)
    return _readable(_unquoted(text, charset))


# The kinds of file a travel email attaches, shown after an attachment's replaced name.
_FILE_KINDS = frozenset(
    "pdf png jpg jpeg gif webp heic svg ics vcs vcf eml msg txt html htm csv xml json zip"
    " pkpass doc docx xls xlsx ppt pptx odt rtf".split()
)


# A calendar (RFC 5545): a line is folded onto the next by a line end and one space or tab; in a
# value a comma, a semicolon, a backslash and a line end are written after a backslash.
_FOLD = re.compile(r"\n[ \t]")
_ESCAPED = re.compile(r"\\([\\;,nN])")
_FOLDED_AT = 200  # characters of a calendar's line as it is written


def _unescaped(m: re.Match[str]) -> str:
    return "\n" if m.group(1) in "nN" else m.group(1)


def _calendar_lines(text: str) -> list[str]:
    """A calendar's lines, each put together again from where it was folded."""
    return _FOLD.sub("", text).split("\n")


def _calendar_parts(line: str) -> tuple[str, str]:
    """A calendar line's name and parameters with their colon, and its value: cut at the first
    colon that stands in no quoted parameter."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[: i + 1], line[i + 1 :]
    return line, ""


def _calendar_reads(text: str) -> str:
    """A calendar as text a reader sees, for what is learnt and harvested from it: its lines
    unfolded, each value's escaped line ends as lines of their own."""
    lines = []
    for line in _calendar_lines(text):
        head, value = _calendar_parts(line)
        lines.append(head + _ESCAPED.sub(_unescaped, value))
    return "\n".join(lines)


def _folded(line: str) -> str:
    """A calendar's line folded at _FOLDED_AT characters: before a space where there is one,
    and never inside a placeholder."""
    out = []
    at = 0
    while len(line) - at > _FOLDED_AT - len(out[-1:]):
        end = at + _FOLDED_AT - len(out[-1:])  # (a line that goes on starts with one space)
        cut = line.rfind(" ", at + _FOLDED_AT // 2, end)
        cut = end - 1 if cut < 0 else cut
        opened = line.rfind("[", max(at + 1, cut - 20), cut)
        if opened >= 0 and "]" not in line[opened:cut]:
            cut = opened
        out.append(line[at:cut])
        at = cut
    out.append(line[at:])
    return "\n ".join(out)


def _attached(part: email.message.Message) -> email.message.Message | None:
    """The email a `message/rfc822` part carries; one sent base64-encoded or quoted-printable
    as a whole (which the mail parser reads as it was sent) is decoded first."""
    payload = part.get_payload()
    inner = payload[0] if isinstance(payload, list) and payload else None
    if not isinstance(inner, email.message.Message):
        return None
    encoding = _encoding_of(part)
    if encoding == "base64" and not (inner.get("From") or inner.get("Subject")):
        try:
            data = base64.b64decode(str(inner.get_payload()), validate=False)
        except binascii.Error, ValueError:
            return inner
        return email.message_from_bytes(data, policy=email.policy.compat32)
    if encoding == "quoted-printable":
        try:
            data = inner.as_bytes()
        except Exception:  # noqa: BLE001 - one that cannot be written out again is read as it is
            return inner
        if any(isinstance(d, email.errors.MissingHeaderBodySeparatorDefect) for d in inner.defects):
            # a soft line break inside a header ended the headers there: the blank line that
            # writing it out put after them was never sent
            data = data.replace(b"\n\n", b"\n", 1)
        return email.message_from_bytes(quopri.decodestring(data), policy=email.policy.compat32)
    return inner


def _count(part: email.message.Message) -> int:
    """How many parts a part is, itself included (1 for one nested too deep to walk)."""
    try:
        return sum(1 for _ in part.walk())
    except RecursionError:
        return 1


def _dropped(part: email.message.Message) -> int:
    """How many headers of a part that is no message are neither kept nor written anew."""
    return sum(1 for name in part.keys() if name.lower() not in _KEPT)


def _mail_node(msg: email.message.Message, depth: int, left: list[int]) -> _Node:
    # as they were sent: `items()` gives a header holding raw 8-bit bytes as a `Header` whose
    # text has each byte replaced, before `_words` could read it as letters
    held = list(msg.raw_items())
    headers = [(str(name), str(value)) for name, value in held[:_MAX_HEADERS]]
    more = max(0, len(held) - _MAX_HEADERS)
    body = _part_node(msg, depth, left, own=True)
    return _Node("mail", headers=headers, dropped=more, children=[body])


def _part_node(
    part: email.message.Message, depth: int, left: list[int], own: bool = False
) -> _Node:
    """`own`: the part is a message's own body, whose headers the message's node holds."""
    left[0] -= 1
    ctype = _type_of(part)
    dropped = 0 if own else _dropped(part)
    name = None if own else _name_of(part)
    if part.is_multipart() and ctype.startswith("multipart/"):
        children: list[_Node] = []
        parts = [p for p in part.get_payload() if isinstance(p, email.message.Message)]
        for i, child in enumerate(parts):
            if left[0] <= 0:
                children.append(_Node("more", more=sum(_count(p) for p in parts[i:])))
                break
            children.append(_part_node(child, depth, left))
        return _Node("multi", ctype, dropped=dropped, children=children)
    if ctype == "message/rfc822":
        inner = _attached(part)
        if inner is not None:
            if depth >= MAX_DEPTH:
                return _Node("more", more=_count(inner), dropped=dropped)
            child = _mail_node(inner, depth + 1, left)
            return _Node("attached", ctype, dropped=dropped, filename=name, children=[child])
    elif (
        ctype.startswith("text/")
        and ctype != "text/rfc822-headers"
        and not part.is_multipart()
        and (ctype == "text/calendar" or not _attachment(part))
    ):
        kind = "html" if ctype in ("text/html", "text/x-amp-html") else "text"
        kind = "calendar" if ctype == "text/calendar" else kind
        return _Node(kind, ctype, dropped=dropped, text=_text_of(part), filename=name)
    size = len(_bytes_of(part)) if not part.is_multipart() else 0
    return _Node("gone", ctype, dropped=dropped, size=size, filename=name)


def _attachment(part: email.message.Message) -> bool:
    """Whether a part's `Content-Disposition` says it is an attachment: a text one is removed
    whole as any other is, whatever it holds, except a calendar file."""
    try:
        return part.get_content_disposition() == "attachment"
    except Exception:  # noqa: BLE001 - a disposition that cannot be read says nothing
        return False


def _tree(raw: bytes) -> _Node:
    """The email as nodes, at most MAX_PARTS parts and MAX_DEPTH attached emails deep. One whose
    parts are nested deeper than the mail parser reads is one part that is not carried."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        return _mail_node(msg, 0, [MAX_PARTS])
    except RecursionError:
        unread = _Node("gone", "application/octet-stream", size=len(raw))
        return _Node("mail", children=[unread])


def _chunks(value: str) -> list[str]:
    """An Authentication-Results value split on `;` as its grammar reads it: never inside a
    quoted string or a comment; comments dropped, quotes unwrapped."""
    out: list[str] = []
    cur: list[str] = []
    depth, quoted, i = 0, False, 0
    while i < len(value):
        ch = value[i]
        if quoted:
            if ch == "\\" and i + 1 < len(value):
                cur.append(value[i + 1])
                i += 2
                continue
            if ch == '"':
                quoted = False
            else:
                cur.append(ch)
        elif depth:
            depth += (ch == "(") - (ch == ")")
        elif ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
            cur.append(" ")
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [c.strip() for c in out if c.strip()]


# ----------------------------------------------------------------------------- names an email gives
def _candidate(text: str, *, least: int = 1) -> str | None:
    """The name at the start of `text`, or None: capitalised words (a particle such as `de`
    between them), at most five, none of them a word that stands where a name could."""
    words: list[str] = []
    end = 0
    for m in re.finditer(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*\.?", text):
        # one space, or a slash or a comma, joins two words of a name: more is a column's edge
        gap = r"(?:[ \t]|[/,][ \t]?)" if words else r"[ \t]{0,3}"
        if not re.fullmatch(gap, text[end : m.start()]) or len(words) == 5:
            break
        word = m.group().rstrip(".")
        plain = _plain(word)
        if plain in _TITLES:
            if words:
                break
            end = m.end()  # a title before the name is no part of it
            continue
        if plain in _COMPANY:
            return None
        if plain in _COMMON:
            return None if not words else _finish(text, words, end, least)
        if not (word[0].isupper() or (words and plain in _PARTICLES)):
            break
        words.append(word)
        end = m.end()
    return _finish(text, words, end, least)


def _finish(text: str, words: list[str], end: int, least: int) -> str | None:
    while words and _plain(words[-1]) in _PARTICLES:
        words.pop()
    if len(words) < least or not words:
        return None
    first = text.find(words[0])
    last = text.rfind(words[-1], 0, end) + len(words[-1])
    return text[first:last].strip()


def _names_a_person(label: str) -> bool:
    """Whether a label, by its words, says its value is a person's name."""
    words = [_plain(word) for word in _LETTERS.findall(label)]
    if not 1 <= len(words) <= 6 or any(word in _THING_WORDS for word in words):
        return False
    # French `Nombre de voyageurs` is how many, never whose name: `nombre de` before a plural
    # with no article (Spanish names a person `Nombre del pasajero`, `Nombre de los pasajeros`)
    if words[:2] == ["nombre", "de"] and len(words) > 2 and words[2][-1:] in ("s", "x"):
        return False
    if any(word in _NAME_WORDS for word in words):
        return True
    people = [word for word in words if word in _PERSON_WORDS]
    return bool(people) and all(word in _WHICH_PERSON for word in words if word not in people)


def _labelled_names(text: str) -> Iterator[str]:
    """What stands behind each label of `text` that names a person: a label of the list, in
    its line or its column, and any label before a colon that its words make one."""
    for m in _LABELLED.finditer(text):
        yield m.group(1)
    for m in _COLON_LABEL.finditer(text):
        if _names_a_person(m.group(1)):
            yield m.group(2)


def _person(display: str, address: str, to: bool, recipients: set[str], forwarder: bool) -> bool:
    """Whether an address header's display name is a person's: a recipient's is; a sender's is
    when the address is also a recipient's (the forwarder), when the email is a forward by hand
    and this is who sent it, when the host is one where people have mail, or when a part of the
    name is in the address's local part and not in its domain."""
    name = display.strip(" '\"")
    if not name or "@" in name or not any(ch.isalpha() for ch in name):
        return False
    if any(_plain(word) in _COMPANY for word in _LETTERS.findall(name)):
        return False
    address = address.lower()
    local, _, domain = address.partition("@")
    if to or forwarder or address in recipients or domain in _PERSONAL_HOSTS:
        return True
    labels = re.split(r"[.\-]", domain)
    local = _plain(local)
    return any(
        len(part) >= 3 and part in local and not any(part in label for label in labels)
        for part in _name_parts(name)
    )


def _addresses(value: str) -> list[tuple[str, str]]:
    try:
        return [(n, a) for n, a in email.utils.getaddresses([value]) if a and "@" in a]
    except Exception:  # noqa: BLE001 - a header that is no address list names nobody
        return []


def _header_addresses(value: str) -> list[tuple[str, str]]:
    """The addresses of an address header as (display name, address): the list is split as it
    was sent and each display name decoded after that, so the comma of a `Last, First` name
    inside an encoded word ends no address."""
    sent = _header_text(re.sub(r"\r?\n[ \t]+", " ", str(value))[:_MAX_HEADER])
    return [(_words(name), _as_text(address)) for name, address in _addresses(_as_text(sent))]


def harvest(raw: bytes) -> tuple[str, ...]:
    """The names the email itself gives away, in the order they are met: the display name of an
    address header that is a person's (`_person`), in the email's headers and in the header
    block of a forward; the words after a greeting, after a title (`Mrs`) and after words
    that a name follows (`prepared for`, `Thank you,`); each `name` of a `Person` in structured
    markup; the value on a line a name's label starts (a label of the list, or any label
    before a colon whose words make it one: `_names_a_person`), and the names under a heading
    such as `Passengers`; and each `SURNAME/GIVEN` token that a label or a title stands beside, or
    whose surname the email prints again with another given name. A candidate is capitalised
    words, at most five, none of them a word for a company or for what a booking holds."""
    tree = _tree(raw)
    mails = [n for n in _walk(tree) if n.kind == "mail"]
    texts: list[str] = []
    blocks: list[Any] = []
    for node in _walk(tree):
        if node.kind == "text":
            texts.append(node.text)
        elif node.kind == "calendar":
            texts.append(_calendar_reads(node.text))
        elif node.kind == "html":
            seen = _rendering(node.text)
            texts.append(seen.text())
            blocks += seen.blocks
    return _harvested(mails, texts, blocks)


def harvest_text(text: str) -> tuple[str, ...]:
    """`harvest`, of text that is no email: it has no header, so its first lines are read as
    the lines of text they are, whatever they look like."""
    return _harvested([], [text], [])


def _harvested(mails: list[_Node], texts: list[str], blocks: list[Any]) -> tuple[str, ...]:
    found: dict[tuple[str, ...], str] = {}

    def add(name: str | None) -> None:
        if not name or not 2 <= len(name.strip()) <= 80 or len(found) >= 200:
            return
        key = tuple(sorted(_name_parts(name)))
        if key and (len(key) > 1 or len(key[0]) >= 3):
            found.setdefault(key, name.strip())

    recipients: set[str] = set()
    for mail in mails:
        for name, value in mail.headers:
            if name.lower() in (*_TO_HEADERS, "x-forwarded-for", "return-path"):
                recipients |= {a.lower() for _, a in _header_addresses(value)}
    lines = [m for text in texts for m in _BLOCK_LINE.finditer(text) if "@" in m.group(2)]
    for m in lines:
        if m.group(1).casefold() in ("to", "cc", "an", "à", "para"):
            recipients |= {a.lower() for _, a in _addresses(m.group(2))}
    forwarded = any(_FORWARD.search(text) for text in texts) or any(
        _FORWARD_SUBJECT.match(_words(value))
        for name, value in (mails[0].headers if mails else ())
        if name.lower() == "subject"
    )
    for i, mail in enumerate(mails):
        for name, value in mail.headers:
            header = name.lower()
            if header in _TO_HEADERS or header in _FROM_HEADERS:
                for display, address in _header_addresses(value):
                    to = header in _TO_HEADERS
                    if _person(display, address, to, recipients, forwarded and i == 0 and not to):
                        add(_candidate(display) or display)
    for m in lines:
        to = m.group(1).casefold() in ("to", "cc", "an", "à", "para")
        for display, address in _addresses(m.group(2)):
            if _person(display, address, to, recipients, False):
                add(_candidate(display) or display)
    for block in blocks:
        for person in _persons(block):
            add(person)
    for text in texts:
        for m in _GREETING.finditer(text):
            add(_candidate(m.group(1)))
        said = [m.group(1) for m in _TITLED.finditer(text)]
        for m in _CUE.finditer(text):
            if m.group().startswith("CN="):
                # a calendar's or a certificate's name ends at its quote, and is one name
                name = re.split(r"[\"';:]", m.group(1), maxsplit=1)[0]
                add(_candidate(name))
            else:
                said.append(m.group(1))
        for value in (*_labelled_names(text), *said):
            for piece in _BETWEEN_NAMES.split(value)[:12]:
                name = _candidate(piece)
                if name is None and "(" in piece:  # `2 adults (Pat Example`
                    name = _candidate(piece.split("(", 1)[1])
                add(name)
        for m in _NAMED_ADDRESS.finditer(text):
            display = re.sub(r"^[AP]M[ \t]+", "", m.group(1).strip())
            if _person(display, m.group(2), False, recipients, False):
                add(_candidate(display) or display)
        for m in _HEADING.finditer(text):
            took = False
            for line in text[m.end() : m.end() + 2000].split("\n")[1:13]:
                if not line.strip():
                    if took:
                        break
                    continue
                name = _candidate(_BULLET.sub("", line), least=2)
                if name is None:
                    break
                add(name)
                took = True
    _slash_names(texts, found, add)
    return tuple(found.values())


# Keys of structured markup whose value is the person a reservation is for, written as a
# plain string or as an object with no type as often as a typed `Person`.
_PERSON_HOLDERS = frozenset(
    {"undername", "customer", "attendee", "passenger", "guest", "traveler"}
)


def _persons(obj: Any, depth: int = 0) -> Iterator[str]:
    """Each `name` of a `Person` in structured markup, and the person a `_PERSON_HOLDERS` key
    names: its string, or the `name` of its object when that has no type or a person's."""
    if depth > 40:
        return
    if isinstance(obj, dict):
        if "person" in str(obj.get("@type", "")).casefold() and isinstance(obj.get("name"), str):
            yield obj["name"]
        for key, value in obj.items():
            if str(key).casefold() not in _PERSON_HOLDERS:
                continue
            for one in value if isinstance(value, list) else [value]:
                if isinstance(one, str):
                    yield one
                elif (
                    isinstance(one, dict)
                    and "@type" not in one
                    and isinstance(one.get("name"), str)
                ):
                    yield one["name"]
        for value in obj.values():
            yield from _persons(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _persons(value, depth + 1)


def _slash_names(texts: list[str], found: dict, add) -> None:
    """`SURNAME/GIVEN` tokens that are names: a place pair (`MINNEAPOLIS/ST PAUL`) is printed
    the same way, so a token counts only on a line with no time, bracketed code or route label,
    and then when a name's label or a title stands on its line, when a part of it is a part of
    a name already found, or when the email prints its surname with two given names."""
    tokens: list[tuple[str, str, str, bool]] = []
    for text in texts:
        for line in text.split("\n"):
            if "/" not in line or _ROUTE_LINE.search(line):
                continue
            labelled = None  # whether a name's label starts the line: asked once a line
            for m in _SLASH_NAME.finditer(line):
                surname, given = m.group(1), m.group(2)
                if len(surname) == 3 and len(given) == 3:
                    continue  # two airport codes are written so
                if labelled is None:
                    by_words = _COLON_LABEL.match(line)
                    labelled = bool(_LABELLED.match(line)) or (
                        by_words is not None and _names_a_person(by_words.group(1))
                    )
                words = given.split()
                titled = len(words) > 1 and _plain(words[-1]) in _TITLES
                tokens.append((surname, given, m.group(), labelled or titled))
    givens: dict[str, set[str]] = {}
    for surname, given, _, _ in tokens:
        givens.setdefault(surname, set()).add(given)
    known = {part for key in found for part in key}
    for surname, _given, token, labelled in tokens:
        shared = any(part in known for part in _name_parts(token))
        if labelled or shared or len(givens[surname]) > 1:
            add(token)


def known_from(
    book: Any,
    extraction: Any,
    typed: Iterable[str],
    harvested: Iterable[str],
    sender_domains: Iterable[str],
) -> Known:
    """What is known of an email: names from the travellers (`book`: every form it lists), from
    every `passengers` entry of the reading (`extraction`, a reading or its JSON form), from
    what was `typed` and what the email gave (`harvested`); references from each confirmation
    code of the reading (`confirmation_code`, or the `pnr` a filed flight holds); addresses from
    each `address`, `pickup_location` and `dropoff_location` that holds a digit;
    `sender_domains`, the shipped sender list."""
    names: dict[str, str] = {}
    references: dict[str, str] = {}
    addresses: dict[str, str] = {}

    def name(value: Any) -> None:
        if isinstance(value, str) and len(value.strip()) >= 2:
            names.setdefault(_fold(value), value.strip())

    if book is not None:
        for alias in getattr(book, "me_aliases", ()):
            name(alias)
        for canonical, aliases in getattr(book, "guests", ()):
            name(canonical)
            for alias in aliases:
                name(alias)
    if extraction is not None and hasattr(extraction, "model_dump"):
        extraction = extraction.model_dump(mode="json")
    reservations = extraction.get("reservations") if isinstance(extraction, dict) else None
    for res in reservations if isinstance(reservations, list) else ():
        if not isinstance(res, dict):
            continue
        legs = [leg for leg in res.get("segments") or () if isinstance(leg, dict)]
        for holder in (res, *legs):
            for passenger in holder.get("passengers") or ():
                name(passenger)
        for holder in (res, *legs):
            seats = holder.get("seat_by_passenger")
            for passenger in seats if isinstance(seats, dict) else ():
                name(passenger)
        for key in ("confirmation_code", "pnr"):  # a reading's field, and a filed flight's
            code = res.get(key)
            if isinstance(code, str) and code.strip():
                references.setdefault(_bare(code), code.strip())
        for key in ("address", "pickup_location", "dropoff_location"):
            place = res.get(key)
            if isinstance(place, str) and any(ch.isdigit() for ch in place):
                addresses.setdefault(_fold(place), place.strip())
    for value in (*typed, *harvested):
        name(value)
    return Known(
        names=tuple(names.values()),
        references=tuple(references.values()),
        addresses=tuple(addresses.values()),
        sender_domains=frozenset(str(d).lower() for d in sender_domains),
    )
