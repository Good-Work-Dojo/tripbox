# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Hostile mail: messages built to break the pipeline. Instructions aimed at the model, a message
over the size cap, broken MIME, deeply nested HTML, attachment names that look like paths, a
forged sender, an allowed sender forwarding junk, an absurd answer and a megabyte subject. Each is
built here at run time from the invented Sample family, or read from the corpus; the model is a
stand-in that answers what the test scripts for each message and counts its calls, the flight log
is one held in memory, and every notice is kept. The defences are structural: the sender gate,
the frozen answer schema, validation that marks instead of trusting, and a flight log that is
sent only the owner's flights with nothing left to check."""

import base64
import contextlib
import datetime as dt
import email.utils
import json
import logging
import re
import resource
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers.flightlog import FIRST_AIRPORT_ID, Scripted, seed
from helpers.inbox import FakeInbox
from test_budget import Asked, Told, pipeline, read_by_the_model, sha, the_corpus
from test_normalize import AR_DKIM

from tripbox import corpus, util
from tripbox.mail import normalize as normalize_module
from tripbox.mail.imap import KV_LAST_UID, MAX_MESSAGE_BYTES
from tripbox.notify.notifier import Notifier
from tripbox.notify.text import check_card, format_outcomes, outcome_line
from tripbox.parse import budget, kitinerary
from tripbox.parse import pipeline as pl
from tripbox.parse.models import (
    READ_BY_MODEL,
    WireExtraction,
    WireLeg,
    WireReservation,
    is_complete,
    to_extraction,
)
from tripbox.parse.provider import ModelError
from tripbox.pipeline import run as runmod
from tripbox.validate import checks, rules
from tripbox.write.sync import Syncer

# The deterministic extractor as shipped, kept before any test stands something in for it.
REAL_EXTRACTOR = kitinerary.run_extractor
OWNER = the_corpus().settings["TRIPBOX_MAIL_ALLOWED_SENDERS"].split(",")[0]
# Gmail's stamp on mail the owner sends from their own account.
STAMP = AR_DKIM["Authentication-Results"]
# The same stamp on mail that only claims to be the owner's: Gmail found neither signature nor
# alignment for the From domain.
FORGED = STAMP.replace("dkim=pass", "dkim=fail").replace("dmarc=pass", "dmarc=fail")
INSTRUCTED = "26-instruction-text"  # the corpus's email that addresses the model
BROKEN = "28-broken-mime"  # the corpus's email whose MIME is cut off
ABSURD_YEAR = 1900
SECONDS = 30.0  # generous on a busy host: each input here is read in a few seconds at most


class StandIn(Asked):
    """The stand-in for the model: it answers a corpus email with the corpus's own answer and any
    other message with what the test scripts for its bytes (`answers`, by SHA-256), counts its
    calls (`calls`) and keeps every message it is asked to read (`read`)."""

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.read = []

    def extract_wire(self, nm, hint=None):
        self.read.append(nm)
        return super().extract_wire(nm, hint)


NOTHING = WireExtraction(document_type="other", confidence=0.9)  # an answer that books nothing


@contextlib.contextmanager
def hostile(
    tmp_path: Path, monkeypatch, *, flight_log: bool = True, **environ
) -> Iterator[tuple[runmod.Context, StandIn, Told, Scripted | None]]:
    """A pipeline context over a new ledger under the corpus's settings and `environ`, its model
    the stand-in under the cap as `provider.build` wraps a provider, every notice kept. With
    `flight_log` one is kept: held in memory, knowing the corpus's airports and airlines, brought
    in step by the pass after each message. The structured-data extractor finds nothing, so the
    model reads every message the gate lets in."""
    with pipeline(tmp_path, monkeypatch, model=False, **environ) as (ctx, _, told):
        model = StandIn(ctx.settings.llm_model)
        ctx.model = budget.Budgeted(model, budget.Budget(ctx.ledger, ctx.settings))
        log = None
        if flight_log:
            ctx.ledger.kv.set("airtrail", "on")
            seed(ctx.ledger, the_corpus())
            log = Scripted(ctx.ledger)
            ctx.airtrail = log
            ctx.syncer = Syncer(
                store=ctx.trips,
                ledger=ctx.ledger,
                book=ctx.trips.book,
                client=log,
                refs=None,
                dry_run=False,
            )
        yield ctx, model, told, log


@contextlib.contextmanager
def bounded_memory(extra: int = 2 * 1024**3) -> Iterator[None]:
    """For its duration this process may map at most `extra` bytes more than it has mapped now:
    a path whose memory grows with the square of its input raises MemoryError here, and the host
    under the tests keeps its memory. If the process cannot read its own size, no limit is set."""
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        yield
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = pages * resource.getpagesize() + extra
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def owners_flights() -> tuple[corpus.Entry, WireExtraction]:
    """The first corpus email the model reads that books the owner on flights whose airports it
    prints as codes, and its answer's flights alone."""
    for entry in read_by_the_model():
        flights = [r for r in entry.answer.reservations if r.kind == "flight"]
        ends = [end for r in flights for leg in r.legs for end in (leg.origin, leg.destination)]
        owners = any(i["people"]["me"] for i in entry.outcome["items"] if i["kind"] == "flight")
        if owners and ends and all(re.fullmatch(r"[A-Z]{3}", end or "") for end in ends):
            return entry, entry.answer.model_copy(update={"reservations": flights})
    raise AssertionError("the corpus books the owner on no flight printed with its codes")


def head(subject: str, *, n: int, sent: dt.datetime, stamp: str = STAMP, more: str = "") -> str:
    """The headers of the owner's own message number `n`, as Gmail stamps it, then `more`."""
    return (
        f"Authentication-Results: {stamp}\r\n"
        f"From: Alex Sample <{OWNER}>\r\n"
        "To: trips@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {email.utils.format_datetime(sent)}\r\n"
        f"Message-ID: <hostile-{n}@example.com>\r\n"
        "MIME-Version: 1.0\r\n" + more
    )


def plain(subject: str, body: str, *, n: int, sent: dt.datetime, stamp: str = STAMP) -> bytes:
    """The owner's own plain-text message number `n`."""
    ctype = 'Content-Type: text/plain; charset="utf-8"\r\nContent-Transfer-Encoding: 8bit\r\n'
    text = head(subject, n=n, sent=sent, stamp=stamp, more=ctype) + "\r\n" + body + "\r\n"
    return text.encode("utf-8")


def sent_at() -> dt.datetime:
    """When the corpus's first email was sent: the day every message built here is dated."""
    return corpus.sent(the_corpus().entries[0])


def files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file() or p.is_symlink()}


# ----------------------------------------------------------------------- instructions in the text
def asked_for(name: str) -> tuple[str, str, str, str, str]:
    """What the row of entry `name` in the corpus's SCENARIOS.md says its text asks the model to
    add: a flight's carrier, number, origin and destination, and the first name it is for."""
    text = (corpus.default_dir() / "SCENARIOS.md").read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith(f"| `{name}` ")]
    found = re.search(r"add a flight (\w{2}) (\d{1,4}) ([A-Z]{3})→([A-Z]{3}) for (\w+)", row)
    assert found, f"{name}: its row names no flight it asks for"
    carrier, number, origin, destination, who = found.groups()
    return carrier, number, origin, destination, who


