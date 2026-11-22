# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""One way out for everything tripbox tells the owner: a `Notice` goes to every channel that is
set up (Telegram, ntfy, or none). The pipeline never asks which: with no channel a notice goes
nowhere."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from tripbox.log import redact

if TYPE_CHECKING:
    from tripbox.config import Settings
    from tripbox.trips.store import TripItem

log = logging.getLogger("tripbox.notify")

# What a notice is about: a message's bookings were filed, a booking waits for a look, something
# failed, the mailbox stopped and started answering, the flight log refused or went quiet, the
# model's budget is used up.
EVENTS = ("filed", "needs_look", "failure", "mail_down", "mail_up", "sync_error", "budget_reached")
# One fixed title per event, plain ASCII (ntfy sends it as an HTTP header).
TITLES: dict[str, str] = {
    "filed": "tripbox: bookings filed",
    "needs_look": "tripbox: a booking to check",
    "failure": "tripbox: something failed",
    "mail_down": "tripbox: mailbox not reached",
    "mail_up": "tripbox: mailbox reached again",
    "sync_error": "tripbox: flight log problem",
    "budget_reached": "tripbox: model budget reached",
}
# The most characters of a notice's body any channel is given: a longer one is cut there, its
# last character `…`, once each configured secret in it is masked.
BODY_MAX = 3500


@dataclass(frozen=True)
class Notice:
    """One thing to tell the owner. `body` is the text every channel sends; `link` the page's
    address for it (a channel that can open one on a tap does); `quiet` asks for no sound;
    `booking` a booking the owner can answer about from the notice, whose answers depend on
    whether a flight log is kept (`flight_log`); `title`, plain ASCII, a channel's title for it
    in place of its event's (TITLES)."""

    event: str
    body: str
    link: str | None = None
    quiet: bool = False
    booking: TripItem | None = None
    flight_log: bool = False
    title: str | None = None

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise ValueError(f"no such notice event: {self.event!r}")
        if self.title is not None and not (self.title.isascii() and self.title.isprintable()):
            raise ValueError("a notice's title is plain ASCII")


class Channel(Protocol):
    name: str

    def send(self, notice: Notice) -> None: ...


class Notifier:
    def __init__(self, channels: Sequence[Channel] = ()) -> None:
        self.channels: tuple[Channel, ...] = tuple(channels)

    def notify(self, notice: Notice) -> None:
        """Send `notice` on every channel, its body masked and cut to BODY_MAX. One that fails is
        logged and stops no other; this never raises, so nothing that was filed is undone by a
        notice that could not go."""
        body = redact(notice.body)
        if len(body) > BODY_MAX:
            body = body[: BODY_MAX - 1] + "…"
        if body != notice.body:
            notice = dataclasses.replace(notice, body=body)
        for channel in self.channels:
            try:
                channel.send(notice)
            except Exception:  # noqa: BLE001 - the next channel still gets it
                log.exception(
                    "the %s channel could not send a %s notice", channel.name, notice.event
                )


def build(settings: Settings, *, offline: bool) -> Notifier:
    """The channels the settings set up: none while offline (nothing leaves the process) or while
    notifications are not on (off, or invalid)."""
    if offline or not settings.on("notifications"):
        return Notifier()
    channels: list[Channel] = []
    if settings.telegram_on:
        from tripbox.notify.telegram import Telegram, TelegramChannel

        telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
        channels.append(TelegramChannel(telegram))
    if settings.ntfy_on:
        from tripbox.notify.ntfy import Ntfy

        channels.append(Ntfy(settings.ntfy_url, settings.ntfy_token))
    return Notifier(channels)
