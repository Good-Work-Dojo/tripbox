# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Saved copies for offline use, at each base: the web app manifest and a service worker scoped to
the base. The worker keeps only what the server marks (`X-Tripbox-Offline: 1`: a signed-in 200 of
the list or a trip), a new release names new stores, and offline switched off serves a worker that
removes its stores and unregisters itself. The login page's one script empties the store again."""

import json
import re
import shutil
from html import unescape

import pytest
from helpers.web import Page, client_for, raw_get, starlette_of
from jwt import PyJWKClient
from test_web_auth import ACCESS, Keys, behind_proxy
from test_web_auth import PUBLIC as ADDRESS
from test_web_login import as_cookie, fresh, get, session_of, sign_in
from test_web_login import page as password_page
from test_web_views import seed_trip

import tripbox
from tripbox.config import KEY_BY_NAME, Settings
from tripbox.trips import ical, shares
from tripbox.web import app as webapp

MARK = "x-tripbox-offline"
MODES = ("password", "cloudflare", "proxy", "none")
STATIC = webapp.HERE / "static"
KEPT_HERE = (
    "Trips you open are kept on this device, so they open without a connection. Browsers allow"
    " this only over https or on localhost."
)
# What else Saved copies says, by login: one with a sign-out, and none
SAVED_COPIES = {
    "signed": KEPT_HERE + " Signing out on this device removes them. Another device keeps its"
    " copies until it next connects, and never more than 30 days.",
    "none": KEPT_HERE + " Copies are kept for up to 30 days, or until you remove them here.",
    "proxy": KEPT_HERE + " Signing out at your login proxy does not remove them: remove them here."
    " A device keeps its copies until it next connects signed out, and never more than 30 days.",
}
REMOVE = "Remove saved copies from this device"
SWITCHED_OFF = "Saved copies are switched off (TRIPBOX_OFFLINE)."


@pytest.fixture
def access(monkeypatch):
    """The Access login's signing keys, served as its key set."""
    k = Keys()
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: k.jwks)
    return k


def signed_in(mode, tmp_path, monkeypatch, base, access, env=None):
    """The page behind the login `mode` under `base` (its settings changed by `env`), its context,
    and the headers of a request that login lets through."""
    tmp = tmp_path / mode
    tmp.mkdir(parents=True, exist_ok=True)
    if mode == "password":
        c, ctx = password_page(tmp, monkeypatch, base, env=env)
        return c, ctx, as_cookie("tripbox_session", session_of(sign_in(c)))
    if mode == "cloudflare":
        page_env = {**ACCESS, "TRIPBOX_BASE_URL": ADDRESS + base, **(env or {})}
        c, ctx = client_for(tmp, monkeypatch, base=base, auth="cloudflare", env=page_env)
        return c, ctx, {"Cf-Access-Jwt-Assertion": access.token()}
    if mode == "proxy":
        c, ctx = behind_proxy(tmp, monkeypatch, base, **(env or {}))
        return c, ctx, {"Remote-User": "alex"}
    c, ctx = client_for(tmp, monkeypatch, base=base, env=env)
    return c, ctx, {}


def html_tag(html: str) -> str:
    (tag,) = re.findall(r"<html\b[^>]*>", html)
    return tag


def scripts(html: str) -> list[str]:
    return re.findall(r"<script\b[^>]*>", html)


def marked(r) -> bool:
    return r.headers.get(MARK) == "1"


