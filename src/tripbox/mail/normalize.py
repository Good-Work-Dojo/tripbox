# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""MIME -> NormalizedMessage: sender-origin gate, forward unwrapping, text extraction, attachments.

Three shapes of legitimate mail arrive in the inbox:
  * Gmail filter auto-forward: original headers kept, `X-Forwarded-For: <owner address> ...` added
    and `Return-Path` rewritten to `<local>+caf_=...` by Gmail.
  * Manual "Forward": From is the owner, body starts with a `---------- Forwarded message ---------`
    header block (original From/Date/Subject) followed by the original body.
  * Manual "Forward as attachment": From is the owner, the original is a `message/rfc822` part.
Anything else is `origin_ok=False` and is never parsed.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import email.utils
import logging
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("tripbox.normalize")

ATTACHMENT_TYPES = {
    "application/pdf",
    "application/vnd.apple.pkpass",
    "text/calendar",
    "application/octet-stream",
}
FORWARD_MARKERS = (
    re.compile(r"^-{3,}\s*Forwarded message\s*-{3,}\s*$", re.I | re.M),
    re.compile(r"^Begin forwarded message:\s*$", re.I | re.M),
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}\s*$", re.I | re.M),
)
HEADER_LINE = re.compile(r"^(From|Date|Subject|To|Cc|Sent|Reply-To):\s*(.*)$", re.I)
FWD_PREFIX = re.compile(r"^(?:(?:fwd?|fw|tr|wg)\s*:\s*)+", re.I)

_LOOSE_FORMATS = (
    "%a, %b %d, %Y at %I:%M %p",  # Gmail: Mon, Oct 19, 2026 at 9:30 AM
    "%a, %b %d, %Y, %I:%M %p",
    "%b %d, %Y at %I:%M %p",
    "%B %d, %Y at %I:%M:%S %p",  # Apple Mail: October 19, 2026 at 9:30:15 AM (zone stripped)
    "%B %d, %Y at %I:%M %p",
    "%A, %B %d, %Y %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %H:%M",
)
# trailing zone abbreviation (CDT, EST, GMT+2 ...) but never the AM/PM meridian
_TZ_ABBR = re.compile(r"\s+(?!(?:AM|PM)$)(?:[A-Z]{2,5}|GMT[+-]\d{1,2}(?::\d{2})?|UTC[+-]\d{1,2})$")
# The longest header value the standard library's header parser is given. It keeps a copy of the
# rest of the header with every encoded word it reads, so its memory grows with the square of a
# header's length: a subject of a megabyte of encoded words takes gigabytes. No real header
# comes near this. A longer one is cut at its last whitespace before the limit, unless it is one
# of READ_WHOLE, which is then read as empty.
MAX_HEADER_CHARS = 16 * 1024
# The headers the sender check, the dedupe and the forward unwrapping read as addresses, stamps,
# dates or ids, and the stamps and signatures beside them (and every ARC- header): cut, one could
# read as something else (an address list cut after its first address is that address alone),
# so one over the limit says nothing: no sender, no stamp, no Message-ID.
READ_WHOLE = frozenset(
    {
        "from",
        "sender",
        "reply-to",
        "to",
        "delivered-to",
        "return-path",
        "x-forwarded-for",
        "authentication-results",
        "received-spf",
        "dkim-signature",
        "message-id",
        "date",
    }
)


class _BoundedPolicy(email.policy.EmailPolicy):
    """`email.policy.default`, every header value read from a message no longer than
    MAX_HEADER_CHARS: one of READ_WHOLE over it is empty, any other is cut."""

    def header_source_parse(self, sourcelines):
        name, value = super().header_source_parse(sourcelines)
        if len(value) > MAX_HEADER_CHARS:
            key = name.strip().lower()
            if key in READ_WHOLE or key.startswith("arc-"):
                return name, ""
            value = value[:MAX_HEADER_CHARS]
            space = max(value.rfind(" "), value.rfind("\t"), value.rfind("\n"))
            value = (value[:space] if space > 0 else value).rstrip("\r\n")
        return name, value


POLICY = _BoundedPolicy()
# The most characters of an email's subject, and of its sender's address (the longest an address
# can be), anything is given: they are cut here, where they are read, so the model, the ledger,
# the bookings, the logs and the notices all hold the cut text.
SUBJECT_MAX, SENDER_MAX = 500, 320
# How many emails attached to a message are handed back to be read as messages of their own
# (`extra_rfc822`); together they hold at most the message's own size. An email nested in one
# that is attached is attached too, so a chain of them would hand back each one again inside
# the one before it.
MAX_ATTACHED = 20


