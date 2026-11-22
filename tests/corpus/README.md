# The corpus

Invented travel emails with known right answers. `tripbox eval` files them, in directory order,
into one empty ledger and scores what was read from each email and what filing it did. Nothing
here was ever sent or received: every person is one of the Sample family (`FAMILY.md`), and
`SCENARIOS.md` says what each email is there to prove.

## Layout

| Path | Holds |
|---|---|
| `corpus.json` | the settings the emails are read under, and which entries the demo files |
| `demo-view.json` | what the demo shows, in days from today: the trips and the unsorted bookings of "The demo" in `SCENARIOS.md`, with their checks |
| `flightlog.json` | what a flight log holds once every email is filed with one kept |
| `config/travellers.yaml` | who the owner and the companions are |
| `NN-slug/email.eml` | one email, byte for byte as it reaches the mailbox |
| `NN-slug/answer.json` | the model's answer to that email, or that the model is not asked |
| `NN-slug/extraction.json` | what the email says |
| `NN-slug/outcome.json` | what filing the email does |

`NN` is two digits and gives the order of arrival; `slug` is lower-case words joined by dashes.
An entry holds exactly these four files. One entry is one email; it may carry one other email as
an attachment, never several. An email is at most 1,000,000 bytes.

## corpus.json

```json
{
  "settings": {
    "TZ": "America/Chicago",
    "TRIPBOX_HOME_AIRPORTS": "MSP",
    "TRIPBOX_MAIL_ALLOWED_SENDERS": "alex@example.com,alex.sample@example.org",
    "TRIPBOX_MAIL_TRUST": "gmail"
  },
  "demo": {"anchor": "2027-06-16", "entries": ["01-markup-flight", "02-markup-hotel"]}
}
```

`settings` may name only `TZ`, `TRIPBOX_HOME_AIRPORTS`, `TRIPBOX_MAIL_ALLOWED_SENDERS`,
`TRIPBOX_MAIL_TRUST` and `TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS`. The corpus is always read with
these and nothing of the installation's own: no flight log, no notifications, writes on.
`demo.entries` are the entries `tripbox demo seed` files, in order; `demo.anchor` is the day the
corpus calls today. `demo-view.json` is what `tripbox demo seed` must produce for those entries,
relative to today: the trips, each booking in them with its days, its times, what it is, who is
on it and its open checks, and the bookings in no trip (`unsorted`), each with what it is, who
is on it and its open checks. When the demo's entries change it is generated again and checked
by reading it against the demo table in `SCENARIOS.md`, which also names the unsorted bookings
and every booking marked to check.

## flightlog.json

What a flight log holds once the whole corpus is filed, in order, with one kept from the start:
`{"flights": [{"flight", "date", "from", "to", "people", "cancelled"}]}`, sorted by date then
flight; `flight` is the carrier's code and the number as one word (`DL9412`), `date` the
departure's local day, `from` and `to` airport codes, `people` the owner as `me` and then each
companion the travellers file knows by their usual name, in name order. It is written by hand
from `SCENARIOS.md` and the entries' `outcome.json`, never from a run: a flight is listed when,
once every email is filed, its booking names the owner, is not put away, has no check open and
was not cancelled before it could be sent, and a flight cancelled after it was sent is listed with
`cancelled` true. A check counts here even when only a flight log opens it: the owner's flights
from a sender no list vouches for (`unverified_sender`) are not listed.

## email.eml: how mail reaches the mailbox

The mailbox trusts Gmail's own verdict, so an email is read only when its topmost
`Authentication-Results` header is Gmail's and vouches for the way it came. Use one of these
shapes, with the headers shown and no `Received` lines.

**auto-forward** — a Gmail filter passes the vendor's email on untouched (structured markup
survives this shape and the attached one; a forward by hand drops it):

```
Return-Path: <alex+caf_=trips=example.com@example.com>
X-Forwarded-To: trips@example.com
X-Forwarded-For: alex@example.com trips@example.com
Authentication-Results: mx.google.com;
       dkim=pass header.i=@SENDER-DOMAIN;
       spf=pass smtp.mailfrom="alex+caf_=trips=example.com@example.com";
       dmarc=pass header.from=SENDER-DOMAIN
From: Vendor Name <address@SENDER-DOMAIN>
To: alex@example.com
```

followed by the vendor's own `Subject`, `Date`, `Message-ID` (every email has all three),
`MIME-Version` and body.

**gmail-forward**, **apple-forward**, **outlook-forward** — Alex forwards it by hand. The headers
are Alex's (from the work address: `alex.sample@example.org`, and `example.org` throughout):

