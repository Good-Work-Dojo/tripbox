# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The Settings page, at each base and behind each login: the status card, the display choices,
the calendar feed and a new address for it, the shared links, how to sign out, and the line that
says what the page is and where its source code is (also at the end of the login page). Every
sentence is checked word for word; the page and its three posts are private and every post
must come from the page itself."""

import re
from html import unescape

import pytest
from helpers.web import attribution_line, client_for
from jwt import PyJWKClient
from test_web_auth import ISS, Keys, behind_access, behind_proxy
from test_web_login import SAME, as_cookie, fresh, get, rows, session_of, set_cookies, sign_in
from test_web_login import page as password_page
from test_web_login import value as cookie_value

import tripbox
from tripbox.config import Settings
from tripbox.trips import display, ical

HEADINGS = [
    "Status",
    "Display",
    "Calendar",
    "Shared links",
    "Saved copies",
    "Sign in",
    "About",
]
CHOICE_LABELS = [
    "12-hour",
    "24-hour",
    "Month first (Mar 13)",
    "Day first (13 Mar)",
    "Apple Maps",
    "Google Maps",
    "OpenStreetMap",
]
CALENDAR = (
    "Subscribe to this address in your calendar app to see every trip there. Anyone who has the"
    " address can read your trips."
)
NEW_ADDRESS = "Make a new address? Calendar apps that use the old one stop updating."
NO_LINKS = "No shared links."
EVERYWHERE = "Sign out on every device?"
EVERYWHERE_SAID = (
    "Signs out every device, this one too. Change the password as well if someone else knows it."
)
NOTICES_FOLLOW = (
    "Notifications follow these when you choose the 24-hour clock or day-first dates."
)
PROXY_SIGN_OUT = "You sign in through your login proxy. Sign out there."
NO_SIGN_IN = "This page has no sign-in and answers only on this computer."
LET_THROUGH = (
    "Let these paths through your login proxy without a sign-in, so shared links and calendar"
    " apps work: {base}/s/, {base}/feed/, {base}/static/."
)
ABOUT = (
    "tripbox {version} · {attribution} · free software under the GNU Affero General Public"
    " License, version 3, with no warranty · Source code"
)
DEFAULT_SOURCE = "https://github.com/Good-Work-Dojo/tripbox"
MODES = ("password", "cloudflare", "proxy", "none")


@pytest.fixture
def access(monkeypatch):
    """The Access login's signing keys, served as its key set."""
    k = Keys()
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: k.jwks)
    return k


def signed_in(mode, tmp_path, monkeypatch, base, access, env=None):
    """The page behind the login `mode` under `base`, its context, and the headers of a request
    that login lets through."""
    tmp = tmp_path / mode
    tmp.mkdir()
    if mode == "password":
        c, ctx = password_page(tmp, monkeypatch, base, env=env)
        return c, ctx, as_cookie("tripbox_session", session_of(sign_in(c)))
    if mode == "cloudflare":
        c, ctx = behind_access(tmp, monkeypatch, base)
        return c, ctx, {"Cf-Access-Jwt-Assertion": access.token()}
    if mode == "proxy":
        c, ctx = behind_proxy(tmp, monkeypatch, base)
        return c, ctx, {"Remote-User": "alex"}
    c, ctx = client_for(tmp, monkeypatch, base=base, env=env)
    return c, ctx, {}


def text_of(html: str) -> str:
    """The page's words, tags out and spaces squeezed."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def section(html: str, heading: str) -> str:
    """The section of the Settings page under `heading`, as HTML."""
    found = re.search(
        r'<section[^>]*>\s*<h2 class="section"[^>]*>'
        + re.escape(heading)
        + r"</h2>(.*?)</section>",
        html,
        re.S,
    )
    assert found, heading
    return found.group(1)


def about(html: str) -> tuple[str, list[str]]:
    """The about line's words and its links."""
    (line,) = re.findall(r'<p class="about[^"]*">(.*?)</p>', html, re.S)
    return text_of(line), re.findall(r'href="([^"]+)"', line)


