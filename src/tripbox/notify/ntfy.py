# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""ntfy: each notice is one POST to the topic's address, its text as the body. The address is a
capability (whoever knows it can read the topic) and the token a password, so neither is ever
written to the log."""

from __future__ import annotations

import logging

import httpx2

from tripbox.log import redact
from tripbox.notify.notifier import TITLES, Notice

log = logging.getLogger("tripbox.ntfy")

TIMEOUT = 20.0
MAX_BYTES = 4096  # the most of a body ntfy sends as a message; a longer one is cut there


class Ntfy:
    name = "ntfy"

    def __init__(self, url: str, token: str = "", *, http: httpx2.Client | None = None) -> None:
        self.url = url
        self.token = token
        self.http = http or httpx2.Client(timeout=TIMEOUT)

    def send(self, notice: Notice) -> None:
        """POST the notice: its own title, else its event's, a tap opening its link, low priority
        when it is quiet, its body masked and cut to MAX_BYTES. A notice that does not go is one
        warning naming the event and why (a status or an error's kind, never its text, which may
        quote the address); this never raises."""
        headers = {"Title": notice.title or TITLES[notice.event]}
        if notice.link:
            headers["Click"] = notice.link
        if notice.quiet:
            headers["Priority"] = "low"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = redact(notice.body).encode("utf-8")
        if len(body) > MAX_BYTES:  # cut on a character's boundary
            body = body[:MAX_BYTES].decode("utf-8", errors="ignore").encode("utf-8")
        try:
            response = self.http.post(
                self.url,
                content=body,
                headers=headers,
                timeout=TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - its text may quote the address
            log.warning("ntfy: a %s notice was not sent (%s)", notice.event, type(exc).__name__)
            return
        if not response.is_success:
            log.warning(
                "ntfy: a %s notice was not sent (HTTP %d)", notice.event, response.status_code
            )
