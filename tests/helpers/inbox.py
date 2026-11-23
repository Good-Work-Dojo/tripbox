# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A mailbox held in memory behind the `Inbox` protocol (`tripbox.mail.imap`), for the tests: no
server and no socket. Each message has a UID, the day it arrived and a size; the folders it is
copied into are recorded, and so is every fetch of a message's bytes and every folder call,
attempted or made."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from tripbox.ledger import Ledger
from tripbox.mail.imap import (
    KV_LAST_UID,
    KV_MAILBOX,
    KV_UIDVALIDITY,
    MAX_HEADER_BYTES,
    mailbox_key,
)
from tripbox.util import now

Raw = bytes | Callable[[], bytes]


def small_message(n: int, sender: str = "Sample Mailer <news@mailer.example>") -> bytes:
    """A short email that books nothing, told apart from the others by `n`."""
    return (
        f"From: {sender}\r\nTo: trips@example.com\r\nSubject: Message {n}\r\n"
        f"Message-ID: <m{n}@mailer.example>\r\n\r\nNothing to book here.\r\n"
    ).encode()


class FakeInbox:
    """`messages` maps a UID to the message's bytes, or to what builds them when they are
    fetched; `days` maps a UID to the day it arrived (a message given none arrived on the day
    the clock says when it was given). `sizes` holds a size the server states for a message, in
    place of its bytes' length. While `fail_copy` is set every copy raises, and while
    `fail_folders` is set every folder call does. `account` is the host and login it is read
    with."""

    def __init__(
        self,
        uidvalidity: int = 1,
        messages: dict[int, Raw] | None = None,
        *,
        days: dict[int, dt.date] | None = None,
        account: str = "imap.example.com|trips@example.com",
    ) -> None:
        self.account = account
        self._uv = uidvalidity
        self.messages: dict[int, Raw] = dict(messages or {})
        today = now().date()
        self.days: dict[int, dt.date] = {uid: today for uid in self.messages} | dict(days or {})
        self.sizes: dict[int, int] = {}
        self.folders: set[str] = set()
        self.copies: list[tuple[int, str]] = []
        self.fetched: list[int] = []  # every fetch_raw call, by UID
        self.headers: list[int] = []  # every fetch_header call, by UID
        # every folder call, as made, before it can fail: ("ensure", names) or ("copy", uid, folder)
        self.folder_calls: list[tuple] = []
        self.fail_copy = False
        self.fail_folders = False
        self._top = max(self.messages, default=0)  # the highest UID ever given

    @classmethod
    def large(
        cls, n: int, since: dt.date, *, per_day: int = 100, uidvalidity: int = 1
    ) -> FakeInbox:
        """`n` small messages, UIDs 1 to n, `per_day` of them arriving each day from `since` on;
        each message's bytes are built only when it is fetched."""
        inbox = cls(uidvalidity)
        for uid in range(1, n + 1):
            inbox.messages[uid] = lambda uid=uid: small_message(uid)
            inbox.days[uid] = since + dt.timedelta(days=(uid - 1) // per_day)
        inbox._top = n
        return inbox

    def new_to(self, ledger: Ledger) -> FakeInbox:
        """Every message here is new to `ledger`, as if tripbox had read this mailbox since
        before the first arrived: its UIDVALIDITY is stored and the cursor sits just below its
        first message."""
        ledger.kv.set(KV_UIDVALIDITY, str(self._uv))
        ledger.kv.set(KV_MAILBOX, mailbox_key(self.account))
        ledger.kv.set(KV_LAST_UID, str(min(self.messages, default=self.uidnext()) - 1))
        return self

    def deliver(self, raw: Raw, day: dt.date | None = None) -> int:
        """A message arrives: it takes the next UID, which is returned."""
        uid = self.uidnext()
        self.messages[uid] = raw
        self.days[uid] = day or now().date()
        self._top = uid
        return uid

    def expunge(self, uid: int) -> None:
        """A message is deleted on the server; its UID is never given again."""
        del self.messages[uid]
        self.days.pop(uid, None)
        self.sizes.pop(uid, None)

    def renumber(self, uidvalidity: int) -> None:
        """The server starts the mailbox over under a new UIDVALIDITY: every message gets a new
        UID, from 1 in the order they arrived, and keeps its day and size."""
        order = sorted(self.messages)
        new = {old: uid for uid, old in enumerate(order, 1)}
        self.messages = {new[old]: self.messages[old] for old in order}
        self.days = {new[old]: self.days[old] for old in order}
        self.sizes = {new[old]: size for old, size in self.sizes.items() if old in new}
        self._uv = uidvalidity
        self._top = len(order)

    def raw(self, uid: int) -> bytes:
        """A message's bytes, not counted as a fetch."""
        value = self.messages[uid]
        return value() if callable(value) else value

    # the Inbox protocol
    def uidvalidity(self) -> int:
        return self._uv

    def uidnext(self) -> int:
        return max(self._top, max(self.messages, default=0)) + 1

    def uids_after(self, last_uid: int) -> list[int]:
        return sorted(u for u in self.messages if u > last_uid)

    def uids_since(self, day: dt.date) -> list[int]:
        return sorted(u for u in self.messages if self.days[u] >= day)

    def size(self, uid: int) -> int:
        return self.sizes[uid] if uid in self.sizes else len(self.raw(uid))

    def fetch_raw(self, uid: int) -> bytes:
        self.fetched.append(uid)
        return self.raw(uid)

    def fetch_header(self, uid: int) -> bytes:
        """The message's header block, through its blank line, at most MAX_HEADER_BYTES."""
        raw = self.raw(uid)
        self.headers.append(uid)
        ends = [(i, len(sep)) for sep in (b"\r\n\r\n", b"\n\n") if (i := raw.find(sep)) >= 0]
        head = raw[: sum(min(ends))] if ends else raw
        return head[:MAX_HEADER_BYTES]

    def ensure_folders(self, names: tuple[str, ...]) -> None:
        self.folder_calls.append(("ensure", names))
        if self.fail_folders:
            raise OSError("the server refuses folders")
        self.folders.update(names)

    def copy_to(self, uid: int, folder: str) -> None:
        self.folder_calls.append(("copy", uid, folder))
        if self.fail_copy or self.fail_folders:
            raise OSError("imap down")
        self.copies.append((uid, folder))
