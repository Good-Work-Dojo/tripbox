# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The Telegram bot (`notify.bot`): `/check` lists the bookings marked to check, each with its
buttons, and a button says through the trip store what the owner says about a booking; `/paste`,
`/share`, `/upcoming` and `/status`. Every booking is the corpus's own, filed through the pipeline
as `test_filed` files it; Telegram is a stand-in that records what is sent."""

import dataclasses
import datetime as dt
import logging
import re

import pytest
from test_checks import booking, edit, first, owners, plain, the_corpus
from test_filed import UNSURE, deliver, filing, model_reads, owners_flight, whole
from test_store_actions import an_hour_later, nameless, owners_flights

from tripbox import util
from tripbox.notify import bot as botmod
from tripbox.notify.bot import COMMANDS, HELP, KV_PASTE, Bot, _terminate
from tripbox.notify.notifier import Notifier
from tripbox.notify.telegram import TelegramChannel, check_keyboard
from tripbox.notify.text import booking_line, check_card
from tripbox.pipeline import run as runmod
from tripbox.validate import checks
from tripbox.validate.keys import is_draft
from tripbox.write.lifecycle import note_airtrail

LINK_BASE = "https://trips.example.com/tripbox"  # TRIPBOX_BASE_URL, set
CHAT = 42
NOTHING_LEFT = "Nothing left to do for this one"
CLAUSE = "not sent to your flight log until checked"


class FakeTG:
    def __init__(self):
        self.sent, self.api_calls = [], []

    def send(self, text, reply_markup=None, silent=False):
        self.sent.append((text, reply_markup))
        return len(self.sent)

    def api(self, method, **params):
        self.api_calls.append((method, params))
        return []

    def answer_callback(self, cq_id, text):
        self.api_calls.append(("answerCallbackQuery", {"id": cq_id, "text": text}))

    def edit_text(self, message_id, text, reply_markup=None):
        edit = {"message_id": message_id, "text": text, "reply_markup": reply_markup}
        self.api_calls.append(("editMessageText", edit))


def bot_for(ctx, tmp_path) -> tuple[Bot, FakeTG]:
    """The bot over `ctx`, its chat the stand-in's, which also gets every notification."""
    tg = FakeTG()
    ctx.notifier = Notifier([TelegramChannel(tg)])
    bot = Bot(ctx, tg, tmp_path / "hb")
    bot.chat_id = str(CHAT)
    return bot, tg


def say(bot: Bot, tg: FakeTG, text: str) -> list[tuple[str, dict | None]]:
    """What the bot sends back to `text` in its chat."""
    before = len(tg.sent)
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": CHAT}, "text": text}})
    return tg.sent[before:]


def said(bot: Bot, tg: FakeTG, text: str) -> str:
    """The one message the bot sends back to `text`."""
    (answer,) = say(bot, tg, text)
    return answer[0]


def tap(
    bot: Bot, tg: FakeTG, data: str, *, card: int = 77, text: str = "card"
) -> tuple[str, str, dict | None]:
    """Tap the button `data` on the card `card` whose text is `text`: the one line this tap is
    answered with, and the card's text and buttons once this tap has edited it."""
    before = len(tg.api_calls)
    message = {"chat": {"id": CHAT}, "message_id": card, "text": text}
    query = {"id": "cq", "data": data, "message": message}
    bot.handle_update({"update_id": 2, "callback_query": query})
    calls = tg.api_calls[before:]
    (answered,) = [p["text"] for m, p in calls if m == "answerCallbackQuery"]
    (edited,) = [p for m, p in calls if m == "editMessageText"]
    assert edited["message_id"] == card
    return answered, edited["text"], edited["reply_markup"]


def buttons(keyboard: dict | None) -> list[list[tuple[str, str]]]:
    """Each row's buttons, label and data; the time a Looks right button carries left out."""
    rows = (keyboard or {}).get("inline_keyboard", [])
    untimed = lambda data: re.sub(r"(:ok):\d+$", r"\1", data)  # noqa: E731
    return [[(b["text"], untimed(b["callback_data"])) for b in row] for row in rows]


def ok_of(keyboard: dict | None) -> str:
    """The data of the card's Looks right button."""
    (data,) = [
        b["callback_data"]
        for row in (keyboard or {}).get("inline_keyboard", [])
        for b in row
        if b["text"] == "Looks right"
    ]
    return data


