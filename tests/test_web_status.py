# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A brand-new install's pages, and the status card: in plain words, from the worker's own record
alone (`tripbox.status`: words and counts, never an error's text), whether the worker runs,
whether mail is read and where to forward confirmations, whether a model is set up and what mail
waits for it, and whether a flight log is kept. Each record here is written by a `Worker` over a
held clock; the emails are the corpus's own, read at run time, and the processes the worker
starts are stand-ins: nothing here starts a process or opens a socket."""

import dataclasses
import datetime as dt
import json
import logging
import re
import signal
from html import unescape

import pytest
from helpers.web import client_for
from test_web_login import SAME, as_cookie, fresh, get, session_of, sign_in
from test_web_login import page as password_page
from test_web_views import seed_trip, undated_stay
from test_worker import a_worker, beat, context, owners_flights, start, submit, the_corpus

from tripbox import status, util, worker
from tripbox.parse import pipeline as pl
from tripbox.parse.provider import ModelError
from tripbox.pipeline import run
from tripbox.trips import ical, shares
from tripbox.web import card, views

AT = dt.datetime(2027, 6, 16, 9, 0, tzinfo=dt.UTC)  # when every worker here starts
MAILBOX = "trips@example.com"
MAIL = {
    "TRIPBOX_MAIL_HOST": "imap.trips.example",
    "TRIPBOX_MAIL_USER": MAILBOX,
    "TRIPBOX_MAIL_PASSWORD": "placeholder",
    "TRIPBOX_MAIL_TRUST": "mailbox",  # gmail trust needs Gmail's own mailbox
}
MODEL = {"TRIPBOX_LLM_API_KEY": "placeholder"}
# The card's sentences, word for word
RUNNING = "Running."
NOT_RUNNING = "Not running. Nothing new is read until it is started."
MAIL_OFF = "Off. Add bookings by pasting or uploading a confirmation."
FIRST_CHECK = f"Forward confirmations to {MAILBOX}. The first check has not finished."
CHECKED = "Forward confirmations to " + MAILBOX + ". Last checked {age} ago."
MAIL_FAILED = "The mailbox could not be checked ({code}). It is tried again by itself."
MODEL_OFF = "Off. Only emails that carry structured booking data are read."
ON = "On."
NO_MODEL = "{n} waiting: no model is set up."
OVER_BUDGET = "{n} waiting: the model's budget is used."
MODEL_DOWN = "{n} waiting: the model cannot be reached."
FLIGHT_LOG_OFF = "Off. Flights are kept in your trips only."
DRY_RUN = "On, in dry-run mode: flights are filed here and nothing is sent."
PASS_FAILED = "The last pass could not reach it ({code})."
INVALID = 'Its settings need a look: run "tripbox doctor".'
# The list's own sentences
NO_TRIPS = "No trips yet"
ADD_TO_START = (
    "Add a booking to start: paste a confirmation's text, upload the email or PDF, or type it in."
)
NO_WORKER = "The worker is not running, so nothing new is read."
FORWARD = f"Nothing coming up. Forward a confirmation to {MAILBOX} and it shows up here."
CARD = re.compile(r'<dl class="status-card">(.*?)</dl>', re.S)


@pytest.fixture(autouse=True)
def stand_ins(monkeypatch):
    """The worker's signal handlers stay out of the test process, and the structured-data
    extractor (a process of its own) finds nothing, so the model reads every message."""
    monkeypatch.setattr(signal, "signal", lambda signum, handler: None)
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [])


class Refuses:
    """The model, refusing every message as `kind` does: it cannot be reached, or its budget is
    used."""

    model = "corpus"

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def extract(self, nm, hint=None):
        word = "month" if self.kind == "over_budget" else None
        raise ModelError(self.kind, "no answer", word=word)


def beating(tmp_path, *, flight_log=False, exit=None, **environ):
    """A worker over a new ledger under the corpus's settings and `environ`, started at AT and
    one beat on; each process it starts ends at once with `exit`, or runs on. The context, the
    worker and its clock."""
    ctx = context(tmp_path, flight_log=flight_log, **environ)
    w, clock, _ = a_worker(ctx, AT, exit=exit)
    start(w)
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    return ctx, w, clock


def rows_at(ctx, at: dt.datetime) -> list[tuple[str, str, bool]]:
    return [(r["label"], r["text"], r["ok"]) for r in card.status_card(ctx.ledger, at)]


def row(ctx, at: dt.datetime, label: str) -> list[tuple[str, bool]]:
    """The card's rows under `label`, each (sentence, ok)."""
    return [(text, ok) for name, text, ok in rows_at(ctx, at) if name == label]


def card_rows(html: str) -> list[tuple[str, str]]:
    """The status card as a page shows it: each row's label and sentence."""
    (body,) = CARD.findall(html)
    shown = []
    for group in re.findall(r'<div class="status-row">(.*?)</div>', body, re.S):
        label = unescape(re.search(r"<dt>(.*?)</dt>", group, re.S).group(1))
        for text in re.findall(r"<dd[^>]*>(.*?)</dd>", group, re.S):
            shown.append((label, " ".join(unescape(text).split())))
    return shown


def test_every_page_of_a_brand_new_install_renders_and_says_what_to_do(
    tmp_path, monkeypatch, base
):
    """No mail, model, flight log, travellers file or worker record: each page answers, and the
    list says what to do first — its heading, the sentence and the button that adds a booking —
    then the card's one row, that the worker is not running."""
    (tmp_path / "config").mkdir()
    env = {"TRIPBOX_CONFIG_DIR": str(tmp_path / "config")}
    c, ctx = client_for(tmp_path, monkeypatch, base=base, airtrail=False, env=env)
    s = ctx.settings
    assert not any(s.on(f) for f in ("mail", "model", "airtrail", "notifications", "enrichment"))
    assert s.travellers.me_name == "" and status.read(ctx.ledger) is None
    paths = (
        "/",
        "/deleted",
        "/add",
        "/add/paste",
        "/add/upload",
        *(f"/add/{word}" for word in views.ADD_KIND),
        f"/feed/{ical.feed_token(ctx.ledger)}.ics",
        "/trips/1",
        "/settings",
    )
    with util.clock_at(AT):
        pages = {path: c.get(path) for path in paths}
    # the list, Recently deleted, Add and its six forms, the feed, a trip, Settings
    assert len(paths) == 12
    for path, r in pages.items():
        assert r.status_code == (404 if path == "/trips/1" else 200), path
        assert " style=" not in r.text and "<style" not in r.text, path
    assert "BEGIN:VCALENDAR" in pages[paths[-3]].text
    page = pages["/"].text
    main = page[page.index('<main class="page">') :]
    assert f'<h2 class="first-run-title" id="first-run-title">{NO_TRIPS}</h2>' in main
    assert ADD_TO_START in unescape(main)
    button = re.findall(r'<a class="btn primary" href="([^"]+)">Add a booking</a>', main)
    assert button == [c.url("/add")]
    assert main.index(NO_TRIPS) < main.index(button[0]) < main.index('class="status-card"')
    assert card_rows(page) == [("Worker", NOT_RUNNING)]
    # the card says the worker is not running; the list says nothing more of it
    assert NO_WORKER not in page and ">Upcoming<" not in page


def test_the_card_says_each_state_in_its_own_words(tmp_path, monkeypatch):
    """One case a row of the card's table, each record written by a worker."""
    # the worker runs; mail, the model and the flight log are off
    ctx, w, clock = beating(tmp_path / "off")
    assert rows_at(ctx, clock()) == [
        ("Worker", RUNNING, True),
        ("Mail", MAIL_OFF, True),
        ("Model", MODEL_OFF, True),
        ("Flight log", FLIGHT_LOG_OFF, True),
    ]
    # it does not: the one row
    gone = clock() + dt.timedelta(seconds=status.STALE_SECONDS)
    assert rows_at(ctx, gone) == [("Worker", NOT_RUNNING, False)]
    # mail on: where to forward confirmations, and how the last check went
    ctx, w, clock = beating(tmp_path / "mail", **MAIL)  # its poll runs on
    assert row(ctx, clock(), "Mail") == [(FIRST_CHECK, True)]
    ctx, w, clock = beating(tmp_path / "checked", exit=0, **MAIL)
    clock.tick(worker.BEAT_SECONDS)
    beat(w)  # the poll that ended is collected
    assert row(ctx, clock(), "Mail") == [(CHECKED.format(age="moments"), True)]
    clock.tick(4 * 60)
    beat(w)  # no poll is due yet
    assert row(ctx, clock(), "Mail") == [(CHECKED.format(age="4 min"), True)]
    ctx, w, clock = beating(tmp_path / "failed", exit=1, **MAIL)
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    assert row(ctx, clock(), "Mail") == [(MAIL_FAILED.format(code="exit_1"), False)]
    # the model on; then mail that waits for it, by reason
    ctx, w, clock = beating(tmp_path / "model", **MODEL)
    assert row(ctx, clock(), "Model") == [(ON, True)]
    ctx = context(tmp_path / "waiting")
    models = (None, None, Refuses("over_budget"), Refuses("unavailable"))
    for model, entry in zip(models, the_corpus().entries, strict=False):
        ctx.model = model
        with util.clock_at(AT):
            run.process_raw(ctx, entry.raw, source="upload", trusted=True, notify=False)
    assert ctx.ledger.messages.waiting() == {"no_model": 2, "over_budget": 1, "model_down": 1}
    w, clock, _ = a_worker(ctx, AT)
    start(w)
    assert row(ctx, clock(), "Model") == [
        (MODEL_OFF, True),
        (NO_MODEL.format(n=2), False),
        (OVER_BUDGET.format(n=1), False),
        (MODEL_DOWN.format(n=1), False),
    ]
    # a flight log kept; in a dry run; one whose last pass could not reach it
    ctx, w, clock = beating(tmp_path / "log", flight_log=True)
    assert row(ctx, clock(), "Flight log") == [(ON, True)]
    dry = {"TRIPBOX_AIRTRAIL_DRY_RUN": "true"}
    ctx, w, clock = beating(tmp_path / "dry", flight_log=True, **dry)
    assert row(ctx, clock(), "Flight log") == [(DRY_RUN, True)]
    ctx = context(tmp_path / "down", flight_log=True)
    submit(ctx, owners_flights().raw, "booking.eml")
    ctx.airtrail.fail_next(503, times=2)  # the pass after the message, and the beat's own
    w, clock, _ = a_worker(ctx, AT)
    start(w)
    beat(w)
    assert row(ctx, clock(), "Flight log") == [(PASS_FAILED.format(code="outage"), False)]
    # settings that need a look
    ctx = context(tmp_path / "invalid", flight_log=True, **MAIL, **MODEL)
    looked = ("mail", "model", "airtrail")
    features = tuple(
        dataclasses.replace(f, state="invalid") if f.name in looked else f
        for f in ctx.settings.features
    )
    ctx.settings = dataclasses.replace(ctx.settings, features=features)
    w, clock, _ = a_worker(ctx, AT)
    start(w)
    assert rows_at(ctx, clock()) == [
        ("Worker", RUNNING, True),
        ("Mail", INVALID, False),
        ("Model", INVALID, False),
        ("Flight log", INVALID, False),
    ]
    # how long ago, in four words
    ages = {0: "moments", 59: "moments", 60: "1 min", 3599: "59 min", 3600: "1 h"}
    ages |= {86399: "23 h", 86400: "1 d", 9 * 86400: "9 d", -60: "moments"}
    assert {seconds: card.age(seconds) for seconds in ages} == ages


def test_the_card_tells_a_failed_message_from_a_failed_mailbox(tmp_path):
    """A poll that read the mailbox but could not read an email (it ends 3) is not a mailbox
    that could not be checked: the card says which."""
    failed = "An email could not be read. It is kept, and the log names it."
    for code, said in ((3, failed), (1, MAIL_FAILED.format(code="exit_1"))):
        ctx, w, clock = beating(tmp_path / str(code), exit=code, **MAIL)
        clock.tick(worker.BEAT_SECONDS)
        beat(w)  # the poll that ended is collected
        assert row(ctx, clock(), "Mail") == [(said, False)], code


def test_a_mail_login_that_is_no_address_is_never_named(tmp_path, monkeypatch):
    """A login that is not an address is no mailbox to forward confirmations to: the record
    names none, and the card says mail is on, and how its last check went, naming nothing; the
    list's empty Upcoming names nothing either. An older worker's record (without the mailbox,
    what waits and the dry run) whose last check failed says so."""
    login = {**MAIL, "TRIPBOX_MAIL_USER": "trips"}
    ctx, w, clock = beating(tmp_path / "first", **login)  # its poll runs on
    assert status.read(ctx.ledger)["mailbox"] is None
    assert row(ctx, clock(), "Mail") == [("On. The first check has not finished.", True)]
    ctx, w, clock = beating(tmp_path / "checked", exit=0, **login)
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    assert row(ctx, clock(), "Mail") == [("On. Last checked moments ago.", True)]
    ctx, w, clock = beating(tmp_path / "failed", exit=1, **login)
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    assert row(ctx, clock(), "Mail") == [(MAIL_FAILED.format(code="exit_1"), False)]
    # an older worker's record: mail on, the last check failed, no mailbox named
    ctx, w, clock = beating(tmp_path / "older", exit=1, **MAIL)
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    record = status.read(ctx.ledger)
    for name in ("mailbox", "waiting", "dry_run"):
        del record[name]
    ctx.ledger.kv.set(status.KV_WORKER, json.dumps(record))
    assert row(ctx, clock(), "Mail") == [(MAIL_FAILED.format(code="exit_1"), False)]
    # the list
    c, page_ctx = client_for(tmp_path / "list", monkeypatch, airtrail=False)
    page_ctx.trips.upsert_item(None, undated_stay())  # one booking: no longer a first run
    ctx = context(tmp_path / "list", **login)
    w, clock, _ = a_worker(ctx, AT)
    start(w)
    with util.clock_at(AT):
        page = unescape(c.get("/").text)
    assert '<p class="empty">Nothing coming up.</p>' in page
    assert "Forward a confirmation" not in page and NO_WORKER not in page


def test_the_card_holds_codes_and_never_error_text(tmp_path, monkeypatch):
    """A part of a beat that breaks is recorded as a word, and the card says that word, never
    the error's text; a record that holds text where a code goes (another program's, a broken
    one) is shown with a word all the same."""
    told = f"could not reach flightlog.example as {MAILBOX}: connection refused"

    def broken(ctx):
        raise RuntimeError(told)

    monkeypatch.setattr(run, "sync", broken)
    ctx, w, clock = beating(tmp_path, flight_log=True, exit=1, **MAIL)
    clock.tick(worker.BEAT_SECONDS)
    beat(w)
    shown = rows_at(ctx, clock())
    assert ("Mail", MAIL_FAILED.format(code="exit_1"), False) in shown
    assert ("Flight log", PASS_FAILED.format(code="error"), False) in shown
    record = status.read(ctx.ledger)
    said = json.dumps(record) + " ".join(text for _, text, _ in shown)
    assert "refused" not in said and "flightlog.example" not in said
    record["last"]["poll"]["code"] = told
    record["last"]["sync"]["code"] = "stopped\nafter 15 minutes"
    ctx.ledger.kv.set(status.KV_WORKER, json.dumps(record))
    assert row(ctx, clock(), "Mail") == [(MAIL_FAILED.format(code="error"), False)]
    assert row(ctx, clock(), "Flight log") == [(PASS_FAILED.format(code="error"), False)]


def test_a_stale_record_reads_as_not_running(tmp_path):
    """By the record's own rule (`status.live`): a beat STALE_SECONDS old, or ahead of the clock
    by as much, is a worker that stopped; a busy one lives BUSY_SECONDS; one that gave the
    ledger up is stopped at once. A worker that is not running is the card's one row."""
    ctx, w, clock = beating(tmp_path, **MAIL)
    beat_at = clock()
    stopped = [("Worker", NOT_RUNNING, False)]
    alive = rows_at(ctx, beat_at + dt.timedelta(seconds=status.STALE_SECONDS - 1))
    assert alive[0] == ("Worker", RUNNING, True) and len(alive) == 4
    for seconds in (status.STALE_SECONDS, 3600, -status.STALE_SECONDS):
        assert rows_at(ctx, beat_at + dt.timedelta(seconds=seconds)) == stopped, seconds
    with util.clock_at(beat_at):
        w._record(busy=True)  # a part that may run long began, and the worker went no further
    busy = rows_at(ctx, beat_at + dt.timedelta(seconds=status.BUSY_SECONDS - 1))
    assert busy[0] == ("Worker", RUNNING, True)
    assert rows_at(ctx, beat_at + dt.timedelta(seconds=status.BUSY_SECONDS)) == stopped
    with util.clock_at(beat_at):
        w._release()
    assert rows_at(ctx, beat_at) == stopped


def test_the_list_says_when_no_worker_runs(tmp_path, monkeypatch, base):
    """Past a first run, while no worker runs the list starts by saying so; while one runs and
    reads mail, an empty Upcoming says where to forward a confirmation. A first run says it in
    the card instead."""
    c, page_ctx = client_for(tmp_path, monkeypatch, base=base, airtrail=False)
    page_ctx.trips.upsert_item(None, undated_stay())  # one booking: no longer a first run

    def main(at: dt.datetime) -> str:
        with util.clock_at(at):
            page = unescape(c.get("/").text)
        return page[page.index('<main class="page">') :]

    shown = main(AT)
    assert re.match(rf'<main class="page">\s*<p class="notice">{NO_WORKER}</p>', shown)
    assert "<p class=\"empty\">Nothing coming up.</p>" in shown and MAILBOX not in shown
    assert NO_TRIPS not in shown and "status-card" not in shown
    ctx = context(tmp_path, **MAIL)  # the worker's own settings, over the same ledger
    w, clock, _ = a_worker(ctx, AT)
    start(w)
    shown = main(AT)
    assert NO_WORKER not in shown and f'<p class="empty">{FORWARD}</p>' in shown
    shown = main(AT + dt.timedelta(seconds=status.STALE_SECONDS))
    assert NO_WORKER in shown and MAILBOX not in shown
    # a first run
    first, _ = client_for(tmp_path / "new", monkeypatch, base=base, airtrail=False)
    with util.clock_at(AT):
        page = first.get("/").text
    assert NO_WORKER not in page and card_rows(page) == [("Worker", NOT_RUNNING)]


def test_the_mailbox_is_shown_only_on_private_pages(tmp_path, monkeypatch, base, caplog):
    """The address mail is read from (with `TRIPBOX_MAIL_TRUST=mailbox`, what lets mail in) is
    in the worker's record and on the signed-in owner's list; no public response holds it — the
    login page, a wrong password, the feed, a page asked for without signing in, the health
    check, a static file, a shared trip's page and a wrong shared link, a sign-out — and no log
    line does."""
    caplog.set_level(logging.DEBUG)
    c, page_ctx = password_page(tmp_path, monkeypatch, base)
    ctx = context(tmp_path, **MAIL)
    w, clock, _ = a_worker(ctx, AT)
    start(w)
    beat(w)
    assert status.read(ctx.ledger)["mailbox"] == MAILBOX
    token = ical.feed_token(page_ctx.ledger)
    public = ("/", "/login", "/healthz", "/static/app.css", "/deleted", "/trips/1", "/add")
    with util.clock_at(AT):
        answers = [fresh(c).get(path) for path in public]
        answers += [fresh(c).get(path, follow_redirects=False) for path in public]
        answers += [fresh(c).get(f"/feed/{token}.ics"), fresh(c).get("/feed/nothing.ics")]
        answers.append(sign_in(c, "not the password"))
        signed = sign_in(c)
        answers.append(signed)
        cookie = as_cookie("tripbox_session", session_of(signed))
        private = get(c, "/", cookie)
        # past the first run: a trip shared by a link, its page and a wrong link, then sign-out
        trip = seed_trip(page_ctx)
        shared = "/s/" + shares.create(page_ctx.ledger, trip.id, shares.FIELDS).token
        answers += [fresh(c).get(shared), fresh(c).get(shared[:-1] + "/x")]
        assert [r.status_code for r in answers[-2:]] == [200, 404]
        answers.append(fresh(c).post("/logout", headers={**cookie, **SAME}, follow_redirects=False))
        assert answers[-1].status_code == 303
    for r in answers:
        assert r.status_code in (200, 303, 401, 404), r.url
        assert MAILBOX not in r.text and MAILBOX not in str(r.headers), r.url
    assert private.status_code == 200
    assert ("Mail", FIRST_CHECK) in card_rows(private.text)
    assert MAILBOX not in caplog.text
