# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Per-message orchestration: archive, normalise, parse, validate, plan, execute, then one sync
pass with the flight log, and notify."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from tripbox import uploads
from tripbox.config import (
    OFFLINE_ENV,
    ConfigError,
    Settings,
    offline,
)
from tripbox.ledger import Ledger
from tripbox.log import redact
from tripbox.mail import imap as imapmod
from tripbox.mail.archive import archive, sha256_hex
from tripbox.mail.normalize import NormalizedMessage, normalize
from tripbox.notify.notifier import Notice, Notifier
from tripbox.notify.notifier import build as build_notifier
from tripbox.notify.text import (
    BUDGET_DAY,
    BUDGET_MONTH,
    MODEL_DOWN,
    MORE_EMAILS,
    MORE_TO_CHECK,
    booking_link,
    check_card,
    format_failure,
    format_outcomes,
)
from tripbox.parse import budget, provider
from tripbox.parse.models import Extraction
from tripbox.parse.pipeline import ParsedMessage, from_text, parse_message
from tripbox.parse.provider import READ_BY_MODEL, Provider, Usage
from tripbox.parse.replay import RecordingExtractor, ReplayExtractor
from tripbox.pipeline.shift import DateShift
from tripbox.trips import display
from tripbox.trips import format as fmt
from tripbox.trips.store import DRY_RUN_KV, TripItem, TripStore
from tripbox.uploads import looks_like_email
from tripbox.util import now, now_iso, parse_iso
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import Item, validate_extraction
from tripbox.write.airtrail import AirTrailClient, AirTrailRefs, MemoryAirTrail
from tripbox.write.executor import Executor, Outcome
from tripbox.write.lifecycle import note_address, note_airtrail
from tripbox.write.planner import plan
from tripbox.write.sync import PassReport, Syncer

log = logging.getLogger("tripbox.run")

# Where a message the owner handed in came from: a paste to the bot, `tripbox ingest`, the page.
# A message whose row names one of these is the owner's own input on every reading: the sender
# check is never asked of it. Of these rows, one with no sender recorded (`from_addr` NULL) was
# read as pasted text, and is read as text again; an email reading always records one (empty
# when the email names none).
HANDED_IN = ("telegram", "cli", "upload")
# What `messages.origin_ok` keeps of the sender check's verdict: rejected; accepted with Gmail's
# authentication, or handed in by the owner; accepted with nothing authenticated (`mailbox` mode,
# or an email attached to such a message); or the owner's by their vouching: an email attached to
# their own authenticated message, whatever its own From says. A later reading asks the check of
# no acceptance again; the sender of one accepted unauthenticated is nobody's on every reading, in
# either mode, until the owner hands the bytes in; one the owner vouched for is theirs on every
# reading, its row read on its own too.
ORIGIN_REJECTED, ORIGIN_ACCEPTED, ORIGIN_UNAUTHENTICATED, ORIGIN_OWNERS = 0, 1, 2, 3
# A second delivery of an email (same Message-ID) is skipped when the first one reached one of
# these; a copy that was rejected or failed is processed again. A message read again after it
# reached one keeps its findings dated as its first reading dated them.
DONE_STATUSES = ("processed", "ignored")
# The `error` of a message whose own reading reached one of DONE_STATUSES while the emails
# attached to it are still being filed: the message is not finished until they are, and the
# next time its bytes come (the poll was stopped, the mailbox hands it over again), or the next
# resume pass takes it up, it files them, reading nothing of its own again.
ATTACHED_UNFILED = "attached_unfiled"
# What the owner is told, once, of an email attached to a message whose filing broke before
# anything recorded it: the message keeps it, unfinished, for the resume pass.
ATTACHED_NOT_FILED = (
    "tripbox could not file an email attached to a message. It is kept and read again."
)
# A message the resume pass took up this many times for its attached emails without finishing
# is failed, told once (a `failure` notice, ATTACHED_GIVEN_UP); the tries are counted by message
# id in the `kv` row KV_RESUME_TRIES, and a message leaves the count once finished or failed.
MAX_RESUMES = 5
KV_RESUME_TRIES = "resume_tries"
ATTACHED_GIVEN_UP = (
    f"an email attached to it could not be filed after {MAX_RESUMES} tries, so it is no longer"
    " tried; add its booking by hand"
)
# Why a message no reader read whole is `waiting` (its `error`): no model is built, the model's
# cap is reached, or the model cannot be reached. It is unlabelled in the mailbox, and the resume
# pass of every poll reads it again once the reason is gone (`resume_waiting`).
WAIT_REASONS = ("no_model", "over_budget", "model_down")
# The `error` of a message the resume pass found with no archived email to read again (a backup
# restored without its mail): it is finished `failed`, told once.
ARCHIVE_MISSING = "archived email missing"
# The period the last notice that the cap is reached was sent for (`budget.Budget.period`), and
# when the model was last found unreachable, until it answers again: each is told once.
KV_BUDGET_ALERTED = "budget_alerted"
KV_MODEL_ALERTED = "model_alerted"
# How many characters of an email's subject, or of any other text from its headers, a log line
# or a notice shows before it is cut with `…`, once each secret in it is masked (a secret across
# the cut leaves nothing of itself). What the ledger stores is not cut here.
SUBJECT_SHOWN = 200
# What one mailed message tells, its attached emails' readings included: at most FILED_NOTICES
# `filed` notices (one per reading that filed something; the last then says how many more
# emails were attached), each showing at most FILED_LINES bookings, and at most CARD_LIMIT cards
# in all (a reading whose marked bookings did not all get one says how many more to check at
# the end of its `filed` notice).
FILED_NOTICES, FILED_LINES, CARD_LIMIT = 5, 12, 5


def shown(text: str | None) -> str:
    """Text from an email's headers as a log line or a notice shows it (SUBJECT_SHOWN)."""
    text = redact(text or "")
    return text if len(text) <= SUBJECT_SHOWN else text[:SUBJECT_SHOWN] + "…"


@dataclass
class Context:
    settings: Settings
    ledger: Ledger
    book: PassengerBook
    refdata: Airports  # the bundled airport table, in every mode
    model: Provider | ReplayExtractor | RecordingExtractor | None
    airtrail: AirTrailClient | None
    trips: TripStore
    executor: Executor
    notifier: Notifier  # with no channel a notice goes nowhere
    airtrail_refs: AirTrailRefs | None = None  # AirTrail's own ids, looked up when written
    # Moves every date an email gives by whole days once the extractors have read it (the demo);
    # None files what the email says.
    shift: DateShift | None = None
    reread_warned: bool = False  # the "--reread needs a model" line is logged once per run
    # Keeps the flight log in step with the trip store; None when no flight log is kept.
    syncer: Syncer | None = None


