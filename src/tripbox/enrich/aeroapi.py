# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""FlightAware AeroAPI v4 (Personal tier): flight lookups for post-landing enrichment."""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx2

from tripbox.log import redact

log = logging.getLogger("tripbox.aeroapi")

BASE = "https://aeroapi.flightaware.com/aeroapi"
EST_COST_PER_CALL = 0.005  # /flights/{ident} is $0.005 per result set on the Personal tier

# AeroAPI field -> AirTrail FlightInput field
FIELD_MAP = {
    "registration": "aircraftReg",
    "actual_off": "takeoffActual",
    "actual_on": "landingActual",
    "gate_origin": "departureGate",
    "gate_destination": "arrivalGate",
    "terminal_origin": "departureTerminal",
    "terminal_destination": "arrivalTerminal",
}


class AeroAPIError(RuntimeError):
    """The service's answer was not a success: its HTTP status (0 when there was no answer), and
    with none, the kind of the error that stood in for one."""

    def __init__(self, message: str, *, status: int = 0, kind: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


class AeroAPI:
    def __init__(
        self, api_key: str, *, http: httpx2.Client | None = None, timeout: float = 30.0
    ) -> None:
        self.http = http or httpx2.Client(
            base_url=BASE,
            timeout=timeout,
            headers={"x-apikey": api_key, "Accept": "application/json"},
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = self.http.get(path, params=params)
        except httpx2.HTTPError as exc:  # timeout, DNS failure, refused
            kind = type(exc).__name__
            raise AeroAPIError(f"{kind}: {exc}", kind=kind) from exc
        if r.status_code >= 400:
            raise AeroAPIError(
                f"HTTP {r.status_code}: {redact(r.text)[:200]}", status=r.status_code
            )
        try:
            return r.json()
        except ValueError as exc:
            raise AeroAPIError(
                f"non-JSON response: {redact(r.text)[:120]!r}", kind="non-JSON"
            ) from exc

    def usage(self) -> dict:
        return self._get("/account/usage")

    def flights(self, ident: str, start: dt.date, end: dt.date) -> list[dict]:
        body = self._get(
            f"/flights/{ident}",
            {"ident_type": "designator", "start": start.isoformat(), "end": end.isoformat()},
        )
        return list(body.get("flights") or [])


def _local_date(iso_utc: str | None, tz: str | None) -> str | None:
    if not iso_utc:
        return None
    try:
        d = dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    if tz:
        try:
            d = d.astimezone(ZoneInfo(tz))
        except ZoneInfoNotFoundError:
            pass
    return d.date().isoformat()


def pick_flight(
    flights: list[dict],
    *,
    origin_iata: str,
    destination_iata: str | None,
    date_local: str,
    origin_tz: str | None,
) -> dict | None:
    """The one result that departed `origin_iata` on `date_local`; None when ambiguous or absent."""
    same_day = [
        f
        for f in flights
        if str(((f.get("origin") or {}).get("code_iata")) or "").upper() == origin_iata.upper()
        and _local_date(f.get("scheduled_out") or f.get("scheduled_off"), origin_tz) == date_local
    ]
    if destination_iata:
        exact = [
            f
            for f in same_day
            if str(((f.get("destination") or {}).get("code_iata")) or "").upper()
            == destination_iata.upper()
        ]
        if exact:
            same_day = exact
    if len(same_day) == 1:
        return same_day[0]
    if len(same_day) > 1:
        log.warning(
            "AeroAPI returned %d candidates for %s on %s; skipping",
            len(same_day),
            origin_iata,
            date_local,
        )
    return None


def fields_from(flight: dict) -> dict:
    out = {}
    for src, dst in FIELD_MAP.items():
        v = flight.get(src)
        if v not in (None, ""):
            out[dst] = str(v)
    if flight.get("aircraft_type"):
        out["_aircraft_icao"] = str(flight["aircraft_type"]).upper()
    return out
