# Configuration

tripbox is configured with environment variables. With Docker Compose they come from a `.env`
file next to `compose.yaml` (copy `.env.example` and edit it); a literal `$` in a value is written
`$$` there. Every setting has a default or is optional until the feature that uses it is wanted.

## Features

A feature is on when its settings are present. Nothing else has to be switched on: set the
mailbox settings and tripbox reads a mailbox, set the AirTrail pair and flights are also logged
in AirTrail. A feature with only some of its settings set is half configured: tripbox leaves it
off and says what is missing. A setting with a bad value is reported as a problem.

Run `tripbox doctor` to see where an installation stands; with Docker Compose, run it in the worker,
which mounts every folder: `docker compose run --rm worker tripbox doctor`. Every other
`tripbox` command in these guides runs the same way, in place of `doctor`; a command that is given
a file (`tripbox ingest`, `tripbox report`) takes it on standard input with `-`, as in
`docker compose run --rm -T worker tripbox ingest - < booking.eml`, because the container sees
only the install's folders. `doctor` prints one row per
feature (on, off or invalid, and what to set next) and one for the travellers file, the lists, the
folders and the worker, then every problem found, and exits 1 when there is one. It reads the
settings and the travellers file and checks each folder as the process will use it: `data`,
`archive`, `logs` and `backups` must take a file (it makes one empty file in each and removes it at
once), `config` must be readable, and the ledger's own files must be readable and writable. A folder
that is absent is never made by the check; it is no problem when it can be made on first use, and
`config` and `backups` never are. Each problem names the folder as `docker compose` mounts it
(`./data`) and the fix. A folder that will not take the file is told apart by why: on a read-only
mount (its line in `compose.yaml` ends `:ro`), on a disk with no room left (or a quota used up), or
not writable by this user, whose fix is the `chown` to run, or the `TRIPBOX_UID` and `TRIPBOX_GID`
to set. A file in `config` this user cannot read is named with who owns it, and the `chmod` that
makes it readable. The
`files` row says who the process runs as (`uid 1000, gid 1000`) and which folders are there. The
`worker` row reads the worker's record without writing to the ledger: `running` (its last beat,
`busy` while it reads something long, how the last mail check ended), `stopped`, `none` (no worker
has run yet, or no ledger yet: neither is a problem) or `unknown` (the ledger could not be read). It
opens no mailbox and makes no network call. It never prints the value of a secret setting.

`tripbox web` checks `data` and `config` when it starts, and `tripbox work` and `tripbox bot`
check `data`, `archive` and `config`: a problem stops it as a settings problem does
(`config error: …`), in the words `doctor` uses.

What a problem stops depends on its feature. A problem in the core settings stops every command
except `doctor`, `backup` and `restore` (which only a folder's setting stops, so a backup can be
taken while the rest is being fixed). A problem in the web settings stops `tripbox web`. A problem
in any other feature leaves that feature off, and each problem is logged once at start. A feature
that is merely unconfigured never stops a command. A bad value for the trusted sender domains trusts
no forwarded sender.

## Core

| Setting | Default | What it does |
|---|---|---|
| `TZ` | `UTC` | The time zone tripbox works in, an IANA name such as `America/Chicago`. |
| `TRIPBOX_HOME_AIRPORTS` | empty | Comma-separated three-letter airport codes you live near, such as `MSP,RST`; each must be an airport in the bundled table. A trip is titled by its first destination that is not one of them. |

## Mail

On when `TRIPBOX_MAIL_HOST`, `TRIPBOX_MAIL_USER`, `TRIPBOX_MAIL_PASSWORD` and
`TRIPBOX_MAIL_ALLOWED_SENDERS` are all set. Without them `tripbox ingest` and `tripbox replay`
still read emails you give them.

