# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox report`: where it reads a message from, what it prints where, what it refuses, and
that it writes nothing but the report. The emails are the corpus's own."""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anthropic
import openai
import pytest
from helpers.eml import DELIVERY_HEADERS, delivered
from helpers.personal import file_corpus

from tripbox import cli, corpus, uploads
from tripbox.config import Settings
from tripbox.parse import provider
from tripbox.report import sample

REPLACED = re.compile(
    r"report: replaced \d+ names, \d+ email addresses, \d+ phone numbers, \d+ booking references,"
    r" \d+ ticket numbers, \d+ street addresses, \d+ numbers and \d+ link queries;"
    r" removed \d+ headers and \d+ attachments"
)
LISTED = re.compile(
    r"  #\d+  \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC  [a-z]+  read by (kitinerary|a model|nobody)"
    r"  \d+ booking\(s\)  .{0,60}"
)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An installation that holds nothing yet: the corpus's settings and travellers, its folders
    under the test's own. `file(...)` files corpus entries into its ledger (`sent`: as
    `file_corpus` takes it)."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    c = corpus.load(corpus.default_dir())
    folders = {name: tmp_path / name for name in ("data", "archive", "logs")}
    for name, value in c.settings.items():
        monkeypatch.setenv(name, value)
    for name, path in folders.items():
        monkeypatch.setenv(f"TRIPBOX_{name.upper()}_DIR", str(path))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(c.root / corpus.CONFIG_DIR))

    def file(*names: str, sent=None) -> dict[str, int]:
        s = corpus.settings(
            c, data_dir=folders["data"], archive_dir=folders["archive"], logs_dir=folders["logs"]
        )
        return file_corpus(c, s, names, sent)

    return SimpleNamespace(root=tmp_path, corpus=c, file=file, **folders)


def sections(report: str) -> dict[str, str]:
    """A report's head and its three sections."""
    head, rest = report.split(f"\n{sample.DID}\n", 1)
    did, rest = rest.split(f"\n{sample.READ}\n", 1)
    read, email = rest.split(f"\n{sample.EMAIL}\n", 1)
    return {"head": head, "did": did.strip(), "read": read.strip(), "email": email}


def tree(folder: Path) -> list[tuple[str, int, int]]:
    """Every file under a folder: its path, size and time of last change."""
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    return [(str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns) for p in files]


def held(box) -> tuple:
    """Everything a report must leave as it is: the ledger's rows, its folder's files and the
    archive's. The rows are read through a connection that itself makes nothing beside the
    ledger."""
    ledger = box.data / "ledger.db"
    conn = sqlite3.connect(f"file:{ledger}?mode=ro&immutable=1", uri=True)
    try:
        rows = list(conn.iterdump())
    finally:
        conn.close()
    return rows, tree(box.data), tree(box.archive)


def test_a_ledger_message_is_reported_and_nothing_is_written(box, capsys):
    filed = box.file("01-markup-flight", "02-markup-hotel")
    before = held(box)
    assert cli.main(["report", str(filed["02-markup-hotel"])]) == 0
    seen = capsys.readouterr()
    assert seen.out.split("\n")[: len(sample.HEAD)] == list(sample.HEAD)
    said = seen.err.splitlines()
    assert REPLACED.fullmatch(said[0]) and said[1:] == [sample.READ_IT]
    part = sections(seen.out)
    assert "source: ledger message #2\nstatus: processed\nread by: kitinerary\n" in part["head"]
    assert json.loads(part["did"]) == {
        "status": "processed",
        "bookings": [
            {
                "kind": "lodging",
                "result": "filed",
                "detail": None,
                "checks": [],
                "key": "lodging|[REF-1]",
            }
        ],
    }
    assert json.loads(part["read"])["reservations"][0]["confirmation_code"] == "[REF-1]"
    assert "Confirmation number: [REF-1]" in part["email"]
    assert held(box) == before
    assert not box.logs.exists()


