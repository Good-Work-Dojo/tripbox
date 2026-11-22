# Security policy

tripbox reads a person's travel confirmation emails and keeps their trips, so a security problem in it matters. Thank
you for reporting one privately.

## Supported versions

Security fixes are made in the newest release only. An install moves to it by pulling the new image.

| Version | Supported |
|---|---|
| The newest release | Yes |
| Any earlier release | No: upgrade first, then check whether the problem remains |

## How to report a problem

Use the repository's **private vulnerability reporting**: the "Report a vulnerability" button under the repository's
Security tab. Only the maintainers can read the report. Never report a security problem in a public issue, a pull
request or a discussion, and never include a real email, a password, a login proxy's token, a share link or a model
or flight-log key in a report: show the problem with invented values.

Say what an attacker can do, from where (the network, a browser, a message sent to the mailbox), and the steps that
show it. The maintainers answer within two weeks, agree a date for disclosure with you, and credit you in the
advisory unless you would rather not be named.

## What is in scope

- **The page's login**: reaching any page, calendar feed, upload or setting without signing in; getting around the
  password's throttle; a session that survives sign-out or a changed password; a request from another site that the
  page acts on; trusting a login proxy's or Cloudflare Access's headers from a peer that is not that proxy.
- **Share links**: seeing more than the owner chose to show, guessing or enumerating a link, a link that survives its
  revocation, or a shared page that runs a script or leaks the link to another site.
- **The sender check**: a message from a sender that is not allowed being filed, or one allowed sender's mail being
  filed under another traveller.
- **The redactor** (`tripbox report`): a sample that still holds a value it is meant to hide (a booking code, a
  name, an address, a ticket number).
- **The capability split**: a way for the page's container to read the mailbox password, a model key or a flight-log
  key, or to write what only the worker writes.

## What is out of scope

- A household's own exposure choices: publishing the page to the internet without HTTPS, a weak password, sharing
  the folders or backups, running tripbox on a machine others control.
- Problems in a mail provider, a model provider, AirTrail or kitinerary themselves, and the base image's operating
  system packages (report those to their projects).
- Findings that need an attacker who already controls the machine or its Docker.
