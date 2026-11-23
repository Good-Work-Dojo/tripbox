# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The Anthropic provider: a Claude model reads the email, and up to three of its PDFs as
documents, through structured outputs (messages.parse + Pydantic), answering in the wire schema.
Each way the call fails is a `ModelError` of its kind (`_failure`)."""

from __future__ import annotations

import base64
import logging
from typing import Any

import anthropic
import pydantic

from tripbox.log import redact
from tripbox.mail.normalize import NormalizedMessage
from tripbox.parse import budget
from tripbox.parse.models import Extraction, WireExtraction, to_extraction
from tripbox.parse.provider import (
    DEFAULT_MODEL,
    MAX_TOKENS,
    SYSTEM,
    ModelError,
    Usage,
    build_text,
)

log = logging.getLogger("tripbox.haiku")

MAX_PDFS = 3
MAX_PDF_BYTES = 5 * 1024 * 1024
# The HTTP statuses that say the model cannot answer now: a timeout, a conflict, the rate limit;
# and every server error. 401 and 403 say the key is not taken.
UNAVAILABLE_STATUSES = (408, 409, 429)
KEY_REJECTED_STATUSES = (401, 403)
NO_CREDIT_STATUS = 402  # a billing problem (`billing_error`), such as no credit left
# The model named is not one the API knows (a retired one, a typo): mail waits until it is set
# right, as for a key not taken. Any other refusal of the request is `rejected`.
UNKNOWN_MODEL_STATUS = 404
TIMEOUT = 180.0
MAX_RETRIES = 2


def estimate_cost(input_tokens: int, output_tokens: int, model: str = DEFAULT_MODEL) -> float:
    """What a call to `model` costs at the shipped price table's price (0.0 when it has none)."""
    return budget.cost(budget.price_of(model), input_tokens, output_tokens)


class AnthropicProvider:
    """`price`: the model's price per million tokens (input, output) when the settings give one;
    else the shipped table's."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
        *,
        price: tuple[float, float] | None = None,
    ) -> None:
        self.model = model
        self.price = budget.price_of(model, price)
        self.client = client or anthropic.Anthropic(
            api_key=api_key, timeout=TIMEOUT, max_retries=MAX_RETRIES
        )

    @staticmethod
    def build_content(nm: NormalizedMessage, hint: Extraction | None) -> list[dict]:
        content: list[dict] = []
        pdfs = [
            a
            for a in nm.attachments
            if len(a.data) <= MAX_PDF_BYTES
            and (
                a.content_type == "application/pdf"
                or a.data.startswith(b"%PDF-")  # sent as application/octet-stream
                or (a.filename or "").lower().endswith(".pdf")
            )
        ]
        for att in pdfs[:MAX_PDFS]:
            content.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(att.data).decode(),
                    },
                    "title": att.filename or "attachment.pdf",
                }
            )
        content.append({"type": "text", "text": build_text(nm, hint)})
        return content

    def extract(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[Extraction, Usage]:
        wire, usage = self.extract_wire(nm, hint)
        parsed = to_extraction(wire)
        log.info(
            "haiku extraction: %d reservations, confidence %.2f, %s in / %s out tokens ($%.4f)",
            len(parsed.reservations),
            parsed.confidence,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
        )
        return parsed, usage

    def extract_wire(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[WireExtraction, Usage]:
        """The model's answer as it arrived (the wire schema), with its usage."""
        content = self.build_content(nm, hint)
        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_format=WireExtraction,
            )
        except (
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
            pydantic.ValidationError,
        ) as exc:
            raise _failure(exc) from exc
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise ModelError("refused", "model refused the request")
        wire = getattr(resp, "parsed_output", None)
        if not isinstance(wire, WireExtraction):
            detail = f"no parsed output (stop_reason={stop})"
            if stop == "max_tokens":
                raise ModelError("truncated", detail)
            raise ModelError("invalid", detail, word="no_answer")
        u = resp.usage
        usage = Usage(
            model=self.model,
            input_tokens=int(u.input_tokens),
            output_tokens=int(u.output_tokens),
            cost_usd=budget.cost(self.price, int(u.input_tokens), int(u.output_tokens)),
        )
        return wire, usage


def _failure(
    exc: anthropic.APIStatusError | anthropic.APIConnectionError | pydantic.ValidationError,
) -> ModelError:
    """The kind of failure the SDK's error is; the detail is the text it has always had."""
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if isinstance(exc, anthropic.RateLimitError):
            detail = f"rate limited: {exc}"
        else:
            detail = f"API error {status}: {exc.message}"
        if status in KEY_REJECTED_STATUSES:
            return ModelError("unavailable", detail, word="key_rejected")
        if status == NO_CREDIT_STATUS or exc.type == "billing_error":
            # the account has no credit: the model answers again once it has, so mail waits
            return ModelError("unavailable", detail, word="no_credit")
        if status == 429:
            return ModelError("unavailable", detail, word="rate_limited")
        if status in UNAVAILABLE_STATUSES or status >= 500:
            return ModelError("unavailable", detail, word="server_error")
        if status == UNKNOWN_MODEL_STATUS:
            return ModelError("unavailable", detail, word="unknown_model")
        return ModelError("rejected", detail)
    if isinstance(exc, anthropic.APIConnectionError):  # a timeout is one too
        return ModelError("unavailable", f"connection error: {exc}", word="unreachable")
    # an answer that does not fit the wire schema (one cut off at max_tokens often does not):
    # the SDK raises while validating it rather than handing back a response
    return ModelError("invalid", f"invalid model output: {redact(str(exc))[:300]}")
