# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The OpenAI-compatible provider: any server that speaks OpenAI's Chat Completions (OpenAI's own
service, or one you run yourself) reads the email's text, never its attachments, and answers in the
wire schema. It asks for an answer that fits the schema first; a server that refuses that request
(HTTP 400 or 422) is asked again once in JSON mode, with the schema stated in the system message,
and is asked that way from then on. Every answer is validated against the wire schema. Each way
the call fails is a `ModelError` of its kind (`_failure`), whose text never quotes the key, the
server's address or what the server sent back."""

from __future__ import annotations

import json
import logging
from typing import Any

import openai
import pydantic

from tripbox.mail.normalize import NormalizedMessage
from tripbox.parse import budget
from tripbox.parse.models import Extraction, WireExtraction, to_extraction
from tripbox.parse.provider import MAX_TOKENS, SYSTEM, ModelError, Usage, build_text

log = logging.getLogger("tripbox.openai")

# The key the SDK is given for a server that takes none (the SDK builds no client without one).
NO_KEY = "no-key"
# OpenAI's own service, for a key with no address: the address is always given, so nothing in
# the environment (OPENAI_BASE_URL) sends the key elsewhere.
HOSTED = "https://api.openai.com/v1"
TIMEOUT = 180.0
MAX_RETRIES = 2
SCHEMA = WireExtraction.model_json_schema()
# Not strict: OpenAI's strict mode takes only schemas whose every field is required.
SCHEMA_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "wire_extraction", "schema": SCHEMA},
}
JSON_MODE = {"type": "json_object"}
JSON_MODE_SYSTEM = (
    f"{SYSTEM}\nAnswer with one JSON object, and nothing else, that fits this JSON schema:\n"
    f"{json.dumps(SCHEMA)}"
)
# The statuses with which a server refuses a request that asks for a JSON schema.
SCHEMA_REJECTED_STATUSES = (400, 422)
# The HTTP statuses that say the server cannot answer now: a timeout, a conflict, the rate limit;
# and every server error. 401 and 403 say the key is not taken.
UNAVAILABLE_STATUSES = (408, 409, 429)
KEY_REJECTED_STATUSES = (401, 403)
# The error code OpenAI's own service gives, on a 429, for an account with no credit left; a
# server that answers 402 (Payment Required) says the same.
NO_CREDIT_CODE = "insufficient_quota"
NO_CREDIT_STATUS = 402
# tripbox check asks once, briefly
CHECK_TIMEOUT = 30.0
NO_LISTING = "the model server listed no models"
NO_ANSWER = "the model server sent no answer"


