# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The demo: part of the corpus filed through the pipeline into a new ledger whose days all count
from a chosen today (`seed`), so that whatever the day, one trip is under way, one is to come and
several are past. Each email is filed with the clock held at its arrival and every date it gives
moved by the days from the corpus's anchor to today, so two seeds with the same today are
identical (`dump`) and every today tells the same story (`relative_view`)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from tripbox import corpus
from tripbox.ledger import Ledger
from tripbox.pipeline.run import build_context, process_raw
from tripbox.pipeline.shift import DateShift
from tripbox.trips.store import TripItem, TripStore
from tripbox.util import clock_at
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rules import FUTURE_LIMIT, parse_local

log = logging.getLogger("tripbox.demo")

# How a demo email may end and still be filed as the story means.
FILED = ("processed", "ignored")
# The days a demo's dates may be moved to: years written with four digits, less room at either
# end for the pipeline's own arithmetic on them (a booking is checked against days up to
# FUTURE_LIMIT from its email's).
FIRST_DAY = dt.date(1000, 1, 1) + 2 * FUTURE_LIMIT
LAST_DAY = dt.date(9999, 12, 31) - 2 * FUTURE_LIMIT
# The dates an email gives that a seed moves (`DateShift.extraction`), in a booking and in a leg.
_BOOKING_DATES = ("check_in", "check_out", "pickup_time", "dropoff_time")
_LEG_DATES = ("departure_local", "arrival_local")


class DemoError(ValueError):
    """Why a demo cannot be seeded; raised before anything is written."""


@dataclass(frozen=True)
class Seeded:
    """A seed: how many emails were filed (ended processed or ignored) into how many trips holding
    how many bookings, how many bookings are in no trip (no date yet), every entry that ended
    otherwise (`<entry>: <status>`), and the ledger."""

    emails: int
    trips: int
    bookings: int
    unsorted: int
    failed: tuple[str, ...]
    ledger: Path


def _given_days(entry: corpus.Entry) -> Iterator[dt.date]:
    """The day the entry's email arrives and every day its truth gives that a seed moves."""
    yield corpus.sent(entry).date()
    for res in entry.truth["reservations"]:
        values = [res.get(f) for f in _BOOKING_DATES]
        values += [leg.get(f) for leg in res.get("segments") or [] for f in _LEG_DATES]
        for value in values:
            for reading in value if isinstance(value, list) else [value]:
                when = parse_local(reading) if isinstance(reading, str) else None
                if when is not None:
                    yield when.date()


def today_range(c: corpus.Corpus) -> tuple[int, int]:
    """The first and the last today, as proleptic ordinals (`date.toordinal`), that a seed of the
    corpus's demo accepts (`c` names demo entries and an anchor): moved by the days from the
    anchor to that today, each demo email's arrival and every date its truth gives stays between
    FIRST_DAY and LAST_DAY. The first is above the last when no today fits."""
    days = [d.toordinal() for name in c.demo for d in _given_days(c.entry(name))]
    anchor = c.anchor.toordinal()
    return anchor + FIRST_DAY.toordinal() - min(days), anchor + LAST_DAY.toordinal() - max(days)


def refusal(out: Path) -> str | None:
    """Why a seed refuses `out` before writing anything — it already holds a ledger, or it is a
    directory that is not empty — or None (`out` is missing, an empty directory, or something
    the seed then cannot write)."""
    out = Path(out).absolute()
    if os.path.lexists(out / "data" / "ledger.db"):
        return f"{out} already holds a ledger"
    try:
        if out.is_dir() and any(out.iterdir()):
            return f"{out} is not empty"
    except OSError:
        return None  # a directory that cannot be listed: the writes find out what it allows
    return None


