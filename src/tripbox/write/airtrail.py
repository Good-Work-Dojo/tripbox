# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""AirTrail v1 API client (httpx2): profile, reference data, flights list/find/create/get/update;
the ids AirTrail gives its airports, airlines and aircraft, looked up when a flight is written;
what its errors say (their code, the scope a refusal names) and what the key may do."""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2

from tripbox import __version__
from tripbox.log import redact

if TYPE_CHECKING:
    from tripbox.ledger import Ledger

log = logging.getLogger("tripbox.airtrail")

MEMORY_USER_ID = "memory-owner"

# The codes of AirTrail's error body, `{"error": {"code", "message", "details"}}`; each has one
# status (unauthorized 401; forbidden and insufficient_scope 403; not_found 404; conflict 409;
# validation_failed 422; bad_request and invalid_json 400; internal_error 500).
ERROR_CODES = (
    "bad_request",
    "conflict",
    "forbidden",
    "insufficient_scope",
    "internal_error",
    "invalid_json",
    "not_found",
    "unauthorized",
    "validation_failed",
)
# What tripbox's key needs: to learn whose it is, to look up airports and airlines, to read,
# create and update the owner's own flights, and to name companions on them.
REQUIRED_SCOPES = (
    "profile.read",
    "reference_data.read",
    "flight.read.own",
    "flight.create.own",
    "flight.update.own",
    "flight.passengers.manage.own",
)
# Every scope AirTrail v3.14.0 knows (its `scopes.ts`): what is stored of a key is shown only
# when it names one of these.
SCOPES = (
    "flight.read.own",
    "flight.read.any",
    "flight.create.own",
    "flight.create.any",
    "flight.update.own",
    "flight.update.any",
    "flight.delete.own",
    "flight.delete.any",
    "flight.export.own",
    "flight.export.any",
    "flight.passengers.manage.own",
    "flight.passengers.manage.any",
    "users.directory.read",
    "roles.manage",
    "profile.read",
    "preferences.write",
    "reference_data.read",
    "custom_fields.read",
    "stats.read",
    "tracks.read",
    "tracks.write",
    "visited_countries.read",
    "visited_countries.write",
    "shares.read",
    "shares.write",
)
# What AirTrail's scope picker adds to the six: ticking "Your flights" to write ticks the whole
# row, so the smallest key it makes holds these two as well. Harmless to tripbox, which uses
# neither: they are listed, never a problem.
PICKER_ADDS = ("flight.export.own", "flight.delete.own")
# Scopes beyond those that write something else or delete; with every `.any` scope (other
# people's flights) they are more than tripbox needs (`key_report`).
WRITES_MORE = (
    "tracks.write",
    "shares.write",
    "visited_countries.write",
    "preferences.write",
    "roles.manage",
)
# A flight's track and custom fields are the owner's own there: a write that names a track,
# `null` included, replaces or deletes it, so neither key is ever in a body.
NEVER_SENT = ("track", "customFields")
SHOWN = 300  # how much of an error's message is kept
DETAILS_SHOWN = 2  # how many of a validation error's details end its message
_REQUIRES = "The credential requires "  # AirTrail's sentence for a scope the key lacks
_SCOPE = re.compile(r"[a-z_]+(\.[a-z_]+){1,3}")


class AirTrailError(RuntimeError):
    """AirTrail's answer to a request that did not go through: its HTTP status (0 when there
    was no answer to read), its message, masked and cut, and its error code when the body
    names one of ERROR_CODES."""

    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(f"AirTrail HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.code = code


def _details(details: Any) -> str:
    """The first DETAILS_SHOWN of a validation error's details, each as its path joined by dots,
    a colon and its message, joined by `; `; empty when there is none to show."""
    shown: list[str] = []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict) or not isinstance(detail.get("message"), str):
            continue
        path = detail.get("path")
        where = ".".join(str(part) for part in path) if isinstance(path, list) else ""
        shown.append(f"{where}: {detail['message']}" if where else detail["message"])
        if len(shown) == DETAILS_SHOWN:
            break
    return "; ".join(shown)


def _error(r: httpx2.Response) -> tuple[str | None, str]:
    """An error answer's code (one of ERROR_CODES, else None) and its message, masked before it
    is cut, for the log and the notice."""
    try:
        body = r.json()
    except ValueError:
        return None, redact(r.text)[:SHOWN]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            code = err.get("code")
            code = code if isinstance(code, str) and code in ERROR_CODES else None
            message = err["message"]
            if code == "validation_failed" and (details := _details(err.get("details"))):
                message = f"{message}: {details}"
            return code, redact(message)[:SHOWN]
        for key in ("message", "error", "detail"):
            if isinstance(body.get(key), str):
                return None, redact(body[key])[:SHOWN]
    return None, redact(str(body))[:SHOWN]


def missing_scope(exc: AirTrailError) -> str | None:
    """The scope an `insufficient_scope` error says the key lacks: what follows AirTrail's
    sentence, when that is a scope's name and nothing else; None for any other error."""
    if exc.code != "insufficient_scope" or not exc.message.startswith(_REQUIRES):
        return None
    name = exc.message[len(_REQUIRES) :]
    return name if _SCOPE.fullmatch(name) else None


