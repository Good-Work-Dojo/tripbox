# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The corpus filed with a flight log kept, against its truth (`tests/corpus/flightlog.json`,
written by hand from the corpus's own files): the flight log is an AirTrail held in memory that
knows every airport and airline the corpus names, and the pass after each message brings it in
step. The flight log reached for real is `test_flightlog_matrix.py`'s."""

import pytest
from helpers.e2e import as_truth, file_corpus, flight_log_truth, held, refile_corpus
from helpers.flightlog import Scripted, seed

from tripbox import corpus
from tripbox.write.airtrail import MEMORY_USER_ID
from tripbox.write.sync import Syncer

# A flight log in the settings, never reached: the context's client is swapped for one in memory.
FLIGHT_LOG = {
    "TRIPBOX_AIRTRAIL_URL": "http://flightlog.example:3000",
    "TRIPBOX_AIRTRAIL_API_KEY": "placeholder",
    "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
}


def in_memory(ctx) -> None:
    """The context's flight log becomes one held in memory over its ledger, seeded for the
    corpus; nothing leaves the process."""
    c = corpus.load(corpus.default_dir())
    seed(ctx.ledger, c)
    if ctx.airtrail is not None:
        ctx.airtrail.close()
    log = Scripted(ctx.ledger)
    ctx.airtrail = log
    ctx.syncer = Syncer(
        store=ctx.trips, ledger=ctx.ledger, book=ctx.trips.book, client=log, refs=None,
        dry_run=False,
    )


@pytest.fixture
def filed(tmp_path):
    ctx = file_corpus(tmp_path, FLIGHT_LOG, prepare=in_memory)
    yield ctx
    ctx.ledger.close()


def named_the_owner(request: tuple) -> bool:
    body = request[-1]
    return any(p.get("userId") == MEMORY_USER_ID for p in body.get("passengers") or [])


def by_day_and_number(flights: list[dict]) -> list[dict]:
    return sorted(flights, key=lambda f: (f["date"], f["flight"]))


def code_and_day(key: str) -> tuple[str, str]:
    carrier, number, day, _ = key.split("|")
    return f"{carrier}{int(number)}", day


def test_the_corpus_with_a_flight_log_sends_the_owners_flights_and_nobody_elses(filed):
    log = filed.airtrail
    assert isinstance(log, Scripted)
    got = by_day_and_number([as_truth(f, MEMORY_USER_ID) for f in log.flights])
    assert got == flight_log_truth()
    assert log.sent and all(named_the_owner(r) for r in log.sent)
    (relatives,) = corpus_entries("18-relative-itinerary")
    theirs = {code_and_day(i["key"]) for i in relatives.outcome["items"] if i["kind"] == "flight"}
    assert len(theirs) == 2
    assert not theirs & {(f["flight"], f["date"]) for f in got}


def test_filing_the_corpus_again_creates_no_flight_and_changes_none(filed):
    log = filed.airtrail
    before = {f["id"]: held(f) for f in log.flights}
    log.requests.clear()
    refile_corpus(filed)
    assert "create" not in [r[0] for r in log.requests]
    assert {f["id"]: held(f) for f in log.flights} == before
    assert all(named_the_owner(r) for r in log.sent)


def test_the_truth_names_only_flights_the_corpus_holds_for_the_owner():
    c = corpus.load(corpus.default_dir())
    owners = {
        code_and_day(i["key"])
        for e in c.entries
        for i in e.outcome["items"]
        if i["kind"] == "flight" and i["booking"] is not None and i["people"]["me"] == 1
        and "|" in i["key"] and not i["key"].startswith("draft|")
    }
    truth = flight_log_truth()
    assert truth == by_day_and_number(truth)
    for f in truth:
        assert (f["flight"], f["date"]) in owners, f
        assert f["people"][0] == "me"


def corpus_entries(name: str) -> list[corpus.Entry]:
    c = corpus.load(corpus.default_dir())
    return [e for e in c.entries if e.name == name]
