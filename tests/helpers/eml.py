# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Build small .eml byte strings for tests without static fixture files."""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime


def make_eml(
    *,
    from_: str,
    to: str = "trips@example.com",
    subject: str,
    date=None,
    text: str | None = None,
    html: str | None = None,
    headers: dict[str, str] | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    message_id: str | None = None,
    rfc822: EmailMessage | None = None,
) -> EmailMessage:
    m = EmailMessage()
    m["From"] = from_
    m["To"] = to
    m["Subject"] = subject
    if date is not None:
        m["Date"] = format_datetime(date)
    if message_id:
        m["Message-ID"] = message_id
    for k, v in (headers or {}).items():
        m[k] = v
    if text is not None and html is not None:
        m.set_content(text)
        m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(text or "")
    for filename, ctype, data in attachments or []:
        maintype, subtype = ctype.split("/", 1)
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    if rfc822 is not None:
        m.add_attachment(rfc822)
    return m


# How many header lines `delivered` puts before an email's own.
DELIVERY_HEADERS = 40


def delivered(raw: bytes) -> bytes:
    """`raw` as a mailbox holds it: under the header lines of its delivery, DELIVERY_HEADERS of
    them and more than 4 KB, in the email's own line endings. Every host is an example's."""
    end = "\r\n" if b"\r\n" in raw[:2000] else "\n"
    head = "".join(
        f"Received: from mta{n}.mail.example (mta{n}.mail.example [192.0.2.{n}]){end}"
        f"\tby mx{n}.mail.example with ESMTPS id k7q2m{n:04d}x{end}"
        for n in range(1, DELIVERY_HEADERS + 1)
    ).encode()
    assert len(head) > 4096
    return head + raw
