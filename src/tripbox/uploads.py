# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What the page is handed to read — a pasted confirmation, an email file or a PDF — stored as it
came in the ledger's waiting list (`uploads`) for the worker to read through the pipeline. The
page reads none of it: this module tells what a body is from its bytes alone, keeps a name only
to show, and stores the body within its limits. It imports nothing that reads mail, asks a model
or writes a flight log, so the page's container can take an upload holding no secret; the
pipeline takes its email test, and the one rule of what an upload came to, from here, never the
reverse."""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import re
import secrets
import unicodedata
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tripbox.ledger import Ledger
    from tripbox.pipeline.run import MessageResult

MAX_BYTES = 10 * 1024 * 1024  # the largest body taken, a file or a paste
MAX_WAITING = 20  # uploads not read yet (waiting or being read); one more is refused
MAX_TEXT_CHARS = 200_000  # the longest text taken: far more than any confirmation prints
MAX_NAME_CHARS = 120  # of a file's name, as shown
# The readings an upload is taken for: one a worker left being read this many times (its
# reading kills the worker, say) is given up, `failed` with the word `stopped`, its bytes kept
MAX_READINGS = 3
# Why an upload was refused (the first four, `Refused.code`) or not filed once read (the rest,
# `uploads.error`: it could not be read, a worker stopped MAX_READINGS times while reading it, or
# its message waits for the model, `run.WAIT_REASONS`).
ERRORS = (
    "too_large",
    "wrong_type",
    "empty",
    "full",
    "unreadable",
    "stopped",
    "no_model",
    "over_budget",
    "model_down",
)
# What the status page says of an upload whose message waits for the model to be able to read
# it; the resume pass of the next poll that can files it.
WAITING = {
    "over_budget": "The model's budget is used, so this waits. It is read when the budget allows.",
    "model_down": "The model could not be reached, so this waits. It is tried again every few"
    " minutes.",
}
# The states an upload ends in with nothing filed: the same bytes handed in again are read again.
DEAD_ENDS = ("nothing", "failed")

# The email a PDF is read as (`as_email`) names it by its file's name, or by these.
PDF_NAME = "upload.pdf"
PDF_SUBJECT = "(uploaded PDF)"

MAX_HEAD = 256 * 1024  # of a body's start, read for the header block of an email
BOM = b"\xef\xbb\xbf"
_LEADING = re.compile(rb"[ \t\r\n]*")
# A header line: its name (printable, no colon, no space), a colon, its value.
_FIELD = re.compile(rb"([!-9;-~]{1,80}):[ \t]*(.*)")
# The headers only mail carries, by name and by how a name starts: a line of the form
# `Word: value` alone says nothing, since pasted text holds such lines too.
_MAIL_ONLY = frozenset(
    (
        b"message-id mime-version content-type content-transfer-encoding return-path"
        b" delivered-to dkim-signature authentication-results in-reply-to received-spf"
    ).split()
)
_MAIL_ONLY_START = (b"x-", b"arc-", b"list-", b"resent-")
# And those that are mail's by what they hold: a hop a message took, an address of mail.
_HOP = re.compile(rb"(?:from|by)[ \t]", re.I)
_ADDRESSED = frozenset(b"from to cc bcc sender reply-to".split())
_AN_ADDRESS = re.compile(rb"[^\s<>@,;:\"()]{1,64}@[^\s<>@,;:\"()]{1,255}")
# A control character (Unicode's Cc: C0, DEL and C1) other than the white space a text holds:
# tab, line feed, vertical tab, form feed (which a PDF's text puts between its pages) and
# carriage return.
NOT_TEXT = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
PDF_MAGIC = b"%PDF-"


class Refused(ValueError):
    """An upload the waiting list does not take; `code` says why (one of the first four of
    ERRORS)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _only_mail_has(name: bytes, value: bytes) -> bool:
    """Whether a header line is one only mail carries."""
    if name in _MAIL_ONLY or name.startswith(_MAIL_ONLY_START):
        return True
    if name == b"received":
        return _HOP.match(value) is not None
    if name not in _ADDRESSED:
        return False
    found = _AN_ADDRESS.search(value)
    return found is not None and b"." in found.group().rpartition(b"@")[2]


def email_of(data: bytes) -> bytes | None:
    """The email in `data`, from its first header line on; None when the bytes are no email.
    The one rule that tells an email from text, for whatever is handed in: an upload, an
    `ingest`, a `report`.

    An email may stand after a byte-order mark, white space and the `From ` line a mailbox file
    puts before each message: none of them is part of it. It begins with header lines (a name
    and a colon, with or without a space after it; a line that starts with white space goes on
    with the one before), however many; and one of them, before the first line that is no
    header, is a header only mail carries: `Message-ID`, `MIME-Version`, `Content-Type`,
    `Return-Path`, `Delivered-To` and their like, any `X-` header, a `Received` that names a
    hop, or a `From`, `To`, `Cc` that holds an address of mail. Lines of the form `Word: value`
    alone make no email: pasted text holds them too."""
    start = len(BOM) if data.startswith(BOM) else 0
    start = _LEADING.match(data, start).end()
    if data.startswith(b"From ", start):
        end = data.find(b"\n", start, start + 1024)
        if end < 0:
            return None
        start = end + 1
    at, limit, seen = start, min(len(data), start + MAX_HEAD), False
    while at < limit:
        end = data.find(b"\n", at, limit)
        line = data[at : end if end >= 0 else limit].rstrip(b"\r")
        at = end + 1 if end >= 0 else limit
        if line[:1] in (b" ", b"\t"):
            if not seen:
                return None
            continue
        field = _FIELD.fullmatch(line)
        if field is None:
            return None
        seen = True
        if _only_mail_has(field.group(1).lower(), field.group(2)):
            return data[start:]
    return None


