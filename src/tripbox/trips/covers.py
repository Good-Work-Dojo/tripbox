# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The rules for a trip's cover photo, over the ledger's `covers` table. A trip has at most one
cover row. An owner's upload (`source='owner'`) beats a Wikimedia photo; `removed` records that
the owner took the photo away, which stops automatic lookups until the place changes. Each
writing method runs in one ledger transaction of its own (joining one that is open). The worker
reads a photo outside any: its answer is written only while the trip still waits for those
bytes (`Pending`), so what the owner did meanwhile stands. This module decodes and fetches
nothing, so the web container can import it."""

from __future__ import annotations

import datetime as dt
import functools
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from tripbox.ledger import Ledger


@dataclass(frozen=True)
class Cover:
    trip_id: int
    source: str  # owner | wikimedia | removed
    state: str  # pending | ready | none | failed
    place: str | None
    digest: str | None
    credit: dict[str, Any] | None
    error: str | None


class Pending(NamedTuple):
    """An owner's photo waiting for the worker: its trip and its bytes as they came. The bytes
    are the token its answer is written under (`finish_upload`, `fail_upload`): the same bytes
    sent again make the same cover."""

    trip_id: int
    data: bytes


def fold(place: str | None) -> str:
    """A place with case and spacing folded, for comparing two spellings."""
    return " ".join((place or "").split()).casefold()


def _cover(row: sqlite3.Row) -> Cover:
    raw = row["credit_json"]
    return Cover(
        trip_id=row["trip_id"],
        source=row["source"],
        state=row["state"],
        place=row["place"],
        digest=row["digest"],
        credit=json.loads(raw) if raw else None,
        error=row["error"],
    )


def _is_cover(row: sqlite3.Row) -> bool:
    """Whether a row gives its trip a cover: an owner's photo, whatever its state, or a ready
    one."""
    return row["source"] == "owner" or row["state"] == "ready"


def _iso(when: dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


_CLEARED: dict[str, Any] = {
    "digest": None,
    "large": None,
    "small": None,
    "upload": None,
    "credit_json": None,
    "error": None,
    "attempts": 0,
    "retry_after": None,
}


def _writes[F: Callable[..., Any]](fn: F) -> F:
    """The method runs in one ledger transaction (joining an open one), so its check and its
    write cannot be split by another process's commit."""

    @functools.wraps(fn)
    def run(self: CoverStore, *a: Any, **kw: Any) -> Any:
        with self.led.transaction():
            return fn(self, *a, **kw)

    return run  # type: ignore[return-value]


