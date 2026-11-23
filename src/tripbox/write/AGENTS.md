# AGENTS.md — the flight log (`src/tripbox/write/`)

The flight log's facts; the hard rules are the repository's `AGENTS.md`.

- The executor (`write/executor.py`) applies a plan through the store only and never asks the
  flight log. `pipeline.run.sync` runs one `Syncer` pass after each message (and its attached
  emails) and after a button in Telegram; its error is logged and never fails the message.
  Each `Outcome` carries the booking's sync state and checks after that pass.
  `TRIPBOX_AIRTRAIL_DRY_RUN` and `--dry-run` mean only "send nothing to the flight log": bookings are
  filed and flights wait (`kv` row `airtrail_dry_run` tells the page; a run made dry by the
  flag alone leaves it as configured).
- Whether AirTrail is on is the ledger's `kv` row
  `airtrail` (none counts as off), which `pipeline.run.build_context` notes through
  `write.lifecycle.note_airtrail`: the same state writes and settles nothing; a change is one
  transaction that reads the row again under the write lock (of two processes, the second finds
  it the same). Switched on, every flight filed while no flight log was kept (at or after `kv`
  row `airtrail_off_at`, written with the flag when it goes off; any, while it never was), never
  sent (no flight id, nothing told) and whose local day is before the configured zone's today
  is held out (`sync_hold` `past`), then every flight is settled, so the upcoming ones the owner
  is on are `pending` by themselves; switched off, every flight is settled and reads `off`,
  keeping its flight id and what was told, so a change made meanwhile is sent when it is on
  again. A flight filed while a flight log is kept is never held: one still waiting (dry run, a
  check, an outage) keeps its place across an off and on. A booking whose key is now another's
  (`KeyCollision`) does not stop a switch or an enrolment: it is logged and left `off`
  (`TripStore._settled_or_left`), and one gone by its turn (another took it in) is passed
  over. Each switch is one INFO line in the log file, never on the
  terminal (`log.FILE_ONLY`). A past flight goes back only through `tripbox airtrail enroll`
  (`TripStore.release_past`: held `past`, dated on or after the day, visible, not cancelled,
  and not one the owner is off).
- `write/sync.py`'s `Syncer` reconciles the flight log: for a flight that is due
  (`TripStore.due_for_sync`) it sends only what `checks.desired` holds that differs from `told`,
  looks a flight with no id up before creating it (day, number's digits less leading zeros,
  origin; a crash after a create adopts it next time; a record not listing the key's user,
  `airtrail.not_listed`, is never adopted),
  and unlinks one deleted there (`sync_hold` `deleted`). A flight held out of the reconcile
  (`sync_hold`) stays out when a later email updates its booking. In a dry run a flight's
  request is logged once and a pass takes it again only when what it would be sent for changes
  (`sync_logged` holds that digest), so the flight log is not asked about the same flights pass
  after pass.
- One ledger keeps one flight log (`write/lifecycle.py`): its flight ids are one AirTrail user's
  flights. Before the first flight of a pass (and of a due flight synced alone) the `Syncer`
  reads `client.profile()` (`/api/v1/me`, asked once per client) and `bind`s the ledger to that
  user (`kv` row `airtrail_user`; `airtrail_key` keeps what `airtrail.key_report` finds missing,
  denied and more). Another user's profile ends the pass as `other` (`PassReport.stopped`):
  nothing claimed or sent, one alert naming `tripbox airtrail forget --apply`. A profile that
  cannot be read stops the pass or is an outage, by the table below; no row is touched, and no
  flight is asked about in a pass that did not begin so (`Syncer._bound`).
  `build_context` notes the address (`note_address`, `kv` row `airtrail_address`: a digest of
  scheme, host, port and path); at another one `ref_cache` is emptied. `tripbox airtrail forget
  [--apply]` (`TripStore.forget_flight_log`) unlinks every flight that holds an id or anything
  told, holds the past ones `past` (they go through `enroll`), keeps any other hold, and empties
  `airtrail_user`, `airtrail_key`, `airtrail_key_problem`, the outage rows
  (`airtrail_down_since`, `airtrail_alerted`, `airtrail_key_rejected`) and `ref_cache`.
  `tripbox check` fails a key of another user than `airtrail_user` and keeps nothing of it;
  else it stamps `airtrail_key` on every look (a pass, only on a change); a check the flight
  log refuses (401/403) stores `{"at", "refused"}` there (`lifecycle.note_refused`) until a
  check is answered.