def test_check_lists_marked_bookings_with_their_buttons(monkeypatch, tmp_path):
    """Each booking with a check open is its own message: its line, the sentence of each check,
    the link to it on the page when the page has an address, and the buttons that settle what a
    button can. A booking with nothing to check is not listed; at most ten are."""
    (entry, flight), (other, second) = owners_flights(2)
    stay_entry, stay = booking("lodging")
    car_entry, car = booking("car")
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        kept = ctx.trips.airtrail_on()
        assert say(bot, tg, "/check") == [("Nothing to check.", None)]
        unnamed = deliver(ctx, monkeypatch, entry, [nameless(flight)], n=0).outcomes[0]
        unsure = deliver(ctx, monkeypatch, stay_entry, [stay], n=1, confidence=UNSURE).outcomes[0]
        undated = edit(second, departure_local=None, arrival_local=None)
        dayless = deliver(ctx, monkeypatch, other, [undated], n=2).outcomes[0]
        fine = deliver(ctx, monkeypatch, car_entry, [car], n=3).outcomes[0]
        assert fine.checks == ()
        marked = ctx.trips.flagged()
        assert [ti.id for ti in marked] == sorted(
            [unnamed.trip_item_id, unsure.trip_item_id], key=lambda i: ctx.trips.item(i).start_at
        ) + [dayless.trip_item_id]
        ctx.settings = dataclasses.replace(ctx.settings, link_base=LINK_BASE)
        cards = say(bot, tg, "/check")
        assert len(cards) == 3
        for (text, keyboard), ti in zip(cards, marked, strict=True):
            assert text.startswith(booking_line(ti, ctx.book)), ti.item_key
            assert all(checks.LABELS[code] in text for code in ti.checks), ti.item_key
            page = f"/trips/{ti.trip_id}#item-{ti.id}" if ti.trip_id else "/"
            assert text.splitlines()[-1] == f"{LINK_BASE}{page}", ti.item_key
            assert buttons(keyboard) == buttons(check_keyboard(ti, flight_log=kept)), ti.item_key
        by_id = {ti.id: ti for ti in marked}
        me, no = (f"ck:{unnamed.trip_item_id}:{v}" for v in ("me", "no"))
        assert buttons(check_keyboard(by_id[unnamed.trip_item_id], flight_log=kept)) == [
            [("I was on this flight", me), ("Not me", no)]
        ]
        assert buttons(check_keyboard(by_id[unsure.trip_item_id], flight_log=kept)) == [
            [("Looks right", f"ck:{unsure.trip_item_id}:ok")]
        ]
        dayless_ti = by_id[dayless.trip_item_id]
        assert check_keyboard(dayless_ti, flight_log=kept) is None  # fixed on the page
        assert check_keyboard(ctx.trips.item(fine.trip_item_id), flight_log=kept) is None
        # without an address for the page, no link
        ctx.settings = dataclasses.replace(ctx.settings, link_base=None)
        assert not any("https://" in text for text, _ in say(bot, tg, "/check"))
        assert "to check: 3 · unsorted: 1" in said(bot, tg, "/status")
        # what the model cost is said of the model, whichever provider it is
        assert said(bot, tg, "/status").splitlines()[-1].startswith("this month: model ")
        # with a flight log kept, a dry run is said in one phrase; with it off, not at all
        ctx.settings = dataclasses.replace(ctx.settings, airtrail_dry_run=True)
        assert said(bot, tg, "/status").splitlines()[0] == (
            "tripbox status · dry run: flights are filed but not sent"
        )
        ctx.settings = dataclasses.replace(ctx.settings, airtrail_dry_run=False)
        assert said(bot, tg, "/status").splitlines()[0] == "tripbox status"
        # another chat is not answered
        before = len(tg.sent)
        bot.handle_update({"update_id": 3, "message": {"chat": {"id": 999999}, "text": "/check"}})
        assert len(tg.sent) == before
        # at most ten are listed, and the rest are counted
        many = [by_id[unsure.trip_item_id]] * 12
        monkeypatch.setattr(ctx.trips, "flagged", lambda trip_id=None: many)
        cards = say(bot, tg, "/check")
        assert len(cards) == 11 and cards[-1] == ("and 2 more on the page", None)


def test_one_card_that_cannot_be_built_does_not_silence_the_others(monkeypatch, tmp_path, caplog):
    """A booking whose card cannot be made is left out, its id logged; the others are sent, after
    a message and on /check."""
    (entry, flight), (_, stay) = owners_flight(), booking("lodging")
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)

        def broken(ti, book, **kw):
            if ti.kind == "flight":
                raise ValueError("no card")
            return check_card(ti, book, **kw)

        monkeypatch.setattr(runmod, "check_card", broken)
        monkeypatch.setattr(botmod, "check_card", broken)
        caplog.set_level(logging.ERROR)
        got = deliver(ctx, monkeypatch, entry, [flight, stay], confidence=UNSURE)
        plane, hotel = sorted(got.outcomes, key=lambda o: o.action.item.kind)
        assert plane.checks == hotel.checks == ("low_confidence",)
        (lines, plain_), (card, keyboard) = tg.sent
        filed = ctx.trips.item(hotel.trip_item_id)
        assert plain_ is None and card.startswith(booking_line(filed, ctx.book))
        assert any(f"#{plane.trip_item_id}" in r.getMessage() for r in caplog.records)
        ((text, keyboard),) = say(bot, tg, "/check")
        assert text == card
        assert buttons(keyboard) == [[("Looks right", f"ck:{hotel.trip_item_id}:ok")]]


