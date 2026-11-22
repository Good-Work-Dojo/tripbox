# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The corpus: invented travel emails with known right answers (`tests/corpus/`, described in its
README). Each entry is a directory `NN-slug` holding one email as it reaches the mailbox, the
model's answer to it (or that the model is not asked), what the email says and what filing it
does. `load` reads a corpus and refuses one with any problem, each named with its file; the corpus
is read under its own settings (`settings`, `process_as`), and its answers stand in for the model
(`CorpusAnswers`)."""

from __future__ import annotations

import contextlib
import datetime as dt
import email
import email.parser
import email.policy
import email.utils
import hashlib
import json
import os
import re
import stat
import time
import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydantic

from tripbox import lists
from tripbox.config import Settings
from tripbox.mail.normalize import NormalizedMessage
from tripbox.parse.models import (
    DocumentType,
    Extraction,
    Reservation,
    Segment,
    Status,
    WireExtraction,
    to_extraction,
)
from tripbox.parse.provider import DEFAULT_MODEL, ModelError, Usage
from tripbox.parse.replay import NO_ANSWER, WIRE_SCHEMA_VERSION
from tripbox.util import use_zone
from tripbox.validate import checks
from tripbox.validate.keys import draft_key, is_draft

FILES = ("email.eml", "answer.json", "extraction.json", "outcome.json")
ENTRY_NAME = re.compile(r"(\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)")
SETTING_NAMES = frozenset(
    {
        "TZ",
        "TRIPBOX_HOME_AIRPORTS",
        "TRIPBOX_MAIL_ALLOWED_SENDERS",
        "TRIPBOX_MAIL_TRUST",
        "TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS",
    }
)
ANSWER_SOURCES = ("hand-written", "recorded", "not-asked")
MESSAGE_STATUSES = ("processed", "ignored", "rejected", "failed")
READERS = ("kitinerary", "model")
RESULTS = ("filed", "updated", "duplicate", "ignored", "failed")
MAX_EMAIL_BYTES = 1_000_000
# Behaviour that filing does not have yet: the id is what an outcome.json names in `gap`, the text
# says what filing such mail is meant to do. The rest of that outcome.json records today's filing.
# None is known today.
GAPS: dict[str, str] = {}

CORPUS_FILE = "corpus.json"
CONFIG_DIR = "config"
TRAVELLERS = f"{CONFIG_DIR}/travellers.yaml"
KINDS = ("flight", "lodging", "car", "train")
DOCUMENT_TYPES: tuple[str, ...] = typing.get_args(DocumentType)
STATUSES: tuple[str, ...] = typing.get_args(Status)
ANSWER_KEYS = ("wire_schema", "source", "model", "answer")
TRUTH_KEYS = ("document_type", "reservations")
OUTCOME_KEYS = ("status", "read_by", "document_type", "items")
ITEM_KEYS = ("kind", "key", "result", "people", "booking")
PEOPLE_KEYS = ("me", "guests", "others")
BOOKING_KEYS = ("status", "travellers", "trip", "checks", "sync")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_BAD_NAME = "not an entry name (two digits, a dash, lower-case words joined by dashes)"
_LINK = "is a link (nothing behind a link is read)"


class CorpusError(ValueError):
    """What is wrong with a corpus: every problem, one per line."""


@dataclass(frozen=True)
class Entry:
    """One email with its answer and its truth. `outcome` is outcome.json without `gap`."""

    name: str
    path: Path
    raw: bytes
    answer: WireExtraction | None
    answer_source: str
    answer_model: str | None
    truth: dict
    outcome: dict
    gap: str | None


@dataclass(frozen=True)
class Corpus:
    """A corpus as loaded: its settings, the demo's day and entries, and every entry in name
    order (the order of arrival)."""

    root: Path
    settings: dict[str, str]
    anchor: dt.date | None
    demo: tuple[str, ...]
    entries: tuple[Entry, ...]

    def entry(self, name: str) -> Entry:
        for e in self.entries:
            if e.name == name:
                return e
        raise KeyError(name)


def sent(entry: Entry) -> dt.datetime:
    """The entry's email's own Date, which the corpus takes for the moment it reached the
    mailbox (UTC when it names no zone)."""
    headers = email.parser.BytesHeaderParser(policy=email.policy.default).parsebytes(entry.raw)
    when = email.utils.parsedate_to_datetime(str(headers["Date"]))
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def default_dir() -> Path:
    """The corpus beside the source tree the package runs from (in the image too, where the
    package is installed from /app/src)."""
    return Path(__file__).resolve().parents[2] / "tests" / "corpus"


def problems(root: Path) -> list[str]:
    """Everything wrong with the corpus at `root`, one line each: `<entry>/<file>: <what>`,
    `corpus.json: <what>` or `<name>: <what>`. Empty when the corpus loads. A relative `root` is
    taken from the working directory."""
    return _read(Path(root).absolute())[0]


def load(root: Path) -> Corpus:
    """The corpus at `root`, its root made absolute (the settings name its config directory by
    an absolute path); CorpusError with every problem when it does not load."""
    root = Path(root).absolute()
    if not root.is_dir() or not (root / CORPUS_FILE).is_file():
        raise CorpusError(f"{root}: no corpus here")
    found, corpus = _read(root)
    if found or corpus is None:
        raise CorpusError("\n".join(found))
    return corpus


# ----------------------------------------------------------------------------- reading
class _Problems:
    """The problems found so far, each under the name of the file it is about."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, where: str, what: str) -> None:
        self.lines.append(f"{where}: {what}")


