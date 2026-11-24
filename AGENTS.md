# AGENTS.md — tripbox

For anyone, human or agent, who changes this repository. The README says what tripbox is for and
how to run it; [docs/architecture.md](docs/architecture.md) is the short tour of the code; this file
holds the rules a contribution must keep and the facts about the code that are not obvious from
reading it. [CONTRIBUTING.md](CONTRIBUTING.md) says how to send a change.

tripbox turns forwarded travel confirmation emails into trips: a deterministic extractor (KDE's
kitinerary), a model as fallback, strict validation, a SQLite ledger that dedupes bookings and
groups them into trips, and a server-rendered, phone-first page. Optionally the owner's flights
are also kept in an AirTrail flight log. One image runs three roles: `web`, `worker` and an optional
`bot`.

## Rules

- **No real data.** No real email, name, booking code, address, ticket or trip enters the
  repository: not in a test, a fixture, a doc, an issue or a commit message. Write a test from
  scratch with the invented Sample family (`tests/corpus/FAMILY.md`), `example.*` hosts and dates in
  2027. A bug report brings `tripbox report`'s redacted output, never an original email.
- **Never invent, never delete.** Every extracted field is optional and nullable; anything an
  email does not print stays empty. Nothing is ever deleted in AirTrail. A flight is written to
  AirTrail only when the owner is on that leg. Page deletions are soft.
- **The capability split.** The container a browser reaches (`web`) holds no mailbox, model,
  AirTrail, flight-data or notification credential, and no raw-email mount. It receives only
  the settings marked `web=True` in `config.KEYS` and its login pair. `tests/test_compose.py`
  pins this; a change that weakens it is refused.
- **Trips are written only through the trip store** (`trips.store.TripStore`) and
  `ledger/repo.py`, never by hand: `tests/test_store_is_the_only_writer.py` fails otherwise.
- **Comments say what the code does and why, in the present tense.** They carry no ticket, task or
  review number.
- **Tests run in Docker.** The host needs bash, git, tar and Docker, and no
  Python, uv or compiler. The gate has been run on Linux only; macOS and WSL2 are untried. `tools/verify/ci.sh` is the gate and must end `CI OK` before a change is
  offered.
- **The header rule.** Every source file starts with the two lines
  `SPDX-License-Identifier: AGPL-3.0-only` and `Additional terms: see NOTICE` (as comments);
  `tools/verify/spdx.py --write` adds them, run in the `Dockerfile`'s Python image as
  `CONTRIBUTING.md` shows.
- **Settings.** A new setting is a `Key` in `config.KEYS` with its check, a row in
  `docs/configuration.md` and its lines in `.env.example`, which `config.env_example()` generates:
  never edit that file by hand. A bad value is a `ConfigProblem`; a secret's value is never shown
  or logged.
- **Context files.** Edit `AGENTS.md`, never the `CLAUDE.md` beside it, which only imports it, and
  never put an `@path` import in an `AGENTS.md`. The two nested files are
  `src/tripbox/web/AGENTS.md` (the page) and `src/tripbox/write/AGENTS.md` (the flight log): read
  the one for the area before you work there.
- **Commits** are small, one per step, each with a subject in the present tense.

## Facts about the code (keep them true as the code changes)

