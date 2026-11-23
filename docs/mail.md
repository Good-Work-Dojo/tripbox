# Mail

tripbox reads one mailbox over IMAP. You forward confirmation emails to it (or have a filter do
it), and each one becomes bookings on the trips page. Mail is optional: you can paste a booking,
upload an `.eml` file or a PDF, or run `tripbox ingest`, with no mailbox at all.

The settings are in [configuration](configuration.md#mail). This page says what happens to the
mail and what it trusts.

## The two ways it believes a message

Anyone can send mail to a mailbox, so tripbox reads a message only when it comes from, or was
forwarded by, an address in `TRIPBOX_MAIL_ALLOWED_SENDERS` (yours, and anyone whose own
bookings you want in your trips). The addresses are matched exactly. What else is checked is the
trust mode, `TRIPBOX_MAIL_TRUST`.

**`gmail`** (the default) is for a Gmail mailbox, which is the one that can vouch for a message:

- A message from an allowed sender is read only when Gmail's own `Authentication-Results`
  header, the topmost one, from `mx.google.com`, says `dkim=pass` for the sender's own domain
  and `dmarc=pass` for it. Mail signed twice (by its own domain and by the service that sends
  it) carries a `dkim=` result for each signature; one passing for the sender's own domain is
  enough. A `dmarc=` or `spf=` result stated twice rejects the message.
- A message forwarded by a Gmail filter carries a return address of the form
  `you+caf_=…@your-domain`; it is read only when the same header says `spf=pass` for exactly that
  address. Gmail adds `X-Forwarded-For` too, which on its own proves nothing and only names the
  path.
- A message that fails these is rejected without being read, and, with `TRIPBOX_MAIL_FILING=labels`,
  filed under `tripbox/rejected`.
- It is a problem, and mail stays off, when `TRIPBOX_MAIL_HOST` is not Gmail's: another
  server's headers say nothing a Gmail check can use.

Who is vouched for, once a message has passed: whatever an allowed sender sent or forwarded by
hand is. What a Gmail filter forwarded is vouched for only when its original sender's domain is on
the list of airline and booking-site domains (`TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS`, replaced
whole when you set it; the shipped list is part of the package) and passes that domain's own
check. The list governs filter forwards alone.

A flight that nobody is vouched for is filed in its trip, and the mark **To check** (the
`unverified_sender` check) is put on it only while AirTrail is kept and you are on the flight:
the mark guards what is sent to the flight log, and nothing else. It is then not sent until you
say it looks right. With no flight log, or on a companion's flight, nothing is marked.

**`mailbox`** is for any other mailbox that offers IMAP over TLS on port 993 with a user name and
a password or app password: there is no setting for another port or for STARTTLS, and no OAuth
sign-in, so Outlook and Microsoft 365 mailboxes, and a local bridge on another port, do not work.
Nothing is authenticated: the address is the secret.
Whatever reaches the mailbox from, or forwarded through, an allowed sender is read. Nobody is
vouched for: while AirTrail is kept, each flight of yours that arrives by mail is marked **To
check** and waits for your look before it can go to the flight log; with no flight log nothing is
marked, so a forged booking shows as an ordinary one. Anyone who learns the mailbox's address and
an allowed sender's address can put a booking in your trips: keep the address private.

In both modes what you hand in yourself (a paste, an upload, `tripbox ingest`) is your own: it
needs no sender check and nothing about it waits for a look. A message once accepted is not
checked again.

## What mail is read

- A new install reads from the moment it first looks. The first poll logs
  `starting at now; older mail is read only by tripbox mail backfill`.
- `tripbox mail backfill --since YYYY-MM-DD` counts the messages that arrived since that day and
  `--apply` reads them as mail (the same sender check, the same model cap, a notice per message).
- When the server renumbers its messages (a new `UIDVALIDITY`), tripbox reads again what arrived
  since the day before its last good poll, and knows what it has read by its content. This
  assumes the server numbers messages in the order they arrive.
- A message over 20 MB is never fetched. You are told once.
- Messages are fetched without marking them read, and never deleted.
- Bounds on one message: header text of 16 KiB (a longer address, authentication or id header
  reads as absent), at most 20 emails attached to it, and at most 20 bookings from one model
  answer before all of them are marked **To check**.

The worker checks the mailbox when it starts and then every five minutes. A message is read
again only when you ask (`tripbox replay`), or while it is waiting for a model. Stop the worker
before `tripbox poll`, `tripbox mail backfill --apply`, `tripbox replay` or `tripbox ingest`: two
readers of one message can double its log rows or ask the model twice.

## With no model

Without a model, only an email that carries structured data is read; any other waits (see
[models](models.md)). That data survives a Gmail filter's forward, an attached original and an
uploaded `.eml` of the original, and a forward made by hand drops it. So with no model, let a
filter do the forwarding, or attach the original email to what you send, or upload its `.eml` on
the page; a booking you forward by hand will wait until a model is set up.

## Filing

`TRIPBOX_MAIL_FILING` says whether tripbox files the messages it handled in the mailbox. With
`labels` (the default with `gmail`) each message is copied once into `tripbox/processed`,
`tripbox/rejected` or `tripbox/failed`: labels in Gmail, folders elsewhere, named with the
server's own separator. A message waiting for the model is left where it is. With `none` (the
default with `mailbox`) the mailbox is untouched.

On a server whose folders must sit under `INBOX.`, `labels` fails each poll. Use `none`.

## Setting up Gmail

[The Gmail recipe](gmail-filters.md) is generated from the sender list: a filter or two that
forward airline and booking mail to the mailbox. The list is mostly North American airlines and
online agencies, with a few European and Gulf airlines. It has no hotel chain, car rental,
railway or home-rental sender, so add a filter, or widen one, for the domains your own
confirmations come from; the list only decides which flights skip your look (see above). Make an
app password for the mailbox's own Google account (2-Step Verification must be on there) and use
it as `TRIPBOX_MAIL_PASSWORD` (spaces in it are removed).

## When nothing arrives

The page's status card and `tripbox doctor` say what is wrong without printing a secret: a
setting missing, a login refused, a mailbox not read yet, a worker that stopped. The worker
records only a word for a failure, never the server's message.

## What has not been tried

The Gmail path has been exercised in tests against recorded headers. Any mailbox that is not
Gmail's, and `labels` outside Gmail, have never been tried against a real server. Nothing here
was run against a live mailbox.