def _read(root: Path) -> tuple[list[str], Corpus | None]:
    found = _Problems()
    names = _entry_names(root)
    meta = _read_corpus_json(root, [n for n, _ in names], found)
    _check_travellers(root, found)
    for name, why in names:
        if why:
            found.add(name, why)
    entries: list[Entry] = []
    previous: tuple[str, dt.datetime] | None = None
    for name, why in names:
        if why in (_BAD_NAME, _LINK):
            continue
        entry, date = _read_entry(root / name, name, previous, found)
        if date is not None:
            previous = (name, date)
        if entry is not None:
            entries.append(entry)
    if found.lines or meta is None:
        return found.lines, None
    settings, anchor, demo = meta
    return [], Corpus(root, settings, anchor, demo, tuple(entries))


def _check_travellers(root: Path, found: _Problems) -> None:
    """The travellers file is checked without following a link: a link in its place, or in its
    directory's, is named; anything but a file of its own there is missing."""
    for name in (CONFIG_DIR, TRAVELLERS):
        if (root / name).is_symlink():
            found.add(name, _LINK)
            return
    try:
        a_file = stat.S_ISREG((root / TRAVELLERS).lstat().st_mode)
    except OSError:
        a_file = False
    if not a_file:
        found.add(TRAVELLERS, "missing (the travellers the corpus is read with)")


def _entry_names(root: Path) -> list[tuple[str, str | None]]:
    """Every directory but `config`, and every link but one to a file (one to a directory, a
    dangling one, one that loops: nothing behind a link is read), in name order, with what is
    wrong with it (or None)."""
    try:
        dirs = sorted(
            p
            for p in root.iterdir()
            if p.name != CONFIG_DIR and (p.is_dir() or (p.is_symlink() and not p.is_file()))
        )
    except OSError:
        return []
    out: list[tuple[str, str | None]] = []
    taken: dict[str, str] = {}
    for path in dirs:
        name = path.name
        m = ENTRY_NAME.fullmatch(name)
        if path.is_symlink():
            out.append((name, _LINK))
        elif m is None:
            out.append((name, _BAD_NAME))
        elif m.group(1) in taken:
            out.append((name, f"number {m.group(1)} is taken by {taken[m.group(1)]}"))
        else:
            taken[m.group(1)] = name
            out.append((name, None))
    return out


# What _json returns for a file it cannot read (a file holding `null` reads as None).
_UNREAD = object()


