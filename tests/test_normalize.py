# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
import email
import email.policy
import email.utils
import inspect
import sys
from zoneinfo import ZoneInfo

from helpers.eml import make_eml

from tripbox.mail import normalize as normalize_module
from tripbox.mail.normalize import _date_loose, html_to_text, normalize, strip_inline_forward

ALLOWED = ("alex@example.com",)
# What Gmail (mx.google.com) stamps on mail Alex sends from their own account ...
AR_DKIM = {
    "Authentication-Results": "mx.google.com; dkim=pass header.i=@example.com header.s=2023; "
    "spf=pass (google.com: domain of alex@example.com designates 209.85.1.1 as permitted sender) "
    "smtp.mailfrom=alex@example.com; dmarc=pass (p=NONE) header.from=example.com"
}
# ... and on a copy their filter auto-forwards (original DKIM, ARC seal, +caf_ envelope).
AR_FWD = {
    "Authentication-Results": "mx.google.com; dkim=pass header.i=@t.delta.com header.s=x; "
    "arc=pass (i=1 spf=pass spfdomain=t.delta.com dkim=pass dkdomain=t.delta.com dmarc=pass "
    "fromdomain=t.delta.com); spf=pass (google.com: domain of "
    "alex+caf_=trips=example.com@example.com designates 209.85.1.2 as permitted) "
    'smtp.mailfrom="alex+caf_=trips=example.com@example.com"; '
    "dmarc=pass header.from=t.delta.com"
}
D1 = dt.datetime(2026, 10, 19, 14, 30, tzinfo=dt.UTC)
D2 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
PDF = b"%PDF-1.4 fake"
CHI = ZoneInfo("America/Chicago")


def test_auto_forward_keeps_original_headers_and_passes_origin():
    m = make_eml(
        from_="Delta Air Lines <DeltaAirLines@t.delta.com>",
        subject="Your Flight Receipt",
        date=D1,
        text="Confirmation # ABC123",
        message_id="<r1@delta>",
        headers={
            "X-Forwarded-For": "alex@example.com trips@example.com",
            "Return-Path": "<alex+caf_=trips=example.com@example.com>",
            **AR_FWD,
        },
    )
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok and "forward envelope" in n.origin_reason and "spf pass" in n.origin_reason
    assert n.forwarded == "none"
    assert n.from_addr == "deltaairlines@t.delta.com"
    assert n.date == D1 and n.email_date == D1
    assert n.message_id == "<r1@delta>"
    assert "ABC123" in n.text


def test_return_path_caf_alone_passes_origin():
    m = make_eml(
        from_="info@email.aa.com",
        subject="x",
        date=D1,
        text="x",
        headers={"Return-Path": "<alex+caf_=trips=example.com@example.com>", **AR_FWD},
    )
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok and "forward envelope" in n.origin_reason


def test_direct_mail_from_stranger_is_rejected_unparsed():
    m = make_eml(from_="spam@example.com", subject="Your booking", date=D1, text="click here")
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "not an allowed sender" in n.origin_reason


def test_inline_forward_recovers_original_date_subject_sender():
    body = (
        "\n\n---------- Forwarded message ---------\nFrom: United Airlines <Receipts@united.com>\n"
        "Date: Mon, Oct 19, 2026 at 9:30 AM\n"
        "Subject: eTicket Itinerary and Receipt for Confirmation ABC123\n"
        "To: <alex@example.com>\n\n\nConfirmation Number: ABC123\nFlight UA 1234"
    )
    m = make_eml(
        from_="Alex Sample <alex@example.com>",
        subject="Fwd: eTicket Itinerary and Receipt",
        date=D2,
        text=body,
        headers=AR_DKIM,
    )
    n = normalize(m.as_bytes(), ALLOWED, local_tz="America/Chicago")
    assert n.origin_ok and n.forwarded == "inline"
    assert n.from_addr == "receipts@united.com"
    assert n.subject == "eTicket Itinerary and Receipt for Confirmation ABC123"
    assert n.date == dt.datetime(2026, 10, 19, 9, 30, tzinfo=CHI)  # from the header block
    assert n.text.startswith("Confirmation Number: ABC123")
    assert "Forwarded message" not in n.text


def test_a_zone_less_forward_date_reads_in_the_given_zone():
    body = (
        "---------- Forwarded message ---------\nFrom: Example Air <trips@air.example>\n"
        "Date: Mon, Oct 19, 2026 at 9:30 AM\nSubject: Your itinerary\n\nConfirmation ZZTEST"
    )
    m = make_eml(
        from_="Alex Sample <alex@example.com>",
        to="trips@example.com",
        subject="Fwd: Your itinerary",
        date=D2,
        text=body,
    )
    owner = ("alex@example.com",)
    n = normalize(m.as_bytes(), owner, local_tz="America/Denver", require_auth=False)
    assert n.forwarded == "inline" and n.date == dt.datetime(2026, 10, 19, 15, 30, tzinfo=dt.UTC)
    n = normalize(m.as_bytes(), owner, require_auth=False)
    assert n.forwarded == "inline" and n.date == dt.datetime(2026, 10, 19, 9, 30, tzinfo=dt.UTC)


