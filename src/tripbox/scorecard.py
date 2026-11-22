# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The two comparisons a corpus run is scored by: what was read from an email against what the
email says (`score_extraction`, field by field), and what filing the email did against what it is
meant to do (`outcome_view` turns a finished pipeline result into the corpus's outcome shape,
`compare_outcome` lists the differences). Pure comparison: nothing here reaches a network or a
model."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.parse.provider import READ_BY_MODEL
from tripbox.pipeline.run import Context, MessageResult
from tripbox.trips.grouping import local_str
from tripbox.validate.passengers import first_last

SCORED: dict[str, tuple[str, ...]] = {
    "booking": ("kind", "status", "confirmation_code", "passengers"),
    "flight": (
        "carrier_iata",
        "flight_number",
        "origin",
        "destination",
        "departure_local",
        "arrival_local",
    ),
    "train": ("train_number", "origin", "destination", "departure_local", "arrival_local"),
    "lodging": ("property_name", "check_in", "check_out"),
    "car": ("provider", "pickup_location", "pickup_time", "dropoff_time"),
}
LEG_KINDS = ("flight", "train")


@dataclass(frozen=True)
class FieldResult:
    """One compared unit: where it is, what the email says, what was read, and whether they
    agree."""

    path: str
    want: object
    got: object
    right: bool


# ----------------------------------------------------------------------------- reductions
def _fold(value: str) -> str:
    """Lower-case letters and digits after folding accents and compatibility forms."""
    text = unicodedata.normalize("NFKD", value)
    return "".join(c for c in text.casefold() if c.isalnum() and not unicodedata.combining(c))