def _file_bytes(path: Path) -> bytes:
    """A regular file's bytes, never read through a link and never blocking on a pipe; OSError
    when it cannot be read."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(os.dup(fd), "rb") as f:
            return f.read()
    finally:
        os.close(fd)


def _json(path: Path, where: str, found: _Problems) -> Any:
    """The file's JSON; _UNREAD (with the problem noted) when it cannot be read or parsed."""
    try:
        data = _file_bytes(path)
    except OSError:
        found.add(where, "cannot be read")
        return _UNREAD
    try:
        return json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, ValueError, RecursionError:
        found.add(where, "not readable as JSON")
        return _UNREAD


def _exact_keys(doc: dict, keys: tuple[str, ...], optional: tuple[str, ...] = ()) -> str | None:
    """What is wrong with the object's keys, or None when it has exactly `keys` (and perhaps some
    of `optional`)."""
    missing = [k for k in keys if k not in doc]
    extra = sorted(k for k in doc if k not in keys and k not in optional)
    if not missing and not extra:
        return None
    parts = []
    if missing:
        parts.append(f"lacks {', '.join(missing)}")
    if extra:
        parts.append(f"has {', '.join(extra)}, which is not one of its keys")
    return "; ".join(parts)


def _read_corpus_json(
    root: Path, names: list[str], found: _Problems
) -> tuple[dict[str, str], dt.date | None, tuple[str, ...]] | None:
    where = CORPUS_FILE
    if (root / CORPUS_FILE).is_symlink():
        found.add(where, _LINK)
        return None
    if not (root / CORPUS_FILE).is_file():
        found.add(where, "missing")
        return None
    doc = _json(root / CORPUS_FILE, where, found)
    if doc is _UNREAD:
        return None
    if not isinstance(doc, dict):
        found.add(where, "not an object")
        return None
    before = len(found.lines)
    if why := _exact_keys(doc, ("settings", "demo")):
        found.add(where, why)
    settings = doc.get("settings")
    if not isinstance(settings, dict):
        found.add(where, "settings is not an object")
        settings = {}
    for name, value in settings.items():
        if name not in SETTING_NAMES:
            found.add(where, f"settings: {name} is not one the corpus may set")
        elif not isinstance(value, str):
            found.add(where, f"settings: {name} is not a string")
    anchor: dt.date | None = None
    demo: list = []
    raw_demo = doc.get("demo")
    if not isinstance(raw_demo, dict):
        found.add(where, "demo is not an object")
    else:
        if why := _exact_keys(raw_demo, ("anchor", "entries")):
            found.add(where, f"demo {why}")
        value = raw_demo.get("anchor")
        if value is not None:
            try:
                if not isinstance(value, str) or not _DAY.fullmatch(value):
                    raise ValueError(value)
                anchor = dt.date.fromisoformat(value)
            except ValueError:
                found.add(where, "demo: anchor is not a day written YYYY-MM-DD")
        demo = raw_demo.get("entries")
        if not isinstance(demo, list) or not all(isinstance(n, str) for n in demo):
            found.add(where, "demo: entries is not a list of entry names")
            demo = []
        for name in demo:
            if name not in names:
                found.add(where, f"demo: {name} is no entry")
        if any(a >= b for a, b in zip(demo, demo[1:], strict=False)):
            found.add(where, "demo: entries do not ascend, or one repeats")
    if len(found.lines) > before:
        return None
    return dict(settings), anchor, tuple(demo)


