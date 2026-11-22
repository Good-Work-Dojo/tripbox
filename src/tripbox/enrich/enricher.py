# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Nightly enrichment: registration, aircraft, actual times and gates for flights that landed,
asked of the flight-data service and put on each flight's record in AirTrail by the syncer
(`Syncer.fill`), which writes only blanks, only on a record that is the owner's, and only while
the ledger is bound to the key's user. Nothing here reads or writes a flight's record itself."""

from __future__ import annotations

import datetime as dt
import logging
import re

from tripbox.enrich.aeroapi import (
    EST_COST_PER_CALL,
    AeroAPI,
    AeroAPIError,
    fields_from,
    pick_flight,
)
from tripbox.pipeline.run import Context
from tripbox.trips.store import TripItem
from tripbox.util import month_key, now, parse_iso
from tripbox.write.airtrail import AirTrailError, AirTrailRefs

log = logging.getLogger("tripbox.enrich")

# 4, not 3: the run is nightly, so a flight that lands less than MIN_AGE before 03:30 is first
# seen the next night; with a 3-day window it got only two of its MAX_ATTEMPTS.
WINDOW_DAYS = 4
MIN_AGE = dt.timedelta(hours=2)
MAX_ATTEMPTS = 3
# The title a channel that shows one gives enrichment's notice.
TITLE = "tripbox: flight details filled in"


def candidates(ctx: Context, at: dt.datetime) -> list[TripItem]:
    """The flights to look up at `at`: each one AirTrail holds (`TripStore.sent_flights`) as its
    trip does — `synced`: told what the booking wants, the owner on it, no check open, not held
    out of the reconcile — that is not cancelled, landed between WINDOW_DAYS and MIN_AGE before
    `at`, and is neither enriched nor out of attempts. Enrichment writes into the flight's record
    there, so a flight AirTrail does not hold as the trip does is never looked up: one edited on
    the page waits until AirTrail has been told the edit, and is then looked up under its number
    and day as they stand. With no flight log every flight is `off`, so none is. The
    `enrichment` and `aeroapi_calls` rows are keyed by the trip item's id."""
    out = []
    for ti in ctx.trips.sent_flights():
        if ti.sync_state != "synced" or ti.status == "cancelled":
            continue
        p = ti.payload
        arr = p.get("arr_utc") or p.get("dep_utc")
        if not arr:
            continue
        when = parse_iso(arr)
        if not (at - dt.timedelta(days=WINDOW_DAYS) <= when <= at - MIN_AGE):
            continue
        e = ctx.ledger.enrichment.get(ti.id)
        if e is not None and (e["enriched_at"] or e["attempts"] >= MAX_ATTEMPTS):
            continue
        out.append(ti)
    return out


def ident_for(payload: dict, refs: AirTrailRefs | None = None) -> str:
    """The designator AeroAPI is asked by: the airline's ICAO code, from the payload (a
    backfilled flight carries it) or else AirTrail's airline record, then the flight number.
    Without either, or when the lookup fails (logged), the IATA carrier stands in for it."""
    carrier = str(payload.get("carrier") or "")
    code = payload.get("airline_icao")
    if not code and carrier and refs is not None:
        try:
            code = refs.airline_icao(carrier)
        except Exception as exc:  # noqa: BLE001 - enrichment never fails on the designator
            log.warning("airline lookup %s failed, asking by its IATA code: %s", carrier, exc)
    num = re.sub(r"\D", "", str(payload.get("flight_number") or ""))
    return f"{code or carrier}{num}".upper()


