# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Carry out a booking plan through the trip store: each booking of a message is filed, merged
into the booking it is, or noted as a copy, and each leaves one row in the message's log
(`items`), written once its action is done. Nothing here asks a flight log anything: the store
works out where each flight's sync stands, and a sync pass (`write.sync`) sends what is due."""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

from tripbox.ledger import Ledger
from tripbox.trips import grouping
from tripbox.trips.store import BLANK, KeyCollision, Trip, TripItem, TripStore
from tripbox.validate import checks
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rules import Item, without_days_alone
from tripbox.write.planner import SCHEDULE_FIELDS, Action, WritePlan, earlier

log = logging.getLogger("tripbox.executor")

# What became of a booking, by the kind of its action: the outcome its log row and its Outcome
# hold. Beside `updated` and `ignored` the row holds the action's word (`Action.reason`), beside
# `failed` the error's sentence.
RESULT_OF = {
    "file": "filed",
    "update": "updated",
    "duplicate": "duplicate",
    "ignore": "ignored",
}
# What belongs to the booking and to no one email: a merge leaves each as the booking holds it.
# (`raised` is the booking's too, and is joined stamp by stamp.)
KEPT = (
    "looked_at",
    "owner_edits",
    "owner_answer",
    "taken_off",
    "cancelled_names",
    "alt_keys",
    "change_recorded",
)
KEY_TAKEN = "another booking is already filed under the key these details make"


@dataclass
class Outcome:
    action: Action
    result: str  # filed | updated | duplicate | ignored | failed
    summary: str
    item_id: int | None = None  # this message's row in items
    trip_item_id: int | None = None
    trip_id: int | None = None
    trip_title: str | None = None
    sync: str | None = None  # the booking's sync state; read again after the message's pass
    checks: tuple[str, ...] = ()  # the checks open on the booking
    error: str | None = None


