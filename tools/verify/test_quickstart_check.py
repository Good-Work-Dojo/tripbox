# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tools/verify/quickstart_check.py against a fake page on loopback that behaves as the trips page
does on the steps the checker walks, and can be told to get one of them wrong."""

import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

HERE = Path(__file__).resolve().parent
CHECKER = HERE / "quickstart_check.py"
PASSWORD = "Sample-family-" + secrets.token_hex(8)
MARK = "SAMPLE-BODY-" + secrets.token_hex(8)
SAMPLE = f"From: Sample Airways <tickets@airline.example>\nSubject: Your trip\n\n{MARK}\n"
STEPS = ("health", "sign-in", "first-run", "paste", "filed", "trip", "sign-out")
FIRST_RUN = "<p>No trips yet</p><a href='/add'>Add a booking</a>"


class Page:
    """What the fake page holds, and the one step it is told to get wrong."""

    def __init__(self, broken: str | None):
        self.broken = broken
        self.sessions: set[str] = set()
        self.uploads: dict[str, float] = {}
        self.trips = 0
        self.posts_without_origin = 0
        self.origin = ""


def handler(page: Page):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def send(self, code, body="", headers=()):
            data = body.encode()
            self.send_response(code)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def session(self):
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "tb_session" and value in page.sessions:
                    return value
            return None

        def challenge(self):
            if "text/html" in self.headers.get("Accept", ""):
                return self.send(303, headers=[("Location", "/login?next=%2F")])
            return self.send(401, "unauthorized")

        def do_GET(self):
            path = self.path
            if path == "/healthz":
                return self.send(200, "no" if page.broken == "health" else "ok")
            if path.startswith("/login"):
                return self.send(200, "<form method=post action=/login></form>")
            if not self.session():
                return self.challenge()
            if path == "/":
                empty = page.trips == 0 or page.broken == "trip"
                if page.broken == "first-run":
                    return self.send(200, "<p>Your trips</p>")
                return self.send(200, FIRST_RUN if empty else "<a href='/trips/1'>Trip</a>")
            if path.startswith("/uploads/"):
                token = path.removeprefix("/uploads/")
                if token not in page.uploads:
                    return self.send(404, "no such upload")
                if page.broken == "filed" or time.monotonic() - page.uploads[token] < 1.5:
                    return self.send(200, "<p>Received.</p><p>It is read within seconds.</p>")
                page.trips = 1
                return self.send(
                    200, "<p class=state-head>Filed</p>"
                    "<a href='/'>Back to trips</a><a href='/trips/1#item-1'>A flight</a>",
                )  # fmt: skip
            if path == "/trips/1" and page.trips and page.broken != "trip":
                return self.send(200, "<h1>Trip</h1>")
            return self.send(404, f"not found: {MARK}")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode())
            if self.headers.get("Origin") != page.origin:
                page.posts_without_origin += 1
                return self.send(403, "cross-site")
            if self.path == "/login":
                typed = form.get("password", [""])[0]
                right = typed == PASSWORD
                if page.broken == "sign-in":
                    right = not right
                if not right:
                    return self.send(401, f"wrong password {typed}")
                token = secrets.token_hex(16)
                page.sessions.add(token)
                return self.send(303, headers=[
                    ("Location", "/"),
                    ("Set-Cookie", f"tb_session={token}; HttpOnly; Path=/; Max-Age=2592000"),
                    ("Set-Cookie", "tb_device=d1; HttpOnly; Path=/; Max-Age=2592000"),
                ])  # fmt: skip
            if self.path == "/logout":
                token = self.session()
                if token and page.broken != "sign-out":
                    page.sessions.discard(token)
                return self.send(303, headers=[
                    ("Location", "/login?f=out"),
                    ("Set-Cookie", "tb_session=; HttpOnly; Path=/; Max-Age=0"),
                ])  # fmt: skip
            if not self.session():
                return self.challenge()
            if self.path == "/add/paste":
                if form.get("text", [""])[0] != SAMPLE or page.broken == "paste":
                    return self.send(200, f"<textarea>{form.get('text', [''])[0]}</textarea>")
                token = secrets.token_hex(8)
                page.uploads[token] = time.monotonic()
                return self.send(303, headers=[("Location", f"/uploads/{token}")])
            return self.send(404, "not found")

    return Handler


@pytest.fixture
def serve():
    servers = []

    def start(broken=None):
        page = Page(broken)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(page))
        page.origin = f"http://localhost:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return page

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def check(tmp_path, page, *extra):
    secret = tmp_path / "password"
    secret.write_text(PASSWORD + "\n")
    sample = tmp_path / "email.eml"
    sample.write_text(SAMPLE)
    return subprocess.run(
        [sys.executable, "-I", str(CHECKER), "--base-url", page.origin,
         "--password-file", str(secret), "--sample", str(sample), *extra],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def test_a_page_that_files_the_sample_passes_every_step(serve, tmp_path):
    page = serve()
    r = check(tmp_path, page, "--timeout", "20")
    assert r.returncode == 0, (r.stdout, r.stderr)
    lines = r.stdout.splitlines()
    assert lines == [f"quickstart check: {step} ok" for step in STEPS] + ["quickstart check: ok"]
    assert page.posts_without_origin == 0  # every post carried the page's origin
    assert page.sessions == set()  # signed out on the server
    assert PASSWORD not in r.stdout + r.stderr and MARK not in r.stdout + r.stderr


@pytest.mark.parametrize("step", STEPS)
def test_each_failing_step_is_named_and_nothing_secret_is_printed(serve, tmp_path, step):
    page = serve(step)
    r = check(tmp_path, page, "--timeout", "3")
    assert r.returncode == 1, (r.stdout, r.stderr)
    lines = r.stdout.splitlines()
    done = STEPS[: STEPS.index(step)]
    assert lines == [f"quickstart check: {s} ok" for s in done] + [f"quickstart check: FAIL {step}"]
    said = r.stdout + r.stderr
    assert PASSWORD not in said and MARK not in said
    assert "Sample Airways" not in said
    if step == "health":  # and a page that is not there at all fails it too

        class Gone:
            origin = "http://127.0.0.1:9"  # nothing listens on the discard port

        r = check(tmp_path, Gone, "--timeout", "2")
        assert (r.returncode, r.stdout) == (1, "quickstart check: FAIL health\n")

