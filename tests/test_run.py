# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import dataclasses
import datetime as dt
import inspect
import json
import logging
from email.message import EmailMessage
from pathlib import Path

import pytest
from helpers.airports import airports
from helpers.eml import make_eml
from helpers.flightlog import flight_log
from helpers.inbox import FakeInbox
from helpers.sample import CONFIG_DIR
from test_budget import DAY, MONTH, UNPRICED, capped, pipeline, read_by_the_model, sha, spend
from test_kitinerary_map import flight_obj

from tripbox import corpus, util
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.notify.notifier import Notifier
from tripbox.notify.telegram import TelegramChannel, check_keyboard
from tripbox.notify.text import booking_line
from tripbox.parse import pipeline as pl
from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.parse.provider import ModelError, Usage
from tripbox.pipeline import run as runmod
from tripbox.pipeline.shift import DateShift
from tripbox.trips.store import TripStore
from tripbox.validate import checks
from tripbox.write import executor as executor_module
from tripbox.write import planner as planner_module
from tripbox.write.executor import Executor
from tripbox.write.sync import PassReport, Syncer

AR = {
    "Authentication-Results": "mx.google.com; dkim=pass header.i=@example.com; "
    "spf=pass smtp.mailfrom=alex@example.com; dmarc=pass header.from=example.com"
}
D = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)


class FakeTelegram:
    """A Telegram client that records what it is asked to send (the chat's notifications reach
    it through the Telegram channel)."""

    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None, silent=False):
        self.sent.append((text, reply_markup))
        return len(self.sent)


def make_ctx(tmp_path, monkeypatch, objects, config_dir=CONFIG_DIR, airtrail=True, env=None):
    """A pipeline context over a new ledger. With `airtrail` a flight log is kept: an AirTrail
    held in memory (`ctx.airtrail`, holding no flight yet) that the pass after each message
    brings in step; without, none is kept and every flight is only filed in its trip. `env`
    adds to the settings' environment, or replaces a name of it."""
    env = {
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(config_dir),
        "TRIPBOX_MAIL_ALLOWED_SENDERS": "alex@example.com",
        "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
        "TZ": "America/Chicago",
        "TRIPBOX_HOME_AIRPORTS": "MSP",
        **(env or {}),
    }
    s = Settings.from_env(env)
    led = Ledger.open(s.ledger_path)
    book = s.travellers
    rd = airports()
    trips = TripStore(led, ("MSP",), book=book)  # the bundled airport table, as a run has it
    log = flight_log(led) if airtrail else None
    if log is None:
        led.kv.set("airtrail", "off")
    tg = FakeTelegram()
    ctx = runmod.Context(
        settings=s,
        ledger=led,
        book=book,
        refdata=rd,
        model=None,
        airtrail=log,
        trips=trips,
        executor=Executor(ledger=led, store=trips, book=book),
        notifier=Notifier([TelegramChannel(tg)]),
        syncer=(
            Syncer(store=trips, ledger=led, book=book, client=log, refs=None, dry_run=False)
            if log is not None
            else None
        ),
    )
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: objects)
    return ctx, tg


def test_process_raw_end_to_end_and_idempotent(tmp_path, monkeypatch):
    ctx, tg = make_ctx(
        tmp_path,
        monkeypatch,
        [flight_obj("SAMPLE/ALEX QUINN", "14A", dep="2027-06-22T08:20:00-05:00")],
    )
    m = make_eml(
        from_="alex@example.com",
        subject="Fwd: Your Flight Receipt",
        date=D,
        text="...",
        headers=AR,
        message_id="<r1@x>",
    )
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=300)
    assert res.status == "processed" and res.label == "tripbox/processed"
    assert [(o.result, o.sync) for o in res.outcomes] == [("filed", "synced")]
    assert len(list((tmp_path / "archive").rglob("*.eml"))) == 1
    row = ctx.ledger.messages.get(res.message_row_id)
    assert row["status"] == "processed" and row["parser"] == "kitinerary" and row["origin_ok"] == 1
    assert len(tg.sent) == 1 and "DL1234 MSP→PHL" in tg.sent[0][0] and " · Me · " in tg.sent[0][0]
    assert "in your flight log" in tg.sent[0][0] and len(ctx.airtrail.flights) == 1
    again = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=301)
    assert again.status == "skipped" and "already processed" in again.skipped_reason
    assert len(tg.sent) == 1 and len(ctx.airtrail.flights) == 1


def test_the_pipeline_starts_without_a_travellers_file(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    env = {
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(tmp_path / "config"),
        "TRIPBOX_MAIL_ALLOWED_SENDERS": "alex@example.com",
        "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
        "TRIPBOX_HOME_AIRPORTS": "MSP",
        "TRIPBOX_AUTH": "none",
    }
    s = Settings.from_env(env)
    assert s.problems == ()
    ctx = runmod.build_context(s, with_model=False, with_notifier=False)
    assert ctx.book.me_name == "" and ctx.executor.book is ctx.book and ctx.syncer is None
    # the owner's own name on a flight files it into a trip, never into AirTrail; its airports
    # come from the bundled table, with no AirTrail to ask
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda data, suffix, ctx_date: [
            flight_obj("SAMPLE/ALEX QUINN", "14A", dep="2027-06-22T08:20:00-05:00")
        ],
    )
    m = make_eml(
        from_="alex@example.com", subject="Fwd: Receipt", date=D, text="x", headers=AR
    )
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=1)
    assert res.status == "processed"
    filed = ctx.trips.item_by_key("DL|1234|2027-06-22|MSP")
    assert (filed.sync_state, filed.airtrail_flight_id) == ("off", None)
    rows = ctx.ledger.items.by_message(res.message_row_id)
    assert [(r["outcome"], r["trip_item_id"]) for r in rows] == [("filed", filed.id)]


def test_poll_without_airtrail_files_every_flight_in_trips(tmp_path, monkeypatch):

    ctx, tg = make_ctx(
        tmp_path,
        monkeypatch,
        [flight_obj("SAMPLE/ALEX QUINN", "14A", number="9123", code="QXR987")],
        airtrail=False,
    )
    raw = make_eml(
        from_="alex@example.com", subject="Fwd: Receipt", date=D, text="x", headers=AR
    ).as_bytes()
    summary = runmod.poll(ctx, FakeInbox(messages={10: raw}).new_to(ctx.ledger))
    assert summary.counts == {"processed": 1} and summary.errors == 0
    filed = ctx.trips.item_by_key("DL|9123|2027-06-22|MSP")
    assert filed is not None and (filed.sync_state, filed.airtrail_flight_id) == ("off", None)
    (row,) = ctx.ledger.items.by_trip_item(filed.id)
    assert (row["outcome"], row["item_key"]) == ("filed", filed.item_key)
    assert ctx.trips.book.classify(filed.passengers).me_present  # the owner's own flight
    assert [t.id for t in ctx.trips.all_trips()] == [filed.trip_id]


def test_rejected_mail_is_archived_but_never_parsed(tmp_path, monkeypatch):
    calls = []
    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda *a: calls.append(a) or [])
    m = make_eml(from_="spam@example.com", subject="Booking", date=D, text="buy now")
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=1)
    assert res.status == "rejected" and res.label == "tripbox/rejected"
    assert calls == [] and tg.sent == []
    assert ctx.ledger.messages.get(res.message_row_id)["status"] == "rejected"


class ReadsNothing:
    """A model that reads an email and finds no booking in it."""

    def extract(self, nm, hint=None):
        return Extraction(document_type="check_in_or_boarding"), Usage("model", 1, 1, 0.0)


def plain_forward() -> bytes:
    """The owner's forward of a booking printed as plain text: nothing structured to read."""
    return make_eml(
        from_="alex@example.com",
        subject="Fwd: Your trip to Philadelphia",
        date=D,
        text="Confirmation QXR987\nDL 9123 MSP 8:20 AM - PHL 11:55 AM on 2027-03-12",
        headers=AR,
        message_id="<qxr987@example.com>",
    ).as_bytes()


def test_mail_that_needs_a_model_waits_when_none_is_configured(tmp_path, monkeypatch, capsys):

    from tripbox import cli

    ctx, tg = make_ctx(tmp_path, monkeypatch, [])  # the extractor finds nothing structured
    assert not ctx.settings.on("model") and ctx.model is None
    raw = plain_forward()
    inbox = FakeInbox(messages={7: raw}).new_to(ctx.ledger)
    summary = runmod.poll(ctx, inbox)
    assert summary.counts == {"waiting": 1} and summary.errors == 0
    assert inbox.copies == []  # no label: the mailbox copy is not marked done
    assert ctx.ledger.kv.get("imap_last_uid") == "7"
    (row,) = ctx.ledger.messages.by_status("waiting")
    assert row["error"] == "no_model"
    assert ctx.ledger.items.by_message(row["id"]) == [] and ctx.trips.all_trips() == []
    assert tg.sent == []  # nothing is notified
    # handed in again with still no model (ingest, a plain replay), it waits again
    res = runmod.process_raw(ctx, raw, source="cli", trusted=True)
    assert (res.status, res.label, res.message_row_id) == ("waiting", None, row["id"])
    assert ctx.ledger.messages.get(row["id"])["status"] == "waiting" and tg.sent == []
    cli._print_result(res, dry_run=False)
    said = f"message #{row['id']}: waiting. No model is set up, so it waits until one is."
    assert capsys.readouterr().out.splitlines()[0] == said


def test_mail_with_no_model_waits_and_is_read_when_one_is_set(tmp_path, monkeypatch):
    """With no model, mail that needs one waits: unlabelled, unannounced, and a poll while
    there is still none leaves it be. Once a model is set up, the next poll reads it, with no
    replay, and tells the owner what it filed."""

    entry = read_by_the_model()[0]
    at = corpus.sent(entry)
    with pipeline(tmp_path, monkeypatch, model=False) as (ctx, asked, told):
        inbox = FakeInbox(messages={7: entry.raw}).new_to(ctx.ledger)
        with util.clock_at(at):
            summary = runmod.poll(ctx, inbox)
            assert runmod.resume_waiting(ctx) == 0  # nothing reads it while no model is built
        assert summary.counts == {"waiting": 1} and inbox.copies == [] and told.notices == []
        (row,) = ctx.ledger.messages.by_status("waiting")
        assert row["error"] == "no_model" and ctx.ledger.messages.waiting() == {"no_model": 1}
        capped(ctx, asked)  # a model is set up
        with util.clock_at(at + dt.timedelta(minutes=5)):
            summary = runmod.poll(ctx, inbox)
        assert summary.fetched == 0 and asked.calls == 1
        row = ctx.ledger.messages.get(row["id"])
        assert (row["status"], row["parser"], row["error"]) == ("processed", "haiku", None)
        assert [b.splitlines()[0] for b in told.of("filed")] == [f"📬 {row['subject']} (haiku)"]
        assert len(ctx.ledger.items.by_message(row["id"])) == len(entry.outcome["items"])


def test_waiting_mail_is_read_when_the_cap_allows_without_a_replay(tmp_path, monkeypatch):
    """Three emails come while this month's model budget is used: each waits, unlabelled, and
    one notice says why. Once the budget is raised, the next poll reads them, oldest first, with
    no replay, and each is told as filed."""

    emails = read_by_the_model()[:3]
    at = corpus.sent(emails[-1])
    with pipeline(tmp_path, monkeypatch, TRIPBOX_LLM_MONTHLY_BUDGET_USD="1.00") as (
        ctx,
        asked,
        told,
    ):
        inbox = FakeInbox(messages={n: e.raw for n, e in enumerate(emails, 1)})
        inbox.new_to(ctx.ledger)
        with util.clock_at(at):
            spend(ctx, 1.00)
            summary = runmod.poll(ctx, inbox)
        assert summary.counts == {"waiting": 3} and inbox.copies == [] and asked.calls == 0
        assert told.of("filed") == [] and told.of("budget_reached") == [
            MONTH.format(budget="1.00")
        ]
        waited = ctx.ledger.messages.by_status("waiting")
        assert [r["error"] for r in waited] == ["over_budget"] * 3
        capped(ctx, asked, TRIPBOX_LLM_MONTHLY_BUDGET_USD="2.00")  # the owner raises it
        with util.clock_at(at + dt.timedelta(minutes=5)):
            summary = runmod.poll(ctx, inbox)
        assert summary.fetched == 0 and asked.calls == 3
        rows = [ctx.ledger.messages.get(r["id"]) for r in waited]
        assert [(r["status"], r["error"]) for r in rows] == [("processed", None)] * 3
        assert [b.splitlines()[0] for b in told.of("filed")] == [
            f"📬 {r['subject']} (haiku)" for r in rows
        ]
        assert len(told.of("budget_reached")) == 1 and ctx.ledger.messages.waiting() == {}


