# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The proxy recipes in docs/proxy/ and the probe that walks the page through each
(tools/verify/proxy/probe.py): the probe against a fake page on loopback that answers as the page
behind a proxy does, and can be told to get one step wrong; the recipes' text."""

import ipaddress
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PROBE = HERE / "proxy" / "probe.py"
RECIPES = ROOT / "docs" / "proxy"
GUIDE = ROOT / "docs" / "reverse-proxy.md"
PASSWORD = "Sample-family-" + secrets.token_hex(8)
MARK = "SAMPLE-BODY-" + secrets.token_hex(8)
SAMPLE = f"From: Sample Airways <tickets@airline.example>\nSubject: Your trip\n\n{MARK}\n"
HOSTNAME = "https://trips.example.com"
SUBPATH = "https://example.com/tripbox"
WALK = ("health", "sign-in", "first-run", "paste", "filed", "trip", "share", "feed", "upload",
        "offline", "sign-out")
STEPS = (*WALK, "slash", "login-page", "static", "paths", "outside")
AT_ROOT = tuple(s for s in STEPS if s not in ("slash", "outside"))
POLICY = "default-src 'self'; script-src 'self'; style-src 'self'"
SHARED_POLICY = "default-src 'self'; script-src 'none'; style-src 'self'"
LARGE = 2 * 1024 * 1024
CASES = {
    "caddy-hostname.Caddyfile": HOSTNAME,
    "caddy-subpath.Caddyfile": SUBPATH,
    "nginx-hostname.conf": HOSTNAME,
    "nginx-subpath.conf": SUBPATH,
    "traefik-hostname.yml": HOSTNAME,
    "traefik-subpath.yml": SUBPATH,
}
# AirTrail and tripbox at one hostname: tripbox under the path, AirTrail at the root
ONEHOST = {"caddy-airtrail.Caddyfile": "https://flights.example.com/tripbox"}


class Page:
    """The page as a proxy in front of it hands it on: its address, what it holds, what it was
    sent, and the one step it is told to get wrong."""

    def __init__(self, base: str, broken: str | None):
        parts = urlsplit(base)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.host = parts.netloc
        self.path = parts.path
        self.broken = broken
        self.sessions: set[str] = set()
        self.uploads: dict[str, float] = {}
        self.large: list[int] = []
        self.trips = 0
        self.links: dict[str, list[str]] = {}
        self.feed = secrets.token_urlsafe(16)
        self.wrong_host = 0
        self.posts_without_origin = 0