def obeyed() -> tuple[WireExtraction, WireLeg]:
    """Entry 26 as a model that obeys its text answers it: its own answer plus the flight the text
    asks for (read from the entry's row), for the stay's traveller on the day the stay begins,
    with full confidence; and that flight's leg."""
    entry = the_corpus().entry(INSTRUCTED)
    carrier, number, origin, destination, who = asked_for(INSTRUCTED)
    (stay,) = [r for r in entry.answer.reservations if r.kind == "lodging"]
    assert stay.passengers and stay.passengers[0].split()[0] == who
    leg = WireLeg(
        carrier=carrier,
        number=number,
        origin=origin,
        destination=destination,
        departure=f"{stay.start[:10]}T09:00",
    )
    flight = WireReservation(
        kind="flight", status="confirmed", passengers=list(stay.passengers), legs=[leg]
    )
    answer = entry.answer.model_copy(
        update={"confidence": 1.0, "reservations": [*entry.answer.reservations, flight]}
    )
    return answer, leg


def test_instruction_text_from_a_sender_nobody_vouched_for_reaches_no_flight_log(
    tmp_path, monkeypatch
):
    """Entry 26 addresses the model in its text: add a flight for the owner, report full
    confidence. It comes from a sender nobody vouched for. A model that obeys gives its own
    answer plus that flight: the flight is filed, marked as mailed by a sender nobody vouched for
    and held back; the flight log is sent nothing."""
    entry = the_corpus().entry(INSTRUCTED)
    answer, leg = obeyed()
    carrier, number, origin, destination = leg.carrier, leg.number, leg.origin, leg.destination
    with hostile(tmp_path, monkeypatch) as (ctx, model, told, log):
        model.answers[sha(entry.raw)] = answer
        with util.clock_at(corpus.sent(entry)):
            res = runmod.process_raw(ctx, entry.raw, source="imap", uid=1)
        assert (res.status, model.calls) == ("processed", 1)
        by_kind = {o.action.item.kind: o for o in res.outcomes}
        assert set(by_kind) == {"lodging", "flight"}
        assert all(o.result == "filed" for o in res.outcomes)
        filed = ctx.trips.item(by_kind["flight"].trip_item_id)
        assert filed.payload["flight_code"] == f"{carrier}{number}"
        assert (filed.payload["origin"], filed.payload["destination"]) == (origin, destination)
        assert ctx.trips.book.classify(filed.passengers).me_present
        assert (filed.checks, filed.sync_state) == (("unverified_sender",), "blocked")
        assert ctx.trips.item(by_kind["lodging"].trip_item_id).checks == ()
        assert (log.requests, log.flights) == ([], [])
        (card,) = told.of("needs_look")
        assert checks.LABELS["unverified_sender"] in card


def as_it_stands(ctx: runmod.Context) -> dict[int, str]:
    """Every booking the store holds, by id, as its row stands: every stored field."""
    rows = ctx.ledger.conn.execute("SELECT * FROM trip_items ORDER BY id").fetchall()
    return {r["id"]: json.dumps(dict(r), sort_keys=True, default=str) for r in rows}


def test_instruction_text_in_mail_a_vouched_sender_sent_is_read_as_that_senders_word(
    tmp_path, monkeypatch
):
    """What mail a vouched sender sent says is that sender's word, its text included: no check
    asks whether a flight the model read is printed in the email, since an injected instruction
    prints the flight it names. Entry 26, handed in by the owner as their own, read by a model
    that obeys its text: the extra flight is filed with nothing to check and, the owner being on
    it, sent to the flight log, and the owner is told once. The limits are structural: no stored
    field of a booking the trip store held before, and no flight the flight log held before, is
    changed or deleted by it."""
    entry = the_corpus().entry(INSTRUCTED)
    answer, leg = obeyed()
    earlier, _ = owners_flights()
    with hostile(tmp_path, monkeypatch) as (ctx, model, told, log):
        for n, code in enumerate((leg.origin, leg.destination)):
            if ctx.ledger.refcache.get("airport", code) is None:  # a flight log knows them all
                known = {"id": FIRST_AIRPORT_ID - 1 - n, "iata": code, "type": "large_airport"}
                ctx.ledger.refcache.put("airport", code, known)
        # read at today's clock, as the reference cache was written: its answers stay fresh
        first = runmod.process_raw(ctx, earlier.raw, source="imap", uid=1)
        assert first.status == "processed" and log.flights
        before, logged = as_it_stands(ctx), log.flights
        told.notices.clear()
        log.requests.clear()

        model.answers[sha(entry.raw)] = answer
        res = runmod.process_raw(ctx, entry.raw, source="cli", trusted=True)
        assert (res.status, model.calls) == ("processed", 2)
        by_kind = {o.action.item.kind: o for o in res.outcomes}
        assert set(by_kind) == {"lodging", "flight"}
        assert all(o.result == "filed" for o in res.outcomes)
        sent = ctx.trips.item(by_kind["flight"].trip_item_id)
        assert sent.payload["flight_code"] == f"{leg.carrier}{leg.number}"
        assert ctx.trips.book.classify(sent.passengers).me_present
        assert (sent.checks, sent.sync_state) == ((), "synced")
        assert [r[0] for r in log.sent] == ["create"]
        assert log.flights[: len(logged)] == logged and len(log.flights) == len(logged) + 1
        after = as_it_stands(ctx)
        assert {i: after[i] for i in before} == before
        assert set(after) - set(before) == {o.trip_item_id for o in res.outcomes}
        assert [n.event for n in told.notices] == ["filed"]


