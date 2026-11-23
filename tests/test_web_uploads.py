# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page's paste, upload and status pages over a ledger with no login: what a browser hands in
is stored as it came, refused by its content and its size, and named by a token alone. Every
body is built at run time from the corpus."""

import asyncio
import datetime as dt
import html
import json
import re
import subprocess
import sys

from helpers.web import ORIGIN, Page
from jwt import PyJWKClient
from test_uploads import a_pdf, a_text, an_email, texts
from test_web_auth import ACCESS, ME, Keys
from test_web_auth import PUBLIC as ADDRESS
from test_web_views import Controls

from tripbox import corpus, demo, status, uploads, util
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.web.app import create_app

SAME_SITE = {"Sec-Fetch-Site": "same-origin"}
CAP = uploads.MAX_BYTES + 65536  # the most of a body the page reads before it answers 413
ALERT = re.compile(r'<p class="error" role="alert" id="([^"]+)">([^<]*)</p>')
NOT_RUNNING = "The worker is not running; this waits until it is."
SOON = "It is read within a few seconds."
BOUNDARY = "sample-boundary"


def page_for(tmp_path, *, data_dir=None, config_dir=None, base="", env=None):
    """The page with no login (unless `env` names one) over the ledger in `data_dir` (a new one
    by default), served under `base`, and its settings."""
    config = config_dir or tmp_path / "config"
    config.mkdir(exist_ok=True)
    s = Settings.from_env(
        {
            "TRIPBOX_AUTH": "none",
            "TRIPBOX_CONFIG_DIR": str(config),
            "TRIPBOX_DATA_DIR": str(data_dir or tmp_path / "data"),
            "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
            "TRIPBOX_BASE_URL": ORIGIN + base,
            **(env or {}),
        }
    )
    assert s.problems == ()
    return create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True), s


def client_for(tmp_path, base, **kw):
    """The page of `page_for` served under `base`, its forms posted from its own pages."""
    app, s = page_for(tmp_path, base=base, **kw)
    return Page(app, base, headers=SAME_SITE), s


def beating(s, at=None, *, busy=False, stopped=False) -> None:
    """A worker's record on the ledger, its last beat at `at` (now): `busy` in a part that may
    run long, or `stopped` then."""
    when = (at or util.now()).isoformat()
    record = {"pid": 1, "host": "worker", "beat_at": when, "busy": busy}
    if stopped:
        record["stopped_at"] = when
    with Ledger.open(s.ledger_path) as led:
        led.kv.set(status.KV_WORKER, json.dumps(record))


def stored(s) -> list[dict]:
    with Ledger.open(s.ledger_path) as led:
        return [dict(r) for r in led.conn.execute("SELECT * FROM uploads ORDER BY id")]


def paste(c, text: str, **kw):
    return c.post("/add/paste", data={"text": text}, follow_redirects=False, **kw)


def upload(c, name: str, body: bytes, declared: str = "application/octet-stream", **kw):
    files = {"file": (name, body, declared)}
    return c.post("/add/upload", files=files, follow_redirects=False, **kw)


def alert(page: str) -> tuple[str, str]:
    """The id and the sentence of the page's one announced error."""
    (found,) = ALERT.findall(page)
    return found[0], html.unescape(found[1])


def described_by(page: str, name: str) -> list[str]:
    """The ids the control `name` points at, when it is marked invalid."""
    m = re.search(rf'name="{name}"[^>]*aria-describedby="([^"]*)"[^>]*aria-invalid="true"', page)
    return m.group(1).split() if m else []