def _read_entry(
    path: Path,
    name: str,
    previous: tuple[str, dt.datetime] | None,
    found: _Problems,
) -> tuple[Entry | None, dt.datetime | None]:
    """The entry, or None when anything in it is wrong; and the email's date when it reads, which
    the next entry's must not precede."""
    before = len(found.lines)
    try:
        present = {p.name: p for p in path.iterdir()}
    except OSError:
        found.add(name, "cannot be listed")
        return None, None
    usable: set[str] = set()
    for file in FILES:
        if file not in present:
            found.add(f"{name}/{file}", "missing")
        elif present[file].is_symlink():
            found.add(f"{name}/{file}", _LINK)
        elif not present[file].is_file():
            found.add(f"{name}/{file}", "not a file")
        else:
            usable.add(file)
    for other in sorted(set(present) - set(FILES)):
        found.add(f"{name}/{other}", f"not one of the entry's files ({', '.join(FILES)})")
    raw, date = (
        _read_email(path / "email.eml", f"{name}/email.eml", previous, found)
        if "email.eml" in usable
        else (None, None)
    )
    answer = (
        _read_answer(path / "answer.json", f"{name}/answer.json", found)
        if "answer.json" in usable
        else None
    )
    truth = (
        _read_truth(path / "extraction.json", f"{name}/extraction.json", found)
        if "extraction.json" in usable
        else None
    )
    outcome = (
        _read_outcome(path / "outcome.json", f"{name}/outcome.json", found)
        if "outcome.json" in usable
        else None
    )
    if answer is not None and outcome is not None:
        _cross_check(answer, outcome, f"{name}/outcome.json", found)
    if raw is not None and outcome is not None:
        _check_drafts(raw, outcome, f"{name}/outcome.json", found)
    if raw is None or answer is None or truth is None or outcome is None:
        # an invariant guard, unreachable from any input: every reader that returns nothing
        # names its problem first; the line is kept so an entry is never dropped silently
        if len(found.lines) == before:
            found.add(name, "could not be read")
        return None, date
    if len(found.lines) > before:
        return None, date
    source, model, wire = answer
    gap = outcome.pop("gap", None)
    entry = Entry(name, path, raw, wire, source, model, truth, outcome, gap)
    return entry, date


def _read_email(
    path: Path, where: str, previous: tuple[str, dt.datetime] | None, found: _Problems
) -> tuple[bytes | None, dt.datetime | None]:
    try:
        size = path.lstat().st_size
        raw = b"" if size > MAX_EMAIL_BYTES else _file_bytes(path)
    except OSError:
        found.add(where, "cannot be read")
        return None, None
    if max(size, len(raw)) > MAX_EMAIL_BYTES:
        found.add(where, f"{max(size, len(raw)):,} bytes, over the limit of {MAX_EMAIL_BYTES:,}")
        return None, None
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        values = {h: str(msg.get(h) or "").strip() for h in ("From", "Message-ID", "Date")}
        # every attached email at any depth: the mail reader files each as a message of its own
        attached = sum(1 for part in msg.walk() if part.get_content_type() == "message/rfc822")
    except Exception:  # noqa: BLE001 - any parser failure is the same problem
        found.add(where, "does not parse as an email")
        return raw, None
    for header in ("From", "Message-ID"):
        if not values[header]:
            found.add(where, f"no {header} header")
    if attached > 1:
        found.add(where, "carries more than one attached email")
    try:
        date = email.utils.parsedate_to_datetime(values["Date"])
    except TypeError, ValueError, IndexError, OverflowError:
        found.add(where, "no Date header that reads as a date")
        return raw, None
    if date.tzinfo is None:
        date = date.replace(tzinfo=dt.UTC)
    if previous is not None and date < previous[1]:
        found.add(where, f"dated before {previous[0]}, the entry ahead of it")
    return raw, date


def _model_of(annotation: Any) -> type[pydantic.BaseModel] | None:
    """The model a list field holds (list[WireLeg] -> WireLeg), or None."""
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, pydantic.BaseModel):
            return arg
    return None


def _unknown_keys(data: Any, model: type[pydantic.BaseModel], at: str) -> list[str]:
    """The keys, at any depth, that `model` has no field for (validation alone ignores them)."""
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            out.append(f"{at}.{key}")
            continue
        inner = _model_of(field.annotation)
        if inner is not None and isinstance(value, list):
            for i, item in enumerate(value):
                out += _unknown_keys(item, inner, f"{at}.{key}[{i}]")
    return out