# The rehearsal's answer switches, read from the environment by build_context (with
# TRIPBOX_NO_NETWORK, see config.offline). Unset, they change nothing.
REPLAY_ENV = "TRIPBOX_REPLAY_DIR"  # answer from recorded model answers instead of the model
RECORD_ENV = "TRIPBOX_RECORD_DIR"  # store the model's answers as they arrive


def _model(
    settings: Settings, ledger: Ledger, with_model: bool, no_network: bool = False
) -> Provider | ReplayExtractor | RecordingExtractor | None:
    if not with_model:
        return None
    replay, record = os.environ.get(REPLAY_ENV), os.environ.get(RECORD_ENV)
    if replay and record:
        raise ConfigError(f"{REPLAY_ENV} and {RECORD_ENV} cannot both be set")
    if replay:
        return ReplayExtractor(Path(replay), settings.llm_model)  # never builds a client
    if no_network and not record:
        # offline, the model is reached only to record answers; a live extractor would call out
        raise ConfigError(f"{OFFLINE_ENV} is set: the model needs {REPLAY_ENV} or {RECORD_ENV}")
    model = provider.build(settings, ledger)  # None while the model is not on
    if model is None:
        return None
    return RecordingExtractor(model, Path(record)) if record else model


def build_context(
    settings: Settings,
    *,
    ledger: Ledger | None = None,
    with_model: bool = True,
    with_notifier: bool = True,
    dry_run_from_flag: bool = False,
) -> Context:
    """`dry_run_from_flag` says that the settings' dry run is on only because this one run was
    started with `--dry-run`: the run sends nothing to the flight log, and the ledger's note of
    the setting (DRY_RUN_KV) stays as configured. Without `with_notifier` the run tells nobody
    anything: its notifier has no channel."""
    ledger = ledger or Ledger.open(settings.ledger_path)
    book = settings.travellers
    no_network = offline()
    # a feature that is off or half configured builds no client (cli says so at start)
    with_airtrail = settings.on("airtrail")
    if with_airtrail:
        # the ids the reference cache holds are one AirTrail's own: at another address they are
        # dropped, before anything asks the cache
        note_address(ledger, settings.airtrail_url)
    if no_network:
        # what would be networked stays in the process: flights go to an AirTrail held in memory
        # (only when one is configured, as live), whose ids come from the ledger's reference cache
        airtrail: AirTrailClient | None = MemoryAirTrail(ledger) if with_airtrail else None
    else:
        airtrail = (
            AirTrailClient(settings.airtrail_url, settings.airtrail_api_key)
            if with_airtrail
            else None
        )
    # validation reads airports from the bundled table; AirTrail is asked only for its own ids,
    # when a flight is written (airtrail_refs)
    refdata = Airports()
    airtrail_refs = AirTrailRefs(airtrail, ledger) if airtrail is not None else None
    model = _model(settings, ledger, with_model, no_network)
    trips = TripStore(ledger, settings.home_airports, book=book, airports=refdata)
    # whether a flight log is kept reaches the page through the ledger, never a setting; a
    # change is an event that settles every flight (a half-configured or invalid AirTrail is off)
    today = now().astimezone(ZoneInfo(settings.tz)).date()
    note_airtrail(trips, ledger, on=with_airtrail, today=today)
    held_back = "on" if settings.airtrail_dry_run and not dry_run_from_flag else "off"
    if ledger.kv.get(DRY_RUN_KV) != held_back:
        ledger.kv.set(DRY_RUN_KV, held_back)
    notifier = build_notifier(settings, offline=no_network) if with_notifier else Notifier()
    executor = Executor(ledger=ledger, store=trips, book=book)
    syncer = (
        Syncer(
            store=trips,
            ledger=ledger,
            book=trips.book,  # the store's own, so what is told equals what settle works out
            client=airtrail,
            refs=airtrail_refs,
            dry_run=settings.airtrail_dry_run,
            alert=_sync_alert(notifier),
        )
        if airtrail is not None
        else None
    )
    return Context(
        settings,
        ledger,
        book,
        refdata,
        model,
        airtrail,
        trips,
        executor,
        notifier,
        airtrail_refs,
        syncer=syncer,
    )


def _sync_alert(notifier: Notifier) -> Callable[[str], None] | None:
    """What the flight log's reconcile tells the owner goes out as a `sync_error` notice; with no
    channel there is nobody to tell."""
    if not notifier.channels:
        return None
    return lambda text: notifier.notify(Notice("sync_error", text))


@dataclass
class MessageResult:
    message_row_id: int | None
    status: str  # processed | ignored | rejected | failed | skipped | waiting
    # the mailbox label; None leaves the mailbox copy unlabelled: it waits, or it was skipped
    # (what the ledger holds was filed when it was read)
    label: str | None
    outcomes: list[Outcome] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    parsed: ParsedMessage | None = None
    normalized: NormalizedMessage | None = None
    skipped_reason: str | None = None
    children: list[MessageResult] = field(default_factory=list)  # further attached emails
    # the earlier delivery of the same email (Message-ID) this one was set aside for
    same_as: int | None = None
    wait_reason: str | None = None  # why it waits (WAIT_REASONS)


LABELS = {
    "processed": imapmod.LABEL_PROCESSED,
    "ignored": imapmod.LABEL_PROCESSED,
    "rejected": imapmod.LABEL_REJECTED,
    "failed": imapmod.LABEL_FAILED,
}


def sync(ctx: Context) -> PassReport | None:
    """One pass of the reconcile with the flight log, as every message ends with one: each
    flight that is due is sent. None when no flight log is kept, and when the pass broke: its
    error is logged here and goes no further, so it never undoes or fails a message that was
    filed (what each booking's sync came to is read from the store afterwards)."""
    if ctx.syncer is None:
        return None
    try:
        return ctx.syncer.run_pass()
    except Exception:  # noqa: BLE001 - the bookings are filed; the next pass tries again
        log.exception("the sync pass after a message failed")
        return None


def _settled(ctx: Context, result: MessageResult, notify: bool) -> None:
    """Once a message and the emails attached to it are filed: one sync pass, then each
    outcome's sync state and checks as the store now holds them, an upload each reading's
    message ended as with nothing filed, then the notifications, in the owner's style
    (`trips.display`)."""
    sync(ctx)
    readings: list[MessageResult] = []
    todo = [result]
    while todo:
        res = todo.pop(0)
        todo.extend(res.children)
        for o in res.outcomes:
            ti = ctx.trips.item(o.trip_item_id) if o.trip_item_id is not None else None
            if ti is not None:
                o.sync, o.checks = ti.sync_state, ti.checks
        came = uploads.came_to(ctx.ledger, res) if res.message_row_id is not None else None
        if came is not None:
            # an upload this message ended as with nothing filed (it waited for a model, say)
            # says what this later reading came to, by the rule its first reading followed
            ctx.ledger.uploads.read_again(res.message_row_id, *came)
        readings.append(res)
    if notify and ctx.notifier.channels:
        try:
            with fmt.styled(display.read(ctx.ledger)):
                _notify(ctx, readings)
        except Exception:  # noqa: BLE001 - the bookings are filed: the status stands
            log.exception("message #%s: notification failed", result.message_row_id)