class OpenAIProvider:
    """`price`: the model's price per million tokens (input, output) when the settings give one;
    else the shipped table's, which knows few of the models such a server serves."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        base_url: str = "",
        client: Any | None = None,
        price: tuple[float, float] | None = None,
    ) -> None:
        self.model = model
        self.price = budget.price_of(model, price)
        # the server refused a JSON schema and took JSON mode: every later call asks that way
        self.json_mode = False
        # OpenAI's own service (no address) limits the answer by max_completion_tokens, which its
        # reasoning models require; a server at an address by max_tokens, which they all take
        self.limit = "max_tokens" if base_url else "max_completion_tokens"
        self.client = client or openai.OpenAI(
            api_key=api_key or NO_KEY,
            base_url=base_url or HOSTED,
            timeout=TIMEOUT,
            max_retries=MAX_RETRIES,
        )

    def extract(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[Extraction, Usage]:
        wire, usage = self.extract_wire(nm, hint)
        parsed = to_extraction(wire)
        log.info(
            "model extraction: %d reservations, confidence %.2f, %s in / %s out tokens",
            len(parsed.reservations),
            parsed.confidence,
            usage.input_tokens,
            usage.output_tokens,
        )
        return parsed, usage

    def extract_wire(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[WireExtraction, Usage]:
        """The model's answer as it arrived (the wire schema), with its usage. The model is
        given the rules and the email's text alone."""
        text = build_text(nm, hint)
        if not self.json_mode:
            try:
                return self._read(self._ask(SYSTEM, text, SCHEMA_FORMAT))
            except openai.APIStatusError as exc:
                if exc.status_code not in SCHEMA_REJECTED_STATUSES:
                    raise _failure(exc) from None
        try:
            resp = self._ask(JSON_MODE_SYSTEM, text, JSON_MODE)
        except openai.APIStatusError as exc:
            raise _failure(exc) from None
        self.json_mode = True
        return self._read(resp)

    def served(self) -> list[str]:
        """The names of the models the server lists: one request, with no retry (tripbox check)."""
        try:
            page = self.client.with_options(timeout=CHECK_TIMEOUT, max_retries=0).models.list()
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            raise _failure(exc) from None
        except Exception:  # an answer the SDK cannot read as a page at all
            raise ModelError("invalid", NO_LISTING, word="no_answer") from None
        data = getattr(page, "data", None)
        if not isinstance(data, list):
            raise ModelError("invalid", NO_LISTING, word="no_answer")
        return [m.id for m in data if isinstance(getattr(m, "id", None), str)]

    def _ask(self, system: str, text: str, response_format: dict) -> Any:
        """One Chat Completions call. An HTTP error is the caller's to read; no connection is
        an outage; a body that says it is JSON and is not is no answer (the SDK's own reading
        of it raises a ValueError)."""
        try:
            return self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                response_format=response_format,
                **{self.limit: MAX_TOKENS},
            )
        except openai.APIConnectionError as exc:  # a timeout is one too
            raise _failure(exc) from None
        except ValueError:
            raise ModelError("invalid", NO_ANSWER, word="no_answer") from None

    def _read(self, resp: Any) -> tuple[WireExtraction, Usage]:
        """The answer in a response, validated against the wire schema. A body that is no chat
        completion (the SDK hands back what it could not read) has no answer."""
        choices = getattr(resp, "choices", None)
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = getattr(choice, "message", None)
        finish = getattr(choice, "finish_reason", None)
        if getattr(message, "refusal", None):
            raise ModelError("refused", "model refused the request")
        if finish == "content_filter":
            raise ModelError("refused", "the server's content filter stopped the answer")
        if finish == "length":
            raise ModelError("truncated", "the answer was cut off (finish_reason=length)")
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ModelError("invalid", NO_ANSWER, word="no_answer")
        try:
            wire = WireExtraction.model_validate_json(content)
        except pydantic.ValidationError:
            # never quoted: the answer holds what the email says
            raise ModelError("invalid", "the answer does not fit the wire schema") from None
        usage = getattr(resp, "usage", None)
        tokens_in, tokens_out = _tokens(usage, "prompt_tokens"), _tokens(usage, "completion_tokens")
        return wire, Usage(
            model=self.model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=budget.cost(self.price, tokens_in, tokens_out),
        )


def _tokens(usage: Any, name: str) -> int:
    """A count the response gives, or 0 when the server sends none."""
    n = getattr(usage, name, None)
    return n if isinstance(n, int) and n >= 0 else 0


def _failure(exc: openai.APIStatusError | openai.APIConnectionError) -> ModelError:
    """The kind of failure the SDK's error is. The text is the status and a fixed phrase: the
    SDK's own message quotes the server's reply, which can echo the key or the address."""
    if isinstance(exc, openai.APITimeoutError):
        return ModelError(
            "unavailable", "the model server did not answer in time", word="unreachable"
        )
    if isinstance(exc, openai.APIConnectionError):
        return ModelError("unavailable", "no connection to the model server", word="unreachable")
    status = exc.status_code
    if status in KEY_REJECTED_STATUSES:
        detail = f"the model server did not take the key (HTTP {status})"
        return ModelError("unavailable", detail, word="key_rejected")
    if status == NO_CREDIT_STATUS or (status == 429 and exc.code == NO_CREDIT_CODE):
        detail = f"the model server's account has no credit (HTTP {status})"
        return ModelError("unavailable", detail, word="no_credit")
    if status == 429:
        return ModelError("unavailable", "rate limited (HTTP 429)", word="rate_limited")
    if status in UNAVAILABLE_STATUSES or status >= 500:
        detail = f"the model server cannot answer now (HTTP {status})"
        return ModelError("unavailable", detail, word="server_error")
    return ModelError("invalid", f"the model server refused the request (HTTP {status})")