def test_settings_shows_each_section_in_every_login_mode(tmp_path, monkeypatch, base, access):
    for mode in MODES:
        c, ctx, headers = signed_in(mode, tmp_path, monkeypatch, base, access)
        r = get(c, "/settings", headers)
        assert r.status_code == 200, mode
        html = r.text
        assert re.findall(r'<h2 class="section"[^>]*>(.*?)</h2>', html) == HEADINGS, mode
        assert " style=" not in html and "<style" not in html and "<script>" not in html
        assert 'class="status-card"' in section(html, "Status")
        # the display choices: three groups, each named, and Save
        shown = section(html, "Display")
        assert text_of(shown).startswith(f"{NOTICES_FOLLOW} Clock ")
        assert re.findall(r"<legend>(.*?)</legend>", shown) == ["Clock", "Dates", "Maps"]
        labels = re.findall(r"<label[^>]*>.*?<span>(.*?)</span>", shown, re.S)
        assert [unescape(t) for t in labels] == CHOICE_LABELS
        assert re.findall(r'<form method="post" action="([^"]+)"', shown) == [
            c.url("/settings/display")
        ]
        checked = re.findall(r'name="(\w+)" value="(\w+)" checked', shown)
        assert checked == [("clock", "12"), ("dates", "mdy"), ("maps", "apple")], mode
        assert ">Save</button>" in shown
        # the calendar: what it is, its address and a new one
        cal = section(html, "Calendar")
        token = ical.feed_token(ctx.ledger)
        address = ctx.settings.origin + c.url(f"/feed/{token}.ics")
        assert CALENDAR in text_of(cal)
        assert f'value="{address}"' in cal and f'data-copy="{address}"' in cal
        assert re.search(
            r'<form method="post" action="' + re.escape(c.url("/settings/feed")) + r'"'
            r' data-confirm="' + re.escape(NEW_ADDRESS) + '"',
            cal,
        ), mode
        assert ">Make a new address</button>" in cal
        assert text_of(section(html, "Shared links")) == NO_LINKS
        # signing in, as the login does it
        said = text_of(section(html, "Sign in"))
        through = LET_THROUGH.format(base=base)
        sign = section(html, "Sign in")
        if mode == "password":
            forms = re.findall(r'<form method="post" action="([^"]+)"([^>]*)>', sign)
            assert forms == [
                (c.url("/logout"), " data-signout"),
                (c.url("/settings/sessions"), f' data-confirm="{EVERYWHERE}" data-signout'),
            ]
            assert ">Sign out</button>" in sign and ">Sign out everywhere</button>" in sign
            assert said == f"Sign out Sign out everywhere {EVERYWHERE_SAID}"
        elif mode == "cloudflare":
            assert re.findall(r'<a [^>]*href="([^"]+)"[^>]*>Sign out</a>', sign) == [
                f"{ISS}/cdn-cgi/access/logout"
            ]
            assert said == f"Sign out {through}"
        elif mode == "proxy":
            assert said == f"{PROXY_SIGN_OUT} {through}"
        else:
            assert said == NO_SIGN_IN
        assert "<form" not in sign or mode == "password"
        # what the page is, and where its source code is
        assert about(section(html, "About")) == (
            ABOUT.format(version=tripbox.__version__, attribution=attribution_line()),
            [DEFAULT_SOURCE],
        )
    # the choices saved, with a toast, are the ones the form shows checked
    r = c.post(
        "/settings/display",
        data={"clock": "24", "dates": "dmy", "maps": "google"},
        headers=SAME,
        follow_redirects=False,
    )
    assert (r.status_code, r.headers["location"]) == (303, c.url("/settings?f=saved"))
    html = c.get("/settings?f=saved").text
    assert 'data-flash="Saved"' in html
    checked = re.findall(r'name="(\w+)" value="(\w+)" checked', html)
    assert checked == [("clock", "24"), ("dates", "dmy"), ("maps", "google")]
    assert display.read(ctx.ledger) == display.Display("24", "dmy", "google")


