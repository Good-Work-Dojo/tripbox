# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The waiting list of what the page is handed (a paste, an email file, a PDF): what a body is told
from its bytes alone, the name kept of it, and the limits that keep the list small and the page
safe. Every body is built at run time from the corpus, but for a few invented lines that are no
email."""

import email
import email.policy
import functools
import sqlite3
import threading

import pytest
from helpers.eml import delivered

from tripbox import corpus, uploads
from tripbox.ledger import Ledger, repo
from tripbox.util import now_iso


@functools.cache
def _corpus() -> corpus.Corpus:
    return corpus.load(corpus.default_dir())


def an_email(n: int = 0) -> bytes:
    """The corpus's `n`th email, as it reached the mailbox."""
    return _corpus().entries[n].raw


def a_pdf() -> bytes:
    """The first PDF a corpus email carries."""
    for entry in _corpus().entries:
        msg = email.message_from_bytes(entry.raw, policy=email.policy.default)
        for part in msg.walk():
            if part.get_content_type() == "application/pdf":
                return part.get_payload(decode=True)
    raise AssertionError("the corpus carries no PDF")


def a_text(n: int = 0) -> bytes:
    """The plain text of the corpus's `n`th email, as a paste of it carries it."""
    msg = email.message_from_bytes(an_email(n), policy=email.policy.default)
    return msg.get_body(preferencelist=("plain",)).get_content().encode()


def texts(count: int) -> list[bytes]:
    """`count` different pasted texts."""
    return [a_text() + f"\nPasted {n}\n".encode() for n in range(count)]


def submit(led: Ledger, body: bytes, name: str | None = None) -> tuple[str, bool]:
    return uploads.submit(led, body, name=name, submitted_by=None)


def refused(led: Ledger, body: bytes, name: str | None = None) -> str:
    with pytest.raises(uploads.Refused) as caught:
        submit(led, body, name)
    assert isinstance(caught.value, ValueError)
    return caught.value.code


def rows(led: Ledger) -> list[sqlite3.Row]:
    return led.conn.execute("SELECT * FROM uploads ORDER BY id").fetchall()


def test_what_a_body_is_told_from_its_bytes():
    pdf, mail, text = a_pdf(), an_email(), a_text()
    assert pdf.startswith(b"%PDF-")
    assert uploads.sniff(pdf) == "pdf"
    assert uploads.sniff(mail) == "eml"
    assert uploads.sniff(text) == "text"
    assert uploads.sniff(text.replace(b"\n", b"\r\n") + b"\tend") == "text"
    assert uploads.sniff("Sample family, voilà".encode()) == "text"
    # the email test is the pipeline's own: the pipeline imports it from here
    from tripbox.pipeline import run

    assert run.looks_like_email is uploads.looks_like_email
    assert uploads.looks_like_email(mail) and not uploads.looks_like_email(text)
    # an archive, an image, an office file or a body with a NUL is none of the three, whatever
    # follows its first bytes
    magic = {
        "zip": b"PK\x03\x04\x14\x00\x00\x00\x08\x00",
        "gzip": b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03",
        "tar": b"ticket.eml".ljust(100, b"\x00") + b"0000644\x00",
        "7z": b"7z\xbc\xaf\x27\x1c\x00\x04",
        "rar": b"Rar!\x1a\x07\x01\x00",
        "png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        "jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",
        "gif": b"GIF89a\x01\x00\x01\x00\x80\x00\x00",
        "office": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "nul": b"Sample\x00",
    }
    for what, head in magic.items():
        for after in (b"", text, mail):
            assert uploads.sniff(head + after) is None, what
    # white space is text: a form feed between two pages of a PDF's text, a vertical tab
    assert uploads.sniff(text + b"\x0c" + a_text(1)) == "text"
    assert uploads.sniff(text + b"\x0b" + a_text(1)) == "text"
    # any other control character; bytes that are no UTF-8
    for body in (text + b"\x1b[0m", b"\x7f" + text, text + b"\xc2\x85", text + b"\x1c"):
        assert uploads.sniff(body) is None, body[-4:]
    assert uploads.sniff(b"\xe9t\xe9") is None
    assert uploads.sniff(b"") is None