def test_the_resume_pass_stops_at_the_first_message_that_waits_again(tmp_path, monkeypatch):
    """Three emails wait for a model. One is set up that may make one call a day: the resume pass
    reads the oldest, the next waits again (now for the cap), and the pass stops there; the last,
    not read, waits for the cap too, as every message that waits does."""
    emails = read_by_the_model()[:3]
    at = corpus.sent(emails[0])
    cap = {"TRIPBOX_LLM_MODEL": UNPRICED, "TRIPBOX_LLM_MAX_CALLS_PER_DAY": "1"}
    with pipeline(tmp_path, monkeypatch, model=False, **cap) as (ctx, asked, told):
        with util.clock_at(at):
            for n, e in enumerate(emails, 1):
                assert runmod.process_raw(ctx, e.raw, source="imap", uid=n).status == "waiting"
            capped(ctx, asked, **cap)
            read: list[bytes] = []
            reading = runmod.process_raw

            def counted(ctx, raw, **kw):
                read.append(raw)
                return reading(ctx, raw, **kw)

            monkeypatch.setattr(runmod, "process_raw", counted)
            assert runmod.resume_waiting(ctx) == 1
        assert asked.calls == 1
        # the pass read the oldest and the next, which waited again, and never the last
        assert [sha(raw) for raw in read] == [sha(emails[0].raw), sha(emails[1].raw)]
        rows = [ctx.ledger.messages.by_sha(sha(e.raw)) for e in emails]
        assert [(r["status"], r["error"]) for r in rows] == [
            ("processed", None),
            ("waiting", "over_budget"),
            ("waiting", "over_budget"),
        ]
        assert ctx.ledger.messages.waiting() == {"over_budget": 2}
        assert told.of("budget_reached") == [DAY.format(n=1)] and told.of("failure") == []


def test_a_resume_reading_cut_short_leaves_the_message_waiting(tmp_path, monkeypatch):
    """The poll that reads what waits can be stopped while the model reads (the worker stops a
    poll that runs past its time). While it reads, the message still counts as waiting; a
    reading cut short leaves it waiting, with its reason, so the next resume pass reads it."""
    import pytest

    entry = read_by_the_model()[0]
    at = corpus.sent(entry)
    with pipeline(tmp_path, monkeypatch, model=False) as (ctx, asked, told):
        with util.clock_at(at):
            assert runmod.process_raw(ctx, entry.raw, source="imap", uid=1).status == "waiting"
        capped(ctx, asked)  # a model is set up
        seen = []

        def stopped():
            seen.append(ctx.ledger.messages.waiting())
            raise SystemExit(1)

        asked.reading = stopped
        with util.clock_at(at + dt.timedelta(minutes=5)), pytest.raises(SystemExit):
            runmod.resume_waiting(ctx)
        assert seen == [{"no_model": 1}] and asked.calls == 1
        row = ctx.ledger.messages.by_sha(sha(entry.raw))
        assert (row["status"], row["error"]) == ("waiting", "no_model")
        assert ctx.ledger.messages.waiting() == {"no_model": 1}
        asked.reading = None
        with util.clock_at(at + dt.timedelta(minutes=10)):
            assert runmod.resume_waiting(ctx) == 1
        row = ctx.ledger.messages.get(row["id"])
        assert (row["status"], row["error"]) == ("processed", None)
        assert ctx.ledger.messages.waiting() == {} and asked.calls == 2


def test_a_model_that_is_down_makes_mail_wait_one_notice_and_resumes(tmp_path, monkeypatch):
    """The model cannot be reached: each email that needs it waits, and one notice says so for
    the whole outage, however often it is tried (within a minute of its last failure it is not
    asked at all). Once it answers, the next poll reads what waited and the outage is over: the
    next one is told again."""
    *emails, later = read_by_the_model()[:4]
    at = corpus.sent(emails[0])
    down = (
        "tripbox cannot reach the model ({}). Mail that needs it waits and is read when it"
        " answers again."
    )
    with pipeline(tmp_path, monkeypatch) as (ctx, asked, told):
        asked.fails = ModelError("unavailable", "no connection", word="unreachable")
        with util.clock_at(at):
            got = [
                runmod.process_raw(ctx, e.raw, source="imap", uid=n)
                for n, e in enumerate(emails, 1)
            ]
        assert [(r.status, r.label) for r in got] == [("waiting", None)] * 3
        assert asked.calls == 1 and ctx.ledger.messages.waiting() == {"model_down": 3}
        assert told.of("failure") == [down.format("unreachable")]
        with util.clock_at(at + dt.timedelta(minutes=5)):
            runmod.poll(ctx, None)  # still down: the oldest is tried, and the pass stops
        assert asked.calls == 2 and told.of("failure") == [down.format("unreachable")]
        asked.fails = None
        with util.clock_at(at + dt.timedelta(minutes=10)):
            runmod.poll(ctx, None)
        assert asked.calls == 5 and ctx.ledger.messages.waiting() == {}
        assert not ctx.ledger.kv.get("model_alerted") and len(told.of("filed")) == 3
        asked.fails = ModelError("unavailable", "HTTP 529", word="server_error")
        with util.clock_at(at + dt.timedelta(minutes=15)):
            assert runmod.process_raw(ctx, later.raw, source="imap", uid=4).status == "waiting"
        assert told.of("failure") == [down.format("unreachable"), down.format("server_error")]


def test_a_refused_or_cut_short_answer_fails_the_message_once_and_is_not_asked_again(
    tmp_path, monkeypatch
):
    """An answer the model refused, cut short, or that does not fit the wire schema (an answer
    cut short usually arrives so) fails the message with one notice; the next poll asks nothing,
    and only `tripbox replay --reread --status failed` asks again."""

    entry = read_by_the_model()[0]
    at = corpus.sent(entry)
    for kind in ("refused", "truncated", "invalid"):
        with pipeline(tmp_path / kind, monkeypatch) as (ctx, asked, told):
            asked.fails = ModelError(kind, f"the answer was {kind}")
            inbox = FakeInbox(messages={7: entry.raw}).new_to(ctx.ledger)
            with util.clock_at(at):
                first = runmod.poll(ctx, inbox)
                second = runmod.poll(ctx, inbox)
            assert first.counts == {"failed": 1} and second.fetched == 0, kind
            assert asked.calls == 1 and inbox.copies == [(7, "tripbox/failed")], kind
            (row,) = ctx.ledger.messages.by_status("failed")
            assert told.of("failure") == [
                f"⚠️ tripbox could not process “{row['subject']}”: the answer was {kind}"
            ], kind
            assert told.of("budget_reached") == [] and ctx.ledger.messages.waiting() == {}
            # replay --reread --status failed
            asked.fails = None
            with util.clock_at(at + dt.timedelta(minutes=5)):
                for failed in ctx.ledger.messages.by_status("failed"):
                    raw = Path(failed["archive_path"]).read_bytes()
                    res = runmod.process_raw(ctx, raw, source="replay", reread=True)
                    assert res.status == "processed", kind
            assert asked.calls == 2, kind


def part_read(monkeypatch) -> None:
    """The structured-data extractor reads a flight of every email in part: no departure time,
    so the model is asked."""
    obj = flight_obj("SAMPLE/ALEX QUINN", "14A")
    del obj["reservationFor"]["departureTime"]
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [obj])


def test_an_email_read_in_part_waits_for_the_model_as_one_read_not_at_all(
    tmp_path, monkeypatch
):
    """The extractor read the email only in part and the model, over its cap, cannot read the
    rest: the part read is not filed; the message waits, told once, and once the cap allows the
    next poll reads it with the model."""
    entry = read_by_the_model()[0]
    at = corpus.sent(entry)
    with pipeline(tmp_path, monkeypatch, TRIPBOX_LLM_MONTHLY_BUDGET_USD="1.00") as (
        ctx,
        asked,
        told,
    ):
        part_read(monkeypatch)
        inbox = FakeInbox(messages={7: entry.raw}).new_to(ctx.ledger)
        with util.clock_at(at):
            spend(ctx, 1.00)
            summary = runmod.poll(ctx, inbox)
        assert summary.counts == {"waiting": 1} and inbox.copies == [] and asked.calls == 0
        (row,) = ctx.ledger.messages.by_status("waiting")
        assert row["error"] == "over_budget"
        assert ctx.ledger.items.by_message(row["id"]) == [] and ctx.trips.all_trips() == []
        assert told.of("budget_reached") == [MONTH.format(budget="1.00")]
        assert told.of("filed") == []
        capped(ctx, asked, TRIPBOX_LLM_MONTHLY_BUDGET_USD="2.00")
        with util.clock_at(at + dt.timedelta(minutes=5)):
            runmod.poll(ctx, inbox)
        row = ctx.ledger.messages.get(row["id"])
        assert (row["status"], row["parser"], row["error"]) == ("processed", "haiku", None)
        assert asked.calls == 1


def test_an_email_read_in_part_waits_while_no_model_is_set_up_or_one_is_down(
    tmp_path, monkeypatch
):
    entry = read_by_the_model()[0]
    at = corpus.sent(entry)
    with pipeline(tmp_path / "none", monkeypatch, model=False) as (ctx, asked, told):
        part_read(monkeypatch)
        with util.clock_at(at):
            res = runmod.process_raw(ctx, entry.raw, source="imap", uid=1)
        assert (res.status, res.wait_reason, res.outcomes) == ("waiting", "no_model", [])
        assert ctx.trips.all_trips() == [] and told.notices == []
    with pipeline(tmp_path / "down", monkeypatch) as (ctx, asked, told):
        part_read(monkeypatch)
        asked.fails = ModelError("unavailable", "no connection", word="unreachable")
        with util.clock_at(at):
            res = runmod.process_raw(ctx, entry.raw, source="imap", uid=1)
        assert (res.status, res.wait_reason, res.outcomes) == ("waiting", "model_down", [])
        assert ctx.trips.all_trips() == [] and len(told.of("failure")) == 1


def test_an_email_read_in_part_fails_when_the_model_s_answer_is_no_use(tmp_path, monkeypatch):
    entry = read_by_the_model()[0]
    at = corpus.sent(entry)
    for kind in ("refused", "truncated", "invalid"):
        with pipeline(tmp_path / kind, monkeypatch) as (ctx, asked, told):
            part_read(monkeypatch)
            asked.fails = ModelError(kind, f"the answer was {kind}")
            with util.clock_at(at):
                res = runmod.process_raw(ctx, entry.raw, source="imap", uid=1)
            assert (res.status, res.label, res.outcomes) == ("failed", "tripbox/failed", []), kind
            assert ctx.trips.all_trips() == [], kind
            row = ctx.ledger.messages.get(res.message_row_id)
            assert row["error"] == f"the answer was {kind}", kind
            assert len(told.of("failure")) == 1 and told.of("filed") == [], kind


def test_noise_without_reservations_is_ignored_quietly(tmp_path, monkeypatch):
    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    ctx.model = ReadsNothing()  # a model read it and found nothing: finished, not waiting
    m = make_eml(
        from_="alex@example.com",
        subject="Fwd: Time to check in!",
        date=D,
        text="check in now",
        headers=AR,
    )
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=2)
    assert res.status == "ignored" and res.label == "tripbox/processed" and tg.sent == []


def test_a_marked_booking_sends_its_buttons(tmp_path, monkeypatch):
    """A flight that names nobody is filed, marked: the message's lines say what is to check,
    and the booking's own card carries the buttons that answer it."""
    ctx, tg = make_ctx(tmp_path, monkeypatch, [flight_obj("", None)])  # no passenger name
    m = make_eml(from_="alex@example.com", subject="Fwd: Receipt", date=D, text="x", headers=AR)
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=3)
    assert res.status == "processed" and res.label == "tripbox/processed"
    (o,) = res.outcomes
    assert (o.result, o.sync, o.checks) == ("filed", "blocked", ("owner_unknown",))
    (lines, plain), (card, markup) = tg.sent
    assert plain is None and "to check: " + checks.LABELS["owner_unknown"] in lines
    filed = ctx.trips.item(o.trip_item_id)
    assert markup == check_keyboard(filed, flight_log=ctx.trips.airtrail_on())
    assert card.startswith(booking_line(filed, ctx.book))
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == [
        f"ck:{filed.id}:me",
        f"ck:{filed.id}:no",
    ]


def test_poll_labels_and_advances_cursor(tmp_path, monkeypatch):

    ctx, tg = make_ctx(
        tmp_path, monkeypatch, [flight_obj("ALEX SAMPLE", "1A", dep="2027-06-22T08:20:00-05:00")]
    )
    good = make_eml(
        from_="alex@example.com", subject="Fwd: Receipt", date=D, text="x", headers=AR
    ).as_bytes()
    bad = make_eml(from_="spam@example.com", subject="hi", date=D, text="x").as_bytes()
    inbox = FakeInbox(messages={10: good, 11: bad}).new_to(ctx.ledger)
    summary = runmod.poll(ctx, inbox)
    assert summary.fetched == 2 and summary.counts == {"processed": 1, "rejected": 1}
    assert sorted(inbox.copies) == [(10, "tripbox/processed"), (11, "tripbox/rejected")]
    assert ctx.ledger.kv.get("imap_last_uid") == "11" and ctx.ledger.kv.get("last_poll_ok")