def _needs_reprocess(row: sqlite3.Row, force: bool) -> bool:
    """Whether a message the ledger knows is read again: when forced, when it was never
    finished (`archived`: it stopped half-way; or ATTACHED_UNFILED), when it waits for the model
    (`waiting`) and when it failed. Reading it again asks no model that answered before (the
    answer is stored with the message), and a booking it already filed is found again as a
    duplicate."""
    return (
        force
        or row["status"] in ("archived", "waiting", "failed")
        or row["error"] == ATTACHED_UNFILED
    )


def _reopen(ledger: Ledger, row_id: int, **fields: object) -> None:
    """Prepare an already-known message for another reading, in one transaction: its rows in
    the log go, for the new reading to write anew, and it is `archived` again, with `fields`
    set; one that waits for the model keeps waiting, with its reason, until the reading ends,
    so a reading cut short (a poll stopped while the model reads) leaves it for the next resume
    pass. The answer stored with the message stays."""
    with ledger.transaction():
        gone = ledger.items.delete_for_message(row_id)
        row = ledger.messages.get(row_id)
        if row is not None and row["status"] == "waiting":
            ledger.messages.update(row_id, **fields)
        else:
            ledger.messages.update(row_id, status="archived", error=None, **fields)
    if gone:
        log.info("message #%s: replaced %d log row(s)", row_id, len(gone))


def _earlier_delivery(ledger: Ledger, message_id: str | None, row_id: int) -> sqlite3.Row | None:
    """Another copy of the same email that was actually handled (filter forward + manual forward).

    A copy that was itself set aside by this check (`ignored` without ever being parsed) does not
    count — otherwise reading the original again would defer to its own shadow.
    """
    if not message_id:
        return None
    for row in ledger.messages.by_message_id(message_id):
        if row["id"] == row_id or row["status"] not in DONE_STATUSES:
            continue
        if row["status"] == "ignored" and row["parser"] is None:
            continue  # deferred to another copy, never parsed itself
        return row
    return None


def _alert(ctx: Context, notify: bool, subject: str, error: str) -> None:
    if notify:
        ctx.notifier.notify(Notice("failure", format_failure(shown(subject), error)))


def _wait_reason(ctx: Context, parsed: ParsedMessage) -> str | None:
    """Why a message no reader read whole (the extractor read nothing of it, or only part)
    waits (WAIT_REASONS), or None when it does not: the model's cap is reached or the model
    cannot be reached (`provider.WAITS`), or no model is built. Any other failure of the model
    fails the message."""
    if parsed.parser != "none":
        return None
    if parsed.error_kind == "over_budget":
        return "over_budget"
    if parsed.error_kind == "unavailable":
        return "model_down"
    if parsed.error is None and ctx.model is None:
        return "no_model"
    return None


def _tell_why_mail_waits(ctx: Context, reason: str, parsed: ParsedMessage) -> None:
    """One notice a period that the model's cap is reached (`budget_reached`), and one an
    outage that the model cannot be reached (a `failure`, its word saying how), until it
    answers again; mail waiting for a model to be set up is told nothing. With no channel there
    is nobody to tell, and nothing is noted."""
    led = ctx.ledger
    if not ctx.notifier.channels or reason == "no_model":
        return
    if reason == "over_budget":
        cap = budget.Budget(led, ctx.settings)
        period = cap.period()
        if led.kv.get(KV_BUDGET_ALERTED) == period:
            return
        s = ctx.settings
        if cap.by_month:
            text = BUDGET_MONTH.format(budget=s.llm_monthly_budget_usd)
        else:
            text = BUDGET_DAY.format(n=s.llm_max_calls_per_day)
        led.kv.set(KV_BUDGET_ALERTED, period)
        ctx.notifier.notify(Notice("budget_reached", text))
        return
    if led.kv.get(KV_MODEL_ALERTED):
        return
    led.kv.set(KV_MODEL_ALERTED, now_iso())
    ctx.notifier.notify(Notice("failure", MODEL_DOWN.format(word=parsed.error_word or "outage")))


def process_raw(
    ctx: Context,
    raw: bytes,
    *,
    source: str,
    uid: int | None = None,
    received: dt.datetime | None = None,
    force: bool = False,
    notify: bool = True,
    trusted: bool = False,
    reread: bool = False,
) -> MessageResult:
    """One message through the pipeline: it and every email attached to it are filed, then the
    flight log is brought in step once (`sync`), each outcome's sync state and checks are read
    again, and the owner is told. `trusted`: the owner handed these bytes in (an upload, an
    ingest), so the message is read whoever sent it and vouched for as the owner's own; one the
    sender check once rejected as mail is read now, and is the owner's from then on. A message
    the ledger holds as handed in (`HANDED_IN`) is read so on every reading, whoever calls. Mail
    the sender check accepted once is not checked again, but whether its sender is vouched for
    is worked out from its headers on every reading, as on the first; one accepted with nothing
    authenticated (`ORIGIN_UNAUTHENTICATED`) is nobody's on every reading, in either mode."""
    result = _file_raw(
        ctx,
        raw,
        source=source,
        uid=uid,
        received=received,
        force=force,
        notify=notify,
        trusted=trusted,
        reread=reread,
        own=trusted,
    )
    _settled(ctx, result, notify)
    return result


