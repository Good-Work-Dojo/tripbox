# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The corpus (`tests/corpus/`): invented travel emails with known right answers. The loader names
every mistake with its file; the entries keep the invention conventions; the markup entries are
read whole by the deterministic extractor; a hand-written answer is found by the email's own bytes;
and the corpus is read under its own settings, never the process's."""

import dataclasses
import email
import email.policy
import email.utils
import hashlib
import json
import os
import re
import shutil
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import pytest
from helpers.eml import make_eml

import tripbox
from tripbox import corpus, lists
from tripbox.mail.normalize import NormalizedMessage, normalize
from tripbox.parse import replay
from tripbox.parse.anthropic_provider import AnthropicProvider
from tripbox.parse.models import to_extraction
from tripbox.parse.pipeline import parse_message
from tripbox.parse.provider import (
    BODY_HEADING,
    DEFAULT_MODEL,
    MAX_TEXT_CHARS,
    ModelError,
    Usage,
    build_text,
)
from tripbox.validate import checks
from tripbox.validate.keys import draft_key
from tripbox.validate.passengers import first_last

FIRST_NAMES = {"ALEX", "SAM", "ROBIN", "JAMIE", "PAT", "LEE"}
LAST_NAMES = {"SAMPLE", "EXAMPLE"}
INVENTED_DOMAINS = ("example.com", "example.org", "example.net")
YEAR = re.compile(r"\b(?:19|20)\d\d\b")
DAY = re.compile(r"\b\d{4}-\d{2}-\d{2}")
ADDRESS = re.compile(r"[\w.+=-]+@[\w.-]+")


def dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "data_dir": tmp_path / "data",
        "archive_dir": tmp_path / "archive",
        "logs_dir": tmp_path / "logs",
    }


def read(raw: bytes, s) -> NormalizedMessage:
    """The message as the mailbox reads it under the corpus's settings."""
    return normalize(
        raw,
        s.allowed_senders,
        s.tz,
        require_auth=s.require_google_auth,
        trusted_domains=s.mail_trusted_sender_domains,
    )


def edit_json(path: Path, change) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _put(*at, value):
    """A change that sets the value at the path `at` (keys and indexes) of a JSON document."""

    def change(doc):
        for step in at[:-1]:
            doc = doc[step]
        doc[at[-1]] = value

    return change


def _drop(*at):
    """A change that removes the key at the path `at` of a JSON document."""

    def change(doc):
        for step in at[:-1]:
            doc = doc[step]
        del doc[at[-1]]

    return change


def readings(value) -> list:
    """A truth value is one reading or a list of acceptable ones."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def test_the_corpus_loads_and_every_entry_is_complete():
    root = corpus.default_dir()
    assert corpus.problems(root) == []
    c = corpus.load(root)
    names = [e.name for e in c.entries]
    assert len(names) >= 3 and names == sorted(names)
    for e in c.entries:
        assert e.raw == (root / e.name / "email.eml").read_bytes()
        assert set(e.outcome) == {"status", "read_by", "document_type", "items"}
    assert c.entry("05-chain-hotel").answer_source == "hand-written"
    with pytest.raises(KeyError):
        c.entry("99-no-such-entry")


# The entries whose booking the story marks for a look, with the checks open on it once the email
# is filed (SCENARIOS.md).
MARKED = {
    "19-unknown-cancellation": ("unknown_cancellation",),
    "25-flight-without-date": ("departure",),
}


def test_the_corpus_marks_exactly_the_bookings_the_story_marks():
    """The two marked entries' bookings carry their checks and no other booking carries one; the
    corpus is read with no flight log, so every booking's sync is off."""
    c = corpus.load(corpus.default_dir())
    want = set()
    for name, codes in MARKED.items():
        (item,) = c.entry(name).outcome["items"]
        want.add((name, item["key"], codes))
    bookings = [
        (e.name, i["key"], i["booking"]) for e in c.entries for i in e.outcome["items"]
        if i["booking"] is not None
    ]
    marked = {(name, key, tuple(b["checks"])) for name, key, b in bookings if b["checks"]}
    assert marked == want
    assert {b["sync"] for _, _, b in bookings} == {"off"}


