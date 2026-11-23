# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The rehearsal seam: recorded model answers (replayed and recorded), the offline AirTrail and
reference data, and the `rehearse` command. Every email, answer and traveller is invented."""

import dataclasses
import datetime as dt
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import httpx2
import pytest
from helpers.airtrail import AIRTRAIL_AIRPORTS
from helpers.eml import make_eml
from helpers.inbox import FakeInbox
from test_normalize import AR_DKIM

from tripbox import cli, evalrun
from tripbox.config import ConfigError, Settings, offline_refusal
from tripbox.enrich import aeroapi
from tripbox.ledger import Ledger
from tripbox.mail import imap as imapmod
from tripbox.mail.normalize import normalize
from tripbox.parse import anthropic_provider as providermod
from tripbox.parse import pipeline as pl
from tripbox.parse.anthropic_provider import AnthropicProvider, estimate_cost
from tripbox.parse.budget import Budgeted
from tripbox.parse.models import WireExtraction, WireLeg, WireReservation, to_extraction
from tripbox.parse.provider import ModelError
from tripbox.parse.replay import (
    REPLAY_FAILURES,
    WIRE_SCHEMA_VERSION,
    RecordingExtractor,
    ReplayExtractor,
    answer_path,
)
from tripbox.pipeline import run as runmod
from tripbox.trips.store import TripStore
from tripbox.validate.refdata import Airports
from tripbox.write import airtrail as airtrailmod
from tripbox.write.airtrail import (
    AirTrailClient,
    AirTrailError,
    AirTrailRefs,
    MemoryAirTrail,
    flight_to_input,
)
from tripbox.write.sync import BACKOFF_MAX, Syncer

D = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
SENDER = "alex@example.com"
TRAVELLERS_YAML = """\
owner:
  names: ["Alex Sample"]
companions:
  - name: "Sam Sample"
