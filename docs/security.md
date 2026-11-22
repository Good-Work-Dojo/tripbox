# Security

tripbox holds where you will be and when, and the names on your bookings. This page says what
protects it, what you must do yourself, and what nobody has tried. To report a hole, see
[SECURITY.md](../SECURITY.md).

## Who can open the page

`TRIPBOX_AUTH` chooses one of four ways.

**`password`** (the default). One shared password, in `.env` as `TRIPBOX_PASSWORD` (10
characters or more) or as the line `tripbox passwd` prints, `TRIPBOX_PASSWORD_HASH`, so that the
password itself is in no file. The password is never chosen in the browser: a page just put on the
internet would let whoever came first choose it.

- A sign-in is a random token in a cookie; the ledger keeps only its SHA-256. A session ends 30
  days after it was last seen, and 180 days after it was made whether seen or not. Changing the
  password, or changing the page's address from http to https, ends every session. Signing out
  ends that browser's. Settings can end every session.
- Ten wrong passwords within 15 minutes close the sign-in to everyone but a browser that has
  signed in before (it carries a second cookie), so a stranger's guesses never lock you out; a
  new browser may have to wait up to 15 minutes. The count is kept for those two groups, never
  for an address.
- The cookie's name and `Secure` flag follow `TRIPBOX_BASE_URL`: over https it is a `__Secure-`
  cookie.

**`proxy`**: a login proxy in front of the page (Authelia, Authentik, oauth2-proxy and the like)
signs people in and names the user in a header. The header is believed only when the request's
connecting address is in `TRIPBOX_TRUSTED_PROXIES` (a network that holds every address is
refused), the header appears exactly once, and it names a user in `TRIPBOX_PROXY_USERS`. Any
other request is a 401. The proxy must let `<base>/s/`, `<base>/feed/` and `<base>/static/`
through without its login.

**`cloudflare`**: a request must carry a valid Cloudflare Access token for your team and
application, from an address in `TRIPBOX_ALLOWED_EMAILS`. The page fetches Cloudflare's public
signing keys, the one request it makes. The same three paths must bypass Access.

**`none`**: no login, for this computer only. It is accepted only while `TRIPBOX_BASE_URL` is on
`localhost`, `127.0.0.1` or `[::1]` and `TRIPBOX_LISTEN` is a loopback address, and the page then
answers 421 to a request addressed to any other name, so another site cannot reach it through a
name of its own that points at your computer.

Some paths answer without a login in `password`, `proxy` and `cloudflare` modes, because a
person or program that cannot sign in needs them: `<base>/s/` (share links), `<base>/feed/` (the
calendar feed), `<base>/static/` (the page's files), the health check, the page's manifest and its
service worker, and the sign-in's own routes. A share link and the feed are reached only by an
address that holds a token nobody can guess. In `none` mode every route, these too, answers only a
request addressed to this computer.

## Docker, the network and no-login mode

By default the compose file publishes the page on `127.0.0.1:8110`, this computer alone.

- Plain http on every interface (`TRIPBOX_LISTEN=0.0.0.0:…`) suits a home network only. A
  server on the internet needs a proxy that serves https, so the password and the cookie are not
  sent in the open.
- Docker's published ports bypass a host firewall such as `ufw`: a port published on `0.0.0.0` is
  open whatever `ufw` says. Publish on `127.0.0.1` and put the proxy in front, or on the same Docker
  network.
- **Docker Engine older than 28.0.0 and no-login mode.** Docker Engine's 28.0.0 release notes,
  under "Networking › Port publishing in bridge networks", say: "Fix a security issue that was
  allowing neighbor hosts to connect to ports mapped on a loopback address." (the linked pull
  request is https://github.com/moby/moby/pull/49325). So on an engine older than 28.0.0, a machine
  on the same network segment could reach a port published on `127.0.0.1`. On such an engine,
  `TRIPBOX_AUTH=none` is not safe even on loopback: use a login mode, or upgrade Docker.
- With a login proxy, list the address the proxy connects from as tripbox sees it, never Docker's
  own gateway address or its network.

## What the page does

- The page runs in its own container, with a read-only root, no Linux capabilities and no new
  privileges, as an ordinary user (1000 unless you change it).
- That container is given only the page's own settings and its login pair. It holds no mailbox,
  model, AirTrail, flight-data or notification credential, no settings file, and no folder of
  raw emails or backups. A hole in the page therefore does not hand over your mailbox. Only the
  page is restricted this way: the worker and the bot are each given the whole `.env`, the
  mailbox password and the model and AirTrail keys with it, and neither one listens for a
  browser.
- Every response carries a content policy that admits the page's own files alone: no other
  site, no inline script or style, no framing; also `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin` and `X-Robots-Tag: noindex, nofollow`. A shared trip's page has
  `script-src 'none'` and `no-referrer`. The page makes no request to another origin.
- Every form post must come from the page's own origin (`Sec-Fetch-Site`, or an `Origin` equal to
  the one `TRIPBOX_BASE_URL` gives; never the `Host`). Uvicorn runs believing no forwarded header.
