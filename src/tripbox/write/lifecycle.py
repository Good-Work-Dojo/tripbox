# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The flight log switched on, off and on again. Whether one is kept is the ledger's `kv` row
`airtrail` (`on` or `off`; a ledger with none keeps none), which every context notes as it is
built (`note_airtrail`), so the page reads it from the ledger and never from a setting. A change
is an event, not a bare write of the row:

- switched on, every flight filed while no flight log was kept and never sent whose local day
  is before today is held out of the reconcile (`sync_hold` `past`: history the flight log never
  knew about), then every flight is settled, so the upcoming flights the owner is on become
  `pending` by themselves and the next sync pass looks for each in the flight log before it
  creates it;
- switched off, the time is noted (`kv` row `airtrail_off_at`, with the flag) and every flight
  is settled and reads `off`: nothing is sent, nothing only a flight log explains stays open,
  and each flight keeps its flight id and what it was told, so one whose booking changed
  meanwhile is `pending` when the flight log is on again and the change is sent.

A flight filed while a flight log is kept is never held, whatever its day: one still waiting
(in a dry run, behind a check, after an outage) keeps its place across an off and on. A past
flight goes only on request (`enroll`). Each switch is noted in the log file, never on the
terminal.

One ledger keeps one flight log. The flight ids it holds are one AirTrail user's flights, and
the ids in its reference cache are one AirTrail's airports and airlines, so the ledger notes
whose key first sent a flight (`bind`, the `kv` row USER_KV) and at which address the flight log
is (`note_address`, ADDRESS_KV): a sync pass sends nothing while the key answers as another
user, and the cached ids are dropped when the address changes. Moving to another flight log is
the owner's word (`forget`): every flight is unlinked, the upcoming ones are looked for in the
flight log now set up before they are added there, and the past ones go only on request."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from urllib.parse import urlsplit

from tripbox.config import AIRTRAIL_KEY_KV
from tripbox.ledger import Ledger
from tripbox.log import FILE_ONLY
from tripbox.trips.store import AIRTRAIL_KV, AIRTRAIL_OFF_AT_KV, TripStore
from tripbox.util import now_iso
from tripbox.write.airtrail import Profile, key_report

log = logging.getLogger("tripbox.lifecycle")

SAME, ON, OFF = "same", "on", "off"
# The ledger's `kv` rows about the one flight log it keeps: the AirTrail user whose flights its
# flight ids are, the digest of the address its cached ids came from, what the key in use may
# do (JSON: `at`, `kind`, `missing`, `denied`, `more`), and why the last sync pass stopped for
# the key (`unauthorized`, `scope:<name>`, `role`, `other_log`). Each is empty ("") or absent
# while there is nothing to say. The sync's own rows (`write.sync`) say since when the flight
# log has not answered, when that was alerted, and since when it has rejected the key.
USER_KV = "airtrail_user"
ADDRESS_KV = "airtrail_address"
KEY_KV = AIRTRAIL_KEY_KV
KEY_PROBLEM_KV = "airtrail_key_problem"
DOWN_SINCE_KV = "airtrail_down_since"
ALERTED_KV = "airtrail_alerted"
KEY_REJECTED_KV = "airtrail_key_rejected"
OTHER_LOG = "other_log"
PORTS = {"http": 80, "https": 443}


def _kept(ledger: Ledger) -> str:
    """Whether the ledger says a flight log is kept: `on`, else `off`."""
    return ON if ledger.kv.get(AIRTRAIL_KV) == ON else OFF


def note_airtrail(store: TripStore, ledger: Ledger, *, on: bool, today: dt.date) -> str:
    """Note that a flight log is kept (`on`) or not, `today` being the configured zone's day.
    `same` when the ledger says so already: nothing is written and nothing is settled, so a
    context is cheap to build. Otherwise the switch (see the module) runs in one transaction
    that reads the row again under the write lock, so of two processes building a context at
    once the second finds it `same`; then `on` or `off`."""
    want = ON if on else OFF
    if _kept(ledger) == want:
        return SAME
    with ledger.transaction():
        if _kept(ledger) == want:  # another process switched it meanwhile
            return SAME
        held = store.hold_past(today=today) if on else 0
        if not on:
            ledger.kv.set(AIRTRAIL_OFF_AT_KV, now_iso())
        ledger.kv.set(AIRTRAIL_KV, want)
        settled = store.settle_flights()
    if on:
        log.info(
            "the flight log is switched on: %d past flights held out, %d flights settled",
            held,
            settled,
            extra=FILE_ONLY,
        )
    else:
        log.info("the flight log is switched off: %d flights settled", settled, extra=FILE_ONLY)
    return want


def enroll(store: TripStore, *, since: dt.date, apply: bool) -> int:
    """The flights that had left when the flight log was switched on, dated on or after `since`,
    that would be queued: how many (`TripStore.release_past`). With `apply` they are queued, and
    the next sync pass sends them."""
    return store.release_past(since=since, apply=apply)


