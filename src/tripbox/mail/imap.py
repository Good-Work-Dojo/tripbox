# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""IMAP access: UID-based fetch of new mail, raw bytes, labels via folder copy (a Gmail label is
a folder there).

The `Inbox` protocol is what the poller talks to; `ImapInbox` is the imap-tools/imaplib adapter,
tests use a fake. Mail is never deleted or marked seen.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from tripbox.ledger import Ledger
from tripbox.util import parse_iso

log = logging.getLogger("tripbox.imap")

# The folders handled mail is filed into while filing is on (TRIPBOX_MAIL_FILING=labels), each
# written with the server's own hierarchy separator in place of "/"; one a mailbox already has
# for another word is left alone.
LABEL_PROCESSED = "tripbox/processed"
LABEL_REJECTED = "tripbox/rejected"
LABEL_FAILED = "tripbox/failed"
ALL_LABELS = (LABEL_PROCESSED, LABEL_REJECTED, LABEL_FAILED)

KV_UIDVALIDITY = "imap_uidvalidity"
# The cursor: the highest UID read (or passed over); every poll reads what is above it.
KV_LAST_UID = "imap_last_uid"
# Whose the cursor is: `mailbox_key` of the host and login it was kept for. Another account is
# another mailbox, whatever UIDVALIDITY it states, and is read from now on.
KV_MAILBOX = "imap_mailbox"
# When the last poll that ended well did (pipeline.run.poll writes it)
KV_LAST_POLL_OK = "last_poll_ok"
STARTED_AT_NOW = "mail: starting at now; older mail is read only by tripbox mail backfill"
# Socket timeout for every IMAP round trip. Without one a server that stops answering holds the
# poll forever, and the worker starts polls one after another — so nothing would poll again.
IMAP_TIMEOUT = 60.0
# A message over this many bytes is never fetched, only its header, for the sender check: one from
# an allowed sender is said once, a stranger's is rejected quietly, and the cursor moves past it.
MAX_MESSAGE_BYTES = 20 * 1024 * 1024
# The most of an oversize message's header that is fetched.
MAX_HEADER_BYTES = 256 * 1024


class Inbox(Protocol):
    account: str  # the host and login it is read with, `host|user`

    def uidvalidity(self) -> int: ...
    def uidnext(self) -> int: ...
    def uids_after(self, last_uid: int) -> list[int]: ...
    def uids_since(self, day: dt.date) -> list[int]: ...  # arrived on or after `day`, ascending
    def size(self, uid: int) -> int: ...
    def fetch_raw(self, uid: int) -> bytes: ...
    def fetch_header(self, uid: int) -> bytes: ...  # at most MAX_HEADER_BYTES of it
    def ensure_folders(self, names: tuple[str, ...]) -> None: ...
    def copy_to(self, uid: int, folder: str) -> None: ...


@dataclass(frozen=True)
class RawMessage:
    uid: int
    raw: bytes


@dataclass(frozen=True)
class Oversize:
    """A message over MAX_MESSAGE_BYTES, never fetched: its UID, the size the server gives and
    its header (at most MAX_HEADER_BYTES), which the sender check reads."""

    uid: int
    size: int
    header: bytes = b""


class ImapInbox:
    """Adapter over imap-tools' MailBox, plus raw imaplib fetch so the archive is byte-exact."""

    def __init__(self, host: str, user: str, password: str, folder: str = "INBOX") -> None:
        from imap_tools import MailBox

        self._mb = MailBox(host, timeout=IMAP_TIMEOUT).login(user, password, initial_folder=folder)
        self.account = f"{host}|{user}"
        self._folder = folder
        self._separator: str | None = None

    def close(self) -> None:
        try:
            self._mb.logout()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def uidvalidity(self) -> int:
        status = self._mb.folder.status(self._folder, ["UIDVALIDITY"])
        return int(status["UIDVALIDITY"])

    def uidnext(self) -> int:
        status = self._mb.folder.status(self._folder, ["UIDNEXT"])
        return int(status["UIDNEXT"])

    def uids_after(self, last_uid: int) -> list[int]:
        # IMAP returns at least the highest UID for N:* even when N exceeds it; filter here.
        uids = self._mb.uids(f"UID {last_uid + 1}:*")
        return sorted(int(u) for u in uids if int(u) > last_uid)

    def uids_since(self, day: dt.date) -> list[int]:
        from imap_tools import AND

        return sorted(int(u) for u in self._mb.uids(AND(date_gte=day)))  # IMAP SINCE

    def size(self, uid: int) -> int:
        typ, data = self._mb.client.uid("FETCH", str(uid), "(RFC822.SIZE)")
        for part in data or ():
            line = part[0] if isinstance(part, tuple) else part
            found = re.search(rb"RFC822\.SIZE (\d+)", line) if isinstance(line, bytes) else None
            if typ == "OK" and found:
                return int(found.group(1))
        raise RuntimeError(f"IMAP gave no size for uid {uid}: {typ}")

    def fetch_raw(self, uid: int) -> bytes:
        typ, data = self._mb.client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError(f"IMAP fetch failed for uid {uid}: {typ}")
        for part in data:
            if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
                return part[1]
        raise RuntimeError(f"IMAP fetch returned no body for uid {uid}")

    def fetch_header(self, uid: int) -> bytes:
        """The message's header block, at most MAX_HEADER_BYTES of it, never its body."""
        what = f"(BODY.PEEK[HEADER]<0.{MAX_HEADER_BYTES}>)"
        typ, data = self._mb.client.uid("FETCH", str(uid), what)
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError(f"IMAP header fetch failed for uid {uid}: {typ}")
        for part in data:
            if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
                return part[1]
        raise RuntimeError(f"IMAP header fetch returned nothing for uid {uid}")

    def _on_server(self, label: str, listed: list | None = None) -> str:
        """`label` as this server names the folder: its "/" written as the server's own
        hierarchy separator, read once from its folder list (`listed`, else asked for; "/" when
        it states none)."""
        if self._separator is None:
            delims = (f.delim for f in (self._mb.folder.list() if listed is None else listed))
            self._separator = next((d for d in delims if d and d.upper() != "NIL"), "/")
        return label.replace("/", self._separator)

    def ensure_folders(self, names: tuple[str, ...]) -> None:
        listed = self._mb.folder.list()
        existing = {f.name for f in listed}
        for name in names:
            folder = self._on_server(name, listed)
            if folder not in existing:
                self._mb.folder.create(folder)
                log.info("created the mailbox folder %s", folder)

    def copy_to(self, uid: int, folder: str) -> None:
        self._mb.copy(str(uid), self._on_server(folder))