def test_looks_right_from_a_button_clears_the_mark_and_a_second_tap_says_nothing_is_left(
    monkeypatch, tmp_path
):
    entry, flight = owners_flight()
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        (o,) = deliver(ctx, monkeypatch, entry, [flight], confidence=UNSURE).outcomes
        assert (o.checks, o.sync, ctx.airtrail.flights) == (("low_confidence",), "blocked", [])
        card, keyboard = tg.sent[-1]  # the message's lines, then the booking's own card
        assert buttons(keyboard) == [[("Looks right", f"ck:{o.trip_item_id}:ok")]]
        ok = ok_of(keyboard)  # it says when its card was made, within Telegram's 64 bytes
        assert re.fullmatch(rf"ck:{o.trip_item_id}:ok:\d+", ok) and len(ok.encode()) <= 64
        answered, edited, left = tap(bot, tg, ok, text=card)
        ti = ctx.trips.item(o.trip_item_id)
        assert (ti.checks, ti.sync_state) == ((), "synced")
        # the card is the booking as it is now, with nothing left to check and no button
        assert answered == "Marked as right" and left is None
        assert edited == f"{check_card(ti, ctx.book)}\n\nMarked as right"
        assert checks.LABELS["low_confidence"] not in edited
        (there,) = ctx.airtrail.flights
        assert ti.airtrail_flight_id == there["id"]
        answered, _, _ = tap(bot, tg, ok, text=card)
        assert answered == NOTHING_LEFT and ctx.trips.item(ti.id) == ti
        assert tap(bot, tg, "ck:999999:ok:0")[0] == NOTHING_LEFT  # a booking that is not there


def test_looks_right_says_the_card_was_right_not_what_came_after_it(monkeypatch, tmp_path):
    """Looks right is the owner's word on what the card showed. A card made two hours ago shows
    a doubt raised three hours ago; an hour ago a change came from a sender nobody vouches for.
    Its Looks right closes the first and leaves the second open, so the flight waits. A button
    that does not say when its card was made, or says it in other than plain digits, does
    nothing."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    later = edit(
        flight,
        departure_local=an_hour_later(leg["departure_local"]),
        arrival_local=an_hour_later(leg["arrival_local"]),
    )
    now = dt.datetime.now(dt.UTC)
    hours = lambda n: now - dt.timedelta(hours=n)  # noqa: E731
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        with util.clock_at(hours(3)):  # read when it is received: its doubt is dated then
            (o,) = deliver(
                ctx, monkeypatch, entry, [flight], n=0, confidence=UNSURE, received=hours(3)
            ).outcomes
        card = tg.sent[-1][0]
        changed = deliver(
            ctx, monkeypatch, entry, [later], n=1, doc="change", unvouched=True, received=hours(1)
        )
        assert [x.result for x in changed.outcomes] == ["updated"]
        assert ctx.trips.item(o.trip_item_id).checks == ("low_confidence", "unverified_sender")
        made = int(hours(2).timestamp())
        answered, _, left = tap(bot, tg, f"ck:{o.trip_item_id}:ok:{made}", text=card)
        ti = ctx.trips.item(o.trip_item_id)
        assert answered == "Marked as right"
        assert (ti.checks, ti.sync_state) == (("unverified_sender",), "blocked")
        when = dt.datetime.fromtimestamp(made, dt.UTC).isoformat(timespec="seconds")
        assert ti.payload["looked_at"] == when
        assert buttons(left) == [[("Looks right", f"ck:{ti.id}:ok")]]
        assert ctx.airtrail.requests == []
        for data in (f"ck:{ti.id}:ok", f"ck:{ti.id}:ok:soon", f"ck:{ti.id}:ok:{made}²"):
            assert tap(bot, tg, data, text=card)[0] == NOTHING_LEFT, data
            assert ctx.trips.item(ti.id) == ti, data


def test_looks_right_that_closes_nothing_says_nothing_is_left(monkeypatch, tmp_path):
    """An old card tapped after the owner looked on the page: what is open now was raised after
    both looks, so the tap closes nothing, says so, and leaves the later look as it was."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    later = edit(
        flight,
        departure_local=an_hour_later(leg["departure_local"]),
        arrival_local=an_hour_later(leg["arrival_local"]),
    )
    now = dt.datetime.now(dt.UTC)
    hours = lambda n: now - dt.timedelta(hours=n)  # noqa: E731
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        with util.clock_at(hours(3)):  # read when it is received: its doubt is dated then
            (o,) = deliver(
                ctx, monkeypatch, entry, [flight], n=0, confidence=UNSURE, received=hours(3)
            ).outcomes
        card = tg.sent[-1][0]
        page = hours(1.5).isoformat(timespec="seconds")
        assert ctx.trips.looks_right(o.trip_item_id, at=page).checks == ()
        deliver(
            ctx, monkeypatch, entry, [later], n=1, doc="change", confidence=UNSURE,
            received=hours(1),
        )
        before = ctx.trips.item(o.trip_item_id)
        assert before.checks == ("low_confidence",)
        made = int(hours(2).timestamp())
        answered, _, left = tap(bot, tg, f"ck:{before.id}:ok:{made}", text=card)
        assert answered == NOTHING_LEFT and ctx.trips.item(before.id) == before
        assert before.payload["looked_at"] == page
        assert buttons(left) == [[("Looks right", f"ck:{before.id}:ok")]]