def enrich_recent(ctx: Context, aero: AeroAPI, *, at: dt.datetime | None = None) -> list[str]:
    """Look up the flights that landed (`candidates`) and have the syncer fill in what their
    records there lack (`Syncer.fill`); one line per flight. A dry run lists them by their IATA
    code and asks no one. Before the service is asked (and paid) at all, the flight log must
    take writes: the key answers and the ledger is bound to its user (`Syncer.bound`)."""
    at = at or now()
    s = ctx.settings
    lines: list[str] = []
    flights = candidates(ctx, at)
    if not flights:
        return ["no flights to enrich"]
    if s.airtrail_dry_run:
        return [
            f"DRY RUN: {len(flights)} flight(s) would be looked up: "
            + ", ".join(str(ti.payload.get("flight_code") or "?") for ti in flights)
        ]
    syncer = ctx.syncer
    if syncer is None:
        return ["no flight log is kept: nothing to enrich"]
    ended = syncer.bound()
    if ended is not None:
        why = _said(ended)
        return [f"the flight log takes no writes now ({why}); {len(flights)} flight(s) wait"]
    try:
        usage = aero.usage()
        spent = float(usage.get("total_cost") or 0.0)
    except Exception as exc:  # noqa: BLE001 - a nightly best-effort job: log, try again tomorrow
        return [f"AeroAPI usage check failed, skipping: {type(exc).__name__}: {exc}"]
    n_month, est_month = ctx.ledger.aeroapi.month_total(month_key(at))
    if spent >= s.aeroapi_monthly_budget_usd or est_month >= s.aeroapi_monthly_budget_usd:
        return [
            f"AeroAPI budget reached (${spent:.2f} reported, ${est_month:.2f} estimated"
            f" of ${s.aeroapi_monthly_budget_usd:.2f}); {len(flights)} flight(s) left unenriched"
        ]
    for ti in flights[: s.aeroapi_max_calls_per_run]:
        p = ti.payload
        ident = ident_for(p, ctx.airtrail_refs)
        dep_date = dt.date.fromisoformat(p["date_local"])
        ctx.ledger.enrichment.bump(ti.id)
        attempt = ctx.ledger.enrichment.get(ti.id)["attempts"]
        head = f"{ident} {p['date_local']}"
        try:
            # the service reads a bare date as its midnight UTC and `end` as exclusive: the day
            # after the local day is the last one asked for, so an evening departure west of UTC
            # (after midnight UTC) is in the window; `pick_flight` keeps the local day's alone
            found = aero.flights(
                ident, dep_date - dt.timedelta(days=1), dep_date + dt.timedelta(days=2)
            )
        except AeroAPIError as exc:
            lines.append(_problem(head, f"AeroAPI {exc}", attempt))
            continue
        except Exception as exc:  # noqa: BLE001 - one flight's failure must not sink the rest
            log.exception("enrichment lookup failed for %s", head)
            lines.append(_problem(head, f"AeroAPI {type(exc).__name__}: {exc}", attempt))
            continue
        ctx.ledger.aeroapi.add(
            month=month_key(at),
            item_id=ti.id,
            ident=ident,
            result_sets=1,
            est_cost_usd=EST_COST_PER_CALL,
        )
        f = pick_flight(
            found,
            origin_iata=p["origin"],
            destination_iata=p.get("destination"),
            date_local=p["date_local"],
            origin_tz=p.get("origin_tz"),
        )
        if f is None:
            lines.append(
                _problem(head, f"no unambiguous AeroAPI match ({len(found)} results)", attempt)
            )
            continue
        fills = fields_from(f)
        icao = fills.pop("_aircraft_icao", None)
        if icao and ctx.airtrail_refs is not None:
            try:
                ac = ctx.airtrail_refs.aircraft(icao)
            except Exception as exc:  # noqa: BLE001 - the type is a nicety, the rest still fills
                log.warning("aircraft lookup %s failed: %s", icao, exc)
                ac = None
            if ac:
                fills["aircraftId"] = ac["id"]
        try:
            operation, put = syncer.fill(ti.id, fills)
        except AirTrailError as exc:
            lines.append(_problem(head, f"AirTrail refused the details: {exc}", attempt))
            continue
        if operation == "nothing":
            ctx.ledger.enrichment.mark_done(ti.id, {})
            lines.append(f"🛬 {head}: nothing new to fill")
        elif operation == "fill":
            ctx.ledger.enrichment.mark_done(ti.id, put)
            desc = " · ".join(f"{k} {v}" for k, v in put.items() if k != "aircraftId")
            if "aircraftId" in put:
                desc += f" · aircraft {icao}"
            lines.append(f"🛬 {head} enriched: {desc}")
        elif operation == "detach":
            lines.append(f"🛬 {head}: no longer yours in the flight log; nothing filled")
        elif operation in ("stop", "other", "outage"):
            why = f"the flight log takes no writes now ({_said(operation)})"
            lines.append(_problem(head, why, attempt))
            break
        else:
            why = f"the flight log could not take it now ({_said(operation)})"
            lines.append(_problem(head, why, attempt))
    return lines


# What a notice says for each word the sync's answers use
SAID = {
    "stop": "the key was refused",
    "other": "another flight log is set up",
    "outage": "the flight log did not answer",
    "wait": "not sent yet",
    "dry_run": "dry run",
}


def _said(word: str) -> str:
    """The words a notice shows for one of the sync's answers; never the answer's own word."""
    return SAID.get(word, "not now")


def _problem(head: str, what: str, attempt: int) -> str:
    """A flight that could not be enriched is retried on the next nights; only the final failure
    is worth a warning (anything earlier is noise that fixes itself)."""
    if attempt >= MAX_ATTEMPTS:
        return f"⚠️ {head}: {what} — giving up after {attempt} attempts"
    return f"🛬 {head}: {what} (attempt {attempt}/{MAX_ATTEMPTS}, retrying)"


def notice_lines(lines: list[str]) -> str | None:
    """The run's lines worth a notice: what was filled in, and what finally failed.

    Retries, budget notes and 'nothing to do' stay in the log only.
    """
    worth = [ln for ln in lines if ln.startswith("⚠️") or " enriched: " in ln]
    return "\n".join(worth) or None
