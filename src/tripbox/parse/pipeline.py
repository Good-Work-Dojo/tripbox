# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""kitinerary first, the model only when kitinerary found nothing or something incomplete."""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from tripbox.mail.normalize import NormalizedMessage
from tripbox.parse import kitinerary
from tripbox.parse.models import Extraction, is_complete
from tripbox.parse.provider import READ_BY_MODEL, WAITS, ModelError, Provider, Usage

log = logging.getLogger("tripbox.parse")

# A subject that says the booking was cancelled ("has been cancelled", "Reservation X canceled",
# "Cancellation confirmed", "Cancelled: …"), never one that only mentions cancelling (a free
# cancellation, a policy) or says it was not.
CANCEL_SUBJECT = re.compile(
    r"(?<!\bnot )\b(?:"
    r"(?:has|have|had|was|were|is|are|been)\s+(?:been\s+)?cancell?ed\b"
    r"|cancell?ation\s+(?:confirmed|confirmation|notice|receipt)\b"
    r"|(?:reservation|booking|ticket|flight|trip|stay|order|itinerary|rental|hotel)"
    r"\s+(?:(?!not\b)[\w#-]+\s+){0,3}cancell?ed\b"
    r")"
    r"|^\s*(?:(?:fwd?|re)\s*:\s*)*cancell?ed\b",
    re.I,
)
Runner = Callable[[bytes, str, str | None], list[dict]]


@dataclass
class ParsedMessage:
    extraction: Extraction
    parser: str  # kitinerary | READ_BY_MODEL | none
    usage: Usage | None = None
    kitinerary_objects: list[dict] = field(default_factory=list)
    used_hint: bool = False
    error: str | None = None
    # the kind of the model's failure (provider.KINDS) and its word, when it gave no answer
    error_kind: str | None = None
    error_word: str | None = None


def from_text(
    text: str, *, now: dt.datetime, subject: str = "(pasted itinerary)", from_addr: str = "manual"
) -> NormalizedMessage:
    """Wrap pasted itinerary text (Telegram /paste, CLI ingest of a .txt) as a message."""
    return NormalizedMessage(
        message_id=None,
        subject=subject,
        from_addr=from_addr,
        date=now,
        outer_date=now,
        text=text,
        attachments=[],
        origin_ok=True,
        origin_reason="manual input",
        forwarded="none",
        raw_for_kitinerary=b"",
        raw=text.encode("utf-8"),
    )


def _apply_subject_cancellation(nm: NormalizedMessage, ex: Extraction) -> Extraction:
    """'Your flight has been cancelled' with a booking-shaped body is a cancellation — unless
    the source marks a reservation confirmed in so many words: a cancelled-and-rebooked email
    carries the NEW flight, and that one is a booking."""
    if ex.document_type == "booking" and CANCEL_SUBJECT.search(nm.subject or ""):
        for r in ex.reservations:
            if r.status != "confirmed":
                r.status = "cancelled"
                for seg in r.segments:
                    if seg.status != "confirmed":
                        seg.status = "cancelled"
        if all(r.status == "cancelled" for r in ex.reservations):
            ex.document_type = "cancellation"
    return ex


def parse_message(
    nm: NormalizedMessage, model: Provider | None, runner: Runner | None = None
) -> ParsedMessage:
    runner = runner or kitinerary.run_extractor
    if nm.raw_for_kitinerary:
        data, suffix = nm.raw_for_kitinerary, ".eml"
    else:
        data, suffix = (nm.text or "").encode("utf-8"), ".txt"
    context = nm.email_date.date().isoformat() if nm.email_date else None
    objects: list[dict] = []
    try:
        objects = runner(data, suffix, context)
    except kitinerary.KitineraryError as exc:
        log.warning("kitinerary failed: %s", exc)
    ex = kitinerary.map_jsonld(objects)
    real = [r for r in ex.reservations if r.kind != "other"]
    complete = bool(real) and all(is_complete(r) for r in real)
    if complete:
        return ParsedMessage(
            extraction=_apply_subject_cancellation(nm, ex),
            parser="kitinerary",
            kitinerary_objects=objects,
        )
    if model is None:
        # nothing more can be done without a model: not an error, the message is just unparsed
        return ParsedMessage(extraction=ex, parser="none", kitinerary_objects=objects)
    hint = ex if ex.reservations else None
    try:
        ex2, usage = model.extract(nm, hint=hint)
    except ModelError as exc:
        # a cap that is reached or a model that cannot be reached is a wait, not a failure
        (log.warning if exc.kind in WAITS else log.error)("the model gave no answer: %s", exc)
        return ParsedMessage(
            extraction=ex,
            parser="none",
            usage=exc.usage,  # what a failure the model was billed for cost
            kitinerary_objects=objects,
            used_hint=hint is not None,
            error=str(exc),
            error_kind=exc.kind,
            error_word=exc.word,
        )
    return ParsedMessage(
        extraction=_apply_subject_cancellation(nm, ex2),
        parser=READ_BY_MODEL,
        usage=usage,
        kitinerary_objects=objects,
        used_hint=hint is not None,
    )
