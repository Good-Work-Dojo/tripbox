# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""AirTrail and tripbox at one hostname (tools/verify/onehost/): the probe against a fake proxy on
loopback that answers as both apps behind the recipe do, and can be told to get one step wrong;
served.py against a fake AirTrail whose scripts do or do not hold the Trips button; and the
harness's compose file."""

import json
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "onehost"
PROBE = HARNESS / "probe.py"
SERVED = HARNESS / "served.py"
HOST = "flights.example.com"
PATH = "/tripbox"
PASSWORD = "Sample-family-" + secrets.token_hex(8)
KEY = "sample-key-" + secrets.token_hex(12)
MARK = "SAMPLE-BODY-" + secrets.token_hex(8)
SAMPLE = f"From: Sample Airways <tickets@airline.example>\nSubject: Your trip\n\n{MARK}\n"
STEPS = ("airtrail", "tripbox", "neighbours", "flight-log-link", "journey")
POLICY = "default-src 'self'; script-src 'self'; frame-ancestors 'none'"
APP_PAGE = '<html><script type="module">import("./_app/immutable/entry/start.js")</script></html>'


def serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def servers():
    started = []

    def start(handler_class):
        server, url = serve(handler_class)
        started.append(server)
        return url

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


class Hostname:
    """What a proxy in front of AirTrail (at /) and tripbox (under /tripbox) answers."""

    def __init__(self, broken=None):
        self.broken = broken
        self.sessions: set[str] = set()
        self.filed_at: float | None = None
        self.wrong_host = 0


def proxy_handler(site: Hostname):
    cookie_path = "/" if site.broken == "tripbox" else PATH

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # quiet
            pass

        def send(self, code, body="", headers=(), tripbox=True):
            data = body.encode()
            self.send_response(code)
            if tripbox:
                self.send_header("Content-Security-Policy", POLICY)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def airtrail(self):
            """AirTrail's own answer, as it sends one: no policy of tripbox's."""
            path = urlsplit(self.path).path
            if path == "/api/v1":
                name = "Elsewhere" if site.broken == "airtrail" else "AirTrail"
                return self.send(200, json.dumps({"instance": {"name": name}}), tripbox=False)
            if path == "/api/v1/flights":
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self.send(401, "{}", tripbox=False)
                cursor = parse_qs(urlsplit(self.path).query).get("cursor", [""])[0]
                if not cursor:  # the demo's own flights first, a page of them
                    seeded = [{"id": n, "note": "seeded", "passengers": []} for n in range(100)]
                    return self.send(200, json.dumps({"data": seeded,
                                                      "page": {"nextCursor": "two"}}),
                                     tripbox=False)
                ours = []
                if site.filed_at is not None and time.monotonic() - site.filed_at > 1.0:
                    guests = 2 if site.broken == "journey" else 3
                    ours = [{"id": 200 + n, "note": f"tripbox · PNR X{n}",
                             "passengers": [{"user": {"id": "u1"}}]
                             + [{"user": None, "guestName": "Sample guest"}] * guests}
                            for n in range(2)]
                return self.send(200, json.dumps({"data": ours, "page": {}}), tripbox=False)
            if path == "/login":
                return self.send(200, APP_PAGE, tripbox=False)
            return self.send(302, headers=[("Location", "/login")], tripbox=False)

        def session(self):
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "tb_session" and value in site.sessions:
                    return value
            return None

        def route(self):
            """The path under /tripbox, or None once answered (AirTrail's, or a wrong Host)."""
            if self.headers.get("Host") != HOST:
                site.wrong_host += 1
                self.send(421, tripbox=False)
                return None
            path = urlsplit(self.path).path
            if path == PATH:
                self.send(307, headers=[("Location", PATH + "/")])
                return None
            if path == PATH + "/api/v1" and site.broken == "api":
                self.airtrail()  # a proxy that hands tripbox's path to AirTrail
                return None
            if not path.startswith(PATH + "/"):
                if path == PATH + "x" and site.broken == "neighbours":
                    self.send(302, headers=[("Location", "/login")])  # tripbox's header
                    return None
                self.airtrail()
                return None
            return path[len(PATH):]

        def do_GET(self):
            path = self.route()
            if path is None:
                return
            if path == "/healthz":
                return self.send(200, "ok")
            if not self.session():  # signed out, sent to sign-in before routing
                return self.send(303, headers=[("Location", f"{PATH}/login")])
            if path == "/":
                link = "" if site.broken == "flight-log-link" else f"http://{HOST}"
                return self.send(200, f'<a href="{link}">Flight log</a>')
            if path == "/uploads/u1":
                filed = site.filed_at is not None and time.monotonic() - site.filed_at > 0.5
                return self.send(200, "<p>Filed</p>" if filed else "<p>Received.</p>")
            return self.send(404, f"not found: {MARK}")

        def do_POST(self):
            path = self.route()
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            if path is None:
                return
            if self.headers.get("Origin") != f"http://{HOST}":
                return self.send(403, "cross-site")
            if path == "/login":
                if form.get("password", [""])[0] != PASSWORD:
                    return self.send(401, "wrong password")
                token = secrets.token_hex(16)
                site.sessions.add(token)
                return self.send(303, headers=[
                    ("Location", f"{PATH}/"),
                    ("Set-Cookie", f"tb_session={token}; HttpOnly; Path={cookie_path}"),
                ])  # fmt: skip
            if not self.session():
                return self.send(303, headers=[("Location", f"{PATH}/login")])
            if path == "/add/paste" and form.get("text", [""])[0] == SAMPLE:
                site.filed_at = time.monotonic()
                return self.send(303, headers=[("Location", f"{PATH}/uploads/u1")])
            return self.send(404, "not found")

    return Handler