def test_the_readme_lists_every_check_with_its_sentence():
    """The README's table of checks, under outcome.json, is the code's own: every check once, in
    their order, hard or soft as the code has it, with the sentence the page shows."""
    readme = (corpus.default_dir() / "README.md").read_text(encoding="utf-8")
    section = readme.split("\n## outcome.json\n", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \| (hard|soft) \| ([^|]+?) \|", section, flags=re.M)
    assert rows == [
        (code, "hard" if code in checks.HARD else "soft", checks.LABELS[code])
        for code in checks.ALL
    ]


def _set_wire_schema(doc):
    doc["wire_schema"] = 0


def _not_asked_with_answer(doc):
    doc["source"] = "not-asked"


def _answer_key_the_schema_lacks(doc):
    doc["answer"]["reservations"][0]["room"] = "a room"


def _reservation_key_that_is_no_field(doc):
    doc["reservations"][0]["hotel"] = "a hotel"


def _result_outside_results(doc):
    doc["items"][0]["result"] = "written"


def _items_out_of_order(doc):
    doc["items"].reverse()


def _unknown_gap(doc):
    doc["gap"] = "anything"


def _kitinerary_beside_an_answer(doc):
    doc["read_by"] = "kitinerary"


def _demo_entry_that_does_not_exist(doc):
    doc["demo"]["entries"] = ["99-no-such-entry"]


def _setting_outside_the_names(doc):
    doc["settings"]["TRIPBOX_AIRTRAIL_DRY_RUN"] = "true"


def _gap_as_a_list(doc):
    doc["gap"] = ["anything"]


def _rejected_with_an_item(doc):
    doc["items"] = [
        {
            "kind": "lodging",
            "key": "lodging|ZWT876",
            "result": "filed",
            "people": {"me": 1, "guests": 0, "others": 0},
            "booking": None,
        }
    ]


def _read_by_the_model(doc):
    doc["read_by"] = "model"


def _people_with_a_wrong_key(doc):
    doc["items"][0]["people"] = {"me": 1, "guests": 0, "strangers": 0}


def _travellers_unsorted(doc):
    doc["items"][0]["booking"]["travellers"].reverse()


def _recorded_without_a_model(doc):
    doc["source"] = "recorded"


def _hand_written_with_a_model(doc):
    doc["model"] = "a-model"


def _answer_key_the_schema_lacks_in_a_leg(doc):
    doc["answer"]["reservations"][0]["legs"] = [{"gate": "B4"}]


def _hand_written_confidence_not_point_nine(doc):
    doc["answer"]["confidence"] = 0.95


def _draft_key_of_another_email(root: Path):
    """Give the first booking of an outcome the draft key another entry's email would carry."""
    other = hashlib.sha256((root / "01-markup-flight" / "email.eml").read_bytes()).hexdigest()

    def change(doc):
        doc["items"][0]["key"] = draft_key(other, 0)

    return change


def _two_attached_emails(root: Path) -> None:
    path = root / "05-chain-hotel" / "email.eml"
    raw = path.read_bytes()
    original = email.message_from_bytes(raw, policy=email.policy.default)
    carrier = EmailMessage()
    for header in ("From", "To", "Subject", "Date", "Message-ID"):
        carrier[header] = str(original[header])
    carrier.set_content("Two emails attached.")
    for _ in range(2):
        carrier.add_attachment(email.message_from_bytes(raw, policy=email.policy.default))
    path.write_bytes(carrier.as_bytes())


def _linked_entry(root: Path) -> None:
    (root / "22-linked").symlink_to(root / "21-stranger", target_is_directory=True)


def _linked(path: Path, elsewhere: Path) -> None:
    """Put a link in the place of a file or a directory, to a copy of it elsewhere."""
    if path.is_dir():
        shutil.copytree(path, elsewhere)
        shutil.rmtree(path)
    else:
        elsewhere.write_bytes(path.read_bytes())
        path.unlink()
    path.symlink_to(elsewhere, target_is_directory=elsewhere.is_dir())


def _directory_in_place(path: Path) -> None:
    path.unlink()
    path.mkdir()


def _without_header(path: Path, name: bytes) -> None:
    """Remove the email's first header of that name, with any folded lines of it."""
    raw = path.read_bytes()
    header = rb"^" + re.escape(name) + rb":[^\r\n]*\r?\n(?:[ \t][^\r\n]*\r?\n)*"
    path.write_bytes(re.sub(header, b"", raw, count=1, flags=re.M))


def _in_envelopes(path: Path) -> None:
    """Wrap the email in more message/rfc822 envelopes than the parser can descend through."""
    layers = 2 * sys.getrecursionlimit()
    path.write_bytes(b"Content-Type: message/rfc822\n\n" * layers + path.read_bytes())


def _refuse(patch: pytest.MonkeyPatch, owner, name: str, path: Path) -> None:
    """Make `owner.name`, a function of a path (or a method of one), fail for `path` alone as
    the system refuses what its user may not read, whatever user runs the test."""
    real = getattr(owner, name)

    def refusing(target, *args, **kwargs):
        if Path(target) == path:
            raise PermissionError(f"{target}: permission denied")
        return real(target, *args, **kwargs)

    patch.setattr(owner, name, refusing)


def _problem_lines(root: Path) -> list[str] | None:
    """The problems a load of `root` refuses it with, or None when it loads. A corpus with no
    corpus.json is refused before anything is read, so its problems are asked for."""
    try:
        corpus.load(root)
    except corpus.CorpusError as err:
        lines = str(err).splitlines()
        return corpus.problems(root) if lines == [f"{root}: no corpus here"] else lines
    return None


def _dated_before_the_entry_ahead(root: Path) -> None:
    path = root / "05-chain-hotel" / "email.eml"
    raw = path.read_bytes()
    earlier = b"Date: Sun, 03 Jan 2027 08:00:00 -0600"
    path.write_bytes(re.sub(rb"^Date: [^\r\n]*", earlier, raw, count=1, flags=re.M))


def _oversize(root: Path) -> None:
    path = root / "05-chain-hotel" / "email.eml"
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * (corpus.MAX_EMAIL_BYTES + 1 - len(raw)))