```
Return-Path: <alex@example.com>
Authentication-Results: mx.google.com;
       dkim=pass header.i=@example.com;
       spf=pass smtp.mailfrom=alex@example.com;
       dmarc=pass header.from=example.com
From: Alex Sample <alex@example.com>
To: trips@example.com
Subject: Fwd: <the original subject>
Date: <when Alex forwarded it>
Message-ID: <a unique token@example.com>
```

and the body opens with the mail program's own block, then two blank lines in the Gmail style
and one in the Apple and Outlook styles, then the original text:

| Shape | Opening line | Header block that follows |
|---|---|---|
| gmail-forward | `---------- Forwarded message ---------` | `From:`, `Date: Wed, Jan 27, 2027 at 6:55 PM`, `Subject:`, `To:` |
| apple-forward | `Begin forwarded message:` then a blank line | `From:`, `Subject:`, `Date: February 9, 2027 at 11:58:12 AM CST`, `To:` |
| outlook-forward | `-----Original Message-----` (subject prefix `FW:`) | `From:`, `Sent: Wednesday, February 10, 2027 4:31 PM`, `To:`, `Subject:` |

**attached** — Alex's headers as above, `multipart/mixed`: a short text part and one
`message/rfc822` part holding the original email whole.

**direct** — the sender writes to `trips@example.com` themselves. Only a stranger does.

```
Return-Path: <address@SENDER-DOMAIN>
Authentication-Results: mx.google.com;
       dkim=pass header.i=@SENDER-DOMAIN;
       spf=pass smtp.mailfrom=address@SENDER-DOMAIN;
       dmarc=pass header.from=SENDER-DOMAIN
From: Sender Name <address@SENDER-DOMAIN>
To: trips@example.com
Subject: <the sender's subject>
Date: <when the sender sent it>
Message-ID: <a unique token@SENDER-DOMAIN>
```

Gmail vouches for the sender's own domain, and the email is still refused: the sender is not one
Alex allowed.

Every outer `Date` header carries the offset `America/Chicago` has on that day; an attached
original keeps its own zone.

## answer.json

```json
{
  "wire_schema": 1,
  "source": "hand-written",
  "model": null,
  "answer": {"document_type": "booking", "confidence": 0.9, "reservations": []}
}
```

`answer` is what a careful model answers when it reads only this email under the rules it is
given (`SYSTEM` in `src/tripbox/parse/provider.py`), in the wire schema (`WireExtraction` in
`src/tripbox/parse/models.py`): nothing the email does not print, times local as printed.
`source` is `hand-written`, `recorded` (then `model` names the model that gave it) or `not-asked`
(then `answer` is null: the deterministic extractor reads the whole email, or the email is
refused before anything reads it). A hand-written answer has `confidence` 0.9.
`tripbox eval --recorded` answers from these files, whatever their `source`: the flag is not the
`source` value `recorded`.

## extraction.json

What the email says, in the pipeline's own vocabulary (`Extraction`, `Reservation` and `Segment`
in `src/tripbox/parse/models.py`), written from the email and not from the answer:

```json
{
  "document_type": "booking",
  "reservations": [
    {"kind": "lodging", "status": "confirmed", "confirmation_code": "ZWT876",
     "passengers": ["Alex Sample"], "property_name": "Example Suites Boston Seaport",
     "check_in": "2027-03-11", "check_out": "2027-03-14"}
  ]
}
```

A compared field left out is null: the email does not print it. Where the email itself prints a
fact in two forms, or where two readings are equally right, a field may list every acceptable
reading, the preferred one first (`"origin": ["MSP", "MINNEAPOLIS/ST PAUL"]`).

Only these fields are compared, each after reducing both sides to a plain form:

| Where | Fields | Compared as |
|---|---|---|
| the email | `document_type` | the word |
| every booking | `kind`, `status` | the word |
| every booking | `confirmation_code` | upper-case letters and digits |
| every booking | `passengers` | the set of first and last names, upper-case, in either order of writing |
| a flight leg | `carrier_iata`, `origin`, `destination` | upper-case letters and digits |
| a flight leg, a train leg | `flight_number`, `train_number` | the number without leading zeros |
| a flight leg, a train leg | `departure_local`, `arrival_local` | `YYYY-MM-DDTHH:MM` |
| a train leg | `origin`, `destination` | lower-case letters and digits |
| a stay | `property_name` | lower-case letters and digits |
| a stay | `check_in`, `check_out` | the day |
| a car | `provider`, `pickup_location` | lower-case letters and digits |
| a car | `pickup_time`, `dropoff_time` | `YYYY-MM-DDTHH:MM` |