def multipart(name: str) -> tuple[bytes, bytes]:
    """The head and the tail of a form posting one file `name`."""
    head = (
        f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\""
        "\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return head, f"\r\n--{BOUNDARY}--\r\n".encode()


def post_as_it_arrives(page: Page, chunks, *, length: str | None = None):
    """POST the chunks to the upload form's address on `page` one receive at a time, with no
    declared length unless `length`; (status, page, how many body bytes the app took)."""
    taken = 0
    source = iter(chunks)
    sent: list[dict] = []

    async def receive():
        nonlocal taken
        chunk = next(source, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        taken += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    headers = [
        (b"host", b"localhost:8110"),
        (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
        (b"sec-fetch-site", b"same-origin"),
    ]
    if length is not None:
        headers.append((b"content-length", length.encode()))
    else:
        headers.append((b"transfer-encoding", b"chunked"))
    path = page.url("/add/upload")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("localhost", 8110),
        "state": {},
    }
    asyncio.run(page.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body.decode(), taken


def endless(head: bytes, piece: bytes, total: int):
    """`head`, then `piece` again and again until `total` bytes have gone."""
    yield head
    sent = len(head)
    while sent < total:
        yield piece
        sent += len(piece)


def test_a_pasted_confirmation_is_stored_and_its_status_page_says_received(tmp_path, base):
    c, s = client_for(tmp_path, base)
    text = a_text().decode()
    r = paste(c, text)
    assert r.status_code == 303
    (row,) = stored(s)
    assert r.headers["location"] == c.url(f"/uploads/{row['token']}")
    assert (row["kind"], row["name"], row["state"], row["submitted_by"]) == (
        "text",
        None,
        "waiting",
        "local",
    )
    assert row["body"] == text.encode() and row["size"] == len(text.encode())
    beating(s)
    for state in ("waiting", "processing"):
        if state == "processing":
            with Ledger.open(s.ledger_path) as led:
                assert led.uploads.claim_next()["token"] == row["token"]
        page = c.get(c.local(r.headers["location"]))
        assert page.status_code == 200, state
        assert "Received." in page.text and SOON in page.text and NOT_RUNNING not in page.text
        head = page.text[: page.text.index("</head>")]
        assert '<meta http-equiv="refresh" content="5">' in head, state
        # what was pasted stays in the ledger: the page names the upload and its state alone
        for line in text.splitlines():
            if len(line.strip()) > 12:
                assert html.escape(line.strip()) not in page.text, line
        assert "Text · " in page.text


def test_a_paste_is_stored_without_the_white_space_before_it(tmp_path, base):
    """An email pasted after two empty lines and a space is stored from its first header and is
    told as an email; the same text pasted without them is the same upload; an uploaded file
    that begins with an empty line is stored as it is."""
    c, s = client_for(tmp_path, base)
    mail = an_email().decode()
    first = paste(c, "\n\n " + mail)
    assert first.status_code == 303
    (row,) = stored(s)
    assert row["body"] == mail.encode() and row["kind"] == "eml"
    assert uploads.sniff(row["body"]) == "eml"
    again = paste(c, mail)
    assert again.headers["location"] == first.headers["location"]
    assert len(stored(s)) == 1
    # a file's bytes are never touched
    held = b"\n" + a_text()
    r = upload(c, "notes.txt", held)
    assert r.status_code == 303
    assert stored(s)[-1]["body"] == held


def test_a_waiting_upload_says_so_while_no_worker_runs(tmp_path, base):
    """Nothing reads an upload until a worker beats on the ledger: while none does, a waiting
    upload's page says so, and still reloads itself. One that is being read was taken by a
    worker, so its page says it is read soon whatever the record says."""
    c, s = client_for(tmp_path, base)
    landed = c.local(paste(c, a_text().decode()).headers["location"])

    def said() -> tuple[bool, bool]:
        page = c.get(landed).text
        assert "Received." in page and '<meta http-equiv="refresh" content="5">' in page
        return NOT_RUNNING in page, SOON in page

    assert said() == (True, False)  # no worker has ever run
    beat = util.now()
    beating(s, beat)
    for late, running in ((0, True), (29, True), (-29, True), (30, False), (31, False)):
        with util.clock_at(beat + dt.timedelta(seconds=late)):
            assert said() == (not running, running), late
    # a worker in the middle of a part that may run long is running for ten minutes
    beating(s, beat, busy=True)
    busy = status.BUSY_SECONDS
    for late, running in ((31, True), (busy - 1, True), (busy, False)):
        with util.clock_at(beat + dt.timedelta(seconds=late)):
            assert said() == (not running, running), late
    # one that stopped gave the ledger up at once
    beating(s, beat, stopped=True)
    with util.clock_at(beat):
        assert said() == (True, False)
    with Ledger.open(s.ledger_path) as led:
        led.kv.set(status.KV_WORKER, "not a record")
        assert status.read(led) is None
    assert said() == (True, False)
    with Ledger.open(s.ledger_path) as led:
        led.uploads.claim_next()
    assert said() == (False, True)


def test_an_uploaded_email_and_a_pdf_are_stored_with_their_cleaned_names(tmp_path, base):
    c, s = client_for(tmp_path, base)
    sent = [
        ("booking.eml", an_email(), "text/plain", "eml"),
        ("e-ticket.pdf", a_pdf(), "message/rfc822", "pdf"),
        ("itinerary.txt", a_text(1), "application/pdf", "text"),
    ]
    for name, body, declared, kind in sent:
        r = upload(c, name, body, declared)
        assert r.status_code == 303, name
        token = r.headers["location"].removeprefix(c.url("/uploads/"))
        row = next(x for x in stored(s) if x["token"] == token)
        assert (row["kind"], row["name"], row["size"], row["body"]) == (
            kind,
            name,
            len(body),
            body,
        )
        page = c.get(c.local(r.headers["location"]))
        assert page.status_code == 200 and f"{name} · " in page.text and "Received." in page.text
    assert len(stored(s)) == 3


def test_an_oversize_upload_is_refused_at_ten_megabytes_and_stores_nothing(tmp_path, base):
    c, s = client_for(tmp_path, base)
    mail = an_email()
    padded = mail + b"x" * (uploads.MAX_BYTES - len(mail))
    r = upload(c, "big.eml", padded + b"x")
    assert r.status_code == 413
    assert alert(r.text)[1] == "That file is larger than 10 MB."
    assert stored(s) == []
    # a text longer than a confirmation is refused the same way, the form kept empty
    text = a_text().decode()
    long = text * (uploads.MAX_TEXT_CHARS // len(text) + 1)
    r = paste(c, long)
    assert r.status_code == 413
    assert alert(r.text)[1] == "That text is longer than 200,000 characters."
    assert text[:40] not in r.text and stored(s) == []
    # ten megabytes are taken
    r = upload(c, "big.eml", padded)
    assert r.status_code == 303
    (row,) = stored(s)
    assert row["size"] == uploads.MAX_BYTES and row["body"] == padded


def test_a_body_with_no_declared_length_is_counted_as_it_arrives(tmp_path, base):
    c, s = client_for(tmp_path, base)
    mail = an_email()
    head, tail = multipart("booking.eml")
    pieces = [mail[i : i + 512] for i in range(0, len(mail), 512)]
    status, _, taken = post_as_it_arrives(c, [head, *pieces, tail])
    assert status == 303 and taken == len(head) + len(mail) + len(tail)
    (row,) = stored(s)
    assert row["body"] == mail and row["name"] == "booking.eml"
    # past the cap the page stops reading and answers, whatever length is declared
    piece = b"x" * 65536
    for length in (None, "100"):
        status, page, taken = post_as_it_arrives(
            c, endless(head + mail, piece, 2 * CAP), length=length
        )
        assert status == 413, length
        assert alert(page)[1] == "That file is larger than 10 MB."
        assert CAP < taken <= CAP + len(piece), (length, taken)
        assert len(stored(s)) == 1
    # a declared length past the cap is answered before a byte is read
    status, _, taken = post_as_it_arrives(
        c, endless(head + mail, piece, 2 * CAP), length=str(CAP + 1)
    )
    assert (status, taken) == (413, 0)


def test_a_wrong_type_and_an_archive_are_refused_by_content_not_by_name(tmp_path, base):
    c, s = client_for(tmp_path, base)
    sentence = "That file is not an email, a PDF or text."
    for name, body in (
        ("ticket.eml", b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + an_email()),
        ("e-ticket.pdf", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + a_pdf()),
        ("itinerary.txt", b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + a_text()),
    ):
        r = upload(c, name, body, "message/rfc822")
        assert r.status_code == 415, name
        assert alert(r.text)[1] == sentence
    r = paste(c, a_text().decode() + "\x00")
    assert r.status_code == 415
    assert alert(r.text)[1] == "That text holds control characters. Paste the text alone."
    assert stored(s) == []
    # a name says nothing either way: a PDF named as text is a PDF
    assert upload(c, "itinerary.txt", a_pdf()).status_code == 303
    assert [r["kind"] for r in stored(s)] == ["pdf"]


def test_a_path_in_the_file_name_never_reaches_the_ledger_or_the_page(tmp_path, base):
    c, s = client_for(tmp_path, base)
    names = ("../../etc/ticket.eml", "scans\\trips\\ticket.eml", "..\\ticket.eml")
    for n, name in enumerate(names):
        r = upload(c, name, an_email(n))
        assert r.status_code == 303, name
        page = c.get(c.local(r.headers["location"])).text
        assert "ticket.eml · " in page
        for part in ("..", "etc/", "scans", "trips\\"):
            assert part not in page, (name, part)
    assert [r["name"] for r in stored(s)] == ["ticket.eml"] * 3
    # a name with nothing left of it is no name
    r = upload(c, "\x1b", an_email(3))
    assert r.status_code == 303 and stored(s)[-1]["name"] is None
    assert "An email · " in c.get(c.local(r.headers["location"])).text


def test_the_same_upload_twice_lands_on_the_same_status_page(tmp_path, base):
    c, s = client_for(tmp_path, base)
    first = upload(c, "booking.eml", an_email())
    again = upload(c, "copy.eml", an_email())
    assert first.status_code == again.status_code == 303
    assert first.headers["location"] == again.headers["location"]
    pasted = paste(c, a_text().decode())
    assert paste(c, a_text().decode()).headers["location"] == pasted.headers["location"]
    assert [(r["kind"], r["name"]) for r in stored(s)] == [("eml", "booking.eml"), ("text", None)]


def test_a_full_list_answers_429_with_a_message(tmp_path, base):
    c, s = client_for(tmp_path, base)
    bodies = texts(uploads.MAX_WAITING + 1)
    with Ledger.open(s.ledger_path) as led:
        for body in bodies[: uploads.MAX_WAITING]:
            uploads.submit(led, body, name=None, submitted_by=None)
    full = "20 uploads are waiting to be read already. Send this again once they are."
    r = paste(c, bodies[-1].decode())
    assert r.status_code == 429 and alert(r.text)[1] == full
    assert html.escape(bodies[-1].decode().splitlines()[0]) in r.text  # what was typed is kept
    r = upload(c, "booking.eml", an_email())
    assert r.status_code == 429 and alert(r.text)[1] == full
    assert len(stored(s)) == uploads.MAX_WAITING
    # one that already waits is no new upload: its page
    r = paste(c, bodies[0].decode())
    assert r.status_code == 303 and r.headers["location"].startswith(c.url("/uploads/"))


def test_an_upload_from_another_site_is_refused(tmp_path, base):
    c, s = client_for(tmp_path, base)
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": ""}):
        r = paste(c, a_text().decode(), headers=headers)
        assert r.status_code == 403, headers
        r = upload(c, "booking.eml", an_email(), headers=headers)
        assert r.status_code == 403, headers
    bare = Page(page_for(tmp_path, base=base)[0], base)
    assert bare.post("/add/paste", data={"text": a_text().decode()}).status_code == 403
    files = {"file": ("booking.eml", an_email(), "message/rfc822")}
    assert bare.post("/add/upload", files=files).status_code == 403
    assert stored(s) == []


def test_an_unknown_token_is_the_404_page(tmp_path, base):
    c, s = client_for(tmp_path, base)
    r = paste(c, a_text().decode())
    (row,) = stored(s)
    unknown = c.get("/trips/999999")
    real = row["token"]
    for token in ("x" * 22, real[:-1], real.upper(), str(row["id"]), "a" * 400):
        page = c.get(f"/uploads/{token}")
        assert page.status_code == 404, token
        assert page.text == unknown.text, token
    # a path holding a control character is refused before any route: a bare 404
    page = c.get("/uploads/%00")
    assert (page.status_code, page.text) == (404, "")
    assert c.get(c.local(r.headers["location"])).status_code == 200


def test_who_submitted_it_is_recorded_as_the_login_names_them(tmp_path, monkeypatch, base):
    """The address Cloudflare Access signed in, or "local" on the page with no login."""
    keys = Keys()
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: keys.jwks)
    access = {**ACCESS, "TRIPBOX_AUTH": "cloudflare", "TRIPBOX_BASE_URL": ADDRESS + base}
    behind, s = page_for(tmp_path, base=base, env=access)
    signed = Page(behind, base, headers={**SAME_SITE, "Cf-Access-Jwt-Assertion": keys.token()})
    assert paste(signed, a_text().decode()).status_code == 303
    assert upload(signed, "a.pdf", a_pdf()).status_code == 303
    local, _ = page_for(tmp_path, base=base)
    assert paste(Page(local, base, headers=SAME_SITE), a_text(1).decode()).status_code == 303
    assert [r["submitted_by"] for r in stored(s)] == [ME] * 2 + ["local"]


def test_every_control_of_the_three_forms_has_a_label_and_errors_are_announced(
    tmp_path, base
):
    c, s = client_for(tmp_path, base)
    chooser = c.get("/add")
    assert chooser.status_code == 200
    assert f'href="{c.url("/add/paste")}"' in chooser.text
    assert f'href="{c.url("/add/upload")}"' in chooser.text
    for path, field, button in (
        ("/add/paste", "text", "Send"),
        ("/add/upload", "file", "Upload"),
    ):
        page = c.get(path)
        assert page.status_code == 200 and not ALERT.search(page.text)
        controls = Controls()
        controls.feed(page.text)
        assert controls.found == [field] and controls.unlabelled == [], path
        assert f">{button}</button>" in page.text
        assert f'href="{c.url("/add")}">Cancel</a>' in page.text
        assert described_by(page.text, field) == []
    for r, field in (
        (paste(c, "  \n "), "text"),
        (upload(c, "empty.eml", b""), "file"),
        (upload(c, "ticket.eml", b"PK\x03\x04" + an_email()), "file"),
    ):
        assert r.status_code in (400, 415)
        where, sentence = alert(r.text)
        assert sentence and where in described_by(r.text, field), field
        assert r.text.index("role=\"alert\"") < r.text.index(f'name="{field}"')
        controls = Controls()
        controls.feed(r.text)
        assert controls.unlabelled == []
    assert stored(s) == []


def test_the_new_pages_keep_the_content_security_policy(tmp_path, base):
    c, s = client_for(tmp_path, base)
    landed = c.local(paste(c, a_text().decode()).headers["location"])
    for path in ("/add", "/add/paste", "/add/upload", landed):
        r = c.get(path)
        assert r.status_code == 200, path
        csp = r.headers.get("content-security-policy", "")
        assert "script-src 'self'" in csp and "form-action 'self'" in csp, path
        (script,) = re.findall(r"<script\b[^>]*>", r.text, re.I)
        assert script.startswith(f'<script src="{c.url("/static/app.js")}?v='), path
        assert " style=" not in r.text and "<style" not in r.text, path
        assert "data-refresh" not in r.text, path  # a reload would drop what was typed


def test_the_page_container_needs_nothing_secret_to_take_an_upload():
    """The page stores what it is handed and reads none of it: neither the upload module nor
    the page's views load the pipeline, the model's client, the flight log's or the mailbox's."""
    shown = (
        "import sys, tripbox.uploads, tripbox.web.views; "
        "print(sorted(m for m in sys.modules if m.startswith(("
        "'tripbox.pipeline', 'tripbox.parse.provider', 'tripbox.parse.anthropic_provider',"
        " 'tripbox.parse.openai_provider', 'tripbox.write.airtrail', 'tripbox.mail.imap',"
        " 'anthropic'))))"
    )
    run = subprocess.run([sys.executable, "-c", shown], capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "[]", run.stdout
    alone = (
        "import json, sys, tripbox.uploads; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('tripbox'))))"
    )
    run = subprocess.run([sys.executable, "-c", alone], capture_output=True, text=True, check=True)
    assert set(json.loads(run.stdout)) <= {"tripbox", "tripbox.uploads"}, run.stdout
    # whether a worker runs, the page reads from the ledger and the clock alone
    alone = alone.replace("tripbox.uploads;", "tripbox.status;")
    run = subprocess.run([sys.executable, "-c", alone], capture_output=True, text=True, check=True)
    assert set(json.loads(run.stdout)) == {"tripbox", "tripbox.status", "tripbox.util"}, run.stdout


def test_the_upload_status_page_says_why_it_waits(tmp_path, base):
    """An upload whose message waits for the model says why, and when it is read: once the
    budget allows, or when the model answers again. It is no dead end and no reading under way:
    the page names no missing booking and does not reload itself."""
    client, s = client_for(tmp_path, base)
    said = {
        "over_budget": "The model's budget is used, so this waits. It is read when the budget"
        " allows.",
        "model_down": "The model could not be reached, so this waits. It is tried again every"
        " few minutes.",
    }
    tokens = {}
    with Ledger.open(s.ledger_path) as led:
        for n, why in enumerate(said):
            token, _ = uploads.submit(led, an_email(n), name=f"b{n}.eml", submitted_by=None)
            row = led.uploads.claim_next()
            led.uploads.finish(row["id"], "nothing", error=why)
            tokens[why] = token
    assert set(said) <= set(uploads.ERRORS)
    for why, sentence in said.items():
        page = client.get(f"/uploads/{tokens[why]}")
        shown = html.unescape(page.text)  # as it reads
        assert page.status_code == 200 and sentence in shown, why
        assert "No booking found" not in shown and "refresh" not in shown, why
        assert f'href="{client.url("/add")}"' in shown, why  # it can still be typed in by hand
        others = [other for other in said.values() if other != sentence]
        assert not any(other in shown for other in others), why


def test_a_given_up_upload_says_so_and_offers_the_form(tmp_path, base):
    """An upload tripbox stopped reading MAX_READINGS times is given up: its page says so, says
    it is kept, and links the form to add the booking by hand; it does not reload itself."""
    client, s = client_for(tmp_path, base)
    with Ledger.open(s.ledger_path) as led:
        token, _ = uploads.submit(led, an_email(), name="b0.eml", submitted_by=None)
        for _ in range(uploads.MAX_READINGS):
            assert led.uploads.claim_next()["token"] == token
            led.uploads.requeue()
    page = client.get(f"/uploads/{token}")
    shown = html.unescape(page.text)
    assert page.status_code == 200
    assert (
        "tripbox stopped three times while reading this. It is kept; add the booking by hand."
        in shown
    )
    assert f'href="{client.url("/add")}"' in shown
    assert "refresh" not in shown and "Received." not in shown
    # one that could not be read for another reason does not say so
    with Ledger.open(s.ledger_path) as led:
        other, _ = uploads.submit(led, an_email(1), name="b1.eml", submitted_by=None)
        led.uploads.finish(led.uploads.claim_next()["id"], "failed", error="unreadable")
    assert "stopped three times" not in client.get(f"/uploads/{other}").text


def test_the_status_page_says_what_became_of_the_upload(tmp_path, base):
    """Once read: the bookings of the message it became, each a link to where it is shown (its
    trip, or Unsorted); no booking, with a word on a missing model; or that it could not be read.
    The list and a trip's page link the way in."""
    c = corpus.load(corpus.default_dir())
    seeded = demo.seed(c, tmp_path / "demo", c.anchor)
    assert seeded.failed == ()
    client, s = client_for(
        tmp_path, base, data_dir=tmp_path / "demo" / "data", config_dir=tmp_path / "demo" / "config"
    )
    with Ledger.open(s.ledger_path) as led:
        (undated,) = led.trip_items.unsorted()
        unsorted_message = led.items.by_trip_item(undated["id"])[0]["message_id"]
        in_trips = led.conn.execute(
            "SELECT i.message_id FROM items i JOIN trip_items t ON t.id = i.trip_item_id"
            " WHERE t.trip_id IS NOT NULL ORDER BY i.id LIMIT 1"
        ).fetchone()[0]
        filed = {}
        for n, (mid, state, error) in enumerate(
            (
                (in_trips, "filed", None),
                (unsorted_message, "filed", None),
                (None, "nothing", None),
                (None, "nothing", "no_model"),
                (None, "failed", "unreadable"),
            )
        ):
            token, _ = uploads.submit(led, an_email(n), name=f"b{n}.eml", submitted_by=None)
            row = led.uploads.claim_next()
            assert row["token"] == token
            led.uploads.finish(row["id"], state, message_id=mid, error=error)
            filed[n] = token
        bookings = {
            mid: list(
                dict.fromkeys(
                    r["trip_item_id"] for r in led.items.by_message(mid) if r["trip_item_id"]
                )
            )
            for mid in (in_trips, unsorted_message)
        }
        places = {
            r["id"]: r["trip_id"]
            for r in led.conn.execute("SELECT id, trip_id FROM trip_items").fetchall()
        }
    for n, mid in ((0, in_trips), (1, unsorted_message)):
        page = client.get(f"/uploads/{filed[n]}")
        assert page.status_code == 200 and "Filed" in page.text
        assert "refresh" not in page.text and "Received." not in page.text
        links = re.findall(r'<a class="choice" href="([^"]+)"', page.text)
        assert links == [
            client.url(f"/trips/{places[i]}#item-{i}") if places[i] else client.url("/#unsorted")
            for i in bookings[mid]
        ]
        assert links and all(
            client.get(client.local(link.split("#")[0])).status_code == 200 for link in links
        )
    assert client.url("/#unsorted") in client.get(f"/uploads/{filed[1]}").text
    nothing = client.get(f"/uploads/{filed[2]}").text
    assert "No booking found — fill it in by hand" in nothing
    assert "No model is set up" not in nothing and f'href="{client.url("/add")}"' in nothing
    no_model = client.get(f"/uploads/{filed[3]}").text
    assert "No booking found — fill it in by hand" in no_model
    assert (
        "No model is set up, so only confirmations that carry structured data are read."
        in no_model
    )
    failed = client.get(f"/uploads/{filed[4]}").text
    assert "This could not be read." in failed and f'href="{client.url("/add")}"' in failed
    for token in filed.values():
        page = client.get(f"/uploads/{token}").text
        assert "refresh" not in page and "Received." not in page
    # the way in: the list's header and a trip's menu
    listing = client.get("/").text
    add = re.escape(client.url("/add"))
    assert re.search(rf'<a [^>]*href="{add}" aria-label="Add a booking"', listing)
    trip_id = places[bookings[in_trips][0]]
    trip = client.get(f"/trips/{trip_id}").text
    assert re.search(rf'<a [^>]*href="{add}\?trip={trip_id}"[^>]*>Add a booking</a>', trip)