"""
# AirTrail's answers for the two airports, as the saved reference cache holds them (its ids)
AIRPORTS = {
    "MSP": {
        "id": 101,
        "iata": "MSP",
        "type": "large_airport",
        "timezone": "America/Chicago",
        "name": "Minneapolis-St Paul",
        "municipality": "Minneapolis",
    },
    "BOS": {
        "id": 102,
        "iata": "BOS",
        "type": "large_airport",
        "timezone": "America/New_York",
        "name": "Logan International",
        "municipality": "Boston",
    },
}
AIRLINE = {"id": 7, "iata": "QX", "icao": "QXE", "name": "Example Air"}
# every credential, as placeholders, so every feature is on
CREDENTIALS = {
    "TRIPBOX_MAIL_HOST": "imap.gmail.com",
    "TRIPBOX_MAIL_USER": "trips@example.com",
    "TRIPBOX_MAIL_PASSWORD": "placeholder",
    "TRIPBOX_MAIL_ALLOWED_SENDERS": SENDER,
    "TRIPBOX_AIRTRAIL_URL": "http://flightlog.invalid",
    "TRIPBOX_AIRTRAIL_API_KEY": "placeholder",
    "TRIPBOX_LLM_API_KEY": "placeholder",
    "TRIPBOX_AEROAPI_KEY": "placeholder",
    "TRIPBOX_TELEGRAM_BOT_TOKEN": "placeholder",
    "TRIPBOX_TELEGRAM_CHAT_ID": "1",
}
SWITCHES = ("TRIPBOX_NO_NETWORK", "TRIPBOX_REPLAY_DIR", "TRIPBOX_RECORD_DIR")


def wire(confidence=0.95):
    return WireExtraction(
        document_type="booking",
        confidence=confidence,
        reservations=[
            WireReservation(
                kind="flight",
                status="confirmed",
                code="ZZTEST",
                provider="Example Air",
                passengers=["Alex Sample"],
                legs=[
                    WireLeg(
                        carrier="QX",
                        number="123",
                        origin="MSP",
                        destination="BOS",
                        departure="2027-03-12T08:20",
                        arrival="2027-03-12T12:10",
                        seats=["Alex Sample: 14A"],
                        cabin="Main",
                    )
                ],
            )
        ],
    )


def jsonld():
    """What kitinerary would print for the same booking: complete, so no model is needed."""
    return {
        "@context": "http://schema.org",
        "@type": "FlightReservation",
        "reservationNumber": "ZZTEST",
        "underName": {"@type": "Person", "name": "SAMPLE/ALEX"},
        "reservationFor": {
            "@type": "Flight",
            "flightNumber": "123",
            "airline": {"@type": "Airline", "iataCode": "QX", "name": "Example Air"},
            "departureAirport": {"@type": "Airport", "iataCode": "MSP"},
            "arrivalAirport": {"@type": "Airport", "iataCode": "BOS"},
            "departureTime": "2027-03-12T08:20:00-05:00",
            "arrivalTime": "2027-03-12T12:10:00-04:00",
        },
    }


def email(subject="Your QX123 itinerary", message_id="<zz1@example.com>", sender=SENDER):
    """Mail from `sender` carrying Gmail's stamp for the owner's domain: the owner's own mail,
    vouched for, when it is the owner who sends it."""
    return make_eml(
        from_=sender,
        to="trips@example.com",
        subject=subject,
        date=D,
        text="Confirmation ZZTEST: QX 123 from MSP to BOS",
        message_id=message_id,
        headers=AR_DKIM,
    ).as_bytes()


def nm_for(raw):
    return normalize(raw, (SENDER,), "America/Chicago", require_auth=False)


class FakeMessages:
    def __init__(self, parsed):
        self.parsed, self.calls = parsed, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.parsed,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=4000, output_tokens=600),
        )


class NoCalls:
    def parse(self, **kwargs):
        raise AssertionError("the model was called")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in CREDENTIALS.items():
        monkeypatch.setenv(k, v)
    for k in SWITCHES:
        monkeypatch.delenv(k, raising=False)
    config = tmp_path / "config"
    config.mkdir()
    (config / "travellers.yaml").write_text(TRAVELLERS_YAML)
    (tmp_path / "answers").mkdir()
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(config))
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_DRY_RUN", "false")
    monkeypatch.setenv("TRIPBOX_MAIL_TRUST", "gmail")  # as the rehearsal reads mail
    monkeypatch.setenv("TRIPBOX_HOME_AIRPORTS", "MSP")
    return tmp_path


def seed_refcache(ledger):
    for code, record in AIRPORTS.items():
        ledger.refcache.put("airport", code, record)
    ledger.refcache.put("airline", "QX", AIRLINE)


def saved_refcache(path):
    """A reference cache as the rehearsal is handed it: old stamps that would count as expired."""
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE ref_cache(kind TEXT, code TEXT, json TEXT, fetched_at TEXT)")
        old = "2020-01-01T00:00:00+00:00"
        for code, record in AIRPORTS.items():
            c.execute(
                "INSERT INTO ref_cache VALUES ('airport', ?, ?, ?)", (code, json.dumps(record), old)
            )
        c.execute(
            "INSERT INTO ref_cache VALUES ('airline', 'QX', ?, ?)", (json.dumps(AIRLINE), old)
        )
    return path


# ----------------------------------------------------------------------------- answers
def test_answer_path_is_the_sha256_of_the_raw_bytes_and_the_schema_version(tmp_path):
    raw = email()
    assert WIRE_SCHEMA_VERSION == 1
    assert answer_path(tmp_path, raw) == tmp_path / f"{hashlib.sha256(raw).hexdigest()}.1.json"
    assert nm_for(raw).raw == raw  # the message carries the bytes it was read from


def test_replay_returns_the_recorded_answer_mapped_and_free(tmp_path):
    raw = email()
    answer_path(tmp_path, raw).write_text(wire().model_dump_json())
    ex, usage = ReplayExtractor(tmp_path).extract(nm_for(raw), None)
    assert ex == to_extraction(wire())
    seg = ex.reservations[0].segments[0]
    assert (seg.carrier_iata, seg.flight_number, seg.origin, seg.destination) == (
        "QX",
        "123",
        "MSP",
        "BOS",
    )
    assert seg.seat_by_passenger == {"Alex Sample": "14A"}
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (0, 0, 0.0)


def test_a_missing_answer_raises_no_recorded_answer(tmp_path):
    answer_path(tmp_path, email()).write_text(wire().model_dump_json())
    other = email(subject="Your QX124 itinerary", message_id="<zz2@example.com>")
    with pytest.raises(ModelError, match="no recorded answer"):
        ReplayExtractor(tmp_path).extract(nm_for(other), None)


def test_an_answer_that_does_not_fit_the_wire_schema_is_an_error_that_quotes_nothing(tmp_path):
    raw = email()
    answer_path(tmp_path, raw).write_text('{"document_type": "ZZTEST-is-no-type"}')
    with pytest.raises(ModelError) as info:
        ReplayExtractor(tmp_path).extract(nm_for(raw), None)
    assert "ZZTEST" not in str(info.value)


def test_an_answers_directory_that_does_not_exist_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="TRIPBOX_REPLAY_DIR"):
        ReplayExtractor(tmp_path / "missing")


def test_recording_stores_a_private_answer_and_a_second_run_replays_it(tmp_path):
    raw = email()
    fake = FakeMessages(wire())
    inner = AnthropicProvider(api_key="k", client=SimpleNamespace(messages=fake))
    first, usage = RecordingExtractor(inner, tmp_path).extract(nm_for(raw), None)
    path = answer_path(tmp_path, raw)
    assert path.stat().st_mode & 0o777 == 0o600
    assert WireExtraction.model_validate_json(path.read_bytes()) == wire()
    assert first == to_extraction(wire()) and len(fake.calls) == 1
    assert (usage.input_tokens, usage.output_tokens) == (4000, 600)
    assert usage.cost_usd == estimate_cost(4000, 600)
    stored = path.read_bytes()
    # a second recording run answers from the file and never calls the model
    silent = AnthropicProvider(api_key="k", client=SimpleNamespace(messages=NoCalls()))
    again, free = RecordingExtractor(silent, tmp_path).extract(nm_for(raw), None)
    assert again == first and free.cost_usd == 0.0 and path.read_bytes() == stored
    # and the rehearsal's replay hands back the same extraction, byte for byte
    replayed, _ = ReplayExtractor(tmp_path).extract(nm_for(raw), None)
    assert replayed.model_dump_json() == first.model_dump_json()
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left


def test_a_failed_model_call_stores_nothing(tmp_path):
    class Refusing:
        def parse(self, **kwargs):
            return SimpleNamespace(
                parsed_output=None,
                stop_reason="refusal",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    inner = AnthropicProvider(api_key="k", client=SimpleNamespace(messages=Refusing()))
    with pytest.raises(ModelError):
        RecordingExtractor(inner, tmp_path).extract(nm_for(email()), None)
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------------------- selection
def test_a_bad_flight_log_address_leaves_the_flight_log_kept(env, monkeypatch):
    """The flight log's address is only the page's link: a bad one leaves AirTrail on, the
    context's flight log and its reconcile in place, and the ledger saying one is kept."""
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_PUBLIC_URL", "flightlog.example")
    s = Settings.from_env()
    assert [p.key for p in s.feature("airtrail").problems] == ["TRIPBOX_AIRTRAIL_PUBLIC_URL"]
    assert s.on("airtrail")
    ctx = runmod.build_context(s, with_notifier=False)
    assert type(ctx.airtrail) is AirTrailClient and ctx.syncer is not None
    assert ctx.syncer.client is ctx.airtrail and ctx.trips.airtrail_on()


