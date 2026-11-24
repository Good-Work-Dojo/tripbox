# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import dataclasses
import datetime as dt
import re
import signal
import subprocess
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from helpers.web import ORIGIN, client_for
from markupsafe import escape
from test_checks import booking, validated
from test_planner import BOOK, items_for
from test_store_actions import an_hour_later, rows
from test_trips_store import car_item

import tripbox.web
from tripbox import util
from tripbox.notify.telegram import check_keyboard
from tripbox.trips import format as fmt
from tripbox.trips import grouping
from tripbox.trips.covers import CoverStore
from tripbox.validate import checks
from tripbox.validate.keys import is_draft
from tripbox.web import views

HOME = ("MSP",)
WEB = Path(tripbox.web.__file__).resolve().parent
# The static files that hold text (the rest are images and fonts)
TEXT_FILES = (".css", ".js", ".svg", ".txt", ".html", ".json", ".webmanifest")


def flight(**kw):
    kw.setdefault("dep", "2027-09-16T10:25")
    kw.setdefault("arr", "2027-09-16T13:58")
    kw.setdefault("pnr", "QTWZSV")
    kw.setdefault("number", "9304")
    kw.setdefault("carrier", "SY")
    kw.setdefault("provider", "Sun Country Airlines")
    names = kw.pop("names", ["Alex Sample", "Sam Sample", "Robin Sample"])
    return items_for(names, **kw)[0]


def seed_trip(ctx):
    ctx.ledger.trips.insert(
        title="Philadelphia",
        place="Philadelphia",
        start_date="2027-09-16",
        end_date="2027-09-16",
        id=10,
    )
    st = ctx.trips
    st.upsert_item(st.get(10), flight(seat_by={"Alex Sample": "8A", "Sam Sample": "8B"}))
    hotel = items_for(
        ["Alex Sample", "Sam Sample"],
        kind="lodging",
        dep="2027-09-16",
        arr="2027-09-21",
        pnr="HOTEL1",
    )[0]
    hotel.payload["address"] = "100 Example Street, Philadelphia, PA 00000"
    st.upsert_item(st.get(10), hotel)
    st.upsert_item(
        st.get(10),
        car_item(
            pickup="200 Example Avenue, Philadelphia PA 00000",
            start="2027-09-16T14:45",
            end="2027-09-21T13:15",
            code="CAR123",
        ),
    )
    st.upsert_item(
        st.get(10),
        flight(
            origin="PHL",
            dest="MSP",
            dep="2027-09-21T16:45",
            arr="2027-09-21T18:50",
            pnr="Q9TW8Z",
            number="9246",
            carrier="DL",
            provider="Delta Air Lines",
            names=["Alex Sample"],
        ),
    )
    return st.get(10)


def seed_past(ctx, n, start_year=2025):
    st = ctx.trips
    for i in range(n):
        year = start_year - i // 6
        month = 12 - (i % 6) * 2
        f = flight(
            dest="DEN",
            dep=f"{year}-{month:02d}-05T08:00",
            arr=f"{year}-{month:02d}-05T09:10",
            pnr=f"P{i:03d}",
            number=str(100 + i),
            carrier="DL",
            provider="Delta Air Lines",
            names=["Alex Sample"],
        )
        f.payload["destination_city"] = f"City{i}"
        st.upsert_item(st.find_or_create(f), f)