def test_each_kind_of_mistake_is_named_with_its_file(tmp_path, monkeypatch):
    source = corpus.default_dir()
    root = tmp_path / "corpus"
    hotel, flight = root / "05-chain-hotel", root / "01-markup-flight"
    spoilers = [
        ("05-chain-hotel/answer.json:", lambda: (hotel / "answer.json").unlink()),
        ("05-chain-hotel/notes.txt:", lambda: (hotel / "notes.txt").write_text("a note\n")),
        ("5-bad:", lambda: (root / "5-bad").mkdir()),
        (
            "05-chain-hotel/answer.json: written for wire schema 0",
            lambda: edit_json(hotel / "answer.json", _set_wire_schema),
        ),
        (
            "05-chain-hotel/answer.json:",
            lambda: edit_json(hotel / "answer.json", _not_asked_with_answer),
        ),
        (
            "05-chain-hotel/answer.json:",
            lambda: edit_json(hotel / "answer.json", _answer_key_the_schema_lacks),
        ),
        (
            "05-chain-hotel/extraction.json:",
            lambda: edit_json(hotel / "extraction.json", _reservation_key_that_is_no_field),
        ),
        (
            "05-chain-hotel/outcome.json:",
            lambda: edit_json(hotel / "outcome.json", _result_outside_results),
        ),
        (
            "01-markup-flight/outcome.json:",
            lambda: edit_json(flight / "outcome.json", _items_out_of_order),
        ),
        ("05-chain-hotel/outcome.json:", lambda: edit_json(hotel / "outcome.json", _unknown_gap)),
        (
            "05-chain-hotel/outcome.json:",
            lambda: edit_json(hotel / "outcome.json", _kitinerary_beside_an_answer),
        ),
        ("corpus.json:", lambda: edit_json(root / "corpus.json", _demo_entry_that_does_not_exist)),
        ("corpus.json:", lambda: edit_json(root / "corpus.json", _setting_outside_the_names)),
        ("05-chain-hotel/email.eml:", lambda: _dated_before_the_entry_ahead(root)),
        ("05-chain-hotel/email.eml:", lambda: _oversize(root)),
        (
            "05-chain-hotel/outcome.json: gap is not one of",
            lambda: edit_json(hotel / "outcome.json", _gap_as_a_list),
        ),
        ("22-linked: is a link", lambda: _linked_entry(root)),
        (
            "05-chain-hotel/email.eml: is a link",
            lambda: _linked(hotel / "email.eml", tmp_path / "elsewhere.eml"),
        ),
        (
            "21-stranger/outcome.json: a rejected email",
            lambda: edit_json(root / "21-stranger" / "outcome.json", _rejected_with_an_item),
        ),
        (
            "01-markup-flight/outcome.json: read by the model",
            lambda: edit_json(flight / "outcome.json", _read_by_the_model),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: people",
            lambda: edit_json(hotel / "outcome.json", _people_with_a_wrong_key),
        ),
        (
            "01-markup-flight/outcome.json: items[0]: booking",
            lambda: edit_json(flight / "outcome.json", _travellers_unsorted),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: booking is not null or",
            lambda: edit_json(hotel / "outcome.json", _drop("items", 0, "booking", "sync")),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: booking.checks is not a list of known checks"
            " in their order",
            lambda: edit_json(
                hotel / "outcome.json", _put("items", 0, "booking", "checks", value=["a look"])
            ),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: booking.checks is not a list of known checks"
            " in their order",
            lambda: edit_json(
                hotel / "outcome.json",
                _put("items", 0, "booking", "checks", value=[checks.SOFT[0], checks.HARD[0]]),
            ),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: booking.sync is not one of off, blocked,"
            " pending, synced",
            lambda: edit_json(
                hotel / "outcome.json", _put("items", 0, "booking", "sync", value="sent")
            ),
        ),
        (
            "05-chain-hotel/answer.json: a recorded answer",
            lambda: edit_json(hotel / "answer.json", _recorded_without_a_model),
        ),
        (
            "05-chain-hotel/answer.json: only a recorded answer",
            lambda: edit_json(hotel / "answer.json", _hand_written_with_a_model),
        ),
        (
            "05-chain-hotel/answer.json: answer.reservations[0].legs[0].gate",
            lambda: edit_json(hotel / "answer.json", _answer_key_the_schema_lacks_in_a_leg),
        ),
        (
            "05-chain-hotel/answer.json: a hand-written answer has confidence 0.9",
            lambda: edit_json(hotel / "answer.json", _hand_written_confidence_not_point_nine),
        ),
        (
            "05-chain-hotel/email.eml: carries more than one attached email",
            lambda: _two_attached_emails(root),
        ),
        (
            "25-flight-without-date/outcome.json: items.0.key: a draft key carries",
            lambda: edit_json(
                root / "25-flight-without-date" / "outcome.json",
                _draft_key_of_another_email(root),
            ),
        ),
        # the corpus root: nothing behind a link is read, nor counted as absent
        ("config/travellers.yaml: missing", lambda: (root / corpus.TRAVELLERS).unlink()),
        (
            "config/travellers.yaml: is a link",
            lambda: _linked(root / corpus.TRAVELLERS, tmp_path / "elsewhere-travellers.yaml"),
        ),
        ("config: is a link", lambda: _linked(root / "config", tmp_path / "elsewhere-config")),
        (
            "05-copy: number 05 is taken by 05-chain-hotel",
            lambda: shutil.copytree(hotel, root / "05-copy"),
        ),
        ("31-dangling: is a link", lambda: (root / "31-dangling").symlink_to(tmp_path / "gone")),
        ("32-looping: is a link", lambda: (root / "32-looping").symlink_to(root / "32-looping")),
        # corpus.json
        ("corpus.json: missing", lambda: (root / "corpus.json").unlink()),
        (
            "corpus.json: is a link",
            lambda: _linked(root / "corpus.json", tmp_path / "elsewhere-corpus.json"),
        ),
        ("corpus.json: not readable as JSON", lambda: (root / "corpus.json").write_text("{")),
        ("corpus.json: not an object", lambda: (root / "corpus.json").write_text("[]")),
        (
            "corpus.json: has notes, which is not one of its keys",
            lambda: edit_json(root / "corpus.json", _put("notes", value="a note")),
        ),
        (
            "corpus.json: settings is not an object",
            lambda: edit_json(root / "corpus.json", _put("settings", value=[])),
        ),
        (
            "corpus.json: settings: TZ is not a string",
            lambda: edit_json(root / "corpus.json", _put("settings", "TZ", value=1)),
        ),
        (
            "corpus.json: demo is not an object",
            lambda: edit_json(root / "corpus.json", _put("demo", value=[])),
        ),
        (
            "corpus.json: demo lacks anchor",
            lambda: edit_json(root / "corpus.json", _drop("demo", "anchor")),
        ),
        (
            "corpus.json: demo: anchor is not a day",
            lambda: edit_json(root / "corpus.json", _put("demo", "anchor", value="tomorrow")),
        ),
        (
            "corpus.json: demo: entries is not a list of entry names",
            lambda: edit_json(root / "corpus.json", _put("demo", "entries", value="all")),
        ),
        (
            "corpus.json: demo: entries do not ascend",
            lambda: edit_json(root / "corpus.json", lambda doc: doc["demo"]["entries"].reverse()),
        ),
        # an entry's files
        (
            "05-chain-hotel/answer.json: is a link",
            lambda: _linked(hotel / "answer.json", tmp_path / "elsewhere-answer.json"),
        ),
        (
            "05-chain-hotel/extraction.json: is a link",
            lambda: _linked(hotel / "extraction.json", tmp_path / "elsewhere-extraction.json"),
        ),
        (
            "05-chain-hotel/outcome.json: is a link",
            lambda: _linked(hotel / "outcome.json", tmp_path / "elsewhere-outcome.json"),
        ),
        (
            "05-chain-hotel/outcome.json: not a file",
            lambda: _directory_in_place(hotel / "outcome.json"),
        ),
        # email.eml
        (
            "05-chain-hotel/email.eml: does not parse as an email",
            lambda: _in_envelopes(hotel / "email.eml"),
        ),
        (
            "05-chain-hotel/email.eml: no Message-ID header",
            lambda: _without_header(hotel / "email.eml", b"Message-ID"),
        ),
        (
            "05-chain-hotel/email.eml: no Date header that reads as a date",
            lambda: _without_header(hotel / "email.eml", b"Date"),
        ),
        # answer.json
        (
            "05-chain-hotel/answer.json: not readable as JSON",
            lambda: (hotel / "answer.json").write_text("{"),
        ),
        (
            "05-chain-hotel/answer.json: not an object",
            lambda: (hotel / "answer.json").write_text("[]"),
        ),
        (
            "05-chain-hotel/answer.json: has notes, which is not one of its keys",
            lambda: edit_json(hotel / "answer.json", _put("notes", value="a note")),
        ),
        (
            "05-chain-hotel/answer.json: source is not one of",
            lambda: edit_json(hotel / "answer.json", _put("source", value="guessed")),
        ),
        (
            "05-chain-hotel/answer.json: answer is not an object",
            lambda: edit_json(hotel / "answer.json", _put("answer", value=["a reading"])),
        ),
        (
            "05-chain-hotel/answer.json: answer does not fit the wire schema",
            lambda: edit_json(hotel / "answer.json", _put("answer", "reservations", value="none")),
        ),
        # extraction.json
        (
            "05-chain-hotel/extraction.json: not readable as JSON",
            lambda: (hotel / "extraction.json").write_text("{"),
        ),
        (
            "05-chain-hotel/extraction.json: not an object",
            lambda: (hotel / "extraction.json").write_text("[]"),
        ),
        (
            "05-chain-hotel/extraction.json: has notes, which is not one of its keys",
            lambda: edit_json(hotel / "extraction.json", _put("notes", value="a note")),
        ),
        (
            "05-chain-hotel/extraction.json: document_type is not a document type",
            lambda: edit_json(hotel / "extraction.json", _put("document_type", value="a letter")),
        ),
        (
            "05-chain-hotel/extraction.json: reservations is not a list",
            lambda: edit_json(hotel / "extraction.json", _put("reservations", value="none")),
        ),
        (
            "05-chain-hotel/extraction.json: reservations[0]: not an object",
            lambda: edit_json(hotel / "extraction.json", _put("reservations", 0, value="a stay")),
        ),
        (
            "05-chain-hotel/extraction.json: reservations[0]: no kind",
            lambda: edit_json(hotel / "extraction.json", _drop("reservations", 0, "kind")),
        ),
        (
            "05-chain-hotel/extraction.json: reservations[0]: kind is not one of",
            lambda: edit_json(
                hotel / "extraction.json", _put("reservations", 0, "kind", value="boat")
            ),
        ),
        (
            "05-chain-hotel/extraction.json: reservations[0]: status is not one of",
            lambda: edit_json(
                hotel / "extraction.json", _put("reservations", 0, "status", value="maybe")
            ),
        ),
        (
            "05-chain-hotel/extraction.json: reservations[0]: passengers is not a list of names",
            lambda: edit_json(
                hotel / "extraction.json", _put("reservations", 0, "passengers", value="a name")
            ),
        ),
        (
            "05-chain-hotel/extraction.json: reservations[0]: segments is not a list of objects",
            lambda: edit_json(
                hotel / "extraction.json", _put("reservations", 0, "segments", value=["a leg"])
            ),
        ),
        (
            "05-chain-hotel/extraction.json: reservations[0]: price is not null, a string",
            lambda: edit_json(
                hotel / "extraction.json", _put("reservations", 0, "price", value=True)
            ),
        ),
        # outcome.json
        (
            "05-chain-hotel/outcome.json: not readable as JSON",
            lambda: (hotel / "outcome.json").write_text("{"),
        ),
        (
            "05-chain-hotel/outcome.json: not an object",
            lambda: (hotel / "outcome.json").write_text("[]"),
        ),
        (
            "05-chain-hotel/outcome.json: has notes, which is not one of its keys",
            lambda: edit_json(hotel / "outcome.json", _put("notes", value="a note")),
        ),
        (
            "05-chain-hotel/outcome.json: status is not one of",
            lambda: edit_json(hotel / "outcome.json", _put("status", value="lost")),
        ),
        (
            "05-chain-hotel/outcome.json: read_by is not one of",
            lambda: edit_json(hotel / "outcome.json", _put("read_by", value="a person")),
        ),
        (
            "05-chain-hotel/outcome.json: document_type is not a document type, or null",
            lambda: edit_json(hotel / "outcome.json", _put("document_type", value="a letter")),
        ),
        (
            "05-chain-hotel/outcome.json: items is not a list",
            lambda: edit_json(hotel / "outcome.json", _put("items", value="none")),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: not an object",
            lambda: edit_json(hotel / "outcome.json", _put("items", 0, value="a booking")),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: has notes, which is not one of its keys",
            lambda: edit_json(hotel / "outcome.json", _put("items", 0, "notes", value="a note")),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: kind is not one of",
            lambda: edit_json(hotel / "outcome.json", _put("items", 0, "kind", value="boat")),
        ),
        (
            "05-chain-hotel/outcome.json: items[0]: key is not a booking key",
            lambda: edit_json(hotel / "outcome.json", _put("items", 0, "key", value="")),
        ),
        # what the system refuses its user is refused in the process, for that one path, so
        # these run whatever user runs the suite (root reads and lists whatever the mode)
        (
            "05-chain-hotel: cannot be listed",
            lambda: _refuse(monkeypatch, Path, "iterdir", hotel),
        ),
        (
            "05-chain-hotel/email.eml: cannot be read",
            lambda: _refuse(monkeypatch, corpus, "_file_bytes", hotel / "email.eml"),
        ),
        (
            "05-chain-hotel/answer.json: cannot be read",
            lambda: _refuse(monkeypatch, corpus, "_file_bytes", hotel / "answer.json"),
        ),
    ]
    assert corpus.problems(source) == []  # so every copy of it is clean before it is spoiled
    missed = []
    for n, (prefix, spoil) in enumerate(spoilers):
        shutil.copytree(source, root)
        spoil()
        try:
            found = _problem_lines(root)
        finally:
            monkeypatch.undo()
        if found is None:
            missed.append(f"spoiler {n} ({prefix}): the spoiled corpus loads")
        elif not any(line.startswith(prefix) for line in found):
            missed.append(f"spoiler {n} ({prefix}): {found}")
        shutil.rmtree(root)
    assert not missed, "\n".join(missed)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(corpus.CorpusError) as err:
        corpus.load(empty)
    assert str(err.value).endswith("no corpus here")


