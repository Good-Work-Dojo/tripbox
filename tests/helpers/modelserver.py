# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""An OpenAI-compatible model server held in the test process: the transport of the SDK's own
client, so no socket is ever opened. Each request takes the next scripted behaviour; a request
with nothing scripted fails the test: it is recorded in UNSCRIPTED, which `tests/conftest.py`
checks when the test ends, since the SDK turns the transport's error into a failure of its own
that a provider may answer for. Every request is recorded (`requests`), with its method, path,
headers and JSON body.

A failure echoes what the request carried (its address and its Authorization header), as a
careless or hostile server may: the provider must never quote what comes back."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai

from tripbox.parse.models import WireExtraction

BASE_URL = "http://models.example/v1"

Behaviour = Callable[[httpx2.Request], httpx2.Response]
# Every request any fake server had nothing scripted for, as "METHOD path", until the test ends.
UNSCRIPTED: list[str] = []


def _echo(request: httpx2.Request) -> str:
    """What the request carried that a server must not hand back and the provider must not quote."""
    return f"{request.url} {request.headers.get('authorization', '')}"


def _completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    usage: tuple[int, int] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": "served-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        }
    return body


def _content(answer: WireExtraction | dict | str) -> str:
    if isinstance(answer, WireExtraction):
        return answer.model_dump_json()
    if isinstance(answer, dict):
        return json.dumps(answer)
    return answer


class FakeModelServer:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url
        self.requests: list[dict] = []
        self.script: list[Behaviour] = []

    # ------------------------------------------------------------------------- the SDK's side
    def http_client(self) -> httpx2.Client:
        """An HTTP client whose transport is this server."""
        return httpx2.Client(transport=httpx2.MockTransport(self._handle))

    def client(self, *, api_key: str = "placeholder-key", base_url: str | None = None) -> Any:
        """The SDK's client, talking to this server only; no retry, so one call is one request."""
        return openai.OpenAI(
            api_key=api_key,
            base_url=base_url or self.base_url,
            http_client=self.http_client(),
            max_retries=0,
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        if not self.script:
            UNSCRIPTED.append(f"{request.method} {request.url.path}")
            raise AssertionError(f"an unscripted request: {request.method} {request.url.path}")
        return self.script.pop(0)(request)

    # ------------------------------------------------------------------------- behaviours
    def answer(
        self, answer: WireExtraction | dict | str, *, usage: tuple[int, int] | None = None
    ) -> None:
        """A whole answer: a wire answer as JSON, a dict as JSON (one off the wire schema), or the
        content as given ("" is an empty answer). `usage`: (prompt, completion) tokens; None
        sends no usage, as some servers do."""
        content = _content(answer)
        self.script.append(
            lambda request: httpx2.Response(200, json=_completion(content, usage=usage))
        )

    def reject_schema(self, status: int = 400) -> None:
        """A server that takes no JSON schema: it refuses the request that asks for one."""

        def behaviour(request: httpx2.Request) -> httpx2.Response:
            message = f"response_format json_schema is not supported: {_echo(request)}"
            return httpx2.Response(status, json={"error": {"message": message}})

        self.script.append(behaviour)

    def truncate(self, answer: WireExtraction | dict | str) -> None:
        """An answer cut off at the token limit, whose content would parse all the same."""
        content = _content(answer)
        self.script.append(
            lambda request: httpx2.Response(
                200, json=_completion(content, finish_reason="length", usage=(1, 1))
            )
        )

    def refuse(self, *, filtered: bool = False) -> None:
        """The model declines: a refusal on the message (which echoes the request), or
        (filtered) the server's content filter stops the answer."""

        def behaviour(request: httpx2.Request) -> httpx2.Response:
            if filtered:
                return httpx2.Response(200, json=_completion("", finish_reason="content_filter"))
            refusal = f"I can't help with that: {_echo(request)}"
            return httpx2.Response(200, json=_completion(None, refusal=refusal))

        self.script.append(behaviour)

    def fail(self, status: int, code: str | None = None) -> None:
        """An HTTP error whose body echoes the request, with the error's `code` when given."""
        error = {"code": code} if code is not None else {}
        self.script.append(
            lambda request: httpx2.Response(
                status, json={"error": {"message": f"failed: {_echo(request)}", **error}}
            )
        )

    def drop(self, *, timeout: bool = False) -> None:
        """No answer: the connection fails, or (timeout) the server does not answer in time."""

        def behaviour(request: httpx2.Request) -> httpx2.Response:
            if timeout:
                raise httpx2.ReadTimeout(f"timed out: {_echo(request)}", request=request)
            raise httpx2.ConnectError(f"cannot connect: {_echo(request)}", request=request)

        self.script.append(behaviour)

    def broken(self) -> None:
        """A body that says it is JSON and is not."""
        self.script.append(
            lambda request: httpx2.Response(
                200, content=b"{", headers={"content-type": "application/json"}
            )
        )

    def garbage(self) -> None:
        """An answer that is no JSON at all (it echoes the request)."""
        self.script.append(
            lambda request: httpx2.Response(200, json=_completion(f"no JSON: {_echo(request)}"))
        )

    def listing(self, *names: str) -> None:
        """The models the server lists (`GET /models`)."""
        data = [{"id": n, "object": "model", "created": 0, "owned_by": "fake"} for n in names]
        self.script.append(
            lambda request: httpx2.Response(200, json={"object": "list", "data": data})
        )