def _read_answer(
    path: Path, where: str, found: _Problems
) -> tuple[str, str | None, WireExtraction | None] | None:
    doc = _json(path, where, found)
    if doc is _UNREAD:
        return None
    if not isinstance(doc, dict):
        found.add(where, "not an object")
        return None
    before = len(found.lines)
    if why := _exact_keys(doc, ANSWER_KEYS):
        found.add(where, why)
    version = doc.get("wire_schema")
    if type(version) is not int or version != WIRE_SCHEMA_VERSION:
        shown = version if type(version) is int else json.dumps(version)
        found.add(
            where, f"written for wire schema {shown}; this version reads {WIRE_SCHEMA_VERSION}"
        )
    source, model, answer = doc.get("source"), doc.get("model"), doc.get("answer")
    if source not in ANSWER_SOURCES:
        found.add(where, f"source is not one of {', '.join(ANSWER_SOURCES)}")
    elif (source == "not-asked") != (answer is None):
        found.add(where, "the answer is null exactly when the source is not-asked")
    if source == "recorded" and not (isinstance(model, str) and model.strip()):
        found.add(where, "a recorded answer names the model that gave it")
    elif source != "recorded" and model is not None:
        found.add(where, "only a recorded answer names a model")
    wire: WireExtraction | None = None
    if answer is not None:
        if not isinstance(answer, dict):
            found.add(where, "answer is not an object")
        else:
            for key in _unknown_keys(answer, WireExtraction, "answer"):
                found.add(where, f"{key} is no field of the wire schema")
            try:
                wire = WireExtraction.model_validate(answer)
            except pydantic.ValidationError as exc:
                first = exc.errors()[0]
                at = ".".join(str(p) for p in first["loc"])
                found.add(where, f"answer does not fit the wire schema: {at}: {first['msg']}")
            # below the model's confidence floor an answer's bookings are marked to check: a
            # hand-written one states the same confidence every time, so filing never depends on
            # its author
            confidence = answer.get("confidence")
            if source == "hand-written" and (type(confidence) is not float or confidence != 0.9):
                found.add(where, "a hand-written answer has confidence 0.9")
    if len(found.lines) > before:
        return None
    return str(source), model, wire


def _plain(value: Any) -> bool:
    """A truth value: null, a string, a number, or the acceptable readings (strings, one or
    more)."""
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, int | float) and not isinstance(value, bool):
        return True
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def _choice(value: Any, choices: tuple[str, ...], *, readings: bool = True) -> bool:
    if isinstance(value, str):
        return value in choices
    return (
        readings
        and isinstance(value, list)
        and bool(value)
        and all(isinstance(v, str) and v in choices for v in value)
    )


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _in_order(value: Any, known: tuple[str, ...]) -> bool:
    """A list of codes of `known`, each once, in `known`'s order."""
    return (
        isinstance(value, list)
        and all(isinstance(v, str) for v in value)
        and value == [code for code in known if code in value]
    )


def _check_fields(
    doc: dict, model: type[pydantic.BaseModel], at: str, where: str, found: _Problems
) -> None:
    for key, value in doc.items():
        if key not in model.model_fields:
            found.add(where, f"{at}: {key} is no field of {model.__name__}")
        elif key == "kind":
            if not _choice(value, KINDS, readings=False):
                found.add(where, f"{at}: kind is not one of {', '.join(KINDS)}")
        elif key == "status":
            if value is not None and not _choice(value, STATUSES):
                found.add(where, f"{at}: status is not one of {', '.join(STATUSES)}")
        elif key == "passengers":
            if not _names(value):
                found.add(where, f"{at}: passengers is not a list of names")
        elif key == "segments":
            if not isinstance(value, list) or not all(isinstance(s, dict) for s in value):
                found.add(where, f"{at}: segments is not a list of objects")
                continue
            for i, seg in enumerate(value):
                _check_fields(seg, Segment, f"{at}.segments[{i}]", where, found)
        elif not _plain(value):
            found.add(
                where,
                f"{at}: {key} is not null, a string, a number or a list of acceptable readings",
            )


