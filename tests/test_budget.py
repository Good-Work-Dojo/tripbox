# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The model's spend cap (`parse.budget`): what a call costs, by the shipped price table or the
price the settings give; the cap the configured provider is asked under before every call (a
priced model by its monthly budget, one with no known price by its calls a day, both counted in
UTC); mail that waits while the cap is reached, with one notice a period, and is read once the
cap allows; and the answers read back instead of asked (stored, replayed, the corpus's), which the
cap never refuses or counts. Every email is the corpus's own, read at run time; the model is a
stand-in that answers each with the corpus's answer and counts what it is asked."""

import contextlib
import dataclasses
import datetime as dt
import functools
import hashlib
from collections.abc import Iterator

import pytest

from tripbox import corpus, evalrun, util
from tripbox.ledger import Ledger, repo
from tripbox.notify.notifier import Notifier
from tripbox.parse import budget, provider
from tripbox.parse import pipeline as pl
from tripbox.parse.models import to_extraction
from tripbox.parse.provider import DEFAULT_MODEL, ModelError, Usage
from tripbox.parse.replay import ReplayExtractor, answer_path
from tripbox.pipeline import run

UNPRICED = "served-model"  # a model the price table does not know
KEY = {"TRIPBOX_LLM_API_KEY": "placeholder"}
MONTH = (
    "tripbox has used this month's model budget (${budget}). Mail that needs the model waits"
    " and is read when the budget allows; TRIPBOX_LLM_MONTHLY_BUDGET_USD raises it."
)
DAY = (
    "tripbox has made today's {n} model calls. Mail that needs the model waits until tomorrow"
    " (UTC); TRIPBOX_LLM_MAX_CALLS_PER_DAY raises it."
)


@functools.cache
def the_corpus() -> corpus.Corpus:
    return corpus.load(corpus.default_dir())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_by_the_model() -> list[corpus.Entry]:
    """The corpus's emails the model reads whose every booking is filed with nothing to check,
    each one email alone (none carries another attached), in corpus order."""
    return [
        e
        for e in the_corpus().entries
        if e.answer is not None
        and e.outcome["status"] == "processed"
        and e.outcome["read_by"] == "model"
        and e.outcome["items"]
        and all(i["result"] == "filed" and not i["booking"]["checks"] for i in e.outcome["items"])
        and b"message/rfc822" not in e.raw
    ]


def next_month(at: dt.datetime) -> dt.datetime:
    """The first instant of the UTC month after `at`'s."""
    first = at.astimezone(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + dt.timedelta(days=32)).replace(day=1)


def next_day(at: dt.datetime) -> dt.datetime:
    """The first instant of the UTC day after `at`'s."""
    day = at.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + dt.timedelta(days=1)


class Asked:
    """A stand-in for the configured provider: it answers each corpus email with the corpus's own
    answer, as a provider does (the wire schema, priced as the price table prices `model`), and
    counts every call. While `fails` holds a ModelError, each call raises it; `reading` is called
    while it reads."""

    def __init__(self, model: str = DEFAULT_MODEL, tokens: tuple[int, int] = (4000, 600)) -> None:
        self.model = model
        self.tokens = tokens
        self.calls = 0
        self.fails: ModelError | None = None
        self.reading = None
        self.answers = {sha(e.raw): e.answer for e in the_corpus().entries if e.answer}

    def extract_wire(self, nm, hint=None):
        self.calls += 1
        if self.reading is not None:
            self.reading()
        if self.fails is not None:
            raise self.fails
        wire = self.answers.get(sha(nm.raw))
        if wire is None:
            raise ModelError("invalid", "the corpus has no answer for this message")
        i, o = self.tokens
        return wire, Usage(self.model, i, o, budget.cost(budget.price_of(self.model), i, o))

    def extract(self, nm, hint=None):
        wire, usage = self.extract_wire(nm, hint)
        return to_extraction(wire), usage


class Told:
    """A notification channel that keeps every notice it is given."""

    name = "told"

    def __init__(self) -> None:
        self.notices = []

    def send(self, notice) -> None:
        self.notices.append(notice)

    def of(self, event: str) -> list[str]:
        return [n.body for n in self.notices if n.event == event]


def capped(ctx: run.Context, asked: Asked, **environ) -> None:
    """The context's settings with `environ` changed, and its model the stand-in under the cap
    they set, as `provider.build` wraps the configured provider (a restart with new settings)."""
    s = corpus.settings(
        the_corpus(),
        data_dir=ctx.settings.data_dir,
        archive_dir=ctx.settings.archive_dir,
        logs_dir=ctx.settings.logs_dir,
        environ={**KEY, **environ},
    )
    ctx.settings = s
    ctx.model = budget.Budgeted(asked, budget.Budget(ctx.ledger, s))


@contextlib.contextmanager
def pipeline(
    tmp_path, monkeypatch, *, model: bool = True, **environ
) -> Iterator[tuple[run.Context, Asked, Told]]:
    """A pipeline context over a new ledger under the corpus's settings, a model key and
    `environ`; with `model`, its model the stand-in under the cap those settings set. Every notice
    goes to a stand-in channel. The structured-data extractor finds nothing, so the model reads
    every email."""
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [])
    s = corpus.settings(
        the_corpus(),
        data_dir=tmp_path / "data",
        archive_dir=tmp_path / "archive",
        logs_dir=tmp_path / "logs",
        environ={**KEY, **environ},
    )
    with corpus.process_as(s):
        led = Ledger.open(s.ledger_path)
        ctx = run.build_context(s, ledger=led, with_model=False, with_notifier=False)
        told = Told()
        ctx.notifier = Notifier([told])
        asked = Asked(s.llm_model)
        if model:
            capped(ctx, asked, **environ)
        try:
            yield ctx, asked, told
        finally:
            led.close()