def address_digest(url: str) -> str:
    """A digest that is the same for every spelling of one flight log's address: the SHA-256 of
    its scheme, its host in lower case, its port (the scheme's own when none is written) and its
    path without a trailing slash. A login in the address and a query are left out. An address
    that does not read as one is itself, whole."""
    where = url.strip()
    try:
        parts = urlsplit(where)
        scheme = parts.scheme.lower()
        port = parts.port or PORTS.get(scheme, 0)
        where = f"{scheme}://{(parts.hostname or '').lower()}:{port}{parts.path.rstrip('/')}"
    except ValueError:
        pass
    return hashlib.sha256(where.encode()).hexdigest()


def note_address(ledger: Ledger, url: str) -> bool:
    """Note the address the flight log is at. The first one is kept and nothing else changes; at
    another address the ids the reference cache holds are another AirTrail's, so the cache is
    emptied and the new address kept, in one transaction that reads the row again under the
    write lock, with one line in the log file. Whether the cache was dropped."""
    digest = address_digest(url)
    if ledger.kv.get(ADDRESS_KV) == digest:
        return False
    with ledger.transaction():
        held = ledger.kv.get(ADDRESS_KV)
        if held == digest:  # another process noted it meanwhile
            return False
        ledger.kv.set(ADDRESS_KV, digest)
        if not held:
            return False
        dropped = ledger.refcache.clear()
    log.info(
        "the flight log's address changed: %d cached ids dropped", dropped, extra=FILE_ONLY
    )
    return True


def note_key(ledger: Ledger, profile: Profile, *, checked: bool = False) -> None:
    """Keep what the profile's key lacks, is denied and holds beyond its need (`key_report`),
    with the credential's kind and the time. Written only when it differs, but for its time,
    from what is kept, or always when `checked` (`tripbox check` asked AirTrail just now, so the
    time is when the key was last checked). A profile that names no credential writes nothing,
    except that, when `checked`, it clears a refusal that is kept: AirTrail answered."""
    report = key_report(profile)
    if report is None:
        if checked and _refused(ledger):
            ledger.kv.set(KEY_KV, "")  # answered now: the refusal is over
        return
    found = {
        "kind": profile.kind,
        "missing": list(report.missing),
        "denied": list(report.denied),
        "more": list(report.more),
    }
    try:
        kept = json.loads(ledger.kv.get(KEY_KV) or "{}")
    except ValueError:
        kept = {}
    if (
        checked
        or not isinstance(kept, dict)
        or {k: v for k, v in kept.items() if k != "at"} != found
    ):
        ledger.kv.set(KEY_KV, json.dumps({"at": now_iso(), **found}))


def _refused(ledger: Ledger) -> bool:
    try:
        kept = json.loads(ledger.kv.get(KEY_KV) or "{}")
    except ValueError:
        return False
    return isinstance(kept, dict) and "refused" in kept


def note_refused(ledger: Ledger, status: int) -> None:
    """Keep that the key was refused with HTTP `status` just now, with the time, in the row
    `note_key` keeps; the next look that is answered replaces it."""
    ledger.kv.set(KEY_KV, json.dumps({"at": now_iso(), "refused": status}))


def bind(ledger: Ledger, profile: Profile) -> bool:
    """Whether the profile's user is the one whose flights the ledger's flight ids are. A
    ledger bound to nobody is bound to this user, in one transaction that reads the row again
    under the write lock; one bound to another user is left as it is and nothing is written.
    When it is, what the key may do is kept too (`note_key`)."""
    if ledger.kv.get(USER_KV) != profile.user_id:
        with ledger.transaction():
            held = ledger.kv.get(USER_KV)
            if held and held != profile.user_id:
                return False
            if not held:
                ledger.kv.set(USER_KV, profile.user_id)
                log.info("the flight log's user is noted", extra=FILE_ONLY)
    note_key(ledger, profile)
    return True


def forget(
    store: TripStore, ledger: Ledger, *, today: dt.date, apply: bool
) -> tuple[int, int, int]:
    """The flights linked to the flight log the ledger knew (`TripStore.forget_flight_log`): how
    many, and of them how many are dated from `today` on and how many before. With `apply` they
    are unlinked, the ledger is bound to nobody, what its key might do, a stopped pass's reason,
    an outage and its alert and a rejected key are forgotten (each is the old flight log's) and
    the cached ids are dropped, in one transaction: the next sync pass binds the ledger to the
    flight log then set up."""
    if not apply:
        return store.forget_flight_log(today=today, apply=False)
    with ledger.transaction():
        counts = store.forget_flight_log(today=today, apply=True)
        for key in (USER_KV, KEY_KV, KEY_PROBLEM_KV, DOWN_SINCE_KV, ALERTED_KV, KEY_REJECTED_KV):
            if ledger.kv.get(key):
                ledger.kv.set(key, "")
        dropped = ledger.refcache.clear()
    log.info(
        "the flight log is forgotten: %d flights unlinked, %d cached ids dropped",
        counts[0],
        dropped,
        extra=FILE_ONLY,
    )
    return counts