@dataclass(frozen=True)
class Attachment:
    filename: str | None
    content_type: str
    data: bytes


@dataclass
class NormalizedMessage:
    message_id: str | None
    subject: str
    from_addr: str
    date: dt.datetime | None
    outer_date: dt.datetime | None
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    origin_ok: bool = False
    origin_reason: str = ""
    forwarded: Literal["none", "inline", "attachment"] = "none"
    raw_for_kitinerary: bytes = b""
    # The owner sent or forwarded it, or a filter forwarded it from a known travel sender whose
    # From domain Gmail's DMARC check vouched for. A filter forward from anyone else passes the
    # gate but must not write to AirTrail unseen; with no authentication check, nothing does.
    sender_trusted: bool = True
    # The outer From is an allowed sender (authenticated, with the check): its inline forward is
    # unwrapped and the emails it carries are handed back.
    forwarded_by_owner: bool = False
    # Further emails attached to this one ("Forward as attachment" of several at once, or nested
    # in the one read), at most MAX_ATTACHED holding at most its size; each is read as its own.
    extra_rfc822: list[bytes] = field(default_factory=list)
    # The bytes this message was read from (the whole email as it arrived, or a paste's text):
    # recorded model answers are filed under their SHA-256.
    raw: bytes = b""

    @property
    def email_date(self) -> dt.datetime | None:
        """Date for plausibility checks: the original message's, else the forwarding envelope's."""
        return self.date or self.outer_date


class _HTMLText(HTMLParser):
    BLOCK = {
        "p",
        "br",
        "div",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "section",
        "ul",
        "ol",
    }
    # not "head": a sloppy template never closes it, and skipping to </head> would drop the body
    SKIP = {"script", "style", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.splitlines()]
        out: list[str] = []
        for ln in lines:
            if ln or (out and out[-1]):
                out.append(ln)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    p.close()
    return p.text()


def _addr(value: str | None) -> str:
    return email.utils.parseaddr(value or "")[1].lower()


def _sender(value: str | None) -> str:
    """The address a From header or a forwarded block's From line names, cut to SENDER_MAX."""
    return _addr(value)[:SENDER_MAX]


def _subject(value: str | None) -> str:
    return (value or "").strip()[:SUBJECT_MAX]


def _date(value: str | None) -> dt.datetime | None:
    """RFC 2822 header date -> aware datetime (naive values are taken as UTC)."""
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.UTC)


def _date_loose(value: str | None, local_tz: str) -> dt.datetime | None:
    """Dates copied from a forwarded header block: RFC 2822 first, then mail-client display formats.

    Display formats carry no zone (or only an abbreviation), so they are read in `local_tz`, the
    zone of the person who forwarded the mail.
    """
    if not value:
        return None
    # RFC 2822 dates carry no AM/PM; a display date that does would be read by the strict parser
    # with the meridian dropped ("4:30 PM" -> 04:30) and stamped UTC, so it goes straight to the
    # display formats
    if not re.search(r"\b[AP]M\b", value, re.I):
        strict = _date(value)
        if strict is not None:
            return strict
    cleaned = re.sub(r"\s+", " ", _TZ_ABBR.sub("", value.strip()))
    for fmt in _LOOSE_FORMATS:
        try:
            naive = dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        try:
            return naive.replace(tzinfo=ZoneInfo(local_tz))
        except ZoneInfoNotFoundError:
            return naive.replace(tzinfo=dt.UTC)
    return None


TRUSTED_AUTHSERV = "mx.google.com"


def _ar_chunks(value: str) -> list[str]:
    """Split an Authentication-Results value on ';' the way the header grammar reads it: never
    inside a quoted string or a (comment), comments dropped, quotes unwrapped. A sender can get
    text echoed inside Gmail's own header (the envelope local part, for one), so a naive split
    would let `"a;dkim=pass ..."` conjure up methods Gmail never evaluated."""
    out: list[str] = []
    cur: list[str] = []
    depth, quoted, i = 0, False, 0
    while i < len(value):
        ch = value[i]
        if quoted:
            if ch == "\\" and i + 1 < len(value):
                cur.append(value[i + 1])
                i += 2
                continue
            if ch == '"':
                quoted = False
            else:
                cur.append(ch)
        elif depth:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
            cur.append(" ")
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [c.strip() for c in out if c.strip()]


# The methods an Authentication-Results header states once per signature: a message signed twice
# (by its own domain and by the provider that sends it) carries a result for each.
PER_SIGNATURE = ("dkim",)
AuthResult = tuple[str, dict[str, str]]