def test_process_text_paste(tmp_path, monkeypatch):
    ctx, tg = make_ctx(
        tmp_path, monkeypatch, [flight_obj("ALEX SAMPLE", "1A", dep="2027-06-22T08:20:00-05:00")]
    )
    res = runmod.process_text(ctx, "DL 1234 MSP-PHL 22 Jun", source="telegram")
    assert res.status == "processed" and res.outcomes[0].result == "filed"
    assert list((tmp_path / "archive").rglob("*.eml"))  # pasted text is archived too


def test_same_email_delivered_twice_is_ignored_by_message_id(tmp_path, monkeypatch):
    """The filter's forward first, then the owner's forward of the same mail as an attachment."""
    ctx, tg = make_ctx(
        tmp_path, monkeypatch, [flight_obj("ALEX SAMPLE", "1A", dep="2027-06-22T08:20:00-05:00")]
    )
    first = make_eml(
        from_="alex@example.com",
        subject="Receipt",
        date=D,
        text="a",
        headers=AR,
        message_id="<o@dl>",
    )
    second = make_eml(  # different bytes (subject/body), same original Message-ID
        from_="alex@example.com",
        subject="Fwd: Receipt",
        date=D,
        text="b",
        headers=AR,
        message_id="<o@dl>",
    )
    r1 = runmod.process_raw(ctx, first.as_bytes(), source="imap", uid=1)
    r2 = runmod.process_raw(ctx, second.as_bytes(), source="imap", uid=2)
    assert r1.status == "processed" and r2.status == "ignored" and "Message-ID" in r2.skipped_reason
    rows = ctx.ledger.messages.by_message_id("<o@dl>")
    assert [r["status"] for r in rows] == ["processed", "ignored"]
    assert len(tg.sent) == 1  # no second Telegram line, no second parse
    # --force still lets it through; the ledger then calls the flight a duplicate
    r3 = runmod.process_raw(ctx, second.as_bytes(), source="replay", uid=None, force=True)
    assert r3.status == "ignored" and [o.result for o in r3.outcomes] == ["duplicate"]


def test_reading_a_message_again_replaces_its_log_rows(tmp_path, monkeypatch):
    ctx, tg = make_ctx(tmp_path, monkeypatch, [flight_obj("", None)])  # names nobody
    raw = make_eml(from_="alex@example.com", subject="R", date=D, text="x", headers=AR).as_bytes()
    first = runmod.process_raw(ctx, raw, source="imap", uid=7)
    assert [o.result for o in first.outcomes] == ["filed"]
    second = runmod.process_raw(ctx, raw, source="replay", force=True)
    assert second.message_row_id == first.message_row_id
    # the first reading's row is gone: the message's log is the second reading's alone, and the
    # booking it filed is the one the second finds
    (o,) = second.outcomes
    assert (o.result, o.trip_item_id) == ("duplicate", first.outcomes[0].trip_item_id)
    rows = ctx.ledger.items.by_message(first.message_row_id)
    assert [(r["id"], r["outcome"]) for r in rows] == [(o.item_id, "duplicate")]
    assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM trip_items").fetchone()[0] == 1
    assert ctx.ledger.messages.get(first.message_row_id)["status"] == "ignored"


def test_a_failed_message_is_read_again_and_a_processed_one_is_not(tmp_path, monkeypatch):
    """A message that was processed is skipped when it is handed in again; one that failed is
    read again, from the archive file it already has, and its failed row is replaced. Reading
    it again does not defer to a later copy of the same email that was set aside for it."""
    calls = []
    objs = [flight_obj("ALEX SAMPLE", "1A", dep="2027-06-22T08:20:00-05:00")]
    ctx, tg = make_ctx(tmp_path, monkeypatch, objs)
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda *a: calls.append(1) or objs)
    a = make_eml(
        from_="alex@example.com", subject="R", date=D, text="a", headers=AR, message_id="<same>"
    ).as_bytes()
    b = make_eml(
        from_="alex@example.com",
        subject="Fwd: R",
        date=D,
        text="b",
        headers=AR,
        message_id="<same>",
    ).as_bytes()
    filing = ctx.trips.upsert_item

    def broken(*args, **kw):
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(ctx.trips, "upsert_item", broken)
    failed = runmod.process_raw(ctx, a, source="imap", uid=1)
    assert failed.status == "failed" and [o.result for o in failed.outcomes] == ["failed"]
    assert "the disk is full" in failed.outcomes[0].error and ctx.trips.all_trips() == []
    monkeypatch.setattr(ctx.trips, "upsert_item", filing)
    # the same bytes again: read again, with no force
    res = runmod.process_raw(ctx, a, source="replay")
    assert res.status == "processed" and res.message_row_id == failed.message_row_id
    assert [(o.result, o.sync) for o in res.outcomes] == [("filed", "synced")]
    assert len(calls) == 2 and len(list((tmp_path / "archive").rglob("*.eml"))) == 1
    rows = ctx.ledger.items.by_message(res.message_row_id)
    assert [(r["outcome"], r["detail"]) for r in rows] == [("filed", None)]
    # processed: nothing is read again
    assert runmod.process_raw(ctx, a, source="replay").status == "skipped" and len(calls) == 2
    # a copy under the same Message-ID is set aside for it, never parsed
    assert runmod.process_raw(ctx, b, source="imap", uid=2).status == "ignored"
    assert len(calls) == 2
    # something after the filing went wrong and the first ended up failed: it is read again
    # (not deferred to the copy that was set aside for it), and its booking is a duplicate
    ctx.ledger.messages.finish(res.message_row_id, "failed", error="hiccup after filing")
    again = runmod.process_raw(ctx, a, source="replay")
    assert again.status == "ignored" and [o.result for o in again.outcomes] == ["duplicate"]
    rows = ctx.ledger.items.by_message(again.message_row_id)
    assert [r["outcome"] for r in rows] == ["duplicate"]  # the earlier reading's row is replaced
    assert len(calls) == 3 and len(ctx.airtrail.flights) == 1
    assert runmod.process_raw(ctx, b, source="replay").status == "skipped"


def test_notification_failure_does_not_flip_the_message_status(tmp_path, monkeypatch):
    class BrokenTelegram(FakeTelegram):
        def send(self, text, reply_markup=None, silent=False):
            raise RuntimeError("telegram down")

    ctx, _ = make_ctx(
        tmp_path, monkeypatch, [flight_obj("ALEX SAMPLE", "1A", dep="2027-06-22T08:20:00-05:00")]
    )
    ctx.notifier = Notifier([TelegramChannel(BrokenTelegram())])
    raw = make_eml(from_="alex@example.com", subject="R", date=D, text="x", headers=AR).as_bytes()
    res = runmod.process_raw(ctx, raw, source="imap", uid=1)
    assert res.status == "processed" and res.outcomes[0].result == "filed"
    assert ctx.ledger.messages.get(res.message_row_id)["status"] == "processed"


def owner_forward() -> bytes:
    """The owner's inline forward whose header block prints a date with no zone."""
    body = (
        "---------- Forwarded message ---------\nFrom: Example Air <trips@air.example>\n"
        "Date: Mon, Oct 19, 2026 at 9:30 AM\nSubject: Your itinerary\n\nConfirmation ZZTEST"
    )
    return make_eml(
        from_="alex@example.com",
        to="trips@example.com",
        subject="Fwd: Your itinerary",
        date=D,
        text=body,
    ).as_bytes()


def test_the_pipeline_reads_a_forward_date_in_the_configured_zone(tmp_path, monkeypatch):
    """A zone the settings name, not a built-in default, dates the message."""
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    ctx.settings = dataclasses.replace(
        ctx.settings,
        tz="America/Denver",
        allowed_senders=("alex@example.com",),
        mail_trust="mailbox",
    )
    res = runmod.process_raw(ctx, owner_forward(), source="imap", uid=1)
    assert ctx.ledger.messages.get(res.message_row_id)["email_date"] == "2026-10-19T09:30:00-06:00"


def test_poll_reports_how_long_it_took(tmp_path, monkeypatch, caplog):
    import logging


    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    caplog.set_level(logging.INFO, logger="tripbox.run")
    summary = runmod.poll(ctx, FakeInbox(messages={}))
    assert summary.fetched == 0 and 0.0 <= summary.seconds < 60.0
    done = [r.getMessage() for r in caplog.records if "poll done" in r.getMessage()]
    assert done and f"in {summary.seconds:.1f}s" in done[-1]


def test_an_auto_forwarded_flight_from_an_unknown_sender_is_filed_and_marked(
    tmp_path, monkeypatch
):
    """Filter-forwarded mail from a stranger passes the gate (Gmail did forward it) and is filed,
    but may not reach the flight log on its own: the owner's flight waits there, a check open on
    it, until the owner has looked. The same mail from the airline's own domain goes through."""
    from test_normalize import AR_FWD

    caf = {"Return-Path": "<alex+caf_=trips=example.com@example.com>", **AR_FWD}
    objs = [flight_obj("ALEX SAMPLE", "1A", dep="2027-06-22T08:20:00-05:00")]
    ctx, tg = make_ctx(tmp_path / "stranger", monkeypatch, objs)
    stranger = make_eml(
        from_='"delta.com" <x@evil.example>',
        subject="Reservation confirmation",
        date=D,
        text="x",
        headers=caf,
    )
    res = runmod.process_raw(ctx, stranger.as_bytes(), source="imap", uid=7)
    (o,) = res.outcomes
    assert res.status == "processed" and (o.result, o.sync) == ("filed", "blocked")
    assert o.checks == ("unverified_sender",) and ctx.airtrail.flights == []
    assert ctx.airtrail.sent == []
    (lines, _), (card, markup) = tg.sent
    assert "not sent to your flight log until checked" in lines
    filed = ctx.trips.item(o.trip_item_id)
    assert markup == check_keyboard(filed, flight_log=ctx.trips.airtrail_on())
    assert checks.LABELS["unverified_sender"] in card
    assert "known travel sender" not in str(filed.payload)
    assert checks.LABELS["unverified_sender"] not in str(filed.payload)
    assert filed.payload["raised"]
    ctx, tg = make_ctx(tmp_path / "airline", monkeypatch, objs)
    airline = make_eml(
        from_="Delta <DeltaAirLines@t.delta.com>",
        subject="Your Flight Receipt",
        date=D,
        text="y",
        headers=caf,
        message_id="<real@delta>",
    )
    res = runmod.process_raw(ctx, airline.as_bytes(), source="imap", uid=8)
    (o,) = res.outcomes
    assert res.status == "processed" and (o.result, o.sync, o.checks) == ("filed", "synced", ())
    assert len(ctx.airtrail.flights) == 1


def a_strangers_forward() -> bytes:
    """A receipt a filter forwarded from a sender no list vouches for: it passes the gate."""
    from test_normalize import AR_FWD

    caf = {"Return-Path": "<alex+caf_=trips=example.com@example.com>", **AR_FWD}
    return make_eml(
        from_='"delta.com" <x@evil.example>',
        subject="Reservation confirmation",
        date=D,
        text="x",
        headers=caf,
    ).as_bytes()


def test_an_unvouched_senders_flight_read_again_is_still_held_back(tmp_path, monkeypatch):
    """The gate's verdict on a message is kept with it (`origin_ok`), and says nothing of who
    sent it. A stranger's receipt whose first reading did not finish — it failed, or it waited
    for a model — and that is read again holds the owner's flight back as a first reading
    would: the check is open and nothing is sent."""
    objs = [flight_obj("ALEX SAMPLE", "1A")]
    stranger = a_strangers_forward()
    ctx, _ = make_ctx(tmp_path / "failed", monkeypatch, objs)
    filing = ctx.trips.upsert_item

    def broken(*args, **kw):
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(ctx.trips, "upsert_item", broken)
    first = runmod.process_raw(ctx, stranger, source="imap", uid=7)
    assert first.status == "failed"
    monkeypatch.setattr(ctx.trips, "upsert_item", filing)
    ctx2, _ = make_ctx(tmp_path / "waiting", monkeypatch, [])
    waited = runmod.process_raw(ctx2, stranger, source="imap", uid=7)
    assert waited.status == "waiting"
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: objs)
    for c, earlier in ((ctx, first), (ctx2, waited)):
        assert c.ledger.messages.get(earlier.message_row_id)["origin_ok"] == 1
        again = runmod.process_raw(c, stranger, source="replay")
        assert again.message_row_id == earlier.message_row_id, earlier.status
        (o,) = again.outcomes
        assert (o.result, o.sync) == ("filed", "blocked"), earlier.status
        assert o.checks == ("unverified_sender",), earlier.status
        assert c.airtrail.sent == [] and c.airtrail.flights == [], earlier.status