def test_forward_as_attachment_uses_inner_message_and_its_attachments():
    inner = make_eml(
        from_="Example Hotels <reservations@examplehotels.example>",
        subject="Example Hotels Confirmation 1234567890",
        date=D1,
        text="Check-in Nov 9",
        message_id="<h1@examplehotels.example>",
        attachments=[("confirmation.pdf", "application/pdf", PDF)],
    )
    outer = make_eml(
        from_="alex@example.com",
        subject="Fwd: Example Hotels",
        date=D2,
        text="see attached",
        rfc822=inner,
        headers=AR_DKIM,
    )
    n = normalize(outer.as_bytes(), ALLOWED)
    assert n.origin_ok and n.forwarded == "attachment"
    assert n.from_addr == "reservations@examplehotels.example"
    assert n.subject == "Example Hotels Confirmation 1234567890"
    assert n.date == D1 and n.outer_date == D2 and n.email_date == D1
    assert n.message_id == "<h1@examplehotels.example>"
    assert [a.content_type for a in n.attachments] == ["application/pdf"]
    assert n.attachments[0].data == PDF and n.attachments[0].filename == "confirmation.pdf"
    assert b"Check-in Nov 9" in n.raw_for_kitinerary and b"see attached" not in n.raw_for_kitinerary


def test_forward_as_attachment_without_inner_date_falls_back_to_outer_date():
    inner = make_eml(from_="t.delta.com <noreply@t.delta.com>", subject="Receipt", text="Conf ABC")
    outer = make_eml(
        from_="alex@example.com", subject="Fwd", date=D2, text="", rfc822=inner, headers=AR_DKIM
    )
    n = normalize(outer.as_bytes(), ALLOWED)
    assert n.date is None and n.email_date == D2


def test_html_only_body_becomes_readable_text():
    html = (
        "<html><head><style>.x{color:red}</style><title>T</title></head><body>"
        "<p>Flight <b>DL 1234</b></p><table><tr><td>MSP</td><td>PHL</td></tr></table>"
        "<script>alert(1)</script></body></html>"
    )
    m = make_eml(from_="alex@example.com", subject="x", date=D1, html=html, headers=AR_DKIM)
    n = normalize(m.as_bytes(), ALLOWED)
    assert "Flight DL 1234" in n.text and "MSP PHL" in n.text
    assert "color" not in n.text and "alert" not in n.text


def test_strip_inline_forward_without_header_block_keeps_text():
    text, hdrs, is_fwd = strip_inline_forward(
        "---------- Forwarded message ---------\nsome text only"
    )
    assert is_fwd and hdrs == {} and "some text only" in text


def test_html_to_text_collapses_whitespace():
    assert (
        html_to_text("<div>a</div>\n\n\n<div>   b   c </div>") == "a\n\nb c"
    )  # one blank line max


def test_date_loose_formats():
    assert _date_loose("Mon, Oct 19, 2026 at 9:30 AM", "America/Chicago") == dt.datetime(
        2026, 10, 19, 9, 30, tzinfo=CHI
    )
    assert _date_loose("Mon, Oct 19, 2026 at 2:30 PM", "America/Chicago") == dt.datetime(
        2026, 10, 19, 14, 30, tzinfo=CHI
    )
    assert _date_loose("October 19, 2026 at 9:30:15 AM CDT", "America/Chicago") == dt.datetime(
        2026, 10, 19, 9, 30, 15, tzinfo=CHI
    )
    assert _date_loose("Mon, 19 Oct 2026 14:30:00 +0000", "America/Chicago") == dt.datetime(
        2026, 10, 19, 14, 30, tzinfo=dt.UTC
    )
    assert _date_loose("yesterday-ish", "America/Chicago") is None


def test_spoofed_forwarding_headers_without_google_auth_are_rejected():
    m = make_eml(
        from_="attacker@example.com",
        subject="Your booking",
        date=D1,
        text="fake",
        headers={
            "X-Forwarded-For": "alex@example.com trips@example.com",
            "Return-Path": "<alex+caf_=trips=example.com@example.com>",
        },
    )
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "unauthenticated" in n.origin_reason
    assert "no Authentication-Results header" in n.origin_reason
    assert normalize(m.as_bytes(), ALLOWED, require_auth=False).origin_ok is True  # the opt-out