def test_without_the_switches_the_context_is_built_as_before(env):
    ctx = runmod.build_context(Settings.from_env())
    assert type(ctx.model) is Budgeted and type(ctx.model.inner) is AnthropicProvider
    assert type(ctx.airtrail) is AirTrailClient
    assert [c.name for c in ctx.notifier.channels] == ["telegram"]
    assert ctx.syncer.alert is not None  # the flight log's alerts are notices
    assert type(ctx.refdata) is Airports
    assert type(ctx.airtrail_refs) is AirTrailRefs and ctx.syncer.refs is ctx.airtrail_refs
    # the reconcile is built over the same flight log, store and travellers as the pipeline's
    assert ctx.syncer.client is ctx.airtrail and ctx.syncer.store is ctx.trips
    assert ctx.syncer.book is ctx.trips.book and not ctx.syncer.dry_run
    assert ctx.executor.store is ctx.trips and not hasattr(ctx.executor, "airtrail")
    ctx.airtrail.close()


def test_with_a_replay_dir_the_pipeline_never_builds_the_model_client(env, monkeypatch):
    built = []

    def sentinel(*args, **kwargs):
        built.append(args)
        raise AssertionError("the real model client was constructed")

    monkeypatch.setattr(providermod.anthropic, "Anthropic", sentinel)
    monkeypatch.setattr(providermod, "AnthropicProvider", sentinel)
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    raw = email()
    answer_path(env / "answers", raw).write_text(wire().model_dump_json())
    ctx = runmod.build_context(Settings.from_env())
    seed_refcache(ctx.ledger)
    assert isinstance(ctx.model, ReplayExtractor)
    res = runmod.process_raw(ctx, raw, source="imap", notify=False)
    assert built == []
    assert res.status == "processed" and res.parsed.parser == "haiku"
    assert [(o.result, o.sync) for o in res.outcomes] == [("filed", "synced")]
    filed = ctx.trips.item(res.outcomes[0].trip_item_id)
    assert filed.airtrail_flight_id == 1  # the in-memory AirTrail's first flight
    # a replayed answer asked no model: the cap counts no call for it
    assert ctx.ledger.conn.execute("SELECT * FROM llm_calls").fetchall() == []


def test_a_replayed_low_confidence_answer_is_marked_like_a_model_answer(env, monkeypatch):
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    raw = email()
    answer_path(env / "answers", raw).write_text(wire(confidence=0.5).model_dump_json())
    ctx = runmod.build_context(Settings.from_env(), with_notifier=False)
    seed_refcache(ctx.ledger)
    res = runmod.process_raw(ctx, raw, source="imap", notify=False)
    (o,) = res.outcomes
    assert res.parsed.parser == "haiku" and res.status == "processed"
    assert "low_confidence" in [p.code for p in o.action.item.problems]
    assert (o.result, o.sync, o.checks) == ("filed", "blocked", ("low_confidence",))
    assert len(ctx.trips.all_trips()) == 1 and list(ctx.airtrail.iter_flights()) == []


def test_offline_builds_no_networked_client(env, monkeypatch):
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    ctx = runmod.build_context(Settings.from_env())
    assert isinstance(ctx.airtrail, MemoryAirTrail) and ctx.notifier.channels == ()
    assert ctx.syncer.client is ctx.airtrail and ctx.syncer.alert is None
    assert type(ctx.refdata) is Airports
    assert type(ctx.airtrail_refs) is AirTrailRefs and ctx.syncer.refs is ctx.airtrail_refs


def test_a_record_dir_wraps_the_model_extractor(env, monkeypatch):
    monkeypatch.setenv("TRIPBOX_RECORD_DIR", str(env / "answers"))
    ctx = runmod.build_context(Settings.from_env(), with_notifier=False)
    # the recorder asks the provider under the cap, and answers from a file it already holds
    assert isinstance(ctx.model, RecordingExtractor) and type(ctx.model.inner) is Budgeted
    assert type(ctx.model.inner.inner) is AnthropicProvider


