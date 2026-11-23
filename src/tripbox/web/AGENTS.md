# AGENTS.md — the trips page (`src/tripbox/web/`)

The page's facts; the hard rules are the repository's `AGENTS.md`.

- The page runs in its own container with only `config.KEYS` with `web=True`, its only secrets
  its login pair, `config.PAGE_SECRETS` (`tests/test_compose.py` pins it: no `env_file`, no value interpolating another name): nothing that reaches the
  mailbox, the model, AirTrail, AeroAPI or a notifier channel; it loads no `tripbox.notify`.
- Its address is `TRIPBOX_BASE_URL`, its path's first segment none of `RESERVED_SEGMENTS`.
  `middleware.BasePath` answers a path with a control character by a bare 404, sends the bare
  base to `<base>/`, strips the base off a path under it and passes any other as is (a stripping
  proxy): routes are written from `/`. Every link and redirect (a path) comes from `urls.Urls`, never the request; a notice links only while the setting is set (`link_base`).
  Page tests use the `base` fixture and `tests/helpers/web.py` clients (from `PEER`).
- A route is public only when `app.PUBLIC` names it (a login's own routes too); every other is
  in `app.PRIVATE`, in the private mount (`Mount("")`, last), whose one middleware,
  `LoginRequired`, asks the `auth.Authenticator` (TRIPBOX_AUTH's) and puts the user in
  `scope["state"]["user"]`, else answers its challenge; a path no route takes is challenged,
  then the 404 page. No router redirects a slash. Under `/feed/`: the feed or a bare 404.
- Password sign-in (`login.PasswordLogin`, the default): a session is a random token in the cookie,
  its SHA-256 in `sessions` with `cred` (a fingerprint of the origin and the hash line; at start
  other creds end); good under 30 days unseen and 180 old, `seen_at` rewritten hourly (a refused
  write keeps the session). Cookie names (`__Host-` at an https root) and `Secure` follow the base
  URL alone, path `urls.cookie_path`. `Throttle`: 10 wrong in 900 s close a bucket, a known
  device's (`devices`) or everyone else's, never an address; admission, scrypt and count in one lock, off the loop. Sign-out ends the row; the device cookie outlives it. Signed out, a form
  gets `ended.html`.
- Wrappers, outermost first: `BasePath`, `Headers` (`SECURITY_HEADERS` on every response,
  `no-store` but on `/static/` and a cover's 200 (`COVER_KEPT`)), `BodyLimit` (64 KiB,
  `app.FORM_LIMITS` for the paste and the uploads), `SameOrigin` (every POST: `Sec-Fetch-Site` or
  an `Origin` equal to `Settings.origin`, never Host), the app. uvicorn runs with
  `proxy_headers=False`: no forwarded header is believed.
- Proxy sign-in (`auth.ProxyLogin`): the user header is believed only from a socket peer
  (`auth.peer_address`: `scope["client"]` as an address, IPv4-mapped read as IPv4, none
  untrusted) in `TRIPBOX_TRUSTED_PROXIES` (never a /0), exactly once, naming one of
  `TRIPBOX_PROXY_USERS` (case aside); else 401 and one INFO word. No login: a local base URL and
  a loopback `TRIPBOX_LISTEN` only, and `auth.LocalOnly`, outside `BasePath`, answers 421 to a
  Host but localhost, 127.0.0.1 or [::1] (any port), public routes too.
- The page takes a booking to read (`/add`: pasted text, an `.eml` or a PDF) and only stores it
  in the ledger's `uploads` waiting list for the worker: `tripbox.uploads` (no pipeline import;
  `pipeline.run` takes `looks_like_email` from it) tells the kind by the bytes alone; an upload route counts the body as it arrives and refuses it past 10 MB before the form
  is parsed; the same bytes are one upload, unless it ended with nothing filed
  (`uploads.DEAD_ENDS`: then taken again under its token); at most 20 wait. A status page,
  named by the row's random token (never its id), shows its cleaned name and state, no body.
- Covers: `/covers/upload?trip=<id>` (trip checked first; body limit `COVER_LIMIT`) keeps a
  JPEG, PNG or WebP by its bytes (`coversniff`) for the worker; `/covers/<sha256>[-s].jpg`: a
  ready cover of a trip not deleted, never on a shared page or Recently deleted; a credit links
  only http(s).
- A booking typed in by hand (`/add/{flight,hotel,car,train}`: the edit form, its Trip field's
  first choice leaving it to the dates) goes straight to `TripStore.add_by_hand`: no model, no
  message row, `payload["source"]` `hand`, its real key or a draft key of its own; a booking
  answering to that real key refuses it, as does a form with nothing but its travellers.
- The policy (`SECURITY_HEADERS`) admits the page's own files alone: no other site, no inline
  script or style (no `style=`: a colour is a class, as a tile's `tile-{id % 8}`), no
  framing. The font is in `static/fonts/` (four `@font-face` weights); `app._asset_version`
  hashes every static file. Text colours keep 4.5:1 (`test_web_style.py`).
- A page names only what the install has: `views.KEPT` names the flight log only while one is
  kept, the list links it only with `TRIPBOX_AIRTRAIL_PUBLIC_URL` set, a photo lookup (and
  Wikipedia) only with `TRIPBOX_COVERS=wikimedia`. A stay's or car's second line is `item-N-end`. Browser checks: `tools/verify/screens/checks.py`.
- No trip, booking or deleted trip is a first run: the list says `No trips
  yet`, what to do and `Add a booking`, then the status card (`card.status_card`, `_status.html`)
  from the worker's record alone (`tripbox.status`: codes and counts, never an error's text; a
  code that is no word shows as `error`); a row needing a look has the class `status-look`.
  Past a first run, while no worker is live the list starts by saying so. The record's `mailbox`
  shows on private pages alone, never in a log. The card's labels are the one
  exception to `test_no_page_names_what_the_install_does_not_have` (a row names its feature as off).
- Settings (`settings_page`, private; posts redirect by `views._go`): the status card,
  the display choices, the feed's address (`/settings/feed` makes a new one, the old a bare
  404), shared links, signing out per login (`/settings/sessions`: password only, ends every
  session, keeps devices), the about line (`_about.html`, also ending the login page; source
  `TRIPBOX_SOURCE_URL`). Display choices are the `kv` row `display`, never a setting:
  `views._ctx` enters `format.styled(display.read(...))`, as do the notices, the bot and the CLI;
  at the default every text is as before (notices take the page's words only off the default
  clock and date order). Maps links: `display.maps_url`.