def _file_raw(
    ctx: Context,
    raw: bytes,
    *,
    source: str,
    uid: int | None,
    received: dt.datetime | None,
    force: bool,
    notify: bool,
    trusted: bool,
    reread: bool,
    own: bool = False,
    ungated: bool = False,
    attached: bool = False,
) -> MessageResult:
    """process_raw's filing: the message, then each further email attached to it. `own`: the
    caller handed these very bytes in (never an email attached to them); so it is when the
    ledger holds them as handed in. `trusted`: the owner vouches for them (their own bytes, or an
    email attached to their own message). `ungated`: an email attached to a message from an
    allowed sender that nothing authenticated (`mailbox` mode), read but vouched for by nobody.
    None of these is asked the sender check, nor is a message the check accepted before
    (`origin_ok`); only `own` and `trusted` make its sender the owner's. `attached`: an email
    attached to the message being filed, which files none of the emails attached to it (the
    message's own walk found them all)."""
    led = ctx.ledger
    sha = sha256_hex(raw)
    received = received or now()
    existing = led.messages.by_sha(sha)
    if existing is not None:
        if existing["source"] in HANDED_IN:
            # the owner handed these bytes in: they are theirs on every reading
            own = True
        # a replay of something the owner handed in (a paste, a CLI ingest, an upload): what was
        # read as pasted text goes back through the text path (text with no headers, or a paste
        # that only began like an email)
        if existing["source"] in HANDED_IN and (
            not looks_like_email(raw) or existing["from_addr"] is None
        ):
            return _file_text(
                ctx,
                raw.decode("utf-8", errors="replace"),
                source=existing["source"],
                subject=existing["subject"] or "(pasted itinerary)",
                notify=notify,
                force=force,
                reread=reread,
            )
    # what was accepted once (origin_ok) is accepted again, the bytes being the same; that says
    # nothing of who sent them, which the headers tell on every reading, unless nothing
    # authenticated that acceptance
    accepted = existing is not None and bool(existing["origin_ok"])
    # an email the owner vouched for when it came attached to their message stays theirs
    if existing is not None and existing["origin_ok"] == ORIGIN_OWNERS:
        trusted = True
    # what the sender check rejected as mail, the owner now hands in as their own: it is read
    rejected_once = own and existing is not None and existing["status"] == "rejected"
    if existing is not None and not (_needs_reprocess(existing, force) or rejected_once):
        return MessageResult(
            existing["id"],
            "skipped",
            None,
            skipped_reason=f"already {existing['status']} (message #{existing['id']})",
        )
    # its own reading is done and the emails attached to it are not all filed: only they are
    unfiled = (
        existing is not None
        and not force
        and existing["status"] in DONE_STATUSES
        and existing["error"] == ATTACHED_UNFILED
    )
    if existing is None:
        arch = archive(ctx.settings.archive_dir, raw, received, uid)
        row_id = led.messages.insert(
            sha256=sha,
            source=source,
            status="archived",
            mail_uid=uid,
            received_at=received.isoformat(timespec="seconds"),
            archive_path=str(arch.path),
            # read as an email from the start: a replay never takes it for pasted text
            from_addr="" if own else None,
            # read ungated, it is nobody's from the start: a reading that stops before its
            # verdict is written leaves it so, never to be asked the check of its own headers
            origin_ok=ORIGIN_UNAUTHENTICATED if ungated else ORIGIN_REJECTED,
        )
    else:
        # identical bytes are already in the archive (a replay reads them from there): reuse the
        # row and its file instead of writing a second copy under today's timestamp. Bytes the
        # check rejected as mail that the owner hands in become theirs (its source says so)
        row_id = existing["id"]
        if not unfiled:
            _reopen(led, row_id, **({"source": source} if rejected_once else {}))
    found_at = _found_at(existing)
    try:
        nm = normalize(
            raw,
            ctx.settings.allowed_senders,
            ctx.settings.tz,
            require_auth=ctx.settings.require_google_auth,
            trusted_domains=ctx.settings.mail_trusted_sender_domains,
            gate=not (own or trusted or ungated or accepted),
        )
    except Exception as exc:  # noqa: BLE001
        led.messages.finish(row_id, "failed", error=f"normalize: {exc}")
        log.exception("message #%s: normalize failed", row_id)
        _alert(ctx, notify, f"message #{row_id}", f"normalize: {exc}")
        return MessageResult(row_id, "failed", LABELS["failed"])
    # whether what lets these bytes in vouches for anyone: the owner (their own bytes, or an
    # email attached to their own); the acceptance these bytes were first given, when they were;
    # else the check as this mode asks it now, never for an email read ungated
    if own or trusted:
        vouched = True
    elif accepted and existing is not None:
        vouched = existing["origin_ok"] != ORIGIN_UNAUTHENTICATED
    else:
        vouched = not ungated and ctx.settings.require_google_auth
    if trusted or own:
        nm.sender_trusted = True  # the owner handed it in (CLI, paste, their own attachment)
    elif not vouched:
        nm.sender_trusted = False  # nothing authenticated it: nobody's, in either mode
    # what the message's `error` holds once every email attached to it is filed: why it was set
    # aside, when it was for an earlier delivery of the same email
    done_error: str | None = None
    if unfiled:
        status = existing["status"]
        result = MessageResult(row_id, status, LABELS[status], normalized=nm)
        if status == "ignored" and existing["parser"] is None:
            # its own reading set it aside for an earlier delivery: it still is
            earlier = _earlier_delivery(led, nm.message_id, row_id)
            if earlier is not None:
                result.same_as = earlier["id"]
                result.skipped_reason = done_error = _same_email(earlier)
    else:
        led.messages.update(
            row_id,
            message_id=nm.message_id,
            subject=nm.subject,
            from_addr=nm.from_addr,
            email_date=nm.email_date.isoformat() if nm.email_date else None,
            origin_ok=(
                ORIGIN_REJECTED
                if not nm.origin_ok
                else ORIGIN_OWNERS
                if trusted and not own
                else ORIGIN_ACCEPTED
                if vouched
                else ORIGIN_UNAUTHENTICATED
            ),
        )
        if not nm.origin_ok:
            led.messages.finish(row_id, "rejected", error=nm.origin_reason)
            log.info(
                "rejected uid=%s from=%s: %s", uid, shown(nm.from_addr), shown(nm.origin_reason)
            )
            return MessageResult(row_id, "rejected", LABELS["rejected"], normalized=nm)
        # with emails attached, a reading that is done waits for them before it is finished
        hold = bool(nm.extra_rfc822) and not attached
        earlier = None if force else _earlier_delivery(led, nm.message_id, row_id)
        if earlier is not None:
            # the email it reads as its own was handled before; those attached beside it are
            # still filed, below
            why = done_error = _same_email(earlier)
            led.messages.finish(row_id, "ignored", error=ATTACHED_UNFILED if hold else why)
            log.info("message #%s ignored: %s", row_id, why)
            result = MessageResult(
                row_id,
                "ignored",
                LABELS["ignored"],
                normalized=nm,
                skipped_reason=why,
                same_as=earlier["id"],
            )
        else:
            result = _process(
                ctx,
                nm,
                row_id,
                notify=notify,
                message_hash=sha,
                reread=reread,
                found_at=found_at,
                hold=hold,
            )
    # "Forward as attachment" of several emails at once: normalize hands the rest back only when
    # the outer From is an allowed sender, and each is read whatever its own From says. The
    # owner's trust carries over to them when the owner handed the message in, or when Gmail
    # authenticated it and this mode asks Gmail; else (`mailbox` mode, whatever the message's
    # acceptance said) they are read as nobody's. Each is filed on its own: one that raises
    # fails alone
    carried = nm.forwarded_by_owner and (
        own or trusted or (vouched and ctx.settings.require_google_auth)
    )
    left = False  # an attached email was not filed and has no row: the message keeps it
    for extra in [] if attached else nm.extra_rfc822:
        try:
            child = _file_raw(
                ctx,
                extra,
                source=source,
                uid=None,
                received=received,
                force=force,
                notify=notify,
                trusted=carried,
                reread=reread,
                ungated=nm.forwarded_by_owner and not carried,
                attached=True,
            )
        except Exception as exc:  # noqa: BLE001 - the emails beside it are still filed
            log.exception("message #%s: an email attached to it could not be filed", row_id)
            child = _attached_failed(ctx, extra, exc)
            if child.message_row_id is None:
                # nothing records it but the message, which stays unfinished so the resume
                # pass reads it again; said once, on the reading that first leaves it so
                left = True
                if notify and not unfiled:
                    ctx.notifier.notify(Notice("failure", ATTACHED_NOT_FILED))
        result.children.append(child)
    # finished once every email it carries is filed; met as another's attachment, it filed none
    if (
        not attached
        and not left
        and result.status in DONE_STATUSES
        and led.messages.get(row_id)["error"] == ATTACHED_UNFILED
    ):
        led.messages.update(row_id, error=done_error)
    return result