def test_a_forced_replay_of_a_strangers_receipt_that_changes_the_booking_holds_it_back(
    tmp_path, monkeypatch
):
    """The owner's own receipt files the owner's flight and sends it; a stranger's forward of the
    same receipt changes nothing, so it raises nothing. Read again by force, it now names a
    companion too: the change comes from that stranger, so the owner's flight is held back with
    the check open, and the companion is not sent."""
    alone = [flight_obj("ALEX SAMPLE", "1A")]
    ctx, _ = make_ctx(tmp_path, monkeypatch, alone)
    (mine,) = runmod.process_raw(ctx, owners_receipt(), source="imap", uid=1).outcomes
    assert (mine.result, mine.sync) == ("filed", "synced")
    stranger = a_strangers_forward()
    (copy,) = runmod.process_raw(ctx, stranger, source="imap", uid=2).outcomes
    assert (copy.result, copy.trip_item_id, copy.sync) == ("duplicate", mine.trip_item_id, "synced")
    assert copy.checks == ()
    both = [*alone, flight_obj("Sam Sample", "1B")]
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: both)
    again = runmod.process_raw(ctx, stranger, source="replay", force=True)
    (o,) = again.outcomes
    assert (o.action.reason, o.trip_item_id) == ("add_passengers", mine.trip_item_id)
    assert (o.sync, o.checks) == ("blocked", ("unverified_sender",))
    assert [r[0] for r in ctx.airtrail.sent] == ["create"]


def three_receipts(monkeypatch) -> list[EmailMessage]:
    """Three receipts, each for a flight of the owner's of its own, and an extractor that reads
    a flight from a receipt's own text only, as a reader of text and attachments does: what it
    is handed empty gives it nothing."""
    flights = [
        flight_obj("ALEX SAMPLE", "1A"),
        flight_obj("ALEX SAMPLE", "2B", number="9330", code="XYZ789"),
        flight_obj("ALEX SAMPLE", "14A", number="9123", code="QXR987"),
    ]
    words = ("first receipt", "second receipt", "third receipt")
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda data, suffix, ctx_date: [
            f for word, f in zip(words, flights, strict=True) if word.encode() in data
        ],
    )
    return [
        make_eml(
            from_="a@t.delta.com",
            subject=f"Receipt {n}",
            date=D,
            text=word,
            message_id=f"<i{n}>",
        )
        for n, word in enumerate(words, 1)
    ]


def test_forward_as_attachment_with_several_emails_files_each(tmp_path, monkeypatch):
    """The owner forwards three receipts as attachments of one email: each is read as the
    owner's own, whoever its From says sent it, so each files its flight and goes to the flight
    log. The same three attached to a message a filter forwarded from a sender no list vouches
    for are not the owner's: only the first is read, as that sender's, and held back."""
    ctx, _ = make_ctx(tmp_path / "owner", monkeypatch, [])
    first, *rest = three_receipts(monkeypatch)
    outer = make_eml(
        from_="alex@example.com",
        subject="Fwd",
        date=D,
        text="see attached",
        headers=AR,
        rfc822=first,
    )
    for inner in rest:
        outer.add_attachment(inner)
    res = runmod.process_raw(ctx, outer.as_bytes(), source="imap", uid=9)
    assert [r.normalized.message_id for r in res.children] == ["<i2>", "<i3>"]
    every = [res, *res.children]
    assert [r.status for r in every] == ["processed"] * 3
    assert [(o.result, o.sync, o.checks) for r in every for o in r.outcomes] == [
        ("filed", "synced", ())
    ] * 3
    assert len({o.trip_item_id for r in every for o in r.outcomes}) == 3
    assert len(ctx.airtrail.flights) == 3
    rows = ctx.ledger.conn.execute("SELECT message_id, status FROM messages ORDER BY id").fetchall()
    assert [(r["message_id"], r["status"]) for r in rows] == [
        ("<i1>", "processed"),
        ("<i2>", "processed"),
        ("<i3>", "processed"),
    ]
    from test_normalize import AR_FWD

    caf = {"Return-Path": "<alex+caf_=trips=example.com@example.com>", **AR_FWD}
    ctx, _ = make_ctx(tmp_path / "stranger", monkeypatch, [])
    first, *rest = three_receipts(monkeypatch)
    outer = make_eml(
        from_='"delta.com" <x@evil.example>',
        subject="Fwd",
        date=D,
        text="see attached",
        headers=caf,
        rfc822=first,
    )
    for inner in rest:
        outer.add_attachment(inner)
    for again, result in ((False, "filed"), (True, "duplicate")):
        res = runmod.process_raw(ctx, outer.as_bytes(), source="imap", uid=10, force=again)
        assert res.children == [], again
        (o,) = res.outcomes
        assert (o.result, o.sync, o.checks) == (result, "blocked", ("unverified_sender",)), again
        assert "unverified_sender" in checks.codes(res.items[0]), again
        assert ctx.airtrail.sent == [], again


def owner_forwarding(receipts: list[EmailMessage]) -> bytes:
    """The owner's message carrying `receipts` as attachments: it reads the first as its own,
    and hands the others back to be read as emails of their own."""
    first, *rest = receipts
    outer = make_eml(
        from_="alex@example.com",
        subject="Fwd",
        date=D,
        text="see attached",
        headers=AR,
        rfc822=first,
    )
    for inner in rest:
        outer.add_attachment(inner)
    return outer.as_bytes()


def test_attached_emails_are_filed_when_the_first_was_handled_before(tmp_path, monkeypatch):
    """The first receipt came on its own and was filed. Later the owner forwards it with two
    more as attachments: the first is set aside as the same email, and the other two are filed
    each as its own message."""
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    receipts = three_receipts(monkeypatch)
    alone = runmod.process_raw(ctx, owner_forwarding(receipts[:1]), source="imap", uid=8)
    assert alone.status == "processed"
    res = runmod.process_raw(ctx, owner_forwarding(receipts), source="imap", uid=9)
    assert (res.status, res.same_as, res.label) == (
        "ignored",
        alone.message_row_id,
        "tripbox/processed",
    )
    assert [(r.normalized.message_id, r.status) for r in res.children] == [
        ("<i2>", "processed"),
        ("<i3>", "processed"),
    ]
    assert [o.result for r in res.children for o in r.outcomes] == ["filed", "filed"]
    row = ctx.ledger.messages.get(res.message_row_id)
    assert (row["status"], row["error"]) == ("ignored", res.skipped_reason)
    assert "Message-ID match" in row["error"] and len(ctx.airtrail.flights) == 3
    assert runmod.resume_waiting(ctx) == 0


def test_attached_emails_left_unfiled_beside_one_handled_before_are_taken_up(
    tmp_path, monkeypatch
):
    """As above, but the run stops while the second attached email is read: the message stays
    unfinished, and the resume pass files what was left and finishes it as the same email."""
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    receipts = three_receipts(monkeypatch)
    alone = runmod.process_raw(ctx, owner_forwarding(receipts[:1]), source="imap", uid=8)
    reads = pl.kitinerary.run_extractor

    def stopped(data, suffix, ctx_date):
        if b"second receipt" in data:
            raise SystemExit(1)
        return reads(data, suffix, ctx_date)

    monkeypatch.setattr(pl.kitinerary, "run_extractor", stopped)
    with pytest.raises(SystemExit):
        runmod.process_raw(ctx, owner_forwarding(receipts), source="imap", uid=9)
    row = ctx.ledger.conn.execute("SELECT * FROM messages ORDER BY id").fetchall()[1]
    assert (row["status"], row["error"]) == ("ignored", runmod.ATTACHED_UNFILED)
    monkeypatch.setattr(pl.kitinerary, "run_extractor", reads)
    assert runmod.resume_waiting(ctx) == 1
    row = ctx.ledger.messages.get(row["id"])
    assert row["status"] == "ignored" and f"message #{alone.message_row_id}" in row["error"]
    assert len(ctx.airtrail.flights) == 3


def test_the_resume_pass_takes_up_a_message_whose_attached_emails_were_left(
    tmp_path, monkeypatch
):
    """The owner forwards three receipts as attachments, and the run is stopped while the second
    is read: the message is left with its attached emails unfiled. Its bytes never come again
    (the mailbox let it go), so the resume pass takes it up — with no model, which it does not
    need — and files the two left; the message is finished, and nothing is left to take up."""
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    raw = owner_forwarding(three_receipts(monkeypatch))
    reads = pl.kitinerary.run_extractor

    def stopped(data, suffix, ctx_date):
        if b"second receipt" in data:
            raise SystemExit(1)
        return reads(data, suffix, ctx_date)

    monkeypatch.setattr(pl.kitinerary, "run_extractor", stopped)
    with pytest.raises(SystemExit):
        runmod.process_raw(ctx, raw, source="imap", uid=9)
    rows = ctx.ledger.messages.all()
    assert [(r["status"], r["error"]) for r in rows] == [
        ("processed", runmod.ATTACHED_UNFILED),
        ("archived", None),
    ]
    monkeypatch.setattr(pl.kitinerary, "run_extractor", reads)
    assert ctx.model is None
    assert runmod.resume_waiting(ctx) == 1
    rows = ctx.ledger.messages.all()
    assert [(r["status"], r["error"]) for r in rows] == [("processed", None)] * 3
    logged = [[x["outcome"] for x in ctx.ledger.items.by_message(r["id"])] for r in rows]
    assert logged == [["filed"]] * 3 and len(ctx.airtrail.flights) == 3
    assert runmod.resume_waiting(ctx) == 0


def test_an_attached_email_that_cannot_be_filed_is_said_once(tmp_path, monkeypatch):
    """One of the emails a message carries cannot be kept (its archive file cannot be written),
    so its filing fails before it has a row: the others are filed, one `failure` notice says so
    in plain words, and the message stays unfinished. The resume pass takes it up again: while
    the email still cannot be kept nothing more is said; once it can, it is filed and the
    message is finished."""
    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    raw = owner_forwarding(three_receipts(monkeypatch))
    keep = runmod.archive

    def full(root, data, received, uid=None):
        if b"third receipt" in data and b"first receipt" not in data:  # that email alone
            raise OSError("No space left on device")
        return keep(root, data, received, uid)

    monkeypatch.setattr(runmod, "archive", full)
    res = runmod.process_raw(ctx, raw, source="imap", uid=9)
    assert [r.status for r in (res, *res.children)] == ["processed", "processed", "failed"]
    assert res.children[1].message_row_id is None
    said = "tripbox could not file an email attached to a message. It is kept and read again."

    def told() -> list[str]:
        return [text for text, _ in tg.sent if "could not file" in text]

    assert told() == [said]
    assert not any("No space" in text for text, _ in tg.sent)
    unfinished = ("processed", runmod.ATTACHED_UNFILED)
    row = ctx.ledger.messages.get(res.message_row_id)
    assert (row["status"], row["error"]) == unfinished
    assert runmod.resume_waiting(ctx) == 1
    row = ctx.ledger.messages.get(res.message_row_id)
    assert (row["status"], row["error"]) == unfinished and told() == [said]
    monkeypatch.setattr(runmod, "archive", keep)
    assert runmod.resume_waiting(ctx) == 1
    rows = ctx.ledger.messages.all()
    assert [(r["status"], r["error"]) for r in rows] == [("processed", None)] * 3
    assert told() == [said] and len(ctx.airtrail.flights) == 3


def test_a_message_whose_attached_emails_never_file_is_failed_after_five_tries_and_told_once(
    tmp_path, monkeypatch
):
    """An email attached to a message can never be kept: the resume pass takes the message up
    again at each poll, MAX_RESUMES times, then fails it with one `failure` notice and counts it
    no longer; the next pass takes nothing up, and nothing more is said."""
    assert runmod.MAX_RESUMES == 5
    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    raw = owner_forwarding(three_receipts(monkeypatch))
    keep = runmod.archive

    def full(root, data, received, uid=None):
        if b"third receipt" in data and b"first receipt" not in data:  # that email alone
            raise OSError("No space left on device")
        return keep(root, data, received, uid)

    monkeypatch.setattr(runmod, "archive", full)
    res = runmod.process_raw(ctx, raw, source="imap", uid=9)
    first = list(tg.sent)

    def tries() -> dict:
        return json.loads(ctx.ledger.kv.get(runmod.KV_RESUME_TRIES) or "{}")

    unfinished = ("processed", runmod.ATTACHED_UNFILED)
    for n in range(1, runmod.MAX_RESUMES):
        assert runmod.resume_waiting(ctx) == 1
        row = ctx.ledger.messages.get(res.message_row_id)
        assert (row["status"], row["error"]) == unfinished, n
        assert tries() == {str(res.message_row_id): n}
    assert tg.sent == first  # nothing more said while it is tried
    assert runmod.resume_waiting(ctx) == 1  # the fifth
    row = ctx.ledger.messages.get(res.message_row_id)
    assert row["status"] == "failed"
    told = tg.sent[len(first) :]
    assert len(told) == 1 and "could not process" in told[0][0]
    assert "5 tries" in told[0][0] and "No space" not in told[0][0]
    assert tries() == {}
    assert runmod.resume_waiting(ctx) == 0 and tg.sent[len(first) :] == told
    # its two filed emails stay filed
    assert len(ctx.airtrail.flights) == 2