def looks_like_email(data: bytes) -> bool:
    """Whether the bytes are an email (`email_of`): read as one, else as text."""
    return email_of(data) is not None


def _is_text(body: bytes) -> bool:
    """UTF-8 holding no control character but white space (no NUL, no escape, no C1
    control)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return NOT_TEXT.search(text) is None


def sniff(body: bytes) -> str | None:
    """What the body is, from its bytes alone and never from a name or a declared type: `pdf`,
    `eml` (`looks_like_email`) or `text`; None for anything else — an archive, an image or an
    office file holds a byte that is no UTF-8 or a control character within its first bytes."""
    if body.startswith(PDF_MAGIC):
        return "pdf"
    if looks_like_email(body):
        return "eml"
    if body and _is_text(body):
        return "text"
    return None


def clean_name(name: str | None) -> str | None:
    """The name of an uploaded file as it is shown: its last part after `/` and `\\`, with no
    control or format character (no NUL, no escape, no mark that reorders what follows), white
    space trimmed, at most MAX_NAME_CHARS characters; None when nothing is left. It is never
    used to open a file."""
    last = re.split(r"[/\\]", name or "")[-1]
    kept = "".join(ch for ch in last if unicodedata.category(ch) not in ("Cc", "Cf"))
    kept = kept.strip()[:MAX_NAME_CHARS].strip()
    return kept or None


def as_email(name: str | None, body: bytes, at: dt.datetime) -> bytes:
    """A PDF handed in, as the email that carries it: the pipeline reads it as it reads a PDF
    attached to mail. One attachment, named as the file was (`clean_name`, else PDF_NAME), the
    Subject that name (else PDF_SUBJECT), the Date `at` (in UTC) and no sender; its boundary
    comes from the PDF's bytes, so one file at one instant is always the same email."""
    shown = clean_name(name)
    msg = EmailMessage()
    msg["MIME-Version"] = "1.0"
    msg["Subject"] = shown or PDF_SUBJECT
    msg["Date"] = email.utils.format_datetime(at.astimezone(dt.UTC))
    msg.add_attachment(body, maintype="application", subtype="pdf", filename=shown or PDF_NAME)
    # a boundary holds a `-`, which base64 never does, so it cannot occur in the attachment
    msg.set_boundary("tripbox-" + hashlib.sha256(body).hexdigest()[:32])
    return msg.as_bytes()


def booked(ledger: Ledger, res: MessageResult) -> bool:
    """Whether the reading `res` of a message came to a booking: a row of the log of its
    message, of an email attached to it, or of the earlier delivery of the same email one of
    them was set aside for, points at one (also a row an earlier reading wrote)."""
    todo = [res]
    while todo:
        r = todo.pop()
        todo.extend(r.children)
        for message_id in (r.message_row_id, r.same_as):
            if message_id is not None and any(
                row["trip_item_id"] is not None for row in ledger.items.by_message(message_id)
            ):
                return True
    return False


def came_to(ledger: Ledger, res: MessageResult) -> tuple[str, str | None, int | None] | None:
    """What an upload whose message the reading `res` read came to, on its first reading and on
    every later one: its state, the word for why nothing was filed, and the message whose
    bookings its status page shows (the earlier delivery its message was set aside for, else
    its own). `filed` when it `booked`; else `nothing`, with why when the message waits for the
    model (`run.WAIT_REASONS`), or `failed`, `unreadable`, when the reading failed. None when
    nothing was read (the message was finished before) and it booked nothing."""
    shown = res.same_as or res.message_row_id
    if booked(ledger, res):
        return "filed", None, shown
    if res.status == "skipped":
        return None
    if res.status == "waiting":
        return "nothing", res.wait_reason, shown
    if res.status == "failed":
        return "failed", "unreadable", shown
    return "nothing", None, shown


def submit(
    ledger: Ledger, body: bytes, *, name: str | None, submitted_by: str | None
) -> tuple[str, bool]:
    """Store `body` in the waiting list: (its token, True), or the token of the upload with the
    same bytes and False (the same upload twice is one upload while it waits, is being read or
    was filed). Bytes whose upload ended with nothing filed (`DEAD_ENDS`) are taken again: that
    upload waits again with its body stored anew, under the same token, and True. Refused,
    storing nothing: `too_large` (over MAX_BYTES, or a text over MAX_TEXT_CHARS), `empty`
    (nothing but white space), `wrong_type` (`sniff` says none of the three) and `full`
    (MAX_WAITING are not read yet). The look-up, the count and the write are one write
    transaction, so two submissions at once never both take the last place."""
    if len(body) > MAX_BYTES:
        raise Refused("too_large")
    if not body.strip():
        raise Refused("empty")
    kind = sniff(body)
    if kind is None:
        raise Refused("wrong_type")
    if kind == "text" and len(body.decode("utf-8")) > MAX_TEXT_CHARS:
        raise Refused("too_large")
    digest = hashlib.sha256(body).hexdigest()
    with ledger.transaction():
        same = ledger.uploads.by_sha(digest)
        if same is not None and same["state"] not in DEAD_ENDS:
            return same["token"], False
        if ledger.uploads.waiting() >= MAX_WAITING:
            raise Refused("full")
        if same is not None:
            ledger.uploads.take_again(same["id"], body)
            return same["token"], True
        token = secrets.token_urlsafe(16)
        ledger.uploads.add(
            token=token,
            kind=kind,
            name=clean_name(name),
            sha256=digest,
            body=body,
            submitted_by=submitted_by,
        )
    return token, True