def test_a_file_the_ledger_does_not_hold_is_read_here_with_no_model(box, capsys, monkeypatch):
    # the folder it is read in is made in this test's own temporary folder, which another run
    # running beside it never writes in
    own = box.root / "tmp"
    own.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(own))
    def built(*args, **kwargs):
        raise AssertionError("a report builds no model")

    # a model is set up, and a report still builds none
    monkeypatch.setenv("TRIPBOX_LLM_API_KEY", "sk-ant-not-a-real-key")
    for owner, name in ((provider, "build"), (anthropic, "Anthropic"), (openai, "OpenAI")):
        monkeypatch.setattr(owner, name, built)
    stay = box.root / "stay.eml"
    stay.write_bytes(box.corpus.entry("02-markup-hotel").raw)
    assert cli.main(["report", str(stay)]) == 0
    part = sections(capsys.readouterr().out)
    assert f"source: {sample.FROM_FILE}\nstatus: processed\nread by: kitinerary\n" in part["head"]
    assert [b["key"] for b in json.loads(part["did"])["bookings"]] == ["lodging|[REF-1]"]
    assert json.loads(part["read"])["reservations"][0]["kind"] == "lodging"
    # an email only a model reads waits for one here, and says so
    receipt = box.root / "receipt.eml"
    receipt.write_bytes(box.corpus.entry("05-chain-hotel").raw)
    assert cli.main(["report", str(receipt)]) == 0
    part = sections(capsys.readouterr().out)
    assert "status: waiting\nread by: nobody\n" in part["head"]
    assert json.loads(part["did"]) == {"status": "waiting", "waiting": "no_model", "bookings": []}
    assert part["read"] == sample.NOTHING
    # nothing of the installation was made, and the folder it was read in is gone
    assert not box.data.exists() and not box.archive.exists() and not box.logs.exists()
    assert list(own.iterdir()) == []


def test_a_file_the_ledger_holds_is_reported_from_the_ledger(box, capsys):
    filed = box.file("01-markup-flight", "05-chain-hotel")
    receipt = box.root / "receipt.eml"
    receipt.write_bytes(box.corpus.entry("05-chain-hotel").raw)
    assert cli.main(["report", str(receipt)]) == 0
    part = sections(capsys.readouterr().out)
    n = filed["05-chain-hotel"]
    assert f"source: ledger message #{n}\nstatus: processed\nread by: a model\n" in part["head"]
    # what was read is the answer stored with the message
    assert json.loads(part["read"])["reservations"][0]["passengers"] == ["[NAME-1]"]


def test_standard_input_is_read_for_a_dash(box, capsys, monkeypatch):
    pasted = "Example Desert Inn\nConfirmation number: XTZ732\nGuest: Alex Sample\n"
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(pasted.encode())))
    assert cli.main(["report", "-"]) == 0
    part = sections(capsys.readouterr().out)
    assert f"source: {sample.FROM_FILE}\n" in part["head"]
    # pasted text has no headers: it is shown as text, redacted
    assert part["email"] == "Example Desert Inn\nConfirmation number: [REF-1]\nGuest: [NAME-1]\n"