def test_bad_switch_values_are_config_errors(env, monkeypatch):
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "yes please")
    with pytest.raises(ConfigError, match="TRIPBOX_NO_NETWORK"):
        runmod.build_context(Settings.from_env())
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    monkeypatch.setenv("TRIPBOX_RECORD_DIR", str(env / "answers"))
    with pytest.raises(ConfigError, match="cannot both be set"):
        runmod.build_context(Settings.from_env())


# ----------------------------------------------------------------------------- offline pieces
def test_offline_the_validator_reads_the_bundled_table_never_the_reference_cache(
    env, monkeypatch
):
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    ctx = runmod.build_context(Settings.from_env())
    ctx.ledger.refcache.put("airport", "MSP", AIRPORTS["MSP"])  # AirTrail's answer, with its id
    msp = ctx.refdata.airport("MSP")
    assert "id" not in msp and msp["timezone"] == "America/Chicago"
    assert ctx.refdata.airport("BOS")["icao"] == "KBOS"  # known with nothing cached
    assert ctx.refdata.airport("QQQ") is None
    rows = ctx.ledger.conn.execute("SELECT kind, code FROM ref_cache ORDER BY kind, code")
    assert [tuple(r) for r in rows.fetchall()] == [("airport", "MSP")]


def test_the_memory_airtrail_finds_reads_and_updates_what_it_created(tmp_path):
    led = Ledger.open(tmp_path / "ledger.db")
    seed_refcache(led)
    at = MemoryAirTrail(led)
    payload = {
        "date": "2027-03-12",
        "flightNumber": "QX123",
        "fromId": 101,
        "toId": 102,
        "airlineId": 7,
        "note": "n",
        "passengers": [{"userId": "memory-owner", "seatNumber": "14A", "seatClass": "economy"}],
    }
    assert at.create_flight(payload) == 1
    back = {**payload, "date": "2027-03-13", "fromId": 102, "toId": 101, "passengers": []}
    assert at.create_flight(back) == 2
    hit = at.find_flight(flight_code="QX123", date_local="2027-03-12", origin_iata="MSP")
    assert hit["id"] == 1 and hit["from"]["iata"] == "MSP" and hit["to"]["iata"] == "BOS"
    assert at.find_flight(flight_code="QX123", date_local="2027-03-12", origin_iata="BOS") is None
    inp = flight_to_input(at.get_flight(1))
    inp["passengers"].append({"guestName": "Sam Sample", "seatNumber": "14B", "seatClass": None})
    at.update_flight(1, inp)
    again = flight_to_input(at.get_flight(1))
    assert [p.get("userId") or p.get("guestName") for p in again["passengers"]] == [
        "memory-owner",
        "Sam Sample",
    ]
    assert again["passengers"][0]["id"] == inp["passengers"][0]["id"]  # ids survive an update
    for call in (lambda: at.get_flight(99), lambda: at.update_flight(99, inp)):
        with pytest.raises(AirTrailError) as info:
            call()
        assert info.value.status == 404
    assert at.get_json("/api/v1/airports", {"query": "MSP"}) == []  # its records are the cache
    with pytest.raises(AirTrailError) as info:
        at.get_json("/api/v1/flights", {"limit": "1"})
    assert info.value.status == 0  # no request leaves the process


def test_offline_a_code_the_reference_cache_lacks_is_unknown_not_unreachable(tmp_path):
    from test_planner import BOOK, items_for

    led = Ledger.open(tmp_path / "ledger.db")
    led.kv.set("airtrail", "on")
    at = MemoryAirTrail(led)
    refs = AirTrailRefs(at, led)
    assert refs.airline_id("DL") is None and refs.airport_id("MSP") is None
    assert led.conn.execute("SELECT COUNT(*) FROM ref_cache").fetchone()[0] == 0
    st = TripStore(led, ("MSP",), book=BOOK)
    syncer = Syncer(store=st, ledger=led, book=st.book, client=at, refs=refs, dry_run=False)
    (item,) = items_for(["Alex Sample"])
    ti = st.upsert_item(None, item)
    # an airport the cache lacks is one the flight log does not know (no outage): the flight is
    # marked, and nothing reaches the flight log
    led.refcache.put("airport", "PHL", AIRTRAIL_AIRPORTS["PHL"][0])
    assert syncer.sync_one(ti.id) == "airport"
    marked = st.item(ti.id)
    assert (marked.checks, marked.sync_error) == (("airtrail_airport",), "airport")
    assert list(at.iter_flights()) == [] and not led.kv.get("airtrail_down_since")
    # both airports cached and the airline not: the flight is created without an airline
    led.refcache.put("airport", "MSP", AIRTRAIL_AIRPORTS["MSP"][0])
    later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=BACKOFF_MAX)
    assert syncer.sync_one(ti.id, now=later) == "create"
    sent = st.item(ti.id)
    flight = at.get_flight(sent.airtrail_flight_id)
    assert (flight["from"]["id"], flight["to"]["id"], flight["airline"]) == (9001, 2, None)
    assert sent.sync_state == "synced" and led.refcache.get("airline", "DL") is None


