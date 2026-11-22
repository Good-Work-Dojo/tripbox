# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What the tests against a real AirTrail share: the throwaway stock instance the end-to-end stack
starts, reached at E2E_AIRTRAIL_URL with the keys it hands over by name, each one of its demo
users': E2E_AIRTRAIL_API_KEY (the instance's owner, the six scopes tripbox uses),
E2E_AIRTRAIL_HAND_KEY (the owner's own hands: the six and deleting their own flights),
E2E_AIRTRAIL_NARROW_KEY (the six less the passengers scope), E2E_AIRTRAIL_FULL_KEY (the six and the
three `.any` scopes of a key made before AirTrail had scopes) and E2E_AIRTRAIL_OTHER_KEY (another
user, the six).

Settings are built as for a new install plus the AirTrail ones; the model's answers are recorded
ones written here for the invented family, and kitinerary finds nothing in a plain-text forward.
A user is told by id alone and no AirTrail record is ever shown whole, so no demo user's name can
reach a report. Also here: the corpus filed into one ledger with a flight log kept
(`file_corpus`), which the in-memory corpus test shares."""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx2
import pytest

from helpers.eml import make_eml
from helpers.sample import CONFIG_DIR
from tripbox import cli, corpus
from tripbox.config import Settings
from tripbox.parse import pipeline as pl
from tripbox.parse.models import WireExtraction, WireLeg, WireReservation
from tripbox.parse.replay import answer_path
from tripbox.pipeline.run import Context, build_context, process_raw
from tripbox.write.airtrail import AirTrailClient
from tripbox.write.sync import PassReport

URL = os.environ.get("E2E_AIRTRAIL_URL", "")
KEY = os.environ.get("E2E_AIRTRAIL_API_KEY", "")
HAND_KEY = os.environ.get("E2E_AIRTRAIL_HAND_KEY", "")
NARROW_KEY = os.environ.get("E2E_AIRTRAIL_NARROW_KEY", "")
FULL_KEY = os.environ.get("E2E_AIRTRAIL_FULL_KEY", "")
OTHER_KEY = os.environ.get("E2E_AIRTRAIL_OTHER_KEY", "")
READY_SECONDS = 60
# Why a module of them is skipped: the instance is not there. One that holds the address and not
# its key (or the other way round) fails instead, so a stack that lost its key never passes.
NOT_HERE = "needs a throwaway stock AirTrail at E2E_AIRTRAIL_URL and its keys"
CLOSED = "http://127.0.0.1:9"  # a loopback port nothing listens on: no answer at all
FROM = "alex@example.com"
TO = "trips@example.com"
FIRST_SENT = dt.datetime(2027, 1, 2, 15, 0, tzinfo=dt.UTC)  # before every flight here
# each email handed in is sent a minute after the one before, so a later one is never an older copy
_sent = itertools.count()


def absent() -> bool:
    """Whether the instance is not there at all: neither its address nor its key is set."""
    return not URL and not KEY


def wait_until_ready(key: str = KEY) -> None:
    """Waits until AirTrail answers /api/v1/me with a 200 for `key`; fails the module after
    READY_SECONDS."""
    deadline = time.monotonic() + READY_SECONDS
    last: object = "no answer"
    with httpx2.Client(
        base_url=URL, headers={"Authorization": f"Bearer {key}"}, timeout=5.0
    ) as http:
        while True:
            try:
                last = http.get("/api/v1/me").status_code
            except httpx2.HTTPError as exc:
                last = type(exc).__name__
            if last == 200:
                return
            if time.monotonic() >= deadline:
                pytest.fail(f"/api/v1/me did not answer 200 within {READY_SECONDS} s ({last})")
            time.sleep(2)


def set_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, key: str | None = KEY, url: str = URL
) -> Path:
    """A new install's settings with AirTrail on at `url` with `key` (None: no flight log
    kept), in the process environment; returns the directory of recorded answers."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    folder = tmp_path / "answers"
    folder.mkdir(exist_ok=True)
    for name, value in {
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
        "TRIPBOX_REPLAY_DIR": str(folder),
        "TRIPBOX_HOME_AIRPORTS": "MSP",
        "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
        "TRIPBOX_AUTH": "none",
        "TZ": "America/Chicago",
    }.items():
        monkeypatch.setenv(name, value)
    use_flight_log(monkeypatch, key=key, url=url)
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])
    return folder


def use_flight_log(
    monkeypatch: pytest.MonkeyPatch, *, key: str | None = KEY, url: str = URL
) -> None:
    """From here on the settings keep the flight log at `url` with `key`, or none (None)."""
    if key is None:
        monkeypatch.delenv("TRIPBOX_AIRTRAIL_URL", raising=False)
        monkeypatch.delenv("TRIPBOX_AIRTRAIL_API_KEY", raising=False)
        return
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", url)
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_API_KEY", key)