# ----------------------------------------------------------------------------- what is kept
def test_only_a_signed_in_200_of_the_list_or_a_trip_is_marked_for_keeping(
    tmp_path, monkeypatch, base, access
):
    c, ctx, owner = signed_in("password", tmp_path, monkeypatch, base, access)
    trip = seed_trip(ctx)
    token = shares.create(ctx.ledger, trip.id, ["names"]).token
    feed = ical.feed_token(ctx.ledger)
    for path in ("/", f"/trips/{trip.id}"):
        r = get(c, path, owner)
        assert r.status_code == 200 and marked(r), path
        assert r.headers[MARK] == "1" and len(r.headers.get_list(MARK)) == 1, path
    never = {
        "/settings": 200,
        f"/trips/{trip.id}/share": 200,  # both print live share tokens
        f"/trips/{trip.id}/text": 200,
        f"/trips/{trip.id}/trip.ics": 200,
        "/deleted": 200,
        "/add": 200,
        "/add/paste": 200,
        "/trips/999999": 404,
        f"/s/{token}": 200,
        f"/s/{token}x": 404,
        f"/feed/{feed}.ics": 200,
        "/login": 303,  # signed in: sent on
        "/sw.js": 200,
        "/manifest.webmanifest": 200,
    }
    for path, status in never.items():
        r = get(c, path, owner, follow_redirects=False)
        assert r.status_code == status and not marked(r), path
    # nobody signed in: the login page, and the redirect to it, carry no mark
    assert get(c, "/login").status_code == 200 and not marked(get(c, "/login"))
    page_load = {"Accept": "text/html"}
    for path in ("/", f"/trips/{trip.id}"):
        r = get(c, path, headers=page_load, follow_redirects=False)
        assert r.status_code == 303 and not marked(r), path
        assert not marked(get(c, path)), path  # 401 to a fetch
    # with no login (on this computer): the list and a trip are marked too
    local, lctx, _ = signed_in("none", tmp_path, monkeypatch, base, access)
    local_trip = seed_trip(lctx)
    assert marked(local.get("/")) and marked(local.get(f"/trips/{local_trip.id}"))
    # offline switched off, or a login whose sign-out the page cannot see: never
    off, octx, cookie = signed_in(
        "password", tmp_path / "off", monkeypatch, base, access, env={"TRIPBOX_OFFLINE": "false"}
    )
    off_trip = seed_trip(octx)
    for path in ("/", f"/trips/{off_trip.id}"):
        r = get(off, path, cookie)
        assert r.status_code == 200 and not marked(r), path
    for mode in ("proxy", "cloudflare"):
        p, pctx, headers = signed_in(mode, tmp_path, monkeypatch, base, access)
        p_trip = seed_trip(pctx)
        for path in ("/", f"/trips/{p_trip.id}"):
            r = p.get(path, headers=headers)
            assert r.status_code == 200 and not marked(r), (mode, path)