def _read_truth(path: Path, where: str, found: _Problems) -> dict | None:
    doc = _json(path, where, found)
    if doc is _UNREAD:
        return None
    if not isinstance(doc, dict):
        found.add(where, "not an object")
        return None
    before = len(found.lines)
    if why := _exact_keys(doc, TRUTH_KEYS):
        found.add(where, why)
    if not _choice(doc.get("document_type"), DOCUMENT_TYPES):
        found.add(where, "document_type is not a document type or a list of them")
    reservations = doc.get("reservations")
    if not isinstance(reservations, list):
        found.add(where, "reservations is not a list")
        reservations = []
    for i, res in enumerate(reservations):
        at = f"reservations[{i}]"
        if not isinstance(res, dict):
            found.add(where, f"{at}: not an object")
            continue
        if "kind" not in res:
            found.add(where, f"{at}: no kind")
        _check_fields(res, Reservation, at, where, found)
    return None if len(found.lines) > before else doc


def _count(value: Any, *, most: int | None = None) -> bool:
    return type(value) is int and value >= 0 and (most is None or value <= most)


def _check_item(item: Any, at: str, where: str, found: _Problems) -> None:
    if not isinstance(item, dict):
        found.add(where, f"{at}: not an object")
        return
    if why := _exact_keys(item, ITEM_KEYS):
        found.add(where, f"{at}: {why}")
    if item.get("kind") not in KINDS:
        found.add(where, f"{at}: kind is not one of {', '.join(KINDS)}")
    if not (isinstance(item.get("key"), str) and item["key"]):
        found.add(where, f"{at}: key is not a booking key")
    if item.get("result") not in RESULTS:
        found.add(where, f"{at}: result is not one of {', '.join(RESULTS)}")
    people = item.get("people")
    if not (
        isinstance(people, dict)
        and _exact_keys(people, PEOPLE_KEYS) is None
        and _count(people["me"], most=1)
        and _count(people["guests"])
        and _count(people["others"])
    ):
        found.add(where, f"{at}: people is not {{me: 0 or 1, guests: n, others: n}}")
    booking = item.get("booking")
    if booking is not None and not (
        isinstance(booking, dict)
        and _exact_keys(booking, BOOKING_KEYS) is None
        and isinstance(booking["status"], str)
        and booking["status"]
        and _names(booking["travellers"])
        and booking["travellers"] == sorted(booking["travellers"])
        and (booking["trip"] is None or isinstance(booking["trip"], str))
    ):
        found.add(
            where,
            f"{at}: booking is not null or {{status, travellers (sorted), trip, checks, sync}}",
        )
    if isinstance(booking, dict):
        if "checks" in booking and not _in_order(booking["checks"], checks.ALL):
            found.add(where, f"{at}: booking.checks is not a list of known checks in their order")
        if "sync" in booking and booking["sync"] not in checks.SYNC_STATES:
            found.add(
                where, f"{at}: booking.sync is not one of {', '.join(checks.SYNC_STATES)}"
            )


def _read_outcome(path: Path, where: str, found: _Problems) -> dict | None:
    doc = _json(path, where, found)
    if doc is _UNREAD:
        return None
    if not isinstance(doc, dict):
        found.add(where, "not an object")
        return None
    before = len(found.lines)
    if why := _exact_keys(doc, OUTCOME_KEYS, optional=("gap",)):
        found.add(where, why)
    if doc.get("status") not in MESSAGE_STATUSES:
        found.add(where, f"status is not one of {', '.join(MESSAGE_STATUSES)}")
    if doc.get("read_by") is not None and doc.get("read_by") not in READERS:
        found.add(where, f"read_by is not one of {', '.join(READERS)}, or null")
    if doc.get("document_type") is not None and doc.get("document_type") not in DOCUMENT_TYPES:
        found.add(where, "document_type is not a document type, or null")
    items = doc.get("items")
    if not isinstance(items, list):
        found.add(where, "items is not a list")
        items = []
    for i, item in enumerate(items):
        _check_item(item, f"items[{i}]", where, found)
    order = [
        (item["kind"], item["key"])
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("kind"), str)
        and isinstance(item.get("key"), str)
    ]
    if order != sorted(order):
        found.add(where, "items are not sorted by kind, then key")
    if "gap" in doc and not (isinstance(doc["gap"], str) and doc["gap"] in GAPS):
        found.add(where, f"gap is not one of {', '.join(GAPS) or 'the known gaps (none today)'}")
    return None if len(found.lines) > before else doc