def test_a_take_up_killed_mid_way_still_counts(tmp_path, monkeypatch):
    """The resume pass counts a take-up before it starts it: one the poll's kill ends mid-way
    (here, an exit while it reads) still counts, so a message whose reading hangs every time
    is failed all the same, told once, without being taken up again."""
    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    raw = owner_forwarding(three_receipts(monkeypatch))
    keep = runmod.archive

    def full(root, data, received, uid=None):
        if b"third receipt" in data and b"first receipt" not in data:  # that email alone
            raise OSError("No space left on device")
        return keep(root, data, received, uid)

    monkeypatch.setattr(runmod, "archive", full)
    res = runmod.process_raw(ctx, raw, source="imap", uid=9)
    first = list(tg.sent)
    taken = []

    def killed(*args, **kwargs):
        taken.append(1)
        raise SystemExit(-9)

    monkeypatch.setattr(runmod, "process_raw", killed)
    for n in range(1, runmod.MAX_RESUMES + 1):
        with pytest.raises(SystemExit):
            runmod.resume_waiting(ctx)
        tries = json.loads(ctx.ledger.kv.get(runmod.KV_RESUME_TRIES))
        assert tries == {str(res.message_row_id): n}
    assert len(taken) == runmod.MAX_RESUMES and tg.sent == first
    assert runmod.resume_waiting(ctx) == 0  # taken up five times: failed, not taken up again
    assert len(taken) == runmod.MAX_RESUMES
    assert ctx.ledger.messages.get(res.message_row_id)["status"] == "failed"
    told = tg.sent[len(first) :]
    assert len(told) == 1 and "5 tries" in told[0][0]
    assert json.loads(ctx.ledger.kv.get(runmod.KV_RESUME_TRIES)) == {}


def mailbox_mode(ctx: runmod.Context) -> runmod.Context:
    """The context with `TRIPBOX_MAIL_TRUST=mailbox`: no authentication check."""
    ctx.settings = dataclasses.replace(ctx.settings, mail_trust="mailbox")
    return ctx


def gmail_mode(ctx: runmod.Context) -> runmod.Context:
    """The context with `TRIPBOX_MAIL_TRUST=gmail`: Gmail's own authentication results."""
    ctx.settings = dataclasses.replace(ctx.settings, mail_trust="gmail")
    return ctx


def read_once_a_model_is_set(monkeypatch, reservations: list[dict], *, noise: str = "") -> None:
    """From here on a message is read as `reservations` by the model once one is set (the
    context's `model`) and waits while none is; one whose subject is `noise` is read as nothing
    to book by the structured-data reader."""
    answer = Extraction.model_validate(
        {"document_type": "booking", "confidence": 1.0, "reservations": reservations}
    )

    def parse(nm, model):
        if noise and nm.subject == noise:
            return pl.ParsedMessage(Extraction(document_type="marketing"), parser="kitinerary")
        if model is None:
            return pl.ParsedMessage(Extraction(document_type="other"), parser="none")
        return pl.ParsedMessage(answer.model_copy(deep=True), parser="haiku")

    monkeypatch.setattr(runmod, "parse_message", parse)


def forged_owner_mail(entry: corpus.Entry, **kw) -> EmailMessage:
    """Mail about `entry` From the owner's address that nothing authenticates: `mailbox` mode
    reads it, `gmail` mode would reject it."""
    from test_filed import SENDER

    return make_eml(
        from_=SENDER,
        subject=f"Fwd: {entry.name}",
        date=corpus.sent(entry),
        text=entry.name,
        message_id=f"<{entry.name}-forged@trips.example>",
        **kw,
    )


def booked(ctx: runmod.Context, row_id: int) -> list:
    """The bookings message `row_id` filed or changed, as the store now holds them."""
    return [ctx.trips.item(r["trip_item_id"]) for r in ctx.ledger.items.by_message(row_id)]


def test_a_waiting_mailbox_message_read_after_a_switch_to_gmail_is_still_nobodys(monkeypatch):
    """`mailbox` mode accepts a forged owner's message that nothing authenticated, and it waits
    for a model. The owner switches to `gmail` and sets a model up: the resume pass reads it
    without asking the check again, and still vouches for nobody, so the owner-named flight is
    held from the flight log with its check."""
    from test_filed import filing, owners_flight

    entry, flight = owners_flight()
    raw = forged_owner_mail(entry).as_bytes()
    read_once_a_model_is_set(monkeypatch, [flight])
    with filing() as ctx:
        first = runmod.process_raw(mailbox_mode(ctx), raw, source="imap", uid=1)
        assert first.status == "waiting"
        gmail_mode(ctx).model = object()  # a model is set up
        assert runmod.resume_waiting(ctx) == 1
        (ti,) = booked(ctx, first.message_row_id)
        assert (ti.checks, ti.sync_state) == (("unverified_sender",), "blocked")
        assert ctx.airtrail.sent == []
        row = ctx.ledger.messages.get(first.message_row_id)
        assert (row["status"], row["origin_ok"]) == ("processed", runmod.ORIGIN_UNAUTHENTICATED)


def test_an_attached_emails_waiting_row_read_on_its_own_after_a_switch_is_still_nobodys(
    monkeypatch,
):
    """In `mailbox` mode a forged owner's message carries a note and a receipt that claims the
    owner's From and Gmail's stamp, both the sender's own text: the receipt is read ungated and
    waits for a model. After a switch to `gmail` the resume pass reads its row on its own, and
    its flight is still nobody's: held, with its check."""
    from test_filed import SENDER, filing, owners_flight

    entry, flight = owners_flight()
    sent = corpus.sent(entry)
    note = make_eml(from_=SENDER, subject="A note", date=sent, text="the receipt is below")
    receipt = make_eml(from_=SENDER, subject="Receipt", date=sent, text=entry.name, headers=AR)
    outer = forged_owner_mail(entry, rfc822=note)
    outer.add_attachment(receipt)
    read_once_a_model_is_set(monkeypatch, [flight], noise="A note")
    with filing() as ctx:
        res = runmod.process_raw(mailbox_mode(ctx), outer.as_bytes(), source="imap", uid=1)
        assert (res.status, [c.status for c in res.children]) == ("ignored", ["waiting"])
        attached = res.children[0].message_row_id
        gmail_mode(ctx).model = object()
        assert runmod.resume_waiting(ctx) == 1  # the receipt's row alone
        (ti,) = booked(ctx, attached)
        assert (ti.checks, ti.sync_state) == (("unverified_sender",), "blocked")
        assert ctx.airtrail.sent == []
        origins = [ctx.ledger.messages.get(i)["origin_ok"] for i in (res.message_row_id, attached)]
        assert origins == [runmod.ORIGIN_UNAUTHENTICATED] * 2


def test_an_email_the_owner_attached_stays_theirs_when_its_waiting_row_is_read_alone(
    monkeypatch,
):
    """In `gmail` mode the owner's authenticated message carries a note and a receipt from a
    sender no list vouches for: the owner vouches for it, and it waits for a model. The resume
    pass reads its row on its own, and its flight is still the owner's: no check, and sent to
    the flight log."""
    from test_filed import SENDER, filing, owners_flight

    entry, flight = owners_flight()
    sent = corpus.sent(entry)
    note = make_eml(from_=SENDER, subject="A note", date=sent, text="the receipt is below")
    receipt = make_eml(
        from_="bookings@unknown-sender.example", subject="Receipt", date=sent, text=entry.name
    )
    outer = make_eml(
        from_=SENDER, subject="Fwd: two", date=sent, text="see attached", headers=AR, rfc822=note
    )
    outer.add_attachment(receipt)
    read_once_a_model_is_set(monkeypatch, [flight], noise="A note")
    with filing() as ctx:
        res = runmod.process_raw(gmail_mode(ctx), outer.as_bytes(), source="imap", uid=1)
        assert (res.status, [c.status for c in res.children]) == ("ignored", ["waiting"])
        attached = res.children[0].message_row_id
        assert ctx.ledger.messages.get(attached)["origin_ok"] == runmod.ORIGIN_OWNERS
        ctx.model = object()  # a model is set up
        assert runmod.resume_waiting(ctx) == 1  # the receipt's row alone
        (ti,) = booked(ctx, attached)
        assert (ti.checks, ti.sync_state) == ((), "synced")
        assert ctx.ledger.messages.get(attached)["origin_ok"] == runmod.ORIGIN_OWNERS
        # read again by force, on its own, it is still the owner's
        raw = Path(ctx.ledger.messages.get(attached)["archive_path"]).read_bytes()
        again = runmod.process_raw(ctx, raw, source="replay", force=True)
        assert [o.checks for o in again.outcomes] == [()]


def test_a_failed_mailbox_message_replayed_after_a_switch_to_gmail_is_still_nobodys(monkeypatch):
    """A forged owner's message `mailbox` mode accepted fails on its first reading. Replayed
    after a switch to `gmail`, it is not asked the check again and is still nobody's: the
    owner-named flight is filed held from the flight log, with its check."""
    from test_filed import filing, owners_flight

    entry, flight = owners_flight()
    raw = forged_owner_mail(entry).as_bytes()
    read_once_a_model_is_set(monkeypatch, [flight])

    def broken(*args, **kw):
        raise RuntimeError("the disk is full")

    with filing() as ctx:
        mailbox_mode(ctx).model = object()
        with monkeypatch.context() as m:
            m.setattr(ctx.executor, "execute", broken)
            first = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert first.status == "failed"
        again = runmod.process_raw(gmail_mode(ctx), raw, source="replay")
        assert again.message_row_id == first.message_row_id
        (o,) = again.outcomes
        assert (o.result, o.sync, o.checks) == ("filed", "blocked", ("unverified_sender",))
        assert ctx.airtrail.sent == []
        row = ctx.ledger.messages.get(first.message_row_id)
        assert row["origin_ok"] == runmod.ORIGIN_UNAUTHENTICATED


def stamped_receipt(entry: corpus.Entry) -> EmailMessage:
    """A receipt about `entry` that claims the owner's From and Gmail's stamp for the owner's
    domain, as a sender can type them both."""
    from test_filed import SENDER

    sent = corpus.sent(entry)
    return make_eml(from_=SENDER, subject="Receipt", date=sent, text=entry.name, headers=AR)


@pytest.mark.parametrize("how", ["reread", "refetch", "redelivery", "unwritten"])
def test_mail_read_with_nothing_authenticated_is_nobodys_however_it_is_read_again(
    monkeypatch, how
):
    """`mailbox` mode accepts a forged owner's message carrying the owner's own Gmail stamp, with
    nothing authenticated, and its first reading fails. After a switch to `gmail` it is read
    again by `replay --reread`, by a backfill that fetches it under another UID once the mailbox
    renumbered, or by a new delivery of the same bytes: it is not asked the check of its own
    headers, so its owner-named flight is nobody's, held with its check. So is a receipt attached
    to such a message whose reading stopped before its verdict was written (`unwritten`),
    replayed on its own: its row says from the start that nothing authenticated it."""
    from test_filed import SENDER, filing, owners_flight

    entry, flight = owners_flight()
    read_once_a_model_is_set(monkeypatch, [flight], noise="A note")
    with filing() as ctx:
        mailbox_mode(ctx).model = object()
        if how == "unwritten":
            note = make_eml(
                from_=SENDER, subject="A note", date=corpus.sent(entry), text="see below"
            )
            outer = forged_owner_mail(entry, rfc822=note)
            outer.add_attachment(stamped_receipt(entry))
            raw = outer.as_bytes()
            s = ctx.settings
            (attached,) = runmod.normalize(raw, s.allowed_senders, require_auth=False).extra_rfc822
            real = runmod.normalize

            def broken(data, *args, **kw):
                if data == attached:
                    raise RuntimeError("the parser broke")
                return real(data, *args, **kw)

            with monkeypatch.context() as m:
                m.setattr(runmod, "normalize", broken)
                (child,) = runmod.process_raw(ctx, raw, source="imap", uid=1).children
            row_id = child.message_row_id
        else:
            with monkeypatch.context() as m:
                m.setattr(ctx.executor, "execute", lambda *a, **kw: 1 / 0)
                first = runmod.process_raw(
                    ctx, forged_owner_mail(entry, headers=AR).as_bytes(), source="imap", uid=1
                )
            row_id = first.message_row_id
        row = ctx.ledger.messages.get(row_id)
        assert (row["status"], row["origin_ok"]) == ("failed", runmod.ORIGIN_UNAUTHENTICATED)
        gmail_mode(ctx)
        again = Path(row["archive_path"]).read_bytes()
        if how == "reread":
            runmod.process_raw(ctx, again, source="replay", reread=True)
        elif how == "unwritten":
            runmod.process_raw(ctx, again, source="replay")
        elif how == "refetch":
            assert runmod.backfill(ctx, FakeInbox(messages={7: again}), [7]).errors == 0
        else:
            assert runmod.poll(ctx, FakeInbox(messages={7: again}).new_to(ctx.ledger)).errors == 0
        row = ctx.ledger.messages.get(row_id)
        assert (row["status"], row["origin_ok"]) == ("processed", runmod.ORIGIN_UNAUTHENTICATED)
        (ti,) = booked(ctx, row_id)
        assert (ti.checks, ti.sync_state) == (("unverified_sender",), "blocked")
        assert ctx.airtrail.sent == []


