# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Share links on the page, at each base: a link opens a read-only page of one trip without a
sign-in (`/s/<token>`), built by an allow-list of its own from what the owner chose to show; it
runs no script, holds no form and no way into the app, and asks not to be kept, indexed or
referred from. Anything but a link that works is the same bare 404. Viewing writes nothing and no
log line holds a token. Making and revoking a link are the owner's, from the page itself."""

import datetime as dt
import itertools
import logging
import re
from html import unescape

from helpers.web import Page, attribution_line, raw_get, starlette_of
from test_planner import items_for
from test_trips_store import car_item
from test_web_login import SAME, as_cookie, fresh, session_of, sign_in
from test_web_login import page as password_page
from test_web_views import flight

import tripbox
from tripbox import util
from tripbox.trips import display, shares
from tripbox.trips import format as fmt
from tripbox.web import shared
from tripbox.web.middleware import SECURITY_HEADERS, SHARED_HEADERS

# Each field's own marker on the trip below: an invented string found nowhere else on its page
TRAVELLER = "Nomina Markerton"
SEAT = "71X"
MARKS = {
    "names": ["Nomina Markerton"],
    "addresses": ["1 Addressmarker Lane", "Deskmarker Counter"],
    "seats": [SEAT],
    "codes": ["CODEMARKF", "CODEMARKH", "CODEMARKC"],
    "notes": ["Notesmarker of the Sample family"],
}
# Bookings the page never shows: one cancelled, one removed
NEVER = ["Cancelledmarker Inn", "Removedmarker Inn"]
REVOKE = "Revoke this link? It stops working at once."
LASTS = (
    "Anyone with the link sees this trip as it is at that moment, without signing in. It works"
    " until 7 days after the trip ends, and at least 30 days."
)
LABELS = ["Names", "Addresses", "Seats", "Times", "Confirmation codes", "Notes"]


def marked_trip(ctx):
    """A trip of the Sample family's whose every field a link may show holds a marker of its own
    (MARKS; its times are its flight's), with a cancelled and a removed stay (NEVER)."""
    st = ctx.trips
    first = flight(
        names=["Alex Sample", TRAVELLER],
        pnr="CODEMARKF",
        seat_by={TRAVELLER: SEAT},
    )
    trip = st.find_or_create(first)
    st.upsert_item(trip, first)
    stays = []
    for code, name in (("CODEMARKH", None), ("CODEMARKX", NEVER[0]), ("CODEMARKY", NEVER[1])):
        stay = items_for(
            ["Alex Sample"],
            kind="lodging",
            dep=first.payload["dep_local"][:10],
            arr=first.payload["arr_local"][:10],
            pnr=code,
        )[0]
        stay.payload["address"] = "1 Addressmarker Lane" if name is None else name + " Road"
        if name is not None:
            stay.payload["property_name"] = name
        stays.append(st.upsert_item(st.get(trip.id), stay))
    car = car_item(
        pickup="Deskmarker Counter",
        start=first.payload["arr_local"],
        end=first.payload["arr_local"],
        code="CODEMARKC",
    )
    st.upsert_item(st.get(trip.id), car)
    st.set_status(stays[1].id, "cancelled")
    st.remove_item(stays[2].id)
    st.set_notes(trip.id, MARKS["notes"][0])
    return st.get(trip.id)


def times_of(ctx, trip) -> list[str]:
    """The times of day the trip's flight shows (its departure and its arrival)."""
    (f,) = [ti for ti in ctx.trips.items(trip.id) if ti.kind == "flight"]
    return [fmt.time_parts(f.start_at, f.start_tz)[0], fmt.time_parts(f.end_at, f.end_tz)[0]]


def link(ctx, trip, shows) -> str:
    """The address, written from `/`, of a new link to `trip` showing `shows`."""
    return "/s/" + shares.create(ctx.ledger, trip.id, shows).token


def owner(c):
    """The Cookie header of the owner, signed in."""
    return as_cookie("tripbox_session", session_of(sign_in(c)))


def present(html: str, marks: list[str]) -> list[bool]:
    return [m in html for m in marks]


