# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Recorded model answers: a rehearsal replays them instead of calling the model, so a run over
saved emails is deterministic, free and needs no network.

An answer is the model's reply in the wire schema (`WireExtraction`) exactly as it arrived, in
`<sha256 of the email's raw bytes>.<WIRE_SCHEMA_VERSION>.json`, mode 600. A change to the wire
schema bumps the version, so an answer recorded under the old schema is never read as the new one.
The shape of each version is frozen in `tests/data/wire_schema/`.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import pydantic

from tripbox.config import ConfigError
from tripbox.mail.normalize import NormalizedMessage
from tripbox.parse.models import Extraction, WireExtraction, to_extraction
from tripbox.parse.provider import DEFAULT_MODEL, ModelError, Provider, Usage

log = logging.getLogger("tripbox.replay")

WIRE_SCHEMA_VERSION: int = 1
# The errors a replay raises, each a ModelError of kind `invalid` whose text is one of these: no
# stored answer, one that cannot be read, one that does not fit the wire schema. The rehearsal
# counts such a message as failed, whatever the pipeline made of kitinerary's partial reading, and
# reports it with the problem code each maps to.
NO_ANSWER = "no recorded answer"
UNREADABLE = "recorded answer unreadable"
OFF_SCHEMA = f"recorded answer does not fit wire schema {WIRE_SCHEMA_VERSION}"
REPLAY_FAILURES = {
    NO_ANSWER: "no_recorded_answer",
    UNREADABLE: "unreadable_recorded_answer",
    OFF_SCHEMA: "recorded_answer_off_schema",
}


def answer_path(root: Path, raw: bytes) -> Path:
    return root / f"{hashlib.sha256(raw).hexdigest()}.{WIRE_SCHEMA_VERSION}.json"


def _answers_dir(root: Path, setting: str) -> Path:
    root = Path(root)
    if not root.is_dir():
        raise ConfigError(f"{setting} is not a directory")
    return root


def load_answer(path: Path) -> WireExtraction:
    """The stored wire answer. An error never quotes the file, which holds what the email says."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ModelError("invalid", NO_ANSWER) from None
    except OSError:
        raise ModelError("invalid", UNREADABLE) from None
    try:
        return WireExtraction.model_validate_json(data)
    except pydantic.ValidationError:
        raise ModelError("invalid", OFF_SCHEMA) from None


def _store(path: Path, wire: WireExtraction) -> None:
    """Owner-only, and complete or not at all: a temporary file renamed into place."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(wire.model_dump_json().encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _free(model: str) -> Usage:
    """An answer read back from a file: no model was asked, so the cap never counts it."""
    return Usage(model=model, input_tokens=0, output_tokens=0, cost_usd=0.0, asked=False)


class ReplayExtractor:
    """The model extractor's interface, answered from recorded wire answers: the stored answer for
    the message's raw bytes, mapped with `to_extraction`, at zero cost. The pipeline tags the result
    as read by the model (`READ_BY_MODEL`) as it does a live answer, so every rule for model
    answers (the confidence floor) applies to a replayed one unchanged."""

    def __init__(self, root: Path, model: str = DEFAULT_MODEL) -> None:
        self.root = _answers_dir(root, "TRIPBOX_REPLAY_DIR")
        self.model = model

    def extract(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[Extraction, Usage]:
        wire = load_answer(answer_path(self.root, nm.raw))
        log.info("replayed a recorded answer: %d reservations", len(wire.reservations))
        return to_extraction(wire), _free(self.model)


class RecordingExtractor:
    """Wraps the model extractor. A message without a stored answer is sent to the model and its
    wire answer is stored before the result is returned; a message that has one is answered from
    it without calling the model, so recording again pays only for what is missing."""

    def __init__(self, inner: Provider, root: Path) -> None:
        self.inner = inner
        self.root = _answers_dir(root, "TRIPBOX_RECORD_DIR")
        self.model = inner.model

    def extract(
        self, nm: NormalizedMessage, hint: Extraction | None = None
    ) -> tuple[Extraction, Usage]:
        path = answer_path(self.root, nm.raw)
        if path.exists():
            wire = load_answer(path)
            log.info("answer already recorded: %d reservations", len(wire.reservations))
            return to_extraction(wire), _free(self.model)
        wire, usage = self.inner.extract_wire(nm, hint)
        _store(path, wire)
        log.info(
            "recorded an answer: %d reservations ($%.4f)", len(wire.reservations), usage.cost_usd
        )
        return to_extraction(wire), usage