def spend(ctx: run.Context, usd: float) -> None:
    """A call already made this month (by the clock as it stands) that cost `usd`."""
    ctx.ledger.llm.add(
        message_id=None, model=DEFAULT_MODEL, input_tokens=0, output_tokens=0, cost_usd=usd
    )


def test_price_of_knows_the_shipped_model_a_dated_name_and_the_override():
    shipped = budget.price_of(DEFAULT_MODEL)
    assert (shipped.input, shipped.output) == (0.10, 0.50)
    # a dated name of a model the table holds is that model; another name is another model
    dated = f"{DEFAULT_MODEL}-{the_corpus().anchor.strftime('%Y%m%d')}"
    assert budget.price_of(dated) == shipped
    assert budget.price_of(DEFAULT_MODEL + "0") is None
    assert budget.price_of(UNPRICED) is None
    # the price the settings give wins over the table's, and prices a model it lacks
    assert budget.price_of(UNPRICED, (0.2, 0.8)) == budget.Price(0.2, 0.8)
    assert budget.price_of(DEFAULT_MODEL, (0.0, 0.0)) == budget.Price(0.0, 0.0)
    # dollars to 6 places; nothing with no known price
    assert budget.cost(shipped, 4000, 600) == 0.0007
    assert budget.cost(budget.Price(1.0, 3.0), 1, 1) == 0.000004
    assert budget.cost(None, 4000, 600) == 0.0


def test_a_prompt_over_the_long_threshold_bills_the_whole_call_at_the_higher_tier():
    shipped = budget.price_of(DEFAULT_MODEL)
    assert shipped.long_after == 100_000
    assert budget.cost(shipped, 100_000, 1000) == 0.0105  # at the threshold: the base tier
    assert budget.cost(shipped, 100_001, 1000) == 0.052501  # over it: input and output both
    # a price with no long tier, such as one the settings give, is flat
    assert budget.cost(budget.Price(0.1, 0.5), 200_000, 1000) == 0.0205


def test_over_the_monthly_budget_no_call_is_made_and_the_message_waits(tmp_path, monkeypatch):
    first, second = read_by_the_model()[:2]
    at = corpus.sent(first)
    with pipeline(tmp_path, monkeypatch, TRIPBOX_LLM_MONTHLY_BUDGET_USD="1.00") as (
        ctx,
        asked,
        told,
    ):
        # the configured provider is built under the cap, whichever it is
        for chosen in ({}, {"TRIPBOX_LLM_PROVIDER": "openai", "TRIPBOX_LLM_MODEL": UNPRICED}):
            s = corpus.settings(
                the_corpus(),
                data_dir=tmp_path / "data",
                archive_dir=tmp_path / "archive",
                logs_dir=tmp_path / "logs",
                environ={**KEY, **chosen},
            )
            built = provider.build(s, ctx.ledger)
            assert isinstance(built, budget.Budgeted) and built.budget.ledger is ctx.ledger
        with util.clock_at(at):
            filed = run.process_raw(ctx, first.raw, source="imap", uid=1)
            assert filed.status == "processed" and asked.calls == 1
            assert budget.Budget(ctx.ledger, ctx.settings).refusal() is None
            spend(ctx, 1.00)  # the whole budget, and the call just made
            assert budget.Budget(ctx.ledger, ctx.settings).refusal() == "month"
            res = run.process_raw(ctx, second.raw, source="imap", uid=2)
        assert asked.calls == 1  # the model was not asked
        assert (res.status, res.label, res.outcomes) == ("waiting", None, [])
        row = ctx.ledger.messages.get(res.message_row_id)
        assert (row["status"], row["error"], row["parser"]) == ("waiting", "over_budget", None)
        assert ctx.ledger.items.by_message(res.message_row_id) == []
        assert told.of("budget_reached") == [MONTH.format(budget="1.00")]
        assert ctx.ledger.messages.waiting() == {"over_budget": 1}
        # the month is UTC's: its last second still counts this month, its first the next
        with util.clock_at(next_month(at) - dt.timedelta(seconds=1)):
            assert run.resume_waiting(ctx) == 0
        assert asked.calls == 1 and ctx.ledger.messages.get(row["id"])["status"] == "waiting"
        with util.clock_at(next_month(at)):
            run.poll(ctx, None)
        assert asked.calls == 2
        row = ctx.ledger.messages.get(row["id"])
        assert (row["status"], row["error"], row["parser"]) == ("processed", None, "haiku")
        assert ctx.ledger.messages.waiting() == {}