def _cross_check(
    answer: tuple[str, str | None, WireExtraction | None],
    outcome: dict,
    where: str,
    found: _Problems,
) -> None:
    source, _, wire = answer
    read_by = outcome["read_by"]
    if read_by == "model" and wire is None:
        found.add(where, "read by the model, but answer.json holds no answer")
    if read_by == "kitinerary" and source != "not-asked":
        found.add(where, f"read by kitinerary, but answer.json holds a {source} answer")
    if outcome["status"] == "rejected" and (
        read_by is not None or outcome["items"] or source != "not-asked"
    ):
        found.add(where, "a rejected email is read by nobody, files nothing and is not asked")


def _check_drafts(raw: bytes, outcome: dict, where: str, found: _Problems) -> None:
    """A draft key names the entry's own email: the draft key of its SHA-256 and a place."""
    digest = hashlib.sha256(raw).hexdigest()
    for i, item in enumerate(outcome["items"]):
        key = item["key"]
        if not is_draft(key):
            continue
        place = key.rsplit("|", 1)[-1]
        if not (place.isdecimal() and key == draft_key(digest, int(place))):
            what = "a draft key carries the first 16 hex digits of the email's SHA-256"
            found.add(where, f"items.{i}.key: {what}")


# ----------------------------------------------------------------------------- reading it as mail
def settings(
    corpus: Corpus,
    *,
    data_dir: Path,
    archive_dir: Path,
    logs_dir: Path,
    config_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """The settings the corpus is read under: its own, its travellers, the three directories,
    writes on, no sign-in, then the caller's extras (such as the model key). The
    process environment is never read. Any problem raises CorpusError."""
    env = {
        **corpus.settings,
        "TRIPBOX_CONFIG_DIR": str(config_dir or corpus.root / CONFIG_DIR),
        "TRIPBOX_DATA_DIR": str(data_dir),
        "TRIPBOX_ARCHIVE_DIR": str(archive_dir),
        "TRIPBOX_LOGS_DIR": str(logs_dir),
        "TRIPBOX_AUTH": "none",
        **(environ or {}),
    }
    s = Settings.from_env(env)
    if s.problems:
        raise CorpusError("\n".join(f"{CORPUS_FILE}: settings: {p}" for p in s.problems))
    return s


_PROCESS_NAMES = ("TZ", "TRIPBOX_CONFIG_DIR")


@contextlib.contextmanager
def process_as(settings: Settings) -> Iterator[None]:
    """For its duration the process's zone is the settings' and TRIPBOX_CONFIG_DIR names their
    config directory: the zone and the list overrides are read from the process, not from
    Settings. Both variables and the zone are as before afterwards; the list caches are dropped
    on the way in and out."""
    saved = {name: os.environ.get(name) for name in _PROCESS_NAMES}
    lists.clear_caches()
    try:
        os.environ["TRIPBOX_CONFIG_DIR"] = str(settings.config_dir)
        use_zone(settings.tz)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        time.tzset()
        lists.clear_caches()


class CorpusAnswers:
    """The model extractor's interface, answered from the corpus: the answer of the entry whose
    email has the message's bytes (matched by SHA-256, computed from the bytes read at load),
    mapped with `to_extraction`, at zero cost. A message that is no entry, or an entry whose
    model is not asked, has no answer."""

    def __init__(self, corpus: Corpus, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._by_sha = {hashlib.sha256(e.raw).hexdigest(): e for e in corpus.entries}

    def extract(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[Extraction, Usage]:
        entry = self._by_sha.get(hashlib.sha256(nm.raw).hexdigest())
        if entry is None or entry.answer is None:
            raise ModelError("invalid", NO_ANSWER)
        return to_extraction(entry.answer), Usage(self.model, 0, 0, 0.0, asked=False)
