# AGENTS.md — the trips page (`src/tripbox/web/`)

The page's facts; the hard rules are the repository's `AGENTS.md`.

- The page runs in its own container, which receives only `config.KEYS` with `web=True`, its only
  secrets its own login pair, `config.PAGE_SECRETS` (pinned by `tests/test_compose.py`, which also
  refuses an `env_file` on the service and a value interpolating any other name): nothing that
  reaches the mailbox, the model, AirTrail, AeroAPI or a notifier channel, and it loads no
  `tripbox.notify`.
- Its address is `TRIPBOX_BASE_URL`, its path's first segment none of `RESERVED_SEGMENTS`.
  `middleware.BasePath` (outermost) answers a path holding a control character with a bare 404,
  sends the bare base to `<base>/`, takes the base off a path under it and passes any other as it
  is (a stripping proxy), so routes are written from `/`.
  Every link and redirect comes from `urls.Urls` (redirects are paths), none from the request; a
  notice links only while the setting is set (`link_base`). Page tests take the `base` fixture
  (`tests/conftest.py`) and their clients from `tests/helpers/web.py`.
- A route is public only when `app.PUBLIC` names it (a login's own routes too); every other one
  goes in `app.PRIVATE`, inside the private mount (`Mount("")`, last), whose one middleware,
  `LoginRequired`, asks the `auth.Authenticator` (TRIPBOX_AUTH's) and puts the user in
  `scope["state"]["user"]`, else answers its challenge; a path no route takes is challenged,
  then the 404 page. No router redirects a slash. Under `/feed/`: the feed or a bare 404.
- Password sign-in (`login.PasswordLogin`, the default): a session is a random token in the cookie,
  its SHA-256 in `sessions` with `cred` (a fingerprint of the origin and the hash line; at start
  other creds end); good under 30 days unseen and 180 old, `seen_at` rewritten hourly (a refused
  write keeps the session). Cookie names and `Secure`
  follow the base URL alone (`login.cookie_prefix`: `__Host-` at the root of an https origin,
  every value of it read; `__Secure-` under a sub-path, none over http, four values read), path
  `urls.cookie_path`; at the root over https a sign-in drops the older `__Secure-` names (a
  device known under one keeps its bucket), and a sign-out drops every session name. `Throttle`: 10 wrong in 900 s close a bucket,
  a known device's (`devices`) or everyone else's, never an address; admission, scrypt and count
  under one lock, off the loop. Sign-out ends the row; the device cookie outlives it. Its
  challenge: a page load (GET/HEAD accepting text/html) 303s to the login with `next`; another
  method accepting text/html (a form posted after the session ended) gets a 401 page
  (`ended.html`, `clear.js` its one script) linking the login, `next` the Referer's path when
  it is of the origin and `_next` takes it; anything else the plain 401.
- Wrappers, outermost first: `BasePath`, `Headers` (`SECURITY_HEADERS`, `X-Robots-Tag` too, on
  every response, `no-store` but on `/static/`), `BodyLimit` (64 KiB, `app.FORM_LIMITS` for paste and upload),
  `SameOrigin` (every POST: `Sec-Fetch-Site` or an `Origin` equal to `Settings.origin`, never
  Host), the app. uvicorn runs with `proxy_headers=False`: no forwarded header is believed.
- Proxy sign-in (`auth.ProxyLogin`): the user header is believed only from a socket peer
  (`auth.peer_address`: `scope["client"]` as an address, IPv4-mapped read as IPv4, none
  untrusted) in `TRIPBOX_TRUSTED_PROXIES` (never a /0), exactly once, naming one of
  `TRIPBOX_PROXY_USERS` (case aside); else 401 and one INFO word. No login: a local base URL
  and a loopback `TRIPBOX_LISTEN` only, and `auth.LocalOnly`, outside `BasePath`, answers 421
  to a Host but localhost, 127.0.0.1 or [::1] (any port), public routes too. Tests' requests
  come from `helpers.web.PEER`.
- The page takes a booking to read (`/add`: pasted text, an `.eml` or a PDF) and only stores it,
  in the ledger's `uploads` waiting list, for the worker to read: `tripbox.uploads` (no pipeline
  import; `pipeline.run` takes `looks_like_email` from it) tells the kind from the bytes alone,
  never the name; the upload route counts the body as it arrives and answers 413 past 10 MB
  before the form is parsed; the same bytes are one upload, unless it ended with nothing filed
  (`uploads.DEAD_ENDS`: it is then taken again under its token); at most 20 wait. A status page
  is named by the row's random token, never its id, and shows the cleaned name and the state,
  never the body.
- A booking typed in by hand (`/add/{flight,hotel,car,train}`: the edit form, with a Trip field
  whose first choice leaves it to the dates) goes straight to `TripStore.add_by_hand`: no model,
  no message row, `payload["source"]` `hand`, its real key or a draft key of its own; a booking
  already answering to that real key refuses it, and so does a form with nothing filled in but
  its travellers.
- The policy (`SECURITY_HEADERS`) admits the page's own files alone: no other site, no inline
  script or style (no `style=`: a colour is a class, such as a trip's tile `tile-{id % 8}`), no
  framing. The font ships in `static/fonts/` (four `@font-face` weights); `app._asset_version`
  hashes every static file. Text colours keep 4.5:1 on their backgrounds (`test_web_style.py`).
- A page names only what the install has: the confirmations' `views.KEPT` names the flight log
  only while one is kept, and the list links it only when `TRIPBOX_AIRTRAIL_PUBLIC_URL` is set.
  A stay's or a car's second line is `item-N-end`. A control repeated per row is described by
  its row's title (`aria-describedby`: `item-N[-end]-title`, `trip-N-title`); a tile's letter
  is `aria-hidden`. The photograph harness's browser checks are
  `tools/verify/screens/checks.py`.
- A ledger with no trip, no booking and nothing deleted is a first run: the list says `No trips
  yet`, what to do and `Add a booking`, then the status card (`card.status_card`, `_status.html`),
  made from the worker's record alone (`tripbox.status`: codes and counts, never an error's text;
  a code that is no word shows as `error`); a row that needs a look has the class `status-look`.
  Past a first run, while no worker is live the list starts by saying so. The record's `mailbox`
  shows only on private pages, never on a public one nor in a log. The card's labels are the one
  exception to `test_no_page_names_what_the_install_does_not_have`: a row names its feature to
  say it is off.
- Settings (`settings_page`, private; its posts redirect through `views._go`): the status card,
  the display choices, the feed's address (`/settings/feed` makes a new one; the old is a bare
  404), shared links, signing out per login (`/settings/sessions`: password only, ends every
  session, keeps devices), the about line (`_about.html`, also ending the login page; source
  `TRIPBOX_SOURCE_URL`). Display choices are the ledger's `kv` row `display`, never a setting:
  `views._ctx` enters `format.styled(display.read(...))`, as do the notices, the bot and the
  CLI; at the default every text is as before (notices take the page's words only off the
  default clock and date order). Maps links: `display.maps_url`.