def test_an_unpriced_model_is_capped_by_calls_per_day(tmp_path, monkeypatch):
    emails = read_by_the_model()[:3]
    at = corpus.sent(emails[0])
    with pipeline(
        tmp_path,
        monkeypatch,
        TRIPBOX_LLM_MODEL=UNPRICED,
        TRIPBOX_LLM_MAX_CALLS_PER_DAY="2",
        TRIPBOX_LLM_MONTHLY_BUDGET_USD="0.01",
    ) as (ctx, asked, told):
        cap = budget.Budget(ctx.ledger, ctx.settings)
        with util.clock_at(at):
            assert cap.period() == at.astimezone(dt.UTC).date().isoformat()
            got = [
                run.process_raw(ctx, e.raw, source="imap", uid=n).status
                for n, e in enumerate(emails, 1)
            ]
        assert got == ["processed", "processed", "waiting"] and asked.calls == 2
        # an unpriced call costs nothing, so the monthly budget never stops it
        assert ctx.ledger.llm.month_cost(at.astimezone(dt.UTC).strftime("%Y-%m"))[1] == 0.0
        assert told.of("budget_reached") == [DAY.format(n=2)]
        # tomorrow (UTC) the cap allows it, and the next poll reads it
        with util.clock_at(next_day(at) - dt.timedelta(seconds=1)):
            run.poll(ctx, None)
        assert asked.calls == 2
        with util.clock_at(next_day(at)):
            run.poll(ctx, None)
        assert asked.calls == 3 and ctx.ledger.messages.waiting() == {}
        # given a price, the same model is capped by the month instead
        priced = dataclasses.replace(ctx.settings, llm_price=(0.1, 0.5))
        with util.clock_at(at):
            assert budget.Budget(ctx.ledger, priced).period() == at.astimezone(dt.UTC).strftime(
                "%Y-%m"
            )
            assert budget.Budget(ctx.ledger, priced).refusal() is None
            # and with a budget of 0, no model is ever asked
            none = dataclasses.replace(ctx.settings, llm_monthly_budget_usd=0.0)
            assert budget.Budget(ctx.ledger, none).refusal() == "month"


def test_one_budget_notice_per_period(tmp_path, monkeypatch):
    emails = read_by_the_model()[:4]
    at = corpus.sent(emails[0])
    later = next_month(at) + dt.timedelta(days=2)
    with pipeline(tmp_path, monkeypatch, TRIPBOX_LLM_MONTHLY_BUDGET_USD="2.50") as (
        ctx,
        asked,
        told,
    ):
        with util.clock_at(at):
            spend(ctx, 2.50)
            for n, e in enumerate(emails[:3], 1):
                assert run.process_raw(ctx, e.raw, source="imap", uid=n).status == "waiting"
            assert ctx.ledger.kv.get("budget_alerted") == at.astimezone(dt.UTC).strftime("%Y-%m")
        assert told.of("budget_reached") == [MONTH.format(budget="2.50")]
        # the next month's budget is used up too: one notice more
        with util.clock_at(later):
            spend(ctx, 2.50)
            run.poll(ctx, None)  # the waiting mail waits on
            assert run.process_raw(ctx, emails[3].raw, source="imap", uid=4).status == "waiting"
        assert told.of("budget_reached") == [MONTH.format(budget="2.50")] * 2
        assert asked.calls == 0 and ctx.ledger.messages.waiting() == {"over_budget": 4}