def handler(page: Page):
    p = page.path
    cookie_path = "/elsewhere" if page.broken == "paths" else (p or "/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # quiet
            pass

        def send(self, code, body="", headers=(), own=True, policy=POLICY):
            data = body.encode()
            self.send_response(code)
            if own:
                self.send_header("Content-Security-Policy", policy)
                self.send_header("X-Robots-Tag", "noindex, nofollow")
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

        def route(self):
            """The path under the base, or None once answered (outside it, the bare base, a
            wrong Host)."""
            if self.headers.get("Host") != page.host:
                page.wrong_host += 1
                self.send(421, own=False)
                return None
            path = self.path
            if p and path.split("?", 1)[0] == p:
                where = p + "/"
                if page.broken == "slash":
                    where = page.origin + where
                self.send(307, headers=[("Location", where)])
                return None
            if p and not path.startswith(p + "/"):
                self.send(404, "not here", own=page.broken == "outside")
                return None
            return path[len(p):]

        def do_GET(self):
            path = self.route()
            if path is None:
                return
            if path == "/healthz":
                return self.send(200, "no" if page.broken == "health" else "ok")
            if path.startswith("/login"):
                return self.send(
                    200, f'<link rel="stylesheet" href="{p}/static/app.css?v=1">'
                    "<form method=post></form>",
                    own=page.broken != "login-page",
                )  # fmt: skip
            if path.startswith("/static/app.css"):
                return self.send(404 if page.broken == "static" else 200, "body{}")
            if path == "/sw.js":
                cache = [] if page.broken == "offline" else [("Cache-Control", "no-cache")]
                return self.send(
                    200, "self.addEventListener('fetch', () => {});",
                    headers=[("Content-Type", "text/javascript"), *cache],
                )  # fmt: skip
            if path == "/manifest.webmanifest":
                body = {"name": "tripbox", "start_url": p + "/", "scope": p + "/"}
                return self.send(200, json.dumps(body),
                                 headers=[("Content-Type", "application/manifest+json")])
            if path.startswith("/s/"):
                if path[3:] not in page.links:
                    return self.send(404, policy=SHARED_POLICY)
                shared = POLICY if page.broken == "share" else SHARED_POLICY
                return self.send(200, "<h1>A trip</h1>", policy=shared)
            if path.startswith("/feed/"):
                if path != f"/feed/{page.feed}.ics":
                    return self.send(404)
                kind = "text/html" if page.broken == "feed" else "text/calendar; charset=utf-8"
                return self.send(200, "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                                 headers=[("Content-Type", kind)])
            if not self.session():
                return self.send(303, headers=[("Location", f"{p}/login?next=%2F")])
            if path == "/":
                empty = page.trips == 0 or page.broken == "trip"
                if page.broken == "first-run":
                    return self.send(200, "<p>Your trips</p>")
                body = "<p>No trips yet</p>" if empty else f"<a href='{p}/trips/1'>Trip</a>"
                return self.send(200, f'<html data-base="{p}" data-sw="{p}/sw.js">{body}')
            if path.startswith("/uploads/"):
                token = path.removeprefix("/uploads/")
                if token not in page.uploads:
                    return self.send(404, "no such upload")
                if page.broken == "filed" or time.monotonic() - page.uploads[token] < 1.0:
                    return self.send(200, "<p>Received.</p>")
                page.trips = 1
                return self.send(200, f"<p>Filed</p><a href='{p}/trips/1#item-1'>A flight</a>")
            if path == "/trips/1" and page.trips and page.broken != "trip":
                return self.send(200, "<h1>Trip</h1>")
            if path == "/trips/1/share":
                rows = "".join(f'<button data-copy="{page.origin}{p}/s/{t}">Copy link</button>'
                               for t in page.links)
                return self.send(
                    200, '<form id="share-form"><input type="checkbox" name="conf" checked>'
                    f'</form><form method="post" action="{p}/trips/1/links">'
                    '<input type="checkbox" name="names" checked>'
                    '<input type="checkbox" name="codes"></form>' + rows,
                )  # fmt: skip
            if path == "/settings":
                return self.send(
                    200, f'<button data-copy="{page.origin}{p}/feed/{page.feed}.ics">Copy</button>')
            return self.send(404, f"not found: {MARK}")

        def do_POST(self):
            path = self.route()
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if path is None:
                return
            if self.headers.get("Origin") != page.origin:
                page.posts_without_origin += 1
                return self.send(403, "cross-site")
            form = parse_qs(body.decode("utf-8", "replace"))
            if path == "/login":
                typed = form.get("password", [""])[0]
                right = (typed == PASSWORD) != (page.broken == "sign-in")
                if not right:
                    return self.send(401, f"wrong password {typed}")
                token = secrets.token_hex(16)
                page.sessions.add(token)
                return self.send(303, headers=[
                    ("Location", f"{p}/"),
                    ("Set-Cookie", f"tb_session={token}; HttpOnly; Path={cookie_path}"),
                    ("Set-Cookie", f"tb_device=d1; HttpOnly; Path={cookie_path}; Max-Age=60"),
                ])  # fmt: skip
            if path == "/logout":
                token = self.session()
                if token and page.broken != "sign-out":
                    page.sessions.discard(token)
                return self.send(303, headers=[
                    ("Location", f"{p}/login?f=out"),
                    ("Set-Cookie", f"tb_session=; HttpOnly; Path={cookie_path}; Max-Age=0"),
                ])  # fmt: skip
            if not self.session():
                return self.send(303, headers=[("Location", f"{p}/login")])
            if path == "/add/paste":
                if form.get("text", [""])[0] != SAMPLE or page.broken == "paste":
                    return self.send(200, "<textarea></textarea>")
                token = secrets.token_hex(8)
                page.uploads[token] = time.monotonic()
                return self.send(303, headers=[("Location", f"{p}/uploads/{token}")])
            if path == "/add/upload":
                if page.broken == "upload":  # as a proxy's body limit answers
                    return self.send(413, "Request Entity Too Large", own=False)
                if b'name="file"' not in body[:400]:
                    return self.send(400, "no file")
                page.large.append(length)
                token = secrets.token_hex(8)
                page.uploads[token] = float("inf")  # received, never filed here
                return self.send(303, headers=[("Location", f"{p}/uploads/{token}")])
            if path == "/trips/1/links":
                page.links[secrets.token_urlsafe(8)] = sorted(form)
                return self.send(303, headers=[("Location", f"{p}/trips/1/share#links")])
            return self.send(404, "not found")

    return Handler


@pytest.fixture
def serve():
    servers = []

    def start(base, broken=None):
        page = Page(base, broken)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(page))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return page, f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def probe(tmp_path, proxy, base, *extra):
    sample = tmp_path / "email.eml"
    sample.write_text(SAMPLE)
    return subprocess.run(
        [sys.executable, "-I", str(PROBE), "--proxy", proxy, "--base-url", base,
         "--sample", str(sample), *extra],
        env={"PROXY_PASSWORD": PASSWORD, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip


def test_the_probe_passes_a_page_under_a_path_and_at_the_root(serve, tmp_path):
    for base, steps in ((SUBPATH, STEPS), (HOSTNAME, AT_ROOT)):
        page, proxy = serve(base)
        r = probe(tmp_path, proxy, base, "--timeout", "20", "--wait", "5")
        assert r.returncode == 0, (base, r.stdout, r.stderr)
        assert r.stdout.splitlines() == [f"proxy probe: {s} ok" for s in steps] + [
            "proxy probe: ok"]
        # every request was addressed to the page's host, every post came from its origin
        assert page.wrong_host == 0 and page.posts_without_origin == 0
        assert page.sessions == set()  # signed out on the server
        assert list(page.links.values()) == [["names"]]  # the form's ticked boxes alone
        assert page.large and min(page.large) > LARGE  # the 2 MiB file and its form
        said = r.stdout + r.stderr
        assert PASSWORD not in said and MARK not in said and "Sample Airways" not in said


@pytest.mark.parametrize("step", STEPS)
def test_each_failing_probe_step_is_named(serve, tmp_path, step):
    page, proxy = serve(SUBPATH, step)
    r = probe(tmp_path, proxy, SUBPATH, "--timeout", "3", "--wait", "2")
    assert r.returncode == 1, (r.stdout, r.stderr)
    done = STEPS[: STEPS.index(step)]
    assert r.stdout.splitlines() == [f"proxy probe: {s} ok" for s in done] + [
        f"proxy probe: FAIL {step}"]
    said = r.stdout + r.stderr
    assert PASSWORD not in said and MARK not in said
    if step == "health":  # and a proxy that is not there at all fails it too
        r = probe(tmp_path, "http://127.0.0.1:9", SUBPATH, "--wait", "1")
        assert (r.returncode, r.stdout) == (1, "proxy probe: FAIL health\n")


def test_the_probe_refuses_a_proxy_it_cannot_reach_in_plain_http(tmp_path):
    for proxy in ("https://proxy:443", "proxy:80", "http://proxy:80/tripbox"):
        r = probe(tmp_path, proxy, SUBPATH)
        assert r.returncode == 2, proxy


def shipped_files() -> list[Path]:
    return [*sorted(RECIPES.iterdir()), GUIDE, *sorted((HERE / "proxy").iterdir())]


DOCUMENTATION = [ipaddress.ip_network(n) for n in
                 ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]


def documentation(found: str) -> bool:
    """Loopback, every interface (a listening address) or a documentation range."""
    net = ipaddress.ip_network(found, strict=False)
    if net.network_address.is_loopback or (net.prefixlen == net.max_prefixlen
                                           and net.network_address.is_unspecified):
        return True
    return any(net.version == d.version and net.subnet_of(d) for d in DOCUMENTATION)


def test_no_recipe_names_a_private_address():
    for path in shipped_files():
        if path.suffix == ".pyc" or path.is_dir():
            continue
        text = path.read_text()
        for found in re.findall(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\w.])", text):
            assert documentation(found), (path.name, found)
        for found in re.findall(r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]*(?:/\d+)?", text, re.I):
            try:
                ipaddress.ip_network(found, strict=False)
            except ValueError:  # a time or a pair of words, not an address
                continue
            assert documentation(found), (path.name, found)
        for host in re.findall(r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|cloud))\b", text):
            assert re.fullmatch(r"(?:[a-z0-9-]+\.)*example\.(?:com|net|org)", host), (
                path.name, host)
        assert "/home/" not in text and "BEGIN CERTIFICATE" not in text, path.name
        assert "PRIVATE KEY" not in text, path.name


def test_every_recipe_names_its_base_url_and_proxies_to_the_page():
    assert sorted(p.name for p in RECIPES.iterdir()) == sorted({**CASES, **ONEHOST})
    guide = GUIDE.read_text()
    for name, base in CASES.items():
        text = (RECIPES / name).read_text()
        first = text.splitlines()[0]
        assert first == f"# TRIPBOX_BASE_URL={base}", name
        assert f"docs/proxy/{name}" in guide, name
        host, path = urlsplit(base).netloc, urlsplit(base).path
        assert host in text, name
        lines = [ln.strip() for ln in text.splitlines() if not ln.strip().startswith("#")]
        if name.startswith("caddy"):
            assert "reverse_proxy web:8110" in lines, name
            assert f"{host} {{" in lines, name  # the site address alone: Caddy's own TLS
            # the prefix stays: no directive that takes it off
            assert not re.search(r"handle_path|strip_prefix|uri ", "\n".join(lines)), name
            if path:
                assert f"@tripbox path {path} {path}/*" in lines, name
                assert "handle @tripbox {" in lines, name
                # the rest of the site in a handle of its own, after tripbox's: the site's own
                # try_files, rewrite or redir run there and never on the page's paths
                raw = [ln.strip() for ln in text.splitlines()]
                at = raw.index("handle {")
                assert at > raw.index("handle @tripbox {"), name
                assert raw[at + 1 : at + 3] == [
                    "# the site's own directives go here, as before", "}"], name
        elif name.startswith("traefik"):
            assert '- url: "http://web:8110"' in lines, name
            assert not re.search(r"(?i)stripprefix", "\n".join(lines)), name
            rule = next(ln for ln in lines if ln.startswith("rule:"))
            assert f"Host(`{host}`)" in rule, name
            if path:
                assert f"Path(`{path}`)" in rule and f"PathPrefix(`{path}/`)" in rule, name
        else:
            assert "proxy_pass http://web:8110;" in lines, name


def test_the_one_hostname_recipe_sends_the_path_to_tripbox_and_the_rest_to_airtrail():
    ((name, base),) = ONEHOST.items()
    text = (RECIPES / name).read_text()
    assert text.splitlines()[0] == f"# TRIPBOX_BASE_URL={base}"
    host, path = urlsplit(base).netloc, urlsplit(base).path
    code = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    # one site, the hostname alone (Caddy's own TLS); tripbox's path first, AirTrail in the last
    # handle, where nothing of tripbox's path reaches it; the path handed on as it came
    assert code == [
        f"{host} {{",
        f"@tripbox path {path} {path}/*",
        "handle @tripbox {",
        "reverse_proxy web:8110",
        "}",
        "handle {",
        "reverse_proxy airtrail:3000",
        "}",
        "}",
    ]
    said = " ".join(ln.lstrip("# ") for ln in text.splitlines() if ln.startswith("#"))
    for fact in ("AirTrail must be at the root", "ORIGIN setting is this hostname,"
                 " https://flights.example.com", "TRIPBOX_AIRTRAIL_URL=http://airtrail:3000",
                 "not through this proxy", "TRIPBOX_AIRTRAIL_PUBLIC_URL, is the hostname",
                 "built with /tripbox"):
        assert fact in said, fact
    guide = " ".join(GUIDE.read_text().split())
    section = guide[guide.index("## AirTrail and tripbox at one hostname"):]
    assert f"docs/proxy/{name}" in section and f"`{base}`" in section
    # nginx: AirTrail's location after tripbox's two; Cloudflare: the path rule first
    assert section.index("location = /tripbox") < section.index("location ^~ /tripbox/") < (
        section.index("location / {"))
    assert section.index("`^/tripbox(/.*)?$`") < section.index("`http://airtrail:3000`")
    assert "**nginx** (not run)" in section and "**Cloudflare Tunnel** (not run)" in section
    for path_through in ("`/tripbox/s/`", "`/tripbox/feed/`", "`/tripbox/static/`"):
        assert path_through in section, path_through


def nginx_blocks(text: str) -> list:
    """nginx's configuration as a tree: each directive a (words, children or None)."""
    tokens = re.findall(r"[{};]|[^\s{};]+", re.sub(r"#[^\n]*", "", text))
    stack: list[list] = [[]]
    words: list[str] = []
    for token in tokens:
        if token == ";":
            stack[-1].append((words, None))
            words = []
        elif token == "{":
            block: list = []
            stack[-1].append((words, block))
            stack.append(block)
            words = []
        elif token == "}":
            assert not words
            stack.pop()
        else:
            words.append(token)
    assert len(stack) == 1 and not words
    return stack[0]


def body_limit(value: str) -> int:
    number, unit = re.fullmatch(r"(\d+)([kKmMgG]?)", value).groups()
    return int(number) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit.lower()]


