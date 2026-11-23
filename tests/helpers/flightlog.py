# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A flight log held in memory, for the tests: AirTrail's reference answers for every airport
and airline the corpus's truth names, seeded into a ledger's reference cache with invented ids;
a stand-in that records every request, fails one on request as AirTrail words a refusal, and
answers its profile as a key it is given; and that stand-in over the hand-written fixtures' own
airports (`flight_log`)."""

from __future__ import annotations

import copy
import re
from typing import Any

from helpers.airtrail import AIRTRAIL_AIRLINES, AIRTRAIL_AIRPORTS
from tripbox.corpus import Corpus
from tripbox.ledger import Ledger
from tripbox.write.airtrail import (
    AirTrailError,
    MemoryAirTrail,
    Profile,
    best_airline,
    best_airport,
)

AIRPORT = re.compile(r"[A-Z]{3}")  # a truth may give a city beside an airport's code
FIRST_AIRPORT_ID, FIRST_AIRLINE_ID = 7001, 8001  # invented: AirTrail's own ids are its business


def _readings(value: Any) -> list[str]:
    """A truth value is one reading or a list of acceptable ones: the readings that are text."""
    return [r for r in (value if isinstance(value, list) else [value]) if isinstance(r, str) and r]


def named(corpus: Corpus) -> tuple[list[str], list[str]]:
    """The airport codes and the airline codes the corpus's truth names on its flights, sorted."""
    airports: set[str] = set()
    airlines: set[str] = set()
    for entry in corpus.entries:
        for res in entry.truth["reservations"]:
            if res["kind"] != "flight":
                continue
            for leg in res.get("segments") or []:
                for end in ("origin", "destination"):
                    airports |= {r for r in _readings(leg.get(end)) if AIRPORT.fullmatch(r)}
                airlines |= set(_readings(leg.get("carrier_iata")))
    return sorted(airports), sorted(airlines)


def seed(ledger: Ledger, corpus: Corpus) -> None:
    """Give every airport and airline the corpus names an invented id in `ledger`'s reference
    cache, in the shape AirTrail answers a lookup with."""
    airports, airlines = named(corpus)
    for n, code in enumerate(airports, start=FIRST_AIRPORT_ID):
        ledger.refcache.put("airport", code, {"id": n, "iata": code, "type": "large_airport"})
    for n, code in enumerate(airlines, start=FIRST_AIRLINE_ID):
        ledger.refcache.put("airline", code, {"id": n, "iata": code})


def seeded(ledger: Ledger, corpus: Corpus) -> MemoryAirTrail:
    """The in-memory AirTrail over `ledger`, its reference cache seeded for `corpus`."""
    seed(ledger, corpus)
    return MemoryAirTrail(ledger)


class Scripted(MemoryAirTrail):
    """The in-memory AirTrail, recording every request in `requests` as (what, …) and failing
    the next one — or the next of one kind (`find`, `create`, `get`, `update`, `me`, `profile`,
    `reference`) — with a given status and, when given, AirTrail's error code and message, on
    request. Given a profile it answers `profile()` from it as a client does (one request, asked
    again only after `answers_as` or a failure); given none, the in-memory AirTrail's own
    profile is no request. With no ledger its airports carry no code, so one instance may serve
    two threads."""

    def __init__(self, ledger: Ledger | None = None, *, profile: Profile | None = None) -> None:
        super().__init__(ledger)
        self.requests: list[tuple] = []
        self._failures: list[tuple[int, str | None, str | None, str | None]] = []
        self._given = profile
        self._answered: Profile | None = None

    def fail_next(
        self,
        status: int,
        *,
        on: str | None = None,
        times: int = 1,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        self._failures.extend([(status, on, code, message)] * times)

    def _asked(self, what: str, *args: Any) -> None:
        self.requests.append((what, *copy.deepcopy(args)))
        if self._failures and self._failures[0][1] in (None, what):
            status, _, code, message = self._failures.pop(0)
            raise AirTrailError(status, message or f"scripted {status} on {what}", code=code)

    def answers_as(self, profile: Profile | None) -> None:
        """From here on the key is this profile's, as a client built with another key (None: the
        in-memory AirTrail's own)."""
        self._given, self._answered = profile, None

    def profile(self) -> Profile:
        if self._given is None:
            return super().profile()
        if self._answered is None:
            self._asked("profile")
            self._answered = self._given
        return self._answered

    @property
    def sent(self) -> list[tuple]:
        """The requests that would change the flight log: creates and updates."""
        return [r for r in self.requests if r[0] in ("create", "update")]

    @property
    def flights(self) -> list[dict]:
        """What the flight log holds, by id."""
        return [copy.deepcopy(self._flights[k]) for k in sorted(self._flights)]

    def by_hand(self, flight_id: int, **fields: Any) -> None:
        """The owner, or enrichment, sets fields on a flight there; no request is recorded."""
        self._flights[flight_id].update(copy.deepcopy(fields))

    def deleted_by_hand(self, flight_id: int) -> None:
        """The owner deletes a flight there; no request is recorded."""
        del self._flights[flight_id]

    def me(self) -> dict:
        self._asked("me")
        given = self._given
        if given is None:
            return super().me()
        credential = {"kind": given.kind, "scopes": sorted(given.scopes)}
        return {
            "id": given.user_id,
            "isOwner": given.is_owner,
            "permissions": sorted(given.permissions),
            **({"credential": credential} if given.kind else {}),
        }

    def get_json(self, path: str, params: dict[str, str]) -> Any:
        self._asked("reference", path, params.get("query"))
        return super().get_json(path, params)

    def find_flight(
        self,
        *,
        flight_code: str,
        date_local: str,
        origin_iata: str | None,
        user_id: str | None = None,
    ) -> dict | None:
        self._asked("find", flight_code, date_local, origin_iata)
        return super().find_flight(
            flight_code=flight_code, date_local=date_local, origin_iata=origin_iata, user_id=user_id
        )

    def create_flight(self, payload: dict) -> int:
        self._asked("create", payload)
        return super().create_flight(payload)

    def get_flight(self, flight_id: int) -> dict:
        self._asked("get", flight_id)
        return super().get_flight(flight_id)

    def update_flight(self, flight_id: int, payload: dict) -> Any:
        self._asked("update", flight_id, payload)
        return super().update_flight(flight_id, payload)


def flight_log(ledger: Ledger) -> Scripted:
    """A flight log held in memory that knows the airports and airlines of the hand-written
    fixtures (`helpers.airtrail`); from here on the ledger says one is kept."""
    for code, rows in AIRTRAIL_AIRPORTS.items():
        ledger.refcache.put("airport", code, best_airport(rows, code))
    for code, rows in AIRTRAIL_AIRLINES.items():
        ledger.refcache.put("airline", code, best_airline(rows, code))
    ledger.kv.set("airtrail", "on")
    return Scripted(ledger)
