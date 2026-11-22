# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The display choices (`trips.display`): the clock, the order of a date's day and month and the
maps an address opens in, kept in the ledger and honoured by the one formatter
(`trips.format.styled`) wherever a booking is said: the page, the share text, the notices, the bot
and the CLI. With no style set, every text reads as it always has (the format and notice tests,
unchanged, hold that); the corpus's own trips, filed as the demo files them, show each style in
the same words on the page and in a notice."""

import datetime as dt
import html
import re
from zoneinfo import ZoneInfo

import pytest
from helpers.web import Page, client_for
from test_bot import bot_for, said
from test_filed import deliver, filing, owners_flight, whole
from test_planner import items_for
from test_trips_store import car_item

from tripbox import cli, corpus, demo
from tripbox.ledger import Ledger
from tripbox.notify import text as notice
from tripbox.trips import display
from tripbox.trips import format as fmt
from tripbox.trips.display import Display
from tripbox.web.app import create_app

STYLES = [
    Display(clock=clock, dates=dates) for clock in ("12", "24") for dates in ("mdy", "dmy")
]
# The styles a notice follows: every one but the default clock and date order
CHANGED = [style for style in STYLES if (style.clock, style.dates) != ("12", "mdy")]
UNREADABLE = ("not json", "[]", "7", '{"clock": "36"}', '{"dates": 5}', '{"maps": "atlas"}')
DAY = dt.date(2027, 6, 17)  # a Thursday
LATER = dt.date(2027, 6, 19)  # the Saturday after it
EVENING = "2027-06-17T21:45"
MORNING = "2027-06-17T08:05"
ZONE = "America/Chicago"


def the_default_texts() -> list[object]:
    """Every text the formatter makes of a few values, and the notices' lines of a flight, a stay
    and a car."""
    far = DAY + dt.timedelta(days=200)  # in the next year
    flight = items_for(["Alex Sample"])[0]
    stay = items_for(["Alex Sample"], kind="lodging", dep=EVENING[:10], arr=LATER.isoformat())[0]
    return [
        fmt.fmt_time(EVENING, ZONE),
        fmt.fmt_time(MORNING, None),
        fmt.time_parts(EVENING, ZONE),
        fmt.time_parts(None, ZONE),
        fmt.day_label(DAY),
        fmt.day_band(DAY),
        fmt.date_range(DAY.isoformat(), DAY.isoformat()),
        fmt.date_range(DAY.isoformat(), LATER.isoformat()),
        fmt.date_range(DAY.isoformat(), far.isoformat()),
        fmt.short_range(DAY.isoformat(), LATER.isoformat(), DAY),
        fmt.short_range(DAY.isoformat(), far.isoformat(), DAY),
        notice.item_line(flight),
        notice.item_line(stay),
        notice.item_line(car_item()),
    ]


def test_no_style_and_an_unreadable_row_are_the_default():
    """No row and an unreadable one read as the default and store no choice; under no style and
    under the default read from such a ledger, every text is the same, and today's."""
    led = Ledger.memory()
    assert display.read(led) == Display() and display.stored(led) is None
    plain = the_default_texts()
    assert plain[:7] == [
        "9:45 PM CDT",
        "8:05 AM",
        ("9:45 PM", "CDT"),
        ("", ""),
        "Thu Jun 17",
        "THURSDAY, JUNE 17, 2027",
        "Thu Jun 17, 2027",
    ]
    assert plain[7] == "Thu Jun 17 – Sat Jun 19, 2027"
    assert plain[9] == "Jun 17 – Jun 19"
    # the notice's flight line as today's default prints it, of the helper's own flight
    p = items_for(["Alex Sample"])[0].payload
    dep = dt.datetime.fromisoformat(p["dep_local"]).replace(tzinfo=ZoneInfo(p["origin_tz"]))
    arr = dt.datetime.fromisoformat(p["arr_local"]).replace(tzinfo=ZoneInfo(p["destination_tz"]))
    route = f"{p['flight_code']} {p['origin']}→{p['destination']}"
    assert plain[11].startswith(f"✈️ {route} {dep:%a %Y-%m-%d %H:%M %Z} → {arr:%H:%M %Z}")
    for raw in UNREADABLE:
        led.kv.set(display.KV, raw)
        assert display.read(led) == Display(), raw
        assert display.stored(led) is None, raw
        with fmt.styled(display.stored(led)):
            assert the_default_texts() == plain, raw
        with fmt.styled(display.read(led)):  # the doors': the default choices, every text as is
            assert the_default_texts() == plain, raw
    assert fmt.style() is None  # each door's style ends with it