- What the flight log answers (`AirTrailError.code`, one of `airtrail.ERROR_CODES`), by the first
  rule that holds:
  1. 401, or a 403 with no reason read: the key is refused; `stop`, `airtrail_key_rejected`.
  2. 403 `insufficient_scope` naming an `.any` scope, or 404, on the read or write of one flight,
     or a record read whose passengers the key's user is not among (`Syncer._off_it`: a key with
     `.any` scopes is refused nothing): it is no longer the owner's there; `detach` (`sync_hold`
     `deleted`), the pass goes on. A 404 counts only while the profile, read afresh, lets the
     role read own flights (else rule 4: defensive, as v3.14.0 answers such a role 403
     `forbidden`), and a write's 404 only when the flight read again is gone too (else rule 5).
  3. 403 `insufficient_scope` otherwise (`airtrail.missing_scope`): `stop`, `scope:<name>`.
  4. 403 `forbidden`: the role refuses; `stop`, `role`.
  5. Any other 4xx but 429 on a create or update: `refused`, a check on the booking.
  6. Anything else: an outage; backoff, and the pass ends.
  Why a pass stopped is one word in `kv` row `airtrail_key_problem` (`unauthorized`,
  `scope:<name>`, `role`, `other_log`); the owner is alerted when the word changes.
  `unauthorized` is over with any answer that does not stop, a scope or the role when a pass gets
  through (`Syncer._went_through`), `other_log` when the profile is the ledger's again.
- A record is the booking's flight only while at least two of its day, its number's digits and
  its origin's code agree with `told` (with nothing told: with `checks.desired`); a part either
  side lacks agrees with nothing, but a number or an origin neither side has is stood in for by
  the destination's code, then the departure's instant (the record's scheduled one when it has
  no other). Otherwise it is another flight (`sync.another_flight`): `detach`, nothing sent.
  What passes unseen: the same number from the same origin on another day (a weekly commuter
  flight), and numberless flights on one route on any day.
- Nothing is written for a flight once the owner is off it. While `told` still names the owner
  (`checks.still_listed`) the soft check `airtrail_still_lists` is open, in the reconcile, from
  the stamp settle puts in `raised` when that begins until the owner looks; the stamp goes when
  the owner is on the flight again. `airtrail_airport` and `airtrail_refused` are shut while the
  owner is off. No request body holds `track` or `customFields` (`airtrail.NEVER_SENT`).
- A flight whose payload `source` is
  `airtrail-backfill` (the backfill files it, a relink carries the flight log's schedule into
  it) is settled without the rebuild — times, day and key stay as the flight log gave them —
  until an email is merged into it (the merge drops that `source`) or the owner edits one of
  its flight fields (`store.FLIGHT_FIELDS` in `owner_edits`). The backfill and a relink record
  it as told what the flight log holds (its own record), so it reads `synced`; once settle
  rekeys it, the key it leaves stays an alias, the key the backfill gives it again on every run.
- The backfill (`trips/backfill.py`, `tripbox airtrail backfill`) reads the profile first:
  refused for a ledger bound to another user, bound on `--apply`; a record not listing the user
  is never filed or linked; a linked id whose record is another flight is relinked or unlinked.
  Each end's city, zone and name come from the bundled table by code (AirTrail's for a code it
  lacks). It and enrichment read the flights AirTrail holds from the trip items
  (`TripStore.sent_flights`), never `items`; `enrichment.item_id` and `aeroapi_calls.item_id`
  are trip item ids.
- Enrichment (`TRIPBOX_ENRICH=aeroapi`) writes only through `Syncer.fill`: claimed as a pass
  claims, bound as a pass begins, the record read and checked as `_update` checks it (else
  `detach`), written only while day, number and origin all agree with `told` (`sync.as_told`;
  two of three: `nothing`, the link left), and only `FILL_FIELDS` put where it holds nothing.
  `enrich_recent` asks `Syncer.bound` before paying for a lookup and never reads or writes a
  record itself. An outage either finds notes nothing: the outage clock is the sync's.