Any other field of a booking or a leg (the provider of a flight or a stay, price, currency,
address, cabin, seats, offsets, a stay's hours) may be stated where the email prints it and is
not scored. A booking's `status` left out is the same as `unknown`.

A value where the truth has none counts as wrong, and so does a booking or a leg the truth does
not hold: nothing is ever filled in.

## outcome.json

```json
{
  "status": "processed",
  "read_by": "model",
  "document_type": "booking",
  "items": [
    {"kind": "lodging", "key": "lodging|ZWT876", "result": "filed",
     "people": {"me": 1, "guests": 0, "others": 0},
     "booking": {"status": "confirmed", "travellers": ["Alex Sample"], "trip": "Boston",
                 "checks": [], "sync": "off"}}
  ]
}
```

- `status`: `processed`, `ignored` (nothing to file, or nothing new), `rejected` (not from an
  allowed sender: never read) or `failed`.
- `read_by`: `kitinerary` (the deterministic extractor read all of it), `model`, or null.
- `document_type`: the kind of document the email was read as, or null when nothing read it.
- `items`, sorted by kind then key: the booking's `key`; `result` is `filed` (new), `updated`,
  `duplicate`, `ignored` or `failed`; `people` counts this email's travellers (the owner, known
  companions, anyone else); `booking` is the booking as it stands once this email is filed —
  its status (`confirmed` or `cancelled`: a change notice leaves it `confirmed`), everyone on it
  by their usual name, sorted, the title of its trip, null for a booking with no date (it is
  in no trip), its `checks` and its `sync` — or null when there is none.
- `gap` would appear only where filing is not yet what it is meant to be, naming which behaviour
  is missing while the rest of the file records what happens today; no entry carries one today.

`checks` are the checks open on the booking once this email is filed, in the order of this
table, `[]` when none is: what is doubtful about the booking, shown on the page with its
sentence. A hard check names a field that is wrong and closes only when that field is fixed. A
soft check closes when the owner looks at the booking (Looks right, or saving the edit form); one
the booking's own fields raise also closes when they no longer show it, and one an email raised
opens again only when a newer email raises it again.