def _texts(msg) -> tuple[list[str], list[str]]:
    """Every header value, and every text/plain and text/html part decoded, of the message and of
    any email it carries."""
    headers: list[str] = []
    parts: list[str] = []
    for part in msg.walk():
        headers.extend(str(value) for value in part.values())
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_content())
            except LookupError:
                raw = part.get_payload(decode=True) or b""
                parts.append(raw.decode("utf-8", errors="replace"))
    return headers, parts


def _invented_domain(domain: str) -> bool:
    return (
        domain in INVENTED_DOMAINS
        or domain.endswith(".example")
        or any(domain == d or domain.endswith("." + d) for d in lists.trusted_sender_domains())
    )


def test_the_corpus_keeps_the_invention_conventions():
    c = corpus.load(corpus.default_dir())
    breaches: list[str] = []
    for e in c.entries:
        reservations = e.truth["reservations"]
        names = [n for r in reservations for n in r.get("passengers", [])]
        for r in reservations:
            names += [n for s in r.get("segments", []) for n in s.get("passengers", [])]
        for name in names:
            fl = first_last(name)
            if fl is None or fl[0] not in FIRST_NAMES or fl[1] not in LAST_NAMES:
                breaches.append(f"{e.name}: passenger {name!r}")
        for r in reservations:
            for code in readings(r.get("confirmation_code")):
                shaped = re.fullmatch(r"[Q-Z]{3}(\d)(\d)(\d)", code)
                if not shaped or not shaped[1] > shaped[2] > shaped[3]:
                    breaches.append(f"{e.name}: booking code {code!r}")
            for s in r.get("segments", []):
                for number in readings(s.get("flight_number")):
                    if not re.fullmatch(r"9\d{3}", str(number)):
                        breaches.append(f"{e.name}: flight number {number!r}")
        for file in ("extraction.json", "outcome.json"):
            text = (e.path / file).read_text(encoding="utf-8")
            for day in DAY.findall(text):
                if not day.startswith("2027"):
                    breaches.append(f"{e.name}: {file} day {day}")
        msg = email.message_from_bytes(e.raw, policy=email.policy.default)
        headers, parts = _texts(msg)
        for text in headers + parts:
            for year in YEAR.findall(text):
                if year != "2027":
                    breaches.append(f"{e.name}: number {year}")
            for address in ADDRESS.findall(text):
                if not _invented_domain(address.rsplit("@", 1)[1].rstrip(".").lower()):
                    breaches.append(f"{e.name}: address {address}")
    assert not breaches, "\n".join(breaches)


