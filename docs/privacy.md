# Privacy

A confirmation email holds names, booking codes, phone numbers and where you will be. tripbox
keeps all of it on your own disk. This page says what leaves your computer, when, and what
`tripbox report` removes before you share anything.

tripbox has no account, no telemetry and no analytics. It sends nothing to its authors.

## What stays on your computer

- `data/ledger.db`: the trips, the bookings, a record of each email read, what a model answered,
  the settings you choose on the page.
- `archive/`: every email that came by mail, as it arrived, as a plain `.eml` file, and every
  email or text you pasted or uploaded, as you handed it in. Only the worker and the Telegram bot
  mount it; the page does not. An upload also waits in the ledger until the worker reads it.
- `logs/`: log files. They mask the value of every secret setting and cut an email's subject at
  200 characters. They hold what the program did, such as a subject.
- `backups/`: archives of the ledger and your `config/` folder. An archive holds the saved emails
  only with `--with-mail`. It holds no `.env`. An archive is personal data: keep it private. The
  copies tripbox makes are never pruned.
- On a phone or computer that opens the page: a saved copy of the trips list and of each trip
  opened (at most 60 pages, never Settings or a shared page), for use with no network. The saved
  list can hold the mailbox's address. Signing out removes the copies, as does "Remove saved
  copies" in Settings, which is never saved, so it cannot be reached while offline. Once the
  newest copy is 30 days old with no page loaded online, the copies are refused and emptied. A
  deleted or merged trip's copy stays until its address is asked for again online. Saved copies
  are on by default only with the `password` and `none` logins; `TRIPBOX_OFFLINE=false` turns
  the feature off.

## What leaves your computer, and only when you set it up

