# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Notifications (`notify.notifier`): one notice, every channel. Telegram receives what it always
has; ntfy one POST per notice that never names its address in a log; no channel, nothing built and
nothing sent. Every booking is the corpus's own, filed through the pipeline as `test_filed` files
it; every server is a stand-in inside the test."""

import dataclasses
import datetime as dt
import logging
import os

import httpx2
import pytest
from helpers.inbox import FakeInbox
from helpers.sample import CONFIG_DIR
from test_checks import the_corpus
from test_filed import deliver, filing, owners_flight
from test_store_actions import nameless

from tripbox import cli
from tripbox.config import Settings
from tripbox.enrich import enricher
from tripbox.log import add_secrets, setup_logging
from tripbox.notify.notifier import EVENTS, TITLES, Notice, Notifier, build
from tripbox.notify.ntfy import Ntfy
from tripbox.notify.telegram import Telegram, TelegramChannel, check_keyboard
from tripbox.notify.text import check_card, format_outcomes
from tripbox.pipeline import run as runmod
from tripbox.util import clock_at

ORIGIN = "https://trips.example.com"
LINK_BASE = f"{ORIGIN}/tripbox"  # TRIPBOX_BASE_URL, set
NTFY_URL = "https://ntfy.example/sample-family-trips-4c9f"
NTFY_TOKEN = "tk_samplefamily0123456789abcdefgh"
TELEGRAM_TOKEN = "123456789:" + "A" * 35
CHAT = "42"
BOTH = {
    "TRIPBOX_TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
    "TRIPBOX_TELEGRAM_CHAT_ID": CHAT,
    "TRIPBOX_NTFY_URL": NTFY_URL,
    "TRIPBOX_NTFY_TOKEN": NTFY_TOKEN,
}


class Recorder:
    """A channel that keeps every notice it is sent."""

    def __init__(self, name: str = "recorder") -> None:
        self.name = name
        self.notices: list[Notice] = []

    def send(self, notice: Notice) -> None:
        self.notices.append(notice)


class Broken:
    name = "broken"

    def send(self, notice: Notice) -> None:
        raise RuntimeError("the channel is down")


class _Reply:
    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


class Posts:
    """The HTTP client of a `Telegram`: every call it posts, by its method and parameters."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict | None = None) -> _Reply:
        self.posts.append((url.rsplit("/", 1)[-1], json or {}))
        return _Reply({"ok": True, "result": {"message_id": len(self.posts)}})


def ntfy(handler, token: str = NTFY_TOKEN) -> Ntfy:
    return Ntfy(NTFY_URL, token, http=httpx2.Client(transport=httpx2.MockTransport(handler)))


def every_level(caplog) -> None:
    """Capture every record at every level, the libraries' own levels as every command sets them
    (`log.setup_logging`): the HTTP library's request line, which names the address, stays
    out. Its level is set only there, so a command that let that line through fails here."""
    for name in ("httpx2", "httpcore"):
        caplog.set_level(logging.NOTSET, logger=name)  # what a new process has; restored after
    root = logging.getLogger()
    configured, kept = getattr(root, "_tripbox_configured", False), list(root.handlers)
    root._tripbox_configured = False  # type: ignore[attr-defined]
    try:
        setup_logging(None)
    finally:
        for handler in root.handlers[len(kept) :]:
            root.removeHandler(handler)
        root._tripbox_configured = configured  # type: ignore[attr-defined]
    caplog.set_level(logging.DEBUG)


def logged(caplog) -> str:
    return caplog.text + "\n".join(r.getMessage() for r in caplog.records)


def test_an_event_outside_the_list_is_refused():
    assert len(EVENTS) == 7 and set(TITLES) == set(EVENTS)
    assert all(title.isascii() and title for title in TITLES.values())  # an HTTP header
    for event in EVENTS:
        assert Notice(event, "x").event == event
    for event in ("sent", "FILED", "", "needs look"):
        with pytest.raises(ValueError):
            Notice(event, "x")