class CoverStore:
    def __init__(self, ledger: Ledger) -> None:
        self.led = ledger
        self.repo = ledger.covers

    # ------------------------------------------------------------------ reading
    def get(self, trip_id: int) -> Cover | None:
        """The trip's cover, read without its images."""
        row = self.repo.about(trip_id)
        return _cover(row) if row else None

    def all(self) -> dict[int, Cover]:
        """Every trip's cover by trip, read in one pass without the images (the list's cards)."""
        return {row["trip_id"]: _cover(row) for row in self.repo.all_for_lookup()}

    def image(self, digest: str, size: Literal["large", "small"]) -> bytes | None:
        row = self.repo.by_digest(digest)
        if row is None or row["state"] != "ready":
            return None
        return row[size]

    def shown_image(self, digest: str, size: Literal["large", "small"]) -> bytes | None:
        """The image the page serves under `digest`: a ready cover's, of a trip that is not
        deleted (Recently deleted keeps tiles), else None."""
        return self.repo.shown_image(digest, size)

    def same_place_ready(self, place: str, exclude: int | None = None) -> Cover | None:
        """A ready Wikimedia cover of another trip with the same place (the lowest trip), so a
        second trip there reuses the picture instead of asking again."""
        want = fold(place)
        if not want:
            return None
        for row in self.repo.all_for_lookup():
            if (
                row["trip_id"] != exclude
                and row["source"] == "wikimedia"
                and row["state"] == "ready"
                and fold(row["place"]) == want
            ):
                return _cover(row)
        return None

    # ------------------------------------------------------------------ the owner's photo
    @_writes
    def upload(self, trip_id: int, data: bytes) -> Pending:
        """An owner row waiting for the worker, replacing whatever the trip had."""
        self.repo.put(trip_id, source="owner", state="pending", place=None, **_CLEARED)
        self.repo.put(trip_id, upload=data)
        return Pending(trip_id, data)

    @_writes
    def remove(self, trip_id: int) -> None:
        self.repo.put(trip_id, source="removed", state="none", place=None, **_CLEARED)

    def next_upload(self) -> Pending | None:
        """The oldest owner's photo waiting for the worker."""
        row = self.repo.pending_upload()
        return Pending(row["trip_id"], row["upload"]) if row and row["upload"] else None

    @_writes
    def finish_upload(self, taken: Pending, large: bytes, small: bytes, digest: str) -> bool:
        """The photo `taken` made into its cover, if its trip still waits for it (whether it
        did)."""
        return self.repo.settle_upload(
            taken.trip_id, taken.data, state="ready", large=large, small=small, digest=digest,
            upload=None, error=None,
        )

    @_writes
    def fail_upload(self, taken: Pending, code: str) -> bool:
        """The photo `taken` could not be used (`code`), if its trip still waits for it
        (whether it did)."""
        return self.repo.settle_upload(
            taken.trip_id, taken.data, state="failed", error=code, upload=None
        )

    # ------------------------------------------------------------------ trip changes
    @_writes
    def place_changed(self, trip_id: int, place: str | None) -> None:
        """A new place: an automatic cover (or a removal) goes, so the place is looked up
        afresh. The owner's photo stays."""
        row = self.repo.get(trip_id)
        if row is not None and row["source"] in ("wikimedia", "removed"):
            self.repo.delete(trip_id)

    @_writes
    def look_again(self, trip_id: int) -> None:
        """The place saved again while automatic covers are on (Save place finds a photo):
        nothing found, a failed lookup's backoff or a removal goes, so the worker looks the
        place up afresh. A ready photo and the owner's stay."""
        row = self.repo.get(trip_id)
        if row is not None and not _is_cover(row):
            self.repo.delete(trip_id)

    @_writes
    def merged(self, src_trip_id: int, dst_trip_id: int) -> None:
        """Called before the source trip goes. Only an owner's photo (in any state) or a ready
        one is a trip's cover: the destination keeps its own, else takes the source's, else has
        none and is looked up for its own place. An owner's photo on the source also replaces
        an automatic one on the destination. The source's row never stays, nor moves when it is
        no cover (nothing found, a failed lookup, a removal: all of its own place)."""
        src = self.repo.get(src_trip_id)
        if src is None:
            return
        dst = self.repo.get(dst_trip_id)
        takes = _is_cover(src) and (
            dst is None
            or not _is_cover(dst)
            or (src["source"] == "owner" and dst["source"] != "owner")
        )
        if not takes:
            self.repo.delete(src_trip_id)
            return
        if dst is not None:
            self.repo.delete(dst_trip_id)
        self.repo.move(src_trip_id, dst_trip_id)

    # ------------------------------------------------------------------ Wikimedia lookups
    def due_lookups(
        self, trips: list[tuple[int, str]], now: dt.datetime, limit: int
    ) -> list[tuple[int, str]]:
        """Of (trip id, place) pairs the caller gives (visible trips), up to `limit` that need a
        lookup: no row, or a Wikimedia row in none/failed whose retry_after has passed. Trips
        with a blank place, and deleted ones, are skipped."""
        rows = {r["trip_id"]: r for r in self.repo.all_for_lookup()}
        out: list[tuple[int, str]] = []
        for trip_id, place in trips:
            if len(out) >= limit:
                break
            if not fold(place):
                continue
            row = rows.get(trip_id)
            if row is not None:
                if row["source"] != "wikimedia" or row["state"] not in ("none", "failed"):
                    continue
                after = row["retry_after"]
                if after and dt.datetime.fromisoformat(after) > now:
                    continue
            trip = self.led.trips.get(trip_id)
            if trip is None or trip["deleted_at"]:
                continue
            out.append((trip_id, place))
        return out

    def _open_for_lookup(self, trip_id: int, place: str) -> bool:
        """Whether an answer for this place may be stored: the trip still exists with that
        place, and the owner has not set or removed a photo meanwhile."""
        trip = self.led.trips.get(trip_id)
        if trip is None or fold(trip["place"]) != fold(place):
            return False
        row = self.repo.get(trip_id)
        return row is None or row["source"] == "wikimedia"

    @_writes
    def found(
        self,
        trip_id: int,
        place: str,
        large: bytes,
        small: bytes,
        digest: str,
        credit: dict[str, Any],
    ) -> None:
        if not self._open_for_lookup(trip_id, place):
            return
        self.repo.put(
            trip_id, source="wikimedia", state="ready", place=place, **_CLEARED,
        )
        self.repo.put(
            trip_id, large=large, small=small, digest=digest,
            credit_json=json.dumps(credit, sort_keys=True),
        )

    @_writes
    def none_found(self, trip_id: int, place: str, until: dt.datetime) -> None:
        if not self._open_for_lookup(trip_id, place):
            return
        self.repo.put(
            trip_id, source="wikimedia", state="none", place=place,
            **{**_CLEARED, "retry_after": _iso(until)},
        )

    @_writes
    def lookup_failed(
        self, trip_id: int, place: str, code: str, retry_after: dt.datetime
    ) -> None:
        if not self._open_for_lookup(trip_id, place):
            return
        row = self.repo.get(trip_id)
        attempts = (row["attempts"] if row else 0) + 1
        self.repo.put(
            trip_id, source="wikimedia", state="failed", place=place,
            **{**_CLEARED, "error": code, "attempts": attempts,
               "retry_after": _iso(retry_after)},
        )