def test_the_manifest_and_worker_are_scoped_to_the_base(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    version = starlette_of(c.app).state.version
    # the manifest: public, its addresses under the base
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == "application/manifest+json"
    assert r.json() == {
        "name": "tripbox",
        "short_name": "Trips",
        "start_url": c.url("/"),
        "scope": c.url("/"),
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [
            {
                "src": c.url("/static/apple-touch-icon.png"),
                "sizes": "180x180",
                "type": "image/png",
            },
            {"src": c.url("/static/favicon.svg"), "sizes": "any", "type": "image/svg+xml"},
        ],
    }
    for icon in r.json()["icons"]:
        assert c.get(c.local(icon["src"])).status_code == 200, icon
    # the worker's script: public, never kept stale, never allowed a wider scope
    w = c.get("/sw.js")
    assert w.status_code == 200
    assert w.headers["content-type"].split(";")[0] == "text/javascript"
    assert w.headers["cache-control"] == "no-cache"
    script = w.text
    assert f"const BASE = {json.dumps(base)};" in script
    assert f"const VERSION = {json.dumps(version)};" in script
    assert 'const PREFIX = "tripbox" + BASE + ":";' in script
    assert 'PREFIX + "static:" + VERSION' in script and 'PREFIX + "pages:" + VERSION' in script
    assert "X-Tripbox-Offline" in script and "skipWaiting" in script and "clients.claim" in script
    # the un-based address (a stripping proxy's) answers the same script, naming the based scope
    stripped = Page(c.app, base, stripped=True)
    for path in ("/sw.js", "/manifest.webmanifest"):
        assert stripped.get(path).content == c.get(path).content, path
    status, headers, body = raw_get(c.app, b"/sw.js")
    assert status == 200 and body.decode() == script
    # no response carries a wider worker scope
    for answer in (w, r, stripped.get("/sw.js"), c.get("/login"), sign_in(c)):
        assert "service-worker-allowed" not in {k.lower() for k in answer.headers}
    assert "service-worker-allowed" not in headers
    # every page names the base and registers the worker under it, and links the manifest
    owner = as_cookie("tripbox_session", session_of(sign_in(c)))
    trip = seed_trip(ctx)
    for path in ("/", f"/trips/{trip.id}", "/settings", "/login"):
        html = get(c, path, owner if path != "/login" else None).text
        assert html_tag(html) == (
            f'<html lang="en" data-base="{c.url("")}" data-sw="{c.url("/sw.js")}">'
        ), path
        link = (
            f'<link rel="manifest" href="{c.url("/manifest.webmanifest")}"'
            ' crossorigin="use-credentials">'
        )
        assert link in html, path
    # the script that registers it names the scope: the base, never the origin's root
    js = (STATIC / "app.js").read_text()
    assert 'register(sw, { scope: base + "/" })' in js


def test_the_page_in_view_is_asked_for_again_once_a_worker_controls_it():
    """The load that registers the worker comes before it controls the page, and an address with
    a query (a redirect's ?f=) is never kept: so the list or a trip in view is asked for again,
    once when a worker takes control and once when ?f= has left the address. The script's one
    such request is a GET of the path alone; a saved copy asks nothing."""
    js = (STATIC / "app.js").read_text()
    keep = re.search(r"const keepHere = \(\) => \{\n(.*?)\n  \};", js, re.S)
    assert keep, "keepHere"
    body = keep.group(1).splitlines()
    assert body[0].strip() == (
        "if (savedAt || !sw || !navigator.serviceWorker || !navigator.serviceWorker.controller)"
        " return;"
    )
    assert body[-1].strip() == 'fetch(path, { credentials: "same-origin" }).catch(() => {});'
    assert "const path = location.pathname;" in keep.group(1)  # the path alone, never the query
    assert "const KEPT_HERE = /^\\/(trips\\/[0-9]+)?$/;" in js  # the list or a trip, nothing else
    assert 'addEventListener("controllerchange", keepHere, { once: true })' in js
    assert "if (stripped) keepHere();" in js
    assert js.index("const stripped =") < js.index("history.replaceState")
    # no request of the script's is a change: it posts nothing, and asks nothing else of the page
    assert "POST" not in js and "method" not in js
    assert js.count("fetch(") == 2  # this one, and the share page's preview text
    # the worker takes such a request as it takes a page load: the list or a trip, any mode
    worker = (webapp.HERE / "templates" / "sw.js").read_text()
    assert '} else if (path === "/" || TRIP.test(path)) {\n    event.respondWith(page(' in worker


def worker_function(script: str, name: str) -> str:
    """The body of the worker script's function `name`, as served."""
    found = re.search(r"\nasync function " + name + r"\(.*?\) \{\n(.*?)\n\}\n", script, re.S)
    assert found, name
    return found.group(1)


def test_the_network_answers_at_once_and_a_store_that_fails_changes_no_answer(
    tmp_path, monkeypatch, base
):
    """The network's answer to the list or a trip is released as soon as it comes; what it does
    to the store (its whole body read, a write) runs beside it, its failure no one's. Only a page
    with a saved copy falls back to it after WAIT_MS; one with none waits for the network and
    says it has no copy only when the network fails. A static file or any other page is the
    network's answer when a store cannot be read or written."""
    c, _ = password_page(tmp_path, monkeypatch, base)
    script = c.get("/sw.js").text
    assert "await settle(" not in script  # never before the answer is released
    page = worker_function(script, "page")
    assert page.splitlines() == [
        "  const key = url.origin + url.pathname;",
        "  const network = fetch(event.request);",
        "  event.waitUntil(",
        '    network.then((r) => settle(r.clone(), key, url.search !== "")).catch(() => null)',
        "  );",
        "  const answer = await within(network, WAIT_MS);",
        "  if (answer) return answer;",
        "  const copy = await savedCopy(key).catch(() => null);",
        "  return copy || network.catch(() => said(NO_COPY));",
    ]
    file = worker_function(script, "file")
    assert "await store.put(request, r.clone()).catch(() => null);" in file
    assert file.index("} catch (e) {") < file.index("const r = await fetch(request);")
    assert file.index("store.match(request)") < file.index("} catch (e) {")
    other = worker_function(script, "other")
    assert "await caches.delete(PAGES).catch(() => null);" in other
    assert other.index("return said(NO_COPY);") < other.index("caches.delete(PAGES)")
    assert "const WAIT_MS = 3000;" in script


def test_an_unmarked_page_empties_the_store_and_only_the_pages_own_changes_are_answered(
    tmp_path, monkeypatch, base
):
    """A 200 of the list or a trip the server did not mark (a login proxy's own page served at
    the trip's address) empties the pages store, as a 401, a 403 or a redirect does. A change
    (any method but GET) is answered by the worker only when it comes from a page under the base
    of the worker's own origin; any other is left to the browser, never sent again by it."""
    c, _ = password_page(tmp_path, monkeypatch, base)
    script = c.get("/sw.js").text
    settle = worker_function(script, "settle")
    gone = "  if (r.status === 401 || r.status === 403 || redirected(r) || unmarked) {\n"
    assert gone + "    await caches.delete(PAGES);\n" in settle
    assert '  const unmarked = r.status === 200 && r.headers.get(KEEP) !== "1";' in settle
    assert 'r.headers.get(KEEP) === "1" && !hasQuery' in settle
    changes = re.search(r'  if \(asked\.method !== "GET"\) \{\n(.*?)\n  \}\n', script, re.S)
    assert changes
    lines = [line.strip() for line in changes.group(1).splitlines() if "//" not in line]
    assert lines == [
        'if (!asked.referrer.startsWith(self.location.origin + BASE + "/")) return;',
        "event.respondWith(fetch(asked).catch(() => said(NOT_SAVED)));",
        "return;",
    ]


def test_a_saved_copy_locks_what_needs_a_connection_and_keeps_the_copy_buttons(
    tmp_path, monkeypatch, base
):
    """On a saved copy every control is disabled but the copy buttons (copying a confirmation
    code changes nothing, and is what a copy is opened offline for); the ⋯ sheets and Add notes
    are closed and cannot open; the share link is marked disabled and its click cancelled; and
    each of them looks disabled, by the stylesheet. Settings' button says when it is done."""
    js = (STATIC / "app.js").read_text()
    copy = js[js.index("if (savedAt) {") : js.index("// go() once the pages store is empty")]
    assert 'const LOCKED = MENUS + ", details.inline";' in js
    assert '$$("input, select, textarea, button").forEach((el) => {' in copy
    assert 'if (!el.matches("[data-copy]")) el.disabled = true;' in copy
    assert "$$(LOCKED).forEach((d) => {\n      d.open = false;" in copy
    assert '$$("a[data-share]").forEach((a) => a.setAttribute("aria-disabled", "true"));' in copy
    assert "t.closest(`:is(${LOCKED}) > summary, a[data-share]`)" in copy
    css = (STATIC / "app.css").read_text()
    assert (
        "html[data-saved] :is(button, input, select, textarea):disabled,\n"
        'html[data-saved] [aria-disabled="true"] { opacity: .45; cursor: not-allowed; }'
    ) in css
    assert (
        'forget().then(() => toast("Saved copies removed from this device.", 4000));' in js
    )
    # the trip page's share link is the one marked, and its copy buttons are data-copy buttons
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = seed_trip(ctx)
    owner = as_cookie("tripbox_session", session_of(sign_in(c)))
    html = get(c, f"/trips/{trip.id}", owner).text
    (share,) = re.findall(r"<a\b[^>]*\bdata-share\b[^>]*>", html)
    assert f'href="{c.url(f"/trips/{trip.id}/share")}"' in share
    assert re.search(r'<details class="inline">', html)


def test_the_worker_keeps_what_every_saved_copy_needs_when_it_is_installed(
    tmp_path, monkeypatch, base
):
    """The page that registers the worker loads its stylesheet, script and font before the worker
    answers anything, so none of them passes through it; a page kept then (the list sign-in lands
    on) would open offline with no script, so no banner and nothing locked. The worker keeps them
    itself when it is installed: each address as the pages ask for it (the stylesheet and script
    with the release's version, each font as the stylesheet names it), one that fails failing no
    install."""
    c, _ = password_page(tmp_path, monkeypatch, base)
    version = starlette_of(c.app).state.version
    script = c.get("/sw.js").text
    (listed,) = re.findall(r"^const FILES = (\[.*\]);$", script, re.M)
    fonts = sorted(p.name for p in (STATIC / "fonts").glob("*.woff2"))
    assert len(fonts) == 4
    css = (STATIC / "app.css").read_text()
    assert sorted(re.findall(r'url\("fonts/([^"]+)"\)', css)) == fonts
    assert json.loads(listed) == [
        f"/static/app.css?v={version}",
        f"/static/app.js?v={version}",
        *(f"/static/fonts/{name}" for name in fonts),
    ]
    # each page asks for them at these addresses, under the base
    assert f'href="{c.url("/static/app.css")}?v={version}"' in c.get("/login").text
    base_html = (webapp.HERE / "templates" / "base.html").read_text()
    assert '<script src="{{ base }}/static/app.js?v={{ asset_v }}" defer>' in base_html
    install = re.search(
        r'self\.addEventListener\("install", \(event\) => \{\n(.*?)\n\}\);', script, re.S
    )
    assert install
    assert install.group(1).splitlines() == [
        "  self.skipWaiting();",
        "  const kept = (store, f) => store.add(BASE + f).catch(() => null);",
        "  event.waitUntil(",
        "    caches",
        "      .open(STATIC)",
        "      .then((store) => Promise.all(FILES.map((f) => kept(store, f))))",
        "      .catch(() => null)",
        "  );",
    ]


def test_a_new_release_names_new_stores(tmp_path, monkeypatch, base):
    static = tmp_path / "static"
    shutil.copytree(STATIC, static)
    before = webapp._asset_version(static, "1.0.0")
    assert re.fullmatch(r"[0-9a-f]{12}", before)
    assert webapp._asset_version(static, "1.0.0") == before
    assert webapp._asset_version(static, "1.0.1") != before  # a new version
    css = static / "app.css"
    css.write_bytes(css.read_bytes() + b"\n")
    assert webapp._asset_version(static, "1.0.0") != before  # a changed static byte
    # the app's version is the static links' and the worker's, so its stores are new
    (tmp_path / "a").mkdir()
    c, _ = password_page(tmp_path / "a", monkeypatch, base)
    version = starlette_of(c.app).state.version
    assert version == webapp._asset_version(STATIC, tripbox.__version__)
    html = c.get("/login").text
    assert f'href="{c.url("/static/app.css")}?v={version}"' in html
    assert f'const VERSION = "{version}";' in c.get("/sw.js").text
    monkeypatch.setattr(tripbox, "__version__", tripbox.__version__ + ".next")
    (tmp_path / "b").mkdir()
    n, _ = password_page(tmp_path / "b", monkeypatch, base)
    newer = starlette_of(n.app).state.version
    assert newer != version and f'const VERSION = "{newer}";' in n.get("/sw.js").text
    assert f'const VERSION = "{version}";' not in n.get("/sw.js").text


def test_offline_off_serves_a_script_that_unregisters_itself_and_no_page_registers_one(
    tmp_path, monkeypatch, base, access
):
    for mode, env in (
        ("password", {"TRIPBOX_OFFLINE": "false"}),
        ("none", {"TRIPBOX_OFFLINE": "off"}),
        ("proxy", None),
        ("cloudflare", None),
    ):
        c, ctx, headers = signed_in(mode, tmp_path, monkeypatch, base, access, env=env)
        assert not ctx.settings.offline, mode
        trip = seed_trip(ctx)
        script = c.get("/sw.js").text
        assert c.get("/sw.js").headers["cache-control"] == "no-cache"
        assert f"const BASE = {json.dumps(base)};" in script
        assert 'const PREFIX = "tripbox" + BASE + ":";' in script
        assert "name.startsWith(PREFIX)" in script and "caches.delete(name)" in script
        assert "self.registration.unregister()" in script
        assert "fetch" not in script  # it answers no request: the network does
        for path in ("/", f"/trips/{trip.id}", "/settings"):
            html = c.get(path, headers=headers).text
            assert html_tag(html) == f'<html lang="en" data-base="{c.url("")}">', (mode, path)
            assert "data-sw" not in html, (mode, path)


def test_offline_defaults_follow_the_login_mode():
    key = KEY_BY_NAME["TRIPBOX_OFFLINE"]
    assert (key.feature, key.web, key.secret, key.field, key.default) == (
        "web",
        True,
        False,
        "offline",
        "",
    )
    expected = {"password": True, "none": True, "proxy": False, "cloudflare": False}
    for mode, on in expected.items():
        assert Settings.from_env({"TRIPBOX_AUTH": mode}).offline is on, mode
        assert Settings.from_env({"TRIPBOX_AUTH": mode, "TRIPBOX_OFFLINE": " "}).offline is on
        for word, said in (("true", True), ("YES", True), ("0", False), ("off", False)):
            s = Settings.from_env({"TRIPBOX_AUTH": mode, "TRIPBOX_OFFLINE": word})
            assert s.offline is said, (mode, word)
            assert not [p for p in s.problems if p.key == "TRIPBOX_OFFLINE"]
    # a value that is not a yes or a no is a problem of the page, never an exception
    s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_OFFLINE": "sometimes"})
    (problem,) = [p for p in s.problems if p.key == "TRIPBOX_OFFLINE"]
    assert (problem.feature, str(problem)) == (
        "web",
        "TRIPBOX_OFFLINE: 'sometimes' is not true or false — use true/false, 1/0, yes/no or"
        " on/off",
    )
    assert problem in s.stopping("web")


# ----------------------------------------------------------------------------- what empties it
def test_the_login_page_loads_only_the_script_that_clears(tmp_path, monkeypatch, base):
    c, _ = password_page(tmp_path, monkeypatch, base)
    version = starlette_of(c.app).state.version
    clear = f'<script src="{c.url("/static/clear.js")}?v={version}" defer>'
    wrong = fresh(c).post(
        "/login", data={"password": "not the password"}, headers={"Sec-Fetch-Site": "same-origin"}
    )
    for r in (c.get("/login"), c.get("/login?f=out"), wrong):
        assert scripts(r.text) == [clear], r.status_code
        assert "app.js" not in r.text
    assert c.get("/static/clear.js").status_code == 200
    # it empties the pages store of this base: every version's
    js = (STATIC / "clear.js").read_text()
    assert '"tripbox" + base + ":pages:"' in js and "caches.delete" in js
    assert "documentElement.dataset.base" in js
    # every other page loads the page's own script, which empties it before a sign-out is sent
    app_js = (STATIC / "app.js").read_text()
    assert '"tripbox" + base + ":pages:"' in app_js and "data-signout" in app_js


def test_settings_says_how_saved_copies_are_kept_and_removes_them(
    tmp_path, monkeypatch, base, access
):
    def words(html: str) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())

    def section(html: str) -> str:
        found = re.search(
            r'<section[^>]*>\s*<h2 class="section"[^>]*>Saved copies</h2>(.*?)</section>',
            html,
            re.S,
        )
        assert found
        return found.group(1)

    for mode in MODES:
        c, ctx, headers = signed_in(mode, tmp_path, monkeypatch, base, access)
        shown = section(c.get("/settings", headers=headers).text)
        if ctx.settings.offline:
            said = SAVED_COPIES["none" if mode == "none" else "signed"]
            assert words(shown) == f"{said} {REMOVE}", mode
            assert re.findall(r"<button[^>]*>", shown) == [
                '<button type="button" class="btn" data-forget>'
            ], mode
        else:
            assert words(shown) == SWITCHED_OFF, mode
            assert "<button" not in shown, mode
    # with saved copies switched on behind Cloudflare, the sign-out's sentence (the page empties
    # the store before its sign-out link is followed); behind a login proxy, whose sign-out the
    # page never sees, its own
    for mode, said in (("proxy", "proxy"), ("cloudflare", "signed")):
        on = {"TRIPBOX_OFFLINE": "true"}
        c, ctx, headers = signed_in(mode, tmp_path / "on", monkeypatch, base, access, env=on)
        assert ctx.settings.offline, mode
        shown = section(c.get("/settings", headers=headers).text)
        assert words(shown) == f"{SAVED_COPIES[said]} {REMOVE}", mode
    # each mode's own: the page with no login has no sign-out to speak of
    assert "Signing out" not in SAVED_COPIES["none"]
    assert "30 days" in SAVED_COPIES["none"] and "30 days" in SAVED_COPIES["signed"]
    # every sign-out form and link is one the page's script empties the store before
    c, ctx, headers = signed_in("password", tmp_path / "again", monkeypatch, base, access)
    html = c.get("/settings", headers=headers).text
    for action in ("/logout", "/settings/sessions"):
        (form,) = re.findall(
            r'<form method="post" action="' + re.escape(c.url(action)) + r'"[^>]*>', html
        )
        assert "data-signout" in form, action
