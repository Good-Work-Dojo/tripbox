# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Backfill tripbox from AirTrail's flight log (`tripbox airtrail backfill`).

Trip items are otherwise created only from emails, so flights that are in AirTrail but never
came by email (a log kept before tripbox was set up, or one imported into AirTrail later) have
no trips. This files them the way emails would. Re-runnable: every leg is keyed by the rule
that keys an emailed segment (`validate.keys.real_key`) from its carrier, number and origin
and AirTrail's own date for the flight, or `flight|airtrail|<id>` when that rule gives no key;
a key that already has a trip item is skipped, and trip items that still point at AirTrail
flights since deleted and imported again are re-pointed first (`_relink`), so a later email
about one of those bookings updates it instead of logging it again; one whose flight is gone
with nothing in its place is held out of the reconcile as deleted there.

Grouping is done here rather than by `TripStore.find_or_create`, whose "any trip within two
days" rule would file a departure into a trip to another place that ended two days earlier:
legs are chained per PNR, a chain's place is the airport with the longest stay, and a chain only
joins an existing trip of the same place (a return leg under another PNR still finds the trip its
outbound made). Each filed leg is recorded as told what the flight log holds of it — its own
record — so the reconcile has nothing to send for it, and gets a `filed` row in the message log
(`items`) under one synthetic `messages` row per run (`source` = SOURCE). The booking itself
names no message as its source: it came from the flight log.

Each end's city, zone and airport name come from the bundled airport table by its code, as mail's
do, so a backfilled trip and a mailed one spell a place the same way; the flight log's own words
stand only for a code the table lacks. The backfill reads the flight log and writes nothing there.
It goes through the same binding as the sync: the ledger is one AirTrail user's (`lifecycle.bind`),
a record that lists passengers but not that user is not the owner's and is never filed or linked,
and a linked record that has become another flight (`sync.another_flight`) is relinked or
unlinked as one that is gone.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from tripbox.ledger import Ledger
from tripbox.trips import grouping
from tripbox.trips.store import FLIGHT_LOG_SOURCE, Trip, TripStore
from tripbox.util import now_iso
from tripbox.validate import checks
from tripbox.validate.keys import real_key
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import parse_local, to_utc_iso
from tripbox.write.lifecycle import USER_KV, bind
from tripbox.write.sync import another_flight, not_listed

# what the backfill stamps on what it files: its synthetic message's source and each flight's
# payload["source"], which keeps the flight log's fields as they are until mail or the owner
# changes the booking (see trips.store.FLIGHT_LOG_SOURCE)
SOURCE = FLIGHT_LOG_SOURCE


class BackfillRefused(ValueError):
    """A precondition the command cannot run without; the message says how to fix it."""


NO_HOME = (
    "TRIPBOX_HOME_AIRPORTS is empty: backfill cannot tell where a trip ends — set it"
    " (e.g. TRIPBOX_HOME_AIRPORTS=MSP) and run again"
)
NO_OWNER = (
    "travellers.yaml names no owner: backfill files AirTrail's flights as the owner's —"
    " add owner: names: first"
)
OTHER_LOG = (
    "The flight log tripbox reaches now is not the one its flights were sent to (another"
    " AirTrail, or another user's key); if this is the flight log to keep, run: tripbox airtrail"
    " forget --apply"
)
PNR_RE = re.compile(r"\bPNR ([A-Z0-9]{5,8})\b")
# an IATA flight code: two-character carrier (letter+alnum or digit+letter) and the number
CODE_RE = re.compile(r"^([A-Z][A-Z0-9]|[0-9][A-Z])\s*0*(\d{1,4})[A-Z]?$")
# what a relinked trip item takes over from the flight that replaced its old one
SCHEDULE_FIELDS = (
    "carrier",
    "flight_number",
    "flight_code",
    "dep_local",
    "arr_local",
    "dep_utc",
    "arr_utc",
    "date_local",
    "duration_min",
)