def _upper(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _number(value: str) -> object:
    text = value.strip()
    return int(text) if text.isascii() and text.isdecimal() else text


def _local(value: str) -> str | None:
    return local_str(value)


def _day(value: str) -> str | None:
    text = local_str(value)
    return text[:10] if text else None


def _words(value: str) -> str:
    return value.strip()


def _status(value: str) -> str | None:
    """A status the email does not state and the pipeline's own `unknown` are the same thing."""
    text = value.strip()
    return None if text == "unknown" else text


def _reducer(unit: str, field: str) -> Callable[[str], object]:
    if field == "status":
        return _status
    if field in ("document_type", "kind"):
        return _words
    if field in ("flight_number", "train_number"):
        return _number
    if field in ("departure_local", "arrival_local", "pickup_time", "dropoff_time"):
        return _local
    if field in ("check_in", "check_out"):
        return _day
    if field == "confirmation_code" or unit == "flight":
        return _upper
    return _fold


def _reduce(reduce: Callable[[str], object], value: Any) -> object:
    """The comparable form of one read or true value; None stays None. A string that does not
    reduce (no digits where a number belongs, a time that does not parse) is kept as it is, so
    it never equals a value that did."""
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        return None
    out = reduce(text)
    if out is None and reduce is _status:
        return None
    return ("raw", text) if out in (None, "") else out


def _people(names: Any) -> frozenset | None:
    # a name that cannot be split into first and last is kept raw: it equals only itself
    pairs = {first_last(str(n)) or ("raw", str(n).strip().upper()) for n in names or ()}
    return frozenset(pairs) or None


def _right(unit: str, field: str, want: Any, got: Any) -> bool:
    if field == "passengers":
        return _people(want) == _people(got)
    reduce = _reducer(unit, field)
    wants = want if isinstance(want, list) else [want]
    seen = _reduce(reduce, got)
    return any(_reduce(reduce, w) == seen for w in wants)


# ----------------------------------------------------------------------------- extraction
def _units(
    path: str, unit: str, truth: dict, read: Reservation | Segment | None
) -> list[FieldResult]:
    out = []
    for field in SCORED[unit]:
        want = truth.get(field)
        got = None if read is None else getattr(read, field, None)
        if field == "passengers" and read is not None:
            got = list(got or [])
        right = read is not None and _right(unit, field, want, got)
        out.append(FieldResult(f"{path}.{field}", want, got, right))
    return out


def _view(read: Reservation | Segment, unit: str, extra: tuple[str, ...] = ()) -> dict:
    fields = SCORED[unit] + extra
    return {f: v for f in fields if (v := getattr(read, f, None)) not in (None, [], "")}


def _same_code(truth_res: dict, read: Reservation) -> bool:
    return _right("booking", "confirmation_code", truth_res.get("confirmation_code"),
                  read.confirmation_code)


def _pair(truths: list[dict], pool: list[Reservation]) -> dict[int, int]:
    """Truth booking index -> read booking index. Two passes: every truth booking that has a
    code takes the unmatched read booking of its kind with the same code; each one left takes the
    first unmatched read booking of its kind. Two code-less bookings of one kind read in the other
    order may pair crosswise."""
    pairs: dict[int, int] = {}
    taken: set[int] = set()
    for strict in (True, False):
        for i, want in enumerate(truths):
            if i in pairs or (strict and not want.get("confirmation_code")):
                continue
            for j, read in enumerate(pool):
                if j in taken or read.kind != want.get("kind"):
                    continue
                if strict and not _same_code(want, read):
                    continue
                pairs[i] = j
                taken.add(j)
                break
    return pairs


def score_extraction(truth: dict, got: Extraction | None) -> tuple[FieldResult, ...]:
    """One result per compared unit, in document order. A booking or a leg the truth lacks is one
    wrong unit; one the read lacks makes each of its units wrong. Nothing read is credited when
    nothing was read."""
    pool = list(got.reservations) if got is not None else []
    want_type = truth.get("document_type")
    got_type = got.document_type if got is not None else None
    results = [
        FieldResult(
            "document_type",
            want_type,
            got_type,
            got is not None and _right("booking", "document_type", want_type, got_type),
        )
    ]
    truths = truth.get("reservations") or []
    pairs = _pair(truths, pool)
    taken = set(pairs.values())
    for i, want in enumerate(truths):
        at = f"reservations.{i}"
        read = pool[pairs[i]] if i in pairs else None
        results += _units(at, "booking", want, read)
        kind = want.get("kind")
        if kind in SCORED and kind not in LEG_KINDS:
            results += _units(at, kind, want, read)
        elif kind in LEG_KINDS:
            legs = want.get("segments") or []
            read_legs = list(read.segments) if read is not None else []
            for k, leg in enumerate(legs):
                leg_read = read_legs[k] if k < len(read_legs) else None
                results += _units(f"{at}.segments.{k}", kind, leg, leg_read)
            for k, extra in enumerate(read_legs[len(legs) :]):
                results.append(
                    FieldResult(f"{at}.segments.+{k}", None, _view(extra, kind), False)
                )
    extras = [r for i, r in enumerate(pool) if i not in taken and r.kind != "other"]
    for k, extra in enumerate(extras):
        results.append(FieldResult(f"reservations.+{k}", None, _view(extra, "booking"), False))
    return tuple(results)


# ----------------------------------------------------------------------------- outcome
_READ_BY = {"kitinerary": "kitinerary", READ_BY_MODEL: "model"}


def outcome_view(result: MessageResult, ctx: Context) -> dict:
    """What filing the message did, in the corpus's outcome shape (without `gap`): its status,
    who read it, the document type, and per booking what became of it, for how many people, and
    the booking as it now stands in the ledger, its open checks and its sync state included."""
    parsed = result.parsed
    items = []
    for o in result.outcomes:
        item = o.action.item
        cls = item.classified
        stored = ctx.trips.item_by_key(item.key)
        booking = None
        if stored is not None:
            # a booking with no date is in no trip
            trip = ctx.trips.get(stored.trip_id) if stored.trip_id is not None else None
            booking = {
                "status": stored.status,
                "travellers": sorted({ctx.book.match(n).canonical for n in stored.passengers}),
                "trip": trip.title if trip else None,
                "checks": list(stored.checks),
                "sync": stored.sync_state,
            }
        items.append(
            {
                "kind": item.kind,
                "key": item.key,
                "result": o.result,
                "people": {
                    "me": int(cls.me_present),
                    "guests": len(cls.guests),
                    "others": len(cls.others),
                },
                "booking": booking,
            }
        )
    items.sort(key=lambda i: (i["kind"], i["key"]))
    return {
        "status": result.status,
        "read_by": _READ_BY.get(parsed.parser) if parsed else None,
        "document_type": parsed.extraction.document_type if parsed else None,
        "items": items,
    }


def compare_outcome(want: dict, got: dict) -> tuple[str, ...]:
    """One line per differing path, such as `items.0.key: want 'A', got 'B'`; a path on one side
    only reads `want <absent>` or `got <absent>`. A `gap` in `want` is not compared. Equal views
    give ()."""
    lines: list[str] = []

    def walk(path: str, a: Any, b: Any) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            keys = [*a, *(k for k in b if k not in a)]
            pairs = [(str(k), k in a, a.get(k), k in b, b.get(k)) for k in keys]
        elif isinstance(a, list) and isinstance(b, list):
            n = max(len(a), len(b))
            pairs = [
                (str(k), k < len(a), a[k] if k < len(a) else None,
                 k < len(b), b[k] if k < len(b) else None)
                for k in range(n)
            ]
        else:
            if a != b:
                lines.append(f"{path}: want {a!r}, got {b!r}")
            return
        for name, in_a, va, in_b, vb in pairs:
            at = f"{path}.{name}" if path else name
            if in_a and in_b:
                walk(at, va, vb)
            elif in_a:
                lines.append(f"{at}: want {va!r}, got <absent>")
            else:
                lines.append(f"{at}: want <absent>, got {vb!r}")

    walk("", {k: v for k, v in want.items() if k != "gap"}, got)
    return tuple(lines)