def test_a_card_time_in_the_future_looks_no_later_than_now(monkeypatch, tmp_path):
    """A Looks right whose card says it was made later than now looks now: a doubt raised after
    the tap, before the time the button said, is still to check."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    later = edit(
        flight,
        departure_local=an_hour_later(leg["departure_local"]),
        arrival_local=an_hour_later(leg["arrival_local"]),
    )
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        (o,) = deliver(ctx, monkeypatch, entry, [flight], n=0, confidence=UNSURE).outcomes
        ahead = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        assert tap(bot, tg, f"ck:{o.trip_item_id}:ok:{int(ahead.timestamp())}")[0] == (
            "Marked as right"
        )
        looked = ctx.trips.item(o.trip_item_id).payload["looked_at"]
        assert looked <= dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        soon = ahead - dt.timedelta(minutes=30)
        deliver(
            ctx, monkeypatch, entry, [later], n=1, doc="change", confidence=UNSURE, received=soon
        )
        assert ctx.trips.item(o.trip_item_id).checks == ("low_confidence",)


def test_a_tap_leaves_the_card_with_what_is_still_to_check(monkeypatch, tmp_path):
    """A flight that names nobody, forwarded from a sender no list vouches for: once the owner
    says it is theirs, the sender is still to check, so the card keeps Looks right and the
    flight waits; Looks right sends it and leaves the card with no button."""
    entry, flight = owners_flight()
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        (o,) = deliver(ctx, monkeypatch, entry, [nameless(flight)], unvouched=True).outcomes
        assert o.checks == ("owner_unknown",)
        card, keyboard = tg.sent[-1]
        me = f"ck:{o.trip_item_id}:me"
        assert buttons(keyboard) == [[("I was on this flight", me), ("Not me", me[:-2] + "no")]]
        answered, edited, left = tap(bot, tg, me, text=card)
        ti = ctx.trips.item(o.trip_item_id)
        assert answered == "Added you to this flight"
        assert (ti.checks, ti.sync_state) == (("unverified_sender",), "blocked")
        assert edited == f"{check_card(ti, ctx.book)}\n\n{answered}"
        assert checks.LABELS["unverified_sender"] in edited
        assert buttons(left) == [[("Looks right", f"ck:{ti.id}:ok")]]
        assert ctx.airtrail.flights == [] and ctx.airtrail.requests == []
        answered, edited, left = tap(bot, tg, ok_of(left), text=edited)
        ti = ctx.trips.item(o.trip_item_id)
        assert (answered, left, ti.checks, ti.sync_state) == ("Marked as right", None, (), "synced")
        assert edited == f"{check_card(ti, ctx.book)}\n\n{answered}"
        (there,) = ctx.airtrail.flights
        assert there["id"] == ti.airtrail_flight_id


def test_looks_right_on_a_flight_that_names_nobody_sends_nothing(monkeypatch, tmp_path):
    """Looks right closes the other doubts and never the question who was on the flight: it
    stays marked and nothing reaches the flight log."""
    entry, flight = owners_flight()
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        (o,) = deliver(ctx, monkeypatch, entry, [nameless(flight)], confidence=UNSURE).outcomes
        assert o.checks == ("low_confidence", "owner_unknown")
        card, keyboard = tg.sent[-1]
        answered, _, left = tap(bot, tg, ok_of(keyboard), text=card)
        ti = ctx.trips.item(o.trip_item_id)
        assert answered == "Marked as right"
        assert (ti.checks, ti.sync_state) == (("owner_unknown",), "blocked")
        assert [label for row in buttons(left) for label, _ in row] == [
            "I was on this flight",
            "Not me",
        ]
        assert ctx.airtrail.flights == [] and ctx.airtrail.requests == []


def test_a_button_names_a_booking_no_other_booking_is_given_after_it_goes(monkeypatch, tmp_path):
    """A booking's id is never given to another: a draft that becomes one with the booking it is,
    and a booking purged from Recently deleted, leave their ids unused, so a button on an old
    card finds nothing to do, changes nothing and sends nothing."""
    stay_entry, stay = booking("lodging")
    vague = edit(stay, {"confirmation_code": None, "property_name": None, "provider": None})
    car_entry, car = booking("car")
    entry, flight = owners_flight()
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        deliver(ctx, monkeypatch, stay_entry, [stay], n=0)
        (d,) = deliver(ctx, monkeypatch, stay_entry, [vague], n=1).outcomes
        draft = d.trip_item_id
        assert is_draft(ctx.trips.item(draft).item_key)
        # the owner gives the draft its code: it is the stay, and becomes one with it
        ctx.trips.edit_item(draft, {"confirmation_code": stay["confirmation_code"]})
        assert ctx.trips.item(draft) is None
        (o,) = deliver(ctx, monkeypatch, entry, [nameless(flight)], n=2).outcomes
        assert o.trip_item_id > draft and o.checks == ("owner_unknown",)
        before = ctx.trips.item(o.trip_item_id)
        answered, _, left = tap(bot, tg, f"ck:{draft}:me")
        assert (answered, left) == (NOTHING_LEFT, None)
        assert ctx.trips.item(o.trip_item_id) == before and ctx.airtrail.requests == []
        # a booking purged from Recently deleted, and its trip with it
        (c,) = deliver(ctx, monkeypatch, car_entry, [car], n=3).outcomes
        gone = ctx.trips.item(c.trip_item_id)
        ctx.trips.remove_item(gone.id)
        month = dt.datetime.now(dt.UTC) + dt.timedelta(days=40)
        assert ctx.trips.purge_deleted(now=month) == (1, 1)
        assert ctx.trips.item(gone.id) is None and ctx.ledger.trips.get(gone.trip_id) is None
        (again,) = deliver(ctx, monkeypatch, car_entry, [car], n=4).outcomes
        filed = ctx.trips.item(again.trip_item_id)
        assert filed.id > gone.id and filed.trip_id > gone.trip_id
        assert tap(bot, tg, f"ck:{gone.id}:me")[0] == NOTHING_LEFT
        assert ctx.trips.item(o.trip_item_id) == before and ctx.airtrail.requests == []


def test_a_booking_the_owner_cannot_see_gets_no_card_and_no_button_acts_on_it(
    monkeypatch, tmp_path
):
    """A booking in a deleted trip is out of sight: /check does not list it, a button on its old
    card does nothing, and a message that marks it sends its lines but no card for it."""
    entry, flight = owners_flight()
    stay_entry, stay = booking("lodging")
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        (o,) = deliver(ctx, monkeypatch, entry, [flight], n=0, confidence=UNSURE).outcomes
        card, keyboard = tg.sent[-1]
        ctx.trips.delete_trip(ctx.trips.item(o.trip_item_id).trip_id)
        before = ctx.trips.item(o.trip_item_id)
        assert tap(bot, tg, ok_of(keyboard), text=card)[0] == NOTHING_LEFT
        assert ctx.trips.item(o.trip_item_id) == before
        assert say(bot, tg, "/check") == [("Nothing to check.", None)]
        # a cancellation that names nobody, under another code, marks it where it is
        notice = edit(
            flight,
            {
                "status": "cancelled",
                "passengers": [],
                "confirmation_code": stay["confirmation_code"],
            },
            passengers=[],
        )
        sent = len(tg.sent)
        (n,) = deliver(ctx, monkeypatch, entry, [notice], n=1, doc="cancellation").outcomes
        assert (n.result, n.action.reason) == ("ignored", "unknown_cancellation")
        assert "unknown_cancellation" in ctx.trips.item(o.trip_item_id).checks
        ((lines, markup),) = tg.sent[sent:]  # the message's lines alone
        assert markup is None and checks.LABELS["unknown_cancellation"] in lines


def test_the_owners_two_answers_from_buttons(monkeypatch, tmp_path):
    """I was on this flight names the owner on it and sends it; Not me keeps it out of the flight
    log. Each is answered once. With no owner named in the travellers file there is nobody to
    add and no flight log kept: the page offers no answer, so a tap does nothing."""
    (entry, flight), (other, second) = owners_flights(2)
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        yes = deliver(ctx, monkeypatch, entry, [nameless(flight)], n=0).outcomes[0]
        no = deliver(ctx, monkeypatch, other, [nameless(second)], n=1).outcomes[0]
        for o in (yes, no):
            assert (o.checks, o.sync) == (("owner_unknown",), "blocked")
        assert tap(bot, tg, f"ck:{yes.trip_item_id}:me")[0] == "Added you to this flight"
        assert tap(bot, tg, f"ck:{no.trip_item_id}:no")[0] == "Noted: not your flight"
        on, off = ctx.trips.item(yes.trip_item_id), ctx.trips.item(no.trip_item_id)
        assert ctx.book.classify(on.passengers).me_present
        assert (on.checks, on.sync_state) == ((), "synced")
        assert (off.checks, off.sync_state, off.passengers) == ((), "off", [])
        (there,) = ctx.airtrail.flights
        assert there["id"] == on.airtrail_flight_id
        for o in (yes, no):
            for verdict in ("me", "no"):
                assert tap(bot, tg, f"ck:{o.trip_item_id}:{verdict}")[0] == NOTHING_LEFT
        assert len(ctx.airtrail.flights) == 1
    empty = tmp_path / "no-owner"
    empty.mkdir()
    with filing(flight_log=False, config_dir=empty) as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        assert ctx.book.me_name == ""
        (o,) = deliver(ctx, monkeypatch, entry, [nameless(flight)]).outcomes
        before = ctx.trips.item(o.trip_item_id)
        answered, edited, _ = tap(bot, tg, f"ck:{o.trip_item_id}:me")
        assert answered == NOTHING_LEFT and edited.endswith(f"\n\n{NOTHING_LEFT}")
        assert ctx.trips.item(o.trip_item_id) == before


def test_send_again_from_a_button(monkeypatch, tmp_path):
    """A flight deleted by hand in the flight log is held out of it; Send again brings it back,
    once."""
    entry, flight = owners_flight()
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        kept = ctx.trips.airtrail_on()
        (o,) = deliver(ctx, monkeypatch, entry, [flight]).outcomes
        (there,) = ctx.airtrail.flights
        ctx.airtrail.deleted_by_hand(there["id"])
        held = ctx.trips.sync_detached(o.trip_item_id)  # what a pass does on finding it gone
        assert (held.sync_hold, held.sync_state) == ("deleted", "off")
        again = [[("Send again", f"ck:{held.id}:again")]]
        assert kept and buttons(check_keyboard(held, flight_log=kept)) == again
        ctx.airtrail.requests.clear()
        assert tap(bot, tg, f"ck:{held.id}:again")[0] == "It will be sent again"
        back = ctx.trips.item(held.id)
        assert (back.sync_hold, back.sync_state) == (None, "synced")
        # looked for in the flight log before it is made there again
        asked = [r[0] for r in ctx.airtrail.requests if r[0] in ("find", "create", "update")]
        (made,) = ctx.airtrail.flights
        assert asked == ["find", "create"] and back.airtrail_flight_id == made["id"]
        assert check_keyboard(back, flight_log=kept) is None
        assert tap(bot, tg, f"ck:{held.id}:again")[0] == NOTHING_LEFT


def test_a_button_the_page_would_not_offer_does_nothing(monkeypatch, tmp_path):
    """A button acts only while the page offers the same answer (`checks.actions`, one rule for
    both). Once the flight log is switched off, Send again on a flight held as deleted there, and
    I was on this flight or Not me on a flight that names nobody, answer that nothing is left and
    change nothing."""
    (entry, flight), (other, second) = owners_flights(2)
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        (nobody,) = deliver(ctx, monkeypatch, other, [nameless(second)], n=1).outcomes
        assert nobody.checks == ("owner_unknown",)
        (o,) = deliver(ctx, monkeypatch, entry, [flight]).outcomes
        (there,) = ctx.airtrail.flights
        ctx.airtrail.deleted_by_hand(there["id"])
        held = ctx.trips.sync_detached(o.trip_item_id)
        note_airtrail(ctx.trips, ctx.ledger, on=False, today=util.now().date())
        for item_id, what in ((held.id, "again"), (nobody.trip_item_id, "me")):
            for button in (what, "no") if what == "me" else (what,):
                before = ctx.trips.item(item_id)
                kept = ctx.trips.airtrail_on()
                assert check_keyboard(before, flight_log=kept) is None, button
                assert tap(bot, tg, f"ck:{item_id}:{button}")[0] == NOTHING_LEFT, button
                assert ctx.trips.item(item_id) == before, button


def test_no_line_and_no_button_names_the_flight_log_when_there_is_none(monkeypatch, tmp_path):
    """With no flight log kept, nothing the bot sends names AirTrail or a flight log: a message's
    lines and cards, /check, /status, /help, /share, a tap's answer, the card it edits, and every
    button. With one, a marked flight's line says once that it is not sent until it is checked."""
    entry, flight = owners_flight()
    for kept in (False, True):
        with filing(flight_log=kept) as ctx:
            bot, tg = bot_for(ctx, tmp_path)
            (o,) = deliver(ctx, monkeypatch, entry, [flight], confidence=UNSURE).outcomes
            assert o.checks == ("low_confidence",)
            card, keyboard = tg.sent[-1]
            for command in ("/check", "/status", "/help", "/share", f"/share {o.trip_id}"):
                say(bot, tg, command)
            tap(bot, tg, ok_of(keyboard), text=card)
            texts = [text for text, _ in tg.sent]
            markups = [k for _, k in tg.sent]
            for method, params in tg.api_calls:
                if method in ("answerCallbackQuery", "editMessageText"):
                    texts.append(params["text"])
                    markups.append(params.get("reply_markup"))
            texts += [label for k in markups for row in buttons(k) for label, _ in row]
            named = [t for t in texts if "flight log" in t or "AirTrail" in t]
            if kept:
                assert [t for t in texts if CLAUSE in t] == [tg.sent[0][0]]
                assert tg.sent[0][0].count(CLAUSE) == 1
            else:
                assert texts and named == [], named