@dataclass
class BackfillItem:
    """What TripStore.upsert_item needs of an item: its kind, key, payload, PNR, passengers and
    status."""

    kind: str
    key: str
    payload: dict
    pnr: str | None
    passengers: list[str]
    status: str = "confirmed"


@dataclass
class BackfillReport:
    since: str
    applied: bool
    fetched: int = 0
    in_window: int = 0
    relinked: list[tuple[int, str, str, int]] = field(default_factory=list)  # item, old, new, fid
    absent: list[tuple[int, str, int]] = field(default_factory=list)  # item, key, dead fid
    skipped: list[str] = field(default_factory=list)  # keys that already had a trip item
    filed: list[tuple[str, int, str]] = field(default_factory=list)  # key, trip id, trip title
    created: list[tuple[int, str, str, str]] = field(default_factory=list)  # id, title, start, end
    # each trip a leg was filed into, as it stands once they all are: title, first day, last
    # day and the legs' flight codes, in the order they were filed
    trips: list[tuple[str, str, str, list[str]]] = field(default_factory=list)

    def lines(self, *, verbose: bool = False) -> list[str]:
        """What `tripbox airtrail backfill` prints: counts, and with `verbose` one line per
        trip. No key and no id."""
        filed = len(self.filed)
        done = "filed" if self.applied else "would be filed"
        out = [
            f"backfill: {self.fetched} flights in the flight log, {self.in_window} since"
            f" {self.since}",
            f"backfill: {filed} {done} into {len(self.trips)} trips ({len(self.created)} new);"
            f" {len(self.skipped)} are already here; {len(self.relinked)} relinked;"
            f" {len(self.absent)} gone from the flight log",
        ]
        if verbose:
            out += [
                f"  {title}, {first} to {last}: {', '.join(codes)}"
                for title, first, last, codes in self.trips
            ]
        if not self.applied:
            out.append("backfill: nothing was changed; run again with --apply")
        return out


# ----------------------------------------------------------------------------- items
def _local(iso: str | None, tz: str | None) -> tuple[str | None, dt.datetime | None]:
    """AirTrail stores instants with an explicit offset; the page wants airport-local wall time."""
    if not iso:
        return None, None
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    try:
        local = d.astimezone(ZoneInfo(tz)) if tz else d
    except KeyError, ValueError:  # a zone name the host does not know
        local = d
    return local.strftime("%Y-%m-%dT%H:%M"), d


def _end(record: dict) -> dict:
    """One end of a flight as the payload keeps it: the code AirTrail gives it, and the city,
    zone and airport name of the bundled table for that code (as mail's), or AirTrail's own
    words for a code the table lacks."""
    code = str(record.get("iata") or "").upper()
    known = Airports().airport(code)
    if known is not None:
        return {
            "code": code,
            "name": known["name"],
            "city": known["municipality"],
            "tz": known["timezone"],
        }
    return {
        "code": code,
        "name": record.get("name"),
        "city": record.get("municipality"),
        "tz": record.get("timezone"),
    }