@dataclass(frozen=True)
class Profile:
    """Whose key it is and what it may do, as `/api/v1/me` answers: the user's id, the
    credential's kind (`apiKey`, `oauth`; empty when the answer names none) and scopes, the
    permissions the user's role grants and whether the user owns the instance."""

    user_id: str
    kind: str
    scopes: frozenset[str]
    permissions: frozenset[str]
    is_owner: bool


def _names(value: Any) -> frozenset[str]:
    """The names in a list of an answer's; anything else holds none."""
    if not isinstance(value, list):
        return frozenset()
    return frozenset(v for v in value if isinstance(v, str))


def read_profile(me: Any) -> Profile:
    """The profile of a `/api/v1/me` answer. One that names no user is an AirTrailError; one
    that names no credential says nothing of the key, so its sets are empty."""
    user_id = me.get("id") if isinstance(me, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise AirTrailError(
            0,
            "AirTrail's /api/v1/me named no user;"
            " check that the API key has the profile.read scope",
        )
    credential = me.get("credential")
    is_owner = me.get("isOwner") is True
    if not isinstance(credential, dict) or not credential.get("kind"):
        return Profile(user_id, "", frozenset(), frozenset(), is_owner)
    return Profile(
        user_id,
        str(credential["kind"]),
        _names(credential.get("scopes")),
        _names(me.get("permissions")),
        is_owner,
    )


@dataclass(frozen=True)
class KeyReport:
    """What a key lacks and what it holds beyond its need, each a tuple of scope names:
    `missing`, required scopes the key does not hold; `denied`, required scopes the user's role
    does not grant, whatever the key holds; `more`, scopes that write, delete or reach other
    people's flights, and the two AirTrail's picker adds (PICKER_ADDS; `beyond` leaves them
    out)."""

    missing: tuple[str, ...]
    denied: tuple[str, ...]
    more: tuple[str, ...]


def _twin(scope: str) -> str | None:
    """The `.any` scope that covers an `.own` one."""
    return scope[: -len(".own")] + ".any" if scope.endswith(".own") else None


def key_report(profile: Profile) -> KeyReport | None:
    """What the profile's key lacks, is denied and holds beyond its need; None when the profile
    names no credential (nothing is concluded). AirTrail asks for the scope on the key and for
    the role's permission, and an `.any` covers its `.own`; the two scopes that are no
    permission (the profile, the reference data) are never denied, and the instance's owner is
    granted everything."""
    if not profile.kind:
        return None

    def among(held: frozenset[str], scope: str) -> bool:
        return scope in held or _twin(scope) in held

    missing = tuple(s for s in REQUIRED_SCOPES if not among(profile.scopes, s))
    denied = tuple(
        s
        for s in REQUIRED_SCOPES
        if s.startswith("flight.") and not profile.is_owner and not among(profile.permissions, s)
    )
    more = tuple(
        sorted(
            s
            for s in profile.scopes
            if s.startswith("flight.delete.")
            or s.endswith(".any")
            or s in WRITES_MORE
            or s in PICKER_ADDS
        )
    )
    return KeyReport(missing=missing, denied=denied, more=more)


def beyond(more: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The scopes of `more` a narrower key would not hold: all but PICKER_ADDS."""
    return tuple(s for s in more if s not in PICKER_ADDS)


# What `airtrail status` and `doctor` say of the key, from what the last look at it kept (`kv`
# row `airtrail_key`, written by `lifecycle.note_key`).
KEY_NOT_CHECKED = "key: not checked yet (tripbox check asks AirTrail)"
KEY_MORE = "key: it may also {}; tripbox uses none of them — a narrower key is safer"
KEY_FIX = (
    "in AirTrail, Settings → Security, make a key with profile.read, reference_data.read,"
    " flight.read.own, flight.create.own, flight.update.own and flight.passengers.manage.own,"
    " then run tripbox check"
)
REFUSED_FIX = (
    "in AirTrail, Settings → Security, make a new key with the six scopes, then run tripbox check"
)
ROLE_FIX = "ask the AirTrail instance's owner for a role that permits it, then run tripbox check"


def utc_minute(value: object) -> str:
    """A time a ledger row holds, as `status` and `doctor` print one: read as a time and written
    again in UTC to the minute (`YYYY-MM-DD HH:MM`); anything else is `?`, never as stored."""
    import datetime as dt

    if not isinstance(value, str):
        return "?"
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    try:
        return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError):
        return "?"


def scope_names(value: object) -> tuple[str, ...]:
    """A list of scope names a ledger row holds: each one AirTrail knows (SCOPES) as itself, any
    other as `?`; anything that is not a list holds none."""
    if not isinstance(value, list):
        return ()
    return tuple(v if isinstance(v, str) and v in SCOPES else "?" for v in value)


@dataclass(frozen=True)
class KeyNote:
    """What the last look at the key found, as kept: when (`utc_minute`) and the scopes it
    lacked, was denied and held beyond its need (`scope_names`); `refused`, the HTTP status
    that refused the key (the last look was refused: nothing else is known of it)."""

    at: str
    missing: tuple[str, ...]
    denied: tuple[str, ...]
    more: tuple[str, ...]
    refused: int | None = None

    @classmethod
    def read(cls, text: object) -> KeyNote | None:
        """The note a `kv` row holds; None when it holds nothing that reads as one."""
        if not isinstance(text, str):
            return None
        try:
            kept = json.loads(text)
        except ValueError:
            return None
        if not isinstance(kept, dict):
            return None
        refused = kept.get("refused")
        if isinstance(refused, int) and not isinstance(refused, bool):
            return cls(utc_minute(kept.get("at")), (), (), (), refused)
        return cls(
            utc_minute(kept.get("at")),
            scope_names(kept.get("missing")),
            scope_names(kept.get("denied")),
            scope_names(kept.get("more")),
        )

    def lines(self) -> list[str]:
        """The `key:` lines: what it lacks, else what the role denies, else that it holds the
        six; then, when it holds more than a narrower key would, a line saying so. A refused
        key is the one line."""
        if self.refused is not None:
            return [f"key: refused (HTTP {self.refused}), checked {self.at} UTC"]
        if self.missing:
            first = f"key: lacks {', '.join(self.missing)}, checked {self.at} UTC"
        elif self.denied:
            first = (
                f"key: your role there does not permit {', '.join(self.denied)},"
                f" checked {self.at} UTC"
            )
        else:
            first = f"key: the six scopes, checked {self.at} UTC"
        extra = beyond(self.more)
        return [first, KEY_MORE.format(", ".join(extra))] if extra else [first]


def key_lines(text: object) -> list[str]:
    """What `airtrail status` prints of the key from the `kv` row `text`."""
    note = KeyNote.read(text)
    return note.lines() if note is not None else [KEY_NOT_CHECKED]


def _data(body: Any) -> Any:
    return body.get("data") if isinstance(body, dict) and "data" in body else body


class AirTrailClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 20.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._profile: Profile | None = None
        self.http = httpx2.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": f"tripbox/{__version__}",
            },
        )

    def close(self) -> None:
        self.http.close()

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None
    ) -> Any:
        try:
            r = self.http.request(method, path, params=params, json=json)
        except httpx2.HTTPError as exc:  # DNS, refused, timeout: status 0, still an AirTrailError
            raise AirTrailError(0, f"{type(exc).__name__}: {exc}") from exc
        if r.status_code >= 400:
            code, message = _error(r)
            raise AirTrailError(r.status_code, message, code=code)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as exc:
            raise AirTrailError(0, f"non-JSON response: {redact(r.text)[:120]!r}") from exc

    # (path, params) -> parsed JSON; AirTrailRefs' reference lookups go through it
    def get_json(self, path: str, params: dict[str, str]) -> Any:
        return self._request("GET", path, params=params)

    def me(self) -> dict:
        return _data(self._request("GET", "/api/v1/me")) or {}

    def profile(self) -> Profile:
        """Whose key this client sends and what it may do, asked once: only a success is kept."""
        if self._profile is None:
            self._profile = read_profile(self.me())
        return self._profile

    def user_id(self) -> str:
        """The id of the API key's user (`profile`)."""
        return self.profile().user_id

    def iter_flights(self, *, limit: int = 100, until_date: str | None = None) -> Iterator[dict]:
        """Newest first (AirTrail sorts by date desc); stops once a page reaches `until_date`."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", "/api/v1/flights", params=params)
            flights = _data(body) or []
            yield from flights
            if (
                until_date
                and flights
                and min(str(f.get("date") or "9999") for f in flights) < until_date
            ):
                return
            page = body.get("page") if isinstance(body, dict) else None
            cursor = (page or {}).get("nextCursor")
            if not cursor:
                return

    def find_flight(
        self, *, flight_code: str, date_local: str, origin_iata: str | None
    ) -> dict | None:
        """Match on local date + flight number (+ origin when both sides have it). The number's
        digits are compared with leading zeros dropped, as the sync's record check compares them
        (`sync._agreeing`), so a number typed there as 0123 is the booking's 123."""
        code = flight_code.upper().replace(" ", "")
        number = _digits(code)
        for f in self.iter_flights(until_date=date_local):
            if str(f.get("date") or "")[:10] != date_local:
                continue
            fn = str(f.get("flightNumber") or "").upper().replace(" ", "")
            # exact code, or the same digits under another carrier prefix (codeshare / bare number)
            if not fn or not (fn == code or _digits(fn) == number):
                continue
            f_origin = str(((f.get("from") or {}).get("iata")) or "").upper()
            if origin_iata and f_origin and f_origin != origin_iata.upper():
                continue
            return f
        return None

    def create_flight(self, payload: dict) -> int:
        body = self._request("POST", "/api/v1/flights", json=_sendable(payload))
        data = _data(body) or {}
        return int(data["id"])

    def get_flight(self, flight_id: int) -> dict:
        return _data(self._request("GET", f"/api/v1/flights/{flight_id}")) or {}

    def update_flight(self, flight_id: int, payload: dict) -> Any:
        return self._request("PUT", f"/api/v1/flights/{flight_id}", json=_sendable(payload))


def _digits(code: str) -> str:
    """A flight number's digits without leading zeros (all of them when they are all zeros)."""
    digits = re.sub(r"\D", "", code)
    return digits.lstrip("0") or digits


def _sendable(payload: dict) -> dict:
    """A flight's body without the keys that are never sent (NEVER_SENT)."""
    return {k: v for k, v in payload.items() if k not in NEVER_SENT}


AIRPORTS_PATH = "/api/v1/airports"
AIRLINES_PATH = "/api/v1/airlines"
AIRCRAFT_PATH = "/api/v1/aircraft"

_TYPE_RANK = {
    "large_airport": 0,
    "medium_airport": 1,
    "small_airport": 2,
    "seaplane_base": 3,
    "heliport": 4,
    "balloonport": 5,
    "closed": 9,
}


def reference_rows(body: Any) -> list[dict]:
    """The records of a reference-data answer, bare list or wrapped."""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        for key in ("data", "items", "results", "airports", "airlines", "aircraft"):
            if isinstance(body.get(key), list):
                return [r for r in body[key] if isinstance(r, dict)]
    return []


def best_airport(rows: list[dict], code: str) -> dict | None:
    """The airport with exactly this IATA code: open airports first by type, then the lowest id.
    A closed one is taken only when nothing else carries the code (historical flights into
    since-closed fields still need an id)."""
    hits = [r for r in rows if str(r.get("iata") or "").upper() == code]
    hits.sort(key=lambda r: (_TYPE_RANK.get(str(r.get("type")), 8), r.get("id") or 0))
    return hits[0] if hits else None


def best_airline(rows: list[dict], code: str) -> dict | None:
    """The airline with exactly this IATA code: one with an ICAO code first, then the lowest id."""
    hits = [r for r in rows if str(r.get("iata") or "").upper() == code]
    hits.sort(key=lambda r: (0 if r.get("icao") else 1, r.get("id") or 0))
    return hits[0] if hits else None


def best_aircraft(rows: list[dict], code: str) -> dict | None:
    """The aircraft type with exactly this ICAO code."""
    hits = [r for r in rows if str(r.get("icao") or "").upper() == code]
    return hits[0] if hits else None


class AirTrailRefs:
    """AirTrail's own records for airports, airlines and aircraft types, asked for when a flight
    is written. Only a hit is kept (in the ledger's reference cache, read back for 90 days); a
    miss is stored nowhere and asked again next time, so a code added in AirTrail is found on the
    next write. A cached empty record, a miss stored by older code, counts as absent. A failed
    lookup raises the client's error."""

    def __init__(self, client: AirTrailClient, ledger: Ledger | None) -> None:
        self._client = client
        self._ledger = ledger

    def _record(
        self,
        kind: str,
        path: str,
        code: str | None,
        best: Callable[[list[dict], str], dict | None],
    ) -> dict | None:
        code = (code or "").strip().upper()
        if not code:
            return None
        if self._ledger is not None:
            cached = self._ledger.refcache.get(kind, code)
            if isinstance(cached, dict) and cached:
                return cached
        record = best(reference_rows(self._client.get_json(path, {"query": code})), code)
        if record and self._ledger is not None:
            self._ledger.refcache.put(kind, code, record)
        return record or None

    def airport_id(self, iata: str | None) -> int | None:
        return (self._record("airport", AIRPORTS_PATH, iata, best_airport) or {}).get("id")

    def airline_id(self, iata: str | None) -> int | None:
        return (self._record("airline", AIRLINES_PATH, iata, best_airline) or {}).get("id")

    def airline_icao(self, iata: str | None) -> str | None:
        """The airline's ICAO designator, from the same record (and cache) as its id."""
        icao = (self._record("airline", AIRLINES_PATH, iata, best_airline) or {}).get("icao")
        code = str(icao or "").strip().upper()
        return code or None

    def aircraft(self, icao: str | None) -> dict | None:
        return self._record("aircraft", AIRCRAFT_PATH, icao, best_aircraft)


INPUT_FIELDS = (
    "date",
    "datePrecision",
    "departure",
    "arrival",
    "departureScheduled",
    "arrivalScheduled",
    "takeoffScheduled",
    "takeoffActual",
    "landingScheduled",
    "landingActual",
    "duration",
    "departureTerminal",
    "departureGate",
    "arrivalTerminal",
    "arrivalGate",
    "flightNumber",
    "aircraftReg",
    "note",
)
PASSENGER_FIELDS = ("seat", "seatNumber", "seatClass", "flightReason")


def passenger_to_input(p: dict) -> dict:
    """Passenger (response) -> PassengerInput: keep the id so PUT matches the row; one identity."""
    out: dict = {k: p.get(k) for k in PASSENGER_FIELDS}
    if p.get("id") is not None:
        out["id"] = p["id"]
    user = p.get("user")
    user_id = user.get("id") if isinstance(user, dict) else p.get("userId")
    if user_id:
        out["userId"] = user_id
    else:
        out["guestName"] = p.get("guestName")
    return out


def flight_to_input(flight: dict) -> dict:
    """Flight (response) -> FlightInput for a full-replace PUT that changes nothing on its own."""
    out: dict = {k: flight.get(k) for k in INPUT_FIELDS if k in flight}
    out["fromId"] = (flight.get("from") or {}).get("id")
    out["toId"] = (flight.get("to") or {}).get("id")
    airline = flight.get("airline") or {}
    aircraft = flight.get("aircraft") or {}
    out["airlineId"] = airline.get("id")
    out["aircraftId"] = aircraft.get("id")
    out["passengers"] = [passenger_to_input(p) for p in flight.get("passengers") or []]
    return out


class MemoryAirTrail(AirTrailClient):
    """An AirTrail that lives in this process, for offline runs. It starts empty, gives created
    flights the ids 1, 2, 3 … in order, and serves lookups, reads and updates from what it holds,
    in the shapes the real API returns (`find_flight` is the client's own), so duplicates, schedule
    changes and added passengers behave as they would against a real instance; a flight's
    `duration` is held as sent, in seconds, as AirTrail keeps it. Its reference data
    is AirTrail's answers held in the ledger's reference cache (the rehearsal seeds them): a
    flight's `from` and `to` are the airport records there, and a reference lookup (asked only for
    a code the cache lacks) finds nothing, so that code is one this AirTrail does not know. It
    gives ids only; validation reads airports from the bundled table. Nothing is ever deleted, and
    no request leaves the process: anything else fails as an unreachable server would."""

    def __init__(self, ledger: Ledger | None = None) -> None:
        self._ledger = ledger
        self._profile = None
        self._flights: dict[int, dict] = {}
        self._passenger_ids = 0

    def close(self) -> None:
        pass

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None
    ) -> Any:
        raise AirTrailError(0, f"offline: {method} {path} is not served")

    def get_json(self, path: str, params: dict[str, str]) -> Any:
        if path in (AIRPORTS_PATH, AIRLINES_PATH, AIRCRAFT_PATH):
            return []
        return super().get_json(path, params)

    def me(self) -> dict:
        return {"id": MEMORY_USER_ID, "username": "owner"}

    def profile(self) -> Profile:
        """Its one user, who owns it, with a key of exactly the scopes tripbox needs; no request
        is made for it."""
        return Profile(MEMORY_USER_ID, "apiKey", frozenset(REQUIRED_SCOPES), frozenset(), True)

    def iter_flights(self, *, limit: int = 100, until_date: str | None = None) -> Iterator[dict]:
        """Newest first, as AirTrail lists them."""
        order = sorted(
            self._flights.values(), key=lambda f: (str(f.get("date") or ""), f["id"]), reverse=True
        )
        for f in order:
            yield copy.deepcopy(f)

    def create_flight(self, payload: dict) -> int:
        fid = len(self._flights) + 1
        self._flights[fid] = self._stored(fid, payload)
        return fid

    def get_flight(self, flight_id: int) -> dict:
        if flight_id not in self._flights:
            raise AirTrailError(404, "Flight not found")
        return copy.deepcopy(self._flights[flight_id])

    def update_flight(self, flight_id: int, payload: dict) -> Any:
        if flight_id not in self._flights:
            raise AirTrailError(404, "Flight not found")
        self._flights[flight_id] = self._stored(flight_id, payload)
        return None

    def _airport(self, airport_id: Any) -> dict | None:
        if airport_id is None:
            return None
        if self._ledger is not None:
            rows = self._ledger.conn.execute("SELECT json FROM ref_cache WHERE kind='airport'")
            for (text,) in rows:
                record = json.loads(text)
                if isinstance(record, dict) and record.get("id") == airport_id:
                    return record
        return {"id": airport_id}

    def _passenger(self, p: dict) -> dict:
        pid = p.get("id")
        if pid is None:
            self._passenger_ids += 1
            pid = self._passenger_ids
        user_id = p.get("userId")
        out: dict = {k: p.get(k) for k in PASSENGER_FIELDS}
        out.update(
            id=pid,
            user={"id": user_id} if user_id else None,
            guestName=None if user_id else p.get("guestName"),
        )
        return out

    def _stored(self, fid: int, payload: dict) -> dict:
        """A FlightInput as AirTrail answers it back: nested airport, airline and aircraft
        objects, passengers with ids (an update keeps the ids it carries)."""
        out: dict = {k: copy.deepcopy(payload[k]) for k in INPUT_FIELDS if k in payload}
        out["id"] = fid
        out["from"] = self._airport(payload.get("fromId"))
        out["to"] = self._airport(payload.get("toId"))
        airline, aircraft = payload.get("airlineId"), payload.get("aircraftId")
        out["airline"] = {"id": airline} if airline is not None else None
        out["aircraft"] = {"id": aircraft} if aircraft is not None else None
        out["passengers"] = [self._passenger(p) for p in payload.get("passengers") or []]
        return out