def test_substring_lookalikes_do_not_match():
    for hdrs in (
        {"X-Forwarded-For": "alex@example.com.evil.test trips@example.com", **AR_FWD},
        {"X-Forwarded-For": "notalex@example.com", **AR_FWD},
        {"Return-Path": "<alex+caf_=x@example.com.evil.test>", **AR_FWD},
        {"Return-Path": "<xalex+caf_=trips=example.com@example.com>", **AR_FWD},
    ):
        m = make_eml(from_="attacker@example.com", subject="x", date=D1, text="x", headers=hdrs)
        n = normalize(m.as_bytes(), ALLOWED)
        assert n.origin_ok is False, hdrs
        assert "not an allowed sender, no forwarding marker" in n.origin_reason, hdrs


def test_xff_without_return_path_envelope_is_not_enough():
    m = make_eml(
        from_="info@email.aa.com",
        subject="x",
        date=D1,
        text="x",
        headers={"X-Forwarded-For": "alex@example.com trips@example.com", **AR_FWD},
    )
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "spf did not bind" in n.origin_reason


def test_only_topmost_authentication_results_counts():
    from email.message import EmailMessage

    m = make_eml(
        from_="alex@example.com",
        subject="x",
        date=D1,
        text="x",
        headers={"Authentication-Results": "mx.google.com; dkim=fail header.i=@example.com"},
    )
    m2 = EmailMessage()
    m2["Authentication-Results"] = (
        "mx.google.com; dkim=fail header.i=@example.com; dmarc=fail header.from=example.com"
    )
    for k, v in m.items():
        m2[k] = v
    m2["Authentication-Results"] = AR_DKIM[
        "Authentication-Results"
    ]  # attacker-supplied, sits below
    m2.set_content("x")
    n = normalize(m2.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "dkim/dmarc did not bind to example.com" in n.origin_reason


def test_owner_mail_requires_dkim_and_dmarc_for_its_own_domain():
    m = make_eml(
        from_="alex@example.com",
        subject="x",
        date=D1,
        text="x",
        headers={
            "Authentication-Results": "mx.google.com; dkim=fail header.i=@example.com; spf=softfail"
        },
    )
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "dkim/dmarc did not bind to example.com" in n.origin_reason
    m2 = make_eml(
        from_="alex@example.com",
        subject="x",
        date=D1,
        text="x",
        headers={"Authentication-Results": "mx.evil.example; dkim=pass header.i=@example.com"},
    )
    n = normalize(m2.as_bytes(), ALLOWED)  # untrusted authserv-id
    assert n.origin_ok is False and "topmost Authentication-Results is from" in n.origin_reason


CAF = {"Return-Path": "<alex+caf_=trips=example.com@example.com>", **AR_FWD}


def test_auto_forwarded_mail_is_trusted_only_from_known_travel_senders():
    """A Gmail filter forwards whatever matches it, display name included: a stranger's mail
    passes the origin gate (Gmail really did forward it) but is not a trusted sender."""
    stranger = make_eml(
        from_='"delta.com" <x@evil.example>', subject="x", date=D1, text="x", headers=CAF
    )
    n = normalize(stranger.as_bytes(), ALLOWED, trusted_domains=("delta.com",))
    assert n.origin_ok and n.sender_trusted is False
    delta = make_eml(
        from_="Delta <DeltaAirLines@t.delta.com>", subject="x", date=D1, text="x", headers=CAF
    )
    assert (
        normalize(delta.as_bytes(), ALLOWED, trusted_domains=("delta.com",)).sender_trusted is True
    )
    # Alex's own mail and their manual forwards are vouched for by them
    own = make_eml(from_="alex@example.com", subject="Fwd: x", date=D1, text="x", headers=AR_DKIM)
    assert normalize(own.as_bytes(), ALLOWED, trusted_domains=()).sender_trusted is True


def test_authentication_results_injected_through_a_quoted_envelope_is_rejected():
    """Values an attacker controls can be echoed inside Gmail's own header (the envelope sender):
    quoted text must not spawn fake dkim=/dmarc= entries, and a repeated dmarc or spf fails
    closed."""
    injected = {
        "Authentication-Results": "mx.google.com; spf=pass smtp.mailfrom="
        '"a;dkim=pass header.i=@example.com;dmarc=pass header.from=example.com;x"@evil.example; '
        "dkim=fail header.i=@evil.example; dmarc=fail header.from=example.com"
    }
    m = make_eml(from_="alex@example.com", subject="x", date=D1, text="x", headers=injected)
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "dkim/dmarc did not bind to example.com" in n.origin_reason
    repeated = {
        "Authentication-Results": "mx.google.com; dkim=pass header.i=@example.com; "
        "dmarc=pass header.from=example.com; dmarc=fail header.from=example.com"
    }
    m = make_eml(from_="alex@example.com", subject="x", date=D1, text="x", headers=repeated)
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "dkim/dmarc did not bind to example.com" in n.origin_reason
    caf_twice = {
        "Return-Path": "<alex+caf_=trips=example.com@example.com>",
        "Authentication-Results": "mx.google.com; spf=pass smtp.mailfrom="
        "alex+caf_=trips=example.com@example.com; spf=fail smtp.mailfrom=x@evil.example",
    }
    m = make_eml(from_="x@evil.example", subject="x", date=D1, text="x", headers=caf_twice)
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False and "spf did not bind" in n.origin_reason
    comment = {
        "Authentication-Results": 'mx.google.com; dkim=pass (a comment with "quotes;" and a )'
        " paren) header.i=@example.com; dmarc=pass header.from=example.com"
    }
    m = make_eml(from_="alex@example.com", subject="x", date=D1, text="x", headers=comment)
    assert normalize(m.as_bytes(), ALLOWED).origin_ok is True


def test_mail_signed_twice_passes_on_its_own_domain_s_signature():
    """A domain whose mail its provider signs too carries one dkim result per signature: the
    sender's own passing signature binds it, whatever the other says; none of its own does not."""

    def origin(ar: str):
        m = make_eml(
            from_="alex@example.com",
            subject="x",
            date=D1,
            text="x",
            headers={"Authentication-Results": ar},
        )
        return normalize(m.as_bytes(), ALLOWED)

    dmarc = "dmarc=pass header.from=example.com"
    for ar in (
        f"mx.google.com; dkim=pass header.i=@example.com; "
        f"dkim=pass header.i=@example-com.gappssmtp.example; {dmarc}",
        f"mx.google.com; dkim=pass header.i=@provider.example; "
        f"dkim=pass header.i=@example.com; {dmarc}",
        f"mx.google.com; dkim=fail header.i=@example.com; "
        f"dkim=pass header.i=@example.com; {dmarc}",
    ):
        n = origin(ar)
        assert n.origin_ok is True, ar
    for ar in (
        f"mx.google.com; dkim=pass header.i=@provider.example; "
        f"dkim=fail header.i=@example.com; {dmarc}",
        f"mx.google.com; dkim=pass header.i=@provider.example; "
        f"dkim=pass header.i=@other.example; {dmarc}",
    ):
        n = origin(ar)
        assert n.origin_ok is False and "did not bind to example.com" in n.origin_reason, ar


def test_every_attached_email_is_handed_back():
    inner1 = make_eml(
        from_="a@t.delta.com", subject="Receipt 1", date=D1, text="one", message_id="<i1>"
    )
    inner2 = make_eml(
        from_="a@t.delta.com", subject="Receipt 2", date=D1, text="two", message_id="<i2>"
    )
    outer = make_eml(
        from_="alex@example.com",
        subject="Fwd",
        date=D2,
        text="see attached",
        headers=AR_DKIM,
        rfc822=inner1,
    )
    outer.add_attachment(inner2)
    n = normalize(outer.as_bytes(), ALLOWED)
    assert n.forwarded == "attachment" and n.subject == "Receipt 1" and n.message_id == "<i1>"
    assert len(n.extra_rfc822) == 1 and b"Receipt 2" in n.extra_rfc822[0]


def test_base64_encoded_attached_email_is_decoded():
    import base64

    inner = make_eml(
        from_="a@t.delta.com", subject="Receipt B64", date=D1, text="hello", message_id="<b64>"
    )
    raw = (
        "From: alex@example.com\r\nTo: trips@example.com\r\nSubject: Fwd\r\n"
        f"Authentication-Results: {AR_DKIM['Authentication-Results']}\r\n"
        'Content-Type: multipart/mixed; boundary="bb"\r\n\r\n'
        "--bb\r\nContent-Type: text/plain\r\n\r\n"
        "see attached\r\n--bb\r\nContent-Type: message/rfc822\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(inner.as_bytes()).decode()
        + "\r\n--bb--\r\n"
    ).encode()
    n = normalize(raw, ALLOWED)
    assert n.forwarded == "attachment" and n.subject == "Receipt B64" and n.date == D1
    assert "hello" in n.text and n.message_id == "<b64>"


def test_unknown_charset_never_crashes_and_spam_is_rejected_before_its_body_is_read():
    raw = (
        b"From: spam@example.com\r\nTo: trips@example.com\r\nSubject: hi\r\n"
        b"Content-Type: text/plain; charset=x-unknown\r\n\r\nclick here\r\n"
    )
    n = normalize(raw, ALLOWED)
    assert n.origin_ok is False and "not an allowed sender" in n.origin_reason
    raw = (
        "From: alex@example.com\r\nTo: trips@example.com\r\nSubject: Fwd: receipt\r\n"
        f"Authentication-Results: {AR_DKIM['Authentication-Results']}\r\n"
        "Content-Type: text/plain; charset=unknown-8bit\r\n\r\nConf ABC123 caf\xc3\xa9\r\n"
    ).encode("latin-1")
    n = normalize(raw, ALLOWED)
    assert n.origin_ok and "ABC123" in n.text


def test_vendor_replies_keep_their_text_when_auto_forwarded():
    """A hotel answers above Alex's quoted request: the answer is the confirmation. Only a
    manual forward by Alex has a header block worth stripping."""
    body = (
        "Your reservation is confirmed: check-in 8 June, check-out 10 June, conf 90001\n\n"
        "-----Original Message-----\nFrom: alex@example.com\nSent: June 1\n"
        "Subject: Booking request\n\n"
        "Hello, could we book a room for 8-10 June 2027?"
    )
    m = make_eml(
        from_="Front Desk <desk@smallhotel.example>",
        subject="RE: Booking request",
        date=D1,
        text=body,
        headers=CAF,
    )
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.forwarded == "none" and "conf 90001" in n.text and n.subject == "RE: Booking request"
    assert n.from_addr == "desk@smallhotel.example"


def test_outlook_forward_sent_line_is_the_original_date():
    body = (
        "-----Original Message-----\nFrom: Delta <d@t.delta.com>\n"
        "Sent: Monday, October 19, 2026 9:30 AM\nTo: me\nSubject: Your Flight Receipt\n\n"
        "Conf ABC123"
    )
    m = make_eml(
        from_="alex@example.com",
        subject="FW: Your Flight Receipt",
        date=D2,
        text=body,
        headers=AR_DKIM,
    )
    n = normalize(m.as_bytes(), ALLOWED, local_tz="America/Chicago")
    assert n.forwarded == "inline" and n.date == dt.datetime(2026, 10, 19, 9, 30, tzinfo=CHI)
    assert n.subject == "Your Flight Receipt"


def test_html_to_text_survives_an_unclosed_head():
    assert "Hello" in html_to_text("<html><head><title>T</title><body><p>Hello</p></body></html>")
    assert "Hello" in html_to_text("<html><head><title>T</title><p>Hello</p>")
    assert "Depart Arrive" in html_to_text("<table><tr><th>Depart</th><th>Arrive</th></tr></table>")


AR_WORK = {
    "Authentication-Results": "mx.google.com; dkim=pass header.i=@example.org "
    "header.s=google; spf=pass (google.com: domain of alex.sample@example.org "
    "designates 209.85.1.3 as permitted sender) "
    "smtp.mailfrom=alex.sample@example.org; "
    "dmarc=pass (p=REJECT) header.from=example.org"
}


def test_a_second_allowed_sender_at_another_domain_is_bound_to_its_own_domain():
    """An allowed sender at a second domain: the gate binds each allowed address to Gmail's
    dkim+dmarc verdict for THAT domain, not to example.com."""
    both = ("alex@example.com", "alex.sample@example.org")
    m = make_eml(
        from_="alex.sample@example.org",
        subject="x",
        date=D1,
        text="x",
        headers=AR_WORK,
    )
    n = normalize(m.as_bytes(), both)
    assert n.origin_ok is True and n.forwarded_by_owner is True and n.sender_trusted is True
    # not listed → rejected unparsed, however well it authenticates
    n = normalize(m.as_bytes(), ALLOWED)
    assert n.origin_ok is False
    assert "not an allowed sender, no forwarding marker" in n.origin_reason
    # listed, but Gmail vouched for example.com rather than the work domain → rejected
    spoof = make_eml(
        from_="alex.sample@example.org",
        subject="x",
        date=D1,
        text="x",
        headers=AR_DKIM,
    )
    n = normalize(spoof.as_bytes(), both)
    assert n.origin_ok is False
    assert "dkim/dmarc did not bind to example.org" in n.origin_reason


# Gmail's stamp on mail the owner sends, one token per condition the owner's own path binds: the
# server that wrote it, dkim's result and signing domain, dmarc's result and From domain.
OWN_STAMP = "{server}; dkim={dkim} header.i={signed}; dmarc={dmarc} header.from={domain}"
OWN_PASSES = {
    "server": "mx.google.com",
    "dkim": "pass",
    "signed": "@example.com",
    "dmarc": "pass",
    "domain": "example.com",
}
# Each condition's token turned, a lookalike of the passing one where it names a host
OWN_FAILS = {
    "server": "mx.google.com.evil.test",
    "dkim": "fail",
    "signed": "@example.com.evil.test",
    "dmarc": "fail",
    "domain": "example.com.evil.test",
}


def own_mail(stamp: str) -> bytes:
    return make_eml(
        from_="alex@example.com",
        subject="x",
        date=D1,
        text="x",
        headers={"Authentication-Results": stamp},
    ).as_bytes()


def test_each_dkim_dmarc_condition_alone_rejects_and_its_flip_accepts():
    """The owner's own mail is believed only on Gmail's stamp binding dkim and dmarc to the
    owner's domain: each of the five tokens alone turned rejects it, and the stamp that differs
    from that one in this token alone is accepted."""
    accepted = normalize(own_mail(OWN_STAMP.format(**OWN_PASSES)), ALLOWED)
    assert accepted.origin_ok is True and accepted.sender_trusted is True
    for condition, token in OWN_FAILS.items():
        stamp = OWN_STAMP.format(**{**OWN_PASSES, condition: token})
        n = normalize(own_mail(stamp), ALLOWED)
        assert n.origin_ok is False and n.sender_trusted is False, condition
        assert "unauthenticated" in n.origin_reason and n.text == "", condition
        flipped = stamp.replace(token, OWN_PASSES[condition], 1)
        assert flipped == OWN_STAMP.format(**OWN_PASSES), condition
        assert normalize(own_mail(flipped), ALLOWED).origin_ok is True, condition


def test_spf_must_bind_on_the_forwarding_path_too():
    """A filter forward is believed on Gmail's spf verdict for its own +caf_ envelope: dkim and
    dmarc passing for the vendor do not stand in for it, an spf pass for any other envelope says
    nothing of this one, and with no +caf_ envelope at all nothing binds. Each failing stamp
    differs from the accepted one in one token."""
    envelope = "alex+caf_=trips=example.com@example.com"
    stamp = (
        "{server}; dkim=pass header.i=@t.delta.com; spf={spf} smtp.mailfrom={mailfrom}; "
        "dmarc=pass header.from=t.delta.com"
    )
    passes = {"server": "mx.google.com", "spf": "pass", "mailfrom": envelope}
    fails = [
        ("server", "mx.google.com.evil.test"),
        ("spf", "softfail"),
        ("spf", "neutral"),
        ("mailfrom", "alex@example.com"),
        ("mailfrom", "alex+caf_=other=example.com@example.com"),
        ("mailfrom", f"{envelope}.evil.test"),
    ]

    def forwarded(ar: str, return_path: str | None = f"<{envelope}>") -> bytes:
        headers = {"X-Forwarded-For": "alex@example.com trips@example.com"}
        if return_path:
            headers["Return-Path"] = return_path
        headers["Authentication-Results"] = ar
        delta = "Delta <DeltaAirLines@t.delta.com>"
        return make_eml(from_=delta, subject="x", date=D1, text="x", headers=headers).as_bytes()

    n = normalize(forwarded(stamp.format(**passes)), ALLOWED)
    assert n.origin_ok is True and "spf pass for the forwarding envelope" in n.origin_reason
    for condition, token in fails:
        n = normalize(forwarded(stamp.format(**{**passes, condition: token})), ALLOWED)
        assert n.origin_ok is False and n.text == "", (condition, token)
    # the forwarding header names the owner, but no +caf_ envelope names the path: nothing binds
    n = normalize(forwarded(stamp.format(**passes), return_path=None), ALLOWED)
    assert n.origin_ok is False and "spf did not bind" in n.origin_reason


def test_a_quoted_injection_with_no_real_entries_after_it_is_rejected():
    """Text a sender controls can be echoed inside Gmail's own header (a quoted envelope, a
    comment). With no real dkim= or dmarc= entry after it to clash with, the entries it carries
    still count for nothing; the same entries written in the header are believed."""
    fake = "dkim=pass header.i=@example.com;dmarc=pass header.from=example.com"
    for stamp in (
        f'mx.google.com; spf=pass smtp.mailfrom="a;{fake};x"@evil.example',
        f'mx.google.com; spf=pass smtp.mailfrom="a\\";{fake};x"@evil.example',
        f"mx.google.com; spf=pass (google.com: a;{fake}) smtp.mailfrom=a@evil.example",
        f"mx.google.com; spf=pass (a (nested;{fake}) b) smtp.mailfrom=a@evil.example",
        f'mx.google.com; spf=pass smtp.mailfrom="a;{fake}',
    ):
        n = normalize(own_mail(stamp), ALLOWED)
        assert n.origin_ok is False, stamp
        assert "dkim/dmarc did not bind to example.com" in n.origin_reason, stamp
    real = "mx.google.com; spf=pass smtp.mailfrom=a@evil.example; " + fake.replace(";", "; ")
    assert normalize(own_mail(real), ALLOWED).origin_ok is True


# The travel senders this file's fixtures come from: every fixture is decided as if they were
# the trusted sender domains.
VENDORS = ("delta.com", "aa.com", "smallhotel.example")


def every_fixture(monkeypatch) -> dict[str, tuple[bytes, tuple[str, ...]]]:
    """Every message this file's other tests hand `normalize`, with the senders each allows,
    named `<test>:<n>` by its test and its place among that test's messages. Each test that
    takes no fixture is run once while `normalize` records what it is given (and decides it as
    it would)."""
    module = sys.modules[__name__]
    found: dict[str, tuple[bytes, tuple[str, ...]]] = {}
    test = [""]

    def recording(raw, allowed_senders, *args, **kwargs):
        mine = [v for k, v in found.items() if k.rsplit(":", 1)[0] == test[0]]
        if (raw, tuple(allowed_senders)) not in mine:
            found[f"{test[0]}:{len(mine)}"] = (raw, tuple(allowed_senders))
        return normalize_module.normalize(raw, allowed_senders, *args, **kwargs)

    monkeypatch.setattr(module, "normalize", recording)
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("test_") and not inspect.signature(fn).parameters:
            test[0] = name
            fn()
    monkeypatch.undo()
    return found


# What a message comes to: (origin_ok, sender_trusted)
VOUCHED = (True, True)  # read, and its sender vouched for
READ = (True, False)  # read, its sender vouched for by nobody
UNREAD = (False, False)  # rejected unread
# What `gmail` mode decides of each fixture, per test in the order its messages come, with
# VENDORS as the trusted sender domains. Collected before the second trust mode was written; it
# must not move.
GMAIL_DECIDES: dict[str, list[tuple[bool, bool]]] = {
    "test_a_quoted_injection_with_no_real_entries_after_it_is_rejected": [UNREAD] * 5
    + [VOUCHED],
    "test_a_second_allowed_sender_at_another_domain_is_bound_to_its_own_domain": [
        VOUCHED,
        UNREAD,
        UNREAD,
    ],
    "test_a_zone_less_forward_date_reads_in_the_given_zone": [UNREAD],
    "test_authentication_results_injected_through_a_quoted_envelope_is_rejected": [
        UNREAD,
        UNREAD,
        UNREAD,
        VOUCHED,
    ],
    "test_auto_forward_keeps_original_headers_and_passes_origin": [VOUCHED],
    "test_auto_forwarded_mail_is_trusted_only_from_known_travel_senders": [READ, VOUCHED, VOUCHED],
    "test_base64_encoded_attached_email_is_decoded": [VOUCHED],
    "test_direct_mail_from_stranger_is_rejected_unparsed": [UNREAD],
    "test_each_dkim_dmarc_condition_alone_rejects_and_its_flip_accepts": [VOUCHED] + [UNREAD] * 5,
    "test_every_attached_email_is_handed_back": [VOUCHED],
    "test_forward_as_attachment_uses_inner_message_and_its_attachments": [VOUCHED],
    "test_forward_as_attachment_without_inner_date_falls_back_to_outer_date": [VOUCHED],
    "test_html_only_body_becomes_readable_text": [VOUCHED],
    "test_inline_forward_recovers_original_date_subject_sender": [VOUCHED],
    "test_mail_signed_twice_passes_on_its_own_domain_s_signature": [VOUCHED] * 3 + [UNREAD] * 2,
    "test_only_topmost_authentication_results_counts": [UNREAD],
    "test_outlook_forward_sent_line_is_the_original_date": [VOUCHED],
    "test_owner_mail_requires_dkim_and_dmarc_for_its_own_domain": [UNREAD, UNREAD],
    "test_return_path_caf_alone_passes_origin": [READ],
    "test_spf_must_bind_on_the_forwarding_path_too": [VOUCHED] + [UNREAD] * 7,
    "test_spoofed_forwarding_headers_without_google_auth_are_rejected": [UNREAD],
    "test_substring_lookalikes_do_not_match": [UNREAD] * 4,
    "test_unknown_charset_never_crashes_and_spam_is_rejected_before_its_body_is_read": [
        UNREAD,
        VOUCHED,
    ],
    "test_vendor_replies_keep_their_text_when_auto_forwarded": [READ],
    "test_xff_without_return_path_envelope_is_not_enough": [UNREAD],
}


def test_gmail_mode_decides_every_fixture_of_this_file_as_before(monkeypatch):
    decided: dict[str, list[tuple[bool, bool]]] = {}
    for key, (raw, allowed) in every_fixture(monkeypatch).items():
        n = normalize(raw, allowed, require_auth=True, trusted_domains=VENDORS)
        decided.setdefault(key.rsplit(":", 1)[0], []).append((n.origin_ok, n.sender_trusted))
    assert decided == GMAIL_DECIDES


# The fixtures `mailbox` mode rejects too: none names an allowed sender as its From, its +caf_
# envelope or its X-Forwarded-For.
MAILBOX_UNREAD = {
    "test_a_second_allowed_sender_at_another_domain_is_bound_to_its_own_domain:1",
    "test_direct_mail_from_stranger_is_rejected_unparsed:0",
    "test_substring_lookalikes_do_not_match:0",
    "test_substring_lookalikes_do_not_match:1",
    "test_substring_lookalikes_do_not_match:2",
    "test_substring_lookalikes_do_not_match:3",
    "test_unknown_charset_never_crashes_and_spam_is_rejected_before_its_body_is_read:0",
}


def test_mailbox_mode_reads_allowed_senders_and_vouches_for_nobody(monkeypatch):
    """With no authentication check, mail from or via an allowed sender is read as `gmail` mode
    reads what it accepts, and no sender is vouched for: not the owner's own mail, not a vendor
    whose domain Gmail's dmarc passes. Whether the outer From is an allowed sender is still
    said, so an inline forward is unwrapped and attached emails are handed back."""
    unread = set()
    for key, (raw, allowed) in every_fixture(monkeypatch).items():
        gmail = normalize(raw, allowed, require_auth=True, trusted_domains=VENDORS)
        n = normalize(raw, allowed, require_auth=False, trusted_domains=VENDORS)
        assert n.sender_trusted is False, key
        if not n.origin_ok:
            unread.add(key)
            assert not gmail.origin_ok and n.text == "" and n.extra_rfc822 == [], key
            continue
        outer = email.message_from_bytes(raw, policy=email.policy.default)
        own = email.utils.parseaddr(outer["From"])[1].lower() in allowed
        assert n.forwarded_by_owner is own, key
        if gmail.origin_ok:
            seen = ("from_addr", "subject", "date", "text", "forwarded", "extra_rfc822")
            assert [getattr(n, f) for f in seen] == [getattr(gmail, f) for f in seen], key
            assert n.forwarded_by_owner is gmail.forwarded_by_owner, key
    assert unread == MAILBOX_UNREAD
    # the owner's inline forward, with no authentication results at all
    raw = every_fixture(monkeypatch)["test_a_zone_less_forward_date_reads_in_the_given_zone:0"][0]
    n = normalize(raw, ALLOWED, require_auth=False, trusted_domains=VENDORS)
    assert (n.origin_ok, n.forwarded, n.forwarded_by_owner, n.sender_trusted) == (
        True,
        "inline",
        True,
        False,
    )
    # gmail mode is as it was: the owner's authenticated mail is vouched for
    assert normalize(own_mail(OWN_STAMP.format(**OWN_PASSES)), ALLOWED).sender_trusted is True


def test_mailbox_mode_rejects_mail_not_from_or_via_an_allowed_sender_unread(monkeypatch):
    """Mail that names no allowed sender as its From, its +caf_ envelope or its forwarding
    header is rejected in `mailbox` mode as in `gmail` mode, before its body, its attachments or
    an email attached to it is read, whatever stamp it carries and whoever its display name or
    an attached email claims to be."""

    def never(*args, **kwargs):
        raise AssertionError("a rejected message was read past its headers")

    for name in ("_body_text", "_attachments", "_rfc822_parts", "strip_inline_forward"):
        monkeypatch.setattr(normalize_module, name, never)
    owners = make_eml(from_="alex@example.com", subject="Fwd", date=D1, text="x", headers=AR_DKIM)
    for from_, headers in (
        ("spam@example.com", AR_DKIM),
        ('"alex@example.com" <x@evil.example>', {}),
        ("x@evil.example", {"Return-Path": "<alex+caf_=x@example.com.evil.test>"}),
        ("x@evil.example", {"X-Forwarded-For": "alex@example.com.evil.test trips@example.com"}),
        ("x@evil.example", {"Return-Path": "<xalex+caf_=trips=example.com@example.com>"}),
    ):
        m = make_eml(
            from_=from_,
            subject="Your booking",
            date=D1,
            text="Confirmation ZZTEST",
            headers=headers,
            attachments=[("itinerary.pdf", "application/pdf", PDF)],
            rfc822=owners,
        )
        n = normalize(m.as_bytes(), ALLOWED, require_auth=False, trusted_domains=VENDORS)
        assert (n.origin_ok, n.sender_trusted, n.forwarded_by_owner) == (False, False, False)
        assert "not an allowed sender, no forwarding marker" in n.origin_reason, from_
        assert (n.text, n.attachments, n.extra_rfc822) == ("", [], []), from_