def flight_item(f: dict, me_name: str, user_id: str | None = None) -> BackfillItem:
    """One AirTrail flight (API v1 DTO) as the item the validator would have made from an
    email for the same segment — same keys, same formats. Its local times are AirTrail's
    instants read in each end's zone (`_end`); its day is AirTrail's own. The owner's seat and
    class are those of the passenger row of `user_id` (the key's user), or of the first row
    naming a user when none is given."""
    fr, to = _end(f.get("from") or {}), _end(f.get("to") or {})
    code = str(f.get("flightNumber") or "").upper().replace(" ", "")
    m = CODE_RE.match(code)
    carrier, number = (m.group(1), m.group(2)) if m else ("", "")
    dep_local, dep = _local(f.get("departure") or f.get("departureScheduled"), fr["tz"])
    arr_local, arr = _local(f.get("arrival") or f.get("arrivalScheduled"), to["tz"])
    date_local = str(f.get("date") or "")[:10] or (dep_local or "")[:10]
    if not dep_local:
        dep_local = date_local  # the log has no time for this one: a date-only flight
    origin, dest = fr["code"], to["code"]
    pax = f.get("passengers") or []
    if user_id is None:
        mine = next((p for p in pax if p.get("user")), pax[0] if pax else {})
    else:
        mine = next((p for p in pax if _rider_id(p) == user_id), {})
    seat = mine.get("seatNumber")
    pm = PNR_RE.search(str(f.get("note") or ""))
    pnr = pm.group(1) if pm else None
    airline = f.get("airline") or {}
    fid = int(f["id"])
    # Keyed by the rule mail is keyed by, on the day AirTrail gives the flight: mail keys on the
    # local departure day as printed, and AirTrail's date is that day even where its record has
    # no zone for the origin (the departure would then read in UTC). The stored departure is
    # kept as built.
    keying = {
        "carrier": carrier,
        "flight_number": number,
        "origin": origin,
        "dep_local": date_local,
    }
    key = real_key("flight", keying, airports=Airports()) or f"flight|airtrail|{fid}"
    duration = f.get("duration")
    payload = {
        "carrier": carrier,
        "flight_number": number,
        "flight_code": f"{carrier}{int(number)}" if number else code,
        "origin": origin,
        "destination": dest,
        "from_id": (f.get("from") or {}).get("id"),
        "to_id": (f.get("to") or {}).get("id"),
        "airline_id": airline.get("id"),
        "airline_name": airline.get("name"),
        "airline_icao": airline.get("icao"),
        "origin_name": fr["name"],
        "origin_city": fr["city"],
        "origin_tz": fr["tz"],
        "destination_name": to["name"],
        "destination_city": to["city"],
        "destination_tz": to["tz"],
        "dep_local": dep_local,
        "arr_local": arr_local,
        "dep_utc": to_utc_iso(dep) if dep else None,
        "arr_utc": to_utc_iso(arr) if arr else None,
        "date_local": date_local,
        "duration_min": (
            int(duration) // 60
            if duration
            else (int((arr - dep).total_seconds() // 60) if dep and arr else None)
        ),
        "cabin": mine.get("seatClass"),
        "seats": [seat] if seat else [],
        "seat_by_passenger": {me_name: seat} if seat else {},
        "pnr": pnr,
        "provider": airline.get("name"),
        "status": "confirmed",
        "passengers": [me_name],
        "price": None,
        "currency": None,
        "aircraft_reg": f.get("aircraftReg"),
        "source": SOURCE,
        "airtrail_flight_id": fid,
    }
    return BackfillItem("flight", key, payload, pnr, [me_name])


def _rider_id(row: dict) -> object:
    user = row.get("user")
    return user.get("id") if isinstance(user, dict) else row.get("userId")


# ----------------------------------------------------------------------------- grouping
def _sort_key(it: BackfillItem) -> tuple[str, str]:
    p = it.payload
    return (p.get("date_local") or "", p.get("dep_utc") or "")


def chains(items: list[BackfillItem], home: tuple[str, ...]) -> list[list[BackfillItem]]:
    """Legs in departure order, chained per PNR: a PNR is one itinerary, and it is over once a
    leg lands at home (the same code is never reused for a later trip)."""
    open_: dict[str, list[BackfillItem]] = {}
    out: list[list[BackfillItem]] = []
    for it in sorted(items, key=_sort_key):
        k = it.pnr or f"#{it.payload['airtrail_flight_id']}"
        chain = open_.get(k)
        if chain is not None and chain[-1].payload["destination"] not in home:
            chain.append(it)
        else:
            chain = [it]
            out.append(chain)
            open_[k] = chain
    return out


def _instant(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def place_of(chain: list[BackfillItem], home: tuple[str, ...]) -> str:
    """Where the trip was: the airport with the longest stay. A chain that starts away from
    home was already there (the return half of an open-jaw); a chain that ends away stays there."""
    first = chain[0].payload
    best: tuple[float, str] | None = None
    if first["origin"] not in home:
        best = (float("inf"), first.get("origin_city") or first["origin"])
    for i, leg in enumerate(chain):
        p = leg.payload
        if p["destination"] in home:
            continue
        if i + 1 < len(chain):
            nxt = chain[i + 1].payload
            a, d = _instant(p.get("arr_utc")), _instant(nxt.get("dep_utc"))
            if a and d:
                gap = (d - a).total_seconds()
            else:  # date-only legs: count days
                da, dd = parse_local(p.get("date_local")), parse_local(nxt.get("date_local"))
                gap = ((dd - da).total_seconds()) if da and dd else 0.0
        else:
            gap = float("inf")
        city = p.get("destination_city") or p["destination"]
        if best is None or gap > best[0]:
            best = (gap, city)
    if best is None:
        return _short(first.get("destination_city") or first["destination"] or "Trip")
    return _short(best[1])


def _short(city: str) -> str:
    """A city printed with its region ("City, Region", as a flight log's records may print it)
    reads as the city alone on a trip card."""
    return city.split(",", 1)[0].strip() or city


def _span(chain: list[BackfillItem]) -> tuple[dt.date, dt.date]:
    dates = [grouping.item_dates(it) for it in chain]
    dates = [d for d in dates if d]
    if not dates:
        raise ValueError("chain has no dates")
    return min(d[0] for d in dates), max(d[1] for d in dates)


# ----------------------------------------------------------------------------- relink
def _match(
    flights: list[dict], *, date_local: str, codes: tuple[str, ...], origin: str, dest: str
) -> dict | None:
    """The imported flight that is the same booking as a trip item whose AirTrail id died: same
    local date, a PNR (from the note) that is one of the booking's codes, and the same airports
    — the number may differ."""
    for f in flights:
        if str(f.get("date") or "")[:10] != date_local:
            continue
        pm = PNR_RE.search(str(f.get("note") or ""))
        if not pm or pm.group(1) not in codes:
            continue
        fo = str((f.get("from") or {}).get("iata") or "").upper()
        fd = str((f.get("to") or {}).get("iata") or "").upper()
        if origin and fo != origin or dest and fd != dest:
            continue
        return f
    return None


def _relink(
    store: TripStore, flights: list[dict], me_name: str, user_id: str, rep: BackfillReport
) -> None:
    """Every flight the trip store holds a dead AirTrail id for (`TripStore.sent_flights`) is
    pointed at the flight imported in its place, or, with none, unlinked and held out of the
    reconcile as deleted there. An id is dead when the owner's flights (`flights`) hold no record
    under it, or the one they hold has become another flight (`sync.another_flight`, against
    what it was told or else what the booking says). A cancelled flight is left as it is: its
    record there carried the cancellation, which a copy imported in its place does not. The
    message log is left as it was written: its rows point at the booking, which stays."""
    live = {int(f["id"]): f for f in flights}
    for ti in store.sent_flights():
        fid = int(ti.airtrail_flight_id)  # type: ignore[arg-type]
        if ti.status == "cancelled":
            continue
        known = ti.told or checks.desired(ti.payload, ti.passengers, ti.status, store.book)
        if fid in live and not another_flight(live[fid], known):
            continue
        p = ti.payload
        parts = ti.item_key.split("|")
        date_local = str(p.get("date_local") or (parts[2] if len(parts) > 3 else ""))[:10]
        f = _match(
            [g for g in flights if int(g["id"]) != fid],
            date_local=date_local,
            codes=tuple(c for c in (ti.confirmation or "").split(", ") if c),
            origin=str(p.get("origin") or "").upper(),
            dest=str(p.get("destination") or "").upper(),
        )
        if f is None:
            store.sync_detached(ti.id)
            rep.absent.append((ti.id, ti.item_key, fid))
            continue
        new = flight_item(f, me_name, user_id)
        sched = {k: new.payload.get(k) for k in SCHEDULE_FIELDS}
        new_id = int(f["id"])
        store.relink_flight(ti.item_key, new.key, new_id, sched)
        rep.relinked.append((ti.id, ti.item_key, new.key, new_id))


# ----------------------------------------------------------------------------- run
def backfill(
    ledger: Ledger,
    store: TripStore,
    client,
    *,
    since: str,
    me_name: str,
    apply: bool,
) -> BackfillReport:
    """Relink dead AirTrail ids, then file every flight dated on/after `since` that has no trip
    item yet. The flight log's profile is read first: a ledger bound to another user than the
    key's is refused (OTHER_LOG), and one bound to nobody is bound to it when the run is applied.
    Only the user's own flights are read (a record that lists passengers but not the user is
    left out). One transaction: a dry run rolls everything back."""
    if not store.home:
        raise BackfillRefused(NO_HOME)
    if not me_name:
        raise BackfillRefused(NO_OWNER)
    profile = client.profile()
    held = ledger.kv.get(USER_KV)
    if held and held != profile.user_id:
        raise BackfillRefused(OTHER_LOG)
    if apply:
        bind(ledger, profile)
    rep = BackfillReport(since=since, applied=apply)
    listed = list(client.iter_flights(limit=100))  # every page; the window is applied here
    rep.fetched = len(listed)
    flights = [f for f in listed if not not_listed(f, profile.user_id)]
    home = store.home
    c = ledger.conn
    c.execute("BEGIN")
    try:
        _relink(store, flights, me_name, profile.user_id, rep)
        window = [f for f in flights if str(f.get("date") or "")[:10] >= since]
        rep.in_window = len(window)
        items = [flight_item(f, me_name, profile.user_id) for f in window]
        mid: int | None = None
        n = 0  # each leg's place in the run's message
        legs_of: dict[int, list[str]] = {}  # trip id -> codes of the legs filed into it
        for chain in chains(items, home):
            legs = []
            for it in chain:
                if store.item_by_key(it.key) is None:
                    legs.append(it)
                else:
                    rep.skipped.append(it.key)
            if not legs:
                continue
            place = place_of(chain, home)
            d0, d1 = _span(legs)
            trip: Trip | None = store.same_place_trip(place, d0, d1)
            if trip is None and chain[0].payload["origin"] not in home:
                trip = store.covering_trip(d0, d1)  # a return half with no same-place trip yet
            created = trip is None
            if trip is None:
                trip = store.new_trip(place, d0, d1)
            if mid is None:
                stamp = now_iso()
                mid = ledger.messages.insert(
                    sha256=hashlib.sha256(f"{SOURCE}:{stamp}".encode()).hexdigest(),
                    source=SOURCE,
                    status="processed",
                    email_date=stamp,
                    subject=f"AirTrail backfill since {since}",
                )
            for it in legs:
                fid = int(it.payload["airtrail_flight_id"])
                ti = store.upsert_item(trip, it, airtrail_flight_id=fid)
                # the flight log holds what it should of the leg: its own record
                told = checks.desired(ti.payload, ti.passengers, ti.status, store.book)
                ti = store.sync_done(ti.id, flight_id=fid, told=told)
                ledger.items.add(
                    message_id=mid,
                    n=n,
                    kind="flight",
                    item_key=ti.item_key,
                    outcome="filed",
                    payload=it.payload,
                    trip_item_id=ti.id,
                )
                n += 1
                rep.filed.append((it.key, ti.trip_id, trip.title))
                code = str(ti.payload.get("flight_code") or "") or "(no number)"
                legs_of.setdefault(ti.trip_id or trip.id, []).append(code)
            if created:
                rep.created.append((trip.id, trip.title, "", ""))
        # a return leg under another PNR widens a trip after it was created: report final dates
        rep.created = [
            (t.id, t.title, t.start_date, t.end_date)
            for tid, *_ in rep.created
            if (t := store.get(tid)) is not None
        ]
        rep.trips = [
            (t.title, t.start_date, t.end_date, codes)
            for tid, codes in legs_of.items()
            if (t := store.get(tid)) is not None
        ]
        c.execute("COMMIT" if apply else "ROLLBACK")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return rep