| Check | Kind | Sentence on the page | Closes when |
|---|---|---|---|
| `carrier` | hard | The airline's two-character code is missing or unreadable. | the code is fixed |
| `flight_number` | hard | The flight number is missing or is not one to four digits. | the number is fixed |
| `origin` | hard | The departure airport is missing or is not a known airport code. | the airport is fixed |
| `destination` | hard | The arrival airport is missing or is not a known airport code. | the airport is fixed |
| `departure` | hard | The departure time is missing or unreadable, or its time zone is not known. | the time is fixed |
| `arrival_before_departure` | hard | The arrival is before the departure. | a time is fixed |
| `check_in` | hard | The check-in date is missing or unreadable. | the date is fixed |
| `check_out` | hard | The check-out date is missing or unreadable, or is before check-in. | the date is fixed |
| `pickup_time` | hard | The pick-up time is missing or unreadable. | the time is fixed |
| `dropoff_time` | hard | The drop-off time is missing or unreadable, or is before pick-up. | the time is fixed |
| `stations` | hard | The departure or arrival station is missing. | the station is filled in |
| `airtrail_airport` | hard | The flight log does not know one of this flight's airports. | the airport is fixed, or the flight log knows it when the next pass asks again (a flight log only) |
| `low_confidence` | soft | The email was hard to read, so the details may be wrong. | the owner looks |
| `unverified_sender` | soft | The sender of this email could not be verified. | the owner looks (open only on the owner's flight, a flight log only) |
| `owner_unknown` | soft | No traveller is named, so it is not known whether you were on this flight. | the owner answers or a traveller is named; looking does not close it (a flight log only) |
| `date_past` | soft | The date is more than 2 days before the email was sent. | the owner looks, or the date is fixed |
| `date_far` | soft | The date is more than 400 days after the email was sent. | the owner looks, or the date is fixed |
| `long_leg` | soft | The flight takes longer than 20 hours. | the owner looks, or a time is fixed |
| `missing_name` | soft | The name of the hotel or the rental company is missing. | the owner looks, or the name is filled in |
| `unknown_cancellation` | soft | A cancellation email could not be matched for certain: check this booking. | the owner looks |
| `airtrail_refused` | soft | The flight log refused this flight. | the owner looks, or a later attempt to send it succeeds (a flight log only) |
| `airtrail_still_lists` | soft | Your flight log still lists this flight as yours. Remove it there if you did not fly. | the owner looks, or the owner is on the flight again (a flight log only) |

`sync` is where the booking stands with a flight log: `off` (nothing is sent: no flight log is
kept, or the booking is not the owner's flight, or it is held out, or it is a cancelled flight
that was never sent), `blocked` (the owner's flight, held back while a check is open or nobody is
named), `pending` (to be sent at the next pass) or `synced` (the flight log holds it as the
booking says). The corpus is read with no flight log, so every `sync` is `off`, and the five
checks marked "a flight log only" are never open in it.

Keys: a flight is `CARRIER|NUMBER|YYYY-MM-DD|ORIGIN` (the departure's local day; ORIGIN is an
airport code the bundled table knows); a stay is `lodging|CODE`; a car is `car|CODE`; a train
is `train|NUMBER|YYYY-MM-DD|STATION`, the departure's local day and the departure station in
lower-case letters and digits, and a train that prints no number takes its provider's name, in
lower-case letters and digits, in NUMBER's place; a booking that cannot be keyed yet is
`draft|HASH|N`, where HASH is the first sixteen hex digits of the SHA-256 of the email's bytes
and N is the booking's place, from 0, among the bookings as the email is read, a copy that
folds into an earlier booking still counted in its place (one leg read twice, then another leg,
gives 0 and 2).
A trip is titled by the place of the booking that opens it: for a flight, the city it lands in,
or the city it leaves from when it lands at home; a stay's city; a car's pick-up city; a train's
destination.

## Writing a new email

1. Write `extraction.json` and `outcome.json` first, from what the email is meant to say. Then
   the email. Then the answer.
2. Run `tripbox eval --recorded` on your working tree, in the test image `tools/verify/ci.sh`
   builds (`tripboxoss:test`), with the repository mounted over the copy baked into the image (run
   without the mount, it scores the corpus as it was at the last build, not your new email):
   `docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e PYTHONPATH=/repo/src
   -e PYTHONDONTWRITEBYTECODE=1 tripboxoss:test python -m tripbox eval --recorded`. Where it disagrees, the email or the answer changes. A truth
   file changes only when it misstates what the email says. An email that is right and filed
   wrongly is a defect to report, not a truth to bend.
3. Everyone is one of the Sample family. Flight numbers have four digits and begin with 9;
   booking codes are three letters from Q–Z and three falling digits; every date is in 2027 and
   no other number from 1900 to 2099 appears; streets, phones and loyalty numbers are visibly
   fake; invented vendors carry Example, Sample, Exemple, Beispiel or Ejemplo in their name and
   write from `.example` addresses; links go to `https://www.example.com/`. Airlines, airports
   and stations are real; a real sender domain appears only where a rule needs one, and then
   only one listed in `src/tripbox/lists/senders.yaml`. A real airline's address inside a
   forward by hand, and a real airline's Message-ID, are at an invented `.example` domain; phone
   numbers come only from ranges set aside for fiction (US: a geographic area code and
   `555-01xx`; UK `020 7946 0xxx`; France `01 99 00 xx xx`; Munich `089 99998-xxx`; none
   elsewhere); no real company's product names or published figures appear.
4. Nothing is copied from a real message: no wording, markup, link or image. Structured markup
   is written from the schema.org vocabulary: a `<script type="application/ld+json">` block in
   the HTML part holding one `FlightReservation` per traveller per flight (`reservationNumber`,
   `reservationStatus`, `underName.name`, `reservationFor` with `flightNumber`,
   `airline.iataCode` and `name`, `departureAirport.iataCode`, `arrivalAirport.iataCode`,
   `departureTime` and `arrivalTime` with their offsets), a `LodgingReservation`
   (`reservationFor.name` and `address`, `checkinTime`, `checkoutTime`) or one
   `TrainReservation` per traveller (`reservationFor` with `trainNumber`, `provider.name`,
   `departureStation.name`, `arrivalStation.name`, the two times).
5. Weekdays are computed, never remembered.
6. A new entry takes the next free two-digit number, and its `Date` is not earlier than the
   entry before it. It gets a row in the entries table of `SCENARIOS.md` (and one in its
   bookings table when it books something new), its name joins the list in `tests/test_corpus.py`
   that pins which entries the corpus holds, and when it files a booking or opens a trip, the
   ledger totals asserted in `tests/test_eval.py` are counted again by hand. The scorecard's
   figures are also written out in `docs/models.md` (the corpus size in its eval paragraph and
   the whole example scorecard, which you print again with the command above), and the arm64
   workflow `.github/workflows/arm64.yml` pins how many emails the extractor reads
   (`parsers: kitinerary: N`), a count a new email that carries structured data changes. The
   workflows no longer name the number of emails: they accept any scorecard whose last line says
   every email is right.
