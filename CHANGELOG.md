# Changelog

All notable changes to tripbox are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-22

The first release.

### Added

- Forwarded travel confirmation emails, read from a mailbox over IMAP or handed in on the page, become flights,
  hotels, cars, trains and other bookings. KDE's kitinerary extractor reads an email first; a model (Anthropic, or any
  OpenAI-compatible service, one the user chooses) reads what it cannot, and everything either returns is checked
  before it is filed. A message that cannot be read yet waits, and is tried again.
- A ledger in SQLite that files each booking once however many emails confirm it, and groups bookings into trips.
- A phone-first trips page with its own sign-in (a password, a Cloudflare Access login or a login proxy): upcoming
  and past trips, a timeline for each day, notes, merging and moving bookings, share links for a single trip,
  a calendar feed, and copies that work offline.
- Notifications, and a Telegram bot, both optional.
- An optional AirTrail add-on that writes the owner's own flights to an AirTrail flight log, checking each record
  before it writes, and a patch that adds a Trips button to AirTrail's dock.
- One image for three roles (`web`, `worker`, `bot`) that runs as any user, a `compose.yaml`, `tripbox doctor`,
  backup and restore, and recipes for reverse proxies.
- `tripbox report`, a redacted sample of an email and what tripbox did with it, to attach to an issue.
- A corpus of invented emails with known answers, and `tripbox eval` to score a change against it.
