# How tripbox is put together

One image, three roles: `web` (the page), `worker` (everything that reads, writes elsewhere or
runs on a clock) and, if you use it, `bot` (a Telegram bot). They share one SQLite file, the
ledger. This page follows an email from the mailbox to the page, then says where each kind of
fact lives. The facts about the code, for people who change it, are in
[the contributor's guide](../AGENTS.md).

## The path of an email

1. **Mail.** The worker polls the mailbox over IMAP (`mail/imap.py`) every five minutes, or takes
   what you handed in on the page, or what `tripbox ingest` reads. Each message is saved whole as
   a `.eml` file in `archive/`. `mail/normalize.py` checks the sender (the rules are in
   [mail](mail.md)), takes the text and attachments out, and finds the emails attached to it.
2. **The extractor.** kitinerary, KDE's extractor, reads structured data and airline and hotel
   templates, with no model (`parse/kitinerary.py`). It is a separate binary the image carries.
3. **The model.** An email the extractor could not read goes to the provider you chose
   (`parse/provider.py`, `anthropic_provider.py`, `openai_provider.py`), under a spend cap
   (`parse/budget.py`). With no model, or while the cap is reached, the message waits and is read
   later. The model's answer is stored with the message, so reading it again costs nothing.
4. **Validation.** `validate/` checks every field: airports against the table that ships with
   tripbox, times, names against `travellers.yaml`, the sender. A field that fails a check, or
   that the email does not print, is left empty or marked: tripbox never invents one. A booking
   with a doubt is filed with a check on it, which the page shows as **To check**.
5. **The ledger.** `write/planner.py` decides what each booking is: new, a change to one it
   holds, a cancellation, a duplicate, or one that completes a draft. `trips/store.py`
   (`TripStore`) is the one writer of trips and bookings, and `ledger/` is the schema and
   the queries. Bookings are deduplicated by a key built from what they print, and an owner's edit
   on the page is never undone by an older email.
6. **Trips.** `trips/grouping.py` puts bookings into trips by their dates and places. A booking
   with no date waits in **Unsorted**.
7. **The page.** `web/` is a Starlette application that renders trips from the ledger, takes
   bookings to read (stored, for the worker), edits, share links and settings. It reads the
   ledger and never talks to the mailbox, the model or AirTrail.
8. **The worker.** `worker.py` is one process that beats every five seconds: it records its
   status for the page, reads one waiting upload, runs a sync pass for AirTrail and starts the
   mail poll and, at 03:30 in your zone while enrichment is on, the enrichment as processes of
   their own.

Optional ends: `write/` keeps your flights in AirTrail; `notify/` sends a notice for each message
to Telegram or ntfy; `enrich/` fills in landed flights from a flight-data service.

## The capability split

Three containers run the same image with different settings and folders.

| | `web` | `worker` | `bot` |
|---|---|---|---|
| Settings | only the page's own, and its login pair | every setting, from `.env` | every setting, from `.env` |
| `data` (the ledger) | yes | yes | yes |
| `logs` | yes | yes | yes |
| `config` | read-only | read-only | read-only |
| `archive` (raw emails) | no | yes | yes |
| `backups` | no | yes | no |
| Published port | yes | no | no |

The page is the container a browser can reach, so it holds no mailbox, model, AirTrail,
flight-data or notification credential and no raw email. `tests/test_compose.py` pins this. The
page holds no mailbox or AirTrail key, so it cannot read mail or write to AirTrail itself, even if
it is broken into. It can queue an upload for the worker and change trips in the ledger, and the
worker treats both as the owner's own, so a page that is broken into could cause a write there
indirectly.

## The folders

| Folder | Holds | Written by |
|---|---|---|
| `data/` | `ledger.db`, the one SQLite file, and the copies an upgrade makes | web, worker, bot |
| `archive/` | every raw email, `YYYY/MM/<time>_<uid>_<hash>.eml` | worker, bot |
| `logs/` | one log file per process | each process |
| `config/` | `travellers.yaml` and the other optional files | you |
| `backups/` | archives from `tripbox backup` | the worker |

## Where each kind of fact lives

- **Settings:** the environment, read by `config.py`, one entry for each setting. `.env.example`
  is generated from the same entries, and [configuration](configuration.md) has a row for each.
- **What an email said:** the ledger's `messages` table, with the model's answer if one was asked,
  and `items`, a log of what each message did.
- **A booking's present state:** the ledger's `trip_items`, written only by `TripStore`; an
  owner's edits are kept beside it and applied again whenever an email is merged.
- **Trips:** the ledger's `trips`; the list and every trip's text come from `trips/format.py`.
- **Share links, sessions, display choices, the worker's record:** small tables and `kv` rows in
  the same ledger.
- **What AirTrail knows:** the ledger keeps, for each flight, what AirTrail was last told and its
  id there. The airports and airlines come from the table that ships with tripbox, and AirTrail is
  asked only for its own ids.
- **The emails themselves:** `archive/`. A backup holds them only when asked.