def test_both_clocks_and_both_date_orders():
    later = LATER.isoformat()
    far = (DAY + dt.timedelta(days=200)).isoformat()
    want = {
        ("12", "mdy"): (
            "9:45 PM CDT",
            "Thu Jun 17",
            "Thu Jun 17 – Sat Jun 19, 2027",
            "THURSDAY, JUNE 17, 2027",
            "Jun 17 – Jun 19",
        ),
        ("24", "mdy"): (
            "21:45 CDT",
            "Thu Jun 17",
            "Thu Jun 17 – Sat Jun 19, 2027",
            "THURSDAY, JUNE 17, 2027",
            "Jun 17 – Jun 19",
        ),
        ("12", "dmy"): (
            "9:45 PM CDT",
            "Thu 17 Jun",
            "Thu 17 Jun – Sat 19 Jun 2027",
            "THURSDAY 17 JUNE 2027",
            "17 Jun – 19 Jun",
        ),
        ("24", "dmy"): (
            "21:45 CDT",
            "Thu 17 Jun",
            "Thu 17 Jun – Sat 19 Jun 2027",
            "THURSDAY 17 JUNE 2027",
            "17 Jun – 19 Jun",
        ),
    }
    own_words = notice.item_line(items_for(["Alex Sample"])[0])  # with no style set
    for style in STYLES:
        with fmt.styled(style):
            got = (
                fmt.fmt_time(EVENING, ZONE),
                fmt.day_label(DAY),
                fmt.date_range(DAY.isoformat(), later),
                fmt.day_band(DAY),
                fmt.short_range(DAY.isoformat(), later, DAY),
            )
            assert got == want[(style.clock, style.dates)], style
            clock, zone = fmt.time_parts(EVENING, ZONE)
            assert f"{clock} {zone}" == got[0] and zone == "CDT"
            assert fmt.fmt_time(MORNING, None) == ("08:05" if style.clock == "24" else "8:05 AM")
            # one day alone, and a range over a year's end, say each year once
            day = got[1]
            assert fmt.date_range(DAY.isoformat(), DAY.isoformat()) == (
                f"{day} 2027" if style.dates == "dmy" else f"{day}, 2027"
            )
            span = fmt.date_range(DAY.isoformat(), far)
            assert span.count("2027") == 1 and span.endswith(far[:4])
            assert fmt.short_range(DAY.isoformat(), far, DAY).endswith(far[:4])
            # a notice says a booking in the same words, the day then the time, once the clock
            # or the date order is not the default; at the default, in its own words
            flight = items_for(["Alex Sample"])[0]
            if style not in CHANGED:
                assert notice.item_line(flight) == own_words
                continue
            p = flight.payload
            dep = fmt.fmt_time(p["dep_local"], p["origin_tz"])
            arr = fmt.fmt_time(p["arr_local"], p["destination_tz"])
            dep_day = fmt.day_label(dt.date.fromisoformat(p["dep_local"][:10]))
            assert f" {dep_day} {dep} → {arr} · " in notice.item_line(flight), style
            stay = items_for(["Alex Sample"], kind="lodging", dep=EVENING[:10], arr=later)[0]
            assert f" · {day} → {fmt.day_label(LATER)} · " in notice.item_line(stay), style
    assert fmt.style() is None


@pytest.fixture(scope="module")
def demo_page(tmp_path_factory):
    """The corpus's demo, filed into a new ledger, the page over it (no sign-in, at the host's
    root) and the corpus's own settings, which a test reads the page under
    (`corpus.process_as`)."""
    c = corpus.load(corpus.default_dir())
    out = tmp_path_factory.mktemp("demo") / "out"
    seeded = demo.seed(c, out, c.anchor)
    assert seeded.failed == ()
    s = corpus.settings(
        c,
        data_dir=out / "data",
        archive_dir=out / "archive",
        logs_dir=out / "logs",
        config_dir=out / corpus.CONFIG_DIR,
    )
    app = create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)
    return Page(app, ""), s