def test_an_email_is_one_however_long_its_header_block_is(box, capsys):
    """A mailbox's own copy of a message begins with the header lines of its delivery, more of
    them than the 4 KB a handed-in file is told by: it is read and shown as the email it is."""
    raw = box.corpus.entry("02-markup-hotel").raw
    assert len(delivered(raw)) - len(raw) > 4096
    shown = {}
    for name, data in (("stay.eml", raw), ("held.eml", delivered(raw))):
        (box.root / name).write_bytes(data)
        assert cli.main(["report", str(box.root / name)]) == 0
        shown[name] = capsys.readouterr().out
    # what a mail program or an editor puts before the first header changes nothing: an empty
    # line, a byte-order mark, a space, the line a mailbox file begins a message with
    for before in (b"\n", b"\xef\xbb\xbf", b" ", b"From trips@example.com\n"):
        for name in ("stay.eml", "held.eml"):
            (box.root / "saved.eml").write_bytes(before + (box.root / name).read_bytes())
            assert cli.main(["report", str(box.root / "saved.eml")]) == 0
            assert capsys.readouterr().out == shown[name], (before, name)
    # nor does a header written with no space after its colon
    close = delivered(raw).replace(b"Received: from", b"Received:from", 1)
    (box.root / "saved.eml").write_bytes(close)
    assert cli.main(["report", str(box.root / "saved.eml")]) == 0
    assert capsys.readouterr().out == shown["held.eml"]
    removed = re.compile(r"^removed: headers (\d+), attachments 0$", re.M)
    counts = [int(removed.search(shown[name]).group(1)) for name in ("stay.eml", "held.eml")]
    assert counts[1] == counts[0] + DELIVERY_HEADERS
    assert "Received" not in shown["held.eml"] and "mail.example" not in shown["held.eml"]
    assert removed.sub("", shown["held.eml"]) == removed.sub("", shown["stay.eml"])
    assert "status: processed\nread by: kitinerary\n" in shown["held.eml"]
    # and one whose bytes are no UTF-8 is an email like any other
    billet = delivered(
        b"From: Rail Exemple <billets@rail.example>\nSubject: Billet\n"
        b"Content-Type: text/plain; charset=iso-8859-1\n\nR\xe9serv\xe9 pour 2 nuits\n"
    )
    assert uploads.sniff(billet) == "eml"
    (box.root / "billet.eml").write_bytes(billet)
    assert cli.main(["report", str(box.root / "billet.eml")]) == 0
    assert "\nRéservé pour 2 nuits\n" in capsys.readouterr().out
    # a message the ledger holds as its mailbox delivered it is shown the same way
    filed = box.file("02-markup-hotel", sent=delivered)
    assert cli.main(["report", str(filed["02-markup-hotel"])]) == 0
    mailed = capsys.readouterr().out
    assert "source: ledger message #1\nstatus: processed\nread by: kitinerary\n" in mailed
    assert sections(mailed)["email"] == sections(shown["stay.eml"])["email"]


def test_pasted_source_that_is_still_encoded_is_decoded_before_it_is_redacted(
    box, capsys, monkeypatch
):
    pasted = (
        b"<p>Guest: Alex Sam=\r\nple, it=E2=80=99s confirmed.</p>\r\n"
        b'<a href=3D"https://www.example.com/stay?first=3DAlex&amp;la=\r\nst=3DSample">'
        b"Your stay</a>\r\n"
        b"<p>Confirmation number: XTZ=\r\n732</p>\r\n"
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(pasted)))
    assert cli.main(["report", "-"]) == 0
    assert sections(capsys.readouterr().out)["email"] == (
        "<p>Guest: [NAME-1], it’s confirmed.</p>\n"
        '<a href="https://www.example.com/stay?[QUERY]">Your stay</a>\n'
        "<p>Confirmation number: [REF-1]</p>\n"
    )


def test_pasted_lines_of_a_label_and_a_value_are_text_and_give_their_names(
    nobody, capsys, monkeypatch
):
    """Text is harvested as text: its first lines are no headers, whatever they look like, and
    lines of a label and a value alone are no email."""
    pasted = "Name: Robin Sample\nHotel: Example Desert Inn\nDear Pat Sample,\n"
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(pasted.encode())))
    assert cli.main(["report", "-"]) == 0
    seen = capsys.readouterr()
    assert sections(seen.out)["email"] == (
        "Name: [NAME-1]\nHotel: Example Desert Inn\nDear [NAME-2],\n"
    )
    assert sample.NO_NAME not in seen.err
    lines = nobody.with_name("lines.txt")
    lines.write_text("Passenger: Lee Example\nFlight: AS 9203\nSeat: 32A\n\nSee you there.\n")
    assert cli.main(["report", str(lines)]) == 0
    seen = capsys.readouterr()
    assert "removed: headers 0, attachments 0\n" in seen.out
    assert sections(seen.out)["email"] == (
        "Passenger: [NAME-1]\nFlight: AS 9203\nSeat: 32A\n\nSee you there.\n"
    )


