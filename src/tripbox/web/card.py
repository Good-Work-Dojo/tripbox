# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The status card: in plain words, whether the worker runs, whether mail is read and where to
forward confirmations, whether a model is set up and how much mail waits for it, and whether a
flight log is kept. It is made from the worker's own record (`tripbox.status`) alone, which
holds words and counts, never an error's text: the page loads nothing of the worker's, the
mailbox's, the model's or the flight log's, and asks none of them. The mailbox's address is the
one setting the record carries, so the card goes only on pages behind the sign-in."""

from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING

from tripbox import status, util

if TYPE_CHECKING:
    from tripbox.ledger import Ledger

# The rows' labels, in the card's order
WORKER, MAIL, MODEL, FLIGHT_LOG = "Worker", "Mail", "Model", "Flight log"
RUNNING = "Running."
NOT_RUNNING = "Not running. Nothing new is read until it is started."
ON = "On."
# What the install does without a feature that is off, by the record's name for it
OFF = {
    "mail": "Off. Add bookings by pasting or uploading a confirmation.",
    "model": "Off. Only emails that carry structured booking data are read.",
    "airtrail": "Off. Flights are kept in your trips only.",
}
INVALID = 'Its settings need a look: run "tripbox doctor".'
FIRST_CHECK = "Forward confirmations to {mailbox}. The first check has not finished."
CHECKED = "Forward confirmations to {mailbox}. Last checked {age} ago."
# the same, while the record names no address to forward to
ON_FIRST_CHECK = "On. The first check has not finished."
ON_CHECKED = "On. Last checked {age} ago."
MAIL_FAILED = "The mailbox could not be checked ({code}). It is tried again by itself."
# the poll read the mailbox, and an email in it could not be read (`worker.MESSAGE_FAILED`)
MESSAGE_FAILED = "An email could not be read. It is kept, and the log names it."
# What mail waits for the model for (the record's `waiting`), in this order
WAITING = {
    "no_model": "{n} waiting: no model is set up.",
    "over_budget": "{n} waiting: the model's budget is used.",
    "model_down": "{n} waiting: the model cannot be reached.",
}
DRY_RUN = "On, in dry-run mode: flights are filed here and nothing is sent."
PASS_FAILED = "The last pass could not reach it ({code})."
# A code as the worker records one; anything else in its place is shown as `error`
CODE = re.compile(r"[a-z0-9_]{1,40}")


def age(seconds: float) -> str:
    """How long ago, in a word: `moments` under a minute (and for a time ahead of the clock),
    then whole minutes, hours or days."""
    s = int(seconds)
    if s < 60:
        return "moments"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def _row(label: str, text: str, ok: bool) -> dict:
    return {"label": label, "text": text, "ok": ok}


def _state(record: dict, name: str) -> str:
    """The feature's state as the record says it: on, invalid, or off for anything else."""
    features = record.get("features")
    state = features.get(name) if isinstance(features, dict) else None
    return state if state in ("on", "invalid") else "off"


def _last(record: dict, part: str) -> dict | None:
    """How the part of a beat last ended, or None when it has not."""
    last = record.get("last")
    ended = last.get(part) if isinstance(last, dict) else None
    return ended if isinstance(ended, dict) else None


def _code(ended: dict) -> str:
    code = ended.get("code")
    return code if isinstance(code, str) and CODE.fullmatch(code) else "error"


def mailbox(record: dict | None, now: dt.datetime) -> str | None:
    """Where confirmations are forwarded: the address a running worker reads mail from, else
    None."""
    if not status.live(record, now) or _state(record, "mail") != "on":
        return None
    address = record.get("mailbox")
    return address if isinstance(address, str) and address else None


def _mail(record: dict, now: dt.datetime) -> dict:
    state = _state(record, "mail")
    if state == "invalid":
        return _row(MAIL, INVALID, False)
    if state == "off":
        return _row(MAIL, OFF["mail"], True)
    poll = _last(record, "poll")
    if poll is not None and poll.get("ok") is not True:
        if _code(poll) == "message_failed":
            return _row(MAIL, MESSAGE_FAILED, False)
        return _row(MAIL, MAIL_FAILED.format(code=_code(poll)), False)
    # the address to forward to; none when the login is no address, or an older worker's
    # record names none: the card then says only that mail is on
    address = mailbox(record, now)
    try:
        at = util.parse_iso(str(poll.get("at"))) if poll is not None else None
    except ValueError:  # no time a worker writes: as if no check had finished
        at = None
    if at is None:
        if address is None:
            return _row(MAIL, ON_FIRST_CHECK, True)
        return _row(MAIL, FIRST_CHECK.format(mailbox=address), True)
    ago = age((now - at).total_seconds())
    if address is None:
        return _row(MAIL, ON_CHECKED.format(age=ago), True)
    return _row(MAIL, CHECKED.format(mailbox=address, age=ago), True)


def _model(record: dict) -> list[dict]:
    state = _state(record, "model")
    rows = [_row(MODEL, {"on": ON, "off": OFF["model"]}.get(state, INVALID), state != "invalid")]
    waiting = record.get("waiting")
    for reason, sentence in WAITING.items():
        n = waiting.get(reason) if isinstance(waiting, dict) else None
        if type(n) is int and n > 0:
            rows.append(_row(MODEL, sentence.format(n=n), False))
    return rows


def _flight_log(record: dict) -> dict:
    state = _state(record, "airtrail")
    if state != "on":
        return _row(FLIGHT_LOG, OFF["airtrail"] if state == "off" else INVALID, state == "off")
    sync = _last(record, "sync")
    if sync is not None and sync.get("ok") is not True:
        return _row(FLIGHT_LOG, PASS_FAILED.format(code=_code(sync)), False)
    if record.get("dry_run") is True:
        return _row(FLIGHT_LOG, DRY_RUN, True)
    return _row(FLIGHT_LOG, ON, True)


def rows(record: dict | None, now: dt.datetime) -> list[dict]:
    """The card of the worker's `record` at `now`: each row's `label`, its sentence (`text`) and
    whether all is well (`ok`), the Worker first; while no worker runs (`status.live`), that
    row alone."""
    if record is None or not status.live(record, now):
        return [_row(WORKER, NOT_RUNNING, False)]
    return [_row(WORKER, RUNNING, True), _mail(record, now), *_model(record), _flight_log(record)]


def status_card(ledger: Ledger, now: dt.datetime) -> list[dict]:
    """The card of the record the worker last wrote on `ledger` (`rows`)."""
    return rows(status.read(ledger), now)