def test_stored_replayed_and_corpus_answers_are_never_capped(tmp_path, monkeypatch):
    """With a budget of 0 no model is ever asked: a message read before is read again from the
    answer stored with it, recorded answers replay, and the corpus's own answers score the
    corpus whole; none of them is refused, and none adds a call."""
    emails = read_by_the_model()
    counted = []
    real = repo.LLMCalls.add
    # a message read while the budget allowed, read again by force with the budget at 0
    with pipeline(tmp_path / "stored", monkeypatch) as (ctx, asked, told):
        first = run.process_raw(ctx, emails[0].raw, source="imap", uid=1)
        assert first.status == "processed" and asked.calls == 1
        capped(ctx, asked, TRIPBOX_LLM_MONTHLY_BUDGET_USD="0")
        monkeypatch.setattr(repo.LLMCalls, "add", lambda *a, **kw: counted.append(kw))
        again = run.process_raw(ctx, emails[0].raw, source="replay", force=True)
        assert again.status == "ignored" and asked.calls == 1
        assert [o.result for o in again.outcomes] == ["duplicate"] * len(first.outcomes)
        assert again.parsed.parser == "haiku"
    monkeypatch.setattr(repo.LLMCalls, "add", real)
    # recorded answers, replayed offline: the context never builds the provider, so never caps it
    answers = tmp_path / "answers"
    answers.mkdir()
    for e in emails:
        answer_path(answers, e.raw).write_text(e.answer.model_dump_json())
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    monkeypatch.setenv(run.REPLAY_ENV, str(answers))
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [])
    s = corpus.settings(
        the_corpus(),
        data_dir=tmp_path / "replay" / "data",
        archive_dir=tmp_path / "replay" / "archive",
        logs_dir=tmp_path / "replay" / "logs",
        environ={**KEY, "TRIPBOX_LLM_MONTHLY_BUDGET_USD": "0"},
    )
    monkeypatch.setattr(repo.LLMCalls, "add", lambda *a, **kw: counted.append(kw))
    with corpus.process_as(s):
        ctx = run.build_context(s, with_notifier=False)
        try:
            assert type(ctx.model) is ReplayExtractor
            got = [run.process_raw(ctx, e.raw, source="imap", notify=False) for e in emails]
        finally:
            ctx.ledger.close()
    assert [r.status for r in got] == ["processed"] * len(emails)
    # the corpus scored whole: the real extractor reads what it reads, as `eval --recorded` does
    monkeypatch.undo()
    monkeypatch.setattr(repo.LLMCalls, "add", lambda *a, **kw: counted.append(kw))
    card = evalrun.run_corpus(
        the_corpus(), recorded=True, environ={**KEY, "TRIPBOX_LLM_MONTHLY_BUDGET_USD": "0"}
    )
    assert card.right
    assert counted == []
    with pytest.raises(ModelError) as refused:
        budget.Budgeted(Asked(), budget.Budget(Ledger.memory(), s)).extract_wire(None)
    assert (refused.value.kind, refused.value.word) == ("over_budget", "month")


def test_a_model_that_cannot_be_reached_is_not_asked_again_for_a_minute(tmp_path, monkeypatch):
    """The model cannot be reached: for BREAKER_SECONDS after that answer every call fails the
    same way without asking it, so the rest of the mail waits at once instead of each message
    waiting out its own timeout. Three messages: one call, three waiting, one notice. Once that
    time is past the model is asked again, and an answer clears the breaker at once."""
    emails = read_by_the_model()[:4]
    at = corpus.sent(emails[0])
    down = ModelError("unavailable", "no connection", word="unreachable")
    with pipeline(tmp_path, monkeypatch) as (ctx, asked, told):
        asked.fails = down
        with util.clock_at(at):
            got = [
                run.process_raw(ctx, e.raw, source="imap", uid=n)
                for n, e in enumerate(emails[:3], 1)
            ]
        assert [r.status for r in got] == ["waiting"] * 3 and asked.calls == 1
        assert ctx.ledger.messages.waiting() == {"model_down": 3}
        assert [r.parsed.error_word for r in got] == ["unreachable"] * 3
        assert [r.parsed.error for r in got] == [str(down)] * 3
        assert len(told.of("failure")) == 1 and ctx.ledger.llm.day_count(
            at.astimezone(dt.UTC).date().isoformat()
        ) == 0
        late = at + dt.timedelta(seconds=budget.BREAKER_SECONDS - 1)
        with util.clock_at(late):
            assert run.resume_waiting(ctx) == 0
        assert asked.calls == 1 and budget.BREAKER_SECONDS == 60
        asked.fails = None
        with util.clock_at(at + dt.timedelta(seconds=budget.BREAKER_SECONDS)):
            assert run.resume_waiting(ctx) == 3
        assert asked.calls == 4 and ctx.ledger.messages.waiting() == {}
        # an answer clears it: a failure a second later is asked about, and the next is not
        asked.fails = down
        with util.clock_at(at + dt.timedelta(seconds=budget.BREAKER_SECONDS + 1)):
            assert run.process_raw(ctx, emails[3].raw, source="imap", uid=4).status == "waiting"
            assert asked.calls == 5
            assert run.resume_waiting(ctx) == 0
        assert asked.calls == 5