def _same_email(earlier: sqlite3.Row) -> str:
    """Why a message is set aside for `earlier`, a delivery of the same email handled before."""
    return f"same email as message #{earlier['id']} ({earlier['status']}), Message-ID match"


def _attached_failed(ctx: Context, raw: bytes, exc: Exception) -> MessageResult:
    """An email attached to a message whose filing raised: `failed`, when it has a row by then."""
    row = ctx.ledger.messages.by_sha(sha256_hex(raw))
    if row is None:
        return MessageResult(None, "failed", LABELS["failed"])
    ctx.ledger.messages.finish(row["id"], "failed", error=str(exc)[:1000])
    return MessageResult(row["id"], "failed", LABELS["failed"])


def process_text(
    ctx: Context,
    text: str,
    *,
    source: str,
    subject: str = "(pasted itinerary)",
    notify: bool = True,
    force: bool = False,
    reread: bool = False,
) -> MessageResult:
    """Pasted text through the pipeline, as `process_raw` takes an email."""
    result = _file_text(
        ctx, text, source=source, subject=subject, notify=notify, force=force, reread=reread
    )
    _settled(ctx, result, notify)
    return result


def _file_text(
    ctx: Context,
    text: str,
    *,
    source: str,
    subject: str,
    notify: bool,
    force: bool,
    reread: bool,
) -> MessageResult:
    raw = text.encode("utf-8")
    led = ctx.ledger
    sha = sha256_hex(raw)
    existing = led.messages.by_sha(sha)
    if existing is not None and not _needs_reprocess(existing, force):
        return MessageResult(
            existing["id"],
            "skipped",
            None,
            skipped_reason=f"identical text seen before (message #{existing['id']})",
        )
    if existing is None:
        arch = archive(ctx.settings.archive_dir, raw, now(), None)
        row_id = led.messages.insert(
            sha256=sha,
            source=source,
            status="archived",
            subject=subject,
            received_at=now_iso(),
            archive_path=str(arch.path),
            origin_ok=True,
        )
    else:  # a paste that failed, or one handed in again by force
        row_id = existing["id"]
        # read as text now: nothing an earlier reading as an email recorded of a sender, a
        # Message-ID or a Date stands (and a replay reads it as text again)
        _reopen(led, row_id, from_addr=None, message_id=None, email_date=None, subject=subject)
    nm = from_text(text, now=now(), subject=subject, from_addr=source)
    return _process(
        ctx,
        nm,
        row_id,
        notify=notify,
        message_hash=sha,
        reread=reread,
        found_at=_found_at(existing),
    )


def _found_at(earlier: sqlite3.Row | None) -> str | None:
    """When the findings of a message read again are dated: the time its bookings first reached
    the ledger in a reading that ended in one of DONE_STATUSES (`messages.found_at`), so a later
    reading never reopens what a look closed since; None when none did (it is read for the first
    time, or its earlier readings waited for the model, stopped half-way, failed, were refused,
    or were set aside unread for a copy of it), and the reading that files them dates them
    (`_dated`)."""
    return earlier["found_at"] if earlier is not None else None


def _dated(received_at: str | None) -> str:
    """When the findings of a message whose bookings first reach the ledger now are dated: the
    later of when it was received and now, once the extractors have answered, so a look made
    while the model was reading never closes what it did not show. Never the email's own Date,
    which the sender writes."""
    clock = now_iso()
    if not received_at:
        return clock
    try:
        return received_at if parse_iso(received_at) >= parse_iso(clock) else clock
    except ValueError:
        return clock