def test_the_old_commands_are_unknown_and_help_names_check(monkeypatch, tmp_path):
    with filing(flight_log=False) as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        for old in ("/review", "/approve 1", "/reject 1", "/fix 1 origin=x"):
            assert said(bot, tg, old) == f"unknown command {old.split()[0]}\n\n{HELP}"
        assert "/check" in HELP and not re.search(r"/(review|approve|reject|fix)\b", HELP)
        assert said(bot, tg, "/help") == HELP
        names = [c["command"] for c in COMMANDS]
        assert "check" in names and not {"review", "approve", "reject", "fix"} & set(names)
        # the commands Telegram offers are these
        monkeypatch.setattr(botmod.signal, "signal", lambda *args: None)
        monkeypatch.setattr(bot, "poll_once", lambda: _terminate(15, None))
        with pytest.raises(SystemExit):
            bot.run()
        assert [p["commands"] for m, p in tg.api_calls if m == "setMyCommands"] == [COMMANDS]


def test_bot_paste_flow(monkeypatch, tmp_path):
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        model_reads(monkeypatch, [flight])
        assert "Send the itinerary" in said(bot, tg, "/paste")
        say(bot, tg, f"{entry.name} itinerary")
        code = f"{leg['carrier_iata']}{leg['flight_number']}"
        assert any(code in t for t, _ in tg.sent)
        assert not ctx.ledger.kv.get(KV_PASTE)  # the paste window is closed