def later(clock: str, minutes: int) -> str:
    """`clock` (HH:MM) `minutes` later, on a clock that wraps at midnight."""
    h, m = map(int, clock.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def ingest(
    tmp_path: Path,
    answers: Path,
    *,
    passengers: list[str],
    number: str,
    code: str,
    day: str,
    message_id: str,
    status: str = "confirmed",
    departure: str = "08:20",
    seats: Mapping[str, str] | None = None,
    cabin: str | None = None,
    document_type: str | None = None,
) -> None:
    """Hands `ingest` a forwarded email about one MSP to BOS flight on `day` (the arrival 3 h
    50 min after the departure by the clock), with the model's answer for exactly these bytes
    recorded beforehand. `seats` gives a seat per traveller, `cabin` the cabin as printed,
    `document_type` what the email is read as (a cancellation for a cancelled flight, else a
    booking, when none is given); each email is sent a minute after the one before."""
    arrival = later(departure, 230)
    sent = FIRST_SENT + dt.timedelta(minutes=next(_sent))
    lines = [f"Confirmation {code}"]
    if status == "cancelled":
        lines.append("Your flight is cancelled.")
    lines.append(f"DL {number} MSP {departure} - BOS {arrival}, {day}")
    lines += [f"{name}{f'  seat {seats[name]}' if seats and name in seats else ''}"
              for name in passengers]
    if cabin:
        lines.append(f"Cabin: {cabin}")
    raw = make_eml(
        from_=FROM,
        to=TO,
        subject="Fwd: Your trip to Boston",
        date=sent,
        text="\n".join(lines),
        message_id=message_id,
    ).as_bytes()
    extraction = WireExtraction(
        document_type=document_type
        or ("cancellation" if status == "cancelled" else "booking"),
        confidence=0.95,
        reservations=[
            WireReservation(
                kind="flight",
                status=status,
                code=code,
                provider="Delta Air Lines",
                passengers=list(passengers),
                legs=[
                    WireLeg(
                        carrier="DL",
                        number=number,
                        origin="MSP",
                        destination="BOS",
                        departure=f"{day}T{departure}",
                        arrival=f"{day}T{arrival}",
                        seats=[f"{name}: {seat}" for name, seat in (seats or {}).items()],
                        cabin=cabin,
                    )
                ],
            )
        ],
    )
    answer_path(answers, raw).write_text(extraction.model_dump_json())
    eml = tmp_path / f"{message_id.strip('<>').split('@')[0]}.eml"
    eml.write_bytes(raw)
    assert cli.main(["ingest", str(eml), "--quiet"]) == 0


def client(key: str = KEY, url: str = URL) -> AirTrailClient:
    """A client of the instance with `key`; the caller closes it."""
    return AirTrailClient(url, key)


def own_flights(c: AirTrailClient) -> dict[int, dict]:
    """The flights the key's user is on there, by id."""
    return {int(f["id"]): f for f in c.iter_flights()}


def listed(c: AirTrailClient, flight_number: str, day: str | None = None) -> list[int]:
    """The ids of the key user's flights with this number (on this local date)."""
    out = []
    for f in c.iter_flights():
        number = str(f.get("flightNumber") or "").upper().replace(" ", "")
        if number == flight_number and (day is None or str(f.get("date") or "")[:10] == day):
            out.append(int(f["id"]))
    return sorted(out)


def passenger_user_ids(flight: dict) -> list[str | None]:
    """The user id of each passenger: a guest has none."""
    ids = []
    for p in flight.get("passengers") or []:
        user = p.get("user")
        ids.append(user.get("id") if isinstance(user, dict) else p.get("userId"))
    return ids


def delete_flight(c: AirTrailClient, flight_id: int) -> None:
    """The owner deletes a flight there, as their own hands would (`flight.delete.own`)."""
    c._request("DELETE", f"/api/v1/flights/{flight_id}")


def counting(c: AirTrailClient, counts: Counter) -> AirTrailClient:
    """`c`, each request it sends from here on counted in `counts` by its HTTP method."""
    send = c._request

    def counted(method: str, path: str, **kwargs: Any) -> Any:
        counts[method] += 1
        return send(method, path, **kwargs)

    c._request = counted  # type: ignore[method-assign]
    return c


def passes(
    settings: Settings | None = None,
    *,
    alerts: list[str] | None = None,
    now: dt.datetime | None = None,
    times: int = 1,
) -> Counter:
    """`times` sync passes (`Syncer.run_pass`) on a new context of `settings` (the process's
    when none is given), one client for all of them, as a worker keeps one: what each pass did,
    counted by operation (`create`, `adopt`, `update`, `detach`, `outage`, `stop`, `other`, …),
    and every request the client sent, counted by HTTP method (`GET`, `POST`, `PUT`). Alerts
    are appended to `alerts`."""
    s = settings or Settings.from_env()
    ctx = build_context(s, with_model=False, with_notifier=False)
    counts: Counter = Counter()
    try:
        assert ctx.syncer is not None and ctx.airtrail is not None
        counting(ctx.airtrail, counts)
        if alerts is not None:
            ctx.syncer.alert = alerts.append
        for _ in range(times):
            report: PassReport = ctx.syncer.run_pass(now=now)
            counts.update(report.done)
    finally:
        if ctx.airtrail is not None:
            ctx.airtrail.close()
        ctx.ledger.close()
    return counts


def file_corpus(
    tmp_path: Path,
    environ: Mapping[str, str],
    *,
    prepare: Callable[[Context], None] | None = None,
) -> Context:
    """The corpus filed, every entry in order through `process_raw` as mail received when its
    email says, into a new ledger under `tmp_path`, under the corpus's settings with `environ`
    added (a flight log's), the corpus's answers standing in for the model. `prepare` is given
    the context before the first entry (a flight log held in memory swaps itself in there). The
    caller closes the context's ledger and client, and holds `corpus.process_as` around what it
    reads (`filed`)."""
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(
        c,
        data_dir=tmp_path / "data",
        archive_dir=tmp_path / "archive",
        logs_dir=tmp_path / "logs",
        environ=environ,
    )
    with corpus.process_as(s):
        ctx = build_context(s, with_model=False, with_notifier=False)
        ctx.model = corpus.CorpusAnswers(c, s.llm_model)
        if prepare is not None:
            prepare(ctx)
        for entry in c.entries:
            process_raw(ctx, entry.raw, source="eval", received=corpus.sent(entry), notify=False)
    return ctx


def refile_corpus(ctx: Context) -> None:
    """Every entry of the corpus read again into `ctx`, forced, as `replay --force` reads it."""
    c = corpus.load(corpus.default_dir())
    with corpus.process_as(ctx.settings):
        for entry in c.entries:
            process_raw(
                ctx,
                entry.raw,
                source="eval",
                received=corpus.sent(entry),
                notify=False,
                force=True,
            )


def flight_log_truth() -> list[dict]:
    """`tests/corpus/flightlog.json`'s flights."""
    return json.loads((corpus.default_dir() / "flightlog.json").read_text())["flights"]


def as_truth(flight: dict, owner: str) -> dict:
    """A flight log's record read as `flightlog.json` states one: the owner (by user id
    `owner`) as `me` first, then each guest by name in name order; `cancelled` when its note
    begins with the line a cancellation puts first."""
    people = passenger_user_ids(flight)
    guests = sorted(
        str(p.get("guestName") or "")
        for p in flight.get("passengers") or []
        if not (isinstance(p.get("user"), dict) or p.get("userId"))
    )
    return {
        "flight": str(flight.get("flightNumber") or "").upper().replace(" ", ""),
        "date": str(flight.get("date") or "")[:10],
        "from": str((flight.get("from") or {}).get("iata") or ""),
        "to": str((flight.get("to") or {}).get("iata") or ""),
        "people": (["me"] if owner in people else []) + guests,
        "cancelled": str(flight.get("note") or "").startswith("CANCELLED"),
    }


def held(flight: dict) -> dict:
    """What a flight log holds of a flight, its note left out: its day, times, length, number,
    airports, airline, aircraft and registration, and who is aboard with their seats."""
    riders = sorted(
        (
            str((p.get("user") or {}).get("id") or p.get("userId") or p.get("guestName") or ""),
            str(p.get("seat")),
            str(p.get("seatNumber")),
            str(p.get("seatClass")),
        )
        for p in flight.get("passengers") or []
    )
    return {
        "date": str(flight.get("date") or "")[:10],
        **{
            k: flight.get(k)
            for k in (
                "departure",
                "arrival",
                "departureScheduled",
                "arrivalScheduled",
                "duration",
                "flightNumber",
                "aircraftReg",
                "departureGate",
            )
        },
        "from": (flight.get("from") or {}).get("id"),
        "to": (flight.get("to") or {}).get("id"),
        "airline": (flight.get("airline") or {}).get("id"),
        "aircraft": (flight.get("aircraft") or {}).get("id"),
        "riders": riders,
    }

