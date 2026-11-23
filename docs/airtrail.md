# The flight log (AirTrail)

In the order someone who keeps a flight log meets it. tripbox works without one;
everything here applies once `TRIPBOX_AIRTRAIL_URL` and `TRIPBOX_AIRTRAIL_API_KEY` are set
(see [configuration](configuration.md)).

## Switching it on

1. Make the API key in AirTrail (next section).
2. Copy `config/travellers.example.yaml` to `config/travellers.yaml` and put your names in it:
   without an owner there, AirTrail stays off
   ([configuration](configuration.md#travellersyaml-optional)).
3. In `.env` set `TRIPBOX_AIRTRAIL_URL`, the address the worker reaches AirTrail at (not
   `localhost`, which is the worker's own container: AirTrail's service name works only when
   both stacks share a Docker network that you join in a `compose.override.yaml`, otherwise use
   this computer's address on your network), and `TRIPBOX_AIRTRAIL_API_KEY`. Leave
   `TRIPBOX_AIRTRAIL_DRY_RUN` as it is, `true`.
   To join the two stacks' networks (not run), put this in `compose.override.yaml` beside
   `compose.yaml`, with the name of AirTrail's network (`docker network ls` lists it; it is
   usually its project's name and `_default`) in place of `airtrail_default`, and write the URL
   with AirTrail's service name and its port, as `http://airtrail:3000`. The bot needs the same
   network to send a flight after a tap in Telegram:

   ```yaml
   services:
     worker:
       networks: [default, airtrail]
     bot:
       networks: [default, airtrail]
   networks:
     airtrail:
       external: true
       name: airtrail_default
   ```
4. `docker compose up -d`, then `docker compose restart`: the services read the travellers file
   once, when they start, and the restart makes a file you added after they started count.
5. `docker compose run --rm worker tripbox check`, then the same with `doctor`: AirTrail should
   answer, and its row should say `on`.
6. Let a booking arrive and look in the logs for the lines that hold `dry run: would` (after a
   time and a level; where, see [what is sent](#what-is-sent)). When they are right, set
   `TRIPBOX_AIRTRAIL_DRY_RUN=false` and run `docker compose up -d`.

## The key

tripbox reaches AirTrail with one API key, as one AirTrail user: you. The key is made in
AirTrail under **Settings → Security → API Keys → Create**; AirTrail shows it once.

AirTrail's scope picker starts at "Read only", which is not enough. tripbox needs these six
scopes:

| Scope | What tripbox does with it |
|---|---|
| `profile.read` | learns whose key it is, before anything is sent |
| `reference_data.read` | looks up AirTrail's own ids for airports and airlines |
| `flight.read.own` | looks for a flight before adding it, and reads a flight before changing it |
| `flight.create.own` | adds a flight |
| `flight.update.own` | changes a flight it added or found |
| `flight.passengers.manage.own` | names a companion on a flight |

- AirTrail asks for `flight.passengers.manage.own` only when a flight has a second passenger, or
  when its passengers change: a key without it works until the first booking with a companion.
- A scope is a ceiling over your role in AirTrail: AirTrail also asks your role for the same
  permission. The instance's owner has every permission.
- The picker ticks a whole row at a time: ticking **Your flights** to write also ticks
  `flight.export.own` and `flight.delete.own`, so the smallest key it makes holds eight scopes.
  That is fine: tripbox uses neither. `tripbox check` names them as the picker's and counts no
  problem; `tripbox doctor` does not mention them.
- A key that holds more than that works, and tripbox uses none of the rest: before it changes a
  flight for a booking, or fills in a landed flight, it reads who is on it, and writes only when
  you are among its passengers, whatever the key could reach. Scopes that write something else,
  delete other people's flights, or reach them are more than it needs, and `tripbox check` warns
  of them: every scope ending in `.any`, `tracks.write`, `shares.write`,
  `visited_countries.write`, `preferences.write`, `roles.manage`.

`tripbox check` asks AirTrail what the key may do and says it in one line:

```
ok airtrail: answers; the key holds the six scopes
ok airtrail: answers; the key holds the six scopes; it can also: flight.delete.own, flight.export.own (AirTrail's scope picker adds them to the six)
warn airtrail: the key can also flight.read.any; tripbox uses none of them — a narrower key is safer
FAIL airtrail: the key lacks flight.passengers.manage.own
FAIL airtrail: your role there does not permit flight.create.own
FAIL airtrail: refused the key (HTTP 401)
FAIL airtrail: answered HTTP 503, which AirTrail does not for this request — is TRIPBOX_AIRTRAIL_URL the address of AirTrail itself?
FAIL airtrail: this key is another AirTrail user's than the one tripbox's flights were sent as (tripbox airtrail forget)
FAIL airtrail: no answer (ConnectError)
```

What it found is kept with the time it asked, every time it asks (a sync pass, the worker's round of
sending flights to AirTrail, keeps it too, when what it finds has changed): that time is when the
key was last checked. A key of another user than the one tripbox's flights were sent as is a
failure, and nothing of it is kept. `tripbox doctor`, which asks no one, repeats what was kept on
AirTrail's row; a key that lacks a scope, or a role that does not permit one, is a problem `doctor`
counts, though it turns nothing off. `tripbox check` also asks the model, the flight-data service
(its usage, which costs nothing) and Telegram whether they answer; an ntfy topic is only said to be
set, as nothing can be asked of it without posting to it. It ends `dry run: on` or `dry run: off`.

## What is sent

tripbox sends AirTrail only a flight you are on, and only once nothing about its booking is left
to check. A flight of your companions alone, a flight that names nobody, and a flight you answered
"not me" to are never sent. Nothing is ever deleted in AirTrail: a cancelled flight gets a line
first in its note (`CANCELLED` and the day), and one that stands again gets `reinstated`.

Before it adds a flight, tripbox looks for it among your own flights in AirTrail: the same local
day, the same flight number and the same origin. The number is compared by its digits alone, with
leading zeros dropped, so a flight you typed in under another airline's code (a codeshare), with
no code, or as `0123` is found too. A flight that is there with you aboard is taken as it stands
and nothing is sent; one whose passengers do not include you (a companion's alone, or another
user's that a full-access key reads) is not yours and is left alone. Otherwise tripbox adds it:
its day, times and length, its flight number, airports and airline, you as a passenger, each
companion `travellers.yaml` names as a guest with their seat and class, and a note naming the
booking (its confirmation code, the email's Message-ID, the day it
was booked and the provider), with an `Also travelling (not logged): …` line for anyone on the
booking `travellers.yaml` does not name.

When a booking changes, tripbox sends only what changed since AirTrail was last told, onto the
flight as it stands there, and adds a line to the note naming the change. What you or anything
else set there by hand (a gate, a registration, a seat, another arrival time) stays. A companion
who leaves the booking is removed from the flight; your own passenger row is never removed. A
flight's track and custom fields are never in a request.

If you are taken off a flight after it was sent, tripbox does not write to it again. AirTrail goes
on listing it as yours until you remove it there, so the trips page marks the booking: "Your
flight log still lists this flight as yours. Remove it there if you did not fly." **Looks right**
clears the mark.

With `TRIPBOX_AIRTRAIL_DRY_RUN=true`, the default, nothing is sent: each request is written to the
log once, as a line that holds `dry run: would` after its time and level, and the flight waits.
The line is in `logs/tripbox.log` for a booking that came by mail (the poll the worker starts
files it and runs its own sync pass) and for a command you run, such as `tripbox airtrail enroll`.
It is in `logs/tripbox-worker.log` for a booking pasted or uploaded on the page and for the
flights already in trips when AirTrail is switched on. `docker compose logs worker` shows both.

## When AirTrail's answer is not a yes

**No answer, a server error or too many requests** is an outage. tripbox waits (30 seconds,
doubling up to 6 hours) and ends the pass; the flights waiting are sent once AirTrail answers.
After an hour with a flight waiting it sends one alert.

- **The outage is silent when no flight is due.** tripbox asks AirTrail only when a flight is to
  be sent or filled in, so an AirTrail that is down for days while nothing is waiting for it
  raises no alert and shows no trouble. The first flight that comes due after that finds out.
- **A worker started while AirTrail is down** cannot learn whose key it holds, so it asks for
  that again in each sync pass that has a flight due, until AirTrail answers; what it learns is
  then kept until the worker is restarted. A key or a role changed in AirTrail meanwhile is read
  again after a restart.

**A refused key** (401, or a 403 that says nothing more) sends nothing and sends one alert.
**A key that lacks a scope** (a 403 naming it) sends nothing and one alert names the scope.
**A role that does not permit it** (403 `forbidden`) sends nothing and one alert gives AirTrail's
words. In each the pass stops until the key or the role is fixed.

**A flight that is no longer yours there** is handled per flight, and the pass goes on with the
rest. That is what AirTrail's answer means when, for one flight, it says not found, or asks for a
scope ending in `.any`, or when the flight's passengers do not include you: you deleted it or took
yourself off it. tripbox unlinks it and writes nothing to it, and the page offers **Send again**.
(When your role may not read your own flights at all, a not-found means AirTrail hides every
flight from you; v3.14.0 answers such a role with 403 `forbidden` first. tripbox then sends
nothing, unlinks nothing and alerts for the role.)

**A refusal of one new or changed flight** (any other 4xx), or a not-found in answer to a change
while the flight can still be read, is AirTrail declining that booking as it is. The booking is
marked on the page, one alert gives AirTrail's reason, and fixing the booking, or looking at it,
makes tripbox try again. An airport AirTrail does not know marks the booking too, and tripbox asks
again later.

A record in AirTrail is taken for the flight tripbox sent only while at least two of its day, its
flight number and its origin are what tripbox last sent there; for a flight linked before
anything was sent, what the booking says. A part the record lacks counts as different, but when
neither side has a flight number, or neither has an origin code (a flight backfilled from a record
without them), the destination, then the departure time, counts in its place. A flight you
re-dated and re-numbered there by hand is therefore unlinked too, and nothing is written to it:
correct the booking on the trips page to match before you tap **Send again**, or AirTrail gets a
second copy. tripbox cannot tell two flights apart that share a number and an origin on different
days (a weekly commuter flight), nor, where neither side has a number, two flights on one route.
After AirTrail's database is put back from an older copy, look at such flights there.

## The alerts

Each alert goes through your notification channel once, until what it reports is over.

| Alert | What to do |
|---|---|
| "Your flight log has not answered for over an hour; flights waiting for it are sent once it answers again." | Nothing, if AirTrail is coming back. |
| "Your flight log rejected the key tripbox uses (HTTP …); nothing is sent to it until the key works." | Set a key that works. |
| "The key tripbox uses for your flight log lacks the scope …; nothing is sent until a key with all six is set (tripbox check lists what a key lacks)." | Make a key with the six scopes. |
| "Your role in your flight log does not permit what tripbox needs (…); nothing is sent until it does." | Change your role in AirTrail, or use an account whose role permits it. |
| "The flight log tripbox reaches now is not the one its flights were sent to (another AirTrail, or another user's key). Nothing is sent. If this is the flight log to keep, run: tripbox airtrail forget --apply" | See below. |
| "Your flight log refused flight … (HTTP …: …); it is marked on the trips page." | Fix the booking on the page, or look at it and it is tried again. |

## One AirTrail per ledger

The flight ids tripbox keeps are one AirTrail user's flights, and the airport and airline ids it
caches are one AirTrail's own. The first sync pass notes whose key it is. From then on a key that
answers as another user (another AirTrail at the address, or another user's key) is sent nothing:
no flight is looked for, added or changed, and one alert says so. When the address changes, the
cached ids are dropped and asked for again.

To keep the new flight log, run `tripbox airtrail forget` to count, and `tripbox airtrail forget
--apply` to do it:

```
forget: 4 flights are linked to the flight log tripbox knew: 3 upcoming, 1 past; run again with --apply
forget: 4 flights unlinked; of the 3 upcoming, yours are looked for in the flight log now set up and added there if missing; 1 past are held back (tripbox airtrail enroll --since DAY --apply sends them)
```

`--apply` unlinks every flight, forgets what the old key was found to lack and any outage or
refusal it was in, and holds the past flights back. The upcoming flights you are on are then
looked for in the flight log now set up and added there if missing, once AirTrail is on and out
of its dry run. The past ones go with `tripbox airtrail enroll --since YYYY-MM-DD --apply`.
Nothing is changed in either AirTrail by `forget` itself.

## Backfill: the flights the flight log already holds

`tripbox airtrail backfill [--since YYYY-MM-DD] [--apply] [--verbose]` files the flights your flight
log holds since a day (`--since`; two years ago when none is given) as trips, the way their emails
would have: one trip per itinerary (the PNR, the airline's booking reference, in a flight's note),
named for the place you stayed longest, joining a trip an email already made for that place. Without
`--apply` it changes nothing:

```
backfill: 412 flights in the flight log, 38 since 2027-01-01
backfill: 31 would be filed into 9 trips (8 new); 7 are already here; 0 relinked; 0 gone from the flight log
backfill: nothing was changed; run again with --apply
```

`--verbose` adds a line per trip: its name, its days and its flights.

- It reads the flight log and writes nothing there, so it runs in a dry run too. It needs
  `TRIPBOX_HOME_AIRPORTS` (to tell where a trip ends) and an owner in `travellers.yaml`.
- Only your own flights are read: a flight that lists passengers, none of them you, is left
  out. A ledger whose flights went to another AirTrail user's log is refused until
  `tripbox airtrail forget --apply`.
- Each end's city, time zone and airport name come from the airport table tripbox ships, by the
  airport's code, so a backfilled trip and a mailed one name a place the same way; AirTrail's own
  words are kept only for an airport the table does not have. The times are AirTrail's, read in
  that zone, and the day is AirTrail's.
- A backfilled flight is taken as the flight log has it, times and day included, with nothing to
  check, even with no time at all, until an email for it arrives or you edit it on the page. It
  counts as sent: the next sync pass sends nothing for it.
- A flight sent before whose record is gone (deleted, or imported again under another id) is
  pointed at the record now holding it ("relinked", by its day, confirmation code and airports),
  or, with none, unlinked and offered **Send again** ("gone from the flight log").
- Run again, it files nothing twice.

## Status

`tripbox airtrail status` says where the flight log stands, from tripbox's own records alone: it
asks AirTrail nothing and runs offline.

```
flight log: on
flights: 120 sent, 2 waiting, 1 to check, 30 held as past, 1 deleted there, 6 not sent (not yours, or cancelled first)
flights: 1 still name you in the flight log though you are off them (the trips page marks each)
key: the six scopes, checked 2027-08-02 15:00 UTC
```

The first line is `flight log: on`, `on, dry run: nothing is sent`, `off since <time> UTC` or `off`.
With it off, the second line says how many flights still hold a record there from when it was
kept. With it on, the flights line counts the flights you can see by where each stands: sent;
waiting to be sent; to check (a mark on the page holds each back); held as past (they had left when
the flight log was switched on, and `tripbox airtrail enroll` sends them); deleted there (the page
offers **Send again**); and not sent, which counts flights you are not on and flights that were
cancelled before they could be sent. A second line appears while some flights still name you in
the flight log though you are off them.

The key line is what the last look at the key found and when: the six scopes, `lacks` some scope,
`your role there does not permit` something, `refused (HTTP …)`, or `not checked yet (tripbox check
asks AirTrail)`. When the key holds more than tripbox needs, a second line says `key: it may also
…; tripbox uses none of them — a narrower key is safer`.

While sync is stopped, a `trouble:` line says why: `not answering since …`, `the key is refused
since …`, `the key lacks …`, `your role there does not permit what tripbox needs`, or `another
flight log is set up (tripbox airtrail forget)`. It says nothing while the flight log is off.

## Enrichment

With `TRIPBOX_ENRICH=aeroapi` and `TRIPBOX_AEROAPI_KEY` set (and AirTrail on), the worker runs
`tripbox enrich` at 03:30 every night. It is off by default; a flight-data key alone leaves it
off.

- It looks up each flight that landed between two hours and four days ago and that AirTrail
  holds as the trip does (sent, nothing to check, you on it, not cancelled), at most
  `TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN` a night and three nights a flight. Each lookup is one
  request to AeroAPI's `/flights/{designator}` for the days around the flight, by the airline's
  ICAO designator (AirTrail's airline record) and its number, or by its IATA code when AirTrail
  knows no designator. Only an answer that is one flight from that origin on that day is used.
- The budget: before a night's lookups it asks the service what this period has cost so far
  (that costs nothing), and does nothing once that, or tripbox's own count, reaches
  `TRIPBOX_AEROAPI_MONTHLY_BUDGET_USD`.
- It fills blanks only: the registration, the aircraft type (when AirTrail knows it), the
  actual take-off and landing, the gates and the terminals, each only where the flight in
  AirTrail holds nothing. Nothing you or tripbox set there is changed, and nothing the booking
  decides (day, times, number, airports, airline, passengers, note).
- It writes as a sync pass does: only while the key answers as the user whose flights tripbox
  keeps, only on a flight that lists you among its passengers and is still the flight it was
  sent as; a flight that is not is unlinked, as a sync pass would. A flight whose day, number
  or origin you corrected there by hand is left as it is: what was looked up is the booking's
  flight, not yours. A flight a sync pass is
  sending at that moment is left for the next night.
- In a dry run it lists the flights it would look up and asks no one.
- What it filled in, and a flight it gave up on, come in one quiet notice titled "tripbox: flight
  details filled in".

## The Trips button

AirTrail's dock can carry a **Trips** button, after Statistics, that opens tripbox's trips page.
AirTrail takes no plugins, so the button is a patch to AirTrail's source
(`integrations/airtrail/trips-dock.patch`, under AirTrail's own licence, GPL-3.0) and you run an
AirTrail image you build. It is a link and nothing more: tripbox works the same without it.
`integrations/airtrail/README.md` has the details.

1. **Back up AirTrail's database first.** AirTrail upgrades its database on every start, and its
   upgrades run forward only: going back to the image you run now means restoring this backup.
2. **Build:** the install bundle holds none of `integrations/`, so first clone tripbox's repository
   (`git clone https://github.com/Good-Work-Dojo/tripbox`) and run, from its folder,
   `integrations/airtrail/build.sh <the AirTrail release you run> <trips address>
   <image tag>`; the release you run is the first number in the foot of AirTrail's Settings
   window with a `v` before it (`v3.14.0`), since an `image:` line may say `latest`. The address is
   tripbox's `TRIPBOX_BASE_URL` (`https://trips.example.com`, or `/tripbox` when both share one
   hostname: [reverse proxy](reverse-proxy.md)). It ends `build: ok <image tag>`. The patch was
   written for AirTrail v3.14.0; `integrations/airtrail/patch-check.sh --tag <release>` says
   whether it applies to yours.
   Never build an older release than the one you run: AirTrail upgrades its database on start
   and cannot go back.
3. **Use it:** put the tag on the `image:` line of AirTrail's own service in its compose file and
   run `docker compose up -d`.

The way back is the old `image:` line, and the backup from step 1 when the patched image ran a
newer AirTrail. The patch was written for AirTrail v3.14.0; against a release whose dock has
changed, `build.sh` stops at `patch` and builds nothing. tripbox's repository checks every week
that the patch still applies to AirTrail's newest release and opens an issue when it does not.

## Known limits

Matching an email to a booking has limits. Each shows on the trips page, and the page action
named puts the flight log right too. In each case the flight log follows what the page shows.

- **A later receipt under another confirmation code, for a flight the booking has moved away
  from,** joins the booking that moved. Its traveller is then on the moved booking, and on the
  moved flight in AirTrail as a guest. Take them off the booking on the page.
- **A change email that names only some of a booking's travellers** moves everyone on the booking,
  you included, when the booking has a single confirmation code. If only those travellers moved,
  put the booking back on its old day or number on the edit form and take them off it.
- **One email that takes a companion off a flight and puts them on another with someone the
  booking does not hold** moves the whole booking, you included. The same edit puts it right.
- **One email that cancels everyone's seat and puts you alone on another flight** leaves the
  booking moved and cancelled: AirTrail shows the new flight with the `CANCELLED` line. Mark the
  booking confirmed on the page and take the others off it; the flight gets a `reinstated` line.
- **You and a companion booked one flight under two confirmation codes, and the airline mails the
  change once for each code,** the companion's notice first: the new flight becomes a booking of
  its own, yours joins it, and the old booking stays, so AirTrail holds both flights as yours.
  Cancel the old booking on the page; its flight there gets the `CANCELLED` line.
- **A flight typed in without its confirmation code** is not found by a later change: the change
  makes a booking of its own, and AirTrail holds both flights. Cancel the one you typed in. A
  flight typed in with its code is moved by the change instead.
- **A flight whose airports and time could not be read** (a draft marked to check), on a flight
  number that flies two legs in one day, is never completed by the other leg's email. An email
  that prints its time makes a booking of its own; one that prints none can join the draft instead.
  Nothing is sent until you put the draft right on the edit form; it is then sent as the leg you
  typed. Add the other leg by hand if an email joined it.
- **An older email read again** (`replay --force`) brings back a companion a later email took
  off: the page shows them on the booking and AirTrail names them again. Take them off on the
  page; that holds through every later reading. Reading every email again in order takes them off
  once more with the later email, and the note gains a `changed` line each time. No email takes
  you off a flight: one that cancels your seat cancels the booking.
- **Two stays under one itinerary number** in one email fold into one booking.

## What was run

These facts were checked against AirTrail v3.14.0 as upstream ships it (`johly/airtrail`, pinned
by digest), with its own database and its demo users, by `tests/test_flightlog_matrix.py` and
`tests/test_flightlog_e2e.py`. Five keys were used, four of them the instance owner's (a role that
reaches every flight): one with the six scopes, the owner's own hands (the six and
`flight.delete.own`), one without `flight.passengers.manage.own`, one with the three `.any` scopes
a full-access key holds; and another user's with the six.

- A flight is added once, with its length in seconds, its airports and airline by AirTrail's
  ids, you as the one user passenger and companions as guests with their seats and class.
- A change sends only what changed and adds one `changed` line to the note; a second pass sends
  nothing. A registration, a gate, a seat and a note line set by hand stay. A flight read back
  with nothing to change is taken as it stands: AirTrail hands dates and times back as the same
  instants.
- A cancellation is a `CANCELLED` line first in the note, and the flight stays listed.
- A companion's receipt alone sends nothing; your receipt then sends the flight with both.
- A flight deleted by hand is unlinked at its next change and sent again only after **Send
  again**, as a new flight. A flight you took yourself off by hand is unlinked and not written
  to, and the flights behind it in the pass are sent: with the six-scope key AirTrail refuses it
  (asking for `flight.read.any`), with a full-access key tripbox reads it and finds you not
  aboard. A full-access key never writes to another user's flight, which it can read.
- A flight you logged by hand is taken as it stands, not added twice.
- Switched on, upcoming flights are sent and past ones held until `tripbox airtrail enroll`.
- No answer: the flight waits 30 seconds, then 60, doubling; one alert after an hour, and the
  flight is sent once the flight log answers. A key AirTrail never issued stops the pass with one
  alert; a key without the passengers scope sends your own flight and stops at a flight with a
  companion; another user's key stops the pass and nothing is written.
- The whole test corpus, filed with a flight log kept, leaves in AirTrail exactly the flights
  `tests/corpus/flightlog.json` lists; read again, it adds none. AirTrail knew every airport.
- The second and fifth known limits above, run as written.