def test_a_paste_the_bot_was_stopped_while_reading_is_read_on_redelivery(monkeypatch, tmp_path):
    """SIGTERM while the pasted text is being read leaves the update unanswered (the offset not
    advanced) and the paste still pending, so the text Telegram delivers again is read."""
    entry, flight = owners_flight()
    leg = flight["segments"][0]
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        model_reads(monkeypatch, [flight])
        assert "Send the itinerary" in said(bot, tg, "/paste")
        pasted = {"update_id": 9, "message": {"chat": {"id": CHAT}, "text": f"{entry.name} it"}}
        monkeypatch.setattr(tg, "api", lambda method, **params: [pasted])
        reading = botmod.process_text
        calls = []

        def stopped_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                _terminate(15, None)
            return reading(*args, **kwargs)

        monkeypatch.setattr(botmod, "process_text", stopped_once)
        with pytest.raises(SystemExit):
            bot.poll_once()
        assert ctx.ledger.kv.get(botmod.KV_OFFSET, "0") in ("0", "")
        assert bot.poll_once() == 1
        code = f"{leg['carrier_iata']}{leg['flight_number']}"
        assert any(code in t for t, _ in tg.sent) and len(calls) == 2
        assert not ctx.ledger.kv.get(KV_PASTE)
        assert ctx.ledger.kv.get(botmod.KV_OFFSET) == "10"