def test_every_channel_gets_the_notice_and_one_that_fails_stops_no_other(caplog):
    first, last = Recorder("first"), Recorder("last")
    notifier = Notifier([first, Broken(), last])
    assert [c.name for c in notifier.channels] == ["first", "broken", "last"]
    notice = Notice("failure", "tripbox could not process a message")
    caplog.set_level(logging.DEBUG)
    notifier.notify(notice)  # never raises
    assert first.notices == [notice] and last.notices == [notice]
    (said,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert said.name.startswith("tripbox") and "broken" in said.getMessage()
    assert "failure" in said.getMessage()
    caplog.clear()
    Notifier().notify(notice)  # no channel: nothing to do
    assert Notifier().channels == () and caplog.records == []


def test_ntfy_posts_title_body_and_click_and_never_logs_its_address(caplog):
    secret = "S3CRET-NTFY-BODY-7f2c"
    add_secrets([secret])
    every_level(caplog)
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"id": "sample"})

    channel = ntfy(answer)
    assert channel.name == "ntfy"
    link = f"{LINK_BASE}/trips/7#item-9"
    body = f"📬 Fwd: a booking ✈️\nthe flight log said: bad key {secret}"
    channel.send(Notice("filed", body, link=link))
    channel.send(Notice("filed", "🛬 enriched", quiet=True))
    ntfy(answer, token="").send(Notice("mail_up", "tripbox is reading the mailbox again"))
    loud, quiet, bare = seen
    for request in seen:
        assert request.method == "POST" and str(request.url) == NTFY_URL
        assert set(request.extensions["timeout"].values()) == {20.0}
    assert loud.headers["Title"] == TITLES["filed"] and loud.headers["Click"] == link
    assert loud.headers["Authorization"] == f"Bearer {NTFY_TOKEN}"
    assert "Priority" not in loud.headers
    assert loud.content == body.replace(secret, "[REDACTED]").encode("utf-8")
    assert quiet.headers["Priority"] == "low" and "Click" not in quiet.headers
    assert quiet.content == "🛬 enriched".encode()
    assert bare.headers["Title"] == TITLES["mail_up"] and "Authorization" not in bare.headers
    for value in (NTFY_URL, "ntfy.example", NTFY_TOKEN, secret):
        assert value not in logged(caplog), value


def test_ntfy_titles_a_notice_by_its_own_title_when_it_has_one():
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"id": "sample"})

    from tripbox.enrich.enricher import TITLE

    ntfy(answer).send(Notice("filed", "🛬 enriched", quiet=True, title=TITLE))
    ntfy(answer).send(Notice("filed", "📬 a booking"))
    titled, plain = seen
    assert titled.headers["Title"] == TITLE == "tripbox: flight details filled in"
    assert plain.headers["Title"] == TITLES["filed"]
    # a title is one header line of plain text
    for odd in ("tripbox: ✈ filled in", "tripbox:\nInjected: header"):
        with pytest.raises(ValueError, match="plain ASCII"):
            Notice("filed", "x", title=odd)


def test_ntfy_down_is_logged_and_never_raises(caplog):
    every_level(caplog)

    def refused(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text=f"no topic at {request.url}")

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot connect to {request.url}")

    for handler, why in ((refused, "HTTP 500"), (unreachable, "ConnectError")):
        caplog.clear()
        ntfy(handler).send(Notice("failure", "tripbox could not process a message"))
        (said,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert said.name == "tripbox.ntfy" and why in said.getMessage(), why
        assert "failure" in said.getMessage()
        for value in (NTFY_URL, "ntfy.example", NTFY_TOKEN):
            assert value not in logged(caplog), (why, value)


def test_a_notice_body_is_cut_before_any_channel_gets_it():
    """Every channel gets a notice's body cut to BODY_MAX characters, its last one `…`; a shorter
    one goes as it is. ntfy sends at most MAX_BYTES of it, cut on a character's boundary."""
    from tripbox.notify import ntfy as ntfy_module
    from tripbox.notify.notifier import BODY_MAX

    first, last = Recorder("first"), Recorder("last")
    Notifier([first, last]).notify(Notice("filed", "x" * (BODY_MAX + 10), link=ORIGIN))
    for channel in (first, last):
        (got,) = channel.notices
        assert got.body == "x" * (BODY_MAX - 1) + "…" and len(got.body) == BODY_MAX
        assert (got.event, got.link) == ("filed", ORIGIN)
    short = Notice("filed", "x" * BODY_MAX)
    Notifier([first]).notify(short)
    assert first.notices[-1] is short
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"id": "sample"})

    wide = "€"  # three bytes, so 4,096 falls inside one
    ntfy(answer).send(Notice("filed", wide * 1400))
    ntfy(answer).send(Notice("filed", wide * 1365))
    cut, whole = seen
    assert ntfy_module.MAX_BYTES == 4096
    assert cut.content == (wide * 1365).encode("utf-8") == whole.content


