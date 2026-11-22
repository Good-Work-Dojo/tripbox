# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Telegram Bot API (send side) and the buttons of a booking's card. The texts live in
notify/text.py; the long-poll bot lives in notify/bot.py."""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx2

from tripbox.log import redact
from tripbox.util import now
from tripbox.validate import checks

if TYPE_CHECKING:
    from tripbox.notify.notifier import Notice
    from tripbox.trips.store import TripItem

log = logging.getLogger("tripbox.telegram")

MAX_LEN = 3900


class Telegram:
    def __init__(
        self, token: str, chat_id: str, *, http: httpx2.Client | None = None, enabled: bool = True
    ) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = http or httpx2.Client(timeout=60.0)
        self.enabled = enabled and bool(token and chat_id)

    def api(self, method: str, **params: Any) -> Any:
        if not self.enabled:
            shown = redact(json.dumps(params, default=str))[:200]  # masked, then cut
            log.info("telegram disabled; %s %s", method, shown)
            return None
        try:
            r = self.http.post(f"{self.base}/{method}", json=params)
            body = r.json()
        except (httpx2.HTTPError, ValueError) as exc:
            log.warning("telegram %s failed: %s", method, exc)
            return None
        if not body.get("ok"):
            log.warning("telegram %s rejected: %s", method, redact(str(body))[:200])
            return None
        return body.get("result")

    def send(
        self, text: str, *, reply_markup: dict | None = None, silent: bool = False
    ) -> int | None:
        """Send (chunked if long); returns the last message id.

        `silent` delivers without a sound or vibration (Telegram's disable_notification): for
        informational lines that can wait until the phone is next picked up.
        """
        last: int | None = None
        chunks = _chunks(redact(text), MAX_LEN)  # an error string may quote a key or password
        for i, chunk in enumerate(chunks):
            params: dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if silent:
                params["disable_notification"] = True
            if reply_markup and i == len(chunks) - 1:  # keyboard rides on the last chunk
                params["reply_markup"] = reply_markup
            res = self.api("sendMessage", **params)
            if isinstance(res, dict):
                last = res.get("message_id")
        return last

    def edit_text(self, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        """Put `text` on the message `message_id`, with the buttons `reply_markup` (none when
        not given: editing a message's text drops the buttons it does not name)."""
        params: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": redact(text)[:4000],
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        self.api("editMessageText", **params)

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.api("answerCallbackQuery", callback_query_id=callback_query_id, text=text[:200])


class TelegramChannel:
    """Notices to the chat: the text as it is, without a sound when the notice is quiet, and a
    booking's buttons under its card."""

    name = "telegram"

    def __init__(self, telegram: Telegram) -> None:
        self.telegram = telegram

    def send(self, notice: Notice) -> None:
        markup = (
            check_keyboard(notice.booking, flight_log=notice.flight_log)
            if notice.booking is not None
            else None
        )
        self.telegram.send(notice.body, reply_markup=markup, silent=notice.quiet)


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > size:  # one line longer than a message: hard-split, never drop it
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def check_keyboard(
    ti: TripItem, *, flight_log: bool, made: dt.datetime | None = None
) -> dict | None:
    """The buttons for a booking, a row for each thing a button can settle (`ck:<id>:<what>`),
    by the rule the page's box follows too (`checks.actions`; `flight_log` says one is kept):
    Looks right while a soft check other than nobody named is open, carrying the time its card
    is `made` (now when not given) in whole seconds since the epoch (`ck:<id>:ok:<seconds>`), so
    it says the card was right and nothing raised after it; the owner's two answers while nobody
    is named; Send again for a flight deleted in the flight log, while one is kept. None when no
    row applies (a hard check is fixed on the page)."""
    can = checks.actions(ti.checks, hold=ti.sync_hold, flight_log=flight_log)
    rows: list[list[dict]] = []
    if "looks_right" in can:
        seconds = int((made or now()).timestamp())
        rows.append([{"text": "Looks right", "callback_data": f"ck:{ti.id}:ok:{seconds}"}])
    if "on_it" in can:
        rows.append(
            [
                {"text": "I was on this flight", "callback_data": f"ck:{ti.id}:me"},
                {"text": "Not me", "callback_data": f"ck:{ti.id}:no"},
            ]
        )
    if "send_again" in can:
        rows.append([{"text": "Send again", "callback_data": f"ck:{ti.id}:again"}])
    return {"inline_keyboard": rows} if rows else None