def test_a_failed_call_the_model_was_billed_for_counts_against_the_cap(tmp_path, monkeypatch):
    """A refused, truncated or invalid answer was read, and billed: each adds one call to the
    ledger, so the calls a day and the month's cost see it, estimated at one input token per
    three characters the model was sent (its rules and the email's text) and, for an answer cut
    off, MAX_TOKENS output tokens, priced by the same table. A model that cannot be reached adds
    none. Two refused answers reach a cap of two calls a day: the third message waits over the
    cap, and the model is not asked."""
    import math

    emails = read_by_the_model()[:4]
    at = corpus.sent(emails[0])
    day = at.astimezone(dt.UTC).date().isoformat()
    with pipeline(
        tmp_path / "day",
        monkeypatch,
        TRIPBOX_LLM_MODEL=UNPRICED,
        TRIPBOX_LLM_MAX_CALLS_PER_DAY="2",
    ) as (ctx, asked, told):
        asked.fails = ModelError("refused", "model refused the request")
        with util.clock_at(at):
            got = [
                run.process_raw(ctx, e.raw, source="imap", uid=n).status
                for n, e in enumerate(emails[:3], 1)
            ]
        assert got == ["failed", "failed", "waiting"] and asked.calls == 2
        assert ctx.ledger.messages.waiting() == {"over_budget": 1}
        assert ctx.ledger.llm.day_count(day) == 2
    with pipeline(tmp_path / "priced", monkeypatch) as (ctx, asked, told):
        price = budget.price_of(ctx.settings.llm_model)
        kinds = ("refused", "truncated", "invalid", "unavailable")
        expected = []
        for n, (kind, e) in enumerate(zip(kinds, emails, strict=True), 1):
            asked.fails = ModelError(kind, f"the answer was {kind}")
            with util.clock_at(at + dt.timedelta(minutes=n)):
                res = run.process_raw(ctx, e.raw, source="imap", uid=n)
            assert res.status == ("waiting" if kind == "unavailable" else "failed"), kind
            if kind != "unavailable":
                sent = len(provider.SYSTEM) + len(provider.build_text(res.normalized, None))
                tokens = (math.ceil(sent / 3), provider.MAX_TOKENS if kind == "truncated" else 0)
                cost = budget.cost(price, *tokens)
                expected.append((res.message_row_id, ctx.settings.llm_model, *tokens, cost))
        rows = ctx.ledger.conn.execute(
            "SELECT message_id, model, input_tokens, output_tokens, cost_usd FROM llm_calls"
            " ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == expected and all(x[-1] > 0 for x in expected)
        month = at.astimezone(dt.UTC).strftime("%Y-%m")
        assert ctx.ledger.llm.month_cost(month) == (3, pytest.approx(sum(x[-1] for x in expected)))
        assert asked.calls == 4


def test_a_request_the_server_refused_costs_nothing_and_an_unknown_model_makes_mail_wait(
    tmp_path, monkeypatch
):
    """An HTTP refusal came back with no answer: nothing is billed, so no call is added to the
    ledger. A model the server does not know fails nothing: mail waits, told once, as for an
    outage; any other refusal (a request too large, say) fails its email."""
    emails = read_by_the_model()[:3]
    at = corpus.sent(emails[0])
    down = (
        "tripbox cannot reach the model (unknown_model). Mail that needs it waits and is read"
        " when it answers again."
    )
    with pipeline(tmp_path, monkeypatch) as (ctx, asked, told):
        asked.fails = ModelError("unavailable", "API error 404: model", word="unknown_model")
        with util.clock_at(at):
            got = [
                run.process_raw(ctx, e.raw, source="imap", uid=n).status
                for n, e in enumerate(emails[:2], 1)
            ]
        assert got == ["waiting", "waiting"] and told.of("failure") == [down]
        asked.fails = ModelError("rejected", "API error 413: too large")
        with util.clock_at(at + dt.timedelta(minutes=5)):
            res = run.process_raw(ctx, emails[2].raw, source="imap", uid=3)
        assert res.status == "failed" and res.parsed.usage is None
        assert len(told.of("failure")) == 2
        rows = ctx.ledger.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        assert rows == 0 and asked.calls == 2