def test_a_paste_that_waits_for_the_model_is_kept_and_status_counts_what_waits(
    monkeypatch, tmp_path
):
    """A paste only the model can read, while it cannot, is kept: the bot says so and why — no
    model is set up, its budget is used, or it cannot be reached. /status counts the mail that
    waits for the model."""
    from tripbox.parse import budget
    from tripbox.parse import pipeline as pl
    from tripbox.parse.provider import ModelError

    class Down:
        """A model that cannot be reached."""

        model = "stand-in"

        def extract(self, nm, hint=None):
            raise ModelError("unavailable", "no connection", word="unreachable")

    entry, _ = owners_flight()
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, context: [])
    with filing(flight_log=False) as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        assert "waiting for the model: 0" in said(bot, tg, "/status").splitlines()
        assert ctx.model is None
        kept = say(bot, tg, f"/paste {entry.name} itinerary")
        assert kept == [("Kept. No model is set up, so it waits until one is.", None)]
        ctx.settings = dataclasses.replace(ctx.settings, llm_monthly_budget_usd=0.0)
        ctx.model = budget.Budgeted(Down(), budget.Budget(ctx.ledger, ctx.settings))
        kept = say(bot, tg, f"/paste {entry.name} itinerary again")
        assert kept[-1] == (
            "Kept. The model's budget is used; it is read when the budget allows.",
            None,
        )
        ctx.model = Down()
        kept = say(bot, tg, f"/paste {entry.name} itinerary once more")
        assert kept[-1] == (
            "Kept. The model cannot be reached; it is tried again every few minutes.",
            None,
        )
        status = said(bot, tg, "/status").splitlines()
        assert "waiting for the model: 3" in status
        assert status[-1].startswith("this month: model ")
        assert ctx.ledger.messages.waiting() == {"no_model": 1, "over_budget": 1, "model_down": 1}