class Executor:
    def __init__(self, *, ledger: Ledger, store: TripStore, book: PassengerBook) -> None:
        self.ledger = ledger
        self.store = store
        self.book = book
        self._plan_trip_id: int | None = None
        self._plan_span: tuple | None = None

    def execute(self, plan: WritePlan, *, message_row_id: int) -> list[Outcome]:
        """Apply each action in order; one that fails is that booking's `failed` outcome and the
        rest go on. Each action leaves one row in the message's log once it is done, holding
        what became of the booking and the booking it touched; a run that stops between an
        action and its row leaves none for that booking, and the message, not finished, is read
        again."""
        outcomes: list[Outcome] = []
        self._plan_trip_id = None
        self._plan_span = self._span(plan)
        for n, a in enumerate(plan.actions):
            try:
                outcome = self._apply(a, message_row_id)
            except KeyCollision:
                log.warning("%s of %s: its key is another booking's", a.kind, a.item.key)
                outcome = self._failed(a, KEY_TAKEN)
            except Exception as exc:  # noqa: BLE001 - one bad booking must not sink the rest
                log.exception("%s failed for %s", a.kind, a.item.key)
                outcome = self._failed(a, str(exc))
            outcomes.append(self._logged(outcome, message_row_id, n))
        return outcomes

    # ------------------------------------------------------------------ actions
    def _apply(self, a: Action, message_row_id: int) -> Outcome:
        if a.kind == "file":
            trip = self._trip_for(a.item)
            ti = self.store.upsert_item(trip, a.item, source_message_id=message_row_id)
            return self._done(a, ti, f"filed {a.item.kind}")
        row = self.store.item(a.trip_item_id) if a.trip_item_id is not None else None
        if row is None:
            raise LookupError("the booking this email is for is no longer there")
        if a.kind == "update":
            ti = self._update(a, row, message_row_id)
            return self._done(a, ti, f"{a.reason} {a.item.kind}")
        if a.reason == checks.UNKNOWN_CANCELLATION:
            # the booking is left as it is, marked with the doubt the email raised
            stamp = (a.item.payload.get("raised") or {}).get(checks.UNKNOWN_CANCELLATION)
            row = self.store.mark(row.id, {checks.UNKNOWN_CANCELLATION: stamp})
        summary = "already recorded" if a.kind == "duplicate" else a.reason
        return self._done(a, row, summary)

    def _update(self, a: Action, row: TripItem, message_row_id: int) -> TripItem:
        """The booking `row` after the email's update. A cancellation, whole or of some of its
        travellers, changes nothing else of the booking; anything else is merged into it under
        the key it has, and the store's settle gives a moved flight its new key."""
        raised = a.item.payload.get("raised") or None
        source = message_row_id
        if a.reason == "cancel":
            when = a.item.payload.get("email_date")
            ti = self.store.cancel(
                row.item_key, email_date=when, raised=raised, source_message_id=source
            )
        elif a.reason == "remove_passengers":
            ti = self.store.remove_passengers(
                row.item_key,
                a.remove_passengers,
                email_date=a.item.payload.get("email_date"),
                raised=raised,
                source_message_id=source,
            )
        else:
            onto = dataclasses.replace(a.item, key=row.item_key, payload=self._said(a, row))
            ti = self.store.upsert_item(self._shared_trip(), onto, source_message_id=source)
        if ti is None:
            raise LookupError("the booking this email is for is no longer there")
        return ti

    @staticmethod
    def _said(a: Action, row: TripItem) -> dict:
        """What the email says, as it is merged into the booking: without what belongs to the
        booking alone (KEPT), with its stamps joined to the booking's (the later time of each),
        and, unless the action carries a change, without the schedule the booking already holds
        (SCHEDULE_FIELDS), so no copy rewinds it. A change, and a move to another key, is
        recorded as one; completing a draft is not (it is the booking's first whole reading, and
        it keeps whatever it had). Filling in a booking typed in by hand (`fills_in`) takes only
        the fields it holds nothing for, and joins the email's stamps as any update does. The
        booking's `email_date` stays the latest of the
        emails merged into it: an older copy that adds a traveller must not make a later copy
        pass for newer."""
        old = row.payload
        said = {k: v for k, v in without_days_alone(old, a.item.payload).items() if k not in KEPT}
        if earlier(said.get("email_date"), old.get("email_date")):
            del said["email_date"]
        if (a.changes and a.reason != "completes_draft") or a.reason == "rescheduled":
            said["change_recorded"] = True
        if not a.changes:
            said = {k: v for k, v in said.items() if not (k in SCHEDULE_FIELDS and k in old)}
        if a.reason == "fills_in":
            # only what the booking lacks: a copy older than what filled it in never rewrites a
            # value a newer email gave (a cabin or a seat the flight log is told); the email's
            # soft stamps still join the booking's below, so a doubt it raises holds it back
            said = {
                k: v
                for k, v in said.items()
                if k in ("email_date", "raised") or old.get(k) in BLANK
            }
        joined = {"raised": dict(old.get("raised") or {})}
        for code, at in (said.pop("raised", None) or {}).items():
            checks.raise_soft(joined, [code], at)
        if joined["raised"]:
            said["raised"] = joined["raised"]
        return said

    # ------------------------------------------------------------------ trips
    @staticmethod
    def _span(plan: WritePlan):
        dates = [grouping.item_dates(a.item) for a in plan.actions if a.kind == "file"]
        dates = [d for d in dates if d]
        if not dates:
            return None
        return min(d[0] for d in dates), max(d[1] for d in dates)

    def _shared_trip(self) -> Trip | None:
        """The trip this message's bookings share so far, if any."""
        return self.store.get(self._plan_trip_id) if self._plan_trip_id is not None else None

    def _trip_for(self, item: Item) -> Trip | None:
        """The trip a newly filed booking goes into, so that one email lands in one trip: the one
        this message's bookings share, else one that holds its confirmation code, else the one
        the date and place rules pick for the span of everything the message files. None for a
        booking with no date: it is in no trip."""
        if grouping.item_dates(item) is None:
            return None
        preferred = self._plan_trip_id
        if preferred is None and item.pnr:
            preferred = self.store.trip_for_pnr(item.pnr)
        trip = self.store.find_or_create(item, span=self._plan_span, preferred_id=preferred)
        self._plan_trip_id = trip.id
        return trip

    # ------------------------------------------------------------------ outcomes
    def _done(self, a: Action, ti: TripItem, summary: str) -> Outcome:
        """The action's outcome, pointing at the booking as it now stands. A deleted trip is
        never linked (the page would not show it); a booking written into one still names it."""
        trip = self.ledger.trips.get(ti.trip_id) if ti.trip_id is not None else None
        live = trip is not None and not trip["deleted_at"]
        named = trip is not None and (live or a.kind in ("file", "update"))
        return Outcome(
            a,
            RESULT_OF[a.kind],
            summary,
            trip_item_id=ti.id,
            trip_id=ti.trip_id if live else None,
            trip_title=trip["title"] if named else None,
            sync=ti.sync_state,
            checks=ti.checks,
        )

    @staticmethod
    def _failed(a: Action, error: str) -> Outcome:
        return Outcome(a, "failed", f"{a.kind} failed: {error}", error=error)

    def _logged(self, o: Outcome, message_row_id: int, n: int) -> Outcome:
        """`o` with its row in the message's log, written now that its action is done: the
        booking's key as it stands once the message is filed, what the email said, and the
        word beside the outcome."""
        a = o.action
        ti = self.store.item(o.trip_item_id) if o.trip_item_id is not None else None
        detail = o.error if o.result == "failed" else None
        if o.result in ("updated", "ignored"):
            detail = a.reason
        o.item_id = self.ledger.items.add(
            message_id=message_row_id,
            n=n,
            kind=a.item.kind,
            item_key=ti.item_key if ti is not None else a.item.key,
            outcome=o.result,
            payload=a.item.payload,
            detail=detail,
            confidence=a.item.confidence,
            trip_item_id=o.trip_item_id,
        )
        return o