def test_a_new_feed_address_ends_the_old_one(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    old = ical.feed_token(ctx.ledger)
    assert c.get(f"/feed/{old}.ics").status_code == 200
    # a post from elsewhere changes nothing
    cross = c.post("/settings/feed", headers={"Origin": "https://elsewhere.example"})
    assert cross.status_code == 403 and ical.feed_token(ctx.ledger) == old
    r = c.post("/settings/feed", headers=SAME, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, c.url("/settings#calendar"))
    new = ical.feed_token(ctx.ledger)
    assert new != old
    gone = c.get(f"/feed/{old}.ics", follow_redirects=False)
    assert gone.status_code == 404 and gone.content == b"" and "location" not in gone.headers
    assert c.get(f"/feed/{new}.ics").status_code == 200
    html = c.get("/settings").text
    assert f"/feed/{new}.ics" in html and old not in html


def test_sign_out_everywhere_ends_every_session(tmp_path, monkeypatch, base, access):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    here, there = sign_in(c), sign_in(c)  # two browsers
    mine = as_cookie("tripbox_session", session_of(here))
    theirs = as_cookie("tripbox_session", session_of(there))
    devices = {d["id_hash"] for d in rows(ctx, "devices")}
    assert len(devices) == 2 and len(rows(ctx, "sessions")) == 2
    # from elsewhere, or with no session, nothing ends
    elsewhere = {"Origin": "https://elsewhere.example", **mine}
    cross = fresh(c).post("/settings/sessions", headers=elsewhere)
    assert cross.status_code == 403
    assert fresh(c).post("/settings/sessions", headers=SAME).status_code == 401
    assert len(rows(ctx, "sessions")) == 2
    out = fresh(c).post("/settings/sessions", headers={**SAME, **mine}, follow_redirects=False)
    # then it is this browser's sign-out
    assert (out.status_code, out.headers["location"]) == (303, c.url("/login?f=out"))
    assert set(set_cookies(out)) == {"tripbox_session", "__Secure-tripbox_session"}
    assert all("Max-Age=0" in line for line in set_cookies(out).values())
    assert out.headers.get_list("clear-site-data")
    assert rows(ctx, "sessions") == []
    assert get(c, "/", mine).status_code == 401 and get(c, "/", theirs).status_code == 401
    # each browser keeps its own sign-in bucket: the devices stay known
    assert {d["id_hash"] for d in rows(ctx, "devices")} == devices
    device = cookie_value(set_cookies(here)["tripbox_device"])
    assert sign_in(c, cookie=f"tripbox_device={device}").status_code == 303
    assert {d["id_hash"] for d in rows(ctx, "devices")} == devices
    # behind any other login there is nothing of the page's own to end
    for mode in ("cloudflare", "proxy", "none"):
        o, octx, headers = signed_in(mode, tmp_path, monkeypatch, base, access)
        r = fresh(o).post("/settings/sessions", headers={**SAME, **headers})
        assert r.status_code == 404, mode


def test_the_source_link_is_the_configured_one_on_settings_and_login(tmp_path, monkeypatch, base):
    own = "https://code.example/tripbox"
    c, ctx = password_page(tmp_path, monkeypatch, base, env={"TRIPBOX_SOURCE_URL": own})
    signed = as_cookie("tripbox_session", session_of(sign_in(c)))
    line = ABOUT.format(version=tripbox.__version__, attribution=attribution_line())
    assert about(get(c, "/settings", signed).text) == (line, [own])
    login = get(c, "/login").text
    assert about(login) == (line, [own])
    main = login[login.index('<main class="page signin">') : login.index("</main>")]
    assert main.rstrip().endswith("</p>") and main.rindex('class="about') > main.rindex("</form>")
    # unset, the project's own address; a value that is no web address is a problem of the link
    # alone: listed, the page still served, the link the project's own
    assert Settings.from_env({}).source_url == DEFAULT_SOURCE
    for n, bad in enumerate(("code.example/tripbox", "ftp://code.example/", "javascript:alert(1)")):
        s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_SOURCE_URL": bad})
        assert [(p.key, p.feature) for p in s.problems] == [("TRIPBOX_SOURCE_URL", "web")], bad
        assert s.feature("web").state == "on" and s.source_url == DEFAULT_SOURCE, bad
        tmp = tmp_path / f"bad-{n}"
        tmp.mkdir()
        c, ctx = password_page(tmp, monkeypatch, base, env={"TRIPBOX_SOURCE_URL": bad})
        signed = as_cookie("tripbox_session", session_of(sign_in(c)))
        assert about(get(c, "/settings", signed).text) == (line, [DEFAULT_SOURCE]), bad
        assert about(get(c, "/login").text) == (line, [DEFAULT_SOURCE]), bad


def test_the_about_line_shows_the_notices_attribution_line(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    signed = as_cookie("tripbox_session", session_of(sign_in(c)))
    attribution = attribution_line()
    assert attribution.startswith("© ")
    for html in (get(c, "/settings", signed).text, get(c, "/login").text):
        words, links = about(html)
        # after the version, then the licence and the no-warranty words, then the source link
        assert words == ABOUT.format(version=tripbox.__version__, attribution=attribution)
        assert words.index(attribution) > words.index(tripbox.__version__)
        assert words.index(attribution) < words.index("free software under the GNU Affero")
        assert links == [DEFAULT_SOURCE]


def test_the_list_links_settings_and_no_longer_holds_the_feed(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    token = ical.feed_token(ctx.ledger)
    html = c.get("/").text
    links = re.findall(r'<a class="icon-btn" href="([^"]+)" aria-label="Settings">', html)
    assert links == [c.url("/settings")]
    assert "/feed/" not in html and token not in html and "Subscribe" not in html
    assert c.get("/settings").status_code == 200
