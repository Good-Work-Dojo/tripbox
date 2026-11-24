#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Give the demo ledger cover photos, so the screenshot harness photographs them.

    python covers_seed.py LEDGER_DIR        (the folder that holds data/ledger.db)

Run in the tripbox image, as the user that owns the folder, with no network. The pictures are
drawn here with Pillow (gradients and shapes, no file from outside) and made into covers by the
app's own `coverimage.process`. Four trips, chosen from the dates the demo seed gave them:
  - the first one still to come: a ready Wikimedia cover (a wide landscape) with an invented credit;
  - the one under way: a ready owner cover (a tall portrait, which the hero crops);
  - the latest past trip: an owner upload still pending (no worker runs in the stack, so the page
    keeps its "on its way" note);
  - the one before it: an owner upload that failed.
Every other trip keeps its tile. Prints one line per cover."""
from __future__ import annotations

import datetime as dt
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from tripbox.ledger import Ledger
from tripbox.trips import coverimage
from tripbox.trips.covers import CoverStore
from tripbox.trips.store import TripStore

CREDIT = {
    "artist": "Example Photographer",
    "licence": "CC BY-SA 4.0",
    "licence_url": "https://commons.wikimedia.org/wiki/File:Example.jpg#Licensing",
    "source_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
}


Colour = tuple[int, int, int]


def _blend(a: Colour, b: Colour, t: float) -> Colour:
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _sky(size: tuple[int, int], top: Colour, bottom: Colour) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=_blend(top, bottom, y / h))
    return img


def landscape() -> bytes:
    """A wide picture: a pale sky (the title's worst case for a light page), a sun, two hills."""
    w, h = 2400, 1500
    img = _sky((w, h), (214, 232, 246), (250, 236, 214))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.66, h * 0.16, w * 0.66 + 260, h * 0.16 + 260], fill=(255, 250, 235))
    d.ellipse([-400, h * 0.62, w * 0.7, h * 1.9], fill=(88, 128, 96))
    d.ellipse([w * 0.3, h * 0.72, w * 1.5, h * 2.1], fill=(52, 92, 84))
    return _save(img)


def portrait() -> bytes:
    """A tall picture: a bright band in the middle and dark ground below, so a crop to the hero
    shows both."""
    w, h = 1200, 1800
    img = _sky((w, h), (30, 46, 92), (236, 150, 96))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(h * 0.55), w, h], fill=(32, 30, 44))
    for i, x in enumerate(range(80, w, 240)):
        top = int(h * (0.34 + 0.05 * (i % 3)))
        d.rectangle([x, top, x + 150, int(h * 0.55)], fill=(60, 58, 84))
    d.ellipse([w * 0.4, h * 0.42, w * 0.4 + 200, h * 0.42 + 200], fill=(255, 232, 170))
    return _save(img)


def _save(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def chosen(trips: list, today: dt.date) -> dict[str, object]:
    """The trips that take a cover, by role, from the dates: `upcoming` (the first that starts
    after today and is not cancelled), `under_way`, and the two latest past trips (`pending`,
    then `failed`). A role with no trip is left out."""
    live = [t for t in trips if not t.cancelled]
    day = today.isoformat()
    found: dict[str, object] = {}
    for t in sorted(live, key=lambda t: (t.start_date, t.id)):
        if t.start_date > day:
            found.setdefault("upcoming", t)
        elif t.end_date >= day:
            found.setdefault("under_way", t)
    past = sorted((t for t in live if t.end_date < day), key=lambda t: (t.end_date, t.id))
    for role, t in zip(("pending", "failed"), reversed(past), strict=False):
        found[role] = t
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: covers_seed.py LEDGER_DIR", file=sys.stderr)
        return 2
    path = Path(argv[1]) / "data" / "ledger.db"
    ledger = Ledger.open(path)
    try:
        trips = TripStore(ledger, ()).all_trips()
        roles = chosen(trips, dt.date.today())
        covers = CoverStore(ledger)
        wide, tall = coverimage.process(landscape()), coverimage.process(portrait())
        if (t := roles.get("upcoming")) is not None:
            covers.found(t.id, t.place or t.title, wide.large, wide.small, wide.digest, CREDIT)
            print(f"cover: {t.title}: ready, wikimedia")
        if (t := roles.get("under_way")) is not None:
            taken = covers.upload(t.id, portrait())
            covers.finish_upload(taken, tall.large, tall.small, tall.digest)
            print(f"cover: {t.title}: ready, owner")
        if (t := roles.get("pending")) is not None:
            covers.upload(t.id, landscape())
            print(f"cover: {t.title}: pending, owner")
        if (t := roles.get("failed")) is not None:
            taken = covers.upload(t.id, b"not a picture")
            covers.fail_upload(taken, "unreadable")
            print(f"cover: {t.title}: failed, owner")
        if len(roles) != 4:
            print(f"cover: only {len(roles)} of 4 trips found", file=sys.stderr)
            return 1
        ledger.conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