def _shape(reservations: list[dict]) -> list[tuple]:
    """Each booking's kind with how many legs and travellers it has, in a stable order."""
    return sorted(
        (r["kind"], len(r.get("segments") or []), len(r.get("passengers") or []))
        for r in reservations
        if r["kind"] != "other"
    )


def test_the_markup_entries_are_read_by_the_extractor(tmp_path):
    assert shutil.which("kitinerary-extractor") is not None
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(c, **dirs(tmp_path))
    markup = [e for e in c.entries if e.outcome["read_by"] == "kitinerary"]
    assert markup
    wrong = []
    with corpus.process_as(s):
        for e in markup:
            nm = read(e.raw, s)
            if not nm.origin_ok:
                wrong.append(f"{e.name}: refused by the mail gate")
                continue
            parsed = parse_message(nm, None)
            if parsed.parser != "kitinerary":
                wrong.append(f"{e.name}: read by {parsed.parser}")
            elif _shape([r.model_dump() for r in parsed.extraction.reservations]) != _shape(
                e.truth["reservations"]
            ):
                wrong.append(f"{e.name}: read in part")
    assert not wrong, "\n".join(wrong)


def _sheet_row(c, name: str) -> str:
    """The entry's row in the entries table of `SCENARIOS.md`."""
    sheet = (c.root / "SCENARIOS.md").read_text(encoding="utf-8")
    return next(line for line in sheet.splitlines() if line.startswith(f"| `{name}` |"))