def test_an_email_is_told_by_a_header_only_mail_carries():
    mail = an_email()
    assert uploads.email_of(mail) == mail
    # what stands before its first header is no part of it: a byte-order mark, white space, the
    # line a mailbox file puts before each message
    for before in (
        b"\n",
        b"\r\n\r\n",
        b"\xef\xbb\xbf",
        b" ",
        b"\t \n",
        b"From trips@example.com\n",
        b"\xef\xbb\xbf\nFrom MAILER-DAEMON\r\n",
    ):
        assert uploads.email_of(before + mail) == mail, before
        assert uploads.sniff(before + mail) == "eml", before
    # its header block may be as long as a mailbox makes it, hold a header with no space after
    # the colon, and bytes that are no UTF-8
    held = delivered(mail)
    assert len(held) - len(mail) > 4096 and uploads.email_of(held) == held
    close = held.replace(b"Received: from", b"Received:from", 1)
    assert uploads.email_of(close) == close
    billet = b"From: Rail Exemple <billets@rail.example>\nSubject: Billet\n\nR\xe9serv\xe9\n"
    assert uploads.sniff(billet) == "eml" and uploads.sniff(delivered(billet)) == "eml"
    # lines of the form `Word: value` are what pasted text holds too: alone they make no email
    for text in (
        b"Passenger: Alex Sample\nSeat: 32A\n",
        b"Guest: Alex Sample\nConfirmation: XTZ732\n\nTwo nights\n",
        b"From: home\nTo: the coast\nSubject: our trip\nDate: next week\n\nSee you there\n",
        b"Received: with thanks\nTotal: 40.00\n\n",
        b"Note: " + a_text(),
        b"12:30 Depart\n\n",
        b"https://www.example.com/\n\n",
        b"Dear Alex,\nMessage-ID: <1@mail.example>\n\n",
    ):
        assert uploads.email_of(text) is None, text
        assert not uploads.looks_like_email(text) and uploads.sniff(text) == "text", text


def test_a_file_name_is_only_its_last_part():
    assert uploads.clean_name("../../etc/passwd.eml") == "passwd.eml"
    assert uploads.clean_name("C:\\scans\\ticket.pdf") == "ticket.pdf"
    assert uploads.clean_name("scans/2\\ticket.pdf") == "ticket.pdf"
    assert uploads.clean_name("ticket.eml") == "ticket.eml"
    assert uploads.clean_name("tick\x00et\x1b.e\x7fml") == "ticket.eml"
    assert uploads.clean_name("ticket\u202egpj.eml") == "ticketgpj.eml"  # no reordering mark
    assert uploads.clean_name("\x00\x01\x1b\x7f") is None
    assert uploads.clean_name("tickets/") is None
    assert uploads.clean_name("   ") is None
    assert uploads.clean_name("") is None and uploads.clean_name(None) is None
    long = uploads.clean_name("x" * 300 + ".pdf")
    assert long == "x" * 120
    assert len(uploads.clean_name("é" * 300)) == 120


def test_the_same_bytes_twice_are_one_upload():
    led = Ledger.memory()
    body = an_email()
    token, new = uploads.submit(led, body, name="a.eml", submitted_by="alex@example.com")
    assert new and len(token) >= 22 and token.replace("-", "").replace("_", "").isalnum()
    again, new_again = uploads.submit(led, body, name="b.eml", submitted_by=None)
    assert (again, new_again) == (token, False)
    (row,) = rows(led)
    assert (row["token"], row["kind"], row["name"], row["size"], row["submitted_by"]) == (
        token,
        "eml",
        "a.eml",
        len(body),
        "alex@example.com",
    )
    assert row["body"] == body and row["state"] == "waiting" and row["message_id"] is None
    # read and finished, the same bytes are still the same upload: its token, nothing new
    claimed = led.uploads.claim_next()
    led.uploads.finish(claimed["id"], "filed")
    assert submit(led, body) == (token, False) and len(rows(led)) == 1
    other, new = submit(led, a_text())
    assert new and other != token and len(rows(led)) == 2


def test_the_same_bytes_after_a_dead_end_are_taken_again_under_their_token():
    """An upload that ended with nothing filed (`nothing`, or `failed`) is taken again when its
    bytes come back — it waits again with its body stored anew, under the same token, so the
    owner never lands on the old dead end; one that waits, is being read or was filed is still
    the one upload."""
    led = Ledger.memory()
    bodies = [an_email(), a_text(), a_pdf()]
    tokens = [submit(led, body)[0] for body in bodies]
    mid = led.messages.insert(sha256="upload-message", source="upload", status="ignored")
    for token, (state, error) in zip(
        tokens, (("nothing", None), ("nothing", "no_model"), ("failed", "unreadable")), strict=True
    ):
        claimed = led.uploads.claim_next()
        assert claimed["token"] == token
        led.uploads.finish(claimed["id"], state, message_id=mid, error=error)
    for token, body in zip(tokens, bodies, strict=True):
        assert submit(led, body, name="again") == (token, True)
        row = led.uploads.get(token)
        assert (row["state"], row["body"], row["message_id"], row["error"]) == (
            "waiting",
            body,
            None,
            None,
        )
        assert row["size"] == len(body)
    assert len(rows(led)) == 3 and led.uploads.waiting() == 3
    # waiting, being read or filed: the same upload, nothing changes
    assert submit(led, bodies[0]) == (tokens[0], False)
    claimed = led.uploads.claim_next()
    assert submit(led, bodies[0]) == (tokens[0], False)
    assert led.uploads.get(tokens[0])["state"] == "processing"
    led.uploads.finish(claimed["id"], "filed", message_id=mid)
    assert submit(led, bodies[0]) == (tokens[0], False)
    assert led.uploads.get(tokens[0])["state"] == "filed"
    assert led.uploads.get(tokens[0])["body"] is None
    # taken again, it holds a place in the list like any upload that waits
    full = Ledger.memory()
    waiting = texts(uploads.MAX_WAITING + 1)
    first = submit(full, waiting[0])[0]
    full.uploads.finish(full.uploads.claim_next()["id"], "nothing")
    for body in waiting[1:]:
        submit(full, body)
    assert refused(full, waiting[0]) == "full"
    assert full.uploads.get(first)["state"] == "nothing"