def test_list_page_orders_sections_and_annotates_cards(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    seed_past(ctx, 2, start_year=2025)  # City0 Dec 2025, City1 Oct 2025
    st = ctx.trips
    gone = flight(
        dest="DEN",
        dep="2027-07-04T08:00",
        arr="2027-07-04T09:10",
        pnr="GONE1",
        number="9",
        carrier="DL",
        provider="Delta Air Lines",
        names=["Alex Sample"],
    )
    gone.payload["destination_city"] = "Nowhere"
    st.upsert_item(st.find_or_create(gone), gone)
    st.cancel(gone.key)
    html = c.get("/").text
    up = html.index("Upcoming")
    past = html.index("Past trips")
    assert up < html.index("Philadelphia") < past
    assert "Departs Thu Sep 16 · 10:25 AM CDT" in html
    assert "✈️ 2 · 🏨 1 · 🚗 1" in html and "Alex, Sam, Robin" in html
    assert html.index("Nowhere") > past and "Cancelled" in html
    assert past < html.index("City0") < html.index("City1")
    assert html.index('class="year"') > past and ">2025<" in html
    assert "Flight log" not in html  # a flight log is kept, but its address is not set


def test_a_new_install_without_a_travellers_file_shows_the_empty_page(
    tmp_path, monkeypatch, base
):
    (tmp_path / "config").mkdir()
    no_travellers = {"TRIPBOX_CONFIG_DIR": str(tmp_path / "config")}
    c, ctx = client_for(tmp_path, monkeypatch, base=base, airtrail=False, env=no_travellers)
    s = ctx.settings
    assert s.problems == () and s.travellers.me_name == ""
    page = c.get("/")
    assert page.status_code == 200 and ">No trips yet</h2>" in page.text


def test_past_trips_are_limited_to_ten_until_show_all(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_past(ctx, 12)
    html = c.get("/").text
    assert html.count('class="card ') == 10 and "Show all" in html
    assert c.get("/?past=all").text.count('class="card ') == 12


def test_empty_state_and_the_count_of_bookings_to_check(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    html = c.get("/").text  # a first run: what to do, not an empty list
    assert ">No trips yet</h2>" in html and '<p class="empty">' not in html
    assert "to check" not in html
    st = ctx.trips
    # two flights that name nobody, each marked while a flight log is kept: each trip's card
    # counts its own
    one, two = items_for([])[0], flight(names=[])
    first = st.upsert_item(st.find_or_create(one), one)
    assert first.checks == ("owner_unknown",)
    assert Marks.of(c.get("/").text).chips == ["1 to check"]
    second = st.upsert_item(st.find_or_create(two), two)
    assert second.checks == ("owner_unknown",)
    counts = st.flag_counts()
    assert sum(counts.values()) == 2 and set(counts) == {first.trip_id, second.trip_id}
    listing = c.get("/").text
    for trip_id, n in counts.items():
        card = listing[listing.index(f'href="{c.url(f"/trips/{trip_id}")}"') :]
        assert f'class="chip chip-amber">{n} to check<' in card[: card.index("</a>")], trip_id


def undated_stay(pnr="HOTEL1"):
    """A stay whose check-in is not known: filed with no trip and no start."""
    stay = items_for(
        ["Alex Sample"], kind="lodging", dep="2027-09-16", arr="2027-09-21", pnr=pnr
    )[0]
    stay.payload["check_in"] = None
    return stay


def test_no_page_fails_for_a_booking_with_no_date(tmp_path, monkeypatch, base):
    """A booking with no date is in no trip. The list shows it in its Unsorted group, its own
    forms answer without an error (it has no trip to move to, and every redirect lands on the
    list), Recently deleted shows it with no day, and restoring it puts it back in the group."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    hdr = {"Sec-Fetch-Site": "same-origin"}
    st = ctx.trips
    ti = st.upsert_item(None, undated_stay())
    assert (ti.trip_id, ti.start_at, ti.checks) == (None, None, ("check_in",))
    page = c.get("/")
    assert page.status_code == 200 and ti.id in Marks.of(unsorted_group(page.text)).items
    edit = c.get(f"/items/{ti.id}/edit")
    assert edit.status_code == 200
    assert f'href="{c.url("/#unsorted")}" aria-label="Back to the list"' in edit.text
    moved = c.post(f"/items/{ti.id}/move", data={"to": "new"}, headers=hdr)
    assert moved.status_code == 400 and st.all_trips() == []
    r = c.post(
        f"/items/{ti.id}/status",
        data={"status": "cancelled"},
        headers=hdr,
        follow_redirects=False,
    )
    assert (r.status_code, r.headers["location"]) == (303, c.url("/"))
    assert st.item(ti.id).status == "cancelled"
    r = c.post(f"/items/{ti.id}/remove", headers=hdr, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, c.url("/?f=removed"))
    deleted = c.get("/deleted")
    assert deleted.status_code == 200 and "Hilton Example" in deleted.text
    r = c.post(f"/items/{ti.id}/restore", headers=hdr, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, c.url("/?f=restored"))
    back = st.item(ti.id)
    assert (back.removed_at, back.trip_id, back.start_at) == (None, None, None)
    assert [u.id for u in st.unsorted()] == [ti.id]
    page = c.get("/")
    assert page.status_code == 200 and ti.id in Marks.of(unsorted_group(page.text)).items


def test_the_page_loads_no_code_that_files_mail():
    """The page's container reads the ledger, shows it, stores what it is handed to read and
    reads whether a worker runs: nothing that reads mail, asks a model, files a booking, writes
    the flight log, runs the worker or sends a notification is loaded with it, nor a model's
    SDK, nor the HTTP client the worker calls out with, nor the image library (`PIL`). Of the
    readers' package it loads the wire schema alone (`tripbox.parse.models`, the plain data
    shapes the store rebuilds a booking from)."""
    shown = (
        "import sys, tripbox.web.app, tripbox.web.views, tripbox.web.login, tripbox.web.card,"
        " tripbox.trips.format, tripbox.trips.coversniff, tripbox.uploads, tripbox.status; "
        "print(sorted(m for m in sys.modules if m.startswith(("
        "'tripbox.pipeline', 'tripbox.mail', 'tripbox.parse', 'tripbox.write',"
        " 'tripbox.worker', 'tripbox.notify', 'anthropic', 'openai', 'httpx2', 'PIL'))))"
    )
    run = subprocess.run(
        [sys.executable, "-c", shown], capture_output=True, text=True, check=True
    )
    assert run.stdout.strip() == "['tripbox.parse', 'tripbox.parse.models']", run.stdout


def test_a_booking_whose_checks_cannot_be_worked_out_still_shows(tmp_path, monkeypatch, base):
    """A stored payload that no check can be worked out from (a stamp that is no time) keeps the
    checks the booking had, and the pages show it as it is."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    st = ctx.trips
    (unnamed,) = items_for([])  # names nobody: marked while a flight log is kept
    marked = st.upsert_item(trip, unnamed)
    assert marked.checks == ("owner_unknown",)
    payload = {**marked.payload, "raised": {"low_confidence": "soon"}, "looked_at": "later"}
    ctx.ledger.trip_items.update(marked.id, payload=payload)
    assert st.settle(marked.id).checks == ("owner_unknown",)
    assert st.item(marked.id).payload["raised"] == {"low_confidence": "soon"}
    for path in ("/", f"/trips/{trip.id}"):
        page = c.get(path)
        assert page.status_code == 200 and "Philadelphia" in page.text, path
    assert Marks.of(c.get("/").text).chips == ["1 to check"]
    assert unnamed.payload["flight_number"] in c.get(f"/trips/{trip.id}").text


def test_trip_page_timeline(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    seed_past(ctx, 1)  # another trip, so the merge form has a target
    html = c.get("/trips/10").text
    assert "THURSDAY, SEPTEMBER 16, 2027" in html and "TUESDAY, SEPTEMBER 21, 2027" in html
    # timed items first (the 5 PM car), then the date-only hotel check-in
    assert html.index("MSP → PHL") < html.index("Example Rent A Car") < html.index("Hilton Example")
    assert "SY 9304 (Sun Country)" in html and "arrives 1:58 PM EDT" in html
    maps = "https://maps.apple.com/?q=100%20Example%20Street%2C%20Philadelphia%2C%20PA%2000000"
    assert f'href="{maps}"' in html
    assert 'data-copy="QTWZSV"' in html
    assert 'href="https://flightaware.com/live/flight/SY9304"' in html
    assert "2:45 PM" in html and html.count("EDT") >= 2  # the car's times carry Philadelphia's zone
    assert "People" in html and "Alex, Sam, Robin" in html
    assert 'name="title" value="Philadelphia"' in html
    assert f'action="{c.url("/trips/10/merge")}"' in html
    assert f'action="{c.url("/items/")}' in html and '<option value="new">' in html
    assert f'href="{c.url("/trips/10/share")}"' in html


def test_share_page_and_text_endpoint(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    back = st.item_by_key("DL|9246|2027-09-21|PHL")
    st.cancel(back.item_key)
    html = c.get("/trips/10/share").text
    for kind, label in (
        ("all", "All"),
        ("flight", "Flights"),
        ("lodging", "Hotels"),
        ("car", "Cars"),
    ):
        pressed = "true" if kind == "all" else "false"
        assert f'data-kind="{kind}" aria-pressed="{pressed}">{label}</button>' in html
    assert f'value="{back.id}" data-kind="flight">' in html  # cancelled: not checked
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    assert f'value="{out.id}" data-kind="flight" checked>' in html
    # the text's own toggles (the share links' form below it has boxes of its own)
    (text_form,) = re.findall(r'<form id="share-form".*?</form>', html, re.S)
    assert (
        'name="names" checked' in text_form
        and 'name="conf" checked' in text_form
        and 'name="notes"' not in text_form
    )
    r = c.get(f"/trips/10/text?items={out.id},{back.id}&conf=0&names=1")
    assert r.headers["content-type"].startswith("text/plain")
    expect = fmt.trip_text(
        st.get(10),
        st.items(10),
        book=BOOK,
        home_airports=HOME,
        include_conf=False,
        selected={out.id, back.id},
    )
    assert r.text == expect and "CANCELLED" in r.text and "conf" not in r.text
    assert c.get("/trips/10/text").text == fmt.trip_text(
        st.get(10), st.items(10), book=BOOK, home_airports=HOME
    )
    st.set_notes(10, "Gate code 1234")
    html = c.get("/trips/10/share").text
    (text_form,) = re.findall(r'<form id="share-form".*?</form>', html, re.S)
    assert 'name="notes" checked' in text_form
    assert "Notes: Gate code 1234" not in c.get("/trips/10/text?notes=0").text


def test_unknown_trips_are_404(tmp_path, monkeypatch, base):
    c, _ = client_for(tmp_path, monkeypatch, base=base)
    for path in ("/trips/999", "/trips/999/share", "/trips/999/text"):
        assert c.get(path).status_code == 404


def test_rename_merge_and_move_repairs(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    seed_past(ctx, 1)  # one other trip to merge into
    other = [t for t in st.all_trips() if t.id != 10][0]
    hdr = {"Sec-Fetch-Site": "same-origin"}
    r = c.post(
        "/trips/10/rename",
        data={"title": "  Philadelphia in September "},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == c.url("/trips/10")
    assert st.get(10).title == "Philadelphia in September"
    car = st.item_by_key("car|CAR123")
    r = c.post(
        f"/items/{car.id}/move",
        data={"to": str(other.id)},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == c.url(f"/trips/{other.id}")
    assert st.item(car.id).trip_id == other.id
    r = c.post(
        f"/items/{car.id}/move", data={"to": "new"}, headers=hdr, follow_redirects=False
    )
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert new_id not in (10, other.id) and st.get(new_id).title == "Philadelphia"
    r = c.post(
        f"/trips/{other.id}/merge", data={"into": "10"}, headers=hdr, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == c.url("/trips/10")
    assert st.get(other.id) is None and len(st.items(10)) == 4
    # moving the last item out of the new trip deletes it and lands on the target
    last = st.items(new_id)[0]
    r = c.post(
        f"/items/{last.id}/move", data={"to": "10"}, headers=hdr, follow_redirects=False
    )
    assert r.headers["location"] == c.url("/trips/10") and st.get(new_id) is None
    assert c.post("/trips/999/rename", data={"title": "x"}, headers=hdr).status_code == 404
    assert c.post("/items/999/move", data={"to": "10"}, headers=hdr).status_code == 404
    assert c.post("/trips/10/merge", data={"into": "999"}, headers=hdr).status_code == 404
    assert (
        c.post(
            "/trips/10/merge", data={"into": "10"}, headers=hdr, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        c.post("/trips/10/rename", data={"title": "x"}).status_code == 403
    )  # no fetch metadata


def test_notes_round_trip(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    hdr = {"Sec-Fetch-Site": "same-origin"}
    r = c.post(
        "/trips/10/notes",
        data={"notes": "Gate code 1234\r\n\r\nBEGIN:VEVENT is just text 🎉"},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert ctx.trips.get(10).notes == "Gate code 1234\n\nBEGIN:VEVENT is just text 🎉"
    html = c.get("/trips/10").text
    assert (
        "Gate code 1234" in html and "BEGIN:VEVENT is just text 🎉" in html and "Edit notes" in html
    )
    assert "\n\nNotes: Gate code 1234\n\nBEGIN:VEVENT is just text 🎉\n\nThu Sep 16\n" in (
        c.get("/trips/10/text").text
    )
    c.post("/trips/10/notes", data={"notes": "   "}, headers=hdr)
    assert ctx.trips.get(10).notes is None and "Add notes" in c.get("/trips/10").text


def test_calendar_download_and_token_feed(tmp_path, monkeypatch, base):
    from tripbox.trips import ical

    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    r = c.get("/trips/10/trip.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in r.text
    assert 'filename="philadelphia.ics"' in r.headers["content-disposition"]
    token = ical.feed_token(ctx.ledger)
    r = c.get(f"/feed/{token}.ics")
    assert r.status_code == 200 and "X-WR-CALNAME:Trips" in r.text
    bad = c.get("/feed/wrong.ics", follow_redirects=False)
    assert bad.status_code == 404 and bad.content == b"" and "location" not in bad.headers
    assert c.get("/feed/.ics").status_code == 404
    html = c.get("/settings").text
    assert "Calendar" in html and c.url(f"/feed/{token}.ics") in html


def test_share_page_has_no_empty_day_band_for_a_check_out_day(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    ctx.ledger.trips.insert(
        title="Des Moines",
        place="Des Moines",
        start_date="2027-11-16",
        end_date="2027-11-17",
        id=13,
    )
    hotel = items_for(
        ["Alex Sample"], kind="lodging", dep="2027-11-16", arr="2027-11-17", pnr="HM1"
    )[0]
    ctx.trips.upsert_item(ctx.trips.get(13), hotel)
    html = c.get("/trips/13/share").text
    assert "TUESDAY, NOVEMBER 16, 2027" in html and "NOVEMBER 17" not in html


def test_next_up_line_names_the_first_booking_kind(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    st = ctx.trips
    hotel = items_for(
        ["Alex Sample"], kind="lodging", dep="2027-08-10", arr="2027-08-12", pnr="HOT9"
    )[0]
    hotel.payload["address"] = "100 Example Street, Fargo, ND 00000"
    st.upsert_item(st.find_or_create(hotel), hotel)
    car = car_item(pickup="BOS", start="2027-10-12T09:00", end="2027-10-14T09:00", code="CAR9")
    car.payload["pickup_city"] = "Boston"
    st.upsert_item(st.find_or_create(car), car)
    html = c.get("/").text
    assert "Check-in Tue Aug 10" in html and "Pick-up Tue Oct 12 · 9:00 AM" in html
    assert "Departs" not in html


def test_remove_item_from_the_trip_page(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hdr = {"Sec-Fetch-Site": "same-origin"}
    car = st.item_by_key("car|CAR123")
    assert "Remove from trip" in c.get("/trips/10").text
    r = c.post(f"/items/{car.id}/remove", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == c.url("/trips/10?f=removed")
    assert st.item(car.id).removed_at and len(st.items(10)) == 3
    assert c.post(f"/items/{car.id}/remove", headers=hdr).status_code == 404
    assert c.post(f"/items/{st.items(10)[0].id}/remove").status_code == 403
    for ti in st.items(10):  # emptying the trip hides it and lands on the list
        r = c.post(f"/items/{ti.id}/remove", headers=hdr, follow_redirects=False)
    assert r.headers["location"] == c.url("/?f=removed") and st.get(10) is not None
    assert st.all_trips() == [] and "Philadelphia" not in c.get("/").text


def test_feed_rejects_a_non_ascii_token_with_a_bare_404(tmp_path, monkeypatch, base):
    from tripbox.trips import ical

    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    ical.feed_token(ctx.ledger)  # a real token exists, so the comparison itself runs
    r = c.get("/feed/t%C3%B6ken.ics", follow_redirects=False)
    assert r.status_code == 404 and r.content == b""


def test_hostile_strings_come_out_escaped(tmp_path, monkeypatch, base):
    client, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    evil = '<img src=x onerror="alert(1)">Philadelphia'
    ctx.trips.rename(10, evil)
    ctx.trips.set_notes(10, "<script>alert(2)</script> gate code")
    for path in ("/", "/trips/10", "/trips/10/share"):
        body = client.get(path).text
        assert "<img src=x" not in body and "<script>alert(2)" not in body
        assert "&lt;img src=x" in body or "&lt;script&gt;" in body or path == "/"


def test_garbage_ids_are_400_or_404_never_500(tmp_path, monkeypatch, base):
    client, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    assert client.get("/trips/99999999999999999999").status_code == 404
    assert client.get("/trips/10/text?items=²,abc").status_code == 200
    h = {"Sec-Fetch-Site": "same-origin"}
    assert client.post("/trips/10/merge", data={"into": "²"}, headers=h).status_code == 400
    assert (
        client.post(
            "/trips/10/merge", data={"into": "99999999999999999999"}, headers=h
        ).status_code
        == 400
    )
    item = ctx.trips.items(10)[0]
    assert (
        client.post(f"/items/{item.id}/move", data={"to": "²"}, headers=h).status_code
        == 400
    )


def test_two_chips_side_by_side_are_spaced_apart(tmp_path, monkeypatch, base):
    """A cancelled booking to check carries two chips on one line; the stylesheet keeps a gap
    between adjacent chips, so the two words never touch."""
    client, ctx = client_for(tmp_path, monkeypatch, base=base)
    css = client.get("/static/app.css").text
    rule = re.search(r"\.chip\s*\+\s*\.chip\s*\{([^}]*)\}", css)
    assert rule is not None and re.search(r"margin-left:\s*[1-9]", rule.group(1))


def test_pages_version_their_assets_refuse_framing_and_log_repairs(
    tmp_path, monkeypatch, base, caplog
):
    import logging

    client, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    r = client.get("/")
    assert "app.css?v=" in r.text and "app.js?v=" in r.text
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
    caplog.set_level(logging.INFO, logger="tripbox.web")
    r = client.post(
        "/trips/10/rename",
        data={"title": "Philadelphia trip"},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == client.url("/trips/10")
    assert any("rename" in rec.getMessage() and "10" in rec.getMessage() for rec in caplog.records)


def test_unknown_trip_is_an_html_404_with_a_way_back(tmp_path, monkeypatch, base):
    client, ctx = client_for(tmp_path, monkeypatch, base=base)
    r = client.get("/trips/999")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert f'href="{client.url("/")}"' in r.text
    if base:  # the bare base goes on to its slash
        r = client.get("", follow_redirects=False)
        assert r.status_code in (301, 307, 308) and r.headers["location"] == client.url("/")


def test_merge_and_move_selects_start_on_a_placeholder(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    seed_past(ctx, 1)
    html = c.get("/trips/10").text
    for name, label in (("into", "Choose a trip…"), ("to", "Move to…")):
        i = html.index(f'<select name="{name}" required')
        assert html.index(f'<option value="" disabled selected>{label}</option>', i) - i < 160


def test_merge_and_move_refuse_an_empty_target(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    hdr = {"Sec-Fetch-Site": "same-origin"}
    item = ctx.trips.items(10)[0]
    assert c.post("/trips/10/merge", data={"into": ""}, headers=hdr).status_code == 400
    assert c.post(f"/items/{item.id}/move", data={"to": ""}, headers=hdr).status_code == 400
    assert ctx.trips.item(item.id).trip_id == 10 and ctx.trips.get(10) is not None


def confirmations(html: str) -> list[str]:
    """Every sentence a page's forms ask to be confirmed (`data-confirm`), as read."""
    return [unescape(s) for s in re.findall(r'data-confirm="([^"]*)"', html)]


def test_the_confirmations_say_what_is_not_touched(tmp_path, monkeypatch, base):
    """Removing a booking or deleting a trip says what stays as it was: the saved emails, and
    the flight log only while one is kept; Recently deleted says the same of what it holds."""
    restore = "Restore it from Recently deleted within 30 days."
    for kept, said in (
        (True, "Your flight log and the saved emails are not touched."),
        (False, "The saved emails are not touched."),
    ):
        c, ctx = client_for(tmp_path / str(kept), monkeypatch, base=base, airtrail=kept)
        seed_trip(ctx)
        st = ctx.trips
        st.upsert_item(None, undated_stay(pnr="HOT9"))
        page = c.get("/trips/10").text
        assert f'action="{c.url("/trips/10/delete")}"' in page and "Delete trip" in page
        assert set(confirmations(page)) == {
            f"Delete this trip? {said} {restore}",
            f"Remove this booking from the trip? {said} {restore}",
        }, kept
        assert confirmations(unsorted_group(c.get("/").text)) == [
            f"Remove this booking? {said} {restore}"
        ]
        st.remove_item(st.items(10)[0].id)
        note = f"Everything here is removed for good 30 days after it was deleted. {said}"
        assert f'<p class="muted small">{note}</p>' in c.get("/deleted").text, kept


def test_delete_trip_then_restore_from_recently_deleted(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hdr = {"Sec-Fetch-Site": "same-origin"}
    r = c.post("/trips/10/delete", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == c.url("/?f=deleted")
    gone = c.get("/trips/10")
    assert gone.status_code == 404 and c.url("/deleted") in gone.text
    assert "Philadelphia" not in c.get("/").text
    listing = c.get("/deleted").text
    assert "Philadelphia" in listing and f'action="{c.url("/trips/10/restore")}"' in listing
    r = c.post("/trips/10/restore", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == c.url("/trips/10?f=restored")
    assert c.get("/trips/10").status_code == 200 and len(st.items(10)) == 4
    assert "Nothing here" in c.get("/deleted").text
    assert c.post("/trips/10/delete").status_code == 403  # cross-site
    assert c.post("/trips/999/delete", headers=hdr).status_code == 404


def test_remove_item_goes_to_recently_deleted_and_restores(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hdr = {"Sec-Fetch-Site": "same-origin"}
    hotel = st.item_by_key("lodging|HOTEL1")
    r = c.post(f"/items/{hotel.id}/remove", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == c.url("/trips/10?f=removed")
    assert "Recently deleted (1)" in c.get("/").text
    listing = c.get("/deleted").text
    assert "from Philadelphia" in listing and "Thu Sep 16" in listing
    assert f'action="{c.url(f"/items/{hotel.id}/restore")}"' in listing
    r = c.post(f"/items/{hotel.id}/restore", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == c.url("/trips/10?f=restored")
    assert st.item(hotel.id).removed_at is None and len(st.items(10)) == 4
    assert "Recently deleted" not in c.get("/").text


def test_restore_trip_that_has_no_live_rows_redirects_to_the_list(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hdr = {"Sec-Fetch-Site": "same-origin"}
    for ti in st.items(10):
        st.remove_item(ti.id)
    st.delete_trip(10)
    r = c.post("/trips/10/restore", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == c.url("/?f=restored")
    assert st.get(10) is not None and st.all_trips() == []


def test_list_footer_links_recently_deleted_only_when_non_empty(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    assert "Recently deleted" not in c.get("/").text
    st.delete_trip(10)
    assert "Recently deleted (1)" in c.get("/").text
    st.restore_trip(10)
    st.remove_item(st.items(10)[0].id)
    st.delete_trip(10)
    assert "Recently deleted (2)" in c.get("/").text


def test_flash_renders_as_toast(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    assert 'data-flash="Restored"' in c.get("/trips/10?f=restored").text
    assert "data-flash" not in c.get("/trips/10?f=bogus").text
    assert "data-flash" not in c.get("/trips/10").text
    assert 'data-flash="Trip deleted' in c.get("/?f=deleted").text


def test_restore_of_a_live_item_or_trip_is_404(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    hdr = {"Sec-Fetch-Site": "same-origin"}
    item = ctx.trips.items(10)[0]
    assert c.post("/trips/10/restore", headers=hdr).status_code == 404
    assert c.post(f"/items/{item.id}/restore", headers=hdr).status_code == 404
    assert c.post("/items/999/restore", headers=hdr).status_code == 404


def test_stale_posts_on_a_removed_row_are_404(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hdr = {"Sec-Fetch-Site": "same-origin"}
    car = st.item_by_key("car|CAR123")
    st.remove_item(car.id)
    move = {"to": "new"}
    assert c.post(f"/items/{car.id}/move", data=move, headers=hdr).status_code == 404
    assert c.post(f"/items/{car.id}/remove", headers=hdr).status_code == 404
    live = st.items(10)[0]
    st.delete_trip(10)
    assert c.post(f"/items/{live.id}/move", data=move, headers=hdr).status_code == 404
    assert c.post(f"/items/{live.id}/remove", headers=hdr).status_code == 404
    assert st.item(live.id).removed_at is None and st.all_trips() == []


def test_status_toggle_from_the_item_menu(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hdr = {"Sec-Fetch-Site": "same-origin"}
    car = st.item_by_key("car|CAR123")
    url = f"/items/{car.id}/status"
    page = c.get("/trips/10").text
    assert f'action="{c.url(url)}"' in page
    assert "Mark cancelled" in page and "Mark confirmed" not in page
    r = c.post(url, data={"status": "cancelled"}, headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == c.url(f"/trips/10#item-{car.id}")
    assert st.item(car.id).status == "cancelled"
    page = c.get("/trips/10").text
    assert "Mark confirmed" in page and 'class="chip chip-red">Cancelled' in page
    share = c.get("/trips/10/share").text
    assert f'value="{car.id}" data-kind="car">' in share  # unchecked by default now
    r = c.post(url, data={"status": "confirmed"}, headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and st.item(car.id).status == "confirmed"
    assert c.post(url, data={"status": "bogus"}, headers=hdr).status_code == 400
    st.remove_item(car.id)
    assert c.post(url, data={"status": "cancelled"}, headers=hdr).status_code == 404


# What a page of an install that keeps no flight log, reads no mailbox, has no model and finds
# no photo (automatic covers off) never names, and the promise of a number of minutes. Where
# photos come from is named only while automatic covers are on, or in the credit line of a photo
# found before they were turned off (`test_web_covers`).
NOT_HERE = (
    "flight log",
    "airtrail",
    "telegram",
    "wikipedia",
    "wikimedia",
    "finds a photo",
    "mailbox",
)
MINUTES = re.compile(r"\b\d+\s*(?:minutes?|mins?)\b", re.IGNORECASE)
STATUS_CARD = re.compile(r'<dl class="status-card">.*?</dl>', re.S)


def without_the_cards_labels(html: str) -> str:
    """The page with the status card's labels taken out: the one place a page names a feature
    the install lacks, beside the sentence that says it is off."""
    return STATUS_CARD.sub(lambda card: re.sub(r"<dt>.*?</dt>", "", card.group(0)), html)


def test_no_page_names_what_the_install_does_not_have(tmp_path, monkeypatch, base):
    """With no flight log, no mail, no model and no automatic covers, no page, no confirmation a
    page asks and no toast names a flight log, AirTrail, Telegram, Wikipedia or Wikimedia, says a
    place finds a photo, names a mailbox, or promises a number of minutes. The one exception is
    the status card's labels: a first run's list shows the card, whose row for each feature names
    it to say it is off; its sentences, like every other part of every page, name nothing the
    install lacks."""
    from tripbox import worker
    from tripbox.web import card

    c, ctx = client_for(tmp_path, monkeypatch, airtrail=False, base=base)
    s = ctx.settings
    assert not any(s.on(f) for f in ("mail", "model", "airtrail", "notifications"))
    pages = {"the empty list": c.get("/")}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: None)
    with util.clock_at(util.now()):
        worker.Worker(ctx).start()  # its record says what is on
        pages["the first run"] = c.get("/")
    (shown,) = STATUS_CARD.findall(pages["the first run"].text)
    assert re.findall(r"<dt>(.*?)</dt>", shown) == ["Worker", "Mail", "Model", "Flight log"]
    sentences = [" ".join(unescape(t).split()) for t in re.findall(r"<dd[^>]*>(.*?)</dd>", shown)]
    assert sentences == [card.RUNNING, card.OFF["mail"], card.OFF["model"], card.OFF["airtrail"]]
    seed_trip(ctx)
    st = ctx.trips
    undated = st.upsert_item(None, undated_stay(pnr="HOT9"))
    hdr = {"Sec-Fetch-Site": "same-origin"}
    landed = c.post("/add/paste", data={"text": "A confirmation."}, headers=hdr)
    assert landed.status_code == 200 and "/uploads/" in str(landed.url)
    pages["an upload"] = landed
    st.remove_item(st.item_by_key("car|CAR123").id)
    for path in (
        "/",
        "/?past=all",
        "/deleted",
        "/trips/10",
        "/trips/10/share",
        "/trips/999",
        "/add",
        "/add/paste",
        "/add/upload",
        *(f"/add/{word}" for word in views.ADD_KIND),
        *(f"/items/{ti.id}/edit" for ti in [*st.items(10), undated]),
        *(f"/?f={key}" for key in views.FLASH),
    ):
        pages[path] = c.get(path)
    for name, r in pages.items():
        assert r.status_code in (200, 404), name
        said = unescape(without_the_cards_labels(r.text))
        for word in NOT_HERE:
            assert word not in said.lower(), (name, word)
        assert not MINUTES.search(said), (name, MINUTES.search(said))
    for sentence in views.FLASH.values():
        assert not any(word in sentence.lower() for word in NOT_HERE), sentence


def test_the_flight_log_link_needs_a_flight_log_and_its_address(tmp_path, monkeypatch, base):
    """The list links to the flight log only while one is kept and its address is set, and the
    link goes to that address; a value that is no web address links nothing."""
    for n, (kept, address, linked) in enumerate(
        (
            (True, FLIGHT_LOG_ADDRESS + "/", True),
            (True, "", False),
            (False, FLIGHT_LOG_ADDRESS, False),
            (True, "flightlog.example", False),
            (True, "javascript:alert(1)", False),
        )
    ):
        env = {"TRIPBOX_AIRTRAIL_PUBLIC_URL": address}
        c, ctx = client_for(tmp_path / str(n), monkeypatch, base=base, airtrail=kept, env=env)
        html = c.get("/").text
        links = re.findall(r'<a class="top-link" href="([^"]*)">Flight log</a>', html)
        assert links == ([FLIGHT_LOG_ADDRESS] if linked else []), (kept, address)
        assert ("Flight log" in html) is linked, (kept, address)
        # a bad address is a problem of the link alone: it leaves no feature invalid
        assert "invalid" not in {f.state for f in ctx.settings.features}, (kept, address)


def test_saving_a_place_keeps_the_tile_until_a_photo_is_ready(tmp_path, monkeypatch, base):
    """The trip's ⋯ sheet sets the city the trip is about, which bookings there on nearby dates
    join; the trip keeps its title, and with automatic covers off (the default) nothing is
    looked up: with no photo the page shows its coloured tile and no image, and only once a
    photo is ready (the owner's, here) an image in the tile's place, never an inline style. The
    photo lives in its own table, never on the trip."""
    from test_web_covers import ready  # it imports this module

    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hdr = {"Sec-Fetch-Site": "same-origin"}
    tile = f'class="cover big tile tile-{10 % 8}"'
    page = c.get("/trips/10").text
    form = page[page.index(f'action="{c.url("/trips/10/place")}"') :]
    form = form[: form.index("</form>")]
    assert 'name="place" value="Philadelphia"' in form and 'aria-label="Place"' in form
    assert 'aria-describedby="place-help"' in form and ">Save place</button>" in form
    help_text = "The city this trip is about. Bookings there on nearby dates join this trip."
    assert f'id="place-help">{help_text}</p>' in page
    url = "/trips/10/place"
    r = c.post(url, data={"place": " King   of Prussia "}, headers=hdr, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, c.url("/trips/10?f=place"))
    t = st.get(10)
    assert (t.place, t.title) == ("King of Prussia", "Philadelphia")
    shown = c.get("/trips/10?f=place").text
    assert 'data-flash="Place saved"' in shown and tile in shown
    assert "<img" not in shown and " style=" not in shown
    assert ctx.ledger.covers.all_for_lookup() == []  # nothing was looked up
    r = c.post(url, data={"place": "   "}, headers=hdr, follow_redirects=False)
    assert r.headers["location"] == c.url("/trips/10?f=place") and st.get(10).place is None
    assert tile in c.get("/trips/10").text
    assert c.post("/trips/999/place", data={"place": "X"}, headers=hdr).status_code == 404
    assert c.post("/trips/10/cover", data={"place": "X"}, headers=hdr).status_code in (404, 405)
    assert st.get(10).place is None
    assert not [f.name for f in dataclasses.fields(t) if "cover" in f.name]
    digest = ready(ctx, 10)
    shown = c.get("/trips/10").text
    assert tile not in shown and 'class="cover big photo"' in shown
    assert f'<img src="{c.url(f"/covers/{digest}.jpg")}" alt=""' in shown
    assert " style=" not in shown


def test_edit_page_renders_prefilled_fields_for_hotel_and_car(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hotel, car = st.item_by_key("lodging|HOTEL1"), st.item_by_key("car|CAR123")
    page = c.get("/trips/10").text
    assert f'href="{c.url(f"/items/{hotel.id}/edit")}" data-edit="lodging"' in page
    assert f'href="{c.url(f"/items/{car.id}/edit")}" data-edit="car"' in page
    h = c.get(f"/items/{hotel.id}/edit")
    assert h.status_code == 200
    html = h.text
    assert 'name="property_name" value="Hilton Example"' in html
    assert 'name="address" value="100 Example Street, Philadelphia, PA 00000"' in html
    assert 'name="check_in_date" value="2027-09-16"' in html
    assert 'name="check_in_time" value=""' in html
    assert 'name="check_out_date" value="2027-09-21"' in html
    assert 'name="confirmation_code" value="HOTEL1"' in html
    cr = c.get(f"/items/{car.id}/edit").text
    assert 'name="provider" value="Example Rent A Car"' in cr
    assert (
        'name="pickup_date" value="2027-09-16"' in cr and 'name="pickup_time" value="14:45"' in cr
    )
    assert 'name="dropoff_date" value="2027-09-21"' in cr
    assert 'name="dropoff_time" value="13:15"' in cr
    st.remove_item(car.id)
    assert c.get(f"/items/{car.id}/edit").status_code == 404


HDR = {"Sec-Fetch-Site": "same-origin"}


def shown_form(c, item_id: int) -> dict[str, str]:
    """The edit form of booking `item_id` as the page shows it: each field's value by name, the
    hidden ones included."""
    page = c.get(f"/items/{item_id}/edit").text
    form = {
        m["name"]: unescape(m["value"])
        for m in re.finditer(r'<input [^>]*name="(?P<name>[^"]+)" value="(?P<value>[^"]*)"', page)
    }
    for m in re.finditer(r'<textarea [^>]*name="(?P<name>[^"]+)"[^>]*>(?P<value>[^<]*)<', page):
        form[m["name"]] = unescape(m["value"])
    return form


def save(c, item_id: int, changes: dict, form: dict | None = None):
    """The edit form of booking `item_id` posted as the page showed it (`form`, else as it shows
    it now), with `changes` typed in."""
    data = {**(shown_form(c, item_id) if form is None else form), **changes}
    return c.post(f"/items/{item_id}/edit", data=data, headers=HDR, follow_redirects=False)


def test_edit_post_saves_and_redirects(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hotel = st.item_by_key("lodging|HOTEL1")
    form = {
        "property_name": "Hilton Example Riverside",
        "address": "300 Example Road, Philadelphia, PA 00000",
        "check_in_date": "2027-09-16",
        "check_in_time": "15:00",
        "check_out_date": "2027-09-22",
        "check_out_time": "",
        "confirmation_code": "HOTEL1",
    }
    r = save(c, hotel.id, form)
    assert r.status_code == 303
    assert r.headers["location"] == c.url(f"/trips/10?f=saved#item-{hotel.id}")
    ti = st.item(hotel.id)
    assert (ti.start_at, ti.end_at) == ("2027-09-16T15:00", "2027-09-22")
    assert st.get(10).end_date == "2027-09-22"
    page = c.get("/trips/10").text
    assert "Hilton Example Riverside" in page and "300 Example Road" in page and "3:00 PM" in page
    car = st.item_by_key("car|CAR123")
    form = {
        "provider": "Hertz",
        "pickup_location": "Philadelphia Intl Airport",
        "dropoff_location": "",
        "pickup_date": "2027-09-16",
        "pickup_time": "18:00",
        "dropoff_date": "2027-09-21",
        "dropoff_time": "16:00",
        "confirmation_code": "CAR123",
    }
    r = save(c, car.id, form)
    assert r.status_code == 303 and st.item(car.id).payload["provider"] == "Hertz"
    assert st.item(car.id).start_at == "2027-09-16T18:00"
    assert c.post(f"/items/{car.id}/edit", data=shown_form(c, car.id)).status_code == 403


def test_edit_post_with_bad_dates_rerenders_with_the_error(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    hotel = st.item_by_key("lodging|HOTEL1")
    form = {
        "property_name": "Hilton Example Riverside",
        "address": "x",
        "check_in_date": "2027-09-21",
        "check_in_time": "",
        "check_out_date": "2027-09-16",
        "check_out_time": "",
        "confirmation_code": "HOTEL1",
    }
    r = save(c, hotel.id, form)
    assert r.status_code == 400 and "check-out is before check-in" in r.text
    assert 'name="property_name" value="Hilton Example Riverside"' in r.text  # what was typed stays
    assert tied(r.text, "check_out_date") == ["check_out_date"]  # the refusal is about it
    assert st.item(hotel.id).payload["property_name"] == "Hilton Example"
    r = save(c, hotel.id, {"check_in_date": "soon"})
    assert r.status_code == 400 and "check-in is not a date" in r.text
    assert tied(r.text, "check_in_date") == ["check_in_date"]


REFUSED = '<p class="error" role="alert" id="edit-error">'


def tied(page: str, name: str) -> list[str]:
    """The fields of an edit page the refusal it shows is tied to, after checking that the field
    `name` is one of them and is marked invalid."""
    assert REFUSED in page
    field = re.search(rf'<input [^>]*name="{name}"[^>]*>', page).group(0)
    assert 'aria-invalid="true"' in field and "edit-error" in field, field
    return re.findall(r'name="([^"]+)"[^>]* aria-describedby="[^"]*edit-error', page)


def a_train(ctx):
    """The corpus's first train with no finding, filed into a trip of its own."""
    entry, res = booking("train")
    train = validated(entry, res)
    return ctx.trips.upsert_item(ctx.trips.find_or_create(train), train)


def test_edit_page_renders_prefilled_fields_for_a_flight_and_a_train(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    page = c.get("/trips/10").text
    for ti in st.items(10):  # every kind is edited on the page
        assert f'href="{c.url(f"/items/{ti.id}/edit")}" data-edit="{ti.kind}"' in page
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    html = c.get(f"/items/{out.id}/edit").text
    assert "<h1>Edit flight</h1>" in html and "Times are local to each airport." in html
    for name, value in (
        ("carrier", "SY"),
        ("flight_number", "9304"),
        ("origin", "MSP"),
        ("destination", "PHL"),
        ("departs_date", "2027-09-16"),
        ("departs_time", "10:25"),
        ("arrives_date", "2027-09-16"),
        ("arrives_time", "13:58"),
        ("confirmation_code", "QTWZSV"),
    ):
        assert f'name="{name}" value="{value}"' in html, name
    assert ">Alex Sample\nSam Sample\nRobin Sample</textarea>" in html
    train = a_train(ctx)
    p = train.payload
    html = c.get(f"/items/{train.id}/edit").text
    assert "<h1>Edit train</h1>" in html
    leaves, lands = grouping.local_str(p["dep_local"]), grouping.local_str(p["arr_local"])
    for name, value in (
        ("provider", p["provider"]),
        ("train_number", p["train_number"]),
        ("origin", p["origin"]),
        ("destination", p["destination"]),
        ("departs_date", leaves[:10]),
        ("departs_time", leaves[11:]),
        ("arrives_date", lands[:10]),
        ("arrives_time", lands[11:]),
        ("confirmation_code", p["confirmation_code"]),
    ):
        assert f'name="{name}" value="{escape(value)}"' in html, name
    assert f">{escape(chr(10).join(train.passengers))}</textarea>" in html


def test_a_flight_edit_saves_and_returns_to_the_item(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    r = save(c, out.id, {"arrives_time": "14:30", "passengers": "Alex Sample\r\nSam Sample\r\n"})
    assert r.status_code == 303
    assert r.headers["location"] == c.url(f"/trips/10?f=saved#item-{out.id}")
    ti = st.item(out.id)
    assert ti.payload["arr_local"] == "2027-09-16T14:30" and ti.end_at == "2027-09-16T14:30"
    assert ti.passengers == ti.payload["passengers"] == ["Alex Sample", "Sam Sample"]
    assert set(ti.payload["owner_edits"]) == {"arr_local", "passengers"}
    assert ti.item_key == out.item_key and ti.checks == ()
    assert "arrives 2:30 PM EDT" in c.get("/trips/10").text
    # saved with no departure, it waits in the list's unsorted group
    r = save(c, out.id, {"departs_date": "", "departs_time": ""})
    assert (r.status_code, r.headers["location"]) == (303, c.url("/?f=saved#unsorted"))
    assert [u.id for u in st.unsorted()] == [out.id] and "departure" in st.item(out.id).checks


def test_the_form_lists_open_checks_and_ties_each_hard_one_to_its_field(
    tmp_path, monkeypatch, base
):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    # a city where its arrival airport belongs, no times and nobody named
    odd = st.upsert_item(None, flight(dest="Philadelphia", dep="", arr="", names=[]))
    assert odd.checks == ("destination", "departure", "owner_unknown")
    html = c.get(f"/items/{odd.id}/edit").text
    listed = html.index('<ul class="checks">')
    assert listed < html.index('name="carrier"')  # above the fields
    for code in odd.checks:
        assert f'<li id="check-{code}">{escape(checks.LABELS[code])}</li>' in html, code
    for name, code in (("destination", "destination"), ("departs_date", "departure")):
        tied = rf'name="{name}"[^>]* aria-invalid="true" aria-describedby="check-{code}"'
        assert re.search(tied, html), name
    assert html.count('aria-invalid="true"') == 2  # nobody named is about no one field
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    assert 'class="checks"' not in c.get(f"/items/{out.id}/edit").text


def test_fixing_a_draft_into_an_existing_flight_leaves_one_booking(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    ctx.syncer.run_pass()
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    assert out.sync_state == "synced"
    draft = st.upsert_item(st.get(10), flight(number=""))  # its flight number was not read
    assert is_draft(draft.item_key) and draft.sync_state == "blocked"
    r = save(c, draft.id, {"flight_number": out.payload["flight_number"]})
    assert r.status_code == 303
    assert r.headers["location"] == c.url(f"/trips/10?f=saved#item-{out.id}")
    assert st.item(draft.id) is None and draft.item_key in st.item(out.id).payload["alt_keys"]
    flights = [ti.id for ti in st.items(10) if ti.kind == "flight"]
    assert out.id in flights and len(flights) == 2
    ctx.syncer.run_pass()
    assert st.item(out.id).sync_state == "synced"
    assert [r[0] for r in ctx.airtrail.sent] == ["create", "create"]  # the two seeded legs


def test_a_refused_edit_keeps_what_was_typed_and_says_why(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    back = st.item_by_key("DL|9246|2027-09-21|PHL")
    before = dataclasses.asdict(back)
    theirs = shown_form(c, out.id)
    typed = {
        k: theirs[k] for k in ("carrier", "flight_number", "origin", "departs_date", "departs_time")
    }
    r = save(c, back.id, typed)
    assert r.status_code == 400
    assert f"{REFUSED}another booking already has this flight</p>" in r.text
    assert "edit-error" not in r.text.split(REFUSED)[1]  # about the booking, not one field
    for name, value in typed.items():
        assert f'name="{name}" value="{value}"' in r.text, name
    assert dataclasses.asdict(st.item(back.id)) == before
    for name, value, sentence in (
        ("departs_date", "soon", "departure is not a date"),
        ("flight_number", "soon", "Flight number is not a number"),
    ):
        r = save(c, back.id, {name: value})
        assert r.status_code == 400 and f'name="{name}" value="{value}"' in r.text
        assert f"{REFUSED}{sentence}</p>" in r.text
        assert tied(r.text, name) == [name]
        assert dataclasses.asdict(st.item(back.id)) == before


class Controls(HTMLParser):
    """Every input, select and textarea of a page but the hidden ones, and those of them that
    sit in no label and carry no aria-label."""

    def __init__(self) -> None:
        super().__init__()
        self.labels = 0
        self.found: list[str] = []
        self.unlabelled: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.labels += 1
        elif tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.found.append(a.get("name") or tag)
            if not self.labels and not a.get("aria-label"):
                self.unlabelled.append(a.get("name") or tag)

    def handle_endtag(self, tag):
        if tag == "label":
            self.labels -= 1


def test_every_control_of_the_edit_form_has_a_label(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    kinds = {ti.kind: ti.id for ti in [*st.items(10), a_train(ctx)]}
    assert set(kinds) == {"flight", "lodging", "car", "train"}
    for kind, item_id in kinds.items():
        page = Controls()
        page.feed(c.get(f"/items/{item_id}/edit").text)
        assert len(page.found) >= 6 and page.unlabelled == [], kind


# Elements that have no end tag
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: list, parent: Node | None) -> None:
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children: list[Node | str] = []

    def text(self) -> str:
        said = "".join(c if isinstance(c, str) else c.text() for c in self.children)
        return " ".join(said.split())

    def ancestors(self) -> list[Node]:
        out, at = [], self.parent
        while at is not None:
            out.append(at)
            at = at.parent
        return out

    def __str__(self) -> str:
        a = self.attrs
        said = self.tag + (f"#{a['id']}" if a.get("id") else "")
        return said + (f'[name="{a["name"]}"]' if a.get("name") else "")


class Tree(HTMLParser):
    """A page as a tree of elements (`nodes`, in page order), enough to work out what names a
    control."""

    def __init__(self, page: str) -> None:
        super().__init__()
        self.root = Node("#document", [], None)
        self.at = self.root
        self.nodes: list[Node] = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.at)
        self.at.children.append(node)
        self.nodes.append(node)
        if tag not in VOID:
            self.at = node

    def handle_endtag(self, tag):
        at = self.at
        while at is not self.root and at.tag != tag:
            at = at.parent
        if at is not self.root:
            self.at = at.parent

    def handle_data(self, data):
        self.at.children.append(data)

    def by_id(self, ident: str) -> Node | None:
        return next((n for n in self.nodes if n.attrs.get("id") == ident), None)

    def name_of(self, node: Node) -> str:
        """The control's accessible name, as a browser works it out for these elements:
        aria-labelledby, aria-label, then its labels (a form control) or its own text (a button,
        a summary or a link), its title, and for a field its placeholder."""
        a = node.attrs
        referred = [self.by_id(i) for i in (a.get("aria-labelledby") or "").split()]
        said = " ".join(n.text() for n in referred if n is not None).strip()
        if said or (a.get("aria-label") or "").strip():
            return said or a["aria-label"].strip()
        if node.tag in ("input", "select", "textarea"):
            labels = [n for n in node.ancestors() if n.tag == "label"]
            if a.get("id"):
                labels += [
                    n for n in self.nodes if n.tag == "label" and n.attrs.get("for") == a["id"]
                ]
            said = " ".join(n.text() for n in labels).strip()
            if not said and node.tag == "input" and a.get("type") in ("submit", "button"):
                said = (a.get("value") or "").strip()
        else:
            said = node.text()
        if said:
            return said
        if node.tag in ("input", "textarea") and not (a.get("title") or "").strip():
            return (a.get("placeholder") or "").strip()
        return (a.get("title") or "").strip()

    def unnamed(self) -> list[str]:
        """Every input (but a hidden one), select, textarea, button, summary and link that is
        not hidden and has no name, by its tag, id and name."""
        out = []
        for node in self.nodes:
            a = node.attrs
            if node.tag not in ("input", "select", "textarea", "button", "summary", "a"):
                continue
            if (node.tag == "a" and "href" not in a) or a.get("type") == "hidden":
                continue
            if any("hidden" in n.attrs for n in [node, *node.ancestors()]):
                continue
            if not self.name_of(node):
                out.append(str(node))
        return out


def test_every_control_outside_the_edit_form_has_a_name(tmp_path, monkeypatch, base):
    """Every page of a seeded ledger but the booking form (whose fields its own test holds to
    their labels): every control has an accessible name, and no two elements share an id."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = marked_trip(ctx)
    seed_past(ctx, 1)
    st = ctx.trips
    st.upsert_item(None, undated_stay(pnr="HOT9"))
    other = next(t for t in st.all_trips() if t.id != trip.id)
    st.remove_item(st.item_by_key("car|CAR123").id)
    st.delete_trip(other.id)
    covers = CoverStore(ctx.ledger)
    taken = covers.upload(trip.id, b"\xff\xd8\xff cut short")
    covers.fail_upload(taken, "unreadable")  # its note holds a second upload form
    landed = c.post("/add/paste", data={"text": "A confirmation."}, headers=HDR)
    pages = {"an upload": landed.text}
    for path in (
        "/",
        "/?past=all",
        "/?f=saved",
        "/deleted",
        f"/trips/{trip.id}",
        f"/trips/{trip.id}/share",
        "/trips/999",
        "/add",
        "/add/paste",
        "/add/upload",
    ):
        r = c.get(path)
        assert r.status_code in (200, 404), path
        pages[path] = r.text
    locked, _ = client_for(
        tmp_path / "locked",
        monkeypatch,
        base=base,
        auth="password",
        env={"TRIPBOX_PASSWORD": "a-page-test-password"},
    )
    pages["the login page"] = locked.get("/login").text
    for name, page in pages.items():
        tree = Tree(page)
        assert tree.unnamed() == [], name
        ids = [n.attrs["id"] for n in tree.nodes if "id" in n.attrs]
        assert len(ids) == len(set(ids)), (name, sorted(i for i in ids if ids.count(i) > 1))
    controls = Tree(pages[f"/trips/{trip.id}"]).nodes
    assert sum(n.tag in ("input", "select", "textarea", "button", "summary") for n in controls) > 20


def test_a_trips_tile_letter_and_photo_are_no_part_of_its_name(tmp_path, monkeypatch, base):
    """The tile's initial and the photo in its place, on a trip's card, its page's hero and its
    row in Recently deleted, are decoration: hidden from a screen reader (an image with no text
    of its own, inside the hidden cover), so a card's link is named by its title first."""
    from test_web_covers import ready  # it imports this module

    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 2)
    st = ctx.trips
    st.delete_trip(next(t for t in st.all_trips() if t.id != trip.id).id)
    for photo in (False, True):
        if photo:
            ready(ctx, trip.id)
        seen = images = 0
        for path in ("/", f"/trips/{trip.id}", "/deleted"):
            tree = Tree(c.get(path).text)
            covers = [n for n in tree.nodes if "cover" in n.attrs.get("class", "").split()]
            assert covers and all(n.attrs.get("aria-hidden") == "true" for n in covers), path
            seen += len(covers)
            for img in (n for n in tree.nodes if n.tag == "img"):
                assert img.attrs.get("alt") == "", (path, img.attrs)
                assert any(n in covers for n in img.ancestors()), (path, img.attrs)
                images += 1
        assert seen >= 4 and images == (2 if photo else 0), photo
        link = c.url(f"/trips/{trip.id}")
        card = next(n for n in Tree(c.get("/").text).nodes if n.attrs.get("href") == link)
        assert trip.title in card.text(), photo


def described_by(tree: Tree, node: Node) -> str:
    """The text of what the node's aria-describedby points at ("" when it points at nothing)."""
    found = [tree.by_id(i) for i in (node.attrs.get("aria-describedby") or "").split()]
    return " ".join(n.text() for n in found if n is not None)


def test_repeated_controls_say_what_they_act_on(tmp_path, monkeypatch, base):
    """A control repeated per row says which row it acts on: each booking's ⋯ menu (on a trip
    and in Unsorted) and each Restore is described by its row's title; the edit form's second
    field of each pair is named by what it is, not `Time`; the share page's kind chips say
    which one is pressed, not by colour alone."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 1)
    st = ctx.trips
    st.upsert_item(None, undated_stay(pnr="HOT9"))
    pages = {p: Tree(c.get(p).text) for p in ("/", f"/trips/{trip.id}")}
    for path, tree in pages.items():
        menus = [n for n in tree.nodes if n.attrs.get("aria-label") == "More for this item"]
        assert menus, path
        for menu in menus:
            assert described_by(tree, menu), (path, str(menu))
        if path != "/":
            said = [described_by(tree, n) for n in menus]
            assert len(menus) > 2 and len(set(said)) > 2, said
    # Recently deleted: one trip and two bookings, each Restore described by its own row
    st.remove_item(st.item_by_key("car|CAR123").id)
    st.remove_item(st.item_by_key("lodging|HOTEL1").id)
    st.delete_trip(next(t for t in st.all_trips() if t.id != trip.id).id)
    tree = Tree(c.get("/deleted").text)
    restores = [n for n in tree.nodes if n.tag == "button" and n.text() == "Restore"]
    said = [described_by(tree, n) for n in restores]
    assert len(restores) == 3 and all(said) and len(set(said)) == 3, said
    # the edit form: no two fields named alike, a time named by what it times
    timed = {
        "flight": {"departs_time": "Departure time", "arrives_time": "Arrival time"},
        "lodging": {"check_in_time": "Check-in time", "check_out_time": "Check-out time"},
        "car": {"pickup_time": "Pick-up time", "dropoff_time": "Drop-off time"},
        "train": {"departs_time": "Departure time", "arrives_time": "Arrival time"},
    }
    for kind, path in (
        ("flight", "/add/flight"),
        ("lodging", "/add/hotel"),
        ("car", "/add/car"),
        ("train", "/add/train"),
    ):
        form = Tree(c.get(path).text)
        fields = [
            n
            for n in form.nodes
            if n.tag in ("input", "select", "textarea") and n.attrs.get("type") != "hidden"
        ]
        names = {n.attrs.get("name"): form.name_of(n) for n in fields}
        assert {k: names[k] for k in timed[kind]} == timed[kind], kind
        shown = [form.name_of(n) for n in fields]
        assert len(shown) == len(set(shown)), (kind, shown)
        visible = [n for n in form.nodes if n.tag == "span" and n.text() == "Time"]
        assert len(visible) == 2, kind  # the label a sighted reader sees is unchanged
    # the share page's chips: pressed or not, in words a screen reader says
    tree = Tree(c.get(f"/trips/{trip.id}/share").text)
    chips = [n for n in tree.nodes if "chip-btn" in n.attrs.get("class", "").split()]
    assert len(chips) > 1
    assert [(n.attrs.get("data-kind"), n.attrs.get("aria-pressed")) for n in chips] == [
        (n.attrs.get("data-kind"), "true" if n.attrs.get("data-kind") == "all" else "false")
        for n in chips
    ]
    script = (Path(tripbox.web.__file__).parent / "static" / "app.js").read_text()
    assert 'setAttribute("aria-pressed"' in script


def test_the_trip_sheets_fields_are_named_and_a_toast_is_announced(tmp_path, monkeypatch, base):
    """The trip's sheet names its fields (the trip's name, its place, the trip to merge it
    into), each booking's sheet its trip to move to, the notes field its notes; a stay's or a
    car's second line has an id of its own; the toast is a polite status; and a flight or a
    train added by hand says under its travellers that the owner may take their name off."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    seed_past(ctx, 1)
    st = ctx.trips
    tree = Tree(c.get("/trips/10?f=saved").text)
    for name, label in (
        ("title", "Trip name"),
        ("place", "Place"),
        ("into", "Merge into"),
        ("to", "Move to"),
        ("notes", "Notes"),
    ):
        fields = [
            n
            for n in tree.nodes
            if n.attrs.get("name") == name and n.tag in ("input", "select", "textarea")
        ]
        assert fields and {n.attrs.get("aria-label") for n in fields} == {label}, name
    toast = tree.by_id("toast")
    assert toast is not None and toast.attrs.get("data-flash") == "Saved"
    assert (toast.attrs.get("role"), toast.attrs.get("aria-live")) == ("status", "polite")
    for key in ("lodging|HOTEL1", "car|CAR123"):
        ti = st.item_by_key(key)
        start, end = tree.by_id(f"item-{ti.id}"), tree.by_id(f"item-{ti.id}-end")
        assert start is not None and end is not None and start is not end, key
    hint = "One name per line. Take your own name off if you are not travelling."
    for word in ("flight", "train"):
        form = Tree(c.get(f"/add/{word}").text)
        (field,) = [n for n in form.nodes if n.attrs.get("name") == "passengers"]
        described = form.by_id(field.attrs.get("aria-describedby", "").split()[0])
        assert described is not None and described.text() == hint, word
    assert hint not in c.get("/add/hotel").text
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    assert hint not in c.get(f"/items/{out.id}/edit").text  # editing, not adding


def test_a_save_from_a_form_the_booking_has_changed_since_is_refused(tmp_path, monkeypatch, base):
    """An email moves the departure while the form is open: the save stores nothing and shows
    the booking as it is now, so the owner's stale value never pins over the airline's. A form
    that does not say what it showed is refused the same way."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    form = shown_form(c, out.id)
    p = out.payload
    moved = flight(dep=an_hour_later(p["dep_local"]), arr=an_hour_later(p["arr_local"]))
    st.upsert_item(st.get(10), moved)
    changed = st.item(out.id)
    assert changed.payload["dep_local"] == moved.payload["dep_local"] != p["dep_local"]
    for sent in (form, {k: v for k, v in form.items() if k != "shown"}):
        r = save(c, out.id, {"passengers": "Alex Sample"}, sent)
        assert r.status_code == 409
        assert (
            f"{REFUSED}This booking changed since the page was loaded."
            " Check it and save again.</p>" in r.text
        )
        assert f'name="departs_time" value="{moved.payload["dep_local"][11:]}"' in r.text
        assert ">Alex Sample\nSam Sample\nRobin Sample</textarea>" in r.text
        assert dataclasses.asdict(st.item(out.id)) == dataclasses.asdict(changed)
        assert "owner_edits" not in changed.payload
    # the form shown again saves
    assert save(c, out.id, {"passengers": "Alex Sample"}).status_code == 303


def test_a_save_is_a_look_at_the_time_the_form_was_shown(tmp_path, monkeypatch, base):
    """Saving the form closes the soft checks it showed; one raised after the form was shown
    stays open. A time it carries that cannot be read saves the fields and stamps no look, and a
    time ahead of the save is read as the save's."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    hour = dt.timedelta(hours=1)
    shown = dt.datetime.now(dt.UTC).replace(microsecond=0) - 6 * hour
    assert st.mark(out.id, {"low_confidence": (shown - hour).isoformat()}).checks == (
        "low_confidence",
    )
    with util.clock_at(shown):
        form = shown_form(c, out.id)
    assert form["seen"] == str(int(shown.timestamp()))
    with util.clock_at(shown + hour):
        assert save(c, out.id, {}, form).status_code == 303
    looked = st.item(out.id)
    assert looked.checks == () and looked.payload["looked_at"] == shown.isoformat()
    # a finding raised while the form is open stays open after the save
    with util.clock_at(shown + 2 * hour):
        form = shown_form(c, out.id)
    st.mark(out.id, {"low_confidence": (shown + 2.5 * hour).isoformat()})
    with util.clock_at(shown + 3 * hour):
        assert save(c, out.id, {}, form).status_code == 303
    assert st.item(out.id).checks == ("low_confidence",)
    # a time that cannot be read: the fields are saved, nothing is looked at
    r = save(c, out.id, {"seen": "soon", "passengers": "Alex Sample"})
    assert r.status_code == 303 and st.item(out.id).passengers == ["Alex Sample"]
    assert st.item(out.id).checks == ("low_confidence",)
    # a time ahead of the save: the look is the save's
    ahead = str(int((shown + 9 * hour).timestamp()))
    with util.clock_at(shown + 4 * hour):
        assert save(c, out.id, {"seen": ahead}).status_code == 303
    final = st.item(out.id)
    assert final.checks == () and final.payload["looked_at"] == (shown + 4 * hour).isoformat()


POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self';"
    " connect-src 'self'; worker-src 'self'; manifest-src 'self'; form-action 'self';"
    " base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
)


def test_every_page_carries_a_content_security_policy(tmp_path, monkeypatch, base):
    """Every answer (a page, a static file, the 404 page, a refusal) forbids every outside
    source, every inline script and style, and being framed."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    answers = {
        path: c.get(path)
        for path in (
            "/",
            "/trips/10",
            "/trips/10/share",
            "/deleted",
            "/add",
            "/trips/999",
            "/healthz",
            "/static/app.css",
            "/static/fonts/PublicSans-Regular.woff2",
        )
    }
    answers["cross-site post"] = c.post("/trips/10/delete")
    assert answers["cross-site post"].status_code == 403
    for name, r in answers.items():
        assert r.headers["content-security-policy"] == POLICY, name
        assert r.headers["x-frame-options"] == "DENY", name
        assert r.headers["x-content-type-options"] == "nosniff", name
        assert r.headers["referrer-policy"] == "same-origin", name
        assert r.headers["x-robots-tag"] == "noindex, nofollow", name  # a private page


def test_no_template_carries_an_inline_style_or_script():
    """The policy runs no inline script and applies no inline style: no template holds a style
    attribute or element, a script element without a src, an event handler attribute or a
    javascript: address."""
    templates = sorted((WEB / "templates").glob("*.html"))
    assert len(templates) >= 10
    for path in templates:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=", text), path.name
        assert "<style" not in text, path.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path.name
        assert not re.search(r"\son[a-z]+\s*=", text), path.name
        assert "javascript:" not in text.lower(), path.name


# What a static file names that no browser is ever sent to: the icon's XML namespace, and the
# font's licence, which the page never loads
NOT_ASKED = {"favicon.svg": ('xmlns="http://www.w3.org/2000/svg"',), "fonts/OFL.txt": None}
# The links a page may hold to another site, each opened by a tap and never loaded by the page:
# a stay's or a car's place in Maps, a flight's tracker, and the flight log at its address. Only
# the list and a trip's page hold them.
OUTSIDE_LINKS = ("https://maps.apple.com/", "https://flightaware.com/")
FLIGHT_LOG_ADDRESS = "https://flightlog.example"
OUTSIDE_LINK_PAGES = ("/", "/trips/10")


class Named(HTMLParser):
    """Every address a page names, as (tag, attribute, value): each attribute value and each
    run of text that holds `://`."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, str, str]] = []
        self.tag = ""

    def handle_starttag(self, tag, attrs):
        self.tag = tag
        for name, value in attrs:
            if "://" in (value or ""):
                self.found.append((tag, name, value or ""))

    def handle_data(self, data):
        if "://" in data:
            self.found.append((self.tag, "", data.strip()))


def test_no_page_or_static_file_names_another_host(tmp_path, monkeypatch, base):
    """Nothing the page loads names another host: no template and no static text file holds an
    http:// or https:// address (but the icon's namespace and the font's licence, which no
    browser asks for), and the only addresses of another site on a page are the outside links,
    each a link to tap on the list or a trip's page; the page's own address (the calendar
    feed's) is the one other address a page holds."""
    templates = sorted((WEB / "templates").glob("*.html"))
    statics = [p for p in sorted((WEB / "static").rglob("*")) if p.suffix in TEXT_FILES]
    assert {p.name for p in statics} >= {"app.css", "app.js", "favicon.svg", "OFL.txt"}
    for path in [*templates, *statics]:
        text = path.read_text(encoding="utf-8")
        name = str(path.relative_to(WEB / "static")) if path in statics else path.name
        if name in NOT_ASKED and NOT_ASKED[name] is None:
            continue
        for allowed in NOT_ASKED.get(name) or ():
            text = text.replace(allowed, "")
        assert not re.search(r"(?i)https?://|//[a-z0-9.-]+\.[a-z]{2,}/", text), name
    env = {"TRIPBOX_AIRTRAIL_PUBLIC_URL": FLIGHT_LOG_ADDRESS}
    c, ctx = client_for(tmp_path, monkeypatch, base=base, env=env)
    seed_trip(ctx)
    st = ctx.trips
    undated = st.upsert_item(None, undated_stay(pnr="HOT9"))
    pages = [
        "/",
        "/?past=all",
        "/deleted",
        "/add",
        "/add/paste",
        "/add/upload",
        "/add/flight",
        "/trips/10",
        "/trips/10/share",
        "/trips/999",
        *(f"/items/{ti.id}/edit" for ti in [*st.items(10), undated]),
    ]
    own = ORIGIN + base + "/"
    held: set[str] = set()
    for path in pages:
        r = c.get(path)
        assert r.status_code in (200, 404), path
        found = Named()
        found.feed(r.text)
        for tag, attribute, value in found.found:
            if value.startswith(own):
                continue
            outside = (*OUTSIDE_LINKS, FLIGHT_LOG_ADDRESS)
            assert path.split("?")[0] in OUTSIDE_LINK_PAGES, (path, tag, attribute, value)
            assert (tag, attribute) == ("a", "href") and value.startswith(outside), (path, value)
            held.add(next(o for o in outside if value.startswith(o)))
    assert held == {*OUTSIDE_LINKS, FLIGHT_LOG_ADDRESS}


def test_only_the_read_pages_opt_into_pull_and_resume_refresh(tmp_path, monkeypatch, base):
    """A reload would wipe the share page's selections and the edit page's typing."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    hotel = ctx.trips.item_by_key("lodging|HOTEL1")
    for path in ("/", "/trips/10", "/deleted"):
        assert "<body data-refresh>" in c.get(path).text, path
    for path in ("/trips/10/share", f"/items/{hotel.id}/edit"):
        html = c.get(path).text
        assert "<body>" in html and "data-refresh" not in html, path


# ----------------------------------------------------------------------------- marks and answers
class Marks(HTMLParser):
    """What a page marks, booking by booking (each element `id="item-N"`, in `items`): its check
    box (`boxes`: the box's role and label, its sentences in `says`, and the text of each button
    and link in `controls`) and its line about the flight log (`sync`); the text of every amber
    chip on the page (`chips`); and the tags inside a check box that set a style (`styled`)."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[int] = []
        self.boxes: dict[int | None, dict] = {}
        self.sync: dict[int | None, str] = {}
        self.chips: list[str] = []
        self.styled: list[str] = []
        self.item: int | None = None
        self.box = 0  # how many divs are open inside a check box; 0 outside one
        self.reading: list[tuple[str, str, list[str]]] = []  # (what, its tag, its text so far)

    @classmethod
    def of(cls, page: str) -> Marks:
        found = cls()
        found.feed(page)
        return found

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        line = re.fullmatch(r"item-(\d+)(?:-end)?", a.get("id") or "")
        if line:  # a booking's line, or the second line of a stay or a car
            self.item = int(line.group(1))
            self.items.append(self.item)
        if self.box and "style" in a:
            self.styled.append(tag)
        if tag == "div" and self.box:
            self.box += 1
        elif tag == "div" and "check" in classes:
            self.box = 1
            self.boxes[self.item] = {
                "role": a.get("role"),
                "label": a.get("aria-label"),
                "says": [],
                "controls": [],
            }
        what = None
        if tag == "span" and "chip-amber" in classes:
            what = "chip"
        elif "sync" in classes:
            what = "sync"
        elif self.box and tag in ("li", "p"):
            what = "says"
        elif self.box and tag in ("button", "a"):
            what = "controls"
        if what:
            self.reading.append((what, tag, []))

    def handle_data(self, data):
        for _, _, text in self.reading:
            text.append(data)

    def handle_endtag(self, tag):
        if self.reading and self.reading[-1][1] == tag:
            what, _, text = self.reading.pop()
            said = " ".join("".join(text).split())
            if what == "chip":
                self.chips.append(said)
            elif what == "sync":
                self.sync[self.item] = said
            else:
                self.boxes[self.item][what].append(said)
        if tag == "div" and self.box:
            self.box -= 1


def unsorted_group(page: str) -> str:
    """The list page's Unsorted section."""
    start = page.index('<section id="unsorted"')
    return page[start : page.index("</section>", start)]


def act(c, item_id: int, what: str, data: dict | None = None):
    """The check box's form `what` of booking `item_id`, posted from the page."""
    return c.post(
        f"/items/{item_id}/{what}", data=data or {}, headers=HDR, follow_redirects=False
    )


def seen_on(c, page: str, item_id: int) -> str:
    """When the page was rendered, as its Looks right form for booking `item_id` says."""
    action = re.escape(c.url(f"/items/{item_id}/looks-right"))
    form = re.search(
        rf'action="{action}">\s*'
        r'<input type="hidden" name="seen" value="([0-9]+)">',
        page,
    )
    assert form, item_id
    return form.group(1)


def marked_trip(ctx):
    """The seeded trip with one booking of each kind of mark, once both legs are in the flight
    log: the hotel with a soft check, a flight that names nobody, a stay whose check-out is before
    its check-in (a hard check), and the way home deleted by hand in the flight log; the outbound
    leg and the car have none."""
    trip = seed_trip(ctx)
    st = ctx.trips
    ctx.syncer.run_pass()
    hotel = st.item_by_key("lodging|HOTEL1")
    st.mark(hotel.id, {"low_confidence": util.now_iso()})
    st.upsert_item(trip, items_for([])[0])
    (backwards,) = items_for(
        ["Alex Sample"], kind="lodging", dep="2027-09-21", arr="2027-09-16", pnr="HM1"
    )
    st.upsert_item(trip, backwards)
    st.sync_detached(st.item_by_key("DL|9246|2027-09-21|PHL").id)  # gone from the flight log
    return trip


def test_the_unsorted_group_shows_only_when_there_is_an_unsorted_booking(
    tmp_path, monkeypatch, base
):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    seed_trip(ctx)
    html = c.get("/").text
    assert 'id="unsorted"' not in html and "No date yet" not in html
    ti = ctx.trips.upsert_item(None, undated_stay(pnr="HOT9"))  # not the hotel's code
    html = c.get("/").text
    assert html.index('<section id="unsorted"') < html.index(">Upcoming<")
    group = unsorted_group(html)
    assert ">Unsorted<" in group and "Hilton Example" in group and "No date yet" in group
    assert f"<li>{escape(checks.LABELS['check_in'])}</li>" in group
    edit = f'<a class="btn primary" href="{c.url(f"/items/{ti.id}/edit")}">Add the date</a>'
    assert edit in group and Marks.of(group).chips == ["To check"]
    # its menu copies, cancels and removes it; it has no trip to move to
    assert "Copy this item" in group and f'action="{c.url(f"/items/{ti.id}/status")}"' in group
    assert f'action="{c.url(f"/items/{ti.id}/remove")}"' in group and "/move" not in group


def test_a_trips_card_and_page_count_its_bookings_to_check(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    seed_past(ctx, 1)  # a trip with nothing to check
    st = ctx.trips
    other = next(t for t in st.all_trips() if t.id != trip.id)
    st.mark(st.item_by_key("lodging|HOTEL1").id, {"low_confidence": util.now_iso()})
    st.upsert_item(trip, items_for([])[0])  # names nobody
    assert st.flag_counts() == {trip.id: 2}
    listing = c.get("/").text
    card = listing[listing.index(f'href="{c.url(f"/trips/{trip.id}")}"') :]
    assert '<span class="chip chip-amber">2 to check</span>' in card[: card.index("</a>")]
    assert Marks.of(listing).chips == ["2 to check"]  # the other trip's card has none
    page = c.get(f"/trips/{trip.id}").text
    hero = page[page.index('<section class="hero"') : page.index('<section class="notes">')]
    assert '<span class="chip chip-amber">2 to check</span>' in hero
    # and each marked booking says so once, on its first line
    assert sorted(Marks.of(page).chips) == ["2 to check", "To check", "To check"]
    assert Marks.of(c.get(f"/trips/{other.id}").text).chips == []


def test_each_action_is_offered_exactly_when_it_applies(tmp_path, monkeypatch, base):
    """Each booking's box offers the answers its notification's buttons offer, and Edit; a
    booking with nothing to say has none. With no flight log kept, nobody named marks nothing and
    a flight deleted there is not sent again."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = marked_trip(ctx)
    st = ctx.trips
    soft, unnamed, hard = "lodging|HOTEL1", items_for([])[0].key, "lodging|HM1"
    gone, plain = "DL|9246|2027-09-21|PHL", "SY|9304|2027-09-16|MSP"

    def offered(flight_log: bool) -> dict[str, list[str] | None]:
        """What each booking's box offers, by key (None: no box), after checking it against
        the buttons a notification gives the same booking."""
        page = Marks.of(c.get(f"/trips/{trip.id}").text)
        for ti in st.items(trip.id):
            keyboard = check_keyboard(ti, flight_log=flight_log) or {"inline_keyboard": []}
            said = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
            if ti.checks or said:
                assert page.boxes[ti.id]["controls"] == [*said, "Edit"], ti.item_key
            else:
                assert ti.id not in page.boxes, ti.item_key
        return {ti.item_key: page.boxes.get(ti.id, {}).get("controls") for ti in st.items(trip.id)}

    shown = offered(flight_log=True)
    assert shown[soft] == ["Looks right", "Edit"]
    assert shown[unnamed] == ["I was on this flight", "Not me", "Edit"]
    assert shown[hard] == ["Edit"]
    assert shown[gone] == ["Send again", "Edit"]
    assert shown[plain] is None and shown["car|CAR123"] is None
    # no flight log kept: what it alone explains is not open, and nothing is sent again
    ctx.ledger.kv.set("airtrail", "off")
    for ti in st.items(trip.id):
        st.settle(ti.id)
    assert st.item_by_key(gone).sync_hold == "deleted"
    shown = offered(flight_log=False)
    assert shown[soft] == ["Looks right", "Edit"] and shown[hard] == ["Edit"]
    assert shown[unnamed] is None and shown[gone] is None


def test_looks_right_clears_the_mark_and_comes_back_to_the_item(tmp_path, monkeypatch, base):
    """Looks right is a look at the time the page was rendered: what was raised after it stays
    open and the flight stays held back. A form that does not say when changes nothing."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    hour = dt.timedelta(hours=1)
    shown = dt.datetime.now(dt.UTC).replace(microsecond=0) - 6 * hour
    page = f"/trips/{trip.id}"
    st.mark(out.id, {"low_confidence": (shown - hour).isoformat()})
    with util.clock_at(shown):
        seen = seen_on(c, c.get(page).text, out.id)
    assert seen == str(int(shown.timestamp()))
    st.mark(out.id, {"low_confidence": (shown + hour / 2).isoformat()})  # raised after it
    with util.clock_at(shown + hour):
        r = act(c, out.id, "looks-right", {"seen": seen})
    assert (r.status_code, r.headers["location"]) == (303, c.url(f"{page}?f=stale#item-{out.id}"))
    held = st.item(out.id)
    assert (held.checks, held.sync_state) == (("low_confidence",), "blocked")
    assert 'data-flash="The page was out of date: check again"' in c.get(f"{page}?f=stale").text
    # a page rendered after it closes what it showed, and the toast says so
    with util.clock_at(shown + 2 * hour):
        seen = seen_on(c, c.get(page).text, out.id)
    with util.clock_at(shown + 3 * hour):
        r = act(c, out.id, "looks-right", {"seen": seen})
    assert (r.status_code, r.headers["location"]) == (303, c.url(f"{page}?f=looked#item-{out.id}"))
    looked = st.item(out.id)
    assert (looked.checks, looked.sync_state) == ((), "pending")
    assert looked.payload["looked_at"] == (shown + 2 * hour).isoformat()
    assert 'data-flash="Marked as right"' in c.get(f"{page}?f=looked").text
    # a form that does not say when the page was rendered stamps nothing
    st.mark(out.id, {"low_confidence": (shown + 4 * hour).isoformat()})
    before = dataclasses.asdict(st.item(out.id))
    for sent in ({}, {"seen": "soon"}, {"seen": ""}):
        r = act(c, out.id, "looks-right", sent)
        stale = c.url(f"{page}?f=stale#item-{out.id}")
        assert (r.status_code, r.headers["location"]) == (303, stale)
        assert dataclasses.asdict(st.item(out.id)) == before
    # a booking with no date comes back to the list's unsorted group
    stay = st.upsert_item(None, undated_stay(pnr="HOT9"))  # not the hotel's code
    st.mark(stay.id, {"low_confidence": shown.isoformat()})
    r = act(c, stay.id, "looks-right", {"seen": seen_on(c, c.get("/").text, stay.id)})
    assert (r.status_code, r.headers["location"]) == (303, c.url("/?f=looked#unsorted"))
    assert st.item(stay.id).checks == ("check_in",)  # a hard check closes only when fixed


def test_the_owners_two_answers_post_and_redirect(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    st = ctx.trips
    one, two = items_for([])[0], flight(names=[])
    yes = st.upsert_item(st.find_or_create(one), one)
    no = st.upsert_item(st.find_or_create(two), two)
    assert yes.checks == no.checks == ("owner_unknown",)
    before = dataclasses.asdict(st.item(no.id))
    assert act(c, no.id, "owner", {"on_it": "maybe"}).status_code == 400
    assert act(c, no.id, "owner").status_code == 400
    assert dataclasses.asdict(st.item(no.id)) == before
    r = act(c, yes.id, "owner", {"on_it": "yes"})
    back = f"/trips/{yes.trip_id}"
    yes_back = c.url(f"{back}?f=owner_yes#item-{yes.id}")
    assert (r.status_code, r.headers["location"]) == (303, yes_back)
    aboard = st.item(yes.id)
    assert ctx.book.me_name in aboard.passengers
    assert (aboard.checks, aboard.sync_state) == ((), "pending")
    assert 'data-flash="Added you to this flight"' in c.get(f"{back}?f=owner_yes").text
    r = act(c, no.id, "owner", {"on_it": "no"})
    back = f"/trips/{no.trip_id}"
    assert (r.status_code, r.headers["location"]) == (303, c.url(f"{back}?f=owner_no#item-{no.id}"))
    off = st.item(no.id)
    assert off.payload["owner_answer"] == "no" and (off.checks, off.sync_state) == ((), "off")
    assert 'data-flash="Noted: not your flight"' in c.get(f"{back}?f=owner_no").text
    # answered, nothing is left to answer: an answer from a page rendered before changes nothing
    before = dataclasses.asdict(st.item(yes.id))
    r = act(c, yes.id, "owner", {"on_it": "no"})
    stale = c.url(f"/trips/{yes.trip_id}?f=stale#item-{yes.id}")
    assert (r.status_code, r.headers["location"]) == (303, stale)
    assert dataclasses.asdict(st.item(yes.id)) == before


def test_an_answer_with_no_owner_named_says_so_and_changes_nothing(tmp_path, monkeypatch, base):
    (tmp_path / "config").mkdir()
    no_travellers = {"TRIPBOX_CONFIG_DIR": str(tmp_path / "config")}
    c, ctx = client_for(tmp_path, monkeypatch, base=base, env=no_travellers)  # a flight log kept
    assert ctx.settings.travellers.me_name == ""
    st = ctx.trips
    (unnamed,) = items_for([])
    ti = st.upsert_item(st.find_or_create(unnamed), unnamed)
    assert ti.checks == ("owner_unknown",)
    back = f"/trips/{ti.trip_id}"
    box = Marks.of(c.get(back).text).boxes[ti.id]
    assert box["controls"] == ["I was on this flight", "Not me", "Edit"]
    before = dataclasses.asdict(st.item(ti.id))
    r = act(c, ti.id, "owner", {"on_it": "yes"})
    noowner = c.url(f"{back}?f=noowner#item-{ti.id}")
    assert (r.status_code, r.headers["location"]) == (303, noowner)
    assert dataclasses.asdict(st.item(ti.id)) == before
    said = 'data-flash="No owner is named in travellers.yaml"'
    assert said in c.get(f"{back}?f=noowner").text


def test_send_again_is_offered_for_a_flight_deleted_in_the_flight_log(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    st = ctx.trips
    ctx.syncer.run_pass()
    back = st.item_by_key("DL|9246|2027-09-21|PHL")
    assert back.sync_state == "synced"
    gone = st.sync_detached(back.id)  # what a pass does on finding it deleted there
    assert (gone.sync_hold, gone.sync_state, gone.checks) == ("deleted", "off", ())
    page = f"/trips/{trip.id}"
    box = Marks.of(c.get(page).text).boxes[back.id]
    assert box["says"] == ["This flight is no longer in your flight log."]
    assert box["controls"] == ["Send again", "Edit"]
    r = act(c, back.id, "send-again")
    assert (r.status_code, r.headers["location"]) == (303, c.url(f"{page}?f=again#item-{back.id}"))
    again = st.item(back.id)
    assert (again.sync_hold, again.sync_state) == (None, "pending")
    assert 'data-flash="It will be sent again"' in c.get(f"{page}?f=again").text
    assert back.id not in Marks.of(c.get(page).text).boxes
    # sent again, nothing is left to send: a second tap changes nothing
    before = dataclasses.asdict(again)
    r = act(c, back.id, "send-again")
    assert (r.status_code, r.headers["location"]) == (303, c.url(f"{page}?f=stale#item-{back.id}"))
    assert dataclasses.asdict(st.item(back.id)) == before


def test_the_actions_refuse_a_cross_site_post_and_a_removed_booking(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = marked_trip(ctx)
    st = ctx.trips
    seen = str(int(util.now().timestamp()))
    posts = (
        (st.item_by_key("lodging|HOTEL1"), "looks-right", {"seen": seen}),
        (st.item_by_key(items_for([])[0].key), "owner", {"on_it": "yes"}),
        (st.item_by_key("DL|9246|2027-09-21|PHL"), "send-again", {}),
    )
    before = rows(ctx.ledger)
    for ti, what, data in posts:
        assert c.post(f"/items/{ti.id}/{what}", data=data).status_code == 403, what
        assert act(c, 999, what, data).status_code == 404, what
    assert rows(ctx.ledger) == before
    for ti, _, _ in posts:
        st.remove_item(ti.id)
    before = rows(ctx.ledger)
    for ti, what, data in posts:
        assert act(c, ti.id, what, data).status_code == 404, what
    assert rows(ctx.ledger) == before
    # one of a deleted trip is no booking the owner can see either
    for ti, _, _ in posts:
        st.restore_item(ti.id)
    st.delete_trip(trip.id)
    before = rows(ctx.ledger)
    for ti, what, data in posts:
        assert act(c, ti.id, what, data).status_code == 404, what
    assert rows(ctx.ledger) == before


def test_no_page_names_a_flight_log_action_when_there_is_none(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, airtrail=False, base=base)
    trip = seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    st.mark(out.id, {"low_confidence": util.now_iso()})
    unnamed = st.upsert_item(trip, items_for([])[0])
    st.upsert_item(None, undated_stay(pnr="HOT9"))  # not the hotel's code
    assert st.item(out.id).checks == ("low_confidence",) and unnamed.checks == ()
    page = c.get(f"/trips/{trip.id}").text
    marks = Marks.of(page)
    assert marks.boxes[out.id]["controls"] == ["Looks right", "Edit"]
    assert unnamed.id not in marks.boxes and marks.sync == {}
    assert "AirTrail" not in page and "flight log" not in page.lower()
    listing = c.get("/").text
    header = f'<a class="top-link" href="{FLIGHT_LOG_ADDRESS}">Flight log</a>'
    assert "AirTrail" not in listing and "flight log" not in listing.lower()
    st.remove_item(out.id)
    gone = c.get("/deleted").text
    assert f"/items/{out.id}/restore" in gone
    assert "AirTrail" not in gone and "flight log" not in gone.lower()
    # with one kept, the list links to it at its address, Recently deleted says it is not
    # touched, and a flight's line says where it stands there, and a dry run says so
    env = {"TRIPBOX_AIRTRAIL_PUBLIC_URL": FLIGHT_LOG_ADDRESS}
    c, ctx = client_for(tmp_path / "kept", monkeypatch, base=base, env=env)
    trip = seed_trip(ctx)
    st = ctx.trips
    assert c.get("/").text.count(header) == 1
    assert "Your flight log and the saved emails are not touched." in c.get("/deleted").text
    out, back = st.item_by_key("SY|9304|2027-09-16|MSP"), st.item_by_key("DL|9246|2027-09-21|PHL")
    page = f"/trips/{trip.id}"

    def lines() -> dict:
        return Marks.of(c.get(page).text).sync

    waiting = "Waiting to be sent to your flight log"
    assert lines() == {out.id: waiting, back.id: waiting}
    ctx.ledger.kv.set("airtrail_dry_run", "on")
    dry = "Not sent: your flight log is in dry-run mode"
    assert lines() == {out.id: dry, back.id: dry}
    ctx.ledger.kv.set("airtrail_dry_run", "off")
    ctx.syncer.run_pass()
    assert lines() == {out.id: "In your flight log", back.id: "In your flight log"}
    st.mark(out.id, {"low_confidence": util.now_iso()})
    assert lines() == {
        out.id: "Not sent to your flight log until checked",
        back.id: "In your flight log",
    }


def test_a_mark_is_a_word_not_a_colour(tmp_path, monkeypatch, base):
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = marked_trip(ctx)
    st = ctx.trips
    stay = st.upsert_item(None, undated_stay(pnr="HOT9"))  # not the hotel's code
    st.mark(stay.id, {"low_confidence": util.now_iso()})
    for path in ("/", f"/trips/{trip.id}"):
        html = c.get(path).text
        page = Marks.of(html)
        assert page.chips and all(page.chips), path
        assert page.boxes, path
        for item, box in page.boxes.items():
            assert (box["role"], box["label"]) == ("group", "To check"), item
            assert box["says"] and all(box["says"]), item
            assert box["controls"] and all(box["controls"]), item
        # what the page adds runs no script and sets no style of its own
        assert re.findall(r"<script(?![^>]*\bsrc=)", html) == [] and page.styled == [], path
    assert "style=" not in unsorted_group(c.get("/").text)


def test_one_booking_that_cannot_be_shown_leaves_the_page_whole(tmp_path, monkeypatch, base):
    """A stored payload the page cannot read (a list where an airline code belongs) leaves the
    booking on its page as its kind and code, with no box; everything else is shown."""
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    st.mark(out.id, {"low_confidence": util.now_iso()})
    undated = st.upsert_item(None, flight(dep="", arr=""))
    assert out.id != undated.id and st.item(undated.id).checks
    for ti in (st.item(out.id), st.item(undated.id)):
        broken = {**ti.payload, "carrier": [ti.payload["carrier"]]}
        ctx.ledger.trip_items.update(ti.id, payload=broken)
    for path, ti in (("/", undated), (f"/trips/{trip.id}", out)):
        page = c.get(path)
        assert page.status_code == 200, path
        marks = Marks.of(page.text)
        assert ti.id in marks.items and ti.id not in marks.boxes, path
        assert f"conf {ti.confirmation}" in page.text, path
    page = c.get(f"/trips/{trip.id}").text
    shown = Marks.of(page).items
    assert all(ti.id in shown for ti in st.items(trip.id))
    home = st.item_by_key("DL|9246|2027-09-21|PHL").payload  # the others as they are
    assert f"{home['carrier']} {home['flight_number']}" in page
    assert "Hilton Example" in page and "Example Rent A Car" in page
    assert f'href="{c.url(f"/items/{out.id}/edit")}"' in page  # it can still be fixed


def test_one_booking_that_cannot_be_shown_breaks_no_list_text_or_calendar(
    tmp_path, monkeypatch, base
):
    """Two bookings whose stored payload no text can be made from, one in the trip and one the
    owner removed: Recently deleted lists the removed one bare, by its kind and code; the share
    page shows the one in the trip bare, as the trip page does, and the trip's text, the share
    page's text and both calendars leave it out. Every page answers, with everything else on
    it."""
    from tripbox.trips import ical

    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    st = ctx.trips
    out = st.item_by_key("SY|9304|2027-09-16|MSP")
    home = st.item_by_key("DL|9246|2027-09-21|PHL")
    for ti in (out, home):
        broken = {**ti.payload, "carrier": [ti.payload["carrier"]]}
        ctx.ledger.trip_items.update(ti.id, payload=broken)
    st.remove_item(home.id)
    token = ical.feed_token(ctx.ledger)
    pages = {
        path: c.get(path)
        for path in (
            "/deleted",
            f"/trips/{trip.id}/text",
            f"/trips/{trip.id}/share",
            f"/trips/{trip.id}/trip.ics",
            f"/feed/{token}.ics",
        )
    }
    assert {path: r.status_code for path, r in pages.items()} == dict.fromkeys(pages, 200)
    gone = pages["/deleted"].text
    assert f"/items/{home.id}/restore" in gone and f"conf {home.confirmation}" in gone
    for path, r in pages.items():
        if path != "/deleted":
            assert "Hilton Example" in r.text and "Example Rent A Car" in r.text, path
    share = pages[f"/trips/{trip.id}/share"].text
    assert f'name="items" value="{out.id}"' in share  # listed bare, to pick or leave
    for path in pages:
        if path != "/deleted":
            assert out.confirmation not in pages[path].text, path