def test_what_the_model_is_given_holds_each_booking(tmp_path):
    """What the model would be shown of each email it reads, built from the message as the mail
    reader hands it over: the text every provider is given (`build_text`) has the body after its
    body heading, and every booking code is in that body (a code the subject also prints counts
    only there); the PDF entry's code is in the document the Anthropic provider sends and nowhere
    in the text; the oversize entry's text is cut to the budget and its code lies before the cut;
    the hostile entry's injected flight is shown to the model, and its answer leaves the flight
    out."""
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(c, **dirs(tmp_path))
    pdf, oversize, hostile = "24-pdf-e-ticket", "27-oversize", "26-instruction-text"
    injected = re.search(r"add a flight ([A-Z0-9]{2}) (\d+)", _sheet_row(c, hostile))
    assert injected, f"{hostile}: its row in SCENARIOS.md names no injected flight"
    carrier, number = injected.groups()
    entries = [e for e in c.entries if e.outcome["read_by"] == "model"]
    assert {pdf, oversize, hostile} <= {e.name for e in entries}
    wrong: list[str] = []
    with corpus.process_as(s):
        for e in entries:
            nm = read(e.raw, s)
            text = build_text(nm, None)
            content = AnthropicProvider.build_content(nm, None)
            documents = [block for block in content if block["type"] == "document"]
            _, heading, given = text.partition(f"\n{BODY_HEADING}\n")
            if not heading:
                wrong.append(f"{e.name}: the text the model is given has no body heading")
                continue
            body = nm.text.strip()
            if not body or body[:200] not in given:
                wrong.append(f"{e.name}: the body is not in the text the model is given")
            codes = [readings(r.get("confirmation_code")) for r in e.truth["reservations"]]
            if e.name == pdf:
                if not documents:
                    wrong.append(f"{e.name}: the model is given no document")
                # the whole text, subject and all, holds none of the codes
                if not codes or any(code in text for r in codes for code in r):
                    wrong.append(f"{e.name}: the text alone holds the booking code")
            else:
                shown = [any(code in given for code in r) for r in codes]
                missing = [r[0] for r, ok in zip(codes, shown, strict=True) if not ok]
                if missing:
                    wrong.append(f"{e.name}: the body the model is given lacks {missing}")
            if e.name == oversize:
                if not (
                    len(nm.text) > MAX_TEXT_CHARS
                    and nm.text[:MAX_TEXT_CHARS] in given
                    and nm.text not in given
                ):
                    wrong.append(f"{e.name}: the text is not cut to the budget")
                before_cut = nm.text[:MAX_TEXT_CHARS]
                if not all(any(code in before_cut for code in r) for r in codes):
                    wrong.append(f"{e.name}: a booking code lies past the cut")
            if e.name == hostile:
                if not re.search(rf"\b{carrier}\s*{number}\b", text):
                    wrong.append(f"{e.name}: the injected flight is not in the text")
                legs = [leg for r in e.answer.reservations for leg in r.legs]
                if any(leg.number == number for leg in legs):
                    wrong.append(f"{e.name}: the answer holds the injected flight")
    assert not wrong, "\n".join(wrong)