def _since_the_last_good_poll(inbox: Inbox, ledger: Ledger) -> int:
    """The cursor that reads again what arrived since the day before the last good poll (a day
    of slack for the server's zone): just below the first UID a search from that day gives; with
    no such message, or no poll that ever ended well, the top of the mailbox."""
    last_ok = ledger.kv.get(KV_LAST_POLL_OK)
    try:
        day = parse_iso(last_ok).astimezone(dt.UTC).date() if last_ok else None
    except ValueError:
        day = None
    since = inbox.uids_since(day - dt.timedelta(days=1)) if day else []
    return since[0] - 1 if since else inbox.uidnext() - 1


def fetch_new(inbox: Inbox, ledger: Ledger) -> Iterator[RawMessage | Oversize]:
    """Yield the messages above the cursor in UID order, each fetched, or an Oversize for one
    over MAX_MESSAGE_BYTES, which is never fetched; the caller calls `mark_seen_uid` after each.
    A mailbox read for the first time is read from now on (older mail only by `tripbox mail
    backfill`), and so is one at another host or login than the cursor was kept for (KV_MAILBOX;
    a cursor kept before that was noted is taken as this mailbox's); one whose UIDVALIDITY changed
    is read again since the last good poll, and what the ledger holds of that is known by its
    bytes."""
    uv = inbox.uidvalidity()
    stored = ledger.kv.get(KV_UIDVALIDITY)
    mine = mailbox_key(inbox.account)
    held = ledger.kv.get(KV_MAILBOX)
    if not stored or (held and held != mine):
        top = inbox.uidnext() - 1
        with ledger.transaction():
            ledger.kv.set(KV_UIDVALIDITY, str(uv))
            ledger.kv.set(KV_LAST_UID, str(top))
            ledger.kv.set(KV_MAILBOX, mine)
        if stored:
            started = STARTED_AT_NOW.removeprefix("mail: ")
            log.warning("mail: another mailbox (host or login) than before; %s", started)
        else:
            log.info(STARTED_AT_NOW)
    elif stored != str(uv):
        cursor = _since_the_last_good_poll(inbox, ledger)
        log.warning(
            "UIDVALIDITY changed %s -> %s; reading again from uid %d (the ledger knows what it"
            " holds by its bytes)",
            stored,
            uv,
            cursor + 1,
        )
        with ledger.transaction():
            ledger.kv.set(KV_UIDVALIDITY, str(uv))
            ledger.kv.set(KV_LAST_UID, str(cursor))
    if held != mine and ledger.kv.get(KV_MAILBOX) != mine:
        ledger.kv.set(KV_MAILBOX, mine)  # a cursor kept before its mailbox was noted
    last_uid = int(ledger.kv.get(KV_LAST_UID, "0") or 0)
    for uid in inbox.uids_after(last_uid):
        yield from fetch(inbox, [uid])


def fetch(inbox: Inbox, uids: list[int]) -> Iterator[RawMessage | Oversize]:
    """Each of `uids` fetched, or an Oversize, its header alone fetched, when the server says it
    is over the cap."""
    for uid in uids:
        size = inbox.size(uid)
        if size > MAX_MESSAGE_BYTES:
            yield Oversize(uid=uid, size=size, header=inbox.fetch_header(uid))
        else:
            yield RawMessage(uid=uid, raw=inbox.fetch_raw(uid))


def backfill_uids(inbox: Inbox, ledger: Ledger, since: dt.date) -> list[int]:
    """What `tripbox mail backfill` reads: the messages that arrived on or after `since` at or
    below the cursor as it stands, ascending; every one of them when no cursor is stored for
    this mailbox (its UIDVALIDITY, its host and login) (what the ledger holds is then known by
    its bytes)."""
    uids = inbox.uids_since(since)
    last_uid = ledger.kv.get(KV_LAST_UID)
    held = ledger.kv.get(KV_MAILBOX)
    if (
        not last_uid
        or ledger.kv.get(KV_UIDVALIDITY) != str(inbox.uidvalidity())
        or (held and held != mailbox_key(inbox.account))
    ):
        return uids
    return [uid for uid in uids if uid <= int(last_uid)]


def mailbox_key(account: str) -> str:
    """What the ledger keeps of the host and login a cursor is for (KV_MAILBOX): a digest, so
    the login is not written there."""
    return hashlib.sha256(account.strip().lower().encode()).hexdigest()[:16]


def mark_seen_uid(ledger: Ledger, uid: int) -> None:
    ledger.kv.set(KV_LAST_UID, str(uid))


def label(inbox: Inbox, uid: int, name: str) -> None:
    try:
        inbox.copy_to(uid, name)
    except Exception as exc:  # noqa: BLE001 - labelling must never break processing
        log.warning("could not label uid %s as %s: %s", uid, name, exc)
