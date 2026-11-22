# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The scorecard's two comparisons: what was read from an email against what it says, field by
field (`score_extraction`), and what filing the email did against what it is meant to do
(`outcome_view`, `compare_outcome`)."""

import datetime as dt

from helpers.eml import make_eml

from tripbox import corpus, scorecard
from tripbox.ledger import Ledger
from tripbox.parse.models import Extraction, Reservation, Segment, to_extraction
from tripbox.parse.provider import DEFAULT_MODEL, Usage
from tripbox.pipeline import run as runmod

FLIGHT = {
    "kind": "flight",
    "status": "confirmed",
    "confirmation_code": "QWS987",
    "passengers": ["Alex Sample", "Sam Sample"],
    "segments": [
        {"carrier_iata": "DL", "flight_number": "9412", "origin": "MSP", "destination": "SEA",
         "departure_local": "2027-03-11T08:05", "arrival_local": "2027-03-11T10:02"},
        {"carrier_iata": "DL", "flight_number": "9413", "origin": "SEA", "destination": "MSP",
         "departure_local": "2027-03-14T13:30", "arrival_local": None},
    ],
}
STAY = {
    "kind": "lodging",
    "status": "confirmed",
    "confirmation_code": "ZWT876",
    "passengers": ["Alex Sample"],
    "property_name": "Example Suites Seattle Harbor",
    "check_in": "2027-03-11",
    "check_out": "2027-03-14",
}
TRUTH = {"document_type": "booking", "reservations": [FLIGHT, STAY]}


def seg(**kw) -> Segment:
    return Segment(**{k: v for k, v in FLIGHT["segments"][0].items() if v is not None} | kw)


def read_flight(**kw) -> Reservation:
    legs = [
        Segment(**{k: v for k, v in s.items() if v is not None}) for s in FLIGHT["segments"]
    ]
    return Reservation(**(FLIGHT | {"segments": legs} | kw))


def read_stay(**kw) -> Reservation:
    return Reservation(**(STAY | kw))


def extraction(*reservations: Reservation) -> Extraction:
    return Extraction(document_type="booking", reservations=list(reservations))


def wrong(results) -> list[str]:
    return [r.path for r in results if not r.right]


def test_an_extraction_that_matches_the_truth_scores_every_unit():
    results = scorecard.score_extraction(TRUTH, extraction(read_flight(), read_stay()))
    assert len(results) == 1 + 4 + 12 + 4 + 3
    assert all(r.right for r in results)
    assert [r.path for r in results][:6] == [
        "document_type",
        "reservations.0.kind",
        "reservations.0.status",
        "reservations.0.confirmation_code",
        "reservations.0.passengers",
        "reservations.0.segments.0.carrier_iata",
    ]
    assert results[-1].path == "reservations.1.check_out"
    assert results[0].want == "booking" and results[0].got == "booking"


def test_values_are_compared_in_their_plain_form():
    truth = {
        "document_type": "booking",
        "reservations": [
            {
                **FLIGHT,
                "segments": [
                    {**FLIGHT["segments"][0], "origin": ["MSP", "MINNEAPOLIS/ST PAUL"]},
                    FLIGHT["segments"][1],
                ],
            },
            STAY,
        ],
    }
    legs = [
        seg(flight_number="09412", origin="MINNEAPOLIS/ST PAUL", carrier_iata="dl"),
        Segment(**{k: v for k, v in FLIGHT["segments"][1].items() if v is not None}),
    ]
    flight = read_flight(
        confirmation_code="qws987", passengers=["SAMPLE/ALEX QUINN", "Sam Sample"], segments=legs
    )
    stay = read_stay(
        check_in="2027-03-11T15:00", property_name="EXAMPLE  suites, Seattle Harbor"
    )
    assert wrong(scorecard.score_extraction(truth, extraction(flight, stay))) == []
    # the other order of writing, and last name first with a slash
    swapped = read_flight(passengers=["Sam Sample", "Alex Sample"])
    assert wrong(scorecard.score_extraction(TRUTH, extraction(swapped, read_stay()))) == []
    slash = read_flight(passengers=["SAMPLE/SAM", "Alex Sample"])
    assert wrong(scorecard.score_extraction(TRUTH, extraction(slash, read_stay()))) == []
    # a number written with a non-ASCII digit is not a number
    superscript = read_flight(segments=[seg(flight_number="\u00b2"), legs[1]])
    assert wrong(scorecard.score_extraction(TRUTH, extraction(superscript, read_stay()))) == [
        "reservations.0.segments.0.flight_number"
    ]
    # a day where the truth has a time is not the time
    day_only = read_flight(segments=[seg(departure_local="2027-03-11"), legs[1]])
    assert wrong(scorecard.score_extraction(TRUTH, extraction(day_only, read_stay()))) == [
        "reservations.0.segments.0.departure_local"
    ]
    # a listed reading accepts either, and a reading that is listed nowhere is wrong
    other = read_flight(segments=[seg(origin="MSP"), legs[1]])
    assert wrong(scorecard.score_extraction(truth, extraction(other, read_stay()))) == []
    other = read_flight(segments=[seg(origin="STP"), legs[1]])
    assert wrong(scorecard.score_extraction(truth, extraction(other, read_stay()))) == [
        "reservations.0.segments.0.origin"
    ]


def test_an_invented_value_and_an_extra_booking_count_as_wrong():
    # the truth prints no arrival for the second leg: reading one is an invention
    legs = [
        seg(),
        seg(flight_number="9413", origin="SEA", destination="MSP",
            departure_local="2027-03-14T13:30", arrival_local="2027-03-14T15:40"),
    ]
    results = scorecard.score_extraction(
        TRUTH, extraction(read_flight(segments=legs), read_stay())
    )
    assert wrong(results) == ["reservations.0.segments.1.arrival_local"]
    bad = next(r for r in results if not r.right)
    assert bad.want is None and bad.got == "2027-03-14T15:40"
    # a booking the truth lacks is one wrong unit
    extra = scorecard.score_extraction(
        TRUTH, extraction(read_flight(), read_stay(), read_stay(confirmation_code="QRS123"))
    )
    assert wrong(extra) == ["reservations.+0"]
    assert len(extra) == 24 + 1
    # so is a leg the truth lacks
    third = legs[:1] + [Segment(**{k: v for k, v in FLIGHT["segments"][1].items() if v})]
    third.append(seg(flight_number="9414"))
    more = scorecard.score_extraction(
        TRUTH, extraction(read_flight(segments=third), read_stay())
    )
    assert wrong(more) == ["reservations.0.segments.+0"]


def test_an_unstated_status_is_the_same_as_unknown():
    def right(want, got) -> bool:
        truth = {"document_type": "booking", "reservations": [{**STAY, "status": want}]}
        res = scorecard.score_extraction(truth, extraction(read_stay(status=got)))
        return next(r for r in res if r.path == "reservations.0.status").right

    assert right(None, "unknown")
    assert right("unknown", "unknown")
    assert not right(None, "confirmed")
    assert not right("confirmed", "unknown")


def test_bookings_are_paired_by_code_before_order():
    plain = {k: v for k, v in STAY.items() if k != "confirmation_code"}
    truth = {"document_type": "booking", "reservations": [plain, STAY]}
    coded = read_stay()
    bare = Reservation(**plain)
    results = scorecard.score_extraction(truth, extraction(coded, bare))
    assert len(results) == 1 + 2 * 7 and wrong(results) == []


def test_a_name_that_cannot_be_split_is_not_dropped():
    truth = {"document_type": "booking", "reservations": [{**STAY, "passengers": []}]}
    res = scorecard.score_extraction(truth, extraction(read_stay(passengers=["Mr"])))
    assert wrong(res) == ["reservations.0.passengers"]


def test_a_missed_booking_counts_every_unit_it_would_have_had():
    results = scorecard.score_extraction(TRUTH, extraction(read_flight()))
    missed = [r for r in results if r.path.startswith("reservations.1.")]
    assert len(missed) == 7
    assert all(not r.right and r.got is None for r in missed)
    assert wrong(results) == [r.path for r in missed]


def test_no_extraction_scores_nothing_right():
    results = scorecard.score_extraction(TRUTH, None)
    assert len(results) == 24
    assert not any(r.right for r in results)
    assert all(r.got is None for r in results)
    # even a field the truth leaves empty is not right when nothing was read
    empty = scorecard.score_extraction({"document_type": "marketing", "reservations": []}, None)
    assert [(r.path, r.right) for r in empty] == [("document_type", False)]


def test_the_outcome_view_says_what_was_filed_for_whom_and_where(tmp_path):
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(
        c, data_dir=tmp_path / "data", archive_dir=tmp_path / "archive", logs_dir=tmp_path / "logs"
    )
    entry = c.entry("05-chain-hotel")
    with corpus.process_as(s):
        ctx = runmod.build_context(s, ledger=Ledger.memory(), with_notifier=False)
        ctx.model = corpus.CorpusAnswers(c)
        res = runmod.process_raw(ctx, entry.raw, source="imap", notify=False)
        assert scorecard.outcome_view(res, ctx) == entry.outcome
        first = scorecard.outcome_view(res, ctx)["items"][0]["booking"]
        # the booking's open checks and its sync state, as the ledger holds them
        assert list(first) == ["status", "travellers", "trip", "checks", "sync"]
        assert (first["checks"], first["sync"]) == ([], "off")
        undated = c.entry("25-flight-without-date")
        res25 = runmod.process_raw(ctx, undated.raw, source="imap", notify=False)
        view25 = scorecard.outcome_view(res25, ctx)
        assert view25 == undated.outcome
        stored = ctx.trips.item_by_key(view25["items"][0]["key"])
        assert stored.checks, "the flight with no date is filed with a check open"
        assert view25["items"][0]["booking"]["checks"] == list(stored.checks)
        assert view25["items"][0]["booking"]["sync"] == stored.sync_state

        class Same:
            model = DEFAULT_MODEL

            def extract(self, nm, hint=None):
                return to_extraction(entry.answer), Usage(DEFAULT_MODEL, 0, 0, 0.0)

        again = make_eml(
            from_="Alex Sample <alex@example.com>",
            subject="Fwd: Your stay in Seattle",
            date=dt.datetime(2027, 2, 2, 9, 15, tzinfo=dt.timezone(dt.timedelta(hours=-6))),
            text="Forwarding the hotel note again, in case the first one went astray.\n\n"
            "Confirmation number: ZWT876\n",
            message_id="<fwd.again.q7p2@example.com>",
            headers={
                "Return-Path": "<alex@example.com>",
                "Authentication-Results": "mx.google.com; dkim=pass header.i=@example.com;"
                " spf=pass smtp.mailfrom=alex@example.com; dmarc=pass header.from=example.com",
            },
        ).as_bytes()
        ctx.model = Same()
        res2 = runmod.process_raw(ctx, again, source="imap", notify=False)
        view = scorecard.outcome_view(res2, ctx)
    assert view["status"] == "ignored"
    assert [i["result"] for i in view["items"]] == ["duplicate"]
    assert view["items"][0]["booking"] == first


def test_outcome_differences_are_listed_by_path():
    want = {
        "status": "processed",
        "items": [
            {"kind": "lodging", "key": "lodging|ZWT876", "result": "filed"},
            {"kind": "car", "key": "car|QRS123", "result": "filed"},
        ],
    }
    assert scorecard.compare_outcome(want, want) == ()
    got = {
        "status": "processed",
        "items": [{"kind": "lodging", "key": "lodging|ZWT877", "result": "filed"}],
        "gap": "x",
    }
    assert scorecard.compare_outcome({**want, "gap": "y"}, want) == ()
    assert scorecard.compare_outcome(want, got) == (
        "items.0.key: want 'lodging|ZWT876', got 'lodging|ZWT877'",
        "items.1: want {'kind': 'car', 'key': 'car|QRS123', 'result': 'filed'}, got <absent>",
        "gap: want <absent>, got 'x'",
    )


def test_the_outcome_view_takes_each_result_from_the_outcome(tmp_path):
    """Whatever became of a booking is the outcome's own word in the view, one that failed
    included; a booking that was not filed has no booking to show."""
    from tripbox.validate.rules import validate_extraction
    from tripbox.write.executor import Outcome
    from tripbox.write.planner import Action

    c = corpus.load(corpus.default_dir())
    s = corpus.settings(
        c, data_dir=tmp_path / "data", archive_dir=tmp_path / "archive", logs_dir=tmp_path / "logs"
    )
    with corpus.process_as(s):
        ctx = runmod.build_context(s, ledger=Ledger.memory(), with_notifier=False)
        (item,) = validate_extraction(
            extraction(read_stay()), refdata=ctx.refdata, book=ctx.book, email_date=None
        )
        words = ("filed", "updated", "duplicate", "ignored", "failed")
        res = runmod.MessageResult(
            None, "processed", None, outcomes=[Outcome(Action("file", item), w, "") for w in words]
        )
        view = scorecard.outcome_view(res, ctx)
    assert [i["result"] for i in view["items"]] == list(words)
    assert {i["key"] for i in view["items"]} == {item.key}
    assert [i["booking"] for i in view["items"]] == [None] * len(words)
