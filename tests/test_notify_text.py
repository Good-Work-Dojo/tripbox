# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

from test_planner import BOOK, EMAIL, items_for

from tripbox.ledger import Ledger
from tripbox.notify.telegram import _chunks, check_keyboard
from tripbox.notify.text import (
    booking_line,
    check_card,
    fmt_local,
    format_failure,
    format_outcomes,
    human_date,
    item_line,
)
from tripbox.trips import format as fmt
from tripbox.trips.display import Display
from tripbox.trips.store import TripStore
from tripbox.validate import checks
from tripbox.write.executor import Outcome
from tripbox.write.planner import Action

KEYBOARD = {"inline_keyboard": [[{"text": "Looks right", "callback_data": "ck:7:ok"}]]}
CLAUSE = "not sent to your flight log until checked"


def test_item_line_flight_and_hotel():
    f = items_for(["SAMPLE/ALEX QUINN", "SAM SAMPLE", "Max Smith"])[0]
    line = item_line(f)
    assert line.startswith("✈️ DL1234 MSP→PHL Tue 2027-06-22 08:20 CDT → 11:55 EDT")
    assert "Me, Sam, Max Smith" in line and "conf ABC123" in line
    h = items_for(
        ["Casey Sample"], kind="lodging", dep="2027-06-22", arr="2027-06-25", pnr="H1"
    )[0]
    assert item_line(h).startswith(
        "🏨 Hilton Example · 2027-06-22 → 2027-06-25 · Casey Sample · conf H1"
    )


def test_a_flight_printed_with_its_day_alone_shows_no_time():
    f = items_for(["Alex Sample"], dep="2027-06-22", arr="2027-06-22")[0]
    line = item_line(f)
    assert line.startswith("✈️ DL1234 MSP→PHL Tue 2027-06-22 → ? · Me")
    assert "00:00" not in line and "CDT" not in line


def test_format_outcomes_filters_noise_and_says_where_the_flight_log_stands():
    f = items_for(["Alex Sample"])[0]
    dup = Outcome(Action("duplicate", f), "duplicate", "already recorded")
    assert format_outcomes("Receipt", [dup]) is None
    created = Outcome(
        Action("file", f),
        "filed",
        "filed flight",
        trip_id=7,
        trip_title="Philadelphia",
        sync="synced",
    )
    text = format_outcomes(
        "Your Flight Receipt", [created, dup], parser="kitinerary", flight_log=True
    )
    assert text.startswith("📬 Your Flight Receipt (kitinerary)")  # a dry run marks no message
    assert "in your flight log" in text and 'trip "Philadelphia"' in text
    assert "already recorded" in text
    waiting = Outcome(Action("file", f), "filed", "filed flight", sync="pending")
    assert "waiting to go to your flight log" in format_outcomes("x", [waiting], flight_log=True)
    blocked = Outcome(
        Action("file", f), "filed", "filed flight", sync="blocked", checks=("unverified_sender",)
    )
    assert CLAUSE in format_outcomes("x", [blocked], flight_log=True)
    theirs = Outcome(Action("file", f), "filed", "filed flight", sync="off")
    assert "flight log" not in format_outcomes("x", [theirs], flight_log=True)
    # with no flight log kept, no line names one
    for o in (created, waiting, blocked, theirs):
        text = format_outcomes("x", [o])
        assert "flight log" not in text and "AirTrail" not in text, o.sync
    linked = format_outcomes("x", [created], link_base="https://trips.example.com/tripbox")
    assert linked.rstrip().endswith('trip "Philadelphia" https://trips.example.com/tripbox/trips/7')
    assert "https://" not in format_outcomes("x", [created])