- Share links (`trips/shares.py`, `shared.py`): `share_links` keeps the token beside its SHA-256
  and finds a link by the hash alone; it works until 7 days after the trip ends, at least 30
  after it was made, never revoked or while its trip is deleted; a trip that goes for good takes
  its links (`ON DELETE CASCADE`; the store is not asked). `/s/` is a public mount owning its
  prefix: the page `shared_view` builds by its own allow-list (never `views.present`); every miss
  is one bare 404 whatever the token, and the 403 (cross-site POST), 413 and 405 (outside
  `shared.METHODS`) depend on no token. Under it `security_headers` gives
  `SHARED_HEADERS` (`script-src 'none'`, `no-referrer`, `X-Robots-Tag`). It writes nothing and
  logs nothing of a token.
- `export.py` (`tripbox demo export`) writes the page as static files: the real app over a copy
  of a seeded ledger, asked in-process under `EXPORT_BASE` with the clock held, then each page
  rewritten with `html.parser` (relative or switched-off links, disabled controls, no script,
  a content policy, the banner, the about line); no template or static file changes for it, and
  `tools/verify/demo_site_check.py` holds the written tree to those rules.
- Saved copies (`TRIPBOX_OFFLINE`; unset: on with `password` or `none`): `/manifest.webmanifest`,
  `/sw.js` are `PUBLIC`; `sw.js` (`sw_off.js` while off: it deletes the stores and unregisters)
  is `no-cache`, registered by `app.js` from `<base>/sw.js`, scope `<base>/`. It keeps only what
  carries `X-Tripbox-Offline: 1` (`views._keepable`: on, 200, signed in, the list or a trip),
  never by path, and never answers `/login`, `/logout`, `/s/`, `/feed/`. `app.js` asks for the
  list or a trip in view again (a GET of the path) once a worker controls it and once `?f=`
  leaves the address: the load that registers it, and an address with a query, are not kept.
  The network's answer is released at once (the store's write beside it); only a page with a
  saved copy falls back to it after 3 s. At install it keeps `views.worker`'s `files` (the
  versioned stylesheet and script, the fonts): the registering load never passes through it.
  Stores are named
  `tripbox<base>:` + `static:`/`pages:` + `app.state.version`, so a release replaces them. The
  pages store goes on a 401, 403, redirect or unmarked 200 of a kept page, before a sign-out is
  sent (`app.js`) and on the login page (`clear.js`); `Clear-Site-Data` is not relied on to empty
  it. The worker answers a non-GET only when its referrer is under `<origin><base>/`, and
  empties the pages store when the answer is a 401 or 403.