def test_the_settings_file_is_read_like_compose_and_cannot_move_the_run(tmp_path):
    f = tmp_path / "rehearsal.env"
    f.write_text(
        "# placeholder settings\n"
        "\n"
        "TRIPBOX_MAIL_ALLOWED_SENDERS=alex@example.com   # who may send\n"
        "export TRIPBOX_HOME_AIRPORTS=AAA\n"
        'TRIPBOX_ACCESS_TEAM="sample team" # quoted\n'
        "TRIPBOX_MAIL_PASSWORD=a#b\n"
        "TRIPBOX_DATA_DIR=/elsewhere\n"
        "TRIPBOX_LLM_API_KEY=placeholder\n"
    )
    environ = {"TRIPBOX_DATA_DIR": "/work/data", "TZ": "UTC"}
    assert evalrun.apply_env_file(f, environ) == 5
    assert environ == {
        "TRIPBOX_DATA_DIR": "/work/data",
        "TZ": "UTC",
        "TRIPBOX_MAIL_ALLOWED_SENDERS": "alex@example.com",
        "TRIPBOX_HOME_AIRPORTS": "AAA",
        "TRIPBOX_ACCESS_TEAM": "sample team",
        "TRIPBOX_MAIL_PASSWORD": "a#b",
        "TRIPBOX_LLM_API_KEY": "placeholder",
    }
    f.write_text("ZZTEST_WITHOUT_A_VALUE\n")
    with pytest.raises(ConfigError) as info:
        evalrun.read_env_file(f)
    assert "line 1" in str(info.value) and "ZZTEST" not in str(info.value)


def test_the_settings_file_sets_only_tripbox_settings(tmp_path, caplog):
    import logging

    f = tmp_path / "rehearsal.env"
    old_name = "_".join(("IMAP", "HOST"))  # a name from before the prefix, which nothing reads
    f.write_text(f"PATH=/x\nLD_PRELOAD=/y\n{old_name}=x\nTZ=UTC\nTRIPBOX_AIRTRAIL_DRY_RUN=false\n")
    environ: dict[str, str] = {}
    caplog.set_level(logging.INFO, logger="tripbox.eval")
    assert evalrun.apply_env_file(f, environ) == 2
    assert environ == {"TZ": "UTC", "TRIPBOX_AIRTRAIL_DRY_RUN": "false"}
    said = [r.getMessage() for r in caplog.records if r.name == "tripbox.eval"]
    assert said == ["settings file: 3 line(s) are not tripbox settings"]


# ----------------------------------------------------------------------------- the command
def test_rehearse_replays_the_fixtures_in_file_name_order_into_a_fresh_ledger(
    env, monkeypatch, capsys
):
    fixtures = env / "fixtures"
    fixtures.mkdir()
    booking = email()
    stranger = email(subject="Hello", message_id="<zz3@example.com>", sender="pat@example.org")
    (fixtures / "02-stranger.eml").write_bytes(stranger)
    (fixtures / "01-booking.eml").write_bytes(booking)
    answer_path(env / "answers", booking).write_text(wire().model_dump_json())
    refcache = saved_refcache(env / "refcache.db")
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_DRY_RUN", "true")  # the rehearsal sends flights regardless
    report = env / "run.json"
    argv = ["rehearse", str(fixtures), "--report", str(report), "--refcache", str(refcache)]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == (
        "rehearse: messages=2 processed=1 rejected=1 failed=0 items=1"
    )
    assert report.stat().st_mode & 0o777 == 0o600
    doc = json.loads(report.read_text())
    assert doc["schema"] == 2
    first, second = doc["messages"]
    assert (first["n"], first["status"], first["parser"], first["document_type"]) == (
        1,
        "processed",
        "haiku",
        "booking",
    )
    assert first["items"] == [
        {
            "kind": "flight",
            "key": "QX|123|2027-03-12|MSP",
            "result": "filed",
            "sync": "synced",
            "people": {"me": 1, "guests": 0, "others": 0},
            "checks": [],
        }
    ]
    assert (second["n"], second["status"], second["parser"], second["items"]) == (
        2,
        "rejected",
        None,
        [],
    )
    led = sqlite3.connect(env / "data" / "ledger.db")
    assert led.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert led.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 1
    # the flight was sent to the in-memory flight log after its own message, dry run or not
    assert led.execute("SELECT sync_state, airtrail_flight_id FROM trip_items").fetchall() == [
        ("synced", 1)
    ]
    assert led.execute("SELECT v FROM kv WHERE k='airtrail_dry_run'").fetchone()[0] == "off"
    stamps = [r[0] for r in led.execute("SELECT fetched_at FROM ref_cache")]
    assert len(stamps) == 3 and not any(s.startswith("2020") for s in stamps)


def test_a_settings_file_that_still_holds_the_old_dry_run_name_does_not_stop_a_rehearsal(
    env, monkeypatch, capsys
):
    fixtures = env / "fixtures"
    fixtures.mkdir()
    booking = email()
    (fixtures / "01-booking.eml").write_bytes(booking)
    answer_path(env / "answers", booking).write_text(wire().model_dump_json())
    refcache = saved_refcache(env / "refcache.db")
    settings_file = env / "rehearsal.env"
    settings_file.write_text("TRIPBOX_DRY_RUN=true\n")
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    monkeypatch.delenv("TRIPBOX_DRY_RUN", raising=False)
    argv = ["rehearse", str(fixtures), "--env-file", str(settings_file)]
    argv += ["--report", str(env / "run.json"), "--refcache", str(refcache)]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == (
        "rehearse: messages=1 processed=1 rejected=0 failed=0 items=1"
    )
    assert "TRIPBOX_DRY_RUN" not in os.environ


