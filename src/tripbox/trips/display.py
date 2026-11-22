# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""How the owner likes a booking said: the clock (12- or 24-hour), the order of a date's day and
month, and the maps an address opens in. They are the owner's taste, chosen on the Settings page
and kept in the ledger (`kv` row `display`, JSON), not in the settings file. `trips.format.styled`
makes every text honour them; with none stored, every text reads as it always has."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tripbox.ledger import Ledger

KV = "display"
# Each choice and its values, the first its default
CHOICES: dict[str, tuple[str, ...]] = {
    "clock": ("12", "24"),
    "dates": ("mdy", "dmy"),
    "maps": ("apple", "google", "osm"),
}
# Where an address opens, by the maps chosen; `{q}` is the address, quoted
MAPS = {
    "apple": "https://maps.apple.com/?q={q}",
    "google": "https://www.google.com/maps/search/?api=1&query={q}",
    "osm": "https://www.openstreetmap.org/search?query={q}",
}


@dataclass(frozen=True)
class Display:
    clock: str = "12"  # 12 | 24
    dates: str = "mdy"  # mdy: Mar 13 | dmy: 13 Mar
    maps: str = "apple"  # apple | google | osm


def stored(ledger: Ledger) -> Display | None:
    """The choices the ledger holds, or None when it holds none it can read: no row, a row that
    is no JSON object, or one with a value outside CHOICES."""
    raw = ledger.kv.get(KV)
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    chosen = {k: v for k, v in doc.items() if k in CHOICES}
    if any(not isinstance(v, str) or v not in CHOICES[k] for k, v in chosen.items()):
        return None
    return Display(**chosen)


def read(ledger: Ledger) -> Display:
    """The choices the ledger holds; anything unreadable is the default."""
    return stored(ledger) or Display()


def write(ledger: Ledger, **chosen: str) -> Display:
    """Store `chosen` over what the ledger holds and return the whole: a name or a value outside
    CHOICES is a ValueError, and nothing is stored."""
    for name, value in chosen.items():
        if name not in CHOICES or value not in CHOICES[name]:
            raise ValueError(f"{name} cannot be {value!r}")
    display = replace(read(ledger), **chosen)
    ledger.kv.set(KV, json.dumps(asdict(display), sort_keys=True))
    return display


def maps_url(display: Display, query: str) -> str:
    """The address that opens `query` (a street address) in the maps chosen."""
    return MAPS[display.maps].format(q=urllib.parse.quote(query))