def test_one_value_has_one_placeholder_in_the_key_in_what_was_read_and_in_the_email(box):
    """What a key of the reading names (a code of four digits) goes from the email and from
    the booking's key too; what a label of the email names (an address with no number in it)
    goes from the reading too."""
    raw = (
        b"From: Example Marina Lodge <stay@lodge.example>\nTo: alex@example.com\n"
        b"Subject: Your stay\n\n"
        b"Booking number: 4821\nLodge address: Rose Cottage, Example Lane\n"
        b"Meet us at Example Marina, Pier B\n"
        b"Guest: Alex Sample, phone 612\xe2\x80\x91555\xe2\x80\x910142\n"
    )
    stay = {
        "kind": "lodging",
        "confirmation_code": "4821",
        "address": "Rose Cottage, Example Lane",
        "pickup_location": "Example Marina, Pier B",
        "passengers": ["Alex Sample"],
        "notes": "Rose Cottage, Example Lane. Call 6125550142 about 4821.",
    }
    booking = {"kind": "lodging", "result": "filed", "detail": None, "checks": [], "key": ""}
    filed = [booking | {"key": "lodging|4821"}]
    held = sample._Held("processed", "kitinerary", None, filed, {"reservations": [stay]})
    made = sample._write(Settings.from_env(), raw, [], sample.FROM_FILE, held)
    part = sections(made.text)
    assert json.loads(part["did"])["bookings"] == [booking | {"key": "lodging|[REF-1]"}]
    assert json.loads(part["read"])["reservations"] == [
        {
            "kind": "lodging",
            "confirmation_code": "[REF-1]",
            "address": "[ADDRESS-1]",
            "pickup_location": "Example Marina, Pier B",
            "passengers": ["[NAME-1]"],
            "notes": "[ADDRESS-1]. Call [PHONE-1] about [REF-1].",
        }
    ]
    assert part["email"].split("\n\n", 1)[1] == (
        "Booking number: [REF-1]\nLodge address: [ADDRESS-1]\n"
        "Meet us at Example Marina, Pier B\nGuest: [NAME-1], phone [PHONE-1]\n"
    )
    for gone in ("4821", "Rose Cottage", "0142", "Alex"):
        assert gone not in made.text, gone