def test_the_rehearsal_report_says_result_sync_and_checks(env, monkeypatch, capsys):
    """Per booking the report holds what became of it, where its sync with the flight log stood
    once its message's pass was done, and its checks: the owner's flight filed and sent, a copy
    of it a duplicate of a flight the log holds, a companion's flight filed and never sent, and
    the way back of that flight naming nobody under a code of its own, filed and held from the
    flight log with its check."""
    fixtures = env / "fixtures"
    fixtures.mkdir()

    def another(names: list[str], turn: int) -> WireExtraction:
        """The booking of `wire()` a day later, for other travellers under a code of their own
        (the first code with its first `turn` letters moved to its end)."""
        w = wire()
        res = w.reservations[0]
        res.passengers, res.code = names, res.code[turn:] + res.code[:turn]
        leg = res.legs[0]
        leg.seats = []
        leg.departure = leg.departure.replace("2027-03-12", "2027-03-13")
        leg.arrival = leg.arrival.replace("2027-03-12", "2027-03-13")
        return w

    def back(w: WireExtraction) -> WireExtraction:
        """`w`'s flight the other way: a flight no booking is."""
        leg = w.reservations[0].legs[0]
        leg.origin, leg.destination = leg.destination, leg.origin
        return w

    answers = (wire(), wire(), another(["Sam Sample"], 1), back(another([], 2)))
    for n, answer in enumerate(answers, 1):
        raw = email(subject=f"Itinerary {n}", message_id=f"<zz{n}@example.com>")
        (fixtures / f"0{n}.eml").write_bytes(raw)
        answer_path(env / "answers", raw).write_text(answer.model_dump_json())
    refcache = saved_refcache(env / "refcache.db")
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    report = env / "run.json"
    argv = ["rehearse", str(fixtures), "--report", str(report), "--refcache", str(refcache)]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == (
        "rehearse: messages=4 processed=3 rejected=0 failed=0 items=4"
    )
    doc = json.loads(report.read_text())
    assert doc["schema"] == evalrun.REPORT_SCHEMA == 2
    statuses = ["processed", "ignored", "processed", "processed"]
    assert [m["status"] for m in doc["messages"]] == statuses
    items = [m["items"][0] for m in doc["messages"]]
    for item in items:
        assert sorted(item) == ["checks", "key", "kind", "people", "result", "sync"]
    assert [(i["result"], i["sync"], i["checks"]) for i in items] == [
        ("filed", "synced", []),
        ("duplicate", "synced", []),
        ("filed", "off", []),
        ("filed", "blocked", ["owner_unknown"]),
    ]
    assert items[0]["key"] == items[1]["key"] != items[2]["key"] != items[3]["key"]
    assert [i["people"] for i in items[1:3]] == [
        {"me": 1, "guests": 0, "others": 0},
        {"me": 0, "guests": 1, "others": 0},
    ]
    led = sqlite3.connect(env / "data" / "ledger.db")
    states = led.execute("SELECT sync_state FROM trip_items ORDER BY id").fetchall()
    assert [s[0] for s in states] == ["synced", "off", "blocked"]


def test_rehearse_refuses_to_run_online_or_without_recorded_answers(env, monkeypatch):
    fixtures = env / "fixtures"
    fixtures.mkdir()
    (fixtures / "a.eml").write_bytes(email())
    s = Settings.from_env()
    with pytest.raises(ConfigError, match="TRIPBOX_NO_NETWORK"):
        evalrun.run_rehearsal(s, fixtures, env / "run.json")
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    with pytest.raises(ConfigError, match="TRIPBOX_REPLAY_DIR"):
        evalrun.run_rehearsal(s, fixtures, env / "run.json")
    assert not (env / "run.json").exists()


def test_recording_asks_once_for_every_fixture_and_never_for_a_stored_answer(env, monkeypatch):
    fixtures = env / "fixtures"
    fixtures.mkdir()
    settled = email(subject="Booking one", message_id="<zz1@example.com>")
    unread = email(subject="Booking two", message_id="<zz2@example.com>")
    stored = email(subject="Booking three", message_id="<zz3@example.com>")
    for name, raw in (("1.eml", settled), ("2.eml", unread), ("3.eml", stored)):
        (fixtures / name).write_bytes(raw)
    answers = env / "answers"
    answer_path(answers, stored).write_text(wire().model_dump_json())
    fake = FakeMessages(wire())
    monkeypatch.setattr(
        providermod,
        "AnthropicProvider",
        lambda key, model, **kw: AnthropicProvider(
            key, model, client=SimpleNamespace(messages=fake), **kw
        ),
    )
    # kitinerary reads the first email whole and nothing of the others
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda data, suffix, ctx_date: [jsonld()] if b"Booking one" in data else [],
    )
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_RECORD_DIR", str(answers))
    counts = evalrun.run_rehearsal(
        Settings.from_env(),
        fixtures,
        env / "run.json",
        refcache=saved_refcache(env / "refcache.db"),
    )
    assert counts["messages"] == 3 and counts["failed"] == 0 and counts["rejected"] == 0
    assert len(fake.calls) == 2  # the settled email (asked once) and the unread one
    hinted = [c["messages"][0]["content"][-1]["text"] for c in fake.calls]
    assert "deterministic parser" in hinted[0] and "deterministic parser" not in hinted[1]
    assert all(answer_path(answers, raw).exists() for raw in (settled, unread, stored))
    doc = json.loads((env / "run.json").read_text())
    assert [m["parser"] for m in doc["messages"]] == ["kitinerary", "haiku", "haiku"]
    led = sqlite3.connect(env / "data" / "ledger.db")
    spent = led.execute("SELECT SUM(cost_usd) FROM llm_calls").fetchone()[0]
    assert spent == pytest.approx(2 * estimate_cost(4000, 600))


