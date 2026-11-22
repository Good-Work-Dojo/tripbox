# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Share links: a read-only page of one trip that anyone holding the link opens without signing
in, showing what the owner chose of it (`FIELDS`). A link is a random token of 192 bits in an
address; the ledger keeps the token, so the owner can copy the link again, and finds a link by the
token's SHA-256 alone. A link works until 7 days after its trip ends as the trip now stands, and
at least 30 days after the day it was made; never once it is revoked or its trip is deleted. A
trip that goes for good (a merge empties its source, the purge) takes its links with it, by the
ledger's cascade, so a link never shows another trip than the one it was made for."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tripbox.util import parse_iso

if TYPE_CHECKING:
    import sqlite3

    from tripbox.ledger import Ledger
    from tripbox.trips.store import Trip, TripStore

# What a link may show beside the trip's title, its dates and its bookings' kinds and names
# (a flight's route and number, a stay's or a car's name); the first four by default
FIELDS = ("names", "addresses", "seats", "times", "codes", "notes")
DEFAULT = FIELDS[:4]
TOKEN_BYTES = 24  # 192 bits, written as 32 URL-safe characters
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
TOKEN = re.compile(r"[A-Za-z0-9_-]{32}", re.ASCII)
AFTER_THE_TRIP = dt.timedelta(days=7)
AT_LEAST = dt.timedelta(days=30)


@dataclass(frozen=True)
class Link:
    id: int
    trip_id: int
    token: str = field(repr=False)  # opens the trip's page: never in a repr
    shows: tuple[str, ...]
    created_at: str


def digest(token: str) -> str:
    """The SHA-256 of a token, by which its link is found."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _link(row: sqlite3.Row) -> Link:
    return Link(
        id=row["id"],
        trip_id=row["trip_id"],
        token=row["token"],
        shows=tuple(f for f in json.loads(row["shows"]) if f in FIELDS),
        created_at=row["created_at"],
    )


def create(ledger: Ledger, trip_id: int, shows) -> Link:
    """A new link to the trip `trip_id`, showing `shows` (in FIELDS's order); a field outside
    FIELDS is a ValueError and nothing is stored."""
    chosen = set(shows)
    unknown = chosen - set(FIELDS)
    if unknown:
        raise ValueError(f"a share link cannot show {', '.join(sorted(unknown))}")
    ordered = [f for f in FIELDS if f in chosen]
    token = secrets.token_urlsafe(TOKEN_BYTES)
    link_id = ledger.shares.add(
        trip_id=trip_id, token=token, token_hash=digest(token), shows=json.dumps(ordered)
    )
    row = ledger.conn.execute("SELECT * FROM share_links WHERE id=?", (link_id,)).fetchone()
    return _link(row)


def good_until(link: Link, trip: Trip) -> dt.date:
    """The last day the link works: the later of 7 days after the trip's end as it now stands and
    30 days after the day the link was made (in the process's zone, as `today_local`)."""
    made = parse_iso(link.created_at).astimezone().date()
    return max(dt.date.fromisoformat(trip.end_date) + AFTER_THE_TRIP, made + AT_LEAST)


def find(ledger: Ledger, store: TripStore, token: str, today: dt.date) -> tuple[Link, Trip] | None:
    """The link `token` names and its trip, while it works on `today`; None for a token that is
    not 32 URL-safe characters, one no link holds, a revoked link, a day after `good_until` and
    a trip that is deleted. The token is never compared but through its hash."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    hashed = digest(token)
    row = ledger.shares.by_hash(hashed)
    if row is None or not secrets.compare_digest(row["token_hash"], hashed):
        return None
    if row["revoked_at"] is not None:
        return None
    trip = store.get(row["trip_id"])  # None for a deleted trip
    if trip is None:
        return None
    link = _link(row)
    if today > good_until(link, trip):
        return None
    return link, trip


def live(
    ledger: Ledger, store: TripStore, today: dt.date, trip_id: int | None = None
) -> list[tuple[Link, Trip, dt.date]]:
    """The links that work on `today` (of the trip `trip_id`, or of every trip), each with its
    trip and the last day it works, oldest first."""
    rows = ledger.shares.live() if trip_id is None else ledger.shares.by_trip(trip_id)
    out = []
    for row in rows:
        trip = store.get(row["trip_id"])
        if trip is None:
            continue
        link = _link(row)
        until = good_until(link, trip)
        if today <= until:
            out.append((link, trip, until))
    return out