def seed(c: corpus.Corpus, out: Path, today: dt.date) -> Seeded:
    """File the corpus's demo entries, in order, through the pipeline into a new ledger in
    `out/data` (the emails go to `out/archive`, a copy of the corpus's travellers to
    `out/config`), under the corpus's own settings with its answers standing in for the model: no
    network, no model. Every date an email gives moves by the days from the
    corpus's anchor to `today`, and each email is filed with the clock held at its own Date moved
    the same way. Before anything is written it refuses (DemoError) a corpus without demo entries
    or an anchor, a `today` outside `today_range`, and an `out` that `refusal` names. An entry
    that ends other than processed or ignored is listed in `failed` and the run goes on; an
    OSError (the output cannot be written) stops it. The ledger is left as one plain file, its
    write-ahead log folded back."""
    out = Path(out).absolute()
    if not c.demo:
        raise DemoError(f"{c.root}: the corpus names no demo entries")
    if c.anchor is None:
        raise DemoError(f"{c.root}: the corpus names no demo anchor")
    first, last = today_range(c)
    if not first <= today.toordinal() <= last:
        raise DemoError("--today is out of range")
    if (why := refusal(out)) is not None:
        raise DemoError(why)
    dirs = {"data_dir": out / "data", "archive_dir": out / "archive", "logs_dir": out / "logs"}
    ledger_path = dirs["data_dir"] / "ledger.db"
    corpus.settings(c, **dirs)  # the corpus's own settings must hold before anything is written
    config = out / corpus.CONFIG_DIR
    config.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(c.root / corpus.TRAVELLERS, out / corpus.TRAVELLERS)
    s = corpus.settings(c, **dirs, config_dir=config)
    shift = DateShift((today - c.anchor).days)
    filed = 0
    failed: list[str] = []
    with corpus.process_as(s):
        ctx = build_context(s, with_model=False, with_notifier=False)
        ledger = ctx.ledger
        try:
            ctx.model = corpus.CorpusAnswers(c, s.llm_model)
            ctx.shift = shift
            for name in c.demo:
                entry = c.entry(name)
                arrival = corpus.sent(entry) + dt.timedelta(days=shift.days)
                try:
                    with clock_at(arrival):
                        res = process_raw(
                            ctx, entry.raw, source="demo", received=arrival, notify=False
                        )
                    status = res.status
                except OSError:
                    raise  # the output, not the email: nothing later can be written either
                except Exception:  # noqa: BLE001 - as in a poll: this email failed, the run goes on
                    log.exception("demo entry %s: unhandled error", name)
                    status = "failed"
                if status in FILED:
                    filed += 1
                else:
                    failed.append(f"{name}: {status}")
            trips = ctx.trips.all_trips()
            bookings = sum(len(ctx.trips.items(t.id)) for t in trips)
            unsorted = len(ctx.trips.unsorted())
            ledger.conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            ledger.close()
    return Seeded(filed, len(trips), bookings, unsorted, tuple(failed), ledger_path)


def _read_only(ledger: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(Path(ledger).absolute()))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def dump(ledger: Path, archive_dir: Path) -> str:
    """The whole ledger as text, to compare two by: for each table in name order a line
    `# <table>`, then one line per row in rowid order, the row as JSON with sorted keys, where a
    column named `*_json` holds the JSON it stores and a message's archive path is written
    relative to `archive_dir`. A test helper: no command calls it."""
    archive = Path(archive_dir).absolute()
    conn = _read_only(ledger)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite!_%' ESCAPE '!' ORDER BY name"
            )
        ]
        lines: list[str] = []
        for table in tables:
            lines.append(f"# {table}")
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                doc = {}
                for column in row.keys():
                    value = row[column]
                    if value is not None and column.endswith("_json"):
                        value = json.loads(value)
                    elif value is not None and (table, column) == ("messages", "archive_path"):
                        value = Path(value).relative_to(archive).as_posix()
                    doc[column] = value
                lines.append(json.dumps(doc, sort_keys=True, ensure_ascii=False))
    finally:
        conn.close()
    return "\n".join(lines) + "\n"


def _days(value: str, today: dt.date) -> int:
    return (dt.date.fromisoformat(value[:10]) - today).days


def _clock(value: str | None) -> str | None:
    return value[11:16] if value and len(value) > 10 else None


def _what(item: TripItem) -> str | None:
    p = item.payload
    if item.kind == "flight":
        return f"{p.get('flight_code')} {p.get('origin')}-{p.get('destination')}"
    if item.kind == "train":
        return f"{p.get('train_number')} {p.get('origin')} - {p.get('destination')}"
    if item.kind == "lodging":
        return p.get("property_name")
    return p.get("provider")  # a car: the company


def _who(item: TripItem, book: PassengerBook) -> list[str]:
    return sorted({book.match(n).canonical for n in item.passengers})


def relative_view(ledger: Path, today: dt.date, book: PassengerBook) -> dict:
    """The trips the page lists, told in days from `today`: by start date then id, each with its
    title, first and last day, whether it is cancelled and its bookings in the page's order
    (`grouping.sort_key`), each booking with its kind, status, first and last day, local start
    and end time (`HH:MM`, or null for a day alone), what it is, who is on it by their usual
    names, sorted, and its open checks as the ledger holds them (`checks.ALL`'s order). Then the
    bookings in no trip (no date yet), oldest first, each with its kind, status, what it is, who
    is on it and its open checks. A test helper: no command calls it."""
    conn = _read_only(ledger)
    try:
        store = TripStore(Ledger(conn), ())
        trips = sorted(store.all_trips(), key=lambda t: (t.start_date, t.id))
        return {
            "trips": [
                {
                    "title": t.title,
                    "from": _days(t.start_date, today),
                    "to": _days(t.end_date, today),
                    "cancelled": t.cancelled,
                    "items": [
                        {
                            "kind": i.kind,
                            "status": i.status,
                            "from": _days(i.start_at, today),
                            "at": _clock(i.start_at),
                            "to": _days(i.end_at, today) if i.end_at else None,
                            "until": _clock(i.end_at),
                            "what": _what(i),
                            "who": _who(i, book),
                            "checks": list(i.checks),
                        }
                        for i in store.items(t.id)
                    ],
                }
                for t in trips
            ],
            "unsorted": [
                {
                    "kind": i.kind,
                    "status": i.status,
                    "what": _what(i),
                    "who": _who(i, book),
                    "checks": list(i.checks),
                }
                for i in store.unsorted()
            ],
        }
    finally:
        conn.close()