def test_what_is_refused_and_why_stores_nothing():
    led = Ledger.memory()
    assert refused(led, b"") == "empty"
    assert refused(led, b" \r\n\t \n") == "empty"
    assert refused(led, a_text() + b"\x00") == "wrong_type"
    assert refused(led, b"PK\x03\x04" + an_email(), "ticket.eml") == "wrong_type"
    padded = an_email() + b"x" * (uploads.MAX_BYTES - len(an_email()))
    assert len(padded) == uploads.MAX_BYTES
    assert refused(led, padded + b"x") == "too_large"
    # text longer than its own limit, though far smaller than a file's
    text = a_text().decode()
    long = (text * (uploads.MAX_TEXT_CHARS // len(text) + 1))[: uploads.MAX_TEXT_CHARS].encode()
    assert len(long.decode()) == uploads.MAX_TEXT_CHARS
    assert refused(led, long + b"x") == "too_large"
    assert rows(led) == []
    assert set(uploads.ERRORS[:4]) == {"too_large", "wrong_type", "empty", "full"}
    # at the limits they are taken
    assert submit(led, padded)[1] and submit(led, long)[1]
    assert [r["kind"] for r in rows(led)] == ["eml", "text"]
    assert rows(led)[0]["size"] == uploads.MAX_BYTES and len(rows(led)[0]["body"]) == len(padded)


def test_a_full_waiting_list_refuses_politely_and_keeps_what_waits():
    led = Ledger.memory()
    bodies = texts(uploads.MAX_WAITING + 2)
    tokens = [submit(led, b)[0] for b in bodies[: uploads.MAX_WAITING]]
    assert led.uploads.waiting() == uploads.MAX_WAITING
    before = [dict(r) for r in rows(led)]
    assert refused(led, bodies[uploads.MAX_WAITING]) == "full"
    assert [dict(r) for r in rows(led)] == before
    # what already waits is one upload, full list or not
    assert submit(led, bodies[3]) == (tokens[3], False)
    # one being read still holds its place
    first = led.uploads.claim_next()
    assert (first["token"], first["state"]) == (tokens[0], "processing")
    assert led.uploads.waiting() == uploads.MAX_WAITING
    assert refused(led, bodies[uploads.MAX_WAITING]) == "full"
    # one read is out of the list: the next is taken, and the list is full again
    led.uploads.finish(first["id"], "nothing")
    token, new = submit(led, bodies[uploads.MAX_WAITING])
    assert new and led.uploads.get(token)["state"] == "waiting"
    assert refused(led, bodies[uploads.MAX_WAITING + 1]) == "full"
    assert led.uploads.get(tokens[0])["state"] == "nothing"


def test_the_oldest_upload_waiting_is_claimed_once_and_finishing_drops_its_bytes():
    led = Ledger.memory()
    assert led.uploads.claim_next() is None
    tokens = [submit(led, b)[0] for b in texts(3)]
    claimed = [led.uploads.claim_next() for _ in range(3)]
    assert [r["token"] for r in claimed] == tokens
    assert all(r["state"] == "processing" for r in claimed)
    assert led.uploads.claim_next() is None
    assert [r["state"] for r in rows(led)] == ["processing"] * 3
    mid = led.messages.insert(sha256="upload-message", source="upload", status="processed")
    led.uploads.finish(claimed[0]["id"], "filed", message_id=mid)
    led.uploads.finish(claimed[1]["id"], "nothing", error="no_model")
    led.uploads.finish(claimed[2]["id"], "failed", error="unreadable")
    done = rows(led)
    assert [(r["state"], r["message_id"], r["error"]) for r in done] == [
        ("filed", mid, None),
        ("nothing", None, "no_model"),
        ("failed", None, "unreadable"),
    ]
    assert all(r["body"] is None for r in done) and all(r["size"] > 0 for r in done)
    assert {"unreadable", "no_model"} <= set(uploads.ERRORS)
    with pytest.raises(ValueError):
        led.uploads.finish(claimed[0]["id"], "waiting")
    assert led.uploads.get("no such token") is None


def test_an_upload_left_being_read_three_times_is_given_up_and_the_rest_wait_again():
    """Taking an upload to read counts one reading. One a worker left being read waits again,
    until it has been taken MAX_READINGS times: a reading that kills the worker would otherwise
    repeat without end, so it fails, kept with its bytes, with the word `stopped`. Bytes handed
    in again after that are taken again from no count."""
    assert uploads.MAX_READINGS == 3 and "stopped" in uploads.ERRORS
    led = Ledger.memory()
    heavy, light = (submit(led, b)[0] for b in texts(2))
    assert led.uploads.requeue() == (0, 0)
    for n in range(1, uploads.MAX_READINGS):
        assert led.uploads.claim_next()["token"] == heavy  # read, and the worker dies
        assert led.uploads.get(heavy)["attempts"] == n
        assert led.uploads.requeue() == (1, 0)
        assert led.uploads.get(heavy)["state"] == "waiting"
    assert led.uploads.claim_next()["token"] == heavy  # the third reading
    assert led.uploads.claim_next()["token"] == light  # the first of this one
    assert led.uploads.requeue() == (1, 1)
    given_up, waits = led.uploads.get(heavy), led.uploads.get(light)
    assert (given_up["state"], given_up["error"], given_up["attempts"]) == (
        "failed",
        "stopped",
        uploads.MAX_READINGS,
    )
    assert given_up["body"] is not None  # kept
    assert (waits["state"], waits["attempts"]) == ("waiting", 1)
    assert led.uploads.claim_next()["token"] == light
    assert led.uploads.claim_next() is None  # the one given up is not taken again
    # handed in again, it is taken again, its count from nothing
    token, again = submit(led, given_up["body"])
    assert (token, again) == (heavy, True)
    assert (led.uploads.get(heavy)["state"], led.uploads.get(heavy)["attempts"]) == ("waiting", 0)


def test_two_uploads_racing_for_the_last_place_take_it_once(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    bodies = texts(uploads.MAX_WAITING + 1)
    with Ledger.open(path) as led:
        for body in bodies[: uploads.MAX_WAITING - 1]:
            submit(led, body)
    # each submission, once it has counted the list, waits a moment for the other to count it
    # too: two that could both count before either stores would both take the last place
    counted = threading.Barrier(2)
    real = repo.Uploads.waiting

    def waiting(self):
        n = real(self)
        try:
            counted.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return n

    monkeypatch.setattr(repo.Uploads, "waiting", waiting)
    results: dict[int, object] = {}

    def one(n: int) -> None:
        with Ledger.open(path) as led:
            try:
                results[n] = submit(led, bodies[uploads.MAX_WAITING - 1 + n])
            except uploads.Refused as exc:
                results[n] = exc.code

    threads = [threading.Thread(target=one, args=(n,)) for n in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(v if isinstance(v, str) else "taken" for v in results.values()) == [
        "full",
        "taken",
    ]
    assert [v[1] for v in results.values() if isinstance(v, tuple)] == [True]
    with Ledger.open(path) as led:
        assert len(rows(led)) == uploads.MAX_WAITING == led.uploads.waiting()


def test_the_table_takes_only_its_kinds_and_states():
    led = Ledger.memory()
    stamp = now_iso()

    def insert(n: int, kind: str = "text", state: str = "waiting", **kw) -> None:
        cols = {
            "token": f"t{n}",
            "kind": kind,
            "sha256": f"s{n}",
            "size": 1,
            "state": state,
            "created_at": stamp,
            "updated_at": stamp,
            **kw,
        }
        led.conn.execute(
            f"INSERT INTO uploads({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(cols.values()),
        )

    n = 0
    for kind in ("text", "eml", "pdf"):
        for state in ("waiting", "processing", "filed", "nothing", "failed"):
            n += 1
            insert(n, kind, state)
    for kind in ("zip", "png", "EML", ""):
        with pytest.raises(sqlite3.IntegrityError):
            insert(100, kind)
    for state in ("done", "read", "Waiting", ""):
        with pytest.raises(sqlite3.IntegrityError):
            insert(100, state=state)
    with pytest.raises(sqlite3.IntegrityError):  # a token is one upload's
        insert(100, token="t1")
    with pytest.raises(sqlite3.IntegrityError):  # and so are its bytes
        insert(100, sha256="s1")
    with pytest.raises(sqlite3.IntegrityError):  # the message it became exists
        insert(100, message_id=999)
    assert len(rows(led)) == 15
    # a new row waits
    led.conn.execute(
        "INSERT INTO uploads(token, kind, sha256, size, created_at, updated_at)"
        " VALUES ('t0', 'pdf', 's0', 1, ?, ?)",
        (stamp, stamp),
    )
    assert led.uploads.get("t0")["state"] == "waiting"