# ------------------------------------------------------------------------------- over the cap
def test_a_25_mb_message_is_left_unread_and_the_poll_goes_on(tmp_path, monkeypatch):
    """A 25 MB message is never fetched: one notice says so and the poll reads the message after
    it, which the model reads and which is filed."""
    sent = sent_at()
    ctype = "Content-Type: text/plain; charset=us-ascii\r\n"
    top = (head("Sample fare rules", n=1, sent=sent, more=ctype) + "\r\n").encode()
    line = b"Sample fare rule: no part of this ticket is refundable after departure.\r\n"
    size = 25 * runmod.MB
    big = top + line * ((size - len(top)) // len(line))
    big += b"x" * (size - len(big))
    after = read_by_the_model()[0]
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        inbox = FakeInbox(messages={1: big, 2: after.raw}).new_to(ctx.ledger)
        assert inbox.size(1) == size > MAX_MESSAGE_BYTES
        with bounded_memory():
            t0 = time.perf_counter()
            summary = runmod.poll(ctx, inbox)
            took = time.perf_counter() - t0
        assert took < SECONDS
        assert (summary.fetched, summary.counts, summary.errors) == (
            1,
            {"oversize": 1, "processed": 1},
            0,
        )
        assert inbox.fetched == [2] and model.calls == 1
        said = runmod.OVERSIZE.format(n=25, limit=MAX_MESSAGE_BYTES // runmod.MB)
        assert told.of("failure") == [said]
        assert ctx.ledger.kv.get(KV_LAST_UID) == "2"
        rows = ctx.ledger.conn.execute("SELECT mail_uid, status FROM messages").fetchall()
        assert [tuple(r) for r in rows] == [(2, "processed")]


# -------------------------------------------------------------------------------- broken MIME
def entry_28() -> bytes:
    """The corpus's own: an HTML part cut off mid-tag in a charset that does not exist, and no
    closing boundary."""
    return the_corpus().entry(BROKEN).raw


def base64_cut_mid_line() -> bytes:
    """An attached PDF whose base64 stops in the middle of a line, with no closing boundary."""
    pdf = base64.encodebytes(b"%PDF-1.4\n" + b"Sample e-ticket for Alex Sample. " * 60).decode()
    lines = pdf.splitlines()
    cut = "\r\n".join(lines[: len(lines) // 2]) + "\r\n" + lines[len(lines) // 2][:30]
    ctype = 'Content-Type: multipart/mixed; boundary="sample-cut"\r\n'
    body = (
        "\r\n--sample-cut\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        "The e-ticket is attached.\r\n"
        "--sample-cut\r\n"
        'Content-Type: application/pdf; name="e-ticket.pdf"\r\n'
        'Content-Disposition: attachment; filename="e-ticket.pdf"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n" + cut
    )
    return (head("Fwd: Your e-ticket", n=1, sent=sent_at(), more=ctype) + body).encode()


def headers_and_no_body() -> bytes:
    """A header block that simply stops: no blank line, no body, no last line break."""
    return head("Fwd: Your booking", n=1, sent=sent_at()).rstrip("\r\n").encode()


def nul_bytes() -> bytes:
    """NUL bytes in the subject and all through the body."""
    body = "Confirmation\x00\x00 for\x00 Alex Sample\x00\r\n\x00\x00\x00"
    return plain("Fwd: Your\x00 trip", body, n=1, sent=sent_at())


def header_of_10000_lines() -> bytes:
    """A Content-Type header folded over 10,000 lines, a parameter on each."""
    params = ";\r\n ".join(f"x-sample-{n}={n}" for n in range(10_000))
    ctype = f'Content-Type: text/plain; charset="utf-8";\r\n {params}\r\n'
    head_ = head("Fwd: Your booking", n=1, sent=sent_at(), more=ctype)
    return (head_ + "\r\nYour booking for Alex Sample.\r\n").encode()


def charset_that_does_not_exist() -> bytes:
    """A subject, a plain part and an HTML part, each in a charset no codec knows."""
    nowhere = "x-sample-nowhere"
    ctype = 'Content-Type: multipart/alternative; boundary="sample-alt"\r\n'
    body = (
        "\r\n--sample-alt\r\n"
        f'Content-Type: text/plain; charset="{nowhere}"\r\n'
        "Content-Transfer-Encoding: 8bit\r\n\r\n"
    ).encode() + b"R\xe9servation for Alex Sample\r\n" + (
        "--sample-alt\r\n"
        f'Content-Type: text/html; charset="{nowhere}"\r\n'
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        "<p>R=E9servation for Alex Sample</p>\r\n"
        "--sample-alt--\r\n"
    ).encode()
    subject = f"=?{nowhere}?q?Votre_r=E9servation?="
    return head(subject, n=1, sent=sent_at(), more=ctype).encode() + body


@pytest.mark.parametrize(
    "build",
    [
        entry_28,
        base64_cut_mid_line,
        headers_and_no_body,
        nul_bytes,
        header_of_10000_lines,
        charset_that_does_not_exist,
    ],
    ids=lambda build: build.__name__,
)
def test_broken_mime_fails_one_message_never_the_poll(tmp_path, monkeypatch, build):
    """Each broken message ends as it should (the corpus's entry as the corpus says, each built
    here as nothing to book: the model reads what survives of it), the model is asked once for
    it and once for the message after it, a `failure` notice goes out only for a message that
    failed, the poll counts no error, and the message after it is read and filed."""
    raw = build()
    after = read_by_the_model()[0]
    want = the_corpus().entry(BROKEN).outcome["status"] if build is entry_28 else "ignored"
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        if build is not entry_28:
            model.answers[sha(raw)] = NOTHING
        inbox = FakeInbox(messages={1: raw, 2: after.raw}).new_to(ctx.ledger)
        with bounded_memory():
            t0 = time.perf_counter()
            summary = runmod.poll(ctx, inbox)
            took = time.perf_counter() - t0
        assert took < SECONDS and summary.errors == 0
        assert inbox.fetched == [1, 2] and ctx.ledger.kv.get(KV_LAST_UID) == "2"
        rows = ctx.ledger.conn.execute("SELECT mail_uid, status FROM messages ORDER BY id")
        (first, second) = [tuple(r) for r in rows]
        assert (first, second) == ((1, want), (2, "processed")) and model.calls == 2
        assert (told.of("failure") != []) is (want == "failed")


# -------------------------------------------------------------------------------- nested HTML
def test_deeply_nested_html_is_read_without_recursion(tmp_path, monkeypatch, caplog):
    """10,000 nested elements around one line of text: the line comes out, and it is what the
    model is given to read, with no RecursionError and inside a time bound."""
    said = "Your booking for Alex Sample is confirmed."
    tags = ("div", "span", "table", "tr", "td", "b", "font", "p")
    opened = [tags[n % len(tags)] for n in range(10_000)]
    html = "".join(f"<{t}>" for t in opened) + said + "".join(f"</{t}>" for t in opened[::-1])
    ctype = 'Content-Type: text/html; charset="utf-8"\r\nContent-Transfer-Encoding: 8bit\r\n'
    raw = (head("Fwd: Your booking", n=1, sent=sent_at(), more=ctype) + "\r\n" + html).encode()
    caplog.set_level(logging.DEBUG)
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        model.answers[sha(raw)] = NOTHING
        with bounded_memory():
            t0 = time.perf_counter()
            res = runmod.process_raw(ctx, raw, source="imap", uid=1)
            took = time.perf_counter() - t0
        assert took < SECONDS
        assert res.status == "ignored" and model.calls == 1
        (nm,) = model.read
        assert nm.text == said
    assert not [r for r in caplog.records if "RecursionError" in logging.Formatter().format(r)]


# ------------------------------------------------------------------ attachment names as paths
def attached(names: list[str], *, sent: dt.datetime) -> bytes:
    """The owner's message with one small PDF attached under each of `names`, each name given
    in RFC 2231's encoded form, so a name holds any character."""
    pdf = base64.encodebytes(b"%PDF-1.4\nSample e-ticket\n").decode().replace("\n", "\r\n")
    ctype = 'Content-Type: multipart/mixed; boundary="sample-names"\r\n'
    body = "\r\n--sample-names\r\nContent-Type: text/plain\r\n\r\nThe e-tickets are attached.\r\n"
    for name in names:
        quoted = "".join(
            c if c.isalnum() or c in ".-_" else "".join(f"%{b:02X}" for b in c.encode())
            for c in name
        )
        body += (
            "--sample-names\r\n"
            "Content-Type: application/pdf\r\n"
            f"Content-Disposition: attachment; filename*=utf-8''{quoted}\r\n"
            "Content-Transfer-Encoding: base64\r\n\r\n" + pdf
        )
    body += "--sample-names--\r\n"
    return (head("Fwd: Your e-tickets", n=1, sent=sent, more=ctype) + body).encode()


def test_attachment_names_that_look_like_paths_name_no_file(tmp_path, monkeypatch, caplog):
    """Attachments named `../../x.pdf`, an absolute path, a drive path and a name holding a NUL
    reach the reader as named, and the shipped extractor reads the message without failing:
    afterwards the temporary tree holds the ledger and the one file the archive named itself,
    nothing else."""
    base = tmp_path / "run"
    cwd = base / "cwd" / "a" / "b"
    temp = base / "tmp"
    cwd.mkdir(parents=True)
    temp.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    assert shutil.which(kitinerary.BINARY) is not None  # in the image the suite runs in
    names = ["../../x.pdf", str(base / "escaped.pdf"), "C:\\x.pdf", "x\x00.pdf"]
    raw = attached(names, sent=sent_at())
    extracted = []

    def shipped(data, suffix, context):
        extracted.append(suffix)
        return REAL_EXTRACTOR(data, suffix, context)

    caplog.set_level(logging.WARNING)
    with hostile(base, monkeypatch, flight_log=False) as (ctx, model, told, _):
        monkeypatch.setattr(pl.kitinerary, "run_extractor", shipped)
        model.answers[sha(raw)] = NOTHING
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert res.status == "ignored" and model.calls == 1
        assert extracted == [".eml"] and caplog.records == []
        (nm,) = model.read
        assert [a.filename for a in nm.attachments] == names
        archived = Path(ctx.ledger.messages.get(res.message_row_id)["archive_path"])
        ledger = ctx.settings.ledger_path
    assert re.fullmatch(r"\d{8}T\d{6}Z_1_[0-9a-f]{8}\.eml", archived.name)
    kept = {p for p in files_under(base) if not p.name.startswith(ledger.name)}
    assert kept == {archived}
    assert all(p.parent == ledger.parent for p in files_under(base) - kept)


# ------------------------------------------------------------------------------- forged sender
def test_a_forged_sender_failing_dmarc_is_rejected_unread_in_gmail_mode_and_held_in_mailbox_mode(
    tmp_path, monkeypatch
):
    """A message that claims the owner's address, which Gmail found neither signed nor aligned,
    booking the owner's flights. In `gmail` mode it is rejected: no body is decoded and the model
    is never asked. In `mailbox` mode nothing is authenticated, so it is read as the owner's
    address says, and every flight in it is held back as mailed by a sender nobody vouched for."""
    entry, flights = owners_flights()
    raw = plain("Fwd: Your flights", "Your flights.", n=1, sent=corpus.sent(entry), stamp=FORGED)
    decoded: list[str] = []
    for name in ("_body_text", "_attachments", "_rfc822_parts"):
        real = getattr(normalize_module, name)

        def counted(msg, _real=real, _name=name):
            decoded.append(_name)
            return _real(msg)

        monkeypatch.setattr(normalize_module, name, counted)

    with hostile(tmp_path / "gmail", monkeypatch) as (ctx, model, told, log):
        assert ctx.settings.mail_trust == "gmail"
        model.answers[sha(raw)] = flights
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert (res.status, res.outcomes, res.normalized.text) == ("rejected", [], "")
        assert (decoded, model.calls, told.notices, log.requests) == ([], 0, [], [])
        row = ctx.ledger.messages.get(res.message_row_id)
        assert (row["status"], row["origin_ok"]) == ("rejected", runmod.ORIGIN_REJECTED)

    with hostile(tmp_path / "mailbox", monkeypatch, TRIPBOX_MAIL_TRUST="mailbox") as (
        ctx,
        model,
        told,
        log,
    ):
        model.answers[sha(raw)] = flights
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert (res.status, model.calls) == ("processed", 1) and "_body_text" in decoded
        row = ctx.ledger.messages.get(res.message_row_id)
        assert row["origin_ok"] == runmod.ORIGIN_UNAUTHENTICATED
        legs = sum(len(r.legs) for r in flights.reservations)
        assert len(res.outcomes) == legs and all(o.result == "filed" for o in res.outcomes)
        for o in res.outcomes:
            held = ctx.trips.item(o.trip_item_id)
            assert ctx.trips.book.classify(held.passengers).me_present
            assert (held.checks, held.sync_state) == (("unverified_sender",), "blocked")
        assert log.requests == []
        assert len(told.of("needs_look")) == legs


# ------------------------------------------------------------------------------ junk forwarded
def test_an_allowed_sender_forwarding_junk_files_nothing_and_says_nothing(tmp_path, monkeypatch):
    """The owner forwards a newsletter and the model reads it as marketing: the message is
    ignored, nobody is told anything, and the one call is counted against the cap."""
    body = (
        "---------- Forwarded message ---------\r\n"
        "From: Sample Deals <deals@sampledeals.example>\r\n"
        "Subject: This week's sample deals\r\n"
        "To: <alex@example.com>\r\n\r\n\r\n"
        "Save on sample luggage, sample pillows and sample socks. Unsubscribe any time:\r\n"
        "https://www.example.com/\r\n"
    )
    raw = plain("Fwd: This week's sample deals", body, n=1, sent=sent_at())
    junk = WireExtraction(document_type="marketing", confidence=0.9)
    with hostile(tmp_path, monkeypatch) as (ctx, model, told, log):
        model.answers[sha(raw)] = junk
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert (res.status, res.outcomes) == ("ignored", [])
        assert (told.notices, log.requests) == ([], [])
        row = ctx.ledger.messages.get(res.message_row_id)
        assert (row["status"], row["document_type"]) == ("ignored", "marketing")
        cap = budget.Budget(ctx.ledger, ctx.settings)
        assert model.calls == 1 and cap.by_month
        assert ctx.ledger.llm.month_cost(cap.period())[0] == 1
        assert ctx.trips.all_trips() == []


# ------------------------------------------------------------------------------ absurd answers
def later(local: str | None, days: int) -> str | None:
    """A local time `days` later, as the wire writes one."""
    if not local:
        return local
    return (dt.datetime.fromisoformat(local) + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def test_an_absurd_answer_is_filed_marked_and_held(tmp_path, monkeypatch):
    """Answers that fit the schema and not the world, each the only email of its ledger. A
    flight in 1900 (a corpus flight with its year replaced) is filed with `date_past` and held
    back. Forty legs in one email (that flight on forty days) are forty bookings, each marked
    `low_confidence` as more than any one email holds: the flight log is sent nothing, and the
    owner gets one `filed` notice showing FILED_LINES of them, how many more the page holds and
    how many more there are to check, then CARD_LIMIT cards."""
    entry, flights = owners_flights()
    booked = flights.reservations[0]
    leg = booked.legs[0]
    old = leg.model_copy(
        update={
            "departure": f"{ABSURD_YEAR}{leg.departure[4:]}",
            "arrival": f"{ABSURD_YEAR}{leg.arrival[4:]}" if leg.arrival else None,
        }
    )
    forty = [
        leg.model_copy(
            update={"departure": later(leg.departure, n), "arrival": later(leg.arrival, n)}
        )
        for n in range(40)
    ]
    raw = plain("Fwd: Your flights", "Your flights.", n=1, sent=corpus.sent(entry))
    with hostile(tmp_path / "ancient", monkeypatch) as (ctx, model, told, log):
        model.answers[sha(raw)] = flights.model_copy(
            update={"reservations": [booked.model_copy(update={"legs": [old]})]}
        )
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        (o,) = res.outcomes
        ancient = ctx.trips.item(o.trip_item_id)
        assert (res.status, o.result) == ("processed", "filed")
        assert ancient.payload["date_local"].startswith(str(ABSURD_YEAR))
        assert (ancient.checks, ancient.sync_state) == (("date_past",), "blocked")
        assert log.requests == []
        assert [n.event for n in told.notices] == ["filed", "needs_look"]

    with hostile(tmp_path / "forty", monkeypatch) as (ctx, model, told, log):
        model.answers[sha(raw)] = flights.model_copy(
            update={"reservations": [booked.model_copy(update={"legs": forty})]}
        )
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert res.status == "processed" and len(res.outcomes) == 40
        assert len({o.trip_item_id for o in res.outcomes}) == 40
        assert all(o.result == "filed" for o in res.outcomes)
        held = [ctx.trips.item(o.trip_item_id) for o in res.outcomes]
        assert {(ti.checks, ti.sync_state) for ti in held} == {(("low_confidence",), "blocked")}
        assert log.requests == [] and model.calls == 1
        head = f"📬 {res.normalized.subject} ({res.parsed.parser})"
        lines = [outcome_line(o, flight_log=True) for o in res.outcomes[: runmod.FILED_LINES]]
        assert told.of("filed") == [
            "\n".join(
                [
                    head,
                    *lines,
                    f"and {40 - runmod.FILED_LINES} more on the page",
                    f"and {40 - runmod.CARD_LIMIT} more to check on the page",
                ]
            )
        ]
        cards = told.of("needs_look")
        assert cards == [check_card(ti, ctx.book) for ti in held[: runmod.CARD_LIMIT]]
        assert [n.event for n in told.notices] == ["filed"] + ["needs_look"] * runmod.CARD_LIMIT


def test_an_answer_of_more_bookings_than_an_email_holds_is_marked_and_a_reading_is_not(
    tmp_path, monkeypatch
):
    """The owner's flight on MAX_BOOKINGS days in one answer of the model is that many bookings
    with nothing to check; on one day more, every one is marked `low_confidence`. The same one
    more read by the structured-data extractor is left alone: only the model's answer is
    doubted for its size."""
    entry, flights = owners_flights()
    booked = flights.reservations[0]
    leg = booked.legs[0]

    def days(n: int) -> WireExtraction:
        legs = [
            leg.model_copy(
                update={"departure": later(leg.departure, d), "arrival": later(leg.arrival, d)}
            )
            for d in range(n)
        ]
        many = booked.model_copy(update={"legs": legs})
        return flights.model_copy(update={"reservations": [many]})

    raw = plain("Fwd: Your flights", "Your flights.", n=1, sent=corpus.sent(entry))
    most = rules.MAX_BOOKINGS
    for n, read_by, marked in ((most, "model", ()), (most + 1, "model", ("low_confidence",))):
        with hostile(tmp_path / f"{read_by}-{n}", monkeypatch, flight_log=False) as (
            ctx,
            model,
            told,
            _,
        ):
            model.answers[sha(raw)] = days(n)
            res = runmod.process_raw(ctx, raw, source="imap", uid=1)
            assert (res.parsed.parser, len(res.outcomes)) == (READ_BY_MODEL, n)
            assert {ctx.trips.item(o.trip_item_id).checks for o in res.outcomes} == {marked}
    with hostile(tmp_path / "read", monkeypatch, flight_log=False) as (ctx, model, told, _):
        whole = to_extraction(days(most + 1))
        assert all(is_complete(r) for r in whole.reservations)
        monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [{}])
        monkeypatch.setattr(pl.kitinerary, "map_jsonld", lambda objects: whole.model_copy())
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert (res.parsed.parser, len(res.outcomes), model.calls) == ("kitinerary", most + 1, 0)
        assert {ctx.trips.item(o.trip_item_id).checks for o in res.outcomes} == {()}


# ---------------------------------------------------------------------------- megabyte subject
def folded_subject(letters: int, per_word: int = 60) -> str:
    """A Subject value of `letters` letters written as encoded words of `per_word` letters,
    folded one to a line: adjacent encoded words decode with nothing between them, so it reads
    as one run of letters."""
    words = []
    for start in range(0, letters, per_word):
        words.append(f"=?utf-8?q?{'x' * min(per_word, letters - start)}?=")
    return "\r\n ".join(words)


@pytest.mark.parametrize("ends", ["processed", "ignored", "failed", "waiting"])
def test_a_megabyte_subject_is_shown_clipped_in_logs_and_notices(
    tmp_path, monkeypatch, caplog, ends
):
    """The owner's message whose subject is 2,000,000 letters of folded encoded words is read
    like any other, inside a time and a memory bound, whichever way the model's reading ends it:
    its flights filed, nothing to file, no answer, or a model that cannot be reached. Every log
    record and every notice it causes is under 2,000 characters, and it causes at most one
    `failure` or `filed` notice. The subject is cut to SUBJECT_MAX where it is read, so the
    model, the ledger and the bookings all hold that, never cut with the `…` of a notice."""
    entry, flights = owners_flights()
    raw = plain(folded_subject(2_000_000), "Your flights.", n=1, sent=corpus.sent(entry))
    caplog.set_level(logging.DEBUG)
    with hostile(tmp_path, monkeypatch) as (ctx, model, told, log):
        if ends == "processed":
            model.answers[sha(raw)] = flights
        elif ends == "ignored":
            model.answers[sha(raw)] = NOTHING
        elif ends == "waiting":
            model.fails = ModelError("unavailable", "no route to the model", word="unreachable")
        inbox = FakeInbox(messages={1: raw}).new_to(ctx.ledger)
        with bounded_memory():
            t0 = time.perf_counter()
            summary = runmod.poll(ctx, inbox)
            took = time.perf_counter() - t0
        assert took < SECONDS
        assert (summary.errors, summary.counts) == (0, {ends: 1})
        # the same poll's resume pass finds the model still down without asking it again
        assert model.calls == 1
        (row,) = ctx.ledger.conn.execute("SELECT subject FROM messages").fetchall()
        assert row["subject"] == "x" * normalize_module.SUBJECT_MAX
        assert [nm.subject for nm in model.read] == [row["subject"]]
    said = {"processed": ["filed"], "ignored": [], "failed": ["failure"], "waiting": ["failure"]}
    assert [n.event for n in told.notices] == said[ends]
    assert all(len(n.body) < 2000 for n in told.notices)
    long = [r for r in caplog.records if len(logging.Formatter().format(r)) >= 2000]
    assert not long, f"{len(long)} log record(s) of 2,000 characters or more"


def test_a_megabyte_subject_in_a_forwarded_block_is_cut_where_it_is_read(
    tmp_path, monkeypatch, caplog
):
    """The owner forwards an email inline, and the forwarded block's Subject line is 2,000,000
    letters: it is read inside a time and a memory bound, and the subject is cut to SUBJECT_MAX
    where the block is read, so the model, the ledger, the bookings, the logs and the notices
    all get the cut one."""
    entry, flights = owners_flights()
    letters = "x" * 2_000_000
    body = (
        "---------- Forwarded message ---------\r\n"
        "From: Sample Air <bookings@sampleair.example>\r\n"
        f"Subject: {letters}\r\n\r\n"
        "Your flights.\r\n"
    )
    raw = plain("Fwd: Your flights", body, n=1, sent=corpus.sent(entry))
    caplog.set_level(logging.DEBUG)
    with hostile(tmp_path, monkeypatch) as (ctx, model, told, log):
        model.answers[sha(raw)] = flights
        with bounded_memory():
            t0 = time.perf_counter()
            res = runmod.process_raw(ctx, raw, source="imap", uid=1)
            took = time.perf_counter() - t0
        assert took < SECONDS and res.status == "processed"
        cut = "x" * normalize_module.SUBJECT_MAX
        (read,) = model.read
        assert (read.subject, read.from_addr, read.forwarded) == (
            cut,
            "bookings@sampleair.example",
            "inline",
        )
        assert ctx.ledger.messages.get(res.message_row_id)["subject"] == cut
        for o in res.outcomes:
            assert ctx.trips.item(o.trip_item_id).payload["subject"] == cut
    assert told.of("filed") and all(len(n.body) < 2000 for n in told.notices)
    long = [r for r in caplog.records if len(logging.Formatter().format(r)) >= 2000]
    assert not long, f"{len(long)} log record(s) of 2,000 characters or more"


# -------------------------------------------------------------------- headers at and past the limit
# Far above what reading one message here takes, and far below what parsing forty headers of
# encoded words at the limit together would.
PARSE_BUDGET = 256 * 1024**2


def encoded_words(chars: int) -> str:
    """A header value of at most `chars` characters: the shortest encoded words, folded one to a
    line. What the standard library's header parser holds for one grows with the square of its
    length."""
    word, fold = "=?a?q?x?=", "\r\n "
    return fold.join([word] * ((chars + len(fold)) // (len(word) + len(fold))))


def past_the_limit() -> str:
    """A run of words twice as long as the longest header value read as it stands."""
    return " ".join(["sample"] * (2 * normalize_module.MAX_HEADER_CHARS // 7))


def raw_mail(*headers: tuple[str, str], body: str = "Your booking.") -> bytes:
    """A plain-text message with `headers`, in their order, as they stand."""
    lines = "".join(f"{name}: {value}\r\n" for name, value in headers)
    end = "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=us-ascii\r\n\r\n"
    return (lines + end + body + "\r\n").encode()


def owners_headers(n: int, **given: str) -> list[tuple[str, str]]:
    """The headers of the owner's message number `n` as Gmail stamps it, with any of them
    (`From`, `Authentication_Results`, `Message_ID`) given otherwise."""
    return [
        ("Authentication-Results", given.get("Authentication_Results", STAMP)),
        ("From", given.get("From", f"Alex Sample <{OWNER}>")),
        ("To", "trips@example.com"),
        ("Subject", "Fwd: Your booking"),
        ("Date", email.utils.format_datetime(sent_at())),
        ("Message-ID", given.get("Message_ID", f"<hostile-{n}@example.com>")),
    ]


def test_forty_forwarding_headers_at_the_limit_from_a_stranger_are_rejected_unread(
    tmp_path, monkeypatch
):
    """A stranger's message carrying forty X-Forwarded-For headers, each of encoded words up to
    the header limit. The sender check reads that header as it stands, never parsed, so the
    message is rejected inside a memory bound far below what parsing them all would take, read
    no further, and nobody is told."""
    value = encoded_words(normalize_module.MAX_HEADER_CHARS)
    raw = raw_mail(
        ("From", "Sample Stranger <someone@stranger.example>"),
        ("To", "trips@example.com"),
        ("Subject", "Your booking"),
        ("Date", email.utils.format_datetime(sent_at())),
        ("Message-ID", "<stranger-1@stranger.example>"),
        *[("X-Forwarded-For", value)] * 40,
    )
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        with bounded_memory(PARSE_BUDGET):
            res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert (res.status, model.calls, told.notices) == ("rejected", 0, [])
        row = ctx.ledger.messages.get(res.message_row_id)
        assert row["status"] == "rejected" and "not an allowed sender" in row["error"]


XFF_SECONDS = 5.0  # a bound far above what reading these headers as they stand takes


def test_twelve_hundred_forwarding_headers_at_the_limit_are_read_in_linear_time(
    tmp_path, monkeypatch
):
    """A stranger's message carrying 1,200 X-Forwarded-For headers, each of encoded words up to
    the header limit. Only a token that holds an `@` can name an address, so no other is parsed
    as one: the message is rejected well inside the time bound, read no further, and nobody is
    told."""
    value = encoded_words(normalize_module.MAX_HEADER_CHARS)
    raw = raw_mail(
        ("From", "Sample Stranger <someone@stranger.example>"),
        ("To", "trips@example.com"),
        ("Subject", "Your booking"),
        ("Date", email.utils.format_datetime(sent_at())),
        ("Message-ID", "<stranger-2@stranger.example>"),
        *[("X-Forwarded-For", value)] * 1200,
    )
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        with bounded_memory():
            t0 = time.perf_counter()
            res = runmod.process_raw(ctx, raw, source="imap", uid=1)
            took = time.perf_counter() - t0
        assert took < XFF_SECONDS
        assert (res.status, model.calls, told.notices) == ("rejected", 0, [])


def test_forty_stamps_at_the_limit_under_an_allowed_from_are_rejected_unread(
    tmp_path, monkeypatch
):
    """A message that claims the owner's address and carries forty Authentication-Results
    headers of encoded words up to the limit, each naming Gmail. Only the topmost is read, and it
    binds nothing, so the message is rejected inside the same bound, and nobody is told."""
    lead = "mx.google.com; "
    value = lead + encoded_words(normalize_module.MAX_HEADER_CHARS - len(lead))
    stamps = [("Authentication-Results", value)] * 40
    raw = raw_mail(*stamps, *owners_headers(1)[1:])
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        with bounded_memory(PARSE_BUDGET):
            res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert (res.status, model.calls, told.notices) == ("rejected", 0, [])
        assert "did not bind" in ctx.ledger.messages.get(res.message_row_id)["error"]


def test_an_address_list_past_the_limit_is_no_sender(tmp_path, monkeypatch):
    """`From: <the owner> (<a note>), <someone else>` under Gmail's stamp for the owner's domain:
    two mailboxes are no sender the check allows, so it is rejected. With a note past the limit,
    a header cut after its first mailbox would read as the owner's alone; an address header past
    the limit reads as no address, so it is rejected all the same."""
    other = "other@elsewhere.example"
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        for n, note in enumerate(("a note", past_the_limit()), 1):
            raw = raw_mail(*owners_headers(n, From=f"{OWNER} ({note}), {other}"))
            with bounded_memory(PARSE_BUDGET):
                res = runmod.process_raw(ctx, raw, source="imap", uid=n)
            nm = res.normalized
            assert (res.status, nm.from_addr, nm.sender_trusted) == ("rejected", "", False)
        assert (model.calls, told.notices) == (0, [])


def test_a_stamp_past_the_limit_vouches_for_nothing(tmp_path, monkeypatch):
    """The owner's address under Gmail's stamp for it, the stamp carried on past the limit: a
    header cut there would still read as a stamp that binds; a stamp past the limit reads as
    none, so the message is rejected."""
    padded = f"{STAMP} ({past_the_limit()})"
    raw = raw_mail(*owners_headers(1, Authentication_Results=padded))
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        with bounded_memory(PARSE_BUDGET):
            res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        assert (res.status, model.calls, told.notices) == ("rejected", 0, [])


def test_a_message_id_past_the_limit_is_none_and_the_mail_is_known_by_its_bytes(
    tmp_path, monkeypatch
):
    """Two of the owner's messages share a Message-ID past the limit. Cut, it would make the
    second a copy of the first; a Message-ID past the limit reads as none, so each is read, and
    only the very same bytes again are known for what they are."""
    long_id = f"<{'x' * 2 * normalize_module.MAX_HEADER_CHARS}@example.com>"
    first, second = (
        raw_mail(*owners_headers(1, Message_ID=long_id), body=f"Booking {n}.") for n in (1, 2)
    )
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        model.answers[sha(first)] = model.answers[sha(second)] = NOTHING
        with bounded_memory(PARSE_BUDGET):
            got = [
                runmod.process_raw(ctx, raw, source="imap", uid=n)
                for n, raw in enumerate((first, second, first), 1)
            ]
        assert [r.status for r in got] == ["ignored", "ignored", "skipped"]
        assert [r.same_as for r in got] == [None, None, None] and model.calls == 2
        assert got[0].normalized.message_id is None
        assert got[2].message_row_id == got[0].message_row_id


# --------------------------------------------------------------------------- attached emails
def carrying(inner: list[bytes], *, n: int, sent: dt.datetime) -> bytes:
    """The owner's message number `n`: a line of text, then each of `inner` attached as an email
    of its own."""
    edge = f"sample-carried-{n}"
    ctype = f'Content-Type: multipart/mixed; boundary="{edge}"\r\n'
    text = f"\r\n--{edge}\r\nContent-Type: text/plain\r\n\r\nThe emails are attached.\r\n"
    parts = [text.encode()]
    for one in inner:
        parts.append(f"--{edge}\r\nContent-Type: message/rfc822\r\n\r\n".encode() + one + b"\r\n")
    parts.append(f"--{edge}--\r\n".encode())
    return head("Fwd: Your bookings", n=n, sent=sent, more=ctype).encode() + b"".join(parts)


def nested(depth: int, *, sent: dt.datetime) -> bytes:
    """A chain of the owner's messages `depth` deep: each carries the next as an attached email,
    and the last is a plain booking."""
    raw = plain("Your booking", "Your booking for Alex Sample.", n=depth, sent=sent)
    for n in range(depth - 1, 0, -1):
        raw = carrying([raw], n=n, sent=sent)
    return raw


def siblings(count: int, *, sent: dt.datetime) -> bytes:
    """The owner's message carrying `count` attached emails side by side, each its own."""
    inner = [
        plain("Your booking", f"Booking {n} for Alex Sample.", n=1000 + n, sent=sent)
        for n in range(count)
    ]
    return carrying(inner, n=1, sent=sent)


READINGS_BOUND = 100  # far above what a bounded read of any message here takes


@pytest.mark.parametrize(
    ("build", "rows"),
    [
        # the chain hands back the email under the one the message reads, and stops there: the
        # one under it would take the emails handed back past the message's own size
        (lambda sent: nested(30, sent=sent), 2),
        (lambda sent: siblings(200, sent=sent), 1 + normalize_module.MAX_ATTACHED),
    ],
    ids=["nested-30-deep", "200-side-by-side"],
)
def test_attached_emails_are_read_a_bounded_number_of_times(tmp_path, monkeypatch, build, rows):
    """With no model, a chain of the owner's messages nested 30 deep, and one message carrying
    200 attached emails side by side: each is read a bounded number of times inside a time and a
    memory bound, at most MAX_ATTACHED attached emails holding at most the message's own size in
    all are handed back, only the message the mailbox holds files them, the poll counts no
    error, its cursor moves past it and the message after it is read. A second poll adds no
    row."""
    sent = sent_at()
    raw = build(sent)
    after = read_by_the_model()[0]
    readings = []
    real = runmod._process

    def counted(*args, **kw):
        readings.append(args[2])
        if len(readings) > READINGS_BOUND:
            raise AssertionError("past the bound of readings")
        return real(*args, **kw)

    monkeypatch.setattr(runmod, "_process", counted)
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        ctx.model = None
        inbox = FakeInbox(messages={1: raw, 2: after.raw}).new_to(ctx.ledger)
        with bounded_memory():
            t0 = time.perf_counter()
            summary = runmod.poll(ctx, inbox)
            took = time.perf_counter() - t0
        assert took < SECONDS
        assert (summary.errors, summary.fetched) == (0, 2)
        assert ctx.ledger.kv.get(KV_LAST_UID) == "2" and inbox.fetched == [1, 2]
        statuses = ctx.ledger.conn.execute("SELECT mail_uid, status FROM messages ORDER BY id")
        got = [tuple(r) for r in statuses]
        assert got == [(1, "waiting")] + [(None, "waiting")] * (rows - 1) + [(2, "waiting")]
        assert len(readings) == len(got) == len(set(readings))
        sizes = [len(Path(r["archive_path"]).read_bytes()) for r in ctx.ledger.messages.all()]
        assert sum(sizes[1:-1]) <= len(raw)
        with bounded_memory():
            runmod.poll(ctx, inbox)
        assert len(ctx.ledger.messages.all()) == len(got) and len(readings) == len(got)


def flights_attached(count: int, legs: int = 1) -> tuple[bytes, list[WireExtraction]]:
    """The owner's message carrying `count` attached emails, each booking the owner on `legs`
    flights on days of their own under a code of its own, and what the model reads in each: the
    message's own reading reads the first, and the others are read as messages of their own."""
    entry, flights = owners_flights()
    booked = flights.reservations[0]
    leg = booked.legs[0]
    sent = corpus.sent(entry)
    inner, answers = [], []
    for n in range(count):
        days = range(n * legs, (n + 1) * legs)
        moved = [
            leg.model_copy(
                update={"departure": later(leg.departure, d), "arrival": later(leg.arrival, d)}
            )
            for d in days
        ]
        # a booking of its own: its own code, so none is taken for a change of another
        own = booked.model_copy(update={"legs": moved, "code": f"{booked.code or 'SAMPLE'}{n}"})
        answers.append(flights.model_copy(update={"reservations": [own]}))
        inner.append(plain("Your flight", f"Flight {n} for Alex Sample.", n=100 + n, sent=sent))
    return carrying(inner, n=1, sent=sent), answers


def scripted(model: StandIn, ctx: runmod.Context, raw: bytes, answers: list[WireExtraction]):
    """The model answers the message's own reading with the first of `answers` and each email it
    hands back with the next."""
    s = ctx.settings
    nm = normalize_module.normalize(raw, s.allowed_senders, s.tz, require_auth=True)
    assert len(nm.extra_rfc822) == len(answers) - 1
    model.answers[sha(raw)] = answers[0]
    for extra, answer in zip(nm.extra_rfc822, answers[1:], strict=True):
        model.answers[sha(extra)] = answer


def test_a_poll_stopped_among_attached_emails_files_the_rest_next_time(tmp_path, monkeypatch):
    """The owner forwards three flight emails as attachments, and the poll is stopped while the
    model reads the second. The message is not finished until each email it carries is filed,
    so the next poll fetches it again: it reads nothing of its own again and asks the model
    nothing for it, files the two left, each once, and the message ends processed."""
    raw, answers = flights_attached(3)
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        scripted(model, ctx, raw, answers)

        def stopped():
            if model.calls == 2:
                raise SystemExit(1)

        model.reading = stopped
        inbox = FakeInbox(messages={1: raw}).new_to(ctx.ledger)
        with pytest.raises(SystemExit):
            runmod.poll(ctx, inbox)
        assert ctx.ledger.kv.get(KV_LAST_UID) == "0"
        model.reading = None
        summary = runmod.poll(ctx, inbox)
        assert (summary.errors, summary.counts) == (0, {"processed": 1})
        assert ctx.ledger.kv.get(KV_LAST_UID) == "1" and model.calls == 4
        rows = ctx.ledger.messages.all()
        assert [(r["mail_uid"], r["status"], r["error"]) for r in rows] == [
            (1, "processed", None),
            (None, "processed", None),
            (None, "processed", None),
        ]
        logged = [[x["outcome"] for x in ctx.ledger.items.by_message(r["id"])] for r in rows]
        assert logged == [["filed"]] * 3
        assert len(ctx.ledger.conn.execute("SELECT id FROM trip_items").fetchall()) == 3


def test_a_notice_that_raises_undoes_nothing_filed(tmp_path, monkeypatch):
    """Telling the owner breaks on the `filed` notice: the message is filed all the same, its
    bookings with it, and the poll counts no error."""
    entry, flights = owners_flights()
    raw = plain("Fwd: Your flights", "Your flights.", n=1, sent=corpus.sent(entry))

    class Breaks(Notifier):
        def notify(self, notice):
            if notice.event == "filed":
                raise RuntimeError("the notice could not be built")
            super().notify(notice)

    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        ctx.notifier = Breaks([told])
        model.answers[sha(raw)] = flights
        inbox = FakeInbox(messages={1: raw}).new_to(ctx.ledger)
        summary = runmod.poll(ctx, inbox)
        assert (summary.errors, summary.counts) == (0, {"processed": 1})
        (row,) = ctx.ledger.messages.all()
        assert row["status"] == "processed" and inbox.copies == [(1, "tripbox/processed")]
        filed = [x["outcome"] for x in ctx.ledger.items.by_message(row["id"])]
        assert filed and set(filed) == {"filed"}


def test_a_message_met_as_another_ones_attachment_stays_unfinished(tmp_path, monkeypatch):
    """A message whose own attached emails were not all filed (its poll was stopped among them)
    is met again as an email attached to another message. Read so, it files none of its own,
    so it is still unfinished afterwards: its next own reading files them."""
    sent = sent_at()
    inner = [
        plain("Your booking", f"Booking {n} for Alex Sample.", n=200 + n, sent=sent)
        for n in range(2)
    ]
    carrier = carrying(inner, n=2, sent=sent)
    note = plain("A note", "The emails are attached.", n=3, sent=sent)
    outer = carrying([note, carrier], n=1, sent=sent)
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        s = ctx.settings
        nm = normalize_module.normalize(outer, s.allowed_senders, s.tz, require_auth=True)
        met = nm.extra_rfc822[0]  # the carrier, as the other message hands it back
        model.answers[sha(outer)] = model.answers[sha(met)] = NOTHING

        def stopped():
            if model.calls == 2:
                raise SystemExit(1)

        model.reading = stopped
        with pytest.raises(SystemExit):
            runmod.process_raw(ctx, met, source="imap", uid=1)
        model.reading = None
        row = ctx.ledger.messages.by_sha(sha(met))
        assert (row["status"], row["error"]) == ("ignored", runmod.ATTACHED_UNFILED)
        res = runmod.process_raw(ctx, outer, source="imap", uid=2)
        assert res.children[0].message_row_id == row["id"]
        row = ctx.ledger.messages.get(row["id"])
        assert (row["status"], row["error"]) == ("ignored", runmod.ATTACHED_UNFILED)


def test_a_forward_of_eight_flight_emails_tells_within_one_budget(tmp_path, monkeypatch):
    """In `mailbox` mode the owner forwards eight flight emails as attachments, each booking two
    flights nobody vouched for, so each reading files two marked bookings. The message and its
    attached emails tell as one: FILED_NOTICES `filed` notices, the last saying how many more
    emails were attached, and CARD_LIMIT cards in all; a reading whose marked bookings did not
    all get a card ends its `filed` notice with how many more there are to check."""
    raw, answers = flights_attached(8, legs=2)
    with hostile(tmp_path, monkeypatch, TRIPBOX_MAIL_TRUST="mailbox") as (ctx, model, told, log):
        s = ctx.settings
        nm = normalize_module.normalize(raw, s.allowed_senders, s.tz, require_auth=False)
        model.answers[sha(raw)] = answers[0]
        for extra, answer in zip(nm.extra_rfc822, answers[1:], strict=True):
            model.answers[sha(extra)] = answer
        res = runmod.process_raw(ctx, raw, source="imap", uid=1)
        every = [res, *res.children]
        assert [r.status for r in every] == ["processed"] * 8 and log.requests == []
        assert all(o.checks == ("unverified_sender",) for r in every for o in r.outcomes)
        assert (runmod.FILED_NOTICES, runmod.CARD_LIMIT) == (5, 5)
        film = ["filed", "needs_look", "needs_look"] * 2 + ["filed", "needs_look", "filed"] * 1
        assert [n.event for n in told.notices] == [*film, "filed"]
        more = {
            2: ["and 1 more to check on the page"],
            3: ["and 2 more to check on the page"],
            4: ["and 2 more to check on the page", "and 3 more emails were attached; see the page"],
        }
        expected = []
        for k, r in enumerate(every[: runmod.FILED_NOTICES]):
            head = format_outcomes(
                runmod.shown(r.normalized.subject), r.outcomes, parser=r.parsed.parser,
                flight_log=True,
            )
            expected.append("\n".join([head, *more.get(k, [])]))
        assert told.of("filed") == expected
        carded = [o for r in every[:2] for o in r.outcomes] + every[2].outcomes[:1]
        assert told.of("needs_look") == [
            check_card(ctx.trips.item(o.trip_item_id), ctx.book) for o in carded
        ]


def test_an_attached_email_that_raises_fails_alone(tmp_path, monkeypatch):
    """An error filing one of the emails a message carries fails that email alone: the others
    are filed, the message is processed and the poll counts no error."""
    raw, answers = flights_attached(3)
    real = runmod._earlier_delivery

    def broken(ledger, message_id, row_id):
        if message_id == "<hostile-101@example.com>":
            raise RuntimeError("the ledger could not be read")
        return real(ledger, message_id, row_id)

    monkeypatch.setattr(runmod, "_earlier_delivery", broken)
    with hostile(tmp_path, monkeypatch, flight_log=False) as (ctx, model, told, _):
        scripted(model, ctx, raw, answers)
        inbox = FakeInbox(messages={1: raw}).new_to(ctx.ledger)
        summary = runmod.poll(ctx, inbox)
        assert (summary.errors, summary.counts) == (0, {"processed": 1})
        assert ctx.ledger.kv.get(KV_LAST_UID) == "1"
        rows = ctx.ledger.messages.all()
        assert [r["status"] for r in rows] == ["processed", "failed", "processed"]
        assert [r["error"] is None for r in rows] == [True, False, True]
