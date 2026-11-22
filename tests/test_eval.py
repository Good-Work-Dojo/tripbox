# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox eval`: the corpus filed, in order, into one empty ledger through the real pipeline and
scored per email and in total. The corpus is read under its own settings, never the
installation's; a wrong reading, a missing answer or an email the extractor stops reading fails
that email, and the run goes on."""

import datetime as dt
import json
import os
import re
import shutil
import tempfile
import time

import pytest

from tripbox import cli, corpus, evalrun, util
from tripbox.parse import pipeline as pl
from tripbox.parse.provider import DEFAULT_MODEL, ModelError, Usage
from tripbox.parse.replay import NO_ANSWER
from tripbox.pipeline.run import RECORD_ENV, REPLAY_ENV


def copy_corpus(tmp_path):
    root = tmp_path / "corpus"
    shutil.copytree(corpus.default_dir(), root)
    return root


def parsers(card) -> dict[str, int]:
    """The `parsers:` line of a scorecard as reader -> count."""
    line = next(x for x in card.lines() if x.startswith("parsers: "))
    return {name: int(n) for name, n in re.findall(r"(\w+): (\d+)", line[len("parsers: ") :])}


def keys(doc) -> set[str]:
    """Every key of every object in a JSON document, at any depth."""
    if isinstance(doc, dict):
        return set(doc) | {k for v in doc.values() for k in keys(v)}
    if isinstance(doc, list):
        return {k for v in doc for k in keys(v)}
    return set()


class NoAnswer:
    """A model extractor that has no answer for any email."""

    model = DEFAULT_MODEL

    def extract(self, nm, hint=None):
        raise ModelError("invalid", NO_ANSWER)


def test_the_corpus_scores_full_marks_with_its_recorded_answers(no_network):
    c = corpus.load(corpus.default_dir())
    card = evalrun.run_corpus(c, recorded=True)
    assert card.right and card.answers == "recorded"
    assert [e.name for e in card.emails] == [e.name for e in c.entries]
    assert [(e.name, f.path) for e in card.emails for f in e.fields if not f.right] == []
    assert all(e.error is None and e.outcome_diffs == () for e in card.emails)
    seen = parsers(card)
    assert set(seen) == {"kitinerary", "model", "none"}
    assert sum(seen.values()) == len(c.entries) and seen["kitinerary"] >= 1
    n = len(c.entries)
    assert card.lines()[-1] == f"eval: {n} of {n} emails right"
    assert no_network == []


def sheet_row(c, name: str) -> str:
    """The entry's row in the entries table of the corpus's `SCENARIOS.md`."""
    sheet = (c.root / "SCENARIOS.md").read_text(encoding="utf-8")
    return next(line for line in sheet.splitlines() if line.startswith(f"| `{name}` |"))


def test_the_ledger_after_the_run_holds_what_the_story_leaves_standing(monkeypatch):
    c = corpus.load(corpus.default_dir())
    flights: list[tuple[str, dict]] = []
    memory = evalrun.Ledger.memory

    def read_before_closing():
        led = memory()
        close = led.close

        def close_after_reading():
            rows = led.conn.execute(
                "SELECT item_key, payload_json FROM trip_items WHERE kind='flight'"
            ).fetchall()
            flights.extend((r["item_key"], json.loads(r["payload_json"])) for r in rows)
            close()

        led.close = close_after_reading
        return led

    monkeypatch.setattr(evalrun.Ledger, "memory", read_before_closing)
    card = evalrun.run_corpus(c, recorded=True)
    assert card.right
    # counted by hand from SCENARIOS.md: twelve trips, Nashville's cancelled stay its own; every
    # flight leg, stay, car and train in them the story leaves standing, the cancelled stays
    # included; and the flight with no date, in no trip
    trips, bookings, unsorted = 12, 32, 1
    totals = card.as_json()["totals"]
    counted = (totals["trips"], totals["bookings"], totals["unsorted"])
    assert counted == (trips, bookings, unsorted), (
        f"the ledger holds {counted[0]} trips, {counted[1]} bookings in them and {counted[2]}"
        f" with no trip, the count by hand {trips}, {bookings} and {unsorted}: when an entry"
        " files a booking or opens a trip, count the trips and the bookings the story leaves"
        " standing (cancelled ones included) by hand from tests/corpus/SCENARIOS.md and update"
        " the three here"
    )
    lines = card.lines()
    gaps = next(n for n, line in enumerate(lines) if line.startswith("known gaps: "))
    assert lines[gaps] == "known gaps: 0" and totals["known_gaps"] == 0
    assert lines[gaps + 1] == f"ledger: {trips} trips, {bookings} bookings, {unsorted} unsorted"
    assert lines[-1] == f"eval: {len(c.entries)} of {len(c.entries)} emails right"

    # the flight renumbered on the same day keeps one booking, under its new number only
    renumber = "15-same-day-renumber"
    gone = re.search(r"no ([A-Z0-9]{2}) (\d+) remains", sheet_row(c, renumber))
    assert gone, f"{renumber}: its row in SCENARIOS.md names no number that is gone"
    carrier, old = gone.groups()
    prefix = f"{carrier}|{old}|"
    assert any(
        i["key"].startswith(prefix)
        for e in c.entries[: c.entries.index(c.entry(renumber))]
        for i in e.outcome["items"]
    ), f"no email before {renumber} files a flight under the old number"
    assert flights
    left = [
        key
        for key, payload in flights
        if key.startswith(prefix) or payload.get("flight_code") == f"{carrier}{old}"
    ]
    assert left == []


def test_the_scorecard_does_not_depend_on_the_day_it_runs():
    c = corpus.load(corpus.default_dir())
    year = c.anchor.year
    # a day before every email of the corpus, and one years after all its travel
    for day in (dt.datetime(y, 1, 1, tzinfo=dt.UTC) for y in (year - 1, year + 4)):
        with util.clock_at(day):
            card = evalrun.run_corpus(c, recorded=True)
        wrong = [e.name for e in card.emails if not e.right]
        assert card.right, f"clock at {day.date()}: {wrong}"


def test_a_wrong_answer_is_caught(tmp_path, capsys):
    root = copy_corpus(tmp_path)
    path = root / "05-chain-hotel" / "answer.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    stay = doc["answer"]["reservations"][0]
    stay["end"] = (dt.date.fromisoformat(stay["end"]) + dt.timedelta(days=1)).isoformat()
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    card = evalrun.run_corpus(corpus.load(root), recorded=True)
    others = {e.name: e for e in card.emails}
    hotel = others.pop("05-chain-hotel")
    assert not hotel.right and not card.right
    assert [f.path for f in hotel.fields if not f.right] == ["reservations.0.check_out"]
    assert all(e.right for e in others.values())
    capsys.readouterr()
    assert cli.main(["eval", str(root), "--recorded"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  05-chain-hotel" in out
    assert "extraction reservations.0.check_out:" in out


def test_a_missing_answer_fails_its_email_instead_of_skipping_it():
    c = corpus.load(corpus.default_dir())
    card = evalrun.run_corpus(c, recorded=True, extractor=NoAnswer())
    hotel = next(e for e in card.emails if e.name == "05-chain-hotel")
    assert not hotel.right
    assert any(d.startswith("status:") for d in hotel.outcome_diffs)
    assert not card.right
    assert len(card.emails) == len(c.entries)
    assert card.lines()[-1].endswith(f" of {len(c.entries)} emails right")


def test_an_email_the_extractor_stops_reading_is_caught(monkeypatch):
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])
    card = evalrun.run_corpus(corpus.load(corpus.default_dir()), recorded=True)
    flight = next(e for e in card.emails if e.name == "01-markup-flight")
    assert not flight.right and not card.right
    assert parsers(card)["kitinerary"] == 0


def test_an_empty_or_missing_corpus_is_an_error(tmp_path, capsys):
    source = corpus.default_dir()
    empty = tmp_path / "empty"
    empty.mkdir()
    shutil.copytree(source / "config", empty / "config")
    doc = json.loads((source / "corpus.json").read_text(encoding="utf-8"))
    doc["demo"]["entries"] = []
    (empty / "corpus.json").write_text(json.dumps(doc), encoding="utf-8")
    assert corpus.load(empty).entries == ()
    capsys.readouterr()
    assert cli.main(["eval", str(empty), "--recorded"]) == 2
    assert f"eval: {empty} holds no entries" in capsys.readouterr().err.splitlines()
    assert cli.main(["eval", str(tmp_path / "nowhere"), "--recorded"]) == 2
    last = capsys.readouterr().err.splitlines()[-1]
    assert last.startswith("eval: ") and last.endswith("no corpus here")


def test_a_corpus_named_from_the_working_directory_is_read(monkeypatch, capsys):
    monkeypatch.chdir(corpus.default_dir().parents[1])
    here = os.path.join("tests", "corpus")
    assert corpus.load(here).root.is_absolute() and corpus.problems(here) == []
    n = len(corpus.load(here).entries)
    capsys.readouterr()
    assert cli.main(["eval", here, "--recorded"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"eval: {n} of {n} emails right"


def test_eval_exit_codes_and_the_saved_scorecard(tmp_path, capsys):
    saved = tmp_path / "scorecard.json"
    n = len(corpus.load(corpus.default_dir()).entries)
    capsys.readouterr()
    assert cli.main(["eval", "--recorded", "--json", str(saved)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"eval: {n} of {n} emails right"
    text = saved.read_text(encoding="utf-8")
    again = evalrun.run_corpus(corpus.load(corpus.default_dir()), recorded=True).as_json()
    assert text == json.dumps(again, indent=1, sort_keys=True) + "\n"
    doc = json.loads(text)
    assert doc == again
    assert doc["schema"] == 2 and doc["totals"]["emails"] == n
    assert doc["totals"]["emails_right"] == n
    assert not {"time", "date", "root"} & keys(doc)


def test_eval_never_reads_the_installs_own_settings(tmp_path, monkeypatch):
    install = tmp_path / "install"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "travellers.yaml").write_text(
        'owner:\n  names: ["Pat Example"]\n', encoding="utf-8"
    )
    own = {
        "TZ": "Pacific/Auckland",
        "TRIPBOX_HOME_AIRPORTS": "BOS",
        "TRIPBOX_AIRTRAIL_URL": "http://flightlog.example:3000",
        "TRIPBOX_AIRTRAIL_API_KEY": "placeholder",
        "TRIPBOX_AIRTRAIL_DRY_RUN": "true",
        "TRIPBOX_CONFIG_DIR": str(elsewhere),
        "TRIPBOX_DATA_DIR": str(install / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(install / "archive"),
        "TRIPBOX_LOGS_DIR": str(install / "logs"),
    }
    for name, value in own.items():
        monkeypatch.setenv(name, value)
    time.tzset()
    zone = time.tzname
    # the run's own directory is made under this one, and removed
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    assert cli.main(["eval", "--recorded"]) == 0
    assert os.environ["TZ"] == "Pacific/Auckland" and time.tzname == zone
    assert os.environ["TRIPBOX_CONFIG_DIR"] == str(elsewhere)
    assert not install.exists()
    assert list(scratch.iterdir()) == []


def test_an_email_whose_processing_raises_is_saved_with_its_error(monkeypatch):
    c = corpus.load(corpus.default_dir())
    last = c.entries[-1]  # no later email depends on the last one being filed
    real = evalrun.process_raw

    def breaks_on_the_last(ctx, raw, **kw):
        if raw == last.raw:
            raise RuntimeError("the archive is full")
        return real(ctx, raw, **kw)

    monkeypatch.setattr(evalrun, "process_raw", breaks_on_the_last)
    doc = evalrun.run_corpus(c, recorded=True).as_json()
    emails = {e["name"]: e for e in doc["emails"]}
    assert list(emails) == [e.name for e in c.entries]
    broken = emails.pop(last.name)
    assert broken["error"] == "RuntimeError: the archive is full"
    assert broken["right"] is False
    assert all(e["right"] and e["error"] is None for e in emails.values())
    monkeypatch.setattr(evalrun, "process_raw", real)

    # an extractor that raises is caught by the pipeline itself: the email is filed as failed,
    # which the saved outcome says, and it carries no error of its own
    class Broken:
        model = DEFAULT_MODEL

        def extract(self, nm, hint=None):
            raise RuntimeError("the model fell over")

    doc = evalrun.run_corpus(c, recorded=True, extractor=Broken()).as_json()
    hotel = next(e for e in doc["emails"] if e["name"] == "05-chain-hotel")
    assert hotel["right"] is False and hotel["error"] is None
    assert "status: want 'processed', got 'failed'" in hotel["outcome"]


def test_a_scorecard_that_cannot_be_saved_is_an_error(tmp_path, capsys):
    target = tmp_path / "no-such-directory" / "scorecard.json"
    capsys.readouterr()
    assert cli.main(["eval", "--recorded", "--json", str(target)]) == 2
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1].startswith("eval: ")  # printed before the write
    assert f"eval: cannot write {target}" in captured.err.splitlines()
    assert not target.parent.exists()


def test_without_recorded_answers_eval_asks_what_is_configured(tmp_path, monkeypatch, capsys):
    for name in ("TRIPBOX_LLM_API_KEY", "TRIPBOX_NO_NETWORK", REPLAY_ENV, RECORD_ENV):
        monkeypatch.delenv(name, raising=False)
    c = corpus.load(corpus.default_dir())
    card = evalrun.run_corpus(c, recorded=False)
    emails = {e.name: e for e in card.emails}
    assert card.answers == "none" and card.model is None
    assert emails["01-markup-flight"].right
    assert not emails["05-chain-hotel"].right
    capsys.readouterr()
    assert cli.main(["eval"]) == 1

    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    capsys.readouterr()
    assert cli.main(["eval"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert "eval: TRIPBOX_NO_NETWORK is set; run with --recorded" in err
    monkeypatch.delenv("TRIPBOX_NO_NETWORK")

    # the replay and record switches would answer from, or write to, a directory of their own
    answers = tmp_path / "answers"
    monkeypatch.setenv(RECORD_ENV, str(answers))
    assert cli.main(["eval"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert f"eval: {RECORD_ENV} is set; run with --recorded" in err
    monkeypatch.setenv(REPLAY_ENV, str(answers))
    assert cli.main(["eval"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert f"eval: {REPLAY_ENV} is set; run with --recorded" in err
    assert cli.main(["eval", "--recorded"]) == 0
    assert not answers.exists()
    monkeypatch.delenv(REPLAY_ENV)
    monkeypatch.delenv(RECORD_ENV)

    class Priced:
        """Answers from the corpus, each at a fixed price."""

        model = "a-stand-in-model"

        def __init__(self):
            self.answers = corpus.CorpusAnswers(c)
            self.asked = 0

        def extract(self, nm, hint=None):
            self.asked += 1
            extraction, _ = self.answers.extract(nm, hint)
            return extraction, Usage(self.model, 1000, 100, 0.002)

    priced = Priced()
    card = evalrun.run_corpus(c, recorded=False, extractor=priced)
    doc = card.as_json()
    assert card.answers == "model" and card.right
    assert doc["model"] == Priced.model
    assert priced.asked >= 1
    assert doc["totals"]["cost_usd"] == pytest.approx(0.002 * priced.asked)
    assert f"model cost: ${0.002 * priced.asked:.4f}" in card.lines()


def test_eval_without_recorded_goes_through_the_cap(monkeypatch, capsys):
    """Without --recorded, eval asks the configured model under its cap, as a poll would: with
    a monthly budget of 0 the provider is never asked, and every email only the model reads
    waits instead of being scored as read."""
    import anthropic

    asked = []

    class Messages:
        def parse(self, **kw):
            asked.append(kw)
            raise AssertionError("the model was asked")

    class Client:
        def __init__(self, **kw):
            self.messages = Messages()

    for name in ("TRIPBOX_NO_NETWORK", REPLAY_ENV, RECORD_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", Client)
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "placeholder")
    monkeypatch.setenv("TRIPBOX_LLM_MONTHLY_BUDGET_USD", "0")
    c = corpus.load(corpus.default_dir())
    environ = {k: os.environ[k] for k in cli.EVAL_MODEL_KEYS if os.environ.get(k)}
    card = evalrun.run_corpus(c, recorded=False, environ=environ)
    emails = {e.name: e for e in card.emails}
    assert card.answers == "model" and asked == []
    by_model = [e.name for e in c.entries if e.outcome["read_by"] == "model"]
    assert by_model and all(not emails[name].right for name in by_model)
    assert all(emails[name].read_by is None for name in by_model)
    capsys.readouterr()
    assert cli.main(["eval"]) == 1 and asked == []