def timed_flights(led: Ledger, s) -> list:
    """Each trip's flights that say a time and a zone, with their trip: (trip, booking)."""
    from tripbox.trips.store import TripStore

    st = TripStore(led, s.home_airports, book=s.travellers)
    return [
        (t, ti)
        for t in st.all_trips()
        for ti in st.items(t.id)
        if ti.kind == "flight"
        and ti.status != "cancelled"
        and len(ti.start_at or "") > 10
        and ti.start_tz
        and len(ti.end_at or "") > 10
    ]


def test_the_page_and_the_notice_say_a_booking_in_the_same_words_in_every_style(demo_page):
    """A corpus flight, in each style but the default clock and date order (where a notice keeps
    its own words): its trip page shows its day's band and its times, and the trip's text and
    the flight's card (`notify.text`) say the same day and times."""
    page, s = demo_page
    led = Ledger.open(s.ledger_path)
    try:
        trip, ti = timed_flights(led, s)[0]
        start = dt.date.fromisoformat(ti.start_at[:10])
        seen = set()
        for style in CHANGED:
            display.write(led, clock=style.clock, dates=style.dates)
            with corpus.process_as(s):
                shown = html.unescape(page.get(f"/trips/{trip.id}").text)
                text = page.get(f"/trips/{trip.id}/text").text
            with fmt.styled(display.read(led)):
                card = notice.check_card(ti, s.travellers)
                band, day = fmt.day_band(start), fmt.day_label(start)
                clock, zone = fmt.time_parts(ti.start_at, ti.start_tz)
                dep = fmt.fmt_time(ti.start_at, ti.start_tz)
                arr = fmt.fmt_time(ti.end_at, ti.end_tz)
            assert f'<h2 class="day">{band}</h2>' in shown, style
            assert f'<span class="t">{clock}</span><span class="tz">{zone}</span>' in shown
            assert f"arrives {arr}" in shown, style
            assert f"\n{day}\n" in text and f" {dep} → " in text and arr in text, style
            assert f" {day} {dep} → {arr} · " in card, style
            seen.add((band, dep))
        assert len(seen) == 3  # each style says it in words of its own
    finally:
        led.close()


def test_saving_the_default_changes_no_notice(monkeypatch, tmp_path):
    """The default clock and date order, stored (as Save leaves them unchanged), an empty row,
    and a change of the maps alone each leave a message's notice, a booking's card and the bot's
    replies byte for byte as with nothing stored."""
    entry, res = owners_flight()
    rows = {
        "nothing": None,
        "the default": lambda led: display.write(led, clock="12", dates="mdy", maps="apple"),
        "an empty row": lambda led: led.kv.set(display.KV, "{}"),
        "the maps alone": lambda led: display.write(led, maps="osm"),
    }
    said_for: dict[str, list[str]] = {}
    for name, store in rows.items():
        with filing() as ctx:
            bot, tg = bot_for(ctx, tmp_path / name.replace(" ", "-"))
            if store is not None:
                store(ctx.ledger)
            deliver(ctx, monkeypatch, entry, [whole(res)])
            (trip,) = ctx.trips.all_trips()
            (ti,) = ctx.trips.items(trip.id)
            with fmt.styled(display.read(ctx.ledger)):
                card = notice.check_card(ti, ctx.book)
            said_for[name] = [
                *(text for text, _ in tg.sent),
                card,
                said(bot, tg, f"/share {trip.id}"),
            ]
    want = said_for.pop("nothing")
    assert f" {ti.start_at[:10]} " in want[0]  # the notice's own words: the day as ISO
    for name, got in said_for.items():
        assert got == want, name


def test_a_choice_outside_the_list_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    led = Ledger.memory()
    display.write(led, clock="24")
    before = led.kv.get(display.KV)
    for bad in ({"clock": "36"}, {"dates": "ymd"}, {"maps": "atlas"}, {"colour": "red"}):
        with pytest.raises(ValueError):
            display.write(led, **bad)
        assert led.kv.get(display.KV) == before, bad
    with pytest.raises(ValueError):
        display.write(led, dates="dmy", clock="36")  # one bad value stores none of them
    assert display.read(led) == Display(clock="24")
    # the page's own form refuses it the same way
    c, ctx = client_for(tmp_path, monkeypatch)
    same = {"Origin": "http://localhost:8110"}
    good = {"clock": "24", "dates": "dmy", "maps": "osm"}
    saved = c.post("/settings/display", data=good, headers=same, follow_redirects=False)
    assert saved.status_code == 303
    kept = ctx.ledger.kv.get(display.KV)
    for name in good:
        r = c.post("/settings/display", data={**good, name: "x"}, headers=same)
        assert r.status_code == 400 and ctx.ledger.kv.get(display.KV) == kept, name
    assert display.read(ctx.ledger) == Display(clock="24", dates="dmy", maps="osm")


