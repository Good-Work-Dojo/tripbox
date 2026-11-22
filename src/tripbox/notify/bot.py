# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Telegram long-poll bot: /check and its buttons, /paste, /status, /upcoming, /share. One
consumer only."""

from __future__ import annotations

import datetime as dt
import logging
import re
import signal
import time
from pathlib import Path

from tripbox.notify.telegram import Telegram, check_keyboard
from tripbox.notify.text import WAITING_WHY, check_card
from tripbox.pipeline.run import Context, process_text, sync
from tripbox.trips import display
from tripbox.trips import format as fmt
from tripbox.trips.store import TripItem
from tripbox.util import month_key, now_iso, parse_iso, today_local
from tripbox.util import now as now_utc
from tripbox.validate import checks

log = logging.getLogger("tripbox.bot")

HELP = (
    "tripbox commands:\n"
    "/status — last poll, counts, bookings to check, spend\n"
    "/check — the bookings marked to check, each with its buttons\n"
    "/paste — then send itinerary text (or /paste <text>)\n"
    "/upcoming — upcoming trips, ready to paste\n"
    "/share — list trips; /share 10 or /share paris — one trip as text (incl. past trips)\n"
)
# The commands Telegram offers in the chat's menu.
COMMANDS = [
    {"command": "status", "description": "Pipeline status"},
    {"command": "check", "description": "Bookings to check"},
    {"command": "paste", "description": "Ingest pasted itinerary text"},
    {"command": "upcoming", "description": "Upcoming trips"},
    {"command": "share", "description": "A trip as text: /share, /share N"},
    {"command": "help", "description": "Commands"},
]
CHECK_LIMIT = 10  # /check lists at most this many bookings; it says how many more there are
NOTHING_LEFT = "Nothing left to do for this one"
PLAIN_NUMBER = re.compile(r"[0-9]{1,18}")  # a booking's id or a time in a button: ASCII digits
# What each button (`ck:<id>:<what>`) answers once it is done.
DONE = {
    "ok": "Marked as right",
    "me": "Added you to this flight",
    "no": "Noted: not your flight",
    "again": "It will be sent again",
}
# The answer of `checks.actions` each button gives.
BUTTON_ACTION = {"ok": "looks_right", "me": "on_it", "no": "on_it", "again": "send_again"}
KV_OFFSET = "telegram_offset"
KV_PASTE = "telegram_paste_pending"  # ISO time of the /paste, or empty
PASTE_WINDOW = dt.timedelta(minutes=10)  # after that, stray text is not an itinerary
API_RETRY_SLEEP = 5.0  # getUpdates failed (network, 409, 5xx): wait instead of hammering
NOT_TEXT = ("document", "photo", "video", "audio", "voice", "sticker", "animation")


def _terminate(signum, frame) -> None:  # noqa: ARG001
    """SIGTERM (every `docker compose up -d`): leave at once instead of being SIGKILLed after
    the grace period. The update being handled is re-delivered on restart (offset not yet
    advanced), where the ledger dedupe and a button that has nothing left to do make it a
    no-op."""
    raise SystemExit(0)


class Bot:
    def __init__(self, ctx: Context, telegram: Telegram, heartbeat: Path) -> None:
        self.ctx = ctx
        self.tg = telegram
        self.heartbeat = heartbeat
        self.chat_id = str(ctx.settings.telegram_chat_id)

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        signal.signal(signal.SIGTERM, _terminate)
        self.tg.api("setMyCommands", commands=COMMANDS)
        log.info("bot started (chat %s…)", self.chat_id[:3])
        while True:
            self.heartbeat.touch()
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("bot loop error; sleeping 10s")
                time.sleep(10)

    def poll_once(self) -> int:
        offset = int(self.ctx.ledger.kv.get(KV_OFFSET, "0") or 0)
        updates = self.tg.api(
            "getUpdates",
            offset=offset,
            timeout=50,
            allowed_updates=["message", "callback_query"],
        )
        if updates is None:  # the API call itself failed (already logged by Telegram.api)
            time.sleep(API_RETRY_SLEEP)
            return 0
        for u in updates:
            try:
                self.handle_update(u)
            except Exception:  # noqa: BLE001
                log.exception("update %s failed", u.get("update_id"))
            self.ctx.ledger.kv.set(KV_OFFSET, str(int(u["update_id"]) + 1))
        return len(updates)

    # ------------------------------------------------------------------ dispatch
    def handle_update(self, u: dict) -> None:
        """One update, every reply said in the owner's style (`trips.display`)."""
        with fmt.styled(display.read(self.ctx.ledger)):
            self._handle(u)

    def _handle(self, u: dict) -> None:
        if "callback_query" in u:
            self._callback(u["callback_query"])
            return
        msg = u.get("message")
        if not msg:
            return
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            log.warning("ignoring message from foreign chat %s", (msg.get("chat") or {}).get("id"))
            return
        text = (msg.get("text") or "").strip()
        if not text:
            if any(k in msg for k in NOT_TEXT):
                self.tg.send(
                    "I can only read text here. /paste the itinerary as text, or forward the"
                    " email itself to your trips mailbox."
                )
            return
        if text.startswith("/"):
            self._command(text)
        elif self._paste_pending():
            self.ctx.ledger.kv.set(KV_PASTE, "")
            self._ingest_text(text)
        else:
            self.tg.send("Not a command. /paste first if that was an itinerary, or /help.")

    def _paste_pending(self) -> bool:
        stamp = self.ctx.ledger.kv.get(KV_PASTE) or ""
        if not stamp:
            return False
        try:
            return now_utc() - parse_iso(stamp) <= PASTE_WINDOW
        except ValueError:
            return False

    def _command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/start", "/help"):
            self.tg.send(HELP)
        elif cmd == "/status":
            self.tg.send(self.status_text())
        elif cmd == "/check":
            for card, keyboard in self.check_text():
                self.tg.send(card, reply_markup=keyboard)
        elif cmd == "/paste":
            if arg:
                self._ingest_text(arg)
            else:
                self.ctx.ledger.kv.set(KV_PASTE, now_iso())
                self.tg.send("Send the itinerary text as your next message (within 10 minutes).")
        elif cmd == "/upcoming":
            from tripbox.report.upcoming import build_upcoming

            self.tg.send(build_upcoming(self.ctx))
        elif cmd == "/share":
            self.tg.send(self.share_text(arg))
        else:
            self.tg.send(f"unknown command {cmd}\n\n{HELP}")

    # ------------------------------------------------------------------ actions
    def _ingest_text(self, text: str) -> None:
        res = process_text(self.ctx, text, source="telegram", notify=True)
        if res.status == "skipped":
            self.tg.send(f"Seen that text before ({res.skipped_reason}).")
        elif res.status == "waiting":  # the resume pass of a later poll reads it
            self.tg.send(f"Kept. {WAITING_WHY[res.wait_reason]}")
        elif not res.outcomes:
            self.tg.send(f"Parsed nothing usable ({res.status}).")

    def _offered(self, ti: TripItem | None, what: str) -> bool:
        """Whether the page offers, for the booking `ti`, what the button `what` does: the one
        rule for both is `checks.actions`, so a tap acts exactly while the page would. A
        booking the owner cannot see (it is gone, removed, or in a deleted trip: `ti` None)
        offers nothing."""
        if ti is None:
            return False
        can = checks.actions(ti.checks, hold=ti.sync_hold, flight_log=self.ctx.trips.airtrail_on())
        return BUTTON_ACTION.get(what) in can

    def _answer(self, item_id: int, what: str, made: str | None) -> str:
        """Do what the button `what` says for booking `item_id` through the trip store, then bring
        the flight log in step; the one line it is answered with. A button the page would not
        offer for the booking now (`_offered`) does nothing. Looks right is the owner's word
        on the card made at `made`: what an email raised after it stays open, and when it closes
        nothing (all that is open came after the card, or after a later look) nothing is left
        for it to do. Never raises: what the store refuses is answered with its sentence."""
        store = self.ctx.trips
        before = store.visible(item_id)
        if not self._offered(before, what):
            return NOTHING_LEFT
        try:
            if what == "ok":
                after = store.looks_right(item_id, at=made)
                if before is not None and set(before.checks) <= set(after.checks):
                    return NOTHING_LEFT
            elif what in ("me", "no"):
                store.owner_answer(item_id, what == "me")
            else:
                store.send_again(item_id)
        except KeyError:
            return NOTHING_LEFT  # gone between the look and the write
        except ValueError as exc:
            return str(exc)
        except Exception:  # noqa: BLE001 - whatever broke, the person gets an answer
            log.exception("booking #%s: button %s failed", item_id, what)
            return "That did not work; try again later"
        sync(self.ctx)  # its error is logged there; the booking is changed all the same
        return DONE[what]

    @staticmethod
    def _made(seconds: str) -> str | None:
        """The time a card was made, from the seconds since the epoch its Looks right button
        carries, as the ledger writes a time, and never later than now (a look never moves back,
        so a time ahead would pass findings unseen until then); None when it is no number of
        plain digits."""
        if not PLAIN_NUMBER.fullmatch(seconds):
            return None
        try:
            made = dt.datetime.fromtimestamp(int(seconds), dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return min(made, now_utc()).isoformat(timespec="seconds")

    def _callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        card = cq.get("message") or {}
        chat = str((card.get("chat") or {}).get("id"))
        if chat != self.chat_id or not data.startswith("ck:"):
            self.tg.answer_callback(cq["id"], "ignored")
            return
        parts = data.split(":")
        if len(parts) < 3 or not PLAIN_NUMBER.fullmatch(parts[1]) or parts[2] not in DONE:
            self.tg.answer_callback(cq["id"], "bad button")
            return
        item_id, what = int(parts[1]), parts[2]
        made = self._made(parts[3]) if what == "ok" and len(parts) == 4 else None
        if (what == "ok" and made is None) or (what != "ok" and len(parts) != 3):
            text = NOTHING_LEFT  # a Looks right that does not say when its card was made
        else:
            text = self._answer(item_id, what, made)
        self.tg.answer_callback(cq["id"], text[:180])
        mid = card.get("message_id")
        if not mid:
            self.tg.send(text)
            return
        # the card is the booking as it is now: what is still to check, with its buttons
        now = self.ctx.trips.visible(item_id)
        if now is None:
            self.tg.edit_text(mid, f"{card.get('text') or ''}\n\n{text}".strip())
            return
        shown = check_card(now, self.ctx.book, link_base=self.ctx.settings.link_base)
        keyboard = check_keyboard(now, flight_log=self.ctx.trips.airtrail_on())
        self.tg.edit_text(mid, f"{shown}\n\n{text}", reply_markup=keyboard)

    # ------------------------------------------------------------------ texts
    def share_text(self, arg: str) -> str:
        """/share -> the trip list; /share 10 | /share #10 | /share paris -> that trip's text."""
        st, s = self.ctx.trips, self.ctx.settings
        today = today_local()
        if not arg:
            return fmt.trip_list_text(st, today, link_base=s.link_base)
        key = arg.strip().lstrip("#")
        trip = None
        if key.isdigit():
            trip = st.get(int(key))
        else:
            hits = [t for t in st.all_trips() if t.title.lower().startswith(key.lower())]
            if len(hits) > 1:
                return "Several trips match: " + ", ".join(f"#{t.id} {t.title}" for t in hits)
            trip = hits[0] if hits else None
        if trip is None:
            return f"No trip matches {arg.strip()!r}. /share lists them."
        return fmt.trip_text(
            trip, st.items(trip.id), book=self.ctx.book, home_airports=s.home_airports
        )

    def status_text(self) -> str:
        led, s = self.ctx.ledger, self.ctx.settings
        kv = led.kv
        last_ok, last_try = kv.get("last_poll_ok"), kv.get("last_poll_attempt")

        def age(v):
            if not v:
                return "never"
            secs = (parse_iso(now_iso()) - parse_iso(v)).total_seconds()
            return f"{secs / 60:.0f} min ago"

        since = now_iso()[:10] + "T00:00:00+00:00"
        counts = led.messages.counts_since(since)
        n_llm, cost_llm = led.llm.month_cost(month_key())
        n_aero, cost_aero = led.aeroapi.month_total(month_key())
        marked = sum(self.ctx.trips.flag_counts().values())
        unsorted = len(self.ctx.trips.unsorted())
        waiting = sum(led.messages.waiting().values())
        head = "tripbox status"
        if self.ctx.trips.airtrail_on() and s.airtrail_dry_run:  # only when a flight log is kept
            head += " · dry run: flights are filed but not sent"
        return (
            f"{head}\n"
            f"last poll: ok {age(last_ok)}, attempted {age(last_try)}\n"
            f"today (UTC): {counts or 'nothing'}\n"
            f"to check: {marked} · unsorted: {unsorted}\n"
            f"waiting for the model: {waiting}\n"
            f"this month: model {n_llm} calls ${cost_llm:.3f} · "
            f"AeroAPI {n_aero} calls ${cost_aero:.3f}"
        )

    def check_text(self) -> list[tuple[str, dict | None]]:
        """/check: up to CHECK_LIMIT bookings marked to check, those in trips first by start, then
        those with no trip, each as its own message with its buttons, and a last line saying how
        many more the page holds; `Nothing to check.` when there is none. A booking whose card
        cannot be made is left out, its id logged."""
        marked = self.ctx.trips.flagged()
        if not marked:
            return [("Nothing to check.", None)]
        link_base = self.ctx.settings.link_base
        kept = self.ctx.trips.airtrail_on()
        out: list[tuple[str, dict | None]] = []
        for ti in marked[:CHECK_LIMIT]:
            try:
                card = check_card(ti, self.ctx.book, link_base=link_base)
                out.append((card, check_keyboard(ti, flight_log=kept)))
            except Exception:  # noqa: BLE001 - one booking must not hide the rest
                log.exception("booking #%s: its card could not be made", ti.id)
        if len(marked) > CHECK_LIMIT:
            out.append((f"and {len(marked) - CHECK_LIMIT} more on the page", None))
        return out