- Settings: every setting is a `Key` in `config.KEYS`; a core problem stops commands (`backup`
  and `restore` only a `config.PATH_KEYS` one's), a web problem stops `web`, any other invalid feature is left off and logged; a bad value of a
  `config.DISPLAY_ONLY` key (a link shown) is a problem that turns nothing off; `tripbox doctor`
  lists them. The page listens on `TRIPBOX_LISTEN`, loopback only with no login, as is `web --host`
  outside the image (`TRIPBOX_IN_IMAGE`). In `compose.yaml` `web` passes `--host 0.0.0.0` and
  mounts `data`, `logs`, `config` (ro); `bot` adds `archive`; `worker` also `backups`.
  `.env.example` is `config.env_example()`'s output, never edited by hand.
- Folders: `files.problems` checks each as the process uses it (a probe file made and removed;
  absent is made on first use); `doctor` lists them, then a worker row read without writing;
  `web` checks data and config at start, `work` and `bot` data, archive and config.
- The web page (`src/tripbox/web/`, served at `TRIPBOX_BASE_URL`): its facts are in
  `src/tripbox/web/AGENTS.md`; read it before working there.
- Trip text is rendered in one place, `trips/format.py` (notifications, the CLI and the share
  page); hotel and car times take the zone of the latest flight that has landed by then
  (`grouping.zone_at`), flights their own zones.
- kitinerary-extractor (a Qt binary) is KDE's static build, sha256-pinned, on amd64 and Debian's
  older `libkitinerary-bin` elsewhere (`KITINERARY_SOURCE`); it needs `QT_QPA_PLATFORM=offscreen`
  and a UTF-8 `LANG` (set in the image). The image runs as 1000:1000 (any uid works).
- `claude-haiku-5-5`: no `temperature` (400); `messages.parse`, slim wire schema (the full
  `Extraction` fails "Schema is too complex"; a new schema's first call takes ~50 s). The
  OpenAI-compatible provider sends text only: a JSON schema, else JSON mode. Every call is
  capped (`budget.Budgeted`; none for `BREAKER_SECONDS` after an `unavailable`);
  an answer read back is never capped or counted (`Usage.asked`).
- The wire schema's shape (`WireExtraction`) per version is pinned in `tests/data/wire_schema/`.
  A changed field, type or choice bumps `WIRE_SCHEMA_VERSION`, adds the next file and converts or
  re-records stored answers; a change in rendering alone (a library upgrade) regenerates the
  current file in the same commit and says so.
- Airports come from the table bundled with the app (`airportsdata`, read once per process by
  `validate.refdata.Airports`); the validator never asks AirTrail. AirTrail is asked only for its
  own ids (`write.airtrail.AirTrailRefs`), when a flight is written; the ledger's `ref_cache`
  holds those answers alone.
- Airports that came back as city names are resolved in `rules.printed_route` from a code pair
  printed in the same email, and only when the bundled table's city for each code matches the
  printed city and exactly one pair fits. Never resolve a city to "its" airport without a printed
  code.
- Passengers and status are per leg: the validator classifies every leg on its own, and
  `planner.merge_items` builds a new object instead of mutating a shared one. A booking that
  names nobody takes its names from the booking it is for (under its key, or the one it moved
  from) when that holds its confirmation code (a cancellation: only when that is the booking's
  one code, since two bookings may share a flight); one that is no booking the store holds takes
  names only when the visible bookings of its kind under the code all carry the same
  travellers and none holds several codes (seats booked apart), and nobody's otherwise
  (`planner.inherit_passengers`, after the booking is found). The validator raises
  `unverified_sender` on every flight from a sender nobody vouched for, whoever it names, on
  every reading of its message; whose flight that holds back is decided where the booking is
  filed (`checks.open_checks`: the owner's, while the flight is in the flight log's
  reconcile), and a duplicate puts no finding into the booking. A model's answer of over
  `rules.MAX_BOOKINGS` bookings marks each `low_confidence`. A soft finding is dated by when
  its message's bookings first reach the ledger (`messages.found_at`):
  the later of its receipt and when they are planned, once the extractors have answered, so a
  look made while the model reads closes nothing it did not show; every later reading keeps it
  (`run._found_at`); never the email's own Date. A look never moves back (`checks.looked`).
- Logs: the `poll` and `enrich` the worker starts write one file; the long-lived bot, web and
  worker write their own (`setup_logging(name=…)`: `tripbox-bot`, `tripbox-web`,
  `tripbox-worker`): a rotating handler shared by processes loses lines. Logs and notices mask
  each `secret=True` value (`log.add_secrets()`, `redact()`) and cut email header text at
  `run.SUBJECT_SHOWN`.
- Ledger schema changes: version 1 is released, so never edit `SCHEMA` in `ledger/db.py` in place
  (`tests/test_ledger.py::test_the_released_v1_schema_is_not_edited_in_place` fails). Bump
  `SCHEMA_VERSION`, add a step to `MIGRATIONS` and add the old version's
  shape to `RELEASED_SHAPES`. Every ledger states its shape (`kv` row `schema_shape` =
  `SCHEMA_SHAPE`, a hash of `SCHEMA`); `Ledger.open` refuses, writing nothing, one of this
  version stating another (an in-place edit refuses every older ledger), an older one stating no
  released shape or lacking a step, and a newer one (`OutdatedLedger`, `ledger: …`, exit 2).
  `SCHEMA` never runs on an older ledger: the steps (foreign keys off) make all it adds, the
  last ending in a new ledger's schema; before the first, the file is copied beside itself
  (`ledger-before-v<n>-<UTC>.db`), else none runs. A table is rebuilt with `db.rebuild_table`
  (beside, copy, drop, rename; its `sqlite_sequence` row kept), never renamed first.
  `trip_items.trip_id` is NULL exactly when `start_at` is (a booking with no date has no trip).
- Dedupe order in `pipeline/run.py`: sha256 of the raw bytes → Message-ID of an already-handled copy
  → the booking in the trip store. A model's answer is stored with its message
  (`messages.answer_json`, before the demo's date shift): a replay reads it back
  (`run.StoredAnswer`), `replay --reread` asks again. A message is read again
  only when forced, never finished (`archived`), `failed` or `waiting` (no reader read it whole,
  nothing of a part kitinerary read is filed, and the model cannot now: `run.WAIT_REASONS`; a poll that reads the mailbox, or has none, reads it
  (`run.resume_waiting`), oldest first, until one waits again; all then wait for its reason; one
  whose archived email is gone is failed, `run.ARCHIVE_MISSING`, told once), or
  when the owner hands in bytes the sender check rejected as mail (the row's source then becomes the
  owner's). The check (`TRIPBOX_MAIL_TRUST`) is `gmail` (an allowed From bound by Gmail's
  dkim+dmarc, or its `+caf_` envelope by spf) or `mailbox` (an allowed From, envelope or
  X-Forwarded-For, nothing authenticated, nobody vouched for: every mailed flight waits for a look).
  Bytes the owner hands in (`process_raw(trusted=True)`: an upload, an ingest) and every later
  reading of a `run.HANDED_IN` row are the owner's own (`normalize(gate=False)`) in both modes; the
  emails attached to an allowed sender's message (`extra_rfc822`, at most `normalize.MAX_ATTACHED`
  and its size) are filed by it alone, whatever their From says: the owner's when it was handed in
  or vouched for in `gmail` mode, else `ungated` (nobody's from its first row), the message
  unfinished until each is (`run.ATTACHED_UNFILED`: the resume pass takes it up, and at the
  `run.MAX_RESUMES`th try fails it, told once). Mail once accepted is not checked again;
  `messages.origin_ok` keeps whether that acceptance was
  authenticated or the owner's (`run.ORIGIN_*`): if so, the sender is worked out from the headers on
  every reading, but an attached email the owner vouched for (`ORIGIN_OWNERS`) is theirs on every
  reading, its row read on its own too; if not, it is nobody's in either mode until the owner hands the bytes in (a
  finished row is skipped unless forced). A handed-in row with no sender (`from_addr` NULL; an email
  reading records one) was read and is replayed as pasted text.
  An email set aside for an earlier delivery (same Message-ID) names it
  (`MessageResult.same_as`), and the emails attached beside it are filed all the same; an upload of it shows that delivery's bookings (`uploads.came_to`).
  Same-key items of one message are merged (`planner.merge_items`).
  `trip_items.item_key` is UNIQUE. One rule, `validate.keys.real_key`, keys every booking, built
  only from parts the booking has (a flight's origin must be in the bundled airport table); a
  flight backfilled from AirTrail is keyed by it with AirTrail's date. A mailed
  booking that lacks a part gets a draft key, `draft|<first 16 hex of the message's
  sha256>|<place in the email>`, and a backfilled one keeps `flight|airtrail|<id>`. Two drafts
  merge only as two copies of one flight leg (`keys.same_leg`: the same carrier, number, day and
  origin as printed), under the first's key.
- One plan (`write/planner.py`) for every booking, whether or not a flight log is kept: the
  trip item is the one state of a booking, and `items` is only the log of what each message
  did, each row written once its booking's action is done (one row per booking, `n` its place
  in the message: `outcome` holds filed, updated, duplicate, ignored or failed; `detail` the
  word beside updated or ignored — add_passengers, changed, rescheduled, cancel,
  remove_passengers, completes_draft, fills_in, older_copy, unknown_cancellation — or a failure's
  sentence; `trip_item_id` the booking). Only `Items.repoint` (two bookings became one) and
  `Items.forget` (a purged booking) change a row; a message read again has its rows deleted
  and its status reset in one transaction, and written anew. Nothing is held back: a message
  is processed, ignored, rejected, failed or, unread, `waiting`, and every booking it is read
  for is filed with its checks. `trip_items.source_message_id` is the last message that filed
  or changed the booking (NULL for one added by hand or from AirTrail). The booking an email is
  for is the one under its key or an alias (`TripStore.find`, hidden or not: a booking the
  owner put away comes back only for an email sent after that, `removed_at` or the trip's
  `deleted_at`; a re-forward or a late copy is merged in and it stays put away), else the one
  it moved from (`planner.moved_from`: same
  code, kind and route, the same number on another day or another number that day, sharing a
  traveller, and, when it holds more than one code (seats booked apart), naming everyone it
  holds or the owner; under a real key, or under the draft key an edit left it with
  (`keys.own_draft_key`), never an email's draft; exactly one candidate among the bookings the
  owner can see, and only when none of those could have moved, one that was put away; never
  one a cancellation of the same message takes every traveller the leg names off while others
  stay on it, `planner._taken_off`: those travellers leave it, and the leg is filed; a
  booking holding several confirmation codes is moved by an email that names everyone aboard
  or names the owner; an airline change mailed per code that reaches the other traveller's
  notice first files that leg on its own), else,
  for an email with a real key, the one draft it completes (`TripStore.drafts_like`: a visible
  draft under its code that says its day
  and its number or departure time, and its departure time when it does not say its origin
  (a through-flight keeps one number over two legs in a day), agrees with it wherever both say
  something, a value failing its hard check saying nothing, and shares a traveller with it
  when both name someone (`passengers.person`, the rule `moved_from` uses too); exactly one,
  and no other booking of the message fits it), which it updates (`completes_draft`, which
  records no change), else, for a stay or a car, the one booking typed in by hand with no code,
  not cancelled, that has its start day, its end day and its name (property or company, by
  words: one's all among the other's) wherever both say one, and agrees on at least one of
  the two (`TripStore.typed_in_like`, under any key; the same rule of one), which it fills in
  (`fills_in`). A draft lends no names on its own. Then: add travellers (never one the
  owner took off, nor one an email newer than it took off: `store.removed_by_email`), record a
  change (never from an older copy, nor from a plain booking after
  `payload["change_recorded"]`, nor in what an owner's edit holds against the email:
  `store.held_edits`, `planner._not_held`), or duplicate; a duplicate puts nothing into the
  booking, so it raises no check. With no booking it is filed with its checks (under a draft
  key when its fields make no real one; with no date, in no trip).
  A cancellation acts only on the booking it names: neither `moved_from` nor `drafts_like` is
  asked for one. One for a flight or a train the booking is no longer on is an older copy
  (`planner._for_a_flight_it_left`): one the booking is leaving through another booking of the
  same message, when that leg names everyone the cancellation takes off (the held travellers
  it names; all of them when it names nobody; a leg that names nobody carries everyone); or
  one it has left through a recorded change, when the cancellation prints no name or names
  every traveller the booking holds. Otherwise it is decided like any cancellation, so the
  owner is never left live on a flight the email took them off (a stay or a car never leaves a
  key: its other key is a code the owner corrected, and it is cancelled): what it does goes by
  who it names among the travellers the booking holds (`planner._cancellation`: cancel, remove
  them, or nothing when it names none of them; the owner's name cancels only a booking that
  holds the owner; a traveller named by the same message's new leg that moves the booking is
  not removed). One that
  names nobody and took no names from the booking (it prints another code or none, or a code
  two bookings share) cancels nobody: it is `ignored`, detail `unknown_cancellation`
  (`checks.UNKNOWN_CANCELLATION`), and marks the booking with that check (`TripStore.mark`). A
  cancellation for no booking is filed cancelled, marked the same way.
- The flight log (`src/tripbox/write/`: the sync pass, the lifecycle, a backfilled flight,
  enrichment): its facts are in `src/tripbox/write/AGENTS.md`; read it before working there.
- The notifier (`notify/`: Telegram, ntfy or none) sends a message's lines (a marked booking's
  ends `to check:` and its checks' sentences; no flight log named unless one is kept), then a
  card per marked booking the owner can see (Telegram's with `telegram.check_keyboard`), at
  most `run.CARD_LIMIT` cards and `FILED_NOTICES` lists per mailed message, saying how many
  more; the
  bot's `/check` lists up to ten that way, with how many more, and its `ck:<id>:<what>` buttons
  call `looks_right` (at the time its card was made, `ck:<id>:ok:<seconds>`, never later than
  now, so what an email raised after the card stays open; one that closes nothing answers that
  nothing is left), `owner_answer` or `send_again`, then put the booking as it now is, and the
  buttons still open, on the card. `trips.id` and
  `trip_items.id` are never given twice (AUTOINCREMENT), so an old card never acts on another
  booking. Which answers a booking offers is one rule, `checks.actions` (the owner's answer and
  Send again only while a flight log is kept), for the card's buttons, for a tap on one (a
  button the page would not offer now answers that nothing is left) and for the page: a
  marked booking's box on its trip page (its checks' sentences, then a one-button form for
  each answer and Edit), the bookings with no date in the list's Unsorted group, and a word
  (`To check`, `N to check` on a trip's card and by its dates), never a colour alone. The
  page's POSTs (`/items/<id>/looks-right`, `/owner`, `/send-again`) act only on a booking the
  owner can see (else 404) and only while the answer still applies (else `?f=stale`), Looks
  right at the time the page was rendered (its form's `seen`), and send nothing anywhere: the
  next sync pass does. One booking the page cannot present is shown bare (`views._bare`).
- Owner edits live in `payload["owner_edits"]` (field → time of the edit) and any merge path must
  re-apply them: a newer email that prints the field wins, an older one never does (the codes
  the owner set too, on the row as in the payload). The travellers an email names still join,
  but nobody the owner took off on the page (`payload["taken_off"]`), whatever the email's
  date, until the owner adds them again, nor anyone a partial cancellation newer than the email
  took off (`payload["cancelled_names"]`, name → that email's date, written by
  `TripStore.remove_passengers`). A booking added by hand has every typed field stamped
  so at the entry: an email older than it changes nothing typed. An email that adds no
  traveller and changes nothing of it still fills in what it lacks and prints, its code
  included (`fills_in`, `planner._fills_in`: no change recorded, and the merge takes only the
  fields the booking holds nothing for, joining the email's soft stamps as any update does,
  `executor.Executor._said`; `duplicate` once nothing is left to fill), so a later change
  email finds it by that code.
  Every kind is
  edited on the page through `TripStore.edit_item`, which stamps each changed field under the
  name the payload keeps it as (`store.STORED_AS`: a flight's code is `pnr`) and makes every
  save a look at the time the form was shown (an airport typed in where the flight log knew
  none clears that answer and its backoff, so the next pass asks again); the page refuses
  (409) a save from a form whose
  digest of the booking (`views._shown`) is no longer the booking's, and an edit that would
  merge a draft into a booking put away, or a draft holding a flight-log record into another
  booking (`TripStore._refuse_a_merge_the_page_cannot_make`). What else belongs to the
  booking and to no one email survives every merge (`executor.KEPT`: `looked_at`,
  `owner_edits`, `owner_answer`, `taken_off`, `cancelled_names`, `alt_keys`,
  `change_recorded`), `raised` is
  joined stamp by stamp, the later time winning, and `email_date` stays the latest of the
  emails merged in. A corrected confirmation code becomes an
  alias in `payload["alt_keys"]`, and so does the code an email fills in on a stay or a car
  typed in by hand (which keeps its key); every key lookup goes through `TripStore._find_by_key`.
- Every `TripStore` method that changes a booking ends in `settle`: it rebuilds the booking from
  its stored fields (`rebuild.rebuild_item`, the store's airport table), writes back a flight's
  derived times, zones and airport names, gives a flight, a train or a draft the key its fields
  make (the old key becomes an alias, the new one stops being one; a flight or a train whose
  fields make none takes a draft key of its own, `keys.own_draft_key`, keeping its flight-log
  link, so the next email for its leg completes it; a stay or a car keeps its real key),
  stores its open checks and sync state, and places it (no start: no trip; a booking
  already in a trip stays there). When another booking holds that key, a draft is merged into
  it (`TripStore._complete_draft`: the booking keeps its values, place and flight-log link, the
  draft fills what it lacks, never a field the owner edited nor `store.LEFT_WITH_THE_DRAFT`,
  and joins its travellers, codes, owner edits, soft stamps and keys; a "no" the owner gave
  about the leg holds; its log rows are repointed and its row goes; never when each holds a
  different flight-log record) and anything else raises `KeyCollision`. The owner's say goes
  through the store too: `looks_right` (closes every open soft check but `owner_unknown`),
  `owner_answer` (yes adds the owner's name to the travellers
  and the printed names; no sets `payload["owner_answer"]`) and `send_again` (releases
  `sync_hold` `deleted` and a backoff). A sync pass's claim (`claim_sync`, `release_sync`) and
  a dry run's log digest (`sync_logged`) are bookkeeping no settle reads and do not settle.
  Only `trips/store.py` and `ledger/repo.py` write `trips` or `trip_items`
  (`tests/test_store_is_the_only_writer.py`).
- A flight's payload keeps the zone and UTC offset printed beside each time (`dep_zone`,
  `dep_offset`, `arr_zone`, `arr_offset`; absent when not printed, `rules.PRINTED_ZONES`), which
  the rebuild hands back, so a settled flight keeps the instants it was filed with. They travel
  with their end's time through every merge (`rules.carry_printed_zones`) and go when the owner
  changes that end's time or airport.
- `rules.parse_local` reads a day alone as its midnight. A flight's time printed as a day with
  no time (`rules.day_alone`, told from the printed form, so a printed `T00:00` is a time) is
  stored as the day: no instant, no order or length worked out from it, and for the departure
  the hard finding `departure`; the key still takes the day. Code that shows or sends a time
  asks `day_alone` (or the value's length) first. A day alone says nothing about a time held on
  that day: `rules.without_days_alone` drops it from a newer email's payload before every merge
  (the store's, the executor's), the planner's diff and the owner-edit rule. A day alone on
  another day moves that end in the store's merge; the plan counts it as a change only for a
  departure (a flight's `planner.CHANGE_FIELDS` are its day, its instants, its destination
  and its number), so an email whose only news is an arrival's other day alone changes
  nothing. Likewise a value an email prints for a booking the store holds that fails its hard
  check (a city for an airport, a time that is no date; an end before its start, which then
  says nothing of either end) says nothing:
  `rules.without_unreadable` drops it before the store's merge and the planner's diff, and the
  booking keeps what it has. What the owner types is stored as typed, a hard check if wrong.
- Dependency changes: edit `pyproject.toml`, then regenerate `uv.lock` in Docker with the uv image
  the `Dockerfile` pins; the host needs no Python toolchain.
- The corpus (`tests/corpus/`, loaded and checked by `tripbox.corpus`, described in its
  `README.md`) is a collection of invented travel emails, each with what it says
  (`extraction.json`) and what filing it does (`outcome.json`), that extractors are scored against.
- Every time the ledger stores comes from `util.now` (through `now_iso`), never `datetime.now` or
  SQLite's own clock: `util.clock_at` holds it still, which a demo seed does while it files each
  email. `Context.shift` (`pipeline/shift.py`) moves every date an email gives by whole days once
  the extractors have read it; only the seed sets it.
- A corpus email is written from scratch from the corpus's `README.md`, `FAMILY.md`, its rows in
  `SCENARIOS.md`, the wire schema and the model's rules alone, never from a real message, after its
  truth files, which never bend to a run.

## Commands that ship

- `tools/verify/ci.sh` is the gate. It needs Docker and runs every check in it: the workflow
  files and the licence headers (in the `Dockerfile`'s Python image), then it builds the test
  image, audits the packages' licences, runs `ruff check` and `pytest` in it (as a user no account
  names, writing only in `tmp_path`), a new-install smoke, the corpus scorecard, the tools' own
  lint and tests with the repository mounted read-only, the demo site (`demo export` checked by
  `tools/verify/demo_site_check.py`) and the secret scan (gitleaks's image, pulled by digest).
  Its lock is `flock`'s, else perl's. Last line: `CI OK`.
- `tools/verify/narrow.sh tests/test_x.py -k name` runs one pytest selection in the image `ci.sh`
  built (paths from the repository root).
- `tripbox eval --recorded` files the corpus, in order, into an empty ledger under its own
  settings, its answers standing in for the model, and prints a scorecard per email and in
  total; the last line is `eval: X of N emails right` (exit 0 only when all are; `--json FILE`
  saves it; without `--recorded` it asks the model). On the working tree, run it as `narrow.sh`
  does, in the image `ci.sh` built: `-v "$PWD:/repo:ro" -w /repo -e PYTHONPATH=/repo/src`,
  then `python -m tripbox eval --recorded`.
- Covers: the `covers` table of the ledger (schema 2) holds each trip's two JPEG BLOBs, written only
  by `ledger.repo.Covers`; the rules (owner upload beats a found photo, merge, remove, backoff) live
  in `trips/covers.py`. Decoding (`coverimage`, Pillow) and fetching (`wikimedia`) happen only in
  the worker; the web serves bytes and never imports `PIL` or `httpx2`.
- `tripbox work` is the worker (`worker.Worker`): one long-lived process that beats every 5 s.
  A beat writes its status record (JSON in `kv` row `worker`, words and counts: `features`,
  `last`, `mailbox` if mail's login is an address, `waiting` by reason, `dry_run`; the page
  reads it through `tripbox.status` (the ledger and clock) alone; a beat 30 s off is a worker that
  stopped), reads one waiting upload (`worker.take_upload`: the owner's own input; a PDF as an
  email carrying it, `uploads.as_email`, dated when handed in, so every reading is one message;
  a nameless paste that reads as an email with no booking is read again as text; what it came
  to is `uploads.came_to`'s on every reading), runs a sync pass, and starts `poll`
  (at once, then every 5 minutes, killed after 15, past a message's reading; two kills running
  are told once; the resume pass follows new mail) and `enrich` (03:30 in the configured zone
  while enrichment is on, killed after 15 minutes) as processes of their own, never waited for
  in a beat and never two at a time. A part that breaks is recorded as a word
  (`last.<part>.code`), never a message. One worker runs per
  ledger (else `work: another worker is running on this ledger`, exit 2; a record of the same
  pid and host is its own). Before a part that may run long (an upload, a sync pass) the record
  says `busy`, live for `status.BUSY_SECONDS` (900); every record is written
  only while no other process's record is live, and a worker that finds one at a beat stops
  (`work: another worker took over this ledger`, exit 2), writing nothing. It refuses while
  `TRIPBOX_NO_NETWORK` is set, needs no mail, requeues an upload a stopped worker left being
  read (`failed`, `stopped`, once read `uploads.MAX_READINGS` times), with no model makes waiting
  mail wait for one (`run.wait_for_a_model`), and SIGTERM ends it and its children (exit 0) and
  gives the ledger up (`stopped_at`). Compose's `worker` service runs it, given 120 s to stop
  (`stop_grace_period`), past a model's reading, so it is never killed with its record `busy`.
- `tripbox mail backfill --since YYYY-MM-DD [--apply]`: mail is read from the first poll on, and
  from the first poll of another host or login (`imap.KV_MAILBOX`) (a new
  UIDVALIDITY: from the day before the last good poll; over 20 MB: only the header is fetched, for
  the sender check, and a stranger's is rejected quietly). It counts the
  messages since that day up to the cursor; `--apply` reads them as mail, the cursor untouched.
  Mail off, no day, offline: exit 2.
- `tripbox airtrail enroll --since YYYY-MM-DD [--apply]` counts the flights held out as `past`
  since that day that would be queued and with `--apply` queues them for the next sync pass; it
  sends nothing itself. AirTrail must be on (else its off line, exit 2); a `--since` that is not
  a day prints the usage (exit 2).
- `tripbox airtrail forget [--apply]` counts the flights linked to the flight log the ledger
  knew and with `--apply` unlinks them, so another AirTrail or user's key can be kept
  (`src/tripbox/write/AGENTS.md`). AirTrail must be on.
- `tripbox airtrail backfill [--since YYYY-MM-DD] [--apply] [--verbose]` files the owner's
  flights the flight log holds as trips (`trips/backfill.py`); `tripbox airtrail status` says,
  from the ledger alone (offline), where the flight log, its flights and its key stand.
  `tripbox check` asks each feature that is on whether it answers (`ok`, `FAIL`, `warn`,
  `off`, `invalid`), keeps what AirTrail says of the key (or that it refused it), exit 1 on a FAIL.
- `tripbox backup [--with-mail] [--out FILE]`: a 600 tar.gz in `backups/` (manifest, ledger by
  `db.write_copy`, `config/`; mail on request, never where the page reads). `restore FILE
  [--apply]` reads it whole, stops for a held ledger or live worker, copies the ledger beside,
  drops its -wal/-shm, renames, adds missing mail, never writes `config/`.
- `tripbox report [MESSAGE] [--name NAME]... [--out FILE]`: a redacted copy of an email and
  what tripbox did with it, for a parse problem (`report/sample.py`; the rules are
  `redact.py`'s). MESSAGE: a number (ledger and archive read, nothing written), a file or `-`
  (held by SHA-256: from the ledger; else read in a temporary directory, no model),
  none: the ten newest. Bytes handed in (a file, upload, `ingest`, paste) are an email or
  text by one rule, `uploads.email_of`; an email is filed from its first header on. Report to
  stdout or `--out` (new, 600), counts to stderr; no network client is built. What must go is a
  rule with a test, never an exception for one email (`tests/test_report_corpus.py`).
- `tripbox demo seed --out DIR [--today YYYY-MM-DD] [--corpus DIR]` files the corpus's demo
  entries the same way into a new ledger in `DIR/data`, every
  date moved so the corpus's anchor falls on `--today` (default: today in the corpus's zone), so
  one trip is under way, one is to come and four are past; a fifth, its one stay cancelled and
  marked to check, is listed with the past ones, and a flight with no date, to check, waits in
  Unsorted. It prints three `demo:` lines, the last naming the settings to show it with; exit
  0, 1 when an email was not filed, 2 when it refuses (a `DIR` not empty, a `--today` that is no
  day or out of range) or cannot write `DIR`. What it shows, in days from today, is
  `tests/corpus/demo-view.json`, which `tests/test_demo.py` holds it to.
- `tripbox demo export --out DIR [--today YYYY-MM-DD] [--corpus DIR]` writes the demo's page as
  static, read-only files (`web/export.py`; `demo seed`'s refusals, exits): `demo: <p> pages
  and <f> files written to DIR, today <day>`.