def test_an_email_attached_to_mail_gmail_accepted_is_nobodys_in_mailbox_mode(monkeypatch):
    """The owner's own message, which `gmail` mode authenticated, carries a stamped receipt;
    filing stops before the receipt is reached. After a switch to `mailbox` the message comes
    again: the receipt it still carries is read as nobody's, whatever the message's acceptance
    in `gmail` mode said, so its owner-named flight is held with its check."""
    from test_filed import SENDER, filing, owners_flight

    entry, flight = owners_flight()
    read_once_a_model_is_set(monkeypatch, [flight], noise="A note")
    note = make_eml(
        from_=SENDER, subject="A note", date=corpus.sent(entry), text="see below", headers=AR
    )
    outer = make_eml(
        from_=SENDER, subject="Fwd", date=corpus.sent(entry), text="x", headers=AR, rfc822=note
    )
    outer.add_attachment(stamped_receipt(entry))
    raw = outer.as_bytes()
    with filing() as ctx:
        gmail_mode(ctx).model = object()
        real = runmod.archive

        def stopped(directory, data, *args):
            if data != raw:
                raise SystemExit(1)
            return real(directory, data, *args)

        with monkeypatch.context() as m, pytest.raises(SystemExit):
            m.setattr(runmod, "archive", stopped)
            runmod.process_raw(ctx, raw, source="imap", uid=1)
        (parent,) = ctx.ledger.messages.all()
        assert (parent["status"], parent["origin_ok"]) == ("ignored", runmod.ORIGIN_ACCEPTED)
        res = runmod.process_raw(mailbox_mode(ctx), raw, source="imap", uid=1)
        (child,) = res.children
        row = ctx.ledger.messages.get(child.message_row_id)
        assert (row["status"], row["origin_ok"]) == ("processed", runmod.ORIGIN_UNAUTHENTICATED)
        (o,) = child.outcomes
        assert (o.result, o.sync, o.checks) == ("filed", "blocked", ("unverified_sender",))
        assert ctx.airtrail.sent == []


def test_a_mailbox_mode_flight_waits_for_a_look_before_the_flight_log(monkeypatch):
    """With no authentication check nobody is vouched for, the owner's own address included: the
    owner's flight mailed in is filed at once and held from the flight log, its unverified-sender
    check open, until the owner looks; the next pass then sends it. With no flight log kept the
    check is not shown, and a flight log switched on later still holds a flight nobody looked
    at."""
    from helpers.flightlog import Scripted, seed
    from test_filed import deliver, filing, owners_flight

    from tripbox.write.lifecycle import note_airtrail

    entry, flight = owners_flight()
    with filing() as ctx:
        mailbox_mode(ctx)
        got = deliver(ctx, monkeypatch, entry, [flight])
        (o,) = got.outcomes
        assert (got.status, o.result, o.sync) == ("processed", "filed", "blocked")
        assert o.checks == ("unverified_sender",)
        assert "unverified_sender" in checks.codes(got.items[0])
        runmod.sync(ctx)
        assert ctx.airtrail.sent == []
        looked = ctx.trips.looks_right(o.trip_item_id)
        assert (looked.checks, looked.sync_state) == ((), "pending")
        runmod.sync(ctx)
        assert [r[0] for r in ctx.airtrail.sent] == ["create"]
        assert ctx.trips.item(o.trip_item_id).sync_state == "synced"
    with filing(flight_log=False) as ctx:
        mailbox_mode(ctx)
        (o,) = deliver(ctx, monkeypatch, entry, [flight]).outcomes
        assert (o.result, o.sync, o.checks) == ("filed", "off", ())
        # switched on the day the flight leaves: it is to come, so it is not held out as past
        leaves = dt.date.fromisoformat(ctx.trips.item(o.trip_item_id).payload["date_local"])
        assert note_airtrail(ctx.trips, ctx.ledger, on=True, today=leaves) == "on"
        seed(ctx.ledger, corpus.load(corpus.default_dir()))
        ctx.airtrail = log = Scripted(ctx.ledger)
        ctx.syncer = Syncer(
            store=ctx.trips, ledger=ctx.ledger, book=ctx.trips.book, client=log, refs=None,
            dry_run=False,
        )
        held = ctx.trips.item(o.trip_item_id)
        assert (held.checks, held.sync_state, held.sync_hold) == (
            ("unverified_sender",),
            "blocked",
            None,
        )
        runmod.sync(ctx)
        assert log.sent == []
        assert ctx.trips.looks_right(o.trip_item_id).sync_state == "pending"
        runmod.sync(ctx)
        assert [r[0] for r in log.sent] == ["create"]


def test_an_owner_forward_with_attached_emails_in_mailbox_mode_vouches_for_none(
    tmp_path, monkeypatch
):
    """With no authentication check the owner's message is nobody's, and so is what it carries:
    each attached email is read without the sender check, whatever its own From says, and each
    flight it files waits for the owner's look before the flight log. Read again on its own, an
    attachment is still nobody's. Handed in by the owner (an ingest), the same message and its
    attachments are the owner's own and go to the flight log."""
    ctx = mailbox_mode(make_ctx(tmp_path / "mail", monkeypatch, [])[0])
    first, *rest = three_receipts(monkeypatch)
    outer = make_eml(
        from_="alex@example.com", subject="Fwd", date=D, text="see attached", rfc822=first
    )
    for inner in rest:
        outer.add_attachment(inner)
    res = runmod.process_raw(ctx, outer.as_bytes(), source="imap", uid=9)
    every = [res, *res.children]
    assert [r.status for r in every] == ["processed"] * 3
    assert [(o.result, o.sync, o.checks) for r in every for o in r.outcomes] == [
        ("filed", "blocked", ("unverified_sender",))
    ] * 3
    assert ctx.airtrail.sent == []
    rows = ctx.ledger.conn.execute("SELECT message_id, origin_ok FROM messages ORDER BY id")
    assert [(r["message_id"], r["origin_ok"]) for r in rows] == [
        ("<i1>", runmod.ORIGIN_UNAUTHENTICATED),
        ("<i2>", runmod.ORIGIN_UNAUTHENTICATED),
        ("<i3>", runmod.ORIGIN_UNAUTHENTICATED),
    ]
    attached = ctx.ledger.messages.get(res.children[0].message_row_id)
    again = runmod.process_raw(
        ctx, Path(attached["archive_path"]).read_bytes(), source="replay", force=True
    )
    (o,) = again.outcomes
    assert "unverified_sender" in checks.codes(again.items[0]) and o.sync == "blocked"
    assert ctx.airtrail.sent == []
    ctx = mailbox_mode(make_ctx(tmp_path / "ingest", monkeypatch, [])[0])
    three_receipts(monkeypatch)
    res = runmod.process_raw(ctx, outer.as_bytes(), source="cli", trusted=True)
    assert [(o.result, o.sync, o.checks) for r in [res, *res.children] for o in r.outcomes] == [
        ("filed", "synced", ())
    ] * 3
    assert len(ctx.airtrail.flights) == 3


def test_persistent_poll_failures_alert_once_and_announce_recovery(tmp_path, monkeypatch):
    import datetime as dt


    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
    ctx.ledger.kv.set("last_poll_ok", old)
    runmod.poll_failed(ctx, RuntimeError("login failed: [AUTHENTICATIONFAILED]"))
    assert len(tg.sent) == 1 and "AUTHENTICATIONFAILED" in tg.sent[0][0]
    runmod.poll_failed(ctx, RuntimeError("login failed again"))
    assert len(tg.sent) == 1  # one alert, not one every five minutes
    runmod.poll(ctx, FakeInbox(messages={}))
    assert len(tg.sent) == 2 and "again" in tg.sent[1][0].lower()
    assert ctx.ledger.kv.get("poll_alerted") in (None, "", "0")
    # a fresh failure right after a good poll is not worth a message yet
    runmod.poll_failed(ctx, RuntimeError("blip"))
    assert len(tg.sent) == 2


def test_a_failed_paste_can_be_pasted_again_and_replayed(tmp_path, monkeypatch):
    """A paste whose filing failed is not 'seen before': pasting it again re-runs it, and
    `replay` feeds the archived text back through the text path with its original trust instead
    of rejecting it at the email gate."""
    ctx, tg = make_ctx(
        tmp_path, monkeypatch, [flight_obj("ALEX SAMPLE", "1A", dep="2027-06-22T08:20:00-05:00")]
    )
    filing = ctx.trips.upsert_item

    def broken(*args, **kw):
        raise RuntimeError("the disk is full")

    text = "DL 1234 MSP-PHL 22 Jun 2027 conf ABC123 Alex Sample"
    monkeypatch.setattr(ctx.trips, "upsert_item", broken)
    first = runmod.process_text(ctx, text, source="telegram")
    assert first.status == "failed"
    monkeypatch.setattr(ctx.trips, "upsert_item", filing)
    again = runmod.process_text(ctx, text, source="telegram")
    assert again.status == "processed" and again.message_row_id == first.message_row_id
    done = runmod.process_text(ctx, text, source="telegram")
    assert done.status == "skipped"  # identical and complete: nothing to redo
    other = "DL 9330 MSP-PHL 22 Jun 2027 conf XYZ789 Alex Sample"
    monkeypatch.setattr(  # the fake parser must hand back a different flight for the new text
        pl.kitinerary,
        "run_extractor",
        lambda *a: [
            flight_obj("ALEX SAMPLE", "2B", number="9330", dep="2027-06-22T08:50:00-05:00")
        ],
    )
    monkeypatch.setattr(ctx.trips, "upsert_item", broken)
    failed = runmod.process_text(ctx, other, source="telegram")
    assert failed.status == "failed"
    monkeypatch.setattr(ctx.trips, "upsert_item", filing)
    replayed = runmod.process_raw(ctx, other.encode(), source="replay")
    assert replayed.status == "processed" and replayed.message_row_id == failed.message_row_id