- Share links (`trips/shares.py`, `shared.py`): `share_links` keeps the token and its SHA-256,
  a link found by the hash alone; it works until 7 days after the trip ends, at least 30
  after it was made, unless revoked or while its trip is deleted; a trip gone for good takes its
  links (`ON DELETE CASCADE`, not the store). `/s/` is a public mount owning its prefix:
  `shared_view` builds the page by its own allow-list (never `views.present`); every miss is one
  bare 404 whatever the token, and the 403 (cross-site POST), 413 and 405 (outside
  `shared.METHODS`) depend on no token. Under it `security_headers` gives `SHARED_HEADERS`
  (`script-src 'none'`, `no-referrer`). It writes nothing and logs nothing of a token.
- `export.py` (`tripbox demo export`) writes the page as static files: the real app over a seeded
  ledger's copy (its covers dropped: tiles only), asked in-process under `EXPORT_BASE`, clock held, each page rewritten by
  `html.parser` (relative or switched-off links, disabled controls, no script, a content policy,
  the banner, the about line); no template or static file changes for it; `tools/verify/demo_site_check.py` holds the tree to those rules.
- Saved copies (`TRIPBOX_OFFLINE`; unset: on with `password` or `none`): `/manifest.webmanifest`
  and `/sw.js` are `PUBLIC`; `sw.js` (`sw_off.js` while off: deletes the stores,
  unregisters) is `no-cache`, registered by `app.js` from `<base>/sw.js`, scope `<base>/`. It
  keeps only what carries `X-Tripbox-Offline: 1` (`views._keepable`: on, 200, signed in, the list
  or a trip), never by path, and never answers `/login`, `/logout`, `/s/`, `/feed/`. `app.js`
  asks again for the list or trip in view (a GET of the path) once a worker controls it and once
  `?f=` leaves the address (the registering load and an address with a query are not kept). The
  network's answer is released at once, the store written beside it; a saved copy answers only after 3 s. At install it keeps `views.worker`'s `files` (versioned
  stylesheet and script, fonts): the registering load never passes through it. Stores are
  `tripbox<base>:` + `static:`/`pages:` + `app.state.version`, so a release replaces them. The
  pages store goes on a 401, 403, redirect or unmarked 200 of a kept page, before a sign-out is
  sent (`app.js`) and on the login page (`clear.js`), not relying on `Clear-Site-Data`. A
  cover (`/covers/<sha256>[-s].jpg`, no query) is cache-first from the pages store (a 200
  `image/jpeg` only, 120 kept, emptied with the pages). A non-GET is answered only with a
  referrer under `<origin><base>/`.