def test_recording_dates_a_forward_in_the_configured_zone(env, monkeypatch):
    """The answer recorded for a fixture kitinerary settled is asked with the email date read in
    the zone the settings name, as the pipeline reads it."""
    fixtures = env / "fixtures"
    fixtures.mkdir()
    body = (
        "---------- Forwarded message ---------\nFrom: Example Air <trips@air.example>\n"
        "Date: Mon, Oct 19, 2026 at 9:30 AM\nSubject: Your QX123 itinerary\n\n"
        "Confirmation ZZTEST: QX 123 from MSP to BOS"
    )
    raw = make_eml(
        from_=SENDER,
        to="trips@example.com",
        subject="Fwd: Your QX123 itinerary",
        date=D,
        text=body,
        message_id="<zz1@example.com>",
        headers=AR_DKIM,
    ).as_bytes()
    (fixtures / "1.eml").write_bytes(raw)
    fake = FakeMessages(wire())
    monkeypatch.setattr(
        providermod,
        "AnthropicProvider",
        lambda key, model, **kw: AnthropicProvider(
            key, model, client=SimpleNamespace(messages=fake), **kw
        ),
    )
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [jsonld()])
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_RECORD_DIR", str(env / "answers"))
    evalrun.run_rehearsal(
        dataclasses.replace(Settings.from_env(), tz="America/Denver"),
        fixtures,
        env / "run.json",
        refcache=saved_refcache(env / "refcache.db"),
    )
    [call] = fake.calls
    assert "Email date: 2026-10-19T09:30:00-06:00" in call["messages"][0]["content"][-1]["text"]


def test_a_fixture_without_its_recorded_answer_fails_the_rehearsal(env, monkeypatch):
    """kitinerary reads part of the second email, so the pipeline asks the replay, which has no
    answer: the pipeline fails the message, filing nothing of kitinerary's partial reading, and
    the rehearsal counts the email as failed, since nothing tested it."""
    fixtures = env / "fixtures"
    fixtures.mkdir()
    answered = email(subject="Booking one", message_id="<zz1@example.com>")
    unanswered = email(subject="Booking two", message_id="<zz2@example.com>")
    (fixtures / "1.eml").write_bytes(answered)
    (fixtures / "2.eml").write_bytes(unanswered)
    answer_path(env / "answers", answered).write_text(wire().model_dump_json())
    partial = jsonld()
    del partial["reservationFor"]["departureTime"]
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda data, suffix, ctx_date: [partial] if b"Booking two" in data else [],
    )
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    refcache = saved_refcache(env / "refcache.db")
    report = env / "run.json"
    counts = evalrun.run_rehearsal(Settings.from_env(), fixtures, report, refcache=refcache)
    assert (counts["processed"], counts["failed"]) == (1, 1)
    second = json.loads(report.read_text())["messages"][1]
    assert (second["n"], second["status"], second["parser"]) == (2, "failed", "none")
    assert second["items"] == [] and second["problems"] == ["no_recorded_answer"]
    led = sqlite3.connect(env / "data" / "ledger.db")
    statuses = [r[0] for r in led.execute("SELECT status FROM messages ORDER BY id")]
    assert statuses == ["processed", "failed"]  # what the pipeline itself made of it


@pytest.mark.parametrize(
    "spoil,code",
    [
        (lambda path: path.mkdir(), "unreadable_recorded_answer"),
        (lambda path: path.write_text('{"document_type": "ZZTEST-is-no-type"}'),
         "recorded_answer_off_schema"),
        (lambda path: path.write_text("{not json"), "recorded_answer_off_schema"),
    ],
    ids=["unreadable", "off-schema", "not-json"],
)
def test_a_recorded_answer_that_cannot_be_used_fails_the_rehearsal(env, monkeypatch, spoil, code):
    """As with no answer at all: nothing of kitinerary's partial reading of the second email is
    filed, and the replay tested nothing, so the email fails the rehearsal."""
    fixtures = env / "fixtures"
    fixtures.mkdir()
    answered = email(subject="Booking one", message_id="<zz1@example.com>")
    spoiled = email(subject="Booking two", message_id="<zz2@example.com>")
    (fixtures / "1.eml").write_bytes(answered)
    (fixtures / "2.eml").write_bytes(spoiled)
    answer_path(env / "answers", answered).write_text(wire().model_dump_json())
    spoil(answer_path(env / "answers", spoiled))
    partial = jsonld()
    del partial["reservationFor"]["departureTime"]
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda data, suffix, ctx_date: [partial] if b"Booking two" in data else [],
    )
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    report = env / "run.json"
    refcache = saved_refcache(env / "refcache.db")
    counts = evalrun.run_rehearsal(Settings.from_env(), fixtures, report, refcache=refcache)
    assert (counts["processed"], counts["failed"]) == (1, 1)
    second = json.loads(report.read_text())["messages"][1]
    assert (second["n"], second["status"], second["parser"]) == (2, "failed", "none")
    assert second["items"] == [] and second["problems"] == [code]
    assert "ZZTEST" not in report.read_text()