def test_telegram_gets_the_same_text_and_buttons_as_before(monkeypatch):
    """A message that leaves a booking marked reaches Telegram as it always has: the message's
    lines, then the booking's card with its buttons, neither silent; a failure and a flight-log
    alert as their text alone, and the enrichment lines silently. Another channel gets the same
    notices, each with the address of the booking on the page."""
    entry, flight = owners_flight()
    with filing() as ctx:
        ctx.settings = dataclasses.replace(ctx.settings, link_base=LINK_BASE)
        http, other = Posts(), Recorder()
        telegram = TelegramChannel(Telegram(TELEGRAM_TOKEN, CHAT, http=http))
        assert telegram.name == "telegram"
        ctx.notifier = Notifier([telegram, other])
        res = deliver(ctx, monkeypatch, entry, [nameless(flight)])
        (o,) = res.outcomes
        assert o.checks == ("owner_unknown",) and ctx.trips.airtrail_on()
        ti = ctx.trips.visible(o.trip_item_id)
        lines = format_outcomes(
            res.normalized.subject,
            res.outcomes,
            parser=res.parsed.parser,
            link_base=LINK_BASE,
            flight_log=True,
        )
        plain = {"chat_id": CHAT, "disable_web_page_preview": True}
        assert http.posts == [
            ("sendMessage", {**plain, "text": lines}),
            (
                "sendMessage",
                {
                    **plain,
                    "text": check_card(ti, ctx.book, link_base=LINK_BASE),
                    "reply_markup": check_keyboard(ti, flight_log=True),
                },
            ),
        ]
        page = f"{LINK_BASE}{ti.page}"
        assert [(n.event, n.body, n.link) for n in other.notices] == [
            ("filed", lines, page),
            ("needs_look", check_card(ti, ctx.book, link_base=LINK_BASE), page),
        ]
        assert other.notices[1].booking == ti and other.notices[1].flight_log is True
    http.posts.clear()
    for notice in (Notice("failure", "⚠️ failed"), Notice("sync_error", "the flight log")):
        telegram.send(notice)
    telegram.send(Notice("filed", "🛬 enriched", quiet=True))
    assert http.posts == [
        ("sendMessage", {**plain, "text": "⚠️ failed"}),
        ("sendMessage", {**plain, "text": "the flight log"}),
        ("sendMessage", {**plain, "text": "🛬 enriched", "disable_notification": True}),
    ]


def test_no_channel_means_no_client_and_nothing_sent(monkeypatch):
    built: list[str] = []

    def sentinel(name: str):
        def construct(*args, **kwargs):
            built.append(name)
            raise AssertionError(f"a {name} was built")

        return construct

    base = {"TRIPBOX_AUTH": "none"}
    off = Settings.from_env(base)
    both = Settings.from_env({**base, **BOTH})
    assert not off.on("notifications") and both.telegram_on and both.ntfy_on
    with monkeypatch.context() as patched:
        for owner, name in ((httpx2, "Client"), (Telegram, "__init__"), (Ntfy, "__init__")):
            patched.setattr(owner, name, sentinel(f"{owner.__name__}.{name}"))
        for settings, offline in ((off, False), (off, True), (both, True)):
            notifier = build(settings, offline=offline)
            assert notifier.channels == (), (settings.on("notifications"), offline)
            notifier.notify(Notice("failure", "x"))
    assert built == []
    # the same settings online: a client per channel, and building one reaches nothing
    assert [c.name for c in build(both, offline=False).channels] == ["telegram", "ntfy"]


def test_the_mail_down_notice_is_sent_once_and_mail_up_once():
    """Three failed polls an hour apart after a good one: one notice once no poll has been good
    for an hour, saying since when; the next good poll says the mailbox is read again. One
    failure between good polls says nothing."""
    start = dt.datetime.combine(the_corpus().anchor, dt.time(6), tzinfo=dt.UTC)
    hour = dt.timedelta(hours=1)
    error = "login failed: " + "x" * 400
    with filing(flight_log=False) as ctx:
        recorder = Recorder()
        ctx.notifier = Notifier([recorder])
        with clock_at(start):
            runmod.poll(ctx, FakeInbox())
        for n in (1, 2, 3):
            with clock_at(start + n * hour):
                runmod.poll_failed(ctx, RuntimeError(error))
        with clock_at(start + 4 * hour):
            runmod.poll(ctx, FakeInbox())
        with clock_at(start + 4 * hour + dt.timedelta(minutes=5)):
            runmod.poll_failed(ctx, RuntimeError("blip"))
        since = start.isoformat(timespec="seconds")
        assert [(n.event, n.body) for n in recorder.notices] == [
            ("mail_down", f"tripbox has not reached the mailbox since {since}: {error[:300]}"),
            ("mail_up", "tripbox is reading the mailbox again"),
        ]
    with filing(flight_log=False) as ctx:  # no poll was ever good
        recorder = Recorder()
        ctx.notifier = Notifier([recorder])
        runmod.poll_failed(ctx, RuntimeError("refused"))
        assert [n.body for n in recorder.notices] == [
            "tripbox has not reached the mailbox since it started: refused"
        ]


