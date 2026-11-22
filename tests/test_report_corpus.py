# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Every email of the corpus, reported two ways: as it is filed (the travellers file and the
reading are known), and from the file alone with no travellers file and no reading — the case a
parse problem is. Nothing personal of an entry may survive either report."""

from __future__ import annotations

import email
import email.policy
import re
import socket
from types import SimpleNamespace

import pytest
from helpers.eml import DELIVERY_HEADERS, delivered
from helpers.personal import file_corpus, personal, survivors

from tripbox import corpus
from tripbox.mail.normalize import normalize
from tripbox.parse.pipeline import parse_message
from tripbox.report import sample

CORPUS = corpus.load(corpus.default_dir())
ENTRIES = [e.name for e in CORPUS.entries]
# Entries whose report, made with no travellers file and no reading, still holds a traveller's
# name, and why no rule finds it. An entry not listed here leaves none.
NAME_GAPS: dict[str, str] = {}
COUNTS = re.compile(
    r"replaced: names (\d+), email addresses (\d+), phone numbers (\d+), booking references (\d+),"
    r" ticket numbers (\d+), street addresses (\d+), numbers (\d+), link queries (\d+)"
)
SHOWN = ("NAME", "EMAIL", "PHONE", "REF", "TICKET", "ADDRESS", "NUMBER")
REMOVED = re.compile(r"^removed: headers (\d+), attachments \d+$", re.M)


@pytest.fixture(scope="module")
def filed(tmp_path_factory):
    """The corpus filed in order as `tripbox eval --recorded` files it; `settings` are the
    installation's, `number` each entry's message."""
    base = tmp_path_factory.mktemp("filed")
    s = corpus.settings(
        CORPUS, data_dir=base / "data", archive_dir=base / "archive", logs_dir=base / "logs"
    )
    return SimpleNamespace(settings=s, number=file_corpus(CORPUS, s))


@pytest.fixture(scope="module")
def alone(tmp_path_factory):
    """An installation that knows nobody and holds nothing: no travellers file, no ledger; and
    each entry's email as a file."""
    base = tmp_path_factory.mktemp("alone")
    (base / "config").mkdir()
    (base / "mail").mkdir()
    s = corpus.settings(
        CORPUS,
        data_dir=base / "data",
        archive_dir=base / "archive",
        logs_dir=base / "logs",
        config_dir=base / "config",
    )
    paths = {}
    for entry in CORPUS.entries:
        paths[entry.name] = base / "mail" / f"{entry.name}.eml"
        paths[entry.name].write_bytes(entry.raw)
    return SimpleNamespace(settings=s, path=paths)


@pytest.fixture(scope="module")
def reports(filed, alone):
    """Each entry's two reports, made once: `known[entry]` and `unknown[entry]`."""
    known, unknown = {}, {}
    for name in ENTRIES:
        known[name] = sample.make(filed.settings, str(filed.number[name]))
        unknown[name] = sample.make(alone.settings, str(alone.path[name]))
    return SimpleNamespace(known=known, unknown=unknown)


@pytest.mark.parametrize("name", ENTRIES)
def test_nothing_personal_of_an_entry_survives_its_report(reports, filed, name):
    made = reports.known[name]
    assert f"source: ledger message #{filed.number[name]}\n" in made.text
    assert survivors(personal(CORPUS.entry(name)), made.text) == []
    assert sample.NO_NAME not in made.said


@pytest.mark.parametrize("name", ENTRIES)
def test_with_no_travellers_file_and_no_reading_all_but_names_still_goes(reports, name):
    made = reports.unknown[name]
    assert f"source: {sample.FROM_FILE}\n" in made.text
    left = survivors(personal(CORPUS.entry(name)), made.text)
    assert [value for what, value in left if what not in ("names", "parts")] == []
    names = [value for what, value in left if what in ("names", "parts")]
    if sample.NO_NAME in made.said:
        return  # nothing named anyone, and the report said so
    if name in NAME_GAPS:
        assert names, f"{name} leaves no name: it is no gap"
    else:
        assert names == []


@pytest.mark.parametrize("name", ENTRIES)
def test_a_mailboxs_own_copy_is_reported_as_the_email_alone_is(reports, alone, name):
    """A mailbox's own copy of an email begins with the header lines of its delivery, more of
    them than the 4 KB a handed-in file is told by: its report differs by their count alone."""
    plain = reports.unknown[name].text
    held = alone.path[name].with_name(f"{name}.held.eml")
    held.write_bytes(delivered(CORPUS.entry(name).raw))
    made = sample.make(alone.settings, str(held)).text
    counts = [int(REMOVED.search(text).group(1)) for text in (plain, made)]
    assert counts[1] == counts[0] + DELIVERY_HEADERS
    assert REMOVED.sub("", made) == REMOVED.sub("", plain)