def test_a_marked_booking_says_what_is_to_check_and_its_buttons_settle_what_they_can():
    """A booking's line ends with the sentence of each check open on it. Its buttons: Looks
    right for a soft check other than nobody named, the two answers for nobody named, and Send
    again for a flight deleted in the flight log; a hard check is fixed on the page."""
    f = items_for(["Alex Sample"])[0]
    marked = Outcome(
        Action("file", f),
        "filed",
        "filed flight",
        trip_id=7,
        trip_title="Philadelphia",
        sync="blocked",
        checks=("departure", "low_confidence"),
    )
    to_check = " ".join(checks.LABELS[c] for c in marked.checks)
    for kept in (False, True):
        text = format_outcomes("x", [marked], flight_log=kept)
        assert text.endswith(f" · to check: {to_check}"), kept
        assert (CLAUSE in text) is kept and text.count("flight log") == int(kept), kept
    stale = Outcome(Action("ignore", f, reason="older_copy"), "ignored", "", checks=("date_far",))
    assert format_outcomes("x", [stale]) is None
    ignored = Outcome(
        Action("ignore", f, reason="unknown_cancellation"),
        "ignored",
        "",
        checks=("unknown_cancellation",),
    )
    text = format_outcomes("x", [ignored])  # it marked the booking: worth a message
    assert text.endswith(f" · to check: {checks.LABELS['unknown_cancellation']}")
    led = Ledger.memory()
    led.kv.set("airtrail", "on")
    ti = TripStore(led, ("MSP",), book=BOOK).upsert_item(None, f)
    assert booking_line(ti, BOOK) == item_line(f)
    assert check_keyboard(ti, flight_log=True) is None
    # a card says what is to check, and nothing when nothing is
    marked_ti = dataclasses.replace(ti, checks=("low_confidence",))
    assert check_card(marked_ti, BOOK) == f"{booking_line(ti, BOOK)}\n{to_check_of(marked_ti)}"
    assert check_card(ti, BOOK) == booking_line(ti, BOOK)
    link_base = "https://trips.example.com/tripbox"  # TRIPBOX_BASE_URL, set
    link = f"{link_base}/trips/{ti.trip_id}#item-{ti.id}"
    assert check_card(ti, BOOK, link_base=link_base) == f"{booking_line(ti, BOOK)}\n{link}"

    def rows(**kw) -> list[list[tuple[str, str]]]:
        keyboard = check_keyboard(dataclasses.replace(ti, **kw), flight_log=True, made=EMAIL)
        rows = (keyboard or {"inline_keyboard": []})["inline_keyboard"]
        return [[(b["text"], b["callback_data"]) for b in row] for row in rows]

    # Looks right says when its card was made: what was raised after it stays to check
    ok = [("Looks right", f"ck:{ti.id}:ok:{int(EMAIL.timestamp())}")]
    answer = [("I was on this flight", f"ck:{ti.id}:me"), ("Not me", f"ck:{ti.id}:no")]
    again = [("Send again", f"ck:{ti.id}:again")]
    assert rows(checks=("origin", "departure")) == []
    assert rows(checks=("low_confidence",)) == [ok]
    assert rows(checks=("owner_unknown",)) == [answer]
    assert rows(checks=("departure", "owner_unknown", "date_far")) == [ok, answer]
    assert rows(sync_hold="deleted") == [again] and rows(sync_hold="past") == []
    # with no flight log kept, nothing is sent again
    gone = dataclasses.replace(ti, sync_hold="deleted")
    assert check_keyboard(gone, flight_log=False) is None


def to_check_of(ti) -> str:
    return " ".join(checks.LABELS[c] for c in ti.checks)


def test_a_booking_filed_cancelled_says_so():
    f = items_for(["Alex Sample"], status="cancelled")[0]
    filed = Outcome(Action("file", f), "filed", "filed flight", checks=("unknown_cancellation",))
    line = format_outcomes("x", [filed]).splitlines()[-1]
    assert line.startswith("    ❌ CANCELLED · to check: ")
    live = Outcome(Action("file", items_for(["Alex Sample"])[0]), "filed", "filed flight")
    assert "CANCELLED" not in format_outcomes("x", [live])


def test_an_update_says_what_changed_and_an_ignored_copy_says_why():
    f = items_for(["Alex Sample"])[0]
    joined = Outcome(
        Action("update", f, reason="add_passengers", add_passengers=["Sam Sample"]),
        "updated",
        "add_passengers flight",
        sync="synced",
    )
    assert "➕ Sam Sample" in format_outcomes("x", [joined])
    left = Outcome(
        Action("update", f, reason="remove_passengers", remove_passengers=["Sam Sample"]),
        "updated",
        "remove_passengers flight",
    )
    assert "➖ Sam Sample" in format_outcomes("x", [left])
    moved = Outcome(
        Action("update", f, reason="changed", changes={"dep_utc": ("a", "b")}),
        "updated",
        "changed flight",
    )
    assert "✏️ changed: dep_utc a→b" in format_outcomes("x", [moved])
    stale = Outcome(Action("ignore", f, reason="older_copy"), "ignored", "older_copy")
    text = format_outcomes("x", [moved, stale])
    assert "ignored: an older copy of a booking that has changed since" in text
    assert format_outcomes("x", [stale]) is None  # nothing worth a message on its own
    failed = Outcome(Action("file", f), "failed", "file failed: boom", error="boom")
    assert "⚠️ failed: boom" in format_outcomes("x", [failed])
    assert format_failure("Receipt", "boom") == "⚠️ tripbox could not process “Receipt”: boom"


def test_chunks_split_on_lines():
    text = "\n".join(f"line {i}" for i in range(2000))
    parts = _chunks(text, 3900)
    assert len(parts) > 1 and all(len(p) <= 3900 for p in parts) and "\n".join(parts) == text


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeHttp:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return _Resp({"ok": True, "result": {"message_id": len(self.posts)}})