def test_the_nginx_recipes_raise_the_body_limit_and_leave_the_path_alone():
    for name in ("nginx-hostname.conf", "nginx-subpath.conf"):
        tree = nginx_blocks((RECIPES / name).read_text())
        (server,) = [children for words, children in tree if words == ["server"]]
        server_limit = [w[1] for w, c in server if w[0] == "client_max_body_size"]
        locations = {" ".join(w[1:]): c for w, c in server if w[0] == "location"}
        path = urlsplit(CASES[name]).path
        # the bare base has its own exact location, so nginx never answers it with a redirect
        # under a path, `^~` keeps a regex location of the site's own (such as one for every .css
        # or .js file) from answering the page's files instead of the page
        assert set(locations) == ({"/"} if not path else {f"= {path}", f"^~ {path}/"}), name
        for where, children in locations.items():
            said = {w[0]: w[1:] for w, c in children}
            # no path after the address: the request's path is handed on as it came
            assert said["proxy_pass"] == ["http://web:8110"], (name, where)
            assert said["proxy_set_header"] == ["Host", "$host"], (name, where)
            limit = said.get("client_max_body_size", server_limit[-1:])
            # the page takes 10 MB uploads; nginx refuses a body over 1 MB by default
            assert limit and body_limit(limit[0]) >= 11 * 1024**2, (name, where)


