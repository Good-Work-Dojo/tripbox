# Changelog

All notable changes to tripbox are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-11-23

### Added

- Cover photos. With `TRIPBOX_COVERS=wikimedia` the worker finds a freely licensed photo of a
  trip's place on Wikipedia and Wikimedia Commons and the page shows it, with its credit. Off by
  default; see the privacy page.
- Upload your own cover from a phone or a computer, in the trip's "Place and photo" sheet (JPEG,
  PNG or WebP up to 10 MB; HEIC is refused). It is resized and stripped of its metadata, GPS
  position included, and always wins over a found photo.
- **Find photo**: saving a trip's place looks for a photo again. A cover can be removed.

### Changed

- The ledger is schema 2, with a table for covers. The first start upgrades it and keeps a copy
  beside it as `data/ledger-before-v2-<time>.db`.
- Pillow is added, to resize covers in the worker.
- New setting `TRIPBOX_COVERS` (`off` or `wikimedia`).

## [1.0.1] - 2026-11-23

Fixes from a review of the whole of 1.0.0.

### Changed

- At the root of an https address the sign-in cookies are named `__Host-tripbox_session` and
  `__Host-tripbox_device`, so no other site under the same domain can set or shadow them. Such an
  install signs everyone out once on upgrade; a browser that signed in before keeps its own
  allowance of wrong passwords.
- A calendar event's UID is now its booking's row, so an event updates in place when a flight is
  rescheduled or a draft is completed. Every UID changes once with this release: a trip's `.ics`
  imported before it shows its events twice after one new download (a subscribed feed is not
  affected).
- An email the extractor reads only in part waits for the model (over its cap, unreachable, or
  not set up) instead of being filed half-read, and is read whole once the model can.

### Fixed

- Mail: an allowed sender's mail signed twice (a Workspace or Fastmail domain) is accepted on its
  own domain's signature. A forward-as-attachment whose first email was filed before now files
  the others, and an attached email you vouched for stays yours when read again. A stranger's
  message over 20 MB is rejected quietly instead of notifying you. Pointing tripbox at another
  mailbox starts it at now.
- Reading: names match across accents and apostrophes, as tickets print them plain. A subject
  that only mentions cancellation (a "free cancellation" policy) no longer turns a booking into a
  cancellation. Every traveller's seat on a shared train leg is kept. A year outside 1900–2200 is
  a booking to check, not a failed email. A booking's key reads its day as a date, so one booking
  read once with an ISO date and once with a printed one is filed once.
- The model: a request its server refuses costs nothing against the budget, and a retired model
  makes mail wait (with one notice) instead of failing each email. A waiting email whose archived
  file is gone (a restore without mail) fails once instead of logging on every poll.
- The flight log: a companion a partial cancellation took off is not put back, on the page or in
  AirTrail, when an older email for the booking is read later. tripbox never links your flight to
  an AirTrail record you are not on; it adds your own.
- The worker gets two minutes to stop, so a restart during a model's reading no longer locks the
  new worker out for 15 minutes. A `/paste` interrupted by a bot restart is read when Telegram
  delivers it again.
- The page: a form saved after the sign-in ended shows a page that says it was not saved and
  links to the sign-in, instead of the word "unauthorized", and empties the saved offline copies.
  For screen readers, a trip tile's letter is no longer read, each booking's menu and each
  Restore button says what it is for, the booking form's time fields are named, and the share
  page's filter chips say which is selected.
- `tripbox report`: a header sent as raw bytes, or beside an encoded word that does not decode,
  is redacted like the body; a text attachment (a list, a card, a page) is removed whole, as the
  docs said, and a calendar file is still shown redacted; a `mailto:` link's query is replaced.
- `tripbox backup` and `restore` run while a setting they do not read is wrong (a mistyped
  `travellers.yaml`, an unknown home airport).
- The image carries the app's compiled bytecode, so the worker's polls and the health check do
  not compile it each time.
- A patch release on an older line no longer replaces the demo site or GitHub's latest release.
- Docs: what a share link always shows, how long a new browser waits after wrong passwords, when
  flights held by the AirTrail dry run are sent, and Compose's preference for a variable the shell
  exports.

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