class StoredAnswer:
    """The model's earlier answer for a message, kept with its row: it answers at no cost, so a
    reading again gives the same bookings in the same order and asks no model."""

    model = "stored"

    def __init__(self, answer_json: str) -> None:
        self.extraction = Extraction.model_validate_json(answer_json)

    def extract(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[Extraction, Usage]:
        return self.extraction.model_copy(deep=True), Usage(self.model, 0, 0, 0.0, asked=False)


def _stored_answer(ctx: Context, row_id: int) -> StoredAnswer | None:
    text = ctx.ledger.messages.answer(row_id)
    if not text:
        return None
    try:
        return StoredAnswer(text)
    except ValueError:  # an answer that no longer fits the shape is asked for again
        log.warning("message #%s: the stored answer is unreadable; asking the model", row_id)
        return None


def _process(
    ctx: Context,
    nm: NormalizedMessage,
    row_id: int,
    *,
    notify: bool,
    message_hash: str,
    reread: bool = False,
    found_at: str | None = None,
    hold: bool = False,
) -> MessageResult:
    """`message_hash` is the SHA-256 hex digest of the message's bytes: it names the message in
    every booking's payload, and in the draft key of a booking that cannot be keyed yet.
    `found_at` is when an earlier reading dated its findings (`_found_at`); None: this reading
    dates them, once the extractors have answered (`_dated`). `hold`: a reading that ends in one
    of DONE_STATUSES leaves the message not finished (ATTACHED_UNFILED) for its caller to finish
    once the emails attached to it are filed."""
    led = ctx.ledger
    held = {"error": ATTACHED_UNFILED} if hold else {}
    try:
        if reread and ctx.model is None:
            # there is no model to ask: the stored answer stands, so a read booking never turns
            # into mail that waits
            if not ctx.reread_warned:
                log.warning("--reread needs a model; the stored answers were used")
                ctx.reread_warned = True
            reread = False
        stored = None if reread else _stored_answer(ctx, row_id)
        parsed = parse_message(nm, stored or ctx.model)
        if stored is None and parsed.parser == READ_BY_MODEL and parsed.usage:
            # the answer as read: kept before the demo's date shift moves it
            led.messages.set_answer(row_id, parsed.extraction.model_dump_json())
        if ctx.shift is not None:
            # the extractors read the email as written; everything from here reads it moved
            ctx.shift.apply(nm, parsed.extraction)
            led.messages.update(
                row_id, email_date=nm.email_date.isoformat() if nm.email_date else None
            )
        if parsed.usage and parsed.usage.asked and stored is None:
            # a call the model was asked for: what the cap counts (an answer read back is not;
            # one that failed counts when the model was billed for it); an answer ends an outage
            led.llm.add(
                message_id=row_id,
                model=parsed.usage.model,
                input_tokens=parsed.usage.input_tokens,
                output_tokens=parsed.usage.output_tokens,
                cost_usd=parsed.usage.cost_usd,
            )
            if parsed.parser == READ_BY_MODEL and led.kv.get(KV_MODEL_ALERTED):
                led.kv.set(KV_MODEL_ALERTED, "")
        ex = parsed.extraction
        real = [r for r in ex.reservations if r.kind != "other"]
        # no reader read it whole (`parser` none: the extractor read nothing, or only part of
        # it, and the model gave no answer): what the extractor read in part is never filed
        if parsed.parser == "none":
            reason = _wait_reason(ctx, parsed)
            if reason is not None:
                # only the model can read it: the message is not finished but waits,
                # unlabelled, until the resume pass finds the reason gone
                led.messages.update(row_id, status="waiting", error=reason)
                log.info("message #%s waits (%s): '%s'", row_id, reason, shown(nm.subject))
                if notify:
                    _tell_why_mail_waits(ctx, reason, parsed)
                return MessageResult(
                    row_id, "waiting", None, parsed=parsed, normalized=nm, wait_reason=reason
                )
        if not real or parsed.parser == "none":
            if parsed.parser == "none" and parsed.error:
                led.messages.finish(
                    row_id,
                    "failed",
                    parser=parsed.parser,
                    document_type=ex.document_type,
                    error=parsed.error,
                )
                _alert(ctx, notify, nm.subject, parsed.error)
                return MessageResult(
                    row_id, "failed", LABELS["failed"], parsed=parsed, normalized=nm
                )
            led.messages.finish(
                row_id, "ignored", parser=parsed.parser, document_type=ex.document_type, **held
            )
            log.info("ignored '%s': %s with no reservations", shown(nm.subject), ex.document_type)
            return MessageResult(row_id, "ignored", LABELS["ignored"], parsed=parsed, normalized=nm)
        source = {
            "message_id": nm.message_id,
            "email_date": nm.email_date.isoformat() if nm.email_date else None,
            "subject": nm.subject,
            "from": nm.from_addr,
            "message_hash": message_hash,
        }
        items = validate_extraction(
            ex,
            refdata=ctx.refdata,
            book=ctx.book,
            email_date=nm.email_date,
            source=source,
            parser=parsed.parser,
            trusted_sender=nm.sender_trusted,
            text=nm.text,
        )
        # a finding is dated by when its message's bookings first reach the ledger: as an
        # earlier reading that filed them dated it (`found_at`), else now (`_dated`)
        taken_in = led.messages.get(row_id)
        dated = found_at or _dated(taken_in["received_at"] if taken_in is not None else None)
        wp = plan(
            items,
            document_type=ex.document_type,
            store=ctx.trips,
            book=ctx.book,
            received_at=dated,
        )
        outcomes = ctx.executor.execute(wp, message_row_id=row_id)
        results = {o.result for o in outcomes}
        if "failed" in results:
            status = "failed"
        elif results <= {"duplicate", "ignored"}:
            status = "ignored"
        else:
            status = "processed"
        led.messages.finish(
            row_id,
            status,
            parser=parsed.parser,
            document_type=ex.document_type,
            # a reading that ended done dates the findings; one that failed leaves none
            **({"found_at": dated} if status in DONE_STATUSES and not found_at else {}),
            **(held if status in DONE_STATUSES else {}),
        )
        return MessageResult(
            row_id,
            status,
            LABELS[status],
            outcomes=outcomes,
            items=items,
            parsed=parsed,
            normalized=nm,
        )
    except Exception as exc:  # noqa: BLE001 - record and move on; `replay --status failed` retries
        log.exception("processing failed for message #%s", row_id)
        led.messages.finish(row_id, "failed", error=str(exc)[:1000])
        _alert(ctx, notify, nm.subject, str(exc))
        return MessageResult(row_id, "failed", LABELS["failed"], normalized=nm)


@dataclass
class _Told:
    """What one reading of a message tells: its `filed` notice's text and link, and its marked
    bookings the owner can see, each once, in the message's order."""

    text: str
    link: str | None
    marked: list[TripItem]


def _telling(ctx: Context, res: MessageResult) -> _Told | None:
    """What reading `res` tells, or None when it filed nothing to tell (`format_outcomes`): its
    lines, FILED_LINES of them at most, linked to the first booking it filed or changed that the
    owner can see. A flight log is named only when one is kept."""
    if not (res.outcomes and res.normalized and res.parsed):
        return None
    link_base = ctx.settings.link_base
    text = format_outcomes(
        shown(res.normalized.subject),
        res.outcomes,
        parser=res.parsed.parser,
        link_base=link_base,
        flight_log=ctx.trips.airtrail_on(),
        limit=FILED_LINES,
    )
    if not text:
        return None
    # a booking the owner cannot see (removed, or in a deleted trip) is neither linked nor carded
    seen = {
        o.trip_item_id: ctx.trips.visible(o.trip_item_id)
        for o in res.outcomes
        if o.trip_item_id is not None
    }
    filed = [
        seen[o.trip_item_id]
        for o in res.outcomes
        if o.result in ("filed", "updated") and seen.get(o.trip_item_id) is not None
    ]
    link = booking_link(filed[0], link_base) if link_base and filed else None
    marked: dict[int, TripItem] = {}
    for o in res.outcomes:
        ti = seen.get(o.trip_item_id)
        if ti is not None and ti.checks:
            marked.setdefault(ti.id, ti)
    return _Told(text, link, list(marked.values()))


def _notify(ctx: Context, readings: list[MessageResult]) -> None:
    """What a message and the emails attached to it tell, as one budget: for each of the first
    FILED_NOTICES readings that filed something, its lines (a `filed` notice), then a card (a
    `needs_look` notice, with the buttons that settle what a button can) for each booking it
    left marked while fewer than CARD_LIMIT were sent. A reading whose marked bookings did not
    all get a card ends its `filed` notice with how many more the page holds to check, and the
    last `filed` notice with how many more emails that filed something were attached."""
    link_base = ctx.settings.link_base
    kept = ctx.trips.airtrail_on()
    telling: list[_Told] = []
    for res in readings:
        try:
            told = _telling(ctx, res)
        except Exception:  # noqa: BLE001 - the bookings are filed: the status stands
            log.exception("message #%s: notification failed", res.message_row_id)
            continue
        if told is not None:
            telling.append(told)
    cards_left = CARD_LIMIT
    sent = telling[:FILED_NOTICES]
    for n, told in enumerate(sent, 1):
        cards: list[tuple[str, TripItem]] = []
        for ti in told.marked[:cards_left]:
            try:
                cards.append((check_card(ti, ctx.book, link_base=link_base), ti))
            except Exception:  # noqa: BLE001 - one booking must not silence the others
                log.exception("booking #%s: its card could not be made", ti.id)
        cards_left -= len(cards)
        lines = [told.text]
        if len(told.marked) > len(cards):
            lines.append(MORE_TO_CHECK.format(n=len(told.marked) - len(cards)))
        if n == len(sent) and len(telling) > len(sent):
            lines.append(MORE_EMAILS.format(n=len(telling) - len(sent)))
        ctx.notifier.notify(Notice("filed", "\n".join(lines), link=told.link))
        for card, ti in cards:
            link = booking_link(ti, link_base) if link_base else None
            ctx.notifier.notify(Notice("needs_look", card, link=link, booking=ti, flight_log=kept))


KV_POLL_ALERTED = "poll_alerted"
STALE_POLL = dt.timedelta(hours=1)


def poll_failed(ctx: Context, exc: BaseException) -> None:
    """The poll itself failed (IMAP login, network). Logged every time; one `mail_down` notice
    once no poll has succeeded for STALE_POLL (a revoked app password, the mail server down), and
    the next successful poll sends `mail_up`. A single blip between good polls stays in the
    log."""
    led = ctx.ledger
    log.error("poll failed: %s", exc)
    last_ok = led.kv.get("last_poll_ok")
    try:
        stale = last_ok is None or now() - parse_iso(last_ok) > STALE_POLL
    except ValueError:
        stale = True
    if stale and not led.kv.get(KV_POLL_ALERTED) and ctx.notifier.channels:
        since = last_ok or "it started"
        down = f"tripbox has not reached the mailbox since {since}: {redact(str(exc))[:300]}"
        ctx.notifier.notify(Notice("mail_down", down))
        led.kv.set(KV_POLL_ALERTED, now_iso())


# What the owner is told when the worker stopped the poll for its time twice running.
POLL_STOPPED = (
    "tripbox's mail check has not finished since {since}: it was stopped after {n} minutes."
)


def poll_stopped(ctx: Context, minutes: int) -> None:
    """The worker stopped the poll for its time (`minutes`) twice running: one `mail_down`
    notice saying since when no poll finished, told once as `poll_failed` tells its own (the
    next poll that finishes sends `mail_up`); nothing while mail is off, which no `mail_up`
    would follow."""
    led = ctx.ledger
    log.error("the poll was stopped after %d minutes twice running", minutes)
    if not ctx.settings.on("mail") or led.kv.get(KV_POLL_ALERTED) or not ctx.notifier.channels:
        return
    since = led.kv.get("last_poll_ok") or "it started"
    ctx.notifier.notify(Notice("mail_down", POLL_STOPPED.format(since=since, n=minutes)))
    led.kv.set(KV_POLL_ALERTED, now_iso())


@dataclass
class PollSummary:
    fetched: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    seconds: float = 0.0  # wall time of the whole poll, logged so a slow night is diagnosable

    def add(self, status: str) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1


def resume_waiting(ctx: Context) -> int:
    """Take up, oldest first, each message whose attached emails were left unfiled
    (ATTACHED_UNFILED: its own reading is done, so no model is asked for it), then read again
    the messages that wait for the model; each is read from its archive file through
    `process_raw` under its own source and told as any reading is. The second pass stops at the
    first message that waits again, its reason not gone, and every message that waits then
    takes that reason (`wait_for_a_model`); it does not run while no model is built. A message
    whose archived email is gone is failed, told once (`_archived`), and the pass goes on. How
    many were read."""
    read = 0
    led = ctx.ledger
    tries = _resume_tries(led)
    unfiled = led.messages.by_error(ATTACHED_UNFILED)
    # a message finished meanwhile (its bytes came again) leaves the count
    tries = {k: n for k, n in tries.items() if k in {str(r["id"]) for r in unfiled}}
    _keep_resume_tries(led, tries)
    for row in unfiled:
        key = str(row["id"])
        if tries.get(key, 0) < MAX_RESUMES:
            # counted before it is taken up, so a take-up the poll's kill ends still counts
            tries[key] = tries.get(key, 0) + 1
            _keep_resume_tries(led, tries)
            try:
                raw = _archived(ctx, row)
                if raw is not None:
                    process_raw(ctx, raw, source=row["source"])
                    read += 1
            except Exception:  # noqa: BLE001 - one message that cannot be read stops no other
                log.exception("message #%s: the resume pass could not take it up", row["id"])
        now = led.messages.get(row["id"])
        if now is None or now["error"] != ATTACHED_UNFILED:
            tries.pop(key, None)
        elif tries[key] >= MAX_RESUMES:  # taken up that many times, the last one killed or not
            _give_up_attached(ctx, now)
            tries.pop(key)
        _keep_resume_tries(led, tries)
    if read:
        log.info("resume: %d message(s) whose attached emails were left taken up", read)
    if ctx.model is None:
        return read
    waited = 0
    for row in ctx.ledger.messages.by_status("waiting"):
        try:
            raw = _archived(ctx, row)
            if raw is None:
                continue
            res = process_raw(ctx, raw, source=row["source"])
        except Exception:  # noqa: BLE001 - one message that cannot be read stops no other
            log.exception("message #%s: the resume pass could not read it", row["id"])
            continue
        if res.status == "waiting":
            # its reason is the model's, so every other message that waits takes it too
            if res.wait_reason in WAIT_REASONS:
                wait_for_a_model(ctx.ledger, res.wait_reason)
            break
        waited += 1
    if waited:
        log.info("resume: %d message(s) that waited for the model read", waited)
    return read + waited


def _archived(ctx: Context, row: sqlite3.Row) -> bytes | None:
    """The bytes of the email the message `row` was read from, from its archive file. When the
    file is gone (a backup restored without its mail), nothing can read it again: the message is
    finished `failed` (ARCHIVE_MISSING), told once, the upload it ended as says so, and None."""
    path = Path(row["archive_path"] or "")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    led = ctx.ledger
    with led.transaction():
        led.messages.finish(row["id"], "failed", error=ARCHIVE_MISSING)
        led.uploads.read_again(row["id"], "failed", "unreadable", row["id"])
    log.warning("message #%s: its archived email is missing; failed", row["id"])
    _alert(ctx, True, row["subject"] or f"message #{row['id']}", ARCHIVE_MISSING)
    return None


def _resume_tries(ledger: Ledger) -> dict[str, int]:
    """How many times the resume pass took each unfinished message up, by its id (as text)."""
    try:
        held = json.loads(ledger.kv.get(KV_RESUME_TRIES) or "{}")
    except ValueError:
        return {}
    if not isinstance(held, dict):
        return {}
    return {str(k): n for k, n in held.items() if type(n) is int and n > 0}


def _keep_resume_tries(ledger: Ledger, tries: dict[str, int]) -> None:
    """The count kept, written only when it changed."""
    if tries != _resume_tries(ledger):
        ledger.kv.set(KV_RESUME_TRIES, json.dumps(tries, sort_keys=True))


def _give_up_attached(ctx: Context, row: sqlite3.Row) -> None:
    """A message the resume pass took up MAX_RESUMES times for its attached emails is finished
    as `failed`, and the owner told once (a `failure` notice)."""
    ctx.ledger.messages.finish(row["id"], "failed", error=ATTACHED_GIVEN_UP)
    log.warning(
        "message #%s: its attached emails were not filed after %d tries; failed",
        row["id"],
        MAX_RESUMES,
    )
    _alert(ctx, True, row["subject"] or "", ATTACHED_GIVEN_UP)


def wait_for_a_model(ledger: Ledger, reason: str) -> int:
    """Every message that waits now waits for `reason` (one of WAIT_REASONS), whatever it waited
    for when it was read, and the word of each upload it ended as says so, in one transaction:
    what a message waits for is the whole model's state — none is built (`no_model`, when the
    worker starts), or the one message the resume pass found waiting again found the budget
    used or the model unreachable. How many messages changed."""
    with ledger.transaction():
        changed = ledger.messages.wait_for(reason)
        for message_id in changed:
            ledger.uploads.read_again(message_id, "nothing", reason, message_id)
    return len(changed)


# What the owner is told of a message over the cap (imap.MAX_MESSAGE_BYTES), which is never read
OVERSIZE = "tripbox left a message of {n} MB unread: it is over the {limit} MB limit."
MB = 1024 * 1024


def read_mail(
    ctx: Context,
    inbox: imapmod.Inbox,
    msg: imapmod.RawMessage | imapmod.Oversize,
    summary: PollSummary,
    *,
    filing: bool,
) -> None:
    """One message of the mailbox through `process_raw` as mail, counted in `summary`; with
    `filing` its copy goes into the label of what it came to (none while it waits, or when it
    was skipped). One over the cap was never fetched: it counts as `oversize`, one `failure`
    notice says so and it is filed as failed. An error is counted and told, never raised."""
    if isinstance(msg, imapmod.Oversize):
        summary.add("oversize")
        log.warning("uid %s: %d bytes, over the size limit: left unread", msg.uid, msg.size)
        text = OVERSIZE.format(n=math.ceil(msg.size / MB), limit=imapmod.MAX_MESSAGE_BYTES // MB)
        ctx.notifier.notify(Notice("failure", text))
        if filing:
            imapmod.label(inbox, msg.uid, imapmod.LABEL_FAILED)
        return
    summary.fetched += 1
    try:
        result = process_raw(ctx, msg.raw, source="imap", uid=msg.uid)
        summary.add(result.status)
        if filing and result.label:
            imapmod.label(inbox, msg.uid, result.label)
    except Exception as exc:  # noqa: BLE001
        summary.errors += 1
        log.exception("uid %s: unhandled error %s", msg.uid, exc)
        if filing:
            imapmod.label(inbox, msg.uid, imapmod.LABEL_FAILED)
        _alert(ctx, True, f"uid {msg.uid}", str(exc))


def backfill(ctx: Context, inbox: imapmod.Inbox, uids: list[int]) -> PollSummary:
    """`tripbox mail backfill --apply`: each of `uids` read as a poll reads mail (`read_mail`:
    the sender check, the model's cap, filing as set, the size cap), in order. The cursor is
    left where it is; a message the ledger holds is skipped."""
    summary = PollSummary()
    t0 = time.monotonic()
    filing = ctx.settings.mail_filing == "labels"
    if filing:
        inbox.ensure_folders(imapmod.ALL_LABELS)
    for msg in imapmod.fetch(inbox, uids):
        read_mail(ctx, inbox, msg, summary, filing=filing)
    summary.seconds = time.monotonic() - t0
    log.info(
        "backfill done: %d messages %s errors=%d in %.1fs",
        len(uids),
        summary.counts,
        summary.errors,
        summary.seconds,
    )
    return summary


def poll(ctx: Context, inbox: imapmod.Inbox | None) -> PollSummary:
    """One scheduled run: new mail from `inbox`, then the mail that waits for the model
    (`resume_waiting`) and the purge of old deletions. With no inbox (mail is off)
    only the rest runs, and the run still counts as a good poll for the healthcheck."""
    led = ctx.ledger
    summary = PollSummary()
    t0 = time.monotonic()
    led.kv.set("last_poll_attempt", now_iso())
    if inbox is not None:
        filing = ctx.settings.mail_filing == "labels"
        if filing:
            inbox.ensure_folders(imapmod.ALL_LABELS)
        for msg in imapmod.fetch_new(inbox, led):
            read_mail(ctx, inbox, msg, summary, filing=filing)
            imapmod.mark_seen_uid(led, msg.uid)
    resume_waiting(ctx)
    led.kv.set("last_poll_ok", now_iso())
    if led.kv.get(KV_POLL_ALERTED):
        led.kv.set(KV_POLL_ALERTED, "")
        if inbox is not None:  # with mail off nothing reads the mailbox again
            ctx.notifier.notify(Notice("mail_up", "tripbox is reading the mailbox again"))
    try:  # Recently deleted: what has sat there 30 days goes for good (never fails the poll)
        gone_items, gone_trips = ctx.trips.purge_deleted()
        if gone_items or gone_trips:
            log.info("purged %d removed bookings and %d deleted trips", gone_items, gone_trips)
    except Exception:  # noqa: BLE001
        log.exception("purge of deleted trips failed")
    summary.seconds = time.monotonic() - t0
    log.info(
        "poll done: fetched=%d %s errors=%d in %.1fs",
        summary.fetched,
        summary.counts,
        summary.errors,
        summary.seconds,
    )
    return summary