# What a parser reads, as an email prints it: a time, a day with its year, a flight's or a
# train's number. (A day with no year is told from nothing else, so it is not counted.)
READ_BY_A_PARSER = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|\b[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}\b"
    r"|\b\d{1,2}\.? [A-Z][a-zé]{2,9} \d{4}\b|\b[A-Z][A-Z0-9] ?\d{3,4}\b"
)


def plain_text(mail: str | bytes) -> str:
    """The plain-text parts of an email, decoded, one after the other."""
    if isinstance(mail, str):
        msg = email.message_from_string(mail, policy=email.policy.default)
    else:
        msg = email.message_from_bytes(mail, policy=email.policy.default)
    parts = [p for p in msg.walk() if p.get_content_type() == "text/plain"]
    return "\n".join(p.get_content() for p in parts)


@pytest.mark.parametrize("name", ENTRIES)
def test_what_a_parser_reads_in_an_entrys_text_is_still_there(reports, name):
    """No rule takes a time, a day or a flight's number for something personal: each stands in
    the redacted text as often as in the email's own."""
    before = sorted(READ_BY_A_PARSER.findall(plain_text(CORPUS.entry(name).raw)))
    for made in (reports.known[name], reports.unknown[name]):
        shown = plain_text(made.text.split(f"\n{sample.EMAIL}\n", 1)[1])
        assert sorted(READ_BY_A_PARSER.findall(shown)) == before


def bookings(reading) -> list[tuple]:
    """What a reading says is booked: per reservation its kind, its days, and each leg's
    carrier, number, places and days."""
    out = []
    for res in reading["reservations"]:
        legs = [
            (
                leg.get("carrier_iata"),
                leg.get("flight_number") or leg.get("train_number"),
                leg.get("origin"),
                leg.get("destination"),
                (leg.get("departure_local") or "")[:10],
                (leg.get("arrival_local") or "")[:10],
            )
            for leg in res.get("segments", [])
        ]
        stay = tuple((res.get(key) or "")[:10] for key in ("check_in", "check_out"))
        out.append((res["kind"], stay, sorted(legs)))
    return sorted(out)


def test_a_redacted_markup_email_is_read_as_the_same_bookings(reports, filed):
    by_markup = [e for e in CORPUS.entries if e.outcome["read_by"] == "kitinerary"]
    assert len(by_markup) >= 3
    for entry in by_markup:
        redacted = reports.known[entry.name].text.split(f"\n{sample.EMAIL}\n", 1)[1].encode()
        with corpus.process_as(filed.settings):
            read = parse_message(normalize(redacted, (), filed.settings.tz, gate=False), None)
        assert read.parser == "kitinerary", entry.name
        assert bookings(read.extraction.model_dump(mode="json")) == bookings(entry.truth)
        # and nothing of who travels came back with them
        assert survivors(personal(entry), read.extraction.model_dump_json()) == []


@pytest.mark.parametrize("name", ENTRIES)
def test_no_report_opens_a_socket(filed, alone, name, monkeypatch):
    opened = []

    def connect(self, address):
        opened.append(address)
        raise OSError("a report opened a socket")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)
    assert sample.make(filed.settings, str(filed.number[name])).text
    assert sample.make(alone.settings, str(alone.path[name])).text
    assert opened == []


def test_every_report_names_what_it_kept_and_what_it_removed(reports, filed):
    for name in ENTRIES:
        entry = CORPUS.entry(name)
        for made in (reports.known[name], reports.unknown[name]):
            head, body = made.text.split(f"\n{sample.DID}\n", 1)
            lines = head.split("\n")
            assert lines[:5] == list(sample.HEAD) and lines[5] == ""
            assert [line.split(":")[0] for line in lines[6:13]] == [
                "version",
                "wire schema",
                "source",
                "status",
                "read by",
                "replaced",
                "removed",
            ]
            assert lines[13:15] == [sample.SHOWN, ""]
            assert body.index(f"\n{sample.READ}\n") < body.index(f"\n{sample.EMAIL}\n")
            # each count is the number of placeholders of its kind the report shows
            counted = COUNTS.fullmatch(lines[11])
            assert counted, lines[11]
            shown = [len(re.findall(rf"\[{word}-\d+\]", body)) for word in SHOWN]
            assert [int(n) for n in counted.groups()] == [*shown, body.count("?[QUERY]")]
            gone = body.count("[ATTACHMENT REMOVED: ")
            assert re.fullmatch(rf"removed: headers \d+, attachments {gone}", lines[12])
            assert made.said[0].endswith(f"and {gone} attachments")
            assert made.said[-1] == sample.READ_IT
        assert f"\nstatus: {entry.outcome['status']}\n" in reports.known[name].text