def test_no_argument_lists_the_ten_newest_messages(box, capsys):
    assert cli.main(["report"]) == 0
    assert capsys.readouterr().out == f"{sample.NO_MESSAGE}\n"
    # a file nothing has made a ledger of yet holds no message either
    box.data.mkdir()
    (box.data / "ledger.db").write_bytes(b"")
    assert cli.main(["report"]) == 0
    assert capsys.readouterr().out == f"{sample.NO_MESSAGE}\n"
    assert cli.main(["report", "1"]) == 2
    assert capsys.readouterr().err == "report: no message 1 in the ledger\n"
    (box.data / "ledger.db").unlink()
    twelve = tuple(e.name for e in box.corpus.entries[:12])
    filed = box.file(*twelve)
    assert cli.main(["report"]) == 0
    seen = capsys.readouterr()
    lines = seen.out.splitlines()
    assert lines[0] == sample.LISTED and len(lines) == 11 and seen.err == ""
    assert all(LISTED.fullmatch(line) for line in lines[1:])
    assert [int(line.split()[0][1:]) for line in lines[1:]] == list(range(12, 2, -1))
    rail = box.corpus.entries[11]
    when = corpus.sent(rail).astimezone(sample.dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    subject = sample.redact._words(dict(sample.redact._tree(rail.raw).headers)["Subject"])
    assert lines[1] == (
        f"  #{filed[rail.name]}  {when}  processed  read by kitinerary  1 booking(s)"
        f"  {subject[:60]}"
    )
    assert any("read by a model  " in line for line in lines[1:])


def test_out_makes_a_new_file_for_its_owner_alone_and_never_writes_over_one(box, capsys):
    filed = box.file("01-markup-flight")
    out = box.root / "tripbox-report.txt"
    argv = ["report", str(filed["01-markup-flight"]), "--out", str(out)]
    assert cli.main(argv) == 0
    seen = capsys.readouterr()
    assert seen.out == ""
    assert seen.err.splitlines()[-2:] == [sample.READ_IT, f"report: wrote {out}"]
    written = out.read_text(encoding="utf-8")
    assert written.startswith(sample.HEAD[0] + "\n") and sample.EMAIL in written
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert cli.main(argv) == 2
    seen = capsys.readouterr()
    assert (seen.out, seen.err) == ("", f"report: {out} exists; choose another name\n")
    assert out.read_text(encoding="utf-8") == written


def test_each_refusal_is_one_line_and_exit_2(box, capsys, monkeypatch):
    filed = box.file("01-markup-flight")
    n = filed["01-markup-flight"]
    there = box.root / "there.txt"
    there.write_text("kept\n")
    pdf = box.root / "ticket.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    image = box.root / "photo.bin"
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    big = box.root / "big.eml"
    big.write_bytes(box.corpus.entry("01-markup-flight").raw)
    nowhere = box.root / "no-such-folder" / "report.txt"
    absent = box.root / "absent.eml"
    no_pdf = "report: a PDF is never put in a report; describe what it shows instead"
    no_name = "report: --name takes a name of 2 to 80 characters"
    refusals = [
        (["report", "99"], "report: no message 99 in the ledger"),
        (["report", str(absent)], f"report: cannot read {absent}"),
        (["report", str(box.root)], f"report: cannot read {box.root}"),
        (["report", str(n), "--out", str(there)], f"report: {there} exists; choose another name"),
        (["report", str(n), "--out", str(nowhere)], f"report: cannot write {nowhere}"),
        (["report", str(pdf)], no_pdf),
        (["report", str(image)], "report: this is neither text nor an email"),
        (["report", str(n), "--name", "A"], no_name),
        (["report", str(n), "--name", "A" * 81], no_name),
    ]
    for argv, line in refusals:
        assert cli.main(argv) == 2, argv
        seen = capsys.readouterr()
        assert (seen.out, seen.err) == ("", line + "\n"), argv
    assert there.read_text() == "kept\n" and not nowhere.parent.exists()
    # an email over the limit (the limit made small, so the test's email is over it)
    with monkeypatch.context() as smaller:
        smaller.setattr(sample, "MAX_BYTES", 1000)
        for argv in (["report", str(big)], ["report", str(n)]):
            assert cli.main(argv) == 2
            seen = capsys.readouterr()
            assert (seen.out, seen.err) == ("", "report: the email is over 20 MB\n")
    # a message whose saved email is gone, or is no longer the email that was filed
    saved = next(box.archive.rglob("*.eml"))
    saved.write_bytes(b"Subject: another\n\nemail\n")
    gone = f"report: message {n}'s saved email is not in the archive\n"
    assert cli.main(["report", str(n)]) == 2
    assert capsys.readouterr().err == gone
    saved.unlink()
    assert cli.main(["report", str(n)]) == 2
    seen = capsys.readouterr()
    assert (seen.out, seen.err) == ("", gone)


def test_a_ledger_that_cannot_be_read_is_one_refusal_line_and_exit_2(box, capsys):
    """A locked, damaged or older ledger refuses in one line, with what SQLite says of it, for a
    message's number, a file and the newest ten alike, and nothing is written."""
    filed = box.file("01-markup-flight")
    n = filed["01-markup-flight"]
    ledger = box.data / "ledger.db"
    older = sqlite3.connect(ledger)
    older.execute("ALTER TABLE messages DROP COLUMN answer_json")
    older.commit()
    older.close()
    sample_file = box.root / "sample.eml"
    sample_file.write_bytes(box.corpus.entry("01-markup-flight").raw)
    for argv in (["report", str(n)], ["report"]):
        assert cli.main(argv) == 2, argv
        seen = capsys.readouterr()
        assert seen.out == "" and len(seen.err.splitlines()) == 1, argv
        assert seen.err.startswith("report: cannot read the ledger (no such column"), argv
    ledger.write_bytes(b"not a ledger at all, only some bytes of text\n" * 40)
    for argv in (["report", str(n)], ["report", str(sample_file)], ["report"]):
        assert cli.main(argv) == 2, argv
        seen = capsys.readouterr()
        assert (seen.out, seen.err) == (
            "", "report: cannot read the ledger (file is not a database); stop tripbox's"
            " services and try again\n",
        ), argv
    assert sorted(p.name for p in box.data.iterdir()) == ["ledger.db"]


ITINERARY = (
    "From: Sample Trips <itinerary@sampletrips.example>\n"
    "Subject: Your itinerary\n\n"
    "LEE EXAMPLE travels with one guest.\n"
    "Flight AS 9203, Monday, June 14, 2027, seat 32A\n"
)


@pytest.fixture
def nobody(box, monkeypatch):
    """The installation with no travellers file, and an email whose only name no label gives."""
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(box.root / "config"))
    path = box.root / "itinerary.eml"
    path.write_text(ITINERARY)
    return path