def test_every_replay_error_has_its_rehearsal_problem_code(tmp_path):
    raw = email()
    path = answer_path(tmp_path, raw)
    seen = set()
    for prepare, key in zip(
        (lambda: None, path.mkdir, lambda: (path.rmdir(), path.write_text("{}"))),
        REPLAY_FAILURES,
        strict=True,
    ):
        prepare()
        with pytest.raises(ModelError) as info:
            ReplayExtractor(tmp_path).extract(nm_for(raw), None)
        seen.add(str(info.value))
        assert (info.value.kind, str(info.value)) == ("invalid", key)  # the text rehearse reads
    assert seen == set(REPLAY_FAILURES)
    assert len(set(REPLAY_FAILURES.values())) == 3


# ----------------------------------------------------------------------------- offline everywhere
def test_offline_without_recorded_answers_has_no_model(env, monkeypatch):
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    with pytest.raises(ConfigError, match="TRIPBOX_NO_NETWORK is set: the model needs"):
        runmod.build_context(Settings.from_env())
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "")  # with no key either: still no live extractor
    with pytest.raises(ConfigError, match="TRIPBOX_NO_NETWORK is set"):
        runmod.build_context(Settings.from_env())
    ctx = runmod.build_context(Settings.from_env(), with_model=False)
    assert ctx.model is None and ctx.notifier.channels == ()


@pytest.mark.parametrize(
    ("argv", "what"),
    [
        (["poll"], "poll"),
        (["enrich"], "enrich"),
        (["check"], "check"),
        (["airtrail", "backfill"], "airtrail backfill"),
        (["bot"], "bot"),
    ],
)
def test_offline_network_commands_refuse_before_building_anything(
    env, monkeypatch, capsys, argv, what
):
    built = []

    def sentinel(*args, **kwargs):
        built.append(args)
        raise AssertionError("a client was constructed")

    for module, name in (
        (runmod, "build_context"),
        (imapmod, "ImapInbox"),
        (aeroapi, "AeroAPI"),
        (airtrailmod, "AirTrailClient"),
        (providermod.anthropic, "Anthropic"),
        (httpx2, "get"),
        (httpx2, "Client"),
    ):
        monkeypatch.setattr(module, name, sentinel)
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    assert cli.main(argv) == 2
    assert capsys.readouterr().err == f"TRIPBOX_NO_NETWORK is set: {what} needs the network\n"
    assert built == []


def test_a_poll_asks_no_other_site_once_the_mail_is_read(env, monkeypatch):
    """After the mail, a poll only tidies the ledger: it builds no HTTP client, online or
    offline (a trip's tile is drawn by the page, never fetched)."""
    built = []

    def sentinel(*args, **kwargs):
        built.append(args)
        raise AssertionError("a client was constructed")

    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv("TRIPBOX_REPLAY_DIR", str(env / "answers"))
    offline = runmod.build_context(Settings.from_env())
    monkeypatch.delenv("TRIPBOX_NO_NETWORK")
    monkeypatch.delenv("TRIPBOX_REPLAY_DIR")
    online = runmod.build_context(Settings.from_env(), with_notifier=False)
    monkeypatch.setattr(httpx2, "Client", sentinel)  # the flight log's client is built already
    monkeypatch.setattr(httpx2, "get", sentinel)
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    assert runmod.poll(offline, FakeInbox()).fetched == 0
    monkeypatch.delenv("TRIPBOX_NO_NETWORK")
    assert runmod.poll(online, FakeInbox()).errors == 0 and built == []


@pytest.mark.parametrize("value", [None, "", "0"])
def test_the_offline_switch_is_inert_when_unset_empty_or_zero(env, monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("TRIPBOX_NO_NETWORK", raising=False)
    else:
        monkeypatch.setenv("TRIPBOX_NO_NETWORK", value)
    assert offline_refusal("poll") is None
    opened = []

    class Opened(FakeInbox):
        def __init__(self, *args):
            super().__init__()
            opened.append(args)

        def close(self) -> None:
            pass

    monkeypatch.setattr(imapmod, "ImapInbox", Opened)
    monkeypatch.setattr(runmod, "poll", lambda ctx, inbox: runmod.PollSummary())
    assert cli.main(["poll"]) == 0
    assert len(opened) == 1 and capsys.readouterr().out.startswith("poll: fetched=0 ")
    assert type(runmod.build_context(Settings.from_env()).model.inner) is AnthropicProvider