def test_poll_purges_old_deletions_and_never_fails_on_it(tmp_path, monkeypatch):
    from test_planner import items_for

    from tripbox.trips.store import TripStore

    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    st = ctx.trips
    item = items_for(["Alex Sample"])[0]
    ti = st.upsert_item(st.find_or_create(item), item)
    st.remove_item(ti.id)
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=40)).isoformat(timespec="seconds")
    ctx.ledger.trip_items.update(ti.id, removed_at=old)
    runmod.poll(ctx, FakeInbox(messages={}))
    assert st.item(ti.id) is None and ctx.ledger.trips.get(ti.trip_id) is None

    def boom(self, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(TripStore, "purge_deleted", boom)
    assert runmod.poll(ctx, FakeInbox(messages={})).fetched == 0


def test_city_name_airports_resolve_from_codes_printed_in_the_email(tmp_path, monkeypatch):
    """The live path hands the email text to the validator: a receipt whose itinerary block
    prints only cities is filed through the code pair in its baggage table."""
    from tripbox.parse.models import Extraction, Reservation, Segment

    ctx, tg = make_ctx(tmp_path, monkeypatch, [])
    ex = Extraction(
        document_type="booking",
        confidence=0.9,
        reservations=[
            Reservation(
                kind="flight",
                status="confirmed",
                confirmation_code="WZSVYR",
                provider="Delta Air Lines",
                passengers=["LEE EXAMPLE"],
                segments=[
                    Segment(
                        carrier_iata="DL",
                        flight_number="9318",
                        origin="PHILADELPHIA",
                        destination="MINNEAPOLIS",
                        departure_local="2027-10-05T09:10",
                        arrival_local="2027-10-05T11:15",
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(
        runmod, "parse_message", lambda nm, model: pl.ParsedMessage(extraction=ex, parser="haiku")
    )
    m = make_eml(
        from_="alex@example.com",
        subject="Fwd: Your receipt",
        date=D,
        text=(
            "Flight DL 9318\nPHILADELPHIA 09:10 AM  MINNEAPOLIS 11:15 AM\n...\n"
            "Baggage\nTue 5 Oct 2027  PHL-MSP\n"
        ),
        headers=AR,
    )
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=9)
    assert res.status == "processed", [o.action.reason for o in res.outcomes]
    rows = ctx.ledger.items.by_message(res.message_row_id)
    assert [(r["outcome"], r["item_key"]) for r in rows] == [("filed", "DL|9318|2027-10-05|PHL")]
    filed = ctx.trips.item(rows[0]["trip_item_id"])
    payload = filed.payload
    assert (payload["origin"], payload["destination"]) == ("PHL", "MSP")
    assert payload["airports_from_text"] == "PHL-MSP" and payload["passengers"] == ["LEE EXAMPLE"]
    assert filed.sync_state == "off" and ctx.airtrail.requests == []  # not the owner's flight


class CountingModel:
    """A model that reads the plain forward as the owner's one flight and counts its calls."""

    def __init__(self, number="9123"):
        self.calls = 0
        self.number = number
        self.model = "model"

    def extract(self, nm, hint=None):
        self.calls += 1
        leg = Segment(
            carrier_iata="DL",
            flight_number=self.number,
            origin="MSP",
            destination="PHL",
            departure_local="2027-03-12T08:20",
            arrival_local="2027-03-12T11:55",
        )
        res = Reservation(
            kind="flight",
            status="confirmed",
            confirmation_code="QXR987",
            provider="Delta Air Lines",
            passengers=["Alex Sample"],
            segments=[leg],
        )
        return Extraction(document_type="booking", reservations=[res]), Usage(
            "model", 10, 5, 0.01
        )


def _keys(ctx):
    return sorted(
        ti.item_key for trip in ctx.trips.all_trips() for ti in ctx.trips.items(trip.id)
    )


def test_a_replay_reads_the_stored_answer_and_asks_no_model(tmp_path, monkeypatch):
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    model = CountingModel()
    ctx.model = model
    raw = plain_forward()
    first = runmod.process_raw(ctx, raw, source="imap", uid=7)
    assert first.status == "processed" and model.calls == 1
    keys = _keys(ctx)
    calls = ctx.ledger.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    ctx.model = None
    again = runmod.process_raw(ctx, raw, source="replay", force=True)
    assert again.parsed.parser == "haiku"
    assert [o.result for o in again.outcomes] == ["duplicate"]
    assert model.calls == 1 and _keys(ctx) == keys
    assert ctx.ledger.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls


def test_the_stored_answer_is_the_answer_as_read_before_any_shift(tmp_path, monkeypatch):
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    ctx.shift = DateShift(30)
    ctx.model = CountingModel()
    raw = plain_forward()
    first = runmod.process_raw(ctx, raw, source="imap", uid=7)
    stored = ctx.ledger.messages.answer(first.message_row_id)
    assert "2027-03-12T08:20" in stored
    later = [f"DL|9123|{DateShift(30).text('2027-03-12')}|MSP"]
    assert _keys(ctx) == later
    ctx.model = None
    runmod.process_raw(ctx, raw, source="replay", force=True)
    assert _keys(ctx) == later
    assert ctx.ledger.messages.answer(first.message_row_id) == stored


def test_reread_asks_the_model_again_and_replaces_the_stored_answer(tmp_path, monkeypatch):
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    ctx.model = CountingModel()
    raw = plain_forward()
    first = runmod.process_raw(ctx, raw, source="imap", uid=7)
    before = ctx.ledger.messages.answer(first.message_row_id)
    ctx.model = CountingModel(number="9330")
    runmod.process_raw(ctx, raw, source="replay", force=True, reread=True)
    assert ctx.model.calls == 1
    after = ctx.ledger.messages.answer(first.message_row_id)
    assert after != before and "9330" in after
    runmod.process_raw(ctx, raw, source="replay", force=True)
    assert ctx.model.calls == 1  # a plain replay now reads the replaced answer


def test_mail_read_by_the_extractor_alone_stores_no_answer(tmp_path, monkeypatch):
    ctx, _ = make_ctx(
        tmp_path,
        monkeypatch,
        [flight_obj("SAMPLE/ALEX QUINN", "14A", dep="2027-06-22T08:20:00-05:00")],
    )
    model = CountingModel()
    ctx.model = model
    m = make_eml(
        from_="alex@example.com", subject="Fwd: Receipt", date=D, text="x", headers=AR
    )
    res = runmod.process_raw(ctx, m.as_bytes(), source="imap", uid=1)
    assert res.parsed.parser == "kitinerary" and model.calls == 0
    assert ctx.ledger.messages.answer(res.message_row_id) is None


def test_reread_without_a_model_keeps_the_stored_answer_and_says_so(
    tmp_path, monkeypatch, caplog
):
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    ctx.model = CountingModel()
    raw = plain_forward()
    first = runmod.process_raw(ctx, raw, source="imap", uid=7)
    stored = ctx.ledger.messages.answer(first.message_row_id)
    keys = _keys(ctx)
    ctx.model = None
    for _ in range(2):
        res = runmod.process_raw(ctx, raw, source="replay", force=True, reread=True)
        assert res.status != "waiting" and res.parsed.parser == "haiku"
    assert _keys(ctx) == keys
    assert ctx.ledger.messages.answer(first.message_row_id) == stored
    said = [r for r in caplog.records if "--reread needs a model" in r.getMessage()]
    assert len(said) == 1 and "the stored answers were used" in said[0].getMessage()


def test_an_attached_email_stores_its_own_answer_and_replays_the_same_children(
    tmp_path, monkeypatch
):
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    model = CountingModel()
    ctx.model = model
    inner1 = make_eml(
        from_="a@t.delta.com", subject="Receipt 1", date=D, text="one", message_id="<i1>"
    )
    inner2 = make_eml(
        from_="a@t.delta.com", subject="Receipt 2", date=D, text="two", message_id="<i2>"
    )
    outer = make_eml(
        from_="alex@example.com", subject="Fwd", date=D, text="see", headers=AR, rfc822=inner1
    )
    outer.add_attachment(inner2)
    raw = outer.as_bytes()
    first = runmod.process_raw(ctx, raw, source="imap", uid=9)
    assert model.calls == 2 and len(first.children) == 1
    ids = [first.message_row_id, first.children[0].message_row_id]
    assert all(ctx.ledger.messages.answer(i) for i in ids)
    keys = _keys(ctx)
    ctx.model = None
    again = runmod.process_raw(ctx, raw, source="replay", force=True)
    assert model.calls == 2 and _keys(ctx) == keys
    assert [again.message_row_id, again.children[0].message_row_id] == ids
    assert again.parsed.parser == "haiku" and again.children[0].parsed.parser == "haiku"


def test_a_stored_answer_that_no_longer_validates_is_replaced_by_a_fresh_one(
    tmp_path, monkeypatch
):
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    model = CountingModel()
    ctx.model = model
    raw = plain_forward()
    first = runmod.process_raw(ctx, raw, source="imap", uid=7)
    good = ctx.ledger.messages.answer(first.message_row_id)
    ctx.ledger.messages.set_answer(first.message_row_id, '{"reservations": "garbled"')
    runmod.process_raw(ctx, raw, source="replay", force=True)
    assert model.calls == 2
    assert ctx.ledger.messages.answer(first.message_row_id) == good


def owners_receipt(message_id="<r1@x>", text="...") -> bytes:
    return make_eml(
        from_="alex@example.com",
        subject="Fwd: Your Flight Receipt",
        date=D,
        text=text,
        headers=AR,
        message_id=message_id,
    ).as_bytes()


def test_sync_runs_after_each_message_and_the_outcome_says_so(tmp_path, monkeypatch, caplog):
    ctx, tg = make_ctx(
        tmp_path,
        monkeypatch,
        [flight_obj("SAMPLE/ALEX QUINN", "14A", dep="2027-06-22T08:20:00-05:00")],
    )
    passes = []
    run_pass = ctx.syncer.run_pass

    def counted(**kw):
        report = run_pass(**kw)
        passes.append(report)
        return report

    monkeypatch.setattr(ctx.syncer, "run_pass", counted)
    res = runmod.process_raw(ctx, owners_receipt(), source="imap", uid=300)
    (o,) = res.outcomes
    assert res.status == "processed" and (o.result, o.sync, o.checks) == ("filed", "synced", ())
    assert passes == [PassReport(done={"create": 1}, stopped=False)]
    filed = ctx.trips.item(o.trip_item_id)
    (held,) = ctx.airtrail.flights
    assert filed.airtrail_flight_id == held["id"] and filed.sync_state == "synced"
    # a message with a second email attached is one pass, after both are filed
    inner = [
        make_eml(
            from_="a@t.delta.com",
            subject=f"Receipt {n}",
            date=D,
            text=str(n),
            message_id=f"<i{n}>",
        )
        for n in (1, 2)
    ]
    outer = make_eml(
        from_="alex@example.com", subject="Fwd", date=D, text="see", headers=AR, rfc822=inner[0]
    )
    outer.add_attachment(inner[1])
    both = runmod.process_raw(ctx, outer.as_bytes(), source="imap", uid=301)
    assert len(both.children) == 1 and len(passes) == 2
    # a pass that breaks is logged and never undoes or fails the message it follows
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda *a: [flight_obj("ALEX SAMPLE", "2B", number="9330", code="XYZ789")],
    )

    def breaks(**kw):
        raise RuntimeError("the pass broke")

    monkeypatch.setattr(ctx.syncer, "run_pass", breaks)
    caplog.set_level(logging.ERROR, logger="tripbox.run")
    res = runmod.process_raw(ctx, owners_receipt("<r2@x>", "another"), source="imap", uid=302)
    (o,) = res.outcomes
    assert res.status == "processed" and (o.result, o.sync) == ("filed", "pending")
    assert ctx.ledger.messages.get(res.message_row_id)["status"] == "processed"
    assert [r.exc_info[1].args for r in caplog.records if r.exc_info] == [("the pass broke",)]
    assert runmod.sync(ctx) is None and len(ctx.airtrail.flights) == 1
    # the next pass sends what that one left
    monkeypatch.setattr(ctx.syncer, "run_pass", run_pass)
    assert runmod.sync(ctx) == PassReport(done={"create": 1}, stopped=False)
    assert ctx.trips.item(o.trip_item_id).sync_state == "synced"
    # with no flight log kept there is nothing to run
    off, _ = make_ctx(tmp_path / "off", monkeypatch, [], airtrail=False)
    assert off.syncer is None and runmod.sync(off) is None


def flight_log_settings(tmp_path, **env) -> Settings:
    """A new install's settings with a flight log on (never reached: the run is offline)."""
    return Settings.from_env(
        {
            "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
            "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
            "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
            "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
            "TRIPBOX_MAIL_ALLOWED_SENDERS": "alex@example.com",
            "TZ": "America/Chicago",
            "TRIPBOX_HOME_AIRPORTS": "MSP",
            "TRIPBOX_AIRTRAIL_URL": "http://flightlog.example:3000",
            "TRIPBOX_AIRTRAIL_API_KEY": "placeholder",
            **env,
        }
    )


def test_a_dry_run_files_the_trip_and_sends_nothing(tmp_path, monkeypatch, caplog):
    """The dry run holds back the flight log and nothing else: the booking is filed in its trip,
    its flight waits, and what would be sent is logged once."""
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")  # the flight log is one held in memory
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda *a: [flight_obj("SAMPLE/ALEX QUINN", "14A", dep="2027-06-22T08:20:00-05:00")],
    )
    dry = flight_log_settings(tmp_path)  # TRIPBOX_AIRTRAIL_DRY_RUN is on unless it is set off
    assert dry.airtrail_dry_run and dry.on("airtrail")
    ctx = runmod.build_context(dry, with_model=False, with_notifier=False)
    flight_log(ctx.ledger)  # the ids that flight log gives the fixture's airports
    assert ctx.syncer.dry_run and ctx.ledger.kv.get("airtrail_dry_run") == "on"
    caplog.set_level(logging.INFO, logger="tripbox.sync")
    res = runmod.process_raw(ctx, owners_receipt(), source="imap", uid=1)
    (o,) = res.outcomes
    assert res.status == "processed" and (o.result, o.sync, o.checks) == ("filed", "pending", ())
    (trip,) = ctx.trips.all_trips()
    (filed,) = ctx.trips.items(trip.id)
    assert filed.id == o.trip_item_id and filed.airtrail_flight_id is None
    assert list(ctx.airtrail.iter_flights()) == []
    # the pass after the message logged what it would send: the next pass asks nothing again
    assert runmod.sync(ctx) == PassReport(done={}, stopped=False)
    would = [r.getMessage() for r in caplog.records if "dry run: would" in r.getMessage()]
    assert len(would) == 1 and "POST /api/v1/flights" in would[0]
    # the same message again is skipped: there is nothing a dry run left undone
    assert runmod.process_raw(ctx, owners_receipt(), source="replay").status == "skipped"
    # a run started with --dry-run sends nothing either, and leaves the ledger's note of the
    # setting as it is configured
    live = flight_log_settings(tmp_path, TRIPBOX_AIRTRAIL_DRY_RUN="false")
    flagged = runmod.build_context(
        dataclasses.replace(live, airtrail_dry_run=True),
        ledger=ctx.ledger,
        with_model=False,
        with_notifier=False,
        dry_run_from_flag=True,
    )
    assert flagged.syncer.dry_run and ctx.ledger.kv.get("airtrail_dry_run") == "off"
    # what the first dry run logged is not asked about again while it would send the same
    assert runmod.sync(flagged).done == {}
    assert list(flagged.airtrail.iter_flights()) == []
    # with the setting off, the next pass sends the flight that waited
    ctx = runmod.build_context(live, ledger=ctx.ledger, with_model=False, with_notifier=False)
    assert not ctx.syncer.dry_run and ctx.ledger.kv.get("airtrail_dry_run") == "off"
    assert runmod.sync(ctx) == PassReport(done={"create": 1}, stopped=False)
    (held,) = ctx.airtrail.iter_flights()
    after = ctx.trips.item(o.trip_item_id)
    assert (after.sync_state, after.airtrail_flight_id) == ("synced", held["id"])


def test_the_date_shift_still_moves_what_was_read_under_the_one_plan(tmp_path, monkeypatch):
    """A context with a shift files each booking on the moved day, and a later email for the
    same booking, moved the same way, is matched to it: the plan and the executor see only
    moved dates. The shift is applied in one place, once the extractors have read the email."""
    ctx, _ = make_ctx(tmp_path, monkeypatch, [])
    days = 30
    ctx.shift = DateShift(days)
    ctx.model = CountingModel()
    first = runmod.process_raw(ctx, plain_forward(), source="imap", uid=7)
    moved_key = f"DL|9123|{DateShift(days).text('2027-03-12')}|MSP"
    assert [(o.result, o.action.item.key) for o in first.outcomes] == [("filed", moved_key)]
    (filed,) = [ctx.trips.item(o.trip_item_id) for o in first.outcomes]
    assert filed.item_key == moved_key and filed.sync_state == "synced"
    (held,) = ctx.airtrail.flights
    assert held["date"] == DateShift(days).text("2027-03-12")
    sent_on = (D + dt.timedelta(days=days)).date().isoformat()
    assert filed.payload["email_date"].startswith(sent_on)
    # the same booking in a second email, read as written and moved the same way: a duplicate
    again = make_eml(
        from_="alex@example.com",
        subject="Fwd: Your trip to Philadelphia, again",
        date=D,
        text="Confirmation QXR987, as sent before",
        headers=AR,
        message_id="<qxr987-again@example.com>",
    ).as_bytes()
    second = runmod.process_raw(ctx, again, source="imap", uid=8)
    assert [(o.result, o.trip_item_id) for o in second.outcomes] == [("duplicate", filed.id)]
    assert _keys(ctx) == [moved_key] and len(ctx.airtrail.flights) == 1
    # one call site, and neither the plan nor the executor knows of a shift
    assert inspect.getsource(runmod).count("ctx.shift.apply(") == 1
    for module in (planner_module, executor_module):
        assert "shift" not in inspect.getsource(module).lower()


def test_a_finding_is_dated_by_when_its_message_was_received(tmp_path, monkeypatch):
    """What an email raises is stamped with the time its message was taken in, never with the
    Date it prints, and reading the message again keeps that time. An unvouched sender's receipt
    that says it was sent before the owner last looked, and arrives after, holds the flight back;
    read again once the owner has looked, it reopens nothing."""
    from test_normalize import AR_FWD

    caf = {"Return-Path": "<alex+caf_=trips=example.com@example.com>", **AR_FWD}
    hour = dt.timedelta(hours=1)
    alone = [flight_obj("ALEX SAMPLE", "1A")]
    both = [*alone, flight_obj("Sam Sample", "1B")]
    ctx, _ = make_ctx(tmp_path, monkeypatch, alone)
    st = ctx.trips
    booked = runmod.process_raw(ctx, owners_receipt(), source="imap", uid=1, received=D)
    (first,) = booked.outcomes
    assert (first.result, first.sync) == ("filed", "synced")

    def looks(at: dt.datetime):
        payload = st.item(first.trip_item_id).payload
        payload["looked_at"] = at.isoformat(timespec="seconds")
        ctx.ledger.trip_items.update(first.trip_item_id, payload=payload)
        return st.settle(first.trip_item_id)

    looks(D + 2 * hour)
    # a receipt with a companion on it, auto-forwarded from a sender no list vouches for: it is
    # dated before that look, and received after it
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda *a: both)
    stranger = make_eml(
        from_='"delta.com" <x@evil.example>',
        subject="Reservation confirmation",
        date=D + hour,
        text="x",
        headers=caf,
    ).as_bytes()
    received = D + 3 * hour
    with util.clock_at(received):  # read when it is received: its bookings reach the ledger then
        res = runmod.process_raw(ctx, stranger, source="imap", uid=2, received=received)
    (o,) = res.outcomes
    assert (o.result, o.sync, o.checks) == ("updated", "blocked", ("unverified_sender",))
    stamp = received.isoformat(timespec="seconds")
    assert ctx.ledger.messages.get(res.message_row_id)["received_at"] == stamp
    filed = st.item(o.trip_item_id)
    assert filed.id == first.trip_item_id
    assert filed.payload["raised"] == {"unverified_sender": stamp}
    assert [r[0] for r in ctx.airtrail.sent] == ["create"]  # nothing of it is sent
    # the owner looks, and the companion goes to the flight log
    assert looks(D + 4 * hour).sync_state == "pending"
    assert runmod.sync(ctx).done == {"update": 1}
    # the companion's seat is cancelled (the owner's own forward), and then the stranger's
    # receipt is read again, long after: still that stranger's, it raises its finding again as
    # it puts the companion back, stamped with the time it was first received, which the
    # owner's look has cleared
    monkeypatch.setattr(
        pl.kitinerary,
        "run_extractor",
        lambda *a: [
            flight_obj("Sam Sample", None, status="http://schema.org/ReservationCancelled")
        ],
    )
    gone = runmod.process_raw(
        ctx, owners_receipt("<c@x>", "cancelled"), source="imap", uid=3, received=D + 5 * hour
    )
    assert [o.action.reason for o in gone.outcomes] == ["remove_passengers"]
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda *a: both)
    again = runmod.process_raw(ctx, stranger, source="replay", force=True)
    (o,) = again.outcomes
    assert again.message_row_id == res.message_row_id
    assert (o.action.reason, o.result) == ("add_passengers", "updated")
    assert o.action.item.payload["raised"] == {"unverified_sender": stamp}
    assert (o.sync, o.checks) == ("synced", ())
    assert st.item(o.trip_item_id).payload["raised"] == {"unverified_sender": stamp}
    assert ctx.ledger.messages.get(res.message_row_id)["received_at"] == stamp


def test_a_look_made_while_the_model_was_reading_closes_nothing_it_did_not_show(
    tmp_path, monkeypatch
):
    """The owner's own receipt for flights first filed for a companion is taken in; while the
    model reads it (its answer below the confidence floor), the owner says Looks right on a page
    rendered then, which showed the flight without it. What the receipt raises is dated by when
    its bookings reach the ledger, after that look, so it stays open."""
    from test_filed import UNSURE

    from tripbox.trips.store import stamp

    c = corpus.load(corpus.default_dir())
    first, second = c.entry("03-receipt-companion-first"), c.entry("04-receipt-owner-second")
    taken_in = corpus.sent(second)
    page, filed_at = taken_in + dt.timedelta(hours=1), taken_in + dt.timedelta(hours=2)
    with pipeline(tmp_path, monkeypatch) as (ctx, asked, told):
        with util.clock_at(corpus.sent(first)):
            o = runmod.process_raw(ctx, first.raw, source="imap", uid=1).outcomes[0]
        assert ctx.trips.item(o.trip_item_id).checks == ()
        asked.answers[sha(second.raw)] = second.answer.model_copy(update={"confidence": UNSURE})
        asked.reading = lambda: ctx.trips.looks_right(o.trip_item_id, at=stamp(page))
        with util.clock_at(filed_at):
            res = runmod.process_raw(ctx, second.raw, source="imap", uid=2, received=taken_in)
        assert [x.result for x in res.outcomes] == ["updated"] * len(res.outcomes)
        ti = ctx.trips.item(o.trip_item_id)
        assert ti.payload["looked_at"] == stamp(page) and ti.checks == ("low_confidence",)
        assert ti.payload["raised"] == {"low_confidence": stamp(filed_at)}
        assert ctx.ledger.messages.get(res.message_row_id)["found_at"] == stamp(filed_at)


def test_a_reread_after_a_finished_reading_keeps_the_first_stamp(tmp_path, monkeypatch):
    """A message's findings are dated once, when its bookings first reach the ledger: here a
    minute after it was received, once the model answered. Read again after that reading
    finished, by force and with the model asked again (it now reads the first flight an hour
    later), what it raises keeps that first date, which the owner's look since covers."""
    from test_filed import UNSURE
    from test_store_actions import an_hour_later

    from tripbox.trips.store import stamp

    entry = read_by_the_model()[0]
    taken_in = corpus.sent(entry)
    filed_at = taken_in + dt.timedelta(minutes=1)
    looked, again_at = filed_at + dt.timedelta(hours=1), filed_at + dt.timedelta(hours=2)
    with pipeline(tmp_path, monkeypatch) as (ctx, asked, told):
        unsure = entry.answer.model_copy(update={"confidence": UNSURE})
        asked.answers[sha(entry.raw)] = unsure
        with util.clock_at(filed_at):
            first = runmod.process_raw(ctx, entry.raw, source="imap", uid=1, received=taken_in)
        assert ctx.ledger.messages.get(first.message_row_id)["found_at"] == stamp(filed_at)
        ids = [o.trip_item_id for o in first.outcomes]
        for i in ids:
            ti = ctx.trips.item(i)
            assert ti.checks == ("low_confidence",)
            assert ti.payload["raised"] == {"low_confidence": stamp(filed_at)}
            assert ctx.trips.looks_right(i, at=stamp(looked)).checks == ()
        moved = unsure.model_copy(deep=True)
        leg = moved.reservations[0].legs[0]
        leg.departure, leg.arrival = an_hour_later(leg.departure), an_hour_later(leg.arrival)
        asked.answers[sha(entry.raw)] = moved
        with util.clock_at(again_at):
            again = runmod.process_raw(ctx, entry.raw, source="replay", force=True, reread=True)
        assert asked.calls == 2 and "updated" in [o.result for o in again.outcomes]
        for i in ids:
            ti = ctx.trips.item(i)
            assert ti.checks == () and ti.payload["raised"] == {"low_confidence": stamp(filed_at)}
        assert ctx.ledger.messages.get(first.message_row_id)["found_at"] == stamp(filed_at)


def test_a_failed_reading_dates_nothing_so_a_later_one_that_files_dates_its_findings_then(
    tmp_path, monkeypatch
):
    """A message's findings are dated only by a reading that ends done. Here an email changes a
    booking the owner's earlier email filed, with an answer below the confidence floor, and its
    first reading fails: the store refuses the write. The owner then looks at the booking. Read
    again by `replay --reread`, the email changes the booking at last, and the finding it raises
    is dated by that reading, after the look, so it stays open."""
    from test_filed import SENDER, UNSURE
    from test_store_actions import an_hour_later

    from tripbox.trips.store import stamp

    entry = read_by_the_model()[0]
    at = corpus.sent(entry)
    failed_at, looked, again_at = (at + dt.timedelta(hours=h) for h in (1, 2, 3))
    with pipeline(tmp_path, monkeypatch) as (ctx, asked, told):
        with util.clock_at(at):
            first = runmod.process_raw(ctx, entry.raw, source="imap", uid=1)
        ids = [o.trip_item_id for o in first.outcomes]
        moved = entry.answer.model_copy(deep=True, update={"confidence": UNSURE})
        leg = moved.reservations[0].legs[0]
        leg.departure, leg.arrival = an_hour_later(leg.departure), an_hour_later(leg.arrival)
        change = make_eml(
            from_=SENDER, subject="Schedule change", date=at, text=entry.name, headers=AR
        ).as_bytes()
        asked.answers[sha(change)] = moved

        def refused(*args, **kw):
            raise RuntimeError("the store refused the write")

        with monkeypatch.context() as m, util.clock_at(failed_at):
            m.setattr(ctx.executor, "_apply", refused)
            res = runmod.process_raw(ctx, change, source="imap", uid=2)
        assert res.status == "failed" and {o.result for o in res.outcomes} == {"failed"}
        assert ctx.ledger.messages.get(res.message_row_id)["found_at"] is None
        for i in ids:
            assert ctx.trips.looks_right(i, at=stamp(looked)).checks == ()
        with util.clock_at(again_at):
            again = runmod.process_raw(ctx, change, source="replay", reread=True)
        assert again.status == "processed" and "updated" in [o.result for o in again.outcomes]
        changed = ctx.trips.item(next(o.trip_item_id for o in again.outcomes))
        assert changed.checks == ("low_confidence",)
        assert changed.payload["raised"] == {"low_confidence": stamp(again_at)}
        assert ctx.ledger.messages.get(res.message_row_id)["found_at"] == stamp(again_at)