def test_the_maps_link_follows_the_choice(demo_page):
    page, s = demo_page
    address = "1 Sample Street, Sample Town"
    assert display.maps_url(Display(), address) == (
        "https://maps.apple.com/?q=1%20Sample%20Street%2C%20Sample%20Town"
    )
    assert display.maps_url(Display(maps="google"), address).startswith(
        "https://www.google.com/maps/search/?api=1&query=1%20Sample"
    )
    assert display.maps_url(Display(maps="osm"), address).startswith(
        "https://www.openstreetmap.org/search?query=1%20Sample"
    )
    led = Ledger.open(s.ledger_path)
    try:
        from tripbox.trips.store import TripStore

        st = TripStore(led, s.home_airports, book=s.travellers)
        trip, where = next(
            (t, ti.payload["address"])
            for t in st.all_trips()
            for ti in st.items(t.id)
            if ti.kind == "lodging" and ti.payload.get("address")
        )
        for maps in display.CHOICES["maps"]:
            display.write(led, maps=maps)
            with corpus.process_as(s):
                shown = page.get(f"/trips/{trip.id}").text
            links = re.findall(r'<a class="btn link" href="([^"]+)"[^>]*>Open in Maps</a>', shown)
            want = display.maps_url(Display(maps=maps), str(where))
            assert links and {html.unescape(u) for u in links} == {want}, maps
    finally:
        led.close()


def test_every_door_says_a_booking_in_the_stored_style(
    monkeypatch, tmp_path, capsys, demo_page
):
    """The notices of a message, the bot's replies and the CLI's `trips text` and `upcoming` each
    say a booking in the style the ledger holds; with none stored, as before."""
    chosen = Display(clock="24", dates="dmy")
    entry, res = owners_flight()
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        display.write(ctx.ledger, clock=chosen.clock, dates=chosen.dates)
        deliver(ctx, monkeypatch, entry, [whole(res)])
        (trip,) = ctx.trips.all_trips()
        (ti,) = ctx.trips.items(trip.id)
        with fmt.styled(chosen):
            dep = fmt.fmt_time(ti.start_at, ti.start_tz)
            day = fmt.day_label(dt.date.fromisoformat(ti.start_at[:10]))
        (filed,) = [text for text, _ in tg.sent]  # the message's notice
        assert f" {day} {dep} → " in filed
        shared = said(bot, tg, f"/share {trip.id}")
        assert f"\n{day}\n" in shared and f" {dep} → " in shared
        assert said(bot, tg, "/upcoming") == shared
        ctx.ledger.kv.set(display.KV, "")  # none stored: as before
        before = fmt.fmt_time(ti.start_at, ti.start_tz)
        assert f" {before} → " in said(bot, tg, f"/share {trip.id}")
        assert fmt.style() is None  # each door's style ends with it
    page, s = demo_page
    led = Ledger.open(s.ledger_path)
    try:
        trip, ti = timed_flights(led, s)[0]
        display.write(led, clock=chosen.clock, dates=chosen.dates)
        with fmt.styled(chosen):
            dep = fmt.fmt_time(ti.start_at, ti.start_tz)
    finally:
        led.close()
    env = {
        **corpus.load(corpus.default_dir()).settings,
        "TRIPBOX_DATA_DIR": str(s.data_dir),
        "TRIPBOX_CONFIG_DIR": str(s.config_dir),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with corpus.process_as(s):
        assert cli.main(["trips", "text", str(trip.id)]) == 0
        assert f" {dep} → " in capsys.readouterr().out
        assert cli.main(["upcoming"]) == 0
        upcoming = capsys.readouterr().out
        assert re.search(r" \d\d:\d\d [A-Z]", upcoming) and not re.search(r" [AP]M\b", upcoming)
    assert fmt.style() is None