def test_recorded_answers_are_found_by_the_emails_own_bytes(tmp_path):
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(c, **dirs(tmp_path))
    answers = corpus.CorpusAnswers(c)
    hotel = c.entry("05-chain-hotel")
    assert answers.extract(read(hotel.raw, s)) == (
        to_extraction(hotel.answer),
        Usage(DEFAULT_MODEL, 0, 0, 0.0, asked=False),  # read back: no model was asked
    )
    built = make_eml(
        from_="alex@example.com",
        subject="A note",
        text="Nothing to file here.",
        message_id="<a-note@example.com>",
    ).as_bytes()
    for raw in (c.entry("01-markup-flight").raw, built):
        with pytest.raises(ModelError) as err:
            answers.extract(read(raw, s))
        assert str(err.value) == replay.NO_ANSWER


def test_the_corpus_settings_are_the_corpus_own(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    monkeypatch.setenv("TRIPBOX_HOME_AIRPORTS", "BOS")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_URL", "http://flightlog.example:3000")
    monkeypatch.setenv("TRIPBOX_AIRTRAIL_API_KEY", "placeholder")
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(elsewhere))
    time.tzset()
    zone_before = time.tzname
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(c, **dirs(tmp_path))
    assert s.tz == "America/Chicago"
    assert s.home_airports == ("MSP",)
    assert s.allowed_senders == ("alex@example.com", "alex.sample@example.org")
    assert s.travellers.me_name == "Alex Sample"
    assert s.config_dir == c.root / "config"
    assert s.airtrail_dry_run is True
    assert [s.on(f) for f in ("mail", "model", "airtrail", "notifications")] == [False] * 4

    keyed = corpus.settings(c, **dirs(tmp_path), environ={"TRIPBOX_LLM_API_KEY": "placeholder"})
    assert keyed.on("model")

    mars = dataclasses.replace(c, settings={**c.settings, "TZ": "Mars/Base"})
    with pytest.raises(corpus.CorpusError) as err:
        corpus.settings(mars, **dirs(tmp_path))
    assert str(err.value).startswith("corpus.json: settings:")

    with corpus.process_as(s):
        assert os.environ["TRIPBOX_CONFIG_DIR"] == str(c.root / "config")
        assert os.environ["TZ"] == "America/Chicago" and time.tzname != zone_before
    assert os.environ["TRIPBOX_CONFIG_DIR"] == str(elsewhere)
    assert os.environ["TZ"] == "Pacific/Auckland" and time.tzname == zone_before
    # an exception inside leaves the process as it was before, too
    with pytest.raises(RuntimeError), corpus.process_as(s):
        raise RuntimeError("raised inside")
    assert os.environ["TRIPBOX_CONFIG_DIR"] == str(elsewhere)
    assert os.environ["TZ"] == "Pacific/Auckland" and time.tzname == zone_before
    monkeypatch.delenv("TRIPBOX_CONFIG_DIR")
    with corpus.process_as(s):
        assert os.environ["TRIPBOX_CONFIG_DIR"] == str(c.root / "config")
    assert "TRIPBOX_CONFIG_DIR" not in os.environ