A mailbox is read from the moment tripbox first reads it. Older mail is read only when you ask:
`tripbox mail backfill --since YYYY-MM-DD` counts the messages that arrived since that day, and
with `--apply` reads them as mail (the same sender check, the same model cap, filed as set
below). Should the server number its messages anew, tripbox reads again what arrived since the
day before its last good poll, and knows what it has read already by its content. A message over
20 MB is never fetched, only its header, for the sender check: of one from an allowed sender you
are told once, and with labels it is filed under `tripbox/failed`; anyone else's is rejected
quietly, as other mail is.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_MAIL_HOST` | empty | The IMAP server's name alone, such as `imap.example.com`. tripbox connects over TLS on port 993 and logs in with a user name and a password or app password: there is no setting for another port or for STARTTLS, and no OAuth sign-in, so a mailbox that takes only OAuth (Outlook and Microsoft 365 mailboxes) or sits behind a local bridge on another port cannot be read. |
| `TRIPBOX_MAIL_USER` | empty | The mailbox's login, such as `trips@example.com`. When it is an email address, the page shows it as the address to forward confirmations to. |
| `TRIPBOX_MAIL_PASSWORD` | empty | The mailbox's password or app password (secret). Spaces are removed. |
| `TRIPBOX_MAIL_ALLOWED_SENDERS` | empty | Comma-separated addresses whose mail is read, such as `alex@example.com`. Mail that is not from or via one of them is rejected unparsed. |
| `TRIPBOX_MAIL_TRUST` | `gmail` | How far a forwarded message is believed. `gmail`: only when Google's own authentication results vouch for it; it needs Gmail's own mailbox (`imap.gmail.com`), and with any other `TRIPBOX_MAIL_HOST` it is a problem that leaves mail off. `mailbox`, for a mailbox that is not Gmail: no such check; whatever reaches the mailbox from or via an allowed sender is read, but no sender is vouched for, your own address included: with AirTrail on, each flight of yours that arrives by mail is filed, marked to check, and is not sent to AirTrail until you have looked at it and said it looks right. What you hand in yourself (an upload, `tripbox ingest`, a paste) is yours in either mode. |
| `TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS` | the shipped list of airlines and booking sites | Comma-separated domains, used with `gmail` only. With AirTrail on, a flight you are on that is forwarded from one of these (and passes its domain check) is logged in AirTrail without a human look; one forwarded from a sender outside the list is filed in its trip, marked to check, and is not sent to AirTrail until you have looked at it and said it looks right. With AirTrail off, such a flight is filed in its trip like any other. A value you set replaces the shipped list rather than adding to it. A value that is not a list of domains trusts no forwarded sender. |
| `TRIPBOX_MAIL_FILING` | `labels` with `gmail`, `none` with `mailbox` | Whether mail tripbox has handled is filed in the mailbox. `labels`: each message is copied into `tripbox/processed`, `tripbox/rejected` or `tripbox/failed` (labels in Gmail; folders named with the server's own separator elsewhere), once; one that waits for the model is left where it is. `none`: the mailbox is left untouched. Mail is never deleted or marked read either way. |

## The model

On when `TRIPBOX_LLM_API_KEY` is set, unless `TRIPBOX_LLM_PROVIDER` is `none`, which keeps it off
whatever else is set. With `openai` it is on when `TRIPBOX_LLM_MODEL` is set and a key, an address
(`TRIPBOX_LLM_BASE_URL`) or both are; less is a problem. Without it only emails whose
structured data the extractor reads whole are read. Any other email, one it reads only in part
included, waits: it is kept, its mailbox copy gets no label and
nothing is sent about it, and `tripbox ingest` and `tripbox replay` print `waiting.` and why, such
as `waiting. No model is set up, so it waits until one is.`, for it. The first poll once a model
is set up reads it.

The model is asked under a cap: a model whose price is known (tripbox ships prices for the
default model, or give one) stops once this month's calls cost the monthly budget, and any other
once today's calls reach the number a day allows (months and days in UTC). While the cap is
reached, or the model cannot be reached, mail that needs it waits too, and you are told once a
month or day for the cap and once an outage; the first poll after the cap allows or the model
answers reads what waited, oldest first. An answer the model refuses or cuts short fails its
email instead; `tripbox replay --reread --status failed` asks again.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_LLM_PROVIDER` | `anthropic` | Who reads the emails without structured data: `anthropic` (Anthropic's models, with `TRIPBOX_LLM_API_KEY`); `openai`, any server that speaks OpenAI's Chat Completions (OpenAI's own service, or one you run such as Ollama), which is sent the email's text and never its attachments; or `none`, which turns the model off whatever else is set. |
| `TRIPBOX_LLM_API_KEY` | empty | The key for the model that reads emails without structured data (secret). A server of your own may need none. |
| `TRIPBOX_LLM_MODEL` | `claude-haiku-5-5` | The model's name, with no spaces. With `openai` it must be set to a model your server serves: the default is Anthropic's. |
| `TRIPBOX_LLM_BASE_URL` | empty | The address of an OpenAI-compatible server as the worker reaches it — a model server in another container on the same Docker network by its service name, or this machine's address on your network; not localhost — such as `http://models.example:11434/v1`; unset, `openai` means OpenAI's own service. Only `openai` takes it. It may carry a login (secret): tripbox never shows it and masks it in its logs. |
| `TRIPBOX_LLM_MONTHLY_BUDGET_USD` | `5.00` | The most a model whose price is known may cost in a month (UTC), in US dollars. Once this month's calls cost that much, mail that needs the model waits until the budget allows. `0`: the model is never asked. |
| `TRIPBOX_LLM_MAX_CALLS_PER_DAY` | `100` | How many calls a model with no known price may be asked in a day (UTC); mail beyond that waits until the next day. |
| `TRIPBOX_LLM_PRICE_USD_PER_MTOK` | empty | The model's price as `INPUT,OUTPUT`, US dollars per million tokens, such as `0.10,0.50`. Unset, the price is the one tripbox ships for the model (`src/tripbox/lists/prices.yaml`), and a model it lacks has no known price. |

## AirTrail

On when `TRIPBOX_AIRTRAIL_URL` and `TRIPBOX_AIRTRAIL_API_KEY` are both set and the travellers
file names an owner (see "Files in the config directory"). Without them flights go to trips only.
Only flights you are on are written to AirTrail, and nothing is ever deleted there. With both
settings set and no owner named, AirTrail is invalid and stays off.

When you switch AirTrail on with flights already in your trips, each upcoming flight you are on
is queued by itself and goes with tripbox's next sync once `TRIPBOX_AIRTRAIL_DRY_RUN` is `false`
(one AirTrail already holds is linked to it, never copied), while a flight filed while AirTrail
was off that had already left waits until `tripbox airtrail enroll --since YYYY-MM-DD` counts
the ones since that day and the same command with `--apply` queues them for that sync. Switched
off, nothing more is sent and your trips keep every booking, each flight still linked to its
copy in AirTrail. Switched on again, a flight whose booking changed meanwhile is updated there,
and one that was still waiting to be sent keeps its place whatever its day.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_AIRTRAIL_URL` | empty | The address AirTrail answers at, as the worker reaches it, such as `http://flightlog.example:3000`: AirTrail in another container by its service name (which works only when both stacks share a Docker network that you join them to, in a `compose.override.yaml`, shown in [AirTrail](airtrail.md#switching-it-on)), or this computer's address on your network; not `localhost`, which is the worker's own container. |
| `TRIPBOX_AIRTRAIL_API_KEY` | empty | An API key made in AirTrail under its security settings (secret). |
| `TRIPBOX_AIRTRAIL_PUBLIC_URL` | empty | The address you open AirTrail at in a browser, such as `https://flightlog.example`. While AirTrail is on, the trips list links to it ("Flight log"); unset, the list shows no such link. It is only a link: on its own it neither turns AirTrail on nor leaves it half configured. A value that is not an `http://` or `https://` address with a host is a problem of the link alone: `tripbox doctor` lists it and the list shows no link, but AirTrail stays as the other settings make it. |
| `TRIPBOX_AIRTRAIL_DRY_RUN` | `true` | While `true`, nothing is written to AirTrail: every booking is still filed in its trip, a flight that would go to AirTrail waits, and what would be sent is logged once. Set `false` to send flights: the ones that waited go with the next message tripbox handles. On its own it neither turns AirTrail on nor leaves it half configured, and with AirTrail off it changes nothing. |

## Notifications

On when `TRIPBOX_TELEGRAM_BOT_TOKEN` and `TRIPBOX_TELEGRAM_CHAT_ID` are both set, when
`TRIPBOX_NTFY_URL` is, or both. tripbox then sends a message to each of them for each booking it
files or cannot read; `tripbox bot` also answers commands in the Telegram chat. One Telegram
setting without the other, or `TRIPBOX_NTFY_TOKEN` without the address, is half configured.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_TELEGRAM_BOT_TOKEN` | empty | Your Telegram bot's token (secret). |
| `TRIPBOX_TELEGRAM_CHAT_ID` | empty | The chat that gets the messages and may command the bot. |
| `TRIPBOX_NTFY_URL` | empty | Your ntfy topic's whole address, such as `https://ntfy.example/trips` (secret: whoever knows it can read the topic). |
| `TRIPBOX_NTFY_TOKEN` | empty | An access token for the topic, for a server that asks for one (secret). It needs `TRIPBOX_NTFY_URL`. |

To set up Telegram: in Telegram, talk to the bot named BotFather, send it `/newbot` and follow its
questions; it gives you the token. Send your new bot any message, then open
`https://api.telegram.org/bot<token>/getUpdates` in a browser (with your token in place of
`<token>`, and while `tripbox bot` is not running, as only one program can ask Telegram for
updates): the answer holds `"chat":{"id":…}`, and that number is the chat id. Put both in `.env`.
Notices need only these two settings. To answer commands as well, add `COMPOSE_PROFILES=telegram`
to `.env` and run `docker compose up -d`. The bot answers only the chat you named, to `/status`
(the last poll, counts, bookings to check and spend), `/check` (the bookings marked to check, each
with its buttons), `/paste` (then send itinerary text, or `/paste` followed by the text),
`/upcoming` (upcoming trips, ready to paste) and `/share` (a list of trips; `/share 10` or
`/share paris` gives one trip as text, past trips too). For ntfy, `TRIPBOX_NTFY_URL` is the whole
address of a topic on a server you choose; there is nothing else to set unless the server wants a
token.

## Enrichment

On when `TRIPBOX_ENRICH=aeroapi` and `TRIPBOX_AEROAPI_KEY` are both set, and only with AirTrail
on: once a night it fills in details of flights that have landed in AirTrail (see
[the flight log](airtrail.md#enrichment)). A key with no `TRIPBOX_ENRICH` leaves it off, and
`tripbox doctor` says so.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_ENRICH` | empty | Fills in landed flights in AirTrail: `aeroapi`, or empty for none. Any other word is a problem that leaves enrichment off. |
| `TRIPBOX_AEROAPI_KEY` | empty | The flight-data service key (secret). It needs `TRIPBOX_ENRICH=aeroapi`. |
| `TRIPBOX_AEROAPI_MONTHLY_BUDGET_USD` | `1.00` | The most to spend in a calendar month; lookups stop when it is reached. A number of 0 or more. |
| `TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN` | `10` | The most lookups in one run. A whole number of 1 or more. |

## Web

The trips page, served by `tripbox web`. These settings decide who may open it.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_AUTH` | `password` | `password`: the page's own sign-in, with one shared password (the two settings below, one of them). `cloudflare`: a request must carry a valid Cloudflare Access token, which needs the three settings after them, except at `<base>/s/`, `<base>/feed/` and `<base>/static/`, which share links, calendar apps and the page's files use: the Access application must let those three through without its login (a Bypass policy on those paths). `proxy`: a login proxy in front of the page (Authelia, Authentik, oauth2-proxy and the like) signs people in and names the user in a header, which needs `TRIPBOX_TRUSTED_PROXIES` and `TRIPBOX_PROXY_USERS`; the proxy must let `<base>/s/`, `<base>/feed/` and `<base>/static/` through without its own login, as share links, calendar apps and the page's files use them. `none`: no login, for this computer only: accepted only while `TRIPBOX_BASE_URL` is on `localhost`, `127.0.0.1` or `[::1]`, and then a request addressed to any other name is refused (421), so another site cannot reach the page through a name of its own that points at this computer. The page's port must then be reachable from this computer alone: a `TRIPBOX_LISTEN` that is not a loopback address is refused. Nothing else serves the page without a login. |
| `TRIPBOX_PASSWORD` | empty | The page's password, 10 characters or more (secret). A value written as the instructions' placeholder, starting with `<` and ending with `>`, is refused as the example, whatever its length. It is never chosen in the browser: a page just put on the internet would let whoever came first choose it. Signing in lasts 180 days, and 30 days without a visit end it; changing the password ends every sign-in. Ten wrong passwords in 15 minutes close the sign-in for 15 minutes to everyone but a browser that has signed in before, so a stranger's guesses never lock out a browser that has signed in before; a new browser may have to wait up to 15 minutes. |
| `TRIPBOX_PASSWORD_HASH` | empty | Instead of `TRIPBOX_PASSWORD`, never with it: the whole line `tripbox passwd` prints after you type the password twice (`scrypt:` and five more parts, with no `$`), so the password itself is in no file. Secret too. |
| `TRIPBOX_ACCESS_TEAM` | empty | The Zero Trust team name, such as `sample-family` (the part before `.cloudflareaccess.com`). |
| `TRIPBOX_ACCESS_AUD` | empty | The audience tag of the Access application that protects the page. |
| `TRIPBOX_ALLOWED_EMAILS` | empty | Comma-separated addresses that may open the page, such as `alex@example.com`. |
| `TRIPBOX_TRUSTED_PROXIES` | empty | With `TRIPBOX_AUTH=proxy`: the addresses or networks the login proxy connects from, comma-separated, such as `192.0.2.10` or `192.0.2.0/24` (IPv6 too, such as `2001:db8::/32`). List the address your proxy connects from, as tripbox sees it. When tripbox's port is published by Docker, every outside client may appear to come from Docker's own gateway address, so never list that address or its network: put the proxy on the same Docker network as tripbox and list its own address there. The shipped `compose.yaml` publishes tripbox's port on this computer alone by default (`TRIPBOX_LISTEN`), which is harmless: a request through it comes from that gateway address and is never signed in. A request from any other address is never signed in, whatever header it carries. A network that holds every address, such as `0.0.0.0/0`, is refused. |
| `TRIPBOX_PROXY_USER_HEADER` | `Remote-User` | The header the proxy names the signed-in user in: letters, digits and `-`. The proxy must set it itself on every request it passes on, replacing any a browser sent, on the paths it lets through without its login too. A request that carries it twice is refused. |
| `TRIPBOX_PROXY_USERS` | empty | With `TRIPBOX_AUTH=proxy`: the user names the proxy may sign in, comma-separated, such as `alex`; case does not matter. Anyone else the proxy names is refused. |
| `TRIPBOX_BASE_URL` | `http://localhost:8110` | The address people open the page at: `http` or `https`, a host, an optional port and an optional path, such as `https://trips.example.com` or `https://example.com/tripbox`. A port that is the scheme's own (443 for `https`, 80 for `http`) is dropped, as a browser drops it. Every link and redirect of the page is built from it, never from the request (the page believes no `X-Forwarded-*` or `Forwarded` header), and its origin is the one a form post must come from: a post from anywhere else is refused with a sentence naming this address. The page answers under its path, and also when a proxy in front of it takes the path off. The path's first part may not be one of the page's own names, such as `trips` or `static`. Messages link to the page only while it is set. |
| `TRIPBOX_LISTEN` | `127.0.0.1:8110` | Where the page listens: an address and a port, such as `127.0.0.1:8110` for this computer only or `0.0.0.0:8110` for every network it is on; an IPv6 address is written in brackets, such as `[::1]:8110`. A name such as `localhost`, or a port alone, is refused. With Docker Compose it is where the page's port is published, while inside its container the page listens on every interface, on port 8110. With `TRIPBOX_AUTH=none` it must be a loopback address (`127.0.0.1`, `[::1]`): any other is a problem that stops `tripbox web`. `tripbox web --host` and `--port` override it; with `TRIPBOX_AUTH=none`, `--host` must be this computer outside the image. |
| `TRIPBOX_SOURCE_URL` | `https://github.com/Good-Work-Dojo/tripbox` | The address of the page's source code, an `http://` or `https://` address with a host. The about line at the foot of Settings and of the login page links it ("Source code"): the GNU Affero General Public License asks that the people who use the page over a network can find its source. If you change the code you run, point this at your own copy. A value that is not such an address is a problem of the link alone: `tripbox doctor` lists it and the link goes to the default address, but the page is served as the other settings make it. |
| `TRIPBOX_OFFLINE` | `true` with `TRIPBOX_AUTH=password` or `none`, else `false` | Saved copies for use with no network: the page keeps the trip list and each trip you open on the device you open them on, so they open without a connection, under a line saying they are a saved copy and with nothing that can be changed. Nothing else is kept: never the login page, Settings, a trip's share page or a shared trip. Browsers allow this only over `https` or on `localhost`. A device that stays offline keeps its copies for up to 30 days, and a new version of tripbox replaces them. Signing out removes them, which is why it is off behind a login proxy or Cloudflare (the page cannot see their sign-out); set `true` there only if every device that opens the page is your own. `true` or `false` (also `1`/`0`, `yes`/`no`, `on`/`off`); switched off, a device that kept copies removes them the next time it opens the page. |

## Docker Compose

The compose file reads these from `.env` as well; no tripbox process reads them, and none of them
is reported as an unknown setting.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_IMAGE` | `ghcr.io/good-work-dojo/tripbox:1` | The image the services run. |
| `TRIPBOX_UID` | `1000` | The user id the services run as: the owner of the install's folders. |
| `TRIPBOX_GID` | `1000` | The group id the services run as. |

`COMPOSE_PROFILES=telegram` in `.env` runs the Telegram bot as well.

Each service mounts some of the folders beside `compose.yaml`: `web` (the page) mounts `data`,
`logs` and `config` (read-only); `worker` mounts those and `archive` and `backups`; `bot` those and
`archive`. The compose file names each folder's place itself, so a folder setting in `.env` (see
below) changes nothing. The page is given its own settings by name, never the whole `.env`, so it
holds no mailbox, model, AirTrail or notification secret.

## Running without Docker

The image sets where tripbox keeps its files, and the compose file mounts the install's folders
there. Set these only when you run tripbox without Docker; each is an absolute path, and a value
that is not one is a core problem.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_DATA_DIR` | `/app/data` | Where the ledger (a SQLite file) lives. |
| `TRIPBOX_ARCHIVE_DIR` | `/app/archive` | Where every raw email is kept. |
| `TRIPBOX_LOGS_DIR` | `/app/logs` | Where the log files are written. |
| `TRIPBOX_CONFIG_DIR` | `/app/config` | Where the files described under "Files in the config directory" are read from. |
| `TRIPBOX_BACKUPS_DIR` | `/app/backups` | Where `tripbox backup` writes its archives, each a new file of mode 600 holding the ledger and the config folder, and with `--with-mail` every saved email (personal data: keep the folder and its copies private). Only the worker mounts it; `tripbox restore FILE` reads one back. |
| `TRIPBOX_IN_IMAGE` | unset | `1` in the shipped image: set by the image; do not set it yourself. It tells `tripbox web` that its socket is the container's, which Docker Compose publishes at `TRIPBOX_LISTEN`, so with `TRIPBOX_AUTH=none` its `--host` may be every interface. |

## Development switches

These are for testing and replaying emails offline. They are not for real installs.

| Setting | Default | What it does |
|---|---|---|
| `TRIPBOX_NO_NETWORK` | unset | `1`: nothing leaves the process; commands and lookups that need the network refuse. `0` or unset: no effect. Any other value is a core problem. |
| `TRIPBOX_REPLAY_DIR` | unset | A directory of recorded model answers that are used instead of calling the model. |
| `TRIPBOX_RECORD_DIR` | unset | A directory where the model's answers are recorded as they are made. |

## Files in the config directory

`TRIPBOX_CONFIG_DIR` is the directory tripbox reads its configuration files from. Each service
reads them once, when it starts: after you add or change one, run `docker compose restart`, as
`docker compose up -d` recreates nothing for a change in a folder. (`tripbox doctor` runs as a new
process, so it shows the change before the services do.)

### `travellers.yaml` (optional)

Who travels: the owner, whose flights may be written to AirTrail, and the people the owner
usually travels with. tripbox reads only a file named exactly `travellers.yaml` in the config
directory. The repository ships `config/travellers.example.yaml`, which is never read: copy it to
`config/travellers.yaml` and put your own travellers in it.

```yaml
owner:
  names: ["Alex Sample", "Alex Quinn Sample", "Alex Q Sample"]
companions:
  - name: "Sam Sample"
    names: ["Sam Sample"]
  - name: "Robin Sample"
    names: ["Robin Sample"]
  - name: "Jamie Sample"
    names: ["Jamie Sample"]
```

- `owner.names` lists the ways the owner's name is printed on a ticket. The first one is the
  spelling tripbox records. `owner` is optional.
- Each companion has a `name`, the spelling tripbox (and AirTrail, as a guest) records, and
  `names`, the ways it is printed on a ticket. `names` defaults to `[name]`.
- A printed name matches when its first and last names match one of the listed names: case and
  titles (`MR`, `DR`, …) are ignored, `SAMPLE/ALEX QUINN` is read as `Alex Quinn Sample`, and a
  middle name is optional. An airline that glues the middle name or its initial onto the first
  name (`SAMPLE/ALEXQ`) still matches a listed name that has that middle name.
- Every name needs a first and a last name, and a name belongs to one person only.
- Two travellers who share a first and a last name cannot both be listed: a middle name does not
  tell them apart, so tripbox could take one's flights for the other's.

Without the file, or with no `owner` in it, nobody is the owner: bookings are still filed into
trips, and no flight reaches AirTrail. An empty file, or one that holds only comments, names
nobody and is no problem. `tripbox doctor` shows the travellers row as:

- no file: `none` and `travellers.yaml is absent: nobody is the owner, so no flight reaches
  AirTrail`;
- a file with no owner: `on` and `owner: 0 names, companions: M`;
- a file with an owner: `on` and `owner: N names, companions: M`;
- a file with a mistake in it: `invalid` and `see the problems below`.

Each of these mistakes is a core problem: it stops every command but `doctor` (which lists it),
`backup` and `restore`, and until it is fixed nobody is the owner.

- The file cannot be read: it is not a regular file of at most 64 KiB (a link whose target is
  missing, a directory, a pipe, a larger file), or tripbox may not read it.
- It is not valid YAML, nests too deeply to read, or gives one key twice in the same place.
- Its top level is not a mapping, or holds a key other than `owner` and `companions`.
- `owner` holds a key other than `names`, or its `names` is missing, empty or not a list of
  names.
- `companions` is not a list; a companion has no `name`, holds a key other than `name` and
  `names`, or has `names` that is not a list of names.
- A name has only one word.
- Two people (the owner and a companion, or two companions) share a name, or have names that
  read as one person because they have the same first and last name.

### `airlines.yaml` (optional)

tripbox ships a list of airlines: the name, an optional short name used in trip text and the words
an email uses for the airline (`match`), which turn a printed name such as "Delta" into the code
`DL` when the email prints no code. `airlines.yaml` in the config directory adds airlines and
replaces shipped ones. It is a list of entries:

```yaml
- iata: QX
  name: Example Air
  short: Example
  match: [example air]
- iata: DL
  name: Delta
```

- `iata` is two letters or digits and is required; `name` is required; `short` and `match` are
  optional. An entry whose `iata` is already shipped replaces that airline, but leaves its `short`
  and `match` as they were when it does not give its own.
- A blank `short`, or none, inherits the shipped airline's `short`; a missing `match` inherits its
  `match` words, and `match: []` clears them.
- Two entries with one code in the file: the last wins.
- When two airlines share a match word of the same length, the shipped airline wins; a longer word
  always wins over a shorter one (so `air france` is tried before `air`).
- Write a code that YAML reads as true or false in quotes, such as `iata: "NO"` or `iata: "ON"`;
  unquoted, the entry is a core problem that says so.
- Any other key, a top level that is not a list, a file that is not valid YAML, or one that is not
  a regular file of at most 64 KiB is a core problem, keyed `airlines.yaml`: it stops every command
  but `doctor` (which lists it), `backup` and `restore`.

### `us_states.txt` (optional)

The address parser never takes a US state name for a city. The shipped list leaves out New York
and Washington, which are cities first. `us_states.txt` adds names, one per line; blank lines and
lines starting with `#` are ignored, and case does not matter. A file that is not a regular file
of at most 64 KiB, or not UTF-8 text, is a core problem keyed `us_states.txt`.

### Trusted sender domains

The shipped list of travel sender domains whose filter-forwarded mail may write to the flight log
without a human look is part of the package. `TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS` replaces it
whole; there is no file to edit.

The Gmail filters that forward these senders are in [gmail-filters.md](gmail-filters.md).

`tripbox doctor` shows a `lists` row: `shipped`, or `shipped + ` the override files that are
present, or `invalid` when one of them has a mistake.
