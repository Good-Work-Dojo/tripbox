# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What the model may spend. A call's cost comes from the model's price: the one the settings give
(TRIPBOX_LLM_PRICE_USD_PER_MTOK), else the shipped table's (`lists/prices.yaml`). The cap is
asked before every call to the configured provider (`Budgeted`, which `provider.build` wraps it
in): a priced model stops once this month's calls cost its monthly budget, one with no known price
once today's calls reach the number a day allows; a budget of 0 stops every model. Months and days
are UTC, and what was spent is what the ledger's `llm_calls` holds. An answer read back instead of
asked (a replay, one stored with its message, a recorded one, the corpus's) is never wrapped and
never counted. A model that cannot be reached is not asked again for BREAKER_SECONDS."""

from __future__ import annotations

import datetime as dt
import functools
import math
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

import yaml

from tripbox.parse.models import Extraction, WireExtraction
from tripbox.parse.provider import (
    BILLED,
    MAX_TOKENS,
    SYSTEM,
    ModelError,
    Provider,
    Usage,
    build_text,
)
from tripbox.util import now

if TYPE_CHECKING:
    from tripbox.config import Settings
    from tripbox.ledger import Ledger
    from tripbox.mail.normalize import NormalizedMessage

PRICES_FILE = "prices.yaml"
# How long after an answer that the model cannot be reached every call fails the same way
# without asking it (by `util.now`): the rest of a poll's mail waits at once instead of waiting
# out each call's timeout. It holds in the process that asked, until an answer or that time.
BREAKER_SECONDS = 60


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens. A prompt of more than `long_after` tokens bills the whole
    call at the `long_input` and `long_output` prices, when the provider has a tier for it."""

    input: float
    output: float
    long_after: int | None = None
    long_input: float | None = None
    long_output: float | None = None


@functools.cache
def _table() -> dict[str, Price]:
    text = resources.files("tripbox.lists").joinpath(PRICES_FILE).read_text(encoding="utf-8")
    rows = yaml.safe_load(text)["models"]
    return {str(name): _row(r) for name, r in rows.items()}


def _row(r: dict) -> Price:
    long = r.get("long_prompt")
    if not long:
        return Price(float(r["input"]), float(r["output"]))
    return Price(
        float(r["input"]),
        float(r["output"]),
        int(long["above_tokens"]),
        float(long["input"]),
        float(long["output"]),
    )


def price_of(model: str, override: tuple[float, float] | None = None) -> Price | None:
    """The price of `model`: `override` when given; else the table's row for its name; else the
    row whose name it starts with followed by `-` (a dated name), the longest when several do;
    else None (no known price)."""
    if override is not None:
        return Price(*override)
    table = _table()
    if model in table:
        return table[model]
    names = [name for name in table if model.startswith(name + "-")]
    return table[max(names, key=len)] if names else None


def cost(price: Price | None, input_tokens: int, output_tokens: int) -> float:
    """What a call costs at `price`, in US dollars to 6 places; 0.0 with no known price. A prompt
    longer than the price's `long_after` tokens bills the whole call at its long-prompt tier."""
    if price is None:
        return 0.0
    rate_in, rate_out = price.input, price.output
    if price.long_after is not None and input_tokens > price.long_after:
        rate_in, rate_out = price.long_input or rate_in, price.long_output or rate_out
    return round(input_tokens * rate_in / 1e6 + output_tokens * rate_out / 1e6, 6)


def billed(model: str, price: Price | None, sent: int, kind: str) -> Usage | None:
    """What a failed call of a BILLED kind is estimated to have cost: one input token per three
    characters sent, and MAX_TOKENS output tokens for an answer cut off at that limit (none for
    the others), priced as an answer is; None for a kind the model is not billed for."""
    if kind not in BILLED:
        return None
    tokens = (math.ceil(sent / 3), MAX_TOKENS if kind == "truncated" else 0)
    return Usage(model, *tokens, cost(price, *tokens))


def by_month(settings: Settings) -> bool:
    """Whether the model is capped by its monthly budget (it is priced, or the budget is 0, which
    stops every model) rather than by its calls a day."""
    priced = price_of(settings.llm_model, settings.llm_price) is not None
    return priced or settings.llm_monthly_budget_usd <= 0


class Budget:
    """The configured model's cap, from the settings and what the ledger says was spent."""

    def __init__(self, ledger: Ledger, settings: Settings) -> None:
        self.ledger = ledger
        self.settings = settings
        self.by_month = by_month(settings)

    def period(self) -> str:
        """The UTC month (YYYY-MM) the cap counts, or the UTC day (YYYY-MM-DD)."""
        return now().strftime("%Y-%m" if self.by_month else "%Y-%m-%d")

    def refusal(self) -> str | None:
        """`month` while this month's calls cost the monthly budget or more, `day` while today's
        calls are as many as a day allows or more (a model with no known price); else None."""
        if self.by_month:
            _, spent = self.ledger.llm.month_cost(self.period())
            return "month" if spent >= self.settings.llm_monthly_budget_usd else None
        calls = self.ledger.llm.day_count(self.period())
        return "day" if calls >= self.settings.llm_max_calls_per_day else None


class Budgeted:
    """The configured provider under the cap: while the budget refuses, a call raises
    ModelError("over_budget"), its word `month` or `day`, before the provider is asked. For
    BREAKER_SECONDS after the provider answered `unavailable`, a call raises that same failure
    (its detail and word) without asking it."""

    def __init__(self, inner: Provider, budget: Budget) -> None:
        self.inner = inner
        self.budget = budget
        self.model = inner.model
        self._down: tuple[dt.datetime, ModelError] | None = None  # when, and how it failed

    def _allowed(self) -> None:
        why = self.budget.refusal()
        if why == "month":
            raise ModelError("over_budget", "this month's model budget is used", word=why)
        if why == "day":
            raise ModelError("over_budget", "today's model calls are made", word=why)
        if self._down is not None:
            since, how = self._down
            if 0 <= (now() - since).total_seconds() < BREAKER_SECONDS:
                raise ModelError("unavailable", how.detail, word=how.word)

    def _asked[T](
        self, call: Callable[[], T], nm: NormalizedMessage, hint: Extraction | None
    ) -> T:
        """`call` under the cap and the breaker. A failure of a BILLED kind carries what it is
        estimated to have cost (`billed`: the rules and the email's text are what is sent)."""
        self._allowed()
        try:
            answer = call()
        except ModelError as exc:
            # an answer of any other kind came from the model: it can be reached
            self._down = (now(), exc) if exc.kind == "unavailable" else None
            if exc.kind in BILLED:
                price = price_of(self.model, self.budget.settings.llm_price)
                sent = len(SYSTEM) + len(build_text(nm, hint))
                exc.usage = billed(self.model, price, sent, exc.kind)
            raise
        self._down = None
        return answer

    def extract_wire(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[WireExtraction, Usage]:
        return self._asked(lambda: self.inner.extract_wire(nm, hint), nm, hint)

    def extract(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[Extraction, Usage]:
        return self._asked(lambda: self.inner.extract(nm, hint), nm, hint)