def probe(tmp_path, proxy, *extra):
    sample = tmp_path / "email.eml"
    sample.write_text(SAMPLE)
    return subprocess.run(
        [sys.executable, "-I", str(PROBE), "--proxy", proxy, "--host", HOST, "--path", PATH,
         "--sample", str(sample), *extra],
        env={"ONEHOST_PASSWORD": PASSWORD, "ONEHOST_AIRTRAIL_KEY": KEY, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip


def test_the_probe_passes_two_apps_at_one_name_and_names_each_failing_step(servers, tmp_path):
    site = Hostname()
    r = probe(tmp_path, servers(proxy_handler(site)), "--timeout", "20", "--wait", "5")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.splitlines() == [f"onehost probe: {s} ok" for s in STEPS] + [
        "onehost probe: ok"]
    assert site.wrong_host == 0
    said = r.stdout + r.stderr
    assert PASSWORD not in said and KEY not in said and MARK not in said

    for step in STEPS:
        site = Hostname(step)
        r = probe(tmp_path, servers(proxy_handler(site)), "--timeout", "4", "--wait", "3")
        assert r.returncode == 1, (step, r.stdout, r.stderr)
        done = STEPS[: STEPS.index(step)]
        assert r.stdout.splitlines() == [f"onehost probe: {s} ok" for s in done] + [
            f"onehost probe: FAIL {step}"], step
        said = r.stdout + r.stderr
        assert PASSWORD not in said and KEY not in said and MARK not in said

    # <path>/api/v1 answered by AirTrail, not sent to tripbox's sign-in, fails the same step
    r = probe(tmp_path, servers(proxy_handler(Hostname("api"))), "--timeout", "4", "--wait", "3")
    assert r.returncode == 1 and r.stdout.splitlines()[-1] == "onehost probe: FAIL neighbours"

    # a proxy that is not there fails the first step; a bad argument is a usage error
    r = probe(tmp_path, "http://127.0.0.1:9", "--wait", "1")
    assert (r.returncode, r.stdout) == (1, "onehost probe: FAIL airtrail\n")
    for proxy in ("https://proxy:443", "proxy:80", "http://proxy:80/tripbox"):
        assert probe(tmp_path, proxy).returncode == 2, proxy


# ---------------------------------------------------------------- served.py


class Airtrail:
    """An AirTrail's pages and scripts: its sign-in page loads the app's entry, which imports
    its nodes, which import the dock's chunk, as a SvelteKit build does."""

    def __init__(self, dock: str, files: dict[str, str] | None = None):
        page = ('<link rel="modulepreload" href="./_app/immutable/entry/start.js">'
                '<script type="module">Promise.all([import("./_app/immutable/entry/start.js"),'
                'import("./_app/immutable/entry/app.js")])</script>'
                '<script src="/elsewhere.js"></script>')
        self.files = files if files is not None else {
            "/login": page,
            "/_app/immutable/entry/start.js": 'export const s=1;',
            "/_app/immutable/entry/app.js": 'const n=[()=>import("../nodes/0.js")];',
            "/_app/immutable/nodes/0.js": 'import{a as t}from"../chunks/dock.js";',
            "/_app/immutable/chunks/dock.js": dock,
            # outside /_app/: never fetched, whatever it holds
            "/elsewhere.js": 'const t={label:"Trips",testId:"trips-button",href:"/tripbox"};',
        }
        self.asked: list[str] = []


def airtrail_handler(app: Airtrail):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # quiet
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            app.asked.append(path)
            if self.headers.get("Host") != HOST:
                code, body, where = 421, "", ""
            elif path == "/":
                code, body, where = 302, "", "/login"
            elif callable(app.files):
                code, body, where = 200, app.files(path), ""
            elif path in app.files:
                code, body, where = 200, app.files[path], ""
            else:
                code, body, where = 404, "", ""
            data = body.encode()
            self.send_response(code)
            if where:
                self.send_header("Location", where)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def served(url, trips="/tripbox", *extra, host=HOST):
    args = [sys.executable, "-I", str(SERVED), "--url", url, "--trips", trips, *extra]
    if host:
        args += ["--host", host]
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)


PATCHED = 'const t={label:"Trips",icon:L,testId:"trips-button",href:"/tripbox"};'
STOCK = 'const t={label:"Statistics",icon:C,testId:"statistics-button"};'


def test_the_link_is_found_in_a_script_two_imports_deep_and_not_in_a_stock_page(servers):
    app = Airtrail(PATCHED)
    r = served(servers(airtrail_handler(app)) + "/")
    assert (r.returncode, r.stdout) == (0, "dock link: found\n"), r.stderr
    assert app.asked[-1] == "/_app/immutable/chunks/dock.js"
    assert "/elsewhere.js" not in app.asked

    for dock in (STOCK, 'const t={label:"Trips",href:"/tripbox"};',  # no testId
                 'const t={label:"Trips",testId:"trips-button",href:"/tripboxes"};'):
        app = Airtrail(dock)
        r = served(servers(airtrail_handler(app)) + "/")
        assert (r.returncode, r.stdout) == (1, "dock link: not found\n"), dock
        assert "/_app/immutable/chunks/dock.js" in app.asked and "/elsewhere.js" not in app.asked

    # an address of its own, quoted another way
    app = Airtrail("const t={label:'Trips',testId:'trips-button',href:'https://trips.example.com'}")
    r = served(servers(airtrail_handler(app)), "https://trips.example.com")
    assert r.stdout == "dock link: found\n"
    # the host is named in every request: without --host the fake answers 421 to the page
    app = Airtrail(PATCHED)
    r = served(servers(airtrail_handler(app)), host=None)
    assert (r.returncode, r.stdout) == (2, "dock link: FAIL the page answered 421\n")
    r = served("http://127.0.0.1:9")
    assert r.returncode == 2 and r.stdout.startswith("dock link: FAIL no answer for /")


def test_the_crawl_stops_at_its_file_and_byte_limits(servers):
    def endless(path):
        """A script tree with no end: each script names two more."""
        n = int(path.rsplit("/", 1)[1].split(".")[0]) if path.endswith(".js") else 0
        return f'import"./{2 * n + 1}.js";import"./{2 * n + 2}.js";' + "x" * 1000

    app = Airtrail("", files=lambda path: (
        '<script type="module" src="/_app/n/0.js"></script>' if path == "/login"
        else endless(path)))
    r = served(servers(airtrail_handler(app)), "/tripbox", "--max-files", "40")
    assert (r.returncode, r.stdout) == (2, "dock link: FAIL more than 40 files\n")
    assert len(app.asked) == 40  # the redirect and the page count as two of the forty
    app.asked.clear()
    r = served(servers(airtrail_handler(app)), "/tripbox", "--max-bytes", "20000")
    assert (r.returncode, r.stdout) == (2, "dock link: FAIL more than 20000 bytes\n")
    # each script is over 1000 bytes: the page, its redirect and at most 21 scripts were asked
    assert 10 < len(app.asked) <= 23
    # the limits may only be lowered, and by default they are 800 files and 64 MiB
    for extra in (("--max-files", "801"), ("--max-bytes", str(64 * 1024 * 1024 + 1)),
                  ("--max-files", "0")):
        assert served("http://127.0.0.1:9", "/tripbox", *extra).returncode == 2, extra
    help_text = SERVED.read_text()
    assert "at most 800 files and 64 MiB" in help_text
    assert "MAX_FILES = 800" in help_text and "MAX_BYTES = 64 * 1024 * 1024" in help_text


# ---------------------------------------------------------------- the stack


def test_the_harness_names_no_service_as_airtrails_own():
    text = (HARNESS / "compose.yml").read_text()
    model = yaml.safe_load(text)
    assert model["name"] == "tripbox-onehost"
    services = model["services"]
    assert list(services) == ["flightlogdb", "flightlog", "web", "worker", "proxy", "probe",
                              "browser"]
    for name, svc in services.items():
        assert not name.startswith(("airtrail", "travel")), name
        assert "container_name" not in svc and "restart" not in svc, name
        assert "network_mode" not in svc and "networks" not in svc, name
    assert "networks" not in model and "volumes" not in model
    # nothing in the stack, nor in what it runs, reaches AirTrail by the recipe's own name
    for path in (HARNESS / "compose.yml", PROBE, SERVED, HARNESS / "click.py"):
        assert "airtrail:3000" not in path.read_text() and "//airtrail" not in path.read_text()
    flightlog = services["flightlog"]
    assert flightlog["image"] == "${ONEHOST_AIRTRAIL_IMAGE:?}"
    assert flightlog["environment"]["ORIGIN"] == f"http://{HOST}"
    assert flightlog["environment"]["DEMO_MODE"] == "${ONEHOST_DEMO_MODE:-screenshots}"
    assert flightlog["ports"] == ["127.0.0.1:${ONEHOST_PORT:?}:3000"]
    worker = services["worker"]["environment"]
    assert worker["TRIPBOX_AIRTRAIL_URL"] == "http://flightlog:3000"
    assert worker["TRIPBOX_AIRTRAIL_DRY_RUN"] == "false"
    # the key reaches the worker and the probe by name, never in the rendered file
    assert worker["TRIPBOX_AIRTRAIL_API_KEY"] is None
    assert services["probe"]["environment"]["ONEHOST_AIRTRAIL_KEY"] is None
    web = services["web"]["environment"]
    assert web["TRIPBOX_BASE_URL"] == f"http://{HOST}{PATH}"
    assert web["TRIPBOX_AIRTRAIL_PUBLIC_URL"] == f"http://{HOST}"
    assert not any(k.startswith("TRIPBOX_AIRTRAIL") and k != "TRIPBOX_AIRTRAIL_PUBLIC_URL"
                   for k in web)  # the page holds no flight-log credential
    for name in ("web", "worker"):
        assert "./config:/config:ro" in services[name]["volumes"], name
    assert services["browser"]["profiles"] == ["click"]
    assert [v["target"] for v in services["proxy"]["volumes"]] == ["/etc/caddy/Caddyfile"]
    # the limits: 7g in all with the browser, under the 8g a stack may take
    units = {"m": 1, "g": 1024}
    sizes = {name: int(svc["mem_limit"][:-1]) * units[svc["mem_limit"][-1]]
             for name, svc in services.items()}
    assert sizes == {"flightlogdb": 1024, "flightlog": 2048, "web": 512, "worker": 1024,
                     "proxy": 256, "probe": 256, "browser": 2048}
    assert sum(sizes.values()) == 7 * 1024
