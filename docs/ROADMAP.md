# Roadmap

What tripbox does not do yet, in no promised order. A line here is a wish that has been looked at,
not a commitment. If one matters to you, say so in an issue: that is how it moves up.

## Bigger things

- Sign in with AirTrail, so one login serves both.
- TripIt import.
- PDFs for every provider: today only `anthropic` is sent an attachment.
- A generic webhook as a way to hand tripbox a booking.
- A trust mode for other providers' authentication headers, so a mailbox that is not Gmail's
  can vouch for a sender.
- Restaurant, event and ferry bookings.
- More languages for the page.
- More than one user.
- Outlook mailboxes.
- A published, patched AirTrail image, in place of building the Trips button yourself.
- An `export` command that writes every trip as JSON. The ledger is one SQLite file, and `tripbox
  trips`, the calendar feed and copy-as-text already print trips; a JSON shape is a promise to
  keep.
- A table of models that have read the corpus, made from saved scorecards, once there are any.
  No scorecard from a real model has been recorded.

## Mail and the model

- An importable Gmail filters file; today the recipe is pasted into Gmail's filter form.
- Prices for more than one model of your own: a `config/prices.yaml` beside the shipped table, in
  place of the one `TRIPBOX_LLM_PRICE_USD_PER_MTOK` setting.
- A forward-only sender list for the Gmail recipe.
- A claim on a message's row, so that two readings at once cannot double its log rows or ask the
  model twice.
- `last_poll_ok` stamped only when an inbox was read.
- `tripbox mail backfill --quiet`, with no notice for each message.
- Field-length limits in validation.
- A bound on the bookings one structured-data email may bring.
- `tripbox eval` without `--recorded` counted in the installation's month, so it cannot pass
  the cap unseen.
- A neutral word for the parser that reads a model's answer, which is still stored as `haiku`.
- Image signing, provenance attestations and an SBOM for the release.

## The page

- Share links that end on a date you choose. Today a link works until 7 days after the trip ends,
  and at least 30 days after it was made, or until you revoke it.
- A throttle bucket for each client address behind a trusted proxy.
- A shared-secret header for `TRIPBOX_AUTH=proxy`, so that a request that did not come through
  the proxy cannot be taken for one that did.
- A choice of flight tracker for the links on a flight.
- A list of the saved copies on Settings.
- A pass with a screen reader: the toast's live region, and the letters on a trip's tile.
- A trip that keeps its notes when it is emptied.
- Pictures of the README in the dark scheme.
- PDFs, and the dates, in `tripbox report`: a PDF is never included, and dates are kept because
  a parser reads them.

## Running it

- Memory limits as settings, not fixed values in `compose.yaml`.
- Scheduled backups from the worker.
- A worker that stops at once when told.
- A Traefik recipe that was run, and runs of the Cloudflare Tunnel, Nginx Proxy Manager and
  nginx recipes at one hostname with AirTrail.
- A run of the compose stack on arm64 hardware; CI starts it on GitHub's arm64 runner only.
- Docker Desktop, rootless Docker, Podman and an SELinux host, run and written down.
- A digest for each saved email in a backup, so a damaged email is noticed.
- A given-up upload's body kept and backed up, with a way to download or purge it.

## The flight log

- Two stays under one itinerary number kept as two bookings: today they fold into one.
- Companions read back from the flight log.
- A card on the page for the key, in place of `tripbox check`.
- A hand-corrected record given its own word in enrichment's result.
- `build.sh` accepting a registry with a port, and an overall deadline for the check that the
  button is served.
- A role without the passengers permission stopping the pass at once, in place of marking each
  booking as refused.
- Backfill linking a booking you have put away.
- A flight rebooked in day and number after AirTrail's database was restored, taken back
  without **Send again**.