def test_the_shared_page_holds_only_what_was_chosen(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    marks = {**MARKS, "times": times_of(ctx, trip)}
    assert all(marks.values())

    def shown(shows) -> dict[str, list[bool]]:
        r = fresh(c).get(link(ctx, trip, shows))  # no cookie
        assert r.status_code == 200, shows
        for never in NEVER:
            assert never not in r.text
        assert "Hilton Example" in r.text and trip.title in r.text  # the booking itself is there
        return {f: present(r.text, marks[f]) for f in shares.FIELDS}

    nothing = shown(())
    assert all(not any(seen) for seen in nothing.values()), nothing
    everything = shown(shares.FIELDS)
    assert all(all(seen) for seen in everything.values()), everything
    for f in shares.FIELDS:
        alone = shown((f,))
        assert all(alone[f]), f
        assert all(not any(alone[g]) for g in shares.FIELDS if g != f), f
        others = shown(tuple(g for g in shares.FIELDS if g != f))
        assert not any(others[f]), f
        assert all(all(others[g]) for g in shares.FIELDS if g != f), f
    # a seat is bare unless the names are shown too, and then it is the traveller's
    bare = fresh(c).get(link(ctx, trip, ("seats",))).text
    assert f"seats {SEAT}<" in bare and "Nomina" not in bare
    named = fresh(c).get(link(ctx, trip, ("seats", "names"))).text
    assert f"seats Nomina Markerton {SEAT}<" in named


def test_the_shared_page_has_no_script_form_or_way_into_the_app(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    r = fresh(c).get(link(ctx, trip, shares.FIELDS))
    assert r.status_code == 200
    html = r.text
    for never in ("<script", "<form", "data-copy", "/trips/", "/items/", 'id="item-', "<input"):
        assert never not in html, never
    # no booking id in any attribute, and no style of its own
    ids = {str(ti.id) for ti in ctx.trips.items(trip.id)}
    assert not re.search(r'\b(?:id|data-[\w-]+)="[^"]*\b(?:' + "|".join(ids) + r')\b', html)
    assert " style=" not in html and "<style" not in html
    # it links nothing but its icon (else a browser asks the host's root for one), its
    # stylesheet and the source code, which it is not referred from
    version = starlette_of(c.app).state.templates.env.globals["asset_v"]
    icon = c.url("/static/favicon.svg")
    stylesheet = c.url(f"/static/app.css?v={version}")
    source = ctx.settings.source_url
    assert re.findall(r'(?:href|src)="([^"]+)"', html) == [icon, stylesheet, source]
    assert f'<link rel="icon" href="{icon}" type="image/svg+xml">' in html
    assert fresh(c).get("/static/favicon.svg").status_code == 200
    assert f'href="{source}" rel="noopener noreferrer"' in html
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert '<meta name="referrer" content="no-referrer">' in html
    # it ends with where it comes from and the about line
    (about,) = re.findall(r'<p class="about[^"]*">(.*?)</p>', html, re.S)
    words = " ".join(unescape(re.sub(r"<[^>]+>", " ", about)).split())
    assert words == (
        f"Shared from tripbox · tripbox {tripbox.__version__} · {attribution_line()} · free"
        " software under the GNU Affero General Public License, version 3, with no warranty ·"
        " Source code"
    )
    assert html.rindex('class="about') > html.rindex("</article>")


def test_the_about_line_shows_the_notices_attribution_line(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    html = fresh(c).get(link(ctx, trip, shares.FIELDS)).text
    (about,) = re.findall(r'<p class="about[^"]*">(.*?)</p>', html, re.S)
    words = " ".join(unescape(re.sub(r"<[^>]+>", " ", about)).split())
    attribution = attribution_line()
    assert f"· {attribution} · free software under the GNU Affero General Public License" in words
    assert words.index(tripbox.__version__) < words.index(attribution)
    assert words.endswith("with no warranty · Source code")


def test_the_view_model_holds_only_listed_keys(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    items = ctx.trips.items(trip.id)
    book, home = starlette_of(c.app).state.book, ctx.settings.home_airports
    for n in range(len(shares.FIELDS) + 1):
        for shows in itertools.combinations(shares.FIELDS, n):
            view = shared.shared_view(trip, items, shows, book=book, home=home)
            top = set(shared.TRIP) | ({"notes"} if "notes" in shows else set())
            assert set(view) == top, shows
            allowed = set(shared.ALWAYS).union(*(shared.ADDS.get(f, ()) for f in shows))
            assert view["days"], shows
            for day in view["days"]:
                assert set(day) == set(shared.DAY), shows
                for booking in day["bookings"]:
                    assert set(booking) == allowed, shows
                    for key, value in booking.items():
                        assert isinstance(value, str) or (
                            key == "lines" and all(isinstance(x, str) for x in value)
                        ), (shows, key)
    # the trip page's presenter is never asked
    calls = []
    monkeypatch.setattr(shared.views, "present", lambda *a, **k: calls.append(a))
    assert fresh(c).get(link(ctx, trip, shares.FIELDS)).status_code == 200
    assert calls == []


def wrong_ones(ctx, trip, base) -> dict[str, bytes]:
    """A raw path, under `base`, of every way a shared link can fail: a token nobody holds, an
    expired, a revoked and a deleted trip's link, one too long, one too short, one not ASCII,
    one with a line break, and the paths around a good one."""
    good = shares.create(ctx.ledger, trip.id, shares.DEFAULT).token
    revoked = shares.create(ctx.ledger, trip.id, shares.DEFAULT)
    ctx.ledger.shares.revoke(revoked.id)
    other = another_trip(ctx)
    deleted = shares.create(ctx.ledger, other.id, shares.DEFAULT).token
    ctx.trips.delete_trip(other.id)
    paths = {
        "nobody's": "/s/" + "A" * 32,
        "revoked": "/s/" + revoked.token,
        "deleted": "/s/" + deleted,
        "too long": "/s/" + good + "A" * 10_000,
        "too short": "/s/" + good[:-1],
        "not ASCII": "/s/" + "%C3%A9" * 32,
        "a line break": "/s/" + good[:-1] + "%0a",
        "a trailing slash": "/s/" + good + "/",
        "an extra segment": "/s/" + good + "/x",
        "no token": "/s/",
    }
    return {name: (base + path).encode() for name, path in paths.items()} | {"good": good.encode()}


def another_trip(ctx):
    """A trip of its own, of one flight on the marked trip's first day."""
    st = ctx.trips
    item = flight(names=["Alex Sample"], pnr="OTHERTRP", number="9881")
    day = dt.date.fromisoformat(item.payload["dep_local"][:10])
    trip = st.new_trip("Elsewhere", day, day)
    st.upsert_item(trip, item)
    return st.get(trip.id)


def test_a_wrong_expired_revoked_or_deleted_link_is_the_same_bare_404(
    tmp_path, monkeypatch, base
):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    cases = wrong_ones(ctx, trip, base)
    good = cases.pop("good").decode()
    answers = {name: raw_get(c.app, raw) for name, raw in cases.items()}
    # expired: the day after the last day it works
    found = shares.find(ctx.ledger, ctx.trips, good, util.today_local())
    until = shares.good_until(found[0], trip)
    after = dt.datetime.combine(until + dt.timedelta(days=1), dt.time(12), dt.UTC)
    with util.clock_at(after):
        answers["expired"] = raw_get(c.app, f"{base}/s/{good}".encode())
    answers["a post"] = raw_get(
        c.app, f"{base}/s/{good}".encode(), [("sec-fetch-site", "same-origin")], method="POST"
    )
    first = answers["nobody's"]
    assert first[0] == 404 and first[2] == b""
    for name, answer in answers.items():
        assert answer == first, name
    assert raw_get(c.app, f"{base}/s/{good}".encode())[0] == 200  # the good one still works
    # what refuses a request before the shared page is asked (a cross-site post, a body over
    # the limit, a method the route does not take) answers alike whatever the token
    wrong = cases["nobody's"]
    for method, headers, status in (
        ("POST", [("sec-fetch-site", "cross-site")], 403),
        ("POST", [("sec-fetch-site", "same-origin"), ("content-length", "70000")], 413),
        ("TRACE", [], 405),
    ):
        paths = (f"{base}/s/{good}".encode(), wrong)
        refused = [raw_get(c.app, raw, headers, method=method) for raw in paths]
        assert refused[0][0] == status and refused[0] == refused[1], method


def test_a_shared_page_is_not_kept_indexed_or_referred_from(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    good = fresh(c).get(link(ctx, trip, shares.DEFAULT))
    gone = fresh(c).get("/s/" + "B" * 32)
    assert (good.status_code, gone.status_code, gone.content) == (200, 404, b"")
    expected = {**SHARED_HEADERS, "Cache-Control": "no-store"}
    assert expected["Referrer-Policy"] == "no-referrer"
    assert expected["X-Robots-Tag"] == "noindex, nofollow"
    policy = expected["Content-Security-Policy"]
    assert "script-src 'none'" in policy
    assert policy == SECURITY_HEADERS["Content-Security-Policy"].replace(
        "script-src 'self'", "script-src 'none'"
    )
    for r in (good, gone):
        for name, value in expected.items():
            assert r.headers.get_list(name) == [value], (r.status_code, name)
    assert '<meta name="robots" content="noindex, nofollow">' in good.text
    assert '<meta name="referrer" content="no-referrer">' in good.text
    # every other page keeps its own policy, and is never meant to be indexed either
    trip_page = Page(c.app, base, headers=owner(c)).get(f"/trips/{trip.id}")
    assert trip_page.headers.get_list("referrer-policy") == ["same-origin"]
    assert trip_page.headers.get_list("x-robots-tag") == ["noindex, nofollow"]


def dump(ctx) -> list[str]:
    """Every row of every table of the ledger, as SQL."""
    return list(ctx.ledger.conn.iterdump())


def test_viewing_a_shared_page_writes_nothing(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    good = link(ctx, trip, shares.FIELDS)
    before = dump(ctx)
    for path in (good, good + "/", "/s/" + "C" * 32):
        fresh(c).get(path)
        fresh(c).head(c.url(path))
    assert dump(ctx) == before


def test_a_share_token_reaches_no_log(tmp_path, monkeypatch, base, caplog):
    caplog.set_level(logging.DEBUG)
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    signed = Page(c.app, base, headers={**owner(c), **SAME})
    made = signed.post(f"/trips/{trip.id}/links", data={"names": "on"}, follow_redirects=False)
    assert made.status_code == 303
    (row,) = ctx.ledger.shares.live()
    token = row["token"]
    near = token[:-1] + ("B" if token[-1] != "B" else "C")
    for path in (f"/s/{token}", f"/s/{near}", f"/s/{token}/", "/s/" + "D" * 32):
        raw_get(c.app, (base + path).encode())
    revoked = signed.post(f"/links/{row['id']}/revoke", follow_redirects=False)
    assert revoked.status_code == 303
    assert raw_get(c.app, f"{base}/s/{token}".encode())[0] == 404
    ours = [r for r in caplog.records if not r.name.startswith("httpx")]
    assert ours, "the posts log what they did"
    said = "\n".join(r.getMessage() for r in ours)
    for secret in (token, near, row["token_hash"], token[:12]):
        assert secret not in said


def test_a_share_link_carries_the_base(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    signed = Page(c.app, base, headers={**owner(c), **SAME})
    made = signed.post(f"/trips/{trip.id}/links", data={"times": "on"}, follow_redirects=False)
    assert made.headers["location"] == c.url(f"/trips/{trip.id}/share#links")
    (row,) = ctx.ledger.shares.live()
    address = ctx.settings.origin + c.url(f"/s/{row['token']}")
    page = signed.get(f"/trips/{trip.id}/share")
    assert f'data-copy="{address}"' in page.text
    assert fresh(c).get(f"/s/{row['token']}").status_code == 200
    # behind a proxy that strips the base, too
    assert Page(c.app, base, stripped=True).get(f"/s/{row['token']}").status_code == 200
    html = fresh(c).get(f"/s/{row['token']}").text
    assert f'href="{c.url("/static/app.css")}?v=' in html


def test_making_and_revoking_a_link_need_a_login_and_a_same_origin_post(
    tmp_path, monkeypatch, base
):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    cookie = owner(c)
    nobody = Page(c.app, base, headers=SAME)
    elsewhere = Page(c.app, base, headers={**cookie, "Origin": "https://evil.example"})
    signed = Page(c.app, base, headers={**cookie, **SAME})
    form = {"names": "on", "codes": "on", "passwords": "on"}
    assert nobody.post(f"/trips/{trip.id}/links", data=form).status_code == 401
    assert elsewhere.post(f"/trips/{trip.id}/links", data=form).status_code == 403
    assert ctx.ledger.shares.live() == []
    made = signed.post(f"/trips/{trip.id}/links", data=form, follow_redirects=False)
    assert (made.status_code, made.headers["location"]) == (
        303,
        c.url(f"/trips/{trip.id}/share#links"),
    )
    (row,) = ctx.ledger.shares.live()
    found = shares.find(ctx.ledger, ctx.trips, row["token"], util.today_local())
    assert found is not None and found[0].shows == ("names", "codes")
    assert signed.post("/trips/999999/links", data=form).status_code == 404
    # revoking: the owner's, from the page itself; back where the form was
    assert nobody.post(f"/links/{row['id']}/revoke").status_code == 401
    assert elsewhere.post(f"/links/{row['id']}/revoke").status_code == 403
    assert fresh(c).get(f"/s/{row['token']}").status_code == 200
    back = signed.post(f"/links/{row['id']}/revoke", follow_redirects=False)
    assert back.headers["location"] == c.url(f"/trips/{trip.id}/share#links")
    assert fresh(c).get(f"/s/{row['token']}").status_code == 404
    again = shares.create(ctx.ledger, trip.id, ())
    back = signed.post(
        f"/links/{again.id}/revoke", data={"from": "settings"}, follow_redirects=False
    )
    assert back.headers["location"] == c.url("/settings#links")
    assert signed.post("/links/999999/revoke").status_code == 404


def words(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def test_the_share_page_and_settings_list_each_live_link(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    signed = Page(c.app, base, headers=owner(c))
    page = signed.get(f"/trips/{trip.id}/share").text
    (section,) = re.findall(r'<section id="links"[^>]*>(.*?)</section>', page, re.S)
    boxes = re.findall(
        r'<label><input type="checkbox" name="(\w+)"( checked)?> ([^<]+)</label>', section
    )
    assert [(n, bool(on), label) for n, on, label in boxes] == [
        (f, f in shares.DEFAULT, label) for f, label in zip(shares.FIELDS, LABELS, strict=True)
    ]
    assert words(section).startswith("Share a link") and LASTS in words(section)
    assert ">Make a link</button>" in section
    assert "Works until" not in section  # no link yet
    one = shares.create(ctx.ledger, trip.id, ("names", "codes"))
    two = shares.create(ctx.ledger, trip.id, ())
    ctx.ledger.shares.revoke(two.id)
    page = signed.get(f"/trips/{trip.id}/share").text
    (section,) = re.findall(r'<section id="links"[^>]*>(.*?)</section>', page, re.S)
    until = fmt.date_range(*[shares.good_until(one, trip).isoformat()] * 2)
    said = words(section)
    assert f"Names, Confirmation codes Works until {until} Copy link Revoke" in said
    assert said.count("Works until") == 1  # the revoked one is gone
    address = ctx.settings.origin + c.url(f"/s/{one.token}")
    assert f'data-copy="{address}"' in section
    assert re.search(
        r'<form method="post" action="'
        + re.escape(c.url(f"/links/{one.id}/revoke"))
        + r'" data-confirm="'
        + re.escape(REVOKE)
        + '"',
        section,
    )
    # Settings lists every live link the same way, under its trip's title
    settings = signed.get("/settings").text
    (listed,) = re.findall(r'<section id="links"[^>]*>(.*?)</section>', settings, re.S)
    assert words(listed).startswith(f"Shared links {trip.title} Names, Confirmation codes")
    assert f'data-copy="{address}"' in listed and 'name="from" value="settings"' in listed
    ctx.trips.delete_trip(trip.id)
    settings = signed.get("/settings").text
    (listed,) = re.findall(r'<section id="links"[^>]*>(.*?)</section>', settings, re.S)
    assert words(listed) == "Shared links No shared links."


def test_the_page_says_the_trip_in_the_owners_display_style(tmp_path, monkeypatch, base):
    c, ctx = password_page(tmp_path, monkeypatch, base)
    trip = marked_trip(ctx)
    path = link(ctx, trip, shares.DEFAULT)
    default = fresh(c).get(path).text
    signed = Page(c.app, base, headers={**owner(c), **SAME})
    saved = signed.post(
        "/settings/display", data={"clock": "24", "dates": "dmy"}, follow_redirects=False
    )
    assert saved.status_code == 303
    styled = fresh(c).get(path).text
    with fmt.styled(display.Display(clock="24", dates="dmy")):
        band = fmt.day_band(dt.date.fromisoformat(trip.start_date))
        times = times_of(ctx, trip)
    assert band in styled and band not in default
    twelve = times_of(ctx, trip)
    assert all(t in styled for t in times) and not any(t in styled for t in twelve)
    assert all(t in default for t in twelve) and times[1] not in default