def test_a_secret_across_the_cut_is_masked_before_a_text_is_cut(monkeypatch):
    """Text cut for a log line or a notice is masked first, so a secret that runs across the cut
    leaves nothing of itself: the mailbox notice's error, a failure notice's error and header
    text as a log line or a notice shows it."""
    from tripbox import log
    from tripbox.notify.text import format_failure

    monkeypatch.setattr(log, "_SECRETS", set())
    secret = "S3CRET-ACROSS-THE-CUT-7f2c"
    add_secrets([secret])

    def across(cut: int) -> str:
        return "x" * (cut - 10) + secret + " and more"

    def nothing_of_it(text: str) -> bool:
        return not any(secret[:n] in text for n in range(4, len(secret) + 1))

    masked = "[REDACTED]"
    with filing(flight_log=False) as ctx:
        recorder = Recorder()
        ctx.notifier = Notifier([recorder])
        runmod.poll_failed(ctx, RuntimeError(across(300)))
        ((event, body),) = [(n.event, n.body) for n in recorder.notices]
    assert event == "mail_down" and nothing_of_it(body)
    assert body.endswith(": " + ("x" * 290 + masked)[:300])
    failure = format_failure("a subject", across(500))
    assert nothing_of_it(failure) and failure.endswith(("x" * 490 + masked)[:500])
    shown = runmod.shown(across(runmod.SUBJECT_SHOWN))
    assert nothing_of_it(shown) and shown == "x" * 190 + masked + "…"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Nothing configured but where the files go."""
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    return tmp_path


def test_the_bot_needs_telegram_whatever_else_is_set(env, monkeypatch, capsys):
    """The bot answers in a Telegram chat: notifications that go to ntfy alone give it none, and
    Telegram half set is said as the problem it is."""
    built = []
    monkeypatch.setattr(runmod, "build_context", lambda *args, **kwargs: built.append(args))
    monkeypatch.setenv("TRIPBOX_NTFY_URL", NTFY_URL)
    assert cli.main(["bot"]) == 2
    assert (
        "Telegram is off: TRIPBOX_TELEGRAM_BOT_TOKEN, TRIPBOX_TELEGRAM_CHAT_ID not set"
        in capsys.readouterr().err.splitlines()
    )
    monkeypatch.setenv("TRIPBOX_TELEGRAM_CHAT_ID", CHAT)
    assert cli.main(["bot"]) == 2
    assert (
        "TRIPBOX_TELEGRAM_BOT_TOKEN: not set — Telegram is half configured; set it, or remove"
        " TRIPBOX_TELEGRAM_CHAT_ID"
    ) in capsys.readouterr().err.splitlines()
    assert built == []


def test_a_flight_log_alert_and_the_enrichment_lines_are_notices(env, monkeypatch):
    """What the flight log's reconcile tells the owner is a `sync_error` notice; the enrichment
    run's lines worth a glance are a quiet `filed` one."""
    recorder = Recorder()
    monkeypatch.setattr(runmod, "build_notifier", lambda s, offline: Notifier([recorder]))
    for name, value in {
        "TRIPBOX_AIRTRAIL_URL": "http://flightlog.invalid",
        "TRIPBOX_AIRTRAIL_API_KEY": "k",
        "TRIPBOX_ENRICH": "aeroapi",
        "TRIPBOX_AEROAPI_KEY": "f",
        **BOTH,
    }.items():
        monkeypatch.setenv(name, value)
    ctx = runmod.build_context(Settings.from_env(), with_model=False)
    try:
        ctx.syncer.alert("Your flight log has not answered for over an hour")
    finally:
        ctx.ledger.close()
        ctx.airtrail.close()
    lines = ["🛬 the flight enriched: gate B4", "no flights to enrich"]
    monkeypatch.setattr(enricher, "enrich_recent", lambda ctx, api: lines)
    assert cli.main(["enrich"]) == 0
    assert recorder.notices == [
        Notice("sync_error", "Your flight log has not answered for over an hour"),
        Notice("filed", "🛬 the flight enriched: gate B4", quiet=True, title=enricher.TITLE),
    ]