def test_share_lists_trips_and_renders_one(monkeypatch, tmp_path):
    entry, res = next(
        (e, r)
        for e in the_corpus().entries
        for r in e.truth["reservations"]
        if r["kind"] == "flight" and len(r.get("segments") or []) == 2 and owners(plain(r))
    )
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        deliver(ctx, monkeypatch, entry, [whole(res)])
        (trip,) = ctx.trips.all_trips()
        listed = said(bot, tg, "/share")
        assert listed.startswith(f"#{trip.id} {trip.title} · ") and listed.endswith(" · ✈️2")
        ctx.settings = dataclasses.replace(ctx.settings, link_base=LINK_BASE)
        assert said(bot, tg, "/share").endswith(f"✈️2 {LINK_BASE}/trips/{trip.id}")
        full = said(bot, tg, f"/share {trip.id}")
        assert full.startswith(f"{trip.title} · ") and "\n\n" in full
        owner = ctx.book.me_name.split()[0]
        assert re.search(rf"conf {first(res['confirmation_code'])} · {owner}\b", full)
        assert "https://" not in full
        assert said(bot, tg, f"/share {trip.title.lower()}") == full
        assert said(bot, tg, f"/share #{trip.id}") == full
        assert said(bot, tg, "/share nope").startswith("No trip matches")
        assert said(bot, tg, "/upcoming") == full
        assert "/share" in said(bot, tg, "/help")


def test_paste_expires_and_files_get_a_reply(tmp_path):
    with filing(flight_log=False) as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        assert "next message" in said(bot, tg, "/paste")
        stale = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30)).isoformat()
        ctx.ledger.kv.set(KV_PASTE, stale)
        assert "Not a command" in said(bot, tg, "some stray text")  # the paste window closed
        bot.handle_update(
            {"update_id": 3, "message": {"chat": {"id": CHAT}, "document": {"file_name": "x.pdf"}}}
        )
        reply = tg.sent[-1][0]
        assert "text" in reply.lower() and "/paste" in reply
        assert "trips mailbox" in reply and "@" not in reply


def test_sigterm_handler_stops_the_bot_cleanly():
    with pytest.raises(SystemExit):
        _terminate(15, None)


def test_bot_answers_even_when_airtrail_is_unreachable(monkeypatch, tmp_path):
    entry, flight = owners_flight()
    with filing() as ctx:
        bot, tg = bot_for(ctx, tmp_path)
        (o,) = deliver(ctx, monkeypatch, entry, [nameless(flight)]).outcomes
        ctx.airtrail.fail_next(0, times=3)  # no answer from the flight log
        answered, edited, _ = tap(bot, tg, f"ck:{o.trip_item_id}:me", card=78)
        assert answered == "Added you to this flight" and edited.endswith(answered)
        # the booking is filed all the same, and its flight waits for the flight log
        filed = ctx.trips.item(o.trip_item_id)
        assert (filed.sync_state, filed.sync_error) == ("pending", "outage")
        assert ctx.airtrail.flights == []
        # a malformed callback is answered, never raised
        for data in ("ck:x", "ck:²:me"):  # a digit that is no plain digit is no id
            malformed = {"id": "cq2", "data": data, "message": {"chat": {"id": CHAT}}}
            bot.handle_update({"update_id": 6, "callback_query": malformed})
            assert tg.api_calls[-1] == ("answerCallbackQuery", {"id": "cq2", "text": "bad button"})