- A request body is read up to 64 KiB; a paste or an upload up to 10 MB, counted as it arrives.
  At most 20 uploads wait.
- A page deletion is soft: a deleted trip or booking is kept in Recently deleted for 30 days, and
  then erased. The saved email in `archive/` stays.

## Mail

The defences are structural; they do not depend on a model behaving.

- Only mail from, or forwarded by, an allowed sender is read, and in `gmail` mode only when
  Gmail's own authentication results bind it to that sender. In `mailbox` mode nothing is
  authenticated: the mailbox's address is the secret. [Mail](mail.md) has the rules.
- A model is asked for an answer in a fixed shape, and the answer is validated: what fails is
  marked **To check**, never trusted.
- A flight reaches a flight log only when you are on it, and a flight of yours from mail nobody
  vouched for is held for your look first. Nothing is ever deleted in AirTrail.
- **What vouched mail says is that sender's word.** Instruction-like text in mail from an allowed
  sender, or in what you hand in, that a model obeys can add a bogus flight to your trips. A
  model answer that prints a held booking's confirmation code is filed like any email for it: it
  can cancel that booking, take travellers off it, or move it. For your flights, the next sync
  then updates the fields it changed in AirTrail. It never deletes a flight there; a companion taken
  off a booking is removed from the flight's passengers. A test pins
  the case of a new flight that names no held booking.
- Bounds on one message: 20 MB, header text of 16 KiB, 20 attached emails, 20 bookings from one
  model answer before all are marked, 5 cards and 12 lines in one notice, subjects shown at 200
  characters.
- A saved email with no row in the ledger (a file of `archive/` replayed into another ledger) is
  judged by its own headers, as if it had just arrived.

## Share links and saved copies

A share link shows what you ticked (names, addresses, seats and times by default; codes and notes
when ticked) and the trip's title, to anyone who has the link. It works until 7 days after the
trip ends, and at least 30 days after it was made, and it does not work while its trip is deleted.
You can end one at once: **Revoke** on the trip's share page or in Settings. Because a trip's end
can move (a changed booking, a merge), a lapsed link can work again, unless you revoked it. A
login proxy's own log sees a share link's address. In `none` mode the share page still says
"Anyone with the link", though anyone who can reach the page sees every trip anyway.

A saved copy (the offline feature) is the list and each trip opened, on the device that opened
them. It works only over https or on localhost. At the root of a hostname tripbox takes the whole
origin for its service worker and its cookies: give tripbox a hostname of its own or a sub-path.

## What has not been tried

- The four login modes are tested; a login proxy and Cloudflare Access have not been run against
  a real Authelia, Authentik or Cloudflare account.
- A real phone has not been used: the offline copies and the home-screen icon were tested in a
  browser only. The steps to try them are in [install](install.md).
- TLS certificates were not exercised: the proxy recipes were run over plain http.
- Docker Desktop (macOS and Windows), rootless Docker, Podman and an SELinux host have not been
  run.
- The model's behaviour on hostile text has been tested against hand-written answers, never a
  real model.