def _parse_auth_results(
    value: str,
) -> tuple[str, dict[str, AuthResult], dict[str, list[AuthResult]]]:
    """Parse one Authentication-Results header -> (authserv-id, {method: (result, props)},
    {method: [(result, props), …]}). A method of PER_SIGNATURE goes in the last, every result of
    it in order; any other that appears twice fails closed ('conflict'): a real Gmail header
    states each once."""
    chunks = _ar_chunks(" ".join(str(value).split()))
    if not chunks or not chunks[0].split():
        return "", {}, {}
    authserv = chunks[0].split()[0].lower()
    methods: dict[str, AuthResult] = {}
    signed: dict[str, list[AuthResult]] = {}
    twice: set[str] = set()
    for chunk in chunks[1:]:
        parts = chunk.split()
        if not parts or "=" not in parts[0]:
            continue
        method, result = parts[0].split("=", 1)
        method = method.lower()
        props: dict[str, str] = {}
        for kv in parts[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                props[k.lower()] = v.strip().strip("<>").lower()
        if method in PER_SIGNATURE:
            signed.setdefault(method, []).append((result.lower(), props))
        elif method in methods:
            twice.add(method)
        else:
            methods[method] = (result.lower(), props)
    for method in twice:
        methods[method] = ("conflict", {})
    return authserv, methods, signed


def _google_auth_ok(
    msg: EmailMessage, path: str, sender: str, envelope: str | None
) -> tuple[bool, str]:
    """Bind the claimed origin to what the receiving Gmail server (mx.google.com) verified.

    Only the topmost Authentication-Results header counts: the receiving MTA prepends its own, so
    anything a sender typed sits below it. `path` is "from" (the sender mailed it: needs dkim=pass
    signed by the sender's own domain and dmarc=pass for that domain, whichever provider hosts it)
    or "forwarded" (a Gmail filter forward: needs spf=pass whose smtp.mailfrom is exactly the
    +caf_ Return-Path envelope).
    """
    topmost = msg.get("Authentication-Results")  # the first one, alone parsed
    if topmost is None:
        return False, "no Authentication-Results header"
    authserv, methods, signed = _parse_auth_results(str(topmost))
    if authserv != TRUSTED_AUTHSERV:
        return (
            False,
            f"topmost Authentication-Results is from {authserv or '?'}, not {TRUSTED_AUTHSERV}",
        )
    domain = sender.rsplit("@", 1)[-1]
    if path == "from":
        # one signature of the sender's own domain that passed is enough, beside any other
        dkim = any(
            result == "pass" and props.get("header.i") == f"@{domain}"
            for result, props in signed.get("dkim", [])
        )
        dmarc = methods.get("dmarc", ("", {}))
        if (
            dkim
            and dmarc[0] == "pass"
            and dmarc[1].get("header.from") == domain
        ):
            return True, f"dkim+dmarc pass for {domain}"
        return False, f"dkim/dmarc did not bind to {domain}"
    spf = methods.get("spf", ("", {}))
    if envelope and spf[0] == "pass" and spf[1].get("smtp.mailfrom") == envelope:
        return True, "spf pass for the forwarding envelope"
    return False, "spf did not bind to the +caf_ forwarding envelope"


def _caf_envelope(msg: EmailMessage, allowed: set[str]) -> tuple[str, str] | None:
    """Return (allowed sender, envelope address) when Return-Path is Gmail's +caf_ forward of it."""
    rp = _addr(msg.get("Return-Path"))
    if "@" not in rp:
        return None
    local, domain = rp.rsplit("@", 1)
    for a in allowed:
        a_local, a_domain = a.rsplit("@", 1)
        if domain == a_domain and local.startswith(f"{a_local}+caf_="):
            return a, rp
    return None


def check_origin(
    msg: EmailMessage, allowed_senders: tuple[str, ...], require_auth: bool = True
) -> tuple[bool, str]:
    """Only mail from, or forwarded by, an allowed sender is parsed. Exact address matches only."""
    allowed = {a.lower() for a in allowed_senders}
    frm = _addr(msg.get("From"))
    path: str | None = None
    sender = ""
    envelope: str | None = None
    reason = ""

    if frm in allowed:
        path, sender, reason = "from", frm, f"from {frm}"
    else:
        caf = _caf_envelope(msg, allowed)
        if caf is not None:
            sender, envelope = caf
            path, reason = "forwarded", f"gmail forward envelope of {sender}"
        else:
            # Gmail writes "X-Forwarded-For: <owner address> <mailbox address>" (space separated).
            # On its own this header proves nothing; it only names the path, auth below decides.
            # Read as it stands, never parsed: a sender may add any number of them, and Gmail's
            # own carries no encoded word. Only a token holding an @ can name an address
            tokens = [
                t
                for name, v in msg.raw_items()
                if name.lower() == "x-forwarded-for"
                for t in re.split(r"[\s,;]+", str(v))
                if "@" in t
            ]
            xff = [(email.utils.parseaddr(t)[1] or t).strip("<>").lower() for t in tokens]
            hit = next((a for a in xff if a in allowed), None)
            if hit:
                path, sender, reason = "forwarded", hit, f"auto-forwarded via {hit}"
    if path is None:
        return False, f"from {frm or '?'} (not an allowed sender, no forwarding marker)"
    if require_auth:
        ok, why = _google_auth_ok(msg, path, sender, envelope)
        if not ok:
            return False, f"{reason} but unauthenticated: {why}"
        reason = f"{reason} ({why})"
    return True, reason


def _rfc822_parts(msg: EmailMessage) -> list[EmailMessage]:
    """Every email attached to `msg`, in order. A base64-encoded message/rfc822 part comes out of
    the parser as a headerless message whose body is the base64 text; decode that into the real
    message."""
    out: list[EmailMessage] = []
    for part in msg.walk():
        if part.get_content_type() != "message/rfc822":
            continue
        payload = part.get_payload()
        if not (isinstance(payload, list) and payload and isinstance(payload[0], EmailMessage)):
            continue
        inner = payload[0]
        cte = str(part.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte == "base64" and not (inner.get("From") or inner.get("Subject")):
            try:
                decoded = email.message_from_bytes(
                    base64.b64decode(str(inner.get_payload()), validate=False), policy=POLICY
                )
                if isinstance(decoded, EmailMessage):
                    inner = decoded
            except Exception:  # noqa: BLE001 - keep whatever the parser gave us
                log.warning("could not decode a base64 message/rfc822 part")
        out.append(inner)
    return out


def _handed_back(inners: list[EmailMessage], size: int) -> list[bytes]:
    """The bytes of the first of `inners`, in order, up to MAX_ATTACHED of them and up to `size`
    bytes in all; the count of those left out is logged, nothing of them."""
    out: list[bytes] = []
    total = 0
    for inner in inners[:MAX_ATTACHED]:
        data = inner.as_bytes()
        if total + len(data) > size:
            break
        out.append(data)
        total += len(data)
    if len(out) < len(inners):
        log.warning(
            "%d attached email(s) left unread: past %d emails or the message's own size",
            len(inners) - len(out),
            MAX_ATTACHED,
        )
    return out


def _body_text(msg: EmailMessage) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    if body.get_content_maintype() != "text":
        return ""
    try:
        content = body.get_content()
    except LookupError:  # charset=x-unknown and friends: read it as UTF-8, best effort
        raw = body.get_payload(decode=True)
        content = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
    if body.get_content_subtype() == "html":
        return html_to_text(content)
    return content.replace("\r\n", "\n").strip()


def _domain_trusted(addr: str, trusted_domains: tuple[str, ...]) -> bool:
    domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
    return bool(domain) and any(
        domain == d or domain.endswith("." + d) for d in trusted_domains if d
    )


def _dmarc_vouches_for(msg: EmailMessage, addr: str) -> bool:
    """Gmail's topmost Authentication-Results says dmarc=pass for the From domain (or an aligned
    parent/child of it): the sender really is that domain, not a spoofed From header."""
    topmost = msg.get("Authentication-Results")  # the first one, alone parsed
    if topmost is None:
        return False
    authserv, methods, _ = _parse_auth_results(str(topmost))
    if authserv != TRUSTED_AUTHSERV:
        return False
    result, props = methods.get("dmarc", ("", {}))
    hf = props.get("header.from", "")
    domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
    return (
        result == "pass"
        and bool(hf)
        and bool(domain)
        and (hf == domain or domain.endswith("." + hf) or hf.endswith("." + domain))
    )


def _attachments(msg: EmailMessage) -> list[Attachment]:
    out: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_type() == "message/rfc822":
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        if disp == "attachment" or ctype in ATTACHMENT_TYPES:
            data = part.get_payload(decode=True)
            if isinstance(data, bytes) and data:
                out.append(Attachment(filename=part.get_filename(), content_type=ctype, data=data))
    return out


def strip_inline_forward(text: str) -> tuple[str, dict[str, str], bool]:
    """If the body is an inline forward, return (original body, header fields, True).

    Without a recognisable header block after the marker the text is returned untouched.
    """
    for marker in FORWARD_MARKERS:
        m = marker.search(text)
        if not m:
            continue
        lines = text[m.end() :].lstrip("\n").split("\n")
        headers: dict[str, str] = {}
        i = 0
        while i < len(lines) and i < 6 and not HEADER_LINE.match(lines[i].strip()):
            i += 1
        while i < len(lines):
            hm = HEADER_LINE.match(lines[i].strip())
            if not hm:
                break
            headers[hm.group(1).lower()] = hm.group(2).strip()
            i += 1
        if not headers:
            return text, {}, True
        while i < len(lines) and not lines[i].strip():
            i += 1
        return "\n".join(lines[i:]).strip(), headers, True
    return text, {}, False


def normalize(
    raw: bytes,
    allowed_senders: tuple[str, ...],
    local_tz: str = "UTC",
    require_auth: bool = True,
    trusted_domains: tuple[str, ...] = (),
    *,
    gate: bool = True,
) -> NormalizedMessage:
    """`gate` False: the sender check is not asked and the message is read whoever sent it —
    the owner handed these bytes in or vouches for them (an upload, an ingest, an email attached
    to the owner's own), or the check accepted these same bytes before. Whether the sender is
    vouched for (`sender_trusted`) is still worked out from the headers. `require_auth` False
    (`mailbox` mode): the sender check asks no authentication results, and no sender is vouched
    for."""
    outer = email.message_from_bytes(raw, policy=POLICY)
    assert isinstance(outer, EmailMessage)
    if gate:
        origin_ok, origin_reason = check_origin(outer, allowed_senders, require_auth)
    else:
        origin_ok, origin_reason = True, "not asked: the owner's, or accepted before"
    outer_date = _date(outer.get("Date"))
    by_owner = _addr(outer.get("From")) in {a.lower() for a in allowed_senders}
    if not origin_ok:
        # rejected mail is never read past its headers: no body decoding (a bogus charset would
        # turn spam into a 'failed' message and an alert), no attachments, no parsing
        return NormalizedMessage(
            message_id=(outer.get("Message-ID") or "").strip() or None,
            subject=_subject(outer.get("Subject")),
            from_addr=_sender(outer.get("From")),
            date=_date(outer.get("Date")),
            outer_date=outer_date,
            text="",
            origin_ok=False,
            origin_reason=origin_reason,
            sender_trusted=False,
            raw=raw,
        )

    inners = _rfc822_parts(outer)
    if inners:
        primary, forwarded, raw_k = inners[0], "attachment", inners[0].as_bytes()
    else:
        primary, forwarded, raw_k = outer, "none", raw

    text = _body_text(primary)
    subject = _subject(primary.get("Subject"))
    from_addr = _sender(primary.get("From"))
    date = _date(primary.get("Date"))

    if forwarded == "none" and by_owner:
        # only the owner's own forwards carry a header block worth unwrapping; a vendor's reply
        # that quotes the owner's request below "-----Original Message-----" IS the message
        stripped, hdrs, is_fwd = strip_inline_forward(text)
        if is_fwd:
            forwarded = "inline"
            text = stripped
            when = hdrs.get("date") or hdrs.get("sent")  # Outlook prints "Sent:"
            if when:
                date = _date_loose(when, local_tz) or date
            if hdrs.get("subject"):
                subject = hdrs["subject"]
            if hdrs.get("from"):
                from_addr = _sender(hdrs["from"]) or from_addr
            subject = _subject(FWD_PREFIX.sub("", subject))

    return NormalizedMessage(
        message_id=(primary.get("Message-ID") or outer.get("Message-ID") or "").strip() or None,
        subject=subject,
        from_addr=from_addr,
        date=date,
        outer_date=outer_date,
        text=text,
        attachments=_attachments(primary),
        origin_ok=origin_ok,
        origin_reason=origin_reason,
        forwarded=forwarded,
        raw_for_kitinerary=raw_k,
        # trust is judged on the OUTER message, the one Gmail authenticated and forwarded: an
        # attached email's From header is whatever the attachment says it is. With no
        # authentication check (`mailbox` mode) nothing vouches for any sender, the owner's own
        # address included
        sender_trusted=require_auth
        and (
            by_owner
            or (
                _domain_trusted(_addr(outer.get("From")), trusted_domains)
                and _dmarc_vouches_for(outer, _addr(outer.get("From")))
            )
        ),
        forwarded_by_owner=by_owner,
        extra_rfc822=_handed_back(inners[1:], len(raw)) if by_owner else [],
        raw=raw,
    )