| To | When | What |
|---|---|---|
| Your mail server | `TRIPBOX_MAIL_HOST` is set | A login, then each new message is fetched; with `labels`, a copy of it is filed in a folder. Nothing is deleted or marked read. |
| The model's provider | A model is set up, for an email the extractor could not read | The email's subject (cut at 500 characters), its sender's address, its date, its text (cut at 60,000 characters) and, with `anthropic`, up to three PDFs of at most 5 MB each, each with its file name as its title. When the extractor read part of the email, what it read goes too. Nothing from your settings, your trips or your travellers file. |
| AirTrail | `TRIPBOX_AIRTRAIL_URL` and a key are set, and not in a dry run | The flights you are on, and your companions' names and seats as guests; see [the flight log](airtrail.md). |
| A flight-data service (AeroAPI) | `TRIPBOX_ENRICH=aeroapi` | One lookup per landed flight: the airline, the flight number and the days around it. |
| Telegram or an ntfy topic | You set one up | A notice for each message handled: what was filed, and what needs a look. |
| Wikipedia and Wikimedia Commons | `TRIPBOX_COVERS=wikimedia`, for a trip with no cover photo | The worker sends a trip's place names to `en.wikipedia.org` and `commons.wikimedia.org` (at most two places a minute; each place costs up to three requests, two to the APIs and one image download) and downloads the chosen image from `upload.wikimedia.org` or `thumb.wikimedia.org`. What goes is the place name, a User-Agent naming tripbox's version and its source address (`TRIPBOX_SOURCE_URL`, else the project's), and, as with any request, your server's IP address. English Wikipedia only; no booking, date, traveller name or account. |
| Cloudflare | `TRIPBOX_AUTH=cloudflare` | The page fetches Cloudflare's public signing keys. It is the one request the page makes. |

Nothing else makes a request: tripbox's page loads nothing from another site, its airport table
and airline list ship with it, and `tripbox report` builds no network client at all. Two kinds of
link are yours to tap, and your browser then sends the other site what is in the link: a flight
links to FlightAware, with its flight number, and an address links to the maps site you choose
in Settings (Apple, Google or OpenStreetMap), with the address.

A cover photo you upload never leaves your install. The ledger keeps it as it came, its metadata
and GPS position included, only until the worker reads it: seconds, normally, but for as long as
the worker is stopped, and a backup taken meanwhile holds it too. Then only two resized copies
with no metadata remain. The SQLite in the image writes zeros over the space the original took,
though its bytes can stay in the ledger's write-ahead file (`ledger.db-wal`) until later writes
reuse that space.

The model's provider handles the email under its own terms. Choose a provider you are willing to
send your confirmations to, or run a model of your own (`TRIPBOX_LLM_PROVIDER=openai` with
`TRIPBOX_LLM_BASE_URL`), or none: then only emails with structured data are read and nothing is
sent to any model.

## Who can see the page

The page asks for a password, or for a login proxy or Cloudflare Access to vouch for you; see
[security](security.md). It tells search engines to keep away. A share link is the exception, by
your choice: whoever has the link sees the trip it shows, with the fields you ticked (names,
addresses, seats and times by default; codes and notes when ticked), and always the trip's title
and dates, and each booking's route and flight number or the hotel's or car company's name.
You can end a link at once with **Revoke**, on the trip's share page or in Settings.
A login proxy's own log sees a share link's address.

## Reporting a parse problem

When tripbox reads an email wrongly, a report lets someone fix it without seeing your email.
`tripbox report 12 > tripbox-report.txt` makes a redacted copy of message 12 and what tripbox did
with it. It begins with these words:

```
tripbox report: read all of this before you share it.
tripbox cannot know every personal detail in an email. Dates, times, flight and train numbers,
airports, cities, airline and hotel names and prices are kept, because they are what a parser
reads. Names, addresses, codes and numbers it recognised are replaced by words in square brackets.
Remove anything else you do not want to be public.
```

The report goes to standard output, or to `--out FILE` (a new file, mode 600). What was replaced
is counted on standard error. It reads your ledger and archive without writing to either. A file
that is not in the ledger is read in a temporary directory, with no model. Give the names of the
people on the booking with `--name "Alex Sample"` (once for each) when you can: a name nothing
gave tripbox is not replaced, and the report says so.

**Kept**, because a parser reads them: dates and times in any printed form, flight and train
numbers, airport codes, cities, airline and hotel names, prices, seats. Also kept, because nothing
tells them from those: a postcode that stands alone, and a number of five digits or fewer that no
label names.

**Replaced** by a word in square brackets, one word for one value wherever it appears
(`[NAME-1]`, `[EMAIL-1]`, `[PHONE-1]`, `[REF-1]`, `[TICKET-1]`, `[ADDRESS-1]`, `[NUMBER-1]`):

- the names of the travellers, in any order and case, and every address of mail (the domain of a
  listed travel sender stays);
- booking references a label names, and a code of capitals and digits that no label names unless
  it has the shape of a flight number;
- ticket numbers, phone numbers, street addresses (a house number and a street word, a post office
  box, a labelled address, a town with a state and a postcode), and any run of six digits or more;
- in a link, everything after the path (`?[QUERY]`), and any part of its path that is not a
  plain word;
- whatever a label calls personal, such as a date of birth, a passport or member number, the end
  of a card, a door code or a password;
- an encoded value, such as a long token, wherever it stands.

Every attachment except a calendar file, which is redacted like the body, is removed whole:
nothing inside one is read or shown, whether it is a PDF, an image or text (a list, a card, a
page).

**What it cannot do**, so that nobody takes its output for more than it is:

- letters run together are one word: `SAMPLEALEX` stays, and so does a name inside a longer word;
- a name nothing gave it (no travellers file, no `--name`, no greeting or title or label) stays,
  and so does anything personal said in a sentence, such as a request, a reason or a relation;
- a booking reference of letters alone stays unless a label names it;
- a street with no street word it knows, no label and no postcode and town beside it stays, as do
  a town or a postcode standing alone.
- an email forwarded by hand carries the forwarder's note and signature (an employer, a title, a
  phone number written in words); what no rule above recognises there stays.

**Read the report, and edit it, before you share it. Never attach an original email to an
issue.** The issue form asks you to confirm that you read the report and that it holds nothing
you do not want public.