SCENARIO_NAMES = (
    "01-markup-flight",
    "02-markup-hotel",
    "03-receipt-companion-first",
    "04-receipt-owner-second",
    "05-chain-hotel",
    "06-city-names-receipt",
    "07-airport-car",
    "08-independent-hotel",
    "09-html-only",
    "10-french-hotel",
    "11-package",
    "12-markup-rail",
    "13-german-hotel",
    "14-day-first-24h",
    "15-same-day-renumber",
    "16-partial-cancellation",
    "17-full-cancellation",
    "18-relative-itinerary",
    "19-unknown-cancellation",
    "20-non-travel",
    "21-stranger",
    "22-schedule-change",
    "23-date-line-crossing",
    "24-pdf-e-ticket",
    "25-flight-without-date",
    "26-instruction-text",
    "27-oversize",
    "28-broken-mime",
    "29-attached-forward",
    "30-spanish-hotel",
)


def test_the_corpus_covers_every_scenario(tmp_path):
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(c, **dirs(tmp_path))
    names = tuple(e.name for e in c.entries)
    assert names == SCENARIO_NAMES, (
        f"missing {sorted(set(SCENARIO_NAMES) - set(names))}, "
        f"unexpected {sorted(set(names) - set(SCENARIO_NAMES))}"
    )
    by = {e.name: e for e in c.entries}
    gaps = sorted(n for n, e in by.items() if e.gap)
    assert gaps == [], f"entries with a gap: {gaps}"
    with corpus.process_as(s):
        for name, how in (
            ("05-chain-hotel", "inline"),
            ("07-airport-car", "inline"),
            ("08-independent-hotel", "inline"),
            ("29-attached-forward", "attachment"),
        ):
            got = read(by[name].raw, s).forwarded
            assert got == how, f"{name}: forwarded is {got!r}, expected {how!r}"
        # 03: auto-forwarded, from a sender outside the allowed two
        raw03 = by["03-receipt-companion-first"].raw
        msg = email.message_from_bytes(raw03, policy=email.policy.default)
        assert msg["X-Forwarded-For"], "03-receipt-companion-first: no X-Forwarded-For header"
        sender = email.utils.parseaddr(msg["From"])[1].lower()
        assert len(s.allowed_senders) == 2 and sender not in s.allowed_senders, (
            f"03-receipt-companion-first: sender {sender} is among {s.allowed_senders}"
        )
        # 09: the body is one text/html part
        msg = email.message_from_bytes(by["09-html-only"].raw, policy=email.policy.default)
        parts = [p.get_content_type() for p in msg.walk() if not p.is_multipart()]
        assert parts == ["text/html"], f"09-html-only: parts are {parts}"
        # 24: one PDF attachment
        pdfs = [
            a for a in read(by["24-pdf-e-ticket"].raw, s).attachments
            if a.content_type == "application/pdf"
        ]
        assert len(pdfs) == 1, f"24-pdf-e-ticket: {len(pdfs)} PDF attachments"
        # 27: more text than the model reads, in a file still under the size limit
        raw = by["27-oversize"].raw
        text_len = len(read(raw, s).text)
        assert text_len > MAX_TEXT_CHARS, f"27-oversize: text is {text_len} characters"
        assert len(raw) < 900_000, f"27-oversize: file is {len(raw)} bytes"
    # 28: a multipart whose closing boundary line is missing
    raw = by["28-broken-mime"].raw
    found = re.search(rb'boundary="?([^";\s]+)', raw)
    assert found, "28-broken-mime: no boundary declared"
    assert b"--" + found.group(1) + b"--" not in raw, "28-broken-mime: closing boundary present"
    assert by["21-stranger"].outcome["status"] == "rejected"
    for n in ("01-markup-flight", "02-markup-hotel", "12-markup-rail"):
        assert by[n].outcome["read_by"] == "kitinerary", f"{n}: not read by kitinerary"
    assert sorted(n for n, e in by.items() if e.outcome["read_by"] == "kitinerary") == [
        "01-markup-flight",
        "02-markup-hotel",
        "12-markup-rail",
    ]
    answer = by["25-flight-without-date"].answer
    legs = [leg for r in answer.reservations for leg in r.legs]
    assert any(leg.departure is None for leg in legs), "25-flight-without-date: every leg is dated"
    trips = {
        i["booking"]["trip"]
        for e in c.entries
        for i in e.outcome["items"]
        if i["booking"] and i["booking"].get("trip")
    }
    assert len(trips) == 12, f"{len(trips)} trip titles: {sorted(trips)}"


def test_the_default_corpus_is_the_one_beside_the_package():
    expected = Path(tripbox.__file__).resolve().parents[2] / "tests" / "corpus"
    assert corpus.default_dir() == expected
    assert expected.is_dir()