def image_healthcheck() -> list[str]:
    """The command of the app image's own HEALTHCHECK, which every container of it runs unless
    its service sets a check of its own."""
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    (line,) = re.findall(r"^HEALTHCHECK\b.*\bCMD (\[.*\])", text, re.M)
    return json.loads(line)


def test_every_service_of_the_stack_passes_the_health_check_it_runs():
    """`up --wait` fails on any unhealthy container: a service of the app image that runs no
    worker would inherit the image's worker check and never pass it, so each one runs a check
    that what it serves answers."""
    import yaml

    model = yaml.safe_load((HERE / "proxy" / "compose.yml").read_text())
    assert image_healthcheck()[-1] == "worker"  # the image's own check asks for a live worker
    for name, svc in model["services"].items():
        if svc["image"] != "${PROXY_TRIPBOX_IMAGE:?}":
            continue  # the proxy images declare no check of their own
        check = svc.get("healthcheck") or {}
        assert check.get("test") and not check.get("disable"), name
        test = check["test"]
        role = svc["command"][3:4] if svc["command"][:3] == ["python", "-m", "tripbox"] else []
        if role == ["web"]:
            assert test == ["CMD", "python", "/app/scripts/healthcheck.py", "web"], name
        elif role == ["work"]:
            assert test == ["CMD", "python", "/app/scripts/healthcheck.py", "worker"], name
            # the check reads the ledger where the worker writes it
            data = svc["environment"]["TRIPBOX_DATA_DIR"]
            assert any(v.split(":")[1] == data for v in svc["volumes"] if isinstance(v, str)), name
        else:  # it serves nothing: the check asks for what it is given, never a page or a worker
            assert "healthcheck.py" not in " ".join(test), name
            mounted = {v["target"] for v in svc.get("volumes", []) if isinstance(v, dict)}
            assert set(test) & mounted, name


USER_HEADER_RULE = (
    "The proxy must set this header itself on every request it passes to tripbox, replacing any"
    " header of that name a browser sent \u2014 on the paths it lets through without a sign-in as"
    " well. A proxy that only adds the header when its sign-in answers lets a browser choose who"
    " it is."
)


def test_the_guide_says_a_login_proxy_sets_the_user_header_itself():
    guide = " ".join(GUIDE.read_text().split())
    section = guide[guide.index("**`TRIPBOX_AUTH=proxy`.**"):]
    trusted = ("The page believes the user header only from the addresses in"
               " `TRIPBOX_TRUSTED_PROXIES`.")
    assert section.index(trusted) < section.index(USER_HEADER_RULE)
    assert section.index(USER_HEADER_RULE) == section.index(trusted) + len(trusted) + 1
    assert "`TRIPBOX_PROXY_USER_HEADER`" in section and "`docs/configuration.md`" in section