def test_a_typed_name_is_replaced_and_the_warning_goes(nobody, capsys):
    assert cli.main(["report", str(nobody), "--name", "Lee  Example", "--name", "Pat Example"]) == 0
    seen = capsys.readouterr()
    email = sections(seen.out)["email"]
    assert "From: Sample Trips <[EMAIL-1]>\n" in email
    assert "[NAME-1] travels with one guest.\n" in email
    assert not re.search(r"(?i)\blee\b|example", email)
    said = seen.err.splitlines()
    assert REPLACED.fullmatch(said[0]) and said[1:] == [sample.READ_IT]
    assert said[0].startswith("report: replaced 1 names, 1 email addresses,")


def test_names_the_email_alone_gave_say_others_are_not_replaced(nobody, capsys, monkeypatch):
    """With no travellers file and no --name, a harvest may take a word that is no name and
    miss one that is: the report says so, and a --name takes the line away."""
    pasted = "Guest: Robin Sample\nNombre de voyageurs : Deux adultes\n"
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(pasted.encode())))
    assert cli.main(["report", "-"]) == 0
    seen = capsys.readouterr()
    assert sections(seen.out)["email"] == "Guest: [NAME-1]\nNombre de voyageurs : Deux adultes\n"
    said = seen.err.splitlines()
    assert said[1:] == [sample.GUESSED, sample.READ_IT]
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(pasted.encode())))
    assert cli.main(["report", "-", "--name", "Robin Sample"]) == 0
    assert capsys.readouterr().err.splitlines()[1:] == [sample.READ_IT]


def test_no_known_name_says_names_are_not_replaced(nobody, capsys):
    assert cli.main(["report", str(nobody)]) == 0
    seen = capsys.readouterr()
    assert "LEE EXAMPLE travels with one guest." in seen.out
    said = seen.err.splitlines()
    assert REPLACED.fullmatch(said[0]) and said[1:] == [sample.NO_NAME, sample.READ_IT]
    assert said[1] == (
        "report: no traveller's name is known for this email: names in it are NOT replaced."
        ' Name them: --name "Alex Sample"'
    )
    assert "replaced: names 0, email addresses 1, " in seen.out
    # dates, the flight and the seat are what a parser reads: they stay
    assert "Flight AS 9203, Monday, June 14, 2027, seat 32A" in seen.out


def test_report_runs_with_the_network_switched_off(box, capsys, monkeypatch):
    filed = box.file("01-markup-flight")
    stay = box.root / "stay.eml"
    stay.write_bytes(box.corpus.entry("02-markup-hotel").raw)
    runs = (["report", str(filed["01-markup-flight"])], ["report", str(stay)])
    online = []
    for argv in runs:
        assert cli.main(argv) == 0
        online.append(capsys.readouterr())
        assert "TRIPBOX_NO_NETWORK" not in os.environ
    monkeypatch.setenv("TRIPBOX_NO_NETWORK", "1")
    for argv, before in zip(runs, online, strict=True):
        assert cli.main(argv) == 0
        assert capsys.readouterr() == before
        assert os.environ["TRIPBOX_NO_NETWORK"] == "1"