def test_keyboard_rides_on_the_last_chunk_of_a_long_message():
    from tripbox.notify.telegram import Telegram

    http = _FakeHttp()
    tg = Telegram("123456789:" + "A" * 35, "42", http=http)
    long_text = "\n".join(f"line {i}" for i in range(1500))
    mid = tg.send(long_text, reply_markup=KEYBOARD)
    assert len(http.posts) > 1 and mid == len(http.posts)
    assert all("reply_markup" not in p for _, p in http.posts[:-1])
    assert http.posts[-1][1]["reply_markup"] == KEYBOARD
    assert http.posts[-1][1]["chat_id"] == "42"


def test_silent_send_disables_the_notification_sound():
    from tripbox.notify.telegram import Telegram

    http = _FakeHttp()
    tg = Telegram("123456789:" + "A" * 35, "42", http=http)
    tg.send("hello")
    tg.send("hello", silent=True)
    assert "disable_notification" not in http.posts[0][1]
    assert http.posts[1][1]["disable_notification"] is True


def test_an_overlong_single_line_is_split_rather_than_dropped():
    from tripbox.notify.telegram import MAX_LEN, _chunks

    chunks = _chunks("x" * 5000 + "\nshort", MAX_LEN)
    assert all(len(c) <= MAX_LEN for c in chunks) and "".join(chunks).count("x") == 5000


def test_sent_text_is_redacted():
    from tripbox.log import add_secrets
    from tripbox.notify.telegram import Telegram

    add_secrets(["s3cret-api-key-value"])
    http = _FakeHttp()
    tg = Telegram("123456789:" + "A" * 35, "42", http=http)
    tg.send("AirTrail said: bad key s3cret-api-key-value")
    assert "s3cret" not in http.posts[0][1]["text"] and "[REDACTED]" in http.posts[0][1]["text"]


def test_a_deleted_trip_is_named_but_never_linked():
    f = items_for(["Alex Sample"])[0]
    cancelled = Outcome(
        Action("update", f, reason="cancel"),
        "updated",
        "cancel flight",
        trip_id=None,  # the trip is in Recently deleted: a link would land on the 404 page
        trip_title="Philadelphia",
    )
    text = format_outcomes("x", [cancelled], link_base="https://trips.example.com/tripbox")
    assert "❌ CANCELLED" in text
    assert 'trip "Philadelphia" (deleted)' in text and "/tripbox/trips/" not in text


def test_the_default_display_keeps_the_notices_words():
    """At the default clock and date order, whatever the maps, a notice's line is as with no
    style set; another clock or date order says the day as the page does."""
    f = items_for(["SAMPLE/ALEX QUINN", "SAM SAMPLE"])[0]
    h = items_for(["Casey Sample"], kind="lodging", pnr="H1")[0]
    plain = [item_line(f), item_line(h)]
    for maps in ("apple", "google", "osm"):
        with fmt.styled(Display(maps=maps)):
            assert [item_line(f), item_line(h)] == plain, maps
    with fmt.styled(Display(clock="24")):
        assert item_line(f) != plain[0] and "-" not in item_line(f).split(" · ")[0]


def test_a_zone_with_no_letters_reads_as_on_the_page():
    """A zone whose abbreviation is only its offset ('+04') is named as the page names it,
    `UTC+4`, in a notice's own words and in the owner's style alike."""
    local = items_for(["Alex Sample"])[0].payload["dep_local"]
    zone = "Etc/GMT-4"  # four hours ahead of UTC, with no letters of its own
    assert fmt_local(local, zone).endswith(" UTC+4") and "+04" not in fmt_local(local, zone)
    at = dt.datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone))
    assert human_date(at).endswith(" UTC+4") and "+04" not in human_date(at)
    assert human_date(at.replace(tzinfo=dt.UTC)).endswith(" UTC")
    with fmt.styled(Display(clock="24")):
        assert fmt_local(local, zone).endswith(" UTC+4")


def test_an_arrival_on_the_next_day_carries_its_day_in_either_style():
    """An arrival on another day than its departure says its day, in a notice's own words and in
    the owner's style; one on the same day says its time alone."""
    same = items_for(["Alex Sample"])[0]
    dep = dt.datetime.fromisoformat(same.payload["dep_local"])
    nxt = (dep + dt.timedelta(days=1)).replace(hour=6, minute=40)
    late = items_for(["Alex Sample"], arr=nxt.strftime("%Y-%m-%dT%H:%M"))[0]
    arrival = item_line(late).split(" → ", 1)[1].split(" · ")[0]
    assert arrival.startswith(nxt.strftime("%a %Y-%m-%d 06:40")), arrival
    assert nxt.strftime("%a") not in item_line(same).split(" → ", 1)[1].split(" · ")[0]
    with fmt.styled(Display(clock="24")):
        arrival = item_line(late).split(" → ", 1)[1].split(" · ")[0]
        assert arrival.startswith(f"{fmt.day_label(nxt.date())} 06:40"), arrival
        same_arrival = item_line(same).split(" → ", 1)[1].split(" · ")[0]
        assert fmt.day_label(dep.date()) not in same_arrival
