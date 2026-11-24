# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tripbox command line: `python -m tripbox <command>`."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from tripbox.config import (
    DISPLAY_ONLY,
    KEYS,
    MODEL_KEY_ENV,
    OFFLINE_ENV,
    PATH_KEYS,
    TELEGRAM_OFF,
    ConfigError,
    FeatureState,
    Settings,
    in_image,
    off_line,
    offline,
    offline_refusal,
    report,
)
from tripbox.ledger.db import OutdatedLedger
from tripbox.log import FILE_ONLY, add_secrets, setup_logging
from tripbox.util import use_zone

log = logging.getLogger("tripbox.cli")
# What SQLite says of a ledger the process cannot open or write (a folder or a file it does not
# own, a read-only mount), and the line `main` answers it with
LEDGER_REFUSED = ("readonly database", "unable to open database file")
CANNOT_OPEN = 'the ledger cannot be opened or written; "tripbox doctor" says why'
# The long-lived commands write their own log file (see log.setup_logging); the others share one.
LOG_NAMES = {"bot": "tripbox-bot", "web": "tripbox-web", "work": "tripbox-worker"}


def _refuse(s: Settings, *features: str) -> None:
    """A problem in one of these features stops the command (but one of a DISPLAY_ONLY key, a
    link shown, which turns nothing off): each one is a line of the error, which main prints as
    `config error: …`."""
    problems = s.stopping(*features)
    if problems:
        raise ConfigError("\n".join(str(p) for p in problems))


def _folders(s: Settings, *names: str) -> None:
    """A problem with one of these folders (`files.problems`) stops the command as a settings
    problem does."""
    from tripbox import files

    problems = files.problems(s, names)
    if problems:
        raise ConfigError("\n".join(str(p) for p in problems))


def _settings(args: argparse.Namespace, *, folders_only: bool = False) -> Settings:
    """The settings every command but `web` starts with: a core problem stops the command (with
    `folders_only`, for backup and restore, which read the folders and the ledger alone, only a
    problem of a folder's setting does, and the other core problems are logged); then the
    configured zone becomes the process's, the secrets are masked in the logs, and each problem
    of a feature but core and the page is logged once: one that leaves its feature off, and one
    of a DISPLAY_ONLY key, whose feature stays on; so is one that stops nothing, of any
    feature."""
    s = Settings.from_env()
    if folders_only:
        stopping = [p for p in s.stopping("core") if p.key in PATH_KEYS]
        if stopping:
            raise ConfigError("\n".join(str(p) for p in stopping))
    else:
        _refuse(s, "core")
    if getattr(args, "dry_run", False) and not s.airtrail_dry_run:
        # for this one run; what is configured stays the ledger's note of it (_context)
        s = dataclasses.replace(s, airtrail_dry_run=True)
        args.dry_run_from_flag = True
    use_zone(s.tz)
    _mask_secrets(s)
    command = getattr(args, "command", "") or ""
    setup_logging(
        s.logs_dir,
        name=LOG_NAMES.get(command, "tripbox"),
    )
    for p in s.problems:
        # a core problem that stops is the command's error, a web one the page's to tell
        told_elsewhere = p.feature in ("core", "web") and not (folders_only and p.feature == "core")
        if not told_elsewhere or not p.stops:
            log.warning("config: %s", p)
    return s


def _mask_secrets(s: Settings) -> None:
    """Each secret, as configured and as read (a value that fails its check is read as none), is
    masked in the logs."""
    add_secrets(os.environ.get(k.name) for k in KEYS if k.secret)
    add_secrets(getattr(s, k.field) for k in KEYS if k.secret and k.field is not None)


def _not_on(state: FeatureState) -> None:
    """Why a feature a command cannot do without is not on, to stderr: the off line, or each of
    its problems."""
    if state.state == "off":
        print(off_line(state), file=sys.stderr)
    for p in state.problems:
        print(p, file=sys.stderr)


def _refused_offline(what: str) -> bool:
    """While TRIPBOX_NO_NETWORK is set, a command that needs the network says so in one line,
    before it builds anything."""
    line = offline_refusal(what)
    if line:
        print(line, file=sys.stderr)
    return line is not None


def _context(args: argparse.Namespace, s: Settings, **kw):
    """The pipeline's context for a command that takes `--dry-run`: a run the flag alone made a
    dry run sends nothing to AirTrail and leaves the ledger's note of the setting as it is. `kw`
    goes to `build_context` (`with_notifier=False`: the run tells nobody)."""
    from tripbox.pipeline.run import build_context

    return build_context(s, dry_run_from_flag=getattr(args, "dry_run_from_flag", False), **kw)


# ----------------------------------------------------------------------------- commands
def cmd_poll(args: argparse.Namespace) -> int:
    """Read the mailbox (or, with mail off, only the rest of a poll): 0, or POLL_MESSAGE_FAILED
    when it was read and a message failed, 1 when it could not be read, which the worker that
    starts it records as different codes."""
    if _refused_offline("poll"):
        return 2
    from tripbox.mail.imap import ImapInbox
    from tripbox.pipeline.run import poll, poll_failed
    from tripbox.worker import POLL_MESSAGE_FAILED

    s = _settings(args)
    mail = s.feature("mail")
    if mail.state == "invalid":
        _not_on(mail)
        return 2
    ctx = _context(args, s)
    if mail.state == "off":  # no mailbox to read; the rest of the run (the purge) still is
        print(off_line(mail))
        summary = poll(ctx, None)
    else:
        try:
            inbox = ImapInbox(s.mail_host, s.mail_user, s.mail_password)
            try:
                summary = poll(ctx, inbox)
            finally:
                inbox.close()
        except Exception as exc:  # noqa: BLE001 - a dead login must be said once, not every 5 min
            log.exception("poll failed")
            poll_failed(ctx, exc)
            return 1
    print(
        f"poll: fetched={summary.fetched} {summary.counts} "
        f"errors={summary.errors} dry_run={s.airtrail_dry_run} in {summary.seconds:.1f}s"
    )
    return 0 if summary.errors == 0 else POLL_MESSAGE_FAILED


def cmd_ingest(args: argparse.Namespace) -> int:
    from tripbox.pipeline.run import process_raw, process_text
    from tripbox.uploads import email_of

    s = _settings(args)
    ctx = _context(args, s, with_notifier=not args.quiet)
    data = sys.stdin.buffer.read() if args.path == "-" else Path(args.path).read_bytes()
    mail = email_of(data)
    if mail is not None:
        res = process_raw(
            ctx, mail, source=args.source, force=args.force, notify=not args.quiet, trusted=True
        )
    else:
        res = process_text(
            ctx,
            data.decode("utf-8", errors="replace"),
            source=args.source,
            subject=args.subject or "(pasted itinerary)",
            notify=not args.quiet,
        )
    _print_result(res, s.airtrail_dry_run)
    return 0 if res.status not in ("failed",) else 1


def _print_result(res, dry_run: bool) -> None:
    """What `ingest` and `replay` print of a message's reading: a message that waits for the
    model says why in the words the owner is told it (`text.WAITING_WHY`)."""
    from tripbox.notify.text import WAITING_WHY

    extra = f" ({res.skipped_reason})" if res.skipped_reason else ""
    status = res.status
    if status == "waiting" and res.wait_reason in WAITING_WHY:
        status = f"waiting. {WAITING_WHY[res.wait_reason]}"
    print(f"message #{res.message_row_id}: {status}{extra}" + (" [dry-run]" if dry_run else ""))
    if res.parsed:
        print(f"  parser={res.parsed.parser} document_type={res.parsed.extraction.document_type}")
    for o in res.outcomes:
        it = o.action.item
        print(
            f"  - {o.result:9} {it.kind:7} {it.key}  {o.summary}"
            + (f"  sync={o.sync}" if o.sync and o.sync != "off" else "")
            + (f"  checks={','.join(o.checks)}" if o.checks else "")
            + (f"  !! {o.error}" if o.error else "")
        )


def cmd_replay(args: argparse.Namespace) -> int:
    from tripbox.mail.archive import iter_archive
    from tripbox.pipeline.run import process_raw

    s = _settings(args)
    ctx = _context(args, s, with_notifier=not args.quiet)
    files = [Path(args.file)] if args.file else iter_archive(s.archive_dir, args.since)
    if args.status:
        wanted = {r["archive_path"] for r in ctx.ledger.messages.by_status(args.status)}
        files = [f for f in files if str(f) in wanted]
    counts: dict[str, int] = {}
    for f in files:
        res = process_raw(
            ctx,
            f.read_bytes(),
            source="replay",
            force=args.force,
            notify=not args.quiet,
            reread=args.reread,
        )
        counts[res.status] = counts.get(res.status, 0) + 1
        if res.status != "skipped" or args.verbose:
            print(f"{f.name}: ", end="")
            _print_result(res, s.airtrail_dry_run)
    print(f"replay: {len(files)} files {counts}")
    return 0


# What `eval` takes from the process without --recorded: the model's settings, nothing else.
EVAL_MODEL_KEYS = tuple(k.name for k in KEYS if k.feature == "model")


def cmd_eval(args: argparse.Namespace) -> int:
    """The corpus filed into an empty ledger and scored (`evalrun.run_corpus`). It loads none of
    the installation's settings and writes nothing of the installation's: the scorecard goes to
    stdout (and to `--json`), the log to stderr only."""
    from tripbox import corpus
    from tripbox.evalrun import run_corpus
    from tripbox.pipeline.run import RECORD_ENV, REPLAY_ENV

    setup_logging(None)
    try:
        if not args.recorded and offline():
            print(f"eval: {OFFLINE_ENV} is set; run with --recorded", file=sys.stderr)
            return 2
        # either switch makes the model answer from saved answers or save its answers outside the
        # run's own directory: without --recorded, eval asks the configured model and nothing else
        switch = next((n for n in (REPLAY_ENV, RECORD_ENV) if os.environ.get(n)), None)
        if not args.recorded and switch:
            print(f"eval: {switch} is set; run with --recorded", file=sys.stderr)
            return 2
        environ = (
            {}
            if args.recorded
            else {k: os.environ[k] for k in EVAL_MODEL_KEYS if os.environ.get(k)}
        )
        add_secrets([environ.get(MODEL_KEY_ENV)])
        c = corpus.load(Path(args.dir) if args.dir else corpus.default_dir())
        if not c.entries:
            print(f"eval: {c.root} holds no entries", file=sys.stderr)
            return 2
        card = run_corpus(c, recorded=args.recorded, environ=environ)
    except (corpus.CorpusError, ConfigError) as exc:
        for line in str(exc).splitlines() or [""]:
            print(f"eval: {line}", file=sys.stderr)
        return 2
    print("\n".join(card.lines()))
    if args.json:
        try:
            Path(args.json).write_text(
                json.dumps(card.as_json(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            print(f"eval: cannot write {args.json}", file=sys.stderr)
            return 2
    return 0 if card.right else 1


# SQLite's primary result codes for a file it may not make, open or write: permission, read-only,
# input/output, full, cannot open. To the demo they say what an OSError says.
SQLITE_CANNOT_WRITE = frozenset({3, 8, 10, 13, 14})


def cmd_demo(args: argparse.Namespace) -> int:
    """`demo seed`: the corpus's demo emails filed into a new ledger in `--out`, every day
    counted from `--today` (`demo.seed`). Like `eval`, it reads none of the installation's
    settings; the log goes to stderr only. A refusal is one `demo: ` line each on stderr, before
    anything is written; an output that cannot be written (an OSError, or SQLite unable to make
    or write the ledger there) is one line too (exit 2), never an email that was not filed
    (exit 1), and when what the failed write left would be refused next time, the line says to
    remove it."""
    import datetime as dt

    from tripbox import corpus, demo
    from tripbox.util import today_local

    setup_logging(None)
    try:
        today: dt.date | None = None
        if args.today is not None:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.today):
                    raise ValueError(args.today)
                today = dt.date.fromisoformat(args.today)
            except ValueError:
                raise demo.DemoError(
                    f"--today {args.today} is not a day written YYYY-MM-DD"
                ) from None
        c = corpus.load(Path(args.corpus) if args.corpus else corpus.default_dir())
        out = Path(args.out).absolute()
        s = corpus.settings(
            c, data_dir=out / "data", archive_dir=out / "archive", logs_dir=out / "logs"
        )
        if today is None:
            with corpus.process_as(s):  # today where the corpus lives
                today = today_local()
        if args.demo_action == "export":
            return _demo_export(c, out, today)
        try:
            seeded = demo.seed(c, out, today)
        except (OSError, sqlite3.OperationalError) as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if isinstance(exc, sqlite3.Error) and (code or 0) & 0xFF not in SQLITE_CANNOT_WRITE:
                raise
            left = "; remove it and seed again" if demo.refusal(out) else ""
            print(f"demo: cannot write {out}{left}", file=sys.stderr)
            return 2
    except (demo.DemoError, corpus.CorpusError) as exc:
        for line in str(exc).splitlines() or [""]:
            print(f"demo: {line}", file=sys.stderr)
        return 2
    print(
        f"demo: {seeded.emails} emails filed into {seeded.trips} trips"
        f" ({seeded.bookings} bookings, {seeded.unsorted} unsorted), today {today.isoformat()}"
    )
    print(f"demo: ledger {seeded.ledger}")
    print(
        f"demo: show it with TZ={s.tz} TRIPBOX_HOME_AIRPORTS={','.join(s.home_airports)}"
        f" TRIPBOX_AUTH=none TRIPBOX_DATA_DIR={out / 'data'}"
        f" TRIPBOX_CONFIG_DIR={out / corpus.CONFIG_DIR}"
    )
    for line in seeded.failed:
        print(f"demo: FAILED {line}", file=sys.stderr)
    return 1 if seeded.failed else 0


def _demo_export(c, out: Path, today) -> int:
    """`demo export`: the page of the demo written as static files into `out` (`web.export`)."""
    from tripbox import demo
    from tripbox.web import export

    try:
        done = export.export(c, out, today)
    except export.Failed as failed:
        for line in failed.failed:
            print(f"demo: FAILED {line}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.OperationalError):
        # what a failed write leaves is refused next time: say so, as demo seed does
        left = "; remove it and export again" if demo.refusal(out) else ""
        print(f"demo: cannot write {out}{left}", file=sys.stderr)
        return 2
    print(
        f"demo: {done.pages} pages and {done.files} files written to {out},"
        f" today {today.isoformat()}"
    )
    return 0


def cmd_rehearse(args: argparse.Namespace) -> int:
    """Saved emails replayed offline into a fresh ledger; stdout gets counts only."""
    from tripbox.evalrun import apply_env_file, run_rehearsal

    if args.env_file:
        apply_env_file(Path(args.env_file))
    s = _settings(args)
    counts = run_rehearsal(
        s,
        Path(args.dir),
        Path(args.report),
        refcache=Path(args.refcache) if args.refcache else None,
    )
    print("rehearse: " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """A redacted copy of an email and what tripbox did with it (`report.sample`), to attach to
    a parse problem: of a message of the ledger, by its number; of a file, or of standard input
    for `-`; with no argument, the list of the newest messages to choose from. The report goes
    to standard output, or to `--out` (a new file its owner alone may read); what was replaced
    goes to standard error. It reads the settings and the ledger without writing, makes no log
    file and uses no network; a refusal is one line (exit 2) with nothing written."""
    from tripbox.report import sample

    s = Settings.from_env()
    _refuse(s, "core")
    try:
        if args.message is None:
            print("\n".join(sample.newest(s)))
            return 0
        if args.out is not None and os.path.lexists(args.out):
            raise sample.Refused(f"report: {args.out} exists; choose another name")
        made = sample.make(s, args.message, args.name, stdin=sys.stdin.buffer)
        if args.out is not None:
            sample.write_new(args.out, made.text)
    except sample.Refused as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.out is None:
        sys.stdout.write(made.text)
    for line in made.said:
        print(line, file=sys.stderr)
    if args.out is not None:
        print(f"report: wrote {args.out}", file=sys.stderr)
    return 0


# What `tripbox check` asks of each feature that is on, in this order; one line each: `ok`,
# `FAIL` or `warn`, the feature and what was found; `off <feature>` for one that is off and
# `invalid <feature>: tripbox doctor says why` for one whose settings are wrong.
CHECKED = ("airtrail", "model", "enrichment", "notifications")
INVALID = "tripbox doctor says why"
Say = Callable[[str, str, str], None]
IS_IT_AIRTRAIL = "is TRIPBOX_AIRTRAIL_URL the address of AirTrail itself?"


def _check_airtrail(s: Settings, say: Say) -> None:
    """The flight log's profile, read once with the configured key (`/api/v1/me`): whether it
    answers and what the key may do (`airtrail.key_report`), which is kept in the ledger with
    the time (`lifecycle.note_key`) for `doctor` and `airtrail status`. A key of another user
    than the one the ledger's flights were sent as (`lifecycle.USER_KV`) fails, and nothing of
    it is kept. The ledger is never bound here."""
    from tripbox.ledger import Ledger
    from tripbox.write.airtrail import (
        AirTrailClient,
        AirTrailError,
        beyond,
        key_report,
        missing_scope,
        read_profile,
    )
    from tripbox.write.lifecycle import USER_KV, note_key, note_refused

    client = AirTrailClient(s.airtrail_url, s.airtrail_api_key)
    try:
        profile = read_profile(client.me())
    except AirTrailError as exc:
        scope = missing_scope(exc)
        if scope is not None:
            say("FAIL", "airtrail", f"the key lacks {scope}")
        elif exc.status in (401, 403):
            with Ledger.open(s.ledger_path) as led:
                note_refused(led, exc.status)
            say("FAIL", "airtrail", f"refused the key (HTTP {exc.status})")
        elif exc.status:
            say(
                "FAIL",
                "airtrail",
                f"answered HTTP {exc.status}, which AirTrail does not for this request"
                f" — {IS_IT_AIRTRAIL}",
            )
        elif exc.message.startswith("non-JSON response"):
            say(
                "FAIL",
                "airtrail",
                f"the address answers, but not as AirTrail does — {IS_IT_AIRTRAIL}",
            )
        else:
            cause = exc.__cause__
            say("FAIL", "airtrail", f"no answer ({type(cause or exc).__name__})")
        return
    finally:
        client.close()
    with Ledger.open(s.ledger_path) as led:
        bound_to = led.kv.get(USER_KV)
        if bound_to and bound_to != profile.user_id:
            say(
                "FAIL",
                "airtrail",
                "this key is another AirTrail user's than the one tripbox's flights were sent as"
                " (tripbox airtrail forget)",
            )
            return
        note_key(led, profile, checked=True)
    report = key_report(profile)
    if report is None:
        say("ok", "airtrail", "answers; this AirTrail does not say what the key may do")
        return
    if report.missing:
        say("FAIL", "airtrail", f"the key lacks {', '.join(report.missing)}")
    if report.denied:
        say("FAIL", "airtrail", f"your role there does not permit {', '.join(report.denied)}")
    if report.missing or report.denied:
        return
    harmless = [m for m in report.more if m not in beyond(report.more)]
    also = (
        f"; it can also: {', '.join(harmless)} (AirTrail's scope picker adds them to the six)"
        if harmless
        else ""
    )
    say("ok", "airtrail", f"answers; the key holds the six scopes{also}")
    extra = beyond(report.more)
    if extra:
        say(
            "warn",
            "airtrail",
            f"the key can also {', '.join(extra)}; tripbox uses none of them"
            " — a narrower key is safer",
        )


def _check_model(s: Settings, say: Say) -> None:
    """One request to the configured provider through its own client. A failure is said by its
    kind and word, never by the error's text, which could name the key or the address."""
    if s.llm_provider == "openai":
        from tripbox.parse.openai_provider import OpenAIProvider
        from tripbox.parse.provider import ModelError

        try:
            served = OpenAIProvider(
                s.llm_model, api_key=s.llm_api_key, base_url=s.llm_base_url
            ).served()
        except ModelError as exc:
            word = f" ({exc.word})" if exc.word else ""
            say("FAIL", "model", f"openai, {exc.kind}{word}")
            return
        if s.llm_model in served:
            say("ok", "model", f"openai, {s.llm_model} is listed")
        else:
            say(
                "ok",
                "model",
                f"openai, {s.llm_model} is not in the server's list of {len(served)}"
                " (some servers list only what is loaded)",
            )
        return
    import anthropic

    try:
        found = anthropic.Anthropic(api_key=s.llm_api_key, timeout=30).models.retrieve(
            s.llm_model
        )
    except Exception as exc:  # noqa: BLE001 - said by its kind: its text may quote the key
        status = getattr(exc, "status_code", None)
        said = f"HTTP {status}" if status else type(exc).__name__
        say("FAIL", "model", f"anthropic, {said}")
        return
    say("ok", "model", f"anthropic, {found.id}")


def _check_enrichment(s: Settings, say: Say) -> None:
    """The flight-data service's own usage answer: how many calls this period and what they
    cost. It costs nothing."""
    from tripbox.enrich.aeroapi import AeroAPI, AeroAPIError

    try:
        usage = AeroAPI(s.aeroapi_key).usage()
    except AeroAPIError as exc:
        said = f"HTTP {exc.status}" if exc.status else f"no answer ({exc.kind})"
        say("FAIL", "enrichment", f"aeroapi, {said}")
        return
    calls, cost = usage.get("total_calls"), usage.get("total_cost")
    calls = calls if isinstance(calls, int) else 0
    cost = float(cost) if isinstance(cost, int | float) else 0.0
    say("ok", "enrichment", f"aeroapi, {calls} calls, ${cost:.2f} this period")


def _check_notifications(s: Settings, say: Say) -> None:
    """Telegram's bot is asked who it is, which sends nothing to the chat; an ntfy topic cannot
    be asked anything without posting to it, so it is only said to be set."""
    import httpx2

    if s.telegram_on:
        try:
            r = httpx2.get(
                f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe", timeout=20
            )
        except httpx2.HTTPError as exc:
            say("FAIL", "notifications", f"telegram, no answer ({type(exc).__name__})")
        else:
            try:
                body = r.json()
            except ValueError:
                body = {}
            result = body.get("result") if isinstance(body, dict) else None
            bot = result.get("username") if isinstance(result, dict) else None
            if r.status_code == 200 and isinstance(bot, str):
                say("ok", "notifications", f"telegram, @{bot} answers")
            else:
                say("FAIL", "notifications", f"telegram, HTTP {r.status_code}")
    if s.ntfy_on:
        say("ok", "notifications", "ntfy is set; nothing is sent to check it")


def cmd_check(args: argparse.Namespace) -> int:
    """Ask each feature that is on whether it answers (CHECKED), one line each, then the dry
    run's state: 0 when no line says FAIL. It sends nothing anywhere."""
    if _refused_offline("check"):
        return 2
    s = _settings(args)
    failed = False

    def say(word: str, feature: str, detail: str) -> None:
        nonlocal failed
        failed |= word == "FAIL"
        print(f"{word} {feature}: {detail}")

    checks = {
        "airtrail": _check_airtrail,
        "model": _check_model,
        "enrichment": _check_enrichment,
        "notifications": _check_notifications,
    }
    for feature in CHECKED:
        state = s.feature(feature).state
        if state == "invalid":
            print(f"invalid {feature}: {INVALID}")
        elif state == "off":
            print(f"off {feature}")
        else:
            checks[feature](s, say)
    if s.airtrail_dry_run:
        print("dry run: on (flights are filed; nothing is sent to AirTrail)")
    else:
        print("dry run: off")
    return 1 if failed else 0


def cmd_bot(args: argparse.Namespace) -> int:
    """The Telegram bot: it answers in the chat the settings name, so it needs Telegram set up
    (notifications that go to ntfy alone have no chat to answer in)."""
    if _refused_offline("bot"):
        return 2
    from tripbox.notify.bot import Bot
    from tripbox.notify.telegram import Telegram
    from tripbox.pipeline.run import build_context

    s = _settings(args)
    _folders(s, "data", "archive", "config")
    notifications = s.feature("notifications")
    if notifications.state == "invalid":
        _not_on(notifications)
        return 2
    if not s.telegram_on:
        print(TELEGRAM_OFF, file=sys.stderr)
        return 2
    ctx = build_context(s)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    telegram = Telegram(s.telegram_bot_token, s.telegram_chat_id)
    Bot(ctx, telegram, s.data_dir / "bot.heartbeat").run()
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    """The worker (`worker.Worker`): it beats until SIGTERM, reading uploads, keeping the flight
    log in step and starting `poll` and `enrich` when they are due. It needs the network (the
    model, the flight log, the children it starts), so it refuses while offline; mail may be
    off (the `poll` it starts still runs the rest). One worker runs on a ledger at a time."""
    if _refused_offline("work"):
        return 2
    from tripbox.pipeline.run import build_context
    from tripbox.worker import RUNNING, Worker, WorkerRunning

    s = _settings(args)
    _folders(s, "data", "archive", "config")
    worker = Worker(build_context(s))
    try:
        worker.start()
    except WorkerRunning:
        print(f"work: {RUNNING}", file=sys.stderr)
        return 2
    return worker.run()


def cmd_upcoming(args: argparse.Namespace) -> int:
    from tripbox.pipeline.run import build_context
    from tripbox.report.upcoming import build_upcoming
    from tripbox.trips import display
    from tripbox.trips import format as fmt

    s = _settings(args)
    ctx = build_context(s, with_notifier=False, with_model=False)
    with fmt.styled(display.read(ctx.ledger)):  # the owner's style
        print(build_upcoming(ctx))
    return 0


def cmd_trips(args: argparse.Namespace) -> int:
    from tripbox.pipeline.run import build_context
    from tripbox.trips import display
    from tripbox.trips import format as fmt
    from tripbox.util import today_local

    s = _settings(args)
    sub = args.args[0] if args.args else "list"
    ctx = build_context(s, with_notifier=False, with_model=False)
    chosen = display.read(ctx.ledger)  # the owner's style
    if sub == "list":
        with fmt.styled(chosen):
            print(fmt.trip_list_text(ctx.trips, today_local(), past_limit=1000))
        return 0
    if sub == "text" and len(args.args) > 1 and args.args[1].isdigit():
        trip = ctx.trips.get(int(args.args[1]))
        if trip is None:
            print(f"no trip #{args.args[1]}", file=sys.stderr)
            return 1
        with fmt.styled(chosen):
            print(
                fmt.trip_text(
                    trip, ctx.trips.items(trip.id), book=ctx.book, home_airports=s.home_airports
                )
            )
        return 0
    if sub == "feed-token":
        from tripbox.trips.ical import feed_token

        rotate = "--rotate" in args.args
        feed_token(ctx.ledger, rotate=rotate)
        print("rotated" if rotate else "set")  # the URL itself is read from the page, never printed
        return 0
    print("usage: tripbox trips list | text ID | feed-token [--rotate]", file=sys.stderr)
    return 2


ENROLL_USAGE = "usage: tripbox airtrail enroll --since YYYY-MM-DD [--apply]"
AIRTRAIL_BACKFILL_USAGE = (
    "usage: tripbox airtrail backfill [--since YYYY-MM-DD] [--apply] [--verbose]"
)
BACKFILL_YEARS = 2  # how far back `airtrail backfill` reads by default


def _day(text: str | None) -> dt.date | None:
    """The day `text` writes as YYYY-MM-DD, or None when it is no such day."""
    if text is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _years_before(day: dt.date, years: int) -> dt.date:
    """The same day `years` years earlier (the 28th for the 29th of February)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def cmd_airtrail(args: argparse.Namespace) -> int:
    """`airtrail enroll`: a flight that had already left when AirTrail was switched on is held out
    of it; this counts the ones since `--since` that would be queued and, with `--apply`, queues
    them for the next sync pass. `airtrail forget`: another AirTrail, or another user's key, is to
    be kept; this counts the flights linked to the one tripbox knew and, with `--apply`, unlinks
    them, so the next sync pass looks for the upcoming ones in the AirTrail now set up. Neither
    sends anything itself. `airtrail backfill` files the owner's flights the flight log holds
    since a day as trips (`trips.backfill`); `airtrail status` says, from the ledger alone, where
    the flight log stands."""
    from zoneinfo import ZoneInfo

    from tripbox.pipeline.run import build_context
    from tripbox.util import now
    from tripbox.write.lifecycle import enroll, forget

    action = args.airtrail_action
    if action == "status":
        return _airtrail_status(args)
    if action == "backfill":
        return _airtrail_backfill(args)
    forgetting = action == "forget"
    since = None if forgetting else _day(args.since)
    if since is None and not forgetting:
        print(ENROLL_USAGE, file=sys.stderr)
        return 2
    s = _settings(args)
    if not s.on("airtrail"):
        _not_on(s.feature("airtrail"))
        return 2
    ctx = build_context(s, with_notifier=False, with_model=False)
    if since is None:
        today = now().astimezone(ZoneInfo(s.tz)).date()
        linked, upcoming, past = forget(ctx.trips, ctx.ledger, today=today, apply=args.apply)
        if args.apply:
            print(
                f"forget: {linked} flights unlinked; of the {upcoming} upcoming, yours are looked"
                " for in the flight log now set up and added there if missing; "
                f"{past} past are held back"
                " (tripbox airtrail enroll --since DAY --apply sends them)"
            )
        else:
            print(
                f"forget: {linked} flights are linked to the flight log tripbox knew:"
                f" {upcoming} upcoming, {past} past; run again with --apply"
            )
        return 0
    n = enroll(ctx.trips, since=since, apply=args.apply)
    if args.apply:
        print(f"enroll: {n} past flights since {since.isoformat()} queued")
    else:
        print(
            f"enroll: {n} past flights since {since.isoformat()} would be queued;"
            " run again with --apply"
        )
    return 0


def _airtrail_status(args: argparse.Namespace) -> int:
    """Where the flight log stands, read from the ledger alone: whether it is kept (and since
    when it is not), how the flights the owner can see stand with it, what the last look at the
    key found, and, when one applies, why the sync stopped. Nothing is asked of the flight log,
    so it runs offline; every time and scope printed is read as one first (else `?`)."""
    from tripbox.ledger import Ledger
    from tripbox.trips.store import AIRTRAIL_KV, AIRTRAIL_OFF_AT_KV, DRY_RUN_KV, TripStore
    from tripbox.write.airtrail import SCOPES, key_lines, utc_minute
    from tripbox.write.lifecycle import KEY_KV, KEY_PROBLEM_KV, OTHER_LOG
    from tripbox.write.sync import DOWN_SINCE, KEY_REJECTED, ROLE

    s = _settings(args)
    with Ledger.open(s.ledger_path) as led:
        kv = led.kv
        store = TripStore(led, s.home_airports, book=s.travellers)
        kept = kv.get(AIRTRAIL_KV) == "on"
        if kept and kv.get(DRY_RUN_KV) == "on":
            print("flight log: on, dry run: nothing is sent")
        elif kept:
            print("flight log: on")
        elif kv.get(AIRTRAIL_OFF_AT_KV):
            print(f"flight log: off since {utc_minute(kv.get(AIRTRAIL_OFF_AT_KV))} UTC")
        else:
            print("flight log: off")
        if kept:
            n = store.sync_counts()
            print(
                f"flights: {n['synced']} sent, {n['pending']} waiting, {n['blocked']} to check,"
                f" {n['past']} held as past, {n['deleted']} deleted there,"
                f" {n['off']} not sent (not yours, or cancelled first)"
            )
            listed = store.still_listed()
            if listed:
                print(
                    f"flights: {listed} still name you in the flight log though you are off"
                    " them (the trips page marks each)"
                )
        else:
            print(f"flights: {len(store.sent_flights())} hold a record there from when it was kept")
        print("\n".join(key_lines(kv.get(KEY_KV))))
        if not kept:  # a flight log that is off has no trouble to report
            return 0
        if kv.get(DOWN_SINCE):
            print(f"trouble: not answering since {utc_minute(kv.get(DOWN_SINCE))} UTC")
        problem = kv.get(KEY_PROBLEM_KV) or ""
        if kv.get(KEY_REJECTED):
            print(f"trouble: the key is refused since {utc_minute(kv.get(KEY_REJECTED))} UTC")
        if problem.startswith("scope:"):
            scope = problem.removeprefix("scope:")
            print(f"trouble: the key lacks {scope if scope in SCOPES else '?'}")
        elif problem == ROLE:
            print("trouble: your role there does not permit what tripbox needs")
        elif problem == OTHER_LOG:
            print("trouble: another flight log is set up (tripbox airtrail forget)")
    return 0


def _airtrail_backfill(args: argparse.Namespace) -> int:
    """The owner's flights in the flight log since `--since` (two years before today when not
    given) filed as trips (`trips.backfill.backfill`); with `--apply` they are kept. It reads the
    flight log and writes nothing there, so a dry run of the flight log does not hold it back."""
    from tripbox.pipeline.run import build_context
    from tripbox.trips.backfill import BackfillRefused, backfill
    from tripbox.util import today_local
    from tripbox.write.airtrail import AirTrailError

    since = _day(args.since) if args.since is not None else None
    if args.since is not None and since is None:
        print(AIRTRAIL_BACKFILL_USAGE, file=sys.stderr)
        return 2
    if _refused_offline("airtrail backfill"):
        return 2
    s = _settings(args)
    if not s.on("airtrail"):
        _not_on(s.feature("airtrail"))
        return 2
    ctx = build_context(s, with_notifier=False, with_model=False)
    since = since or _years_before(today_local(), BACKFILL_YEARS)
    assert ctx.airtrail is not None  # AirTrail is on (checked above)
    try:
        rep = backfill(
            ctx.ledger,
            ctx.trips,
            ctx.airtrail,
            since=since.isoformat(),
            me_name=ctx.book.me_name,
            apply=args.apply,
        )
    except BackfillRefused as e:
        print(e, file=sys.stderr)
        return 2
    except AirTrailError as e:
        said = f"HTTP {e.status}" if e.status else "no answer"
        print(
            f"backfill: the flight log could not be read ({said}); tripbox check says why",
            file=sys.stderr,
        )
        return 1
    print("\n".join(rep.lines(verbose=args.verbose)))
    return 0


BACKFILL_USAGE = "usage: tripbox mail backfill --since YYYY-MM-DD [--apply]"


def cmd_mail(args: argparse.Namespace) -> int:
    """`mail backfill`: a mailbox is read from the moment tripbox first read it; older mail only
    on request. This counts the messages that arrived since `--since` up to the cursor as it
    stands and, with `--apply`, reads each as the poll reads mail (the sender check, the model's
    cap, filing as set), leaving the cursor where it is."""
    if _refused_offline("mail backfill"):
        return 2
    since = _day(args.since)
    if since is None:
        print(BACKFILL_USAGE, file=sys.stderr)
        return 2
    from tripbox.ledger import Ledger
    from tripbox.mail import imap as imapmod
    from tripbox.pipeline.run import backfill

    s = _settings(args)
    mail = s.feature("mail")
    if mail.state != "on":
        _not_on(mail)
        return 2
    ctx = _context(args, s) if args.apply else None
    ledger = ctx.ledger if ctx is not None else Ledger.open(s.ledger_path)
    try:
        inbox = imapmod.ImapInbox(s.mail_host, s.mail_user, s.mail_password)
        try:
            uids = imapmod.backfill_uids(inbox, ledger, since)
            summary = backfill(ctx, inbox, uids) if ctx is not None else None
        finally:
            inbox.close()
    except Exception:  # noqa: BLE001 - the mailbox could not be read: said, never a traceback
        log.exception("mail backfill failed")
        print("backfill: the mailbox could not be read; see the log", file=sys.stderr)
        return 1
    day = since.isoformat()
    if summary is None:
        print(f"backfill: {len(uids)} messages since {day} would be read; run again with --apply")
        return 0
    print(f"backfill: {len(uids)} messages since {day} read: {summary.counts}")
    return 0 if summary.errors == 0 else 1


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the trips page at TRIPBOX_BASE_URL. Settings come from the environment (compose
    passes this container only web settings: nothing that reaches the mailbox, the model,
    AirTrail, AeroAPI or Telegram, its only secrets its own login pair). A core or a web problem
    stops it: there is no way to serve it past its login. Its secrets are masked in its log, and
    each name that is no setting is logged once. The ledger is opened once before serving, so one
    this version cannot read stops it with the same line as every other command instead of
    failing each request."""
    import uvicorn

    from tripbox.ledger import Ledger
    from tripbox.log import setup_logging
    from tripbox.web.app import create_app

    s = Settings.from_env()
    _refuse(s, "core", "web")
    _folders(s, "data", "config")
    if args.host and s.tripbox_auth == "none" and not in_image() and not _loopback(args.host):
        # outside the image the address bound is the one exposed, and the page's Host check is
        # no wall; inside it (TRIPBOX_IN_IMAGE) compose publishes the port at TRIPBOX_LISTEN,
        # which no login keeps on loopback
        print(
            f"web: --host {args.host} answers beyond this computer, and TRIPBOX_AUTH=none has no"
            " login — use TRIPBOX_AUTH=password, proxy or cloudflare, or --host 127.0.0.1",
            file=sys.stderr,
        )
        return 2
    use_zone(s.tz)
    _mask_secrets(s)
    setup_logging(s.logs_dir, name="tripbox-web")  # its own file: see log.setup_logging
    for p in s.problems:
        if p.feature == "names" or p.key in DISPLAY_ONLY or not p.stops:
            log.warning("config: %s", p)
    Ledger.open(s.ledger_path).close()
    app = create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)
    uvicorn.run(
        app,
        # TRIPBOX_LISTEN unless told: the image's services pass every interface, as a published
        # port reaches nothing on a container's own loopback; the published side is then
        # TRIPBOX_LISTEN, which no-login mode holds to loopback
        host=args.host or s.listen[0],
        port=args.port or s.listen[1],
        access_log=False,
        log_level="info",
        server_header=False,
        # the client is the socket's peer: a forwarded header is anyone's to send, so none is
        # believed
        proxy_headers=False,
    )
    return 0


def _loopback(host: str) -> bool:
    """A loopback address, read as an address as TRIPBOX_LISTEN's is: a name is none."""
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_enrich(args: argparse.Namespace) -> int:
    if _refused_offline("enrich"):
        return 2
    from tripbox.enrich.aeroapi import AeroAPI
    from tripbox.enrich.enricher import TITLE, enrich_recent, notice_lines
    from tripbox.notify.notifier import Notice

    s = _settings(args)
    enrichment = s.feature("enrichment")
    if enrichment.state == "off":  # nothing to do is not a failure of the nightly job
        print(off_line(enrichment))
        return 0
    if enrichment.state == "invalid":
        _not_on(enrichment)
        return 2
    ctx = _context(args, s, with_model=False)
    lines = enrich_recent(ctx, AeroAPI(s.aeroapi_key))
    print("\n".join(lines))
    text = notice_lines(lines)
    if text:  # it runs at 03:30; the news can wait for morning
        ctx.notifier.notify(Notice("filed", text, quiet=True, title=TITLE))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Where the installation stands. It reads the settings, checks the folders (an empty file
    made and removed in each one written) and reads the worker's record and what the last look
    at the flight log's key found without writing, then prints: it makes no log file, zone,
    directory or ledger, uses no network, and a core problem does not stop it. A key found
    lacking a scope is a problem it counts, though it turns nothing off."""
    from tripbox import files
    from tripbox.config import key_problems

    s = Settings.from_env()
    folders = files.problems(s, files.NAMES)
    keys = key_problems(s)
    print("\n".join(report(s, folders, keys)))
    return 1 if s.problems or folders or keys else 0


def cmd_backup(args: argparse.Namespace) -> int:
    """One archive (mode 600) of the ledger, as it stands while tripbox runs, and the config
    folder, and with `--with-mail` every saved email (`backup.backup`)."""
    from tripbox import backup

    s = _settings(args, folders_only=True)
    try:
        lines = backup.backup(s, with_mail=args.with_mail, out=args.out)
    except backup.Refused as exc:
        print(exc, file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Put a backup's ledger, and the saved emails it holds, back (`backup.restore`): only
    from an archive `backup` made, never while anything holds the ledger, after copying the
    ledger it replaces; the config folder is compared, never written. Without `--apply` it says
    what it would do and changes nothing."""
    from tripbox import backup

    s = _settings(args, folders_only=True)
    try:
        backup.restore(s, args.file, apply=args.apply)
    except backup.Refused as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    """The TRIPBOX_PASSWORD_HASH line of a password: typed twice without echo at a terminal, or
    one line from a pipe. It reads no settings and opens no ledger, log or directory; the
    password is never printed."""
    import getpass

    from tripbox import passwords

    if sys.stdin.isatty():
        try:
            first = second = getpass.getpass("Password: ")
            if passwords.MIN_LENGTH <= len(first) <= passwords.MAX_TYPED:
                second = getpass.getpass("Password again: ")
        except EOFError:  # Ctrl-D at a prompt
            print("passwd: no password given", file=sys.stderr)
            return 2
    else:
        first = second = sys.stdin.readline().rstrip("\r\n")
    if passwords.is_example(first):
        print("passwd: choose your own password, not the example", file=sys.stderr)
        return 2
    if len(first) < passwords.MIN_LENGTH:
        print(f"passwd: use {passwords.MIN_LENGTH} characters or more", file=sys.stderr)
        return 2
    if len(first) > passwords.MAX_TYPED:  # the page never checks a longer one: it could not sign in
        print(f"passwd: use {passwords.MAX_TYPED} characters or fewer", file=sys.stderr)
        return 2
    if first != second:
        print("passwd: the two did not match", file=sys.stderr)
        return 2
    print(f"TRIPBOX_PASSWORD_HASH={passwords.make_hash(first)}")
    return 0


# ----------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tripbox", description=__doc__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help_: str, fn, dry=True):
        sp = sub.add_parser(name, help=help_)
        if dry:
            sp.add_argument(
                "--dry-run",
                action="store_true",
                help="send nothing to AirTrail in this run: bookings are still filed and"
                " flights wait (the setting can only force this on)",
            )
        sp.set_defaults(fn=fn)
        return sp

    add("doctor", "Say what is configured, what is off and what is wrong", cmd_doctor, dry=False)
    add(
        "passwd",
        "Print the TRIPBOX_PASSWORD_HASH line of a password typed twice (or piped in once)",
        cmd_passwd,
        dry=False,
    )
    sp = add(
        "backup",
        "Write one archive of the ledger and the config folder into the backups folder",
        cmd_backup,
        dry=False,
    )
    sp.add_argument(
        "--with-mail", action="store_true", help="hold every saved email too (personal data)"
    )
    sp.add_argument("--out", metavar="FILE", help="write the archive here instead (a new file)")
    sp = add(
        "restore",
        "Put a backup's ledger (and its saved emails) back; --apply does it, with tripbox stopped",
        cmd_restore,
        dry=False,
    )
    sp.add_argument("file", metavar="FILE", help="an archive tripbox backup wrote")
    sp.add_argument(
        "--apply", action="store_true", help="put it back (without it nothing is changed)"
    )
    add("poll", "Fetch new mail, archive, parse, plan and write", cmd_poll)
    sp = add("ingest", "Parse one .eml or plain itinerary text (path or '-')", cmd_ingest)
    sp.add_argument("path")
    sp.add_argument("--source", default="cli")
    sp.add_argument("--subject", default=None)
    sp.add_argument("--force", action="store_true", help="re-process even if this content was seen")
    sp.add_argument("--quiet", action="store_true", help="no notification")
    sp = add(
        "replay", "Re-run the pipeline over the archive (idempotent via the ledger)", cmd_replay
    )
    sp.add_argument("--since", help="YYYY-MM")
    sp.add_argument("--file")
    sp.add_argument("--status", help="only messages currently in this ledger status, e.g. failed")
    sp.add_argument("--force", action="store_true")
    sp.add_argument(
        "--reread",
        action="store_true",
        help="ask the model again instead of reading the answer stored with each message",
    )
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp = add(
        "eval",
        "File the corpus of invented emails into an empty ledger and score what was read and"
        " filed",
        cmd_eval,
        dry=False,
    )
    sp.add_argument("dir", nargs="?", help="the corpus (default: the one beside the package)")
    sp.add_argument(
        "--recorded",
        action="store_true",
        help="answer from the corpus's own answers instead of the configured model",
    )
    sp.add_argument("--json", metavar="FILE", help="also write the scorecard to FILE as JSON")
    sp = add(
        "demo",
        "Build a demo ledger from the corpus of invented emails (demo seed --out DIR), or its page"
        " as static files (demo export --out DIR)",
        cmd_demo,
        dry=False,
    )
    actions = sp.add_subparsers(dest="demo_action", metavar="ACTION", required=True)
    seed = actions.add_parser(
        "seed",
        help="File the corpus's demo emails into a new ledger, every day counted from --today",
    )
    seed.add_argument("--out", required=True, metavar="DIR", help="where the demo is written")
    seed.add_argument(
        "--today",
        metavar="YYYY-MM-DD",
        help="the day the demo calls today (default: today in the corpus's zone)",
    )
    seed.add_argument(
        "--corpus", metavar="DIR", help="the corpus (default: the one beside the package)"
    )
    export = actions.add_parser(
        "export",
        help="Write the demo page as static, read-only files (for GitHub Pages) into --out",
    )
    export.add_argument("--out", required=True, metavar="DIR", help="where the pages are written")
    export.add_argument(
        "--today",
        metavar="YYYY-MM-DD",
        help="the day the demo calls today (default: today in the corpus's zone)",
    )
    export.add_argument(
        "--corpus", metavar="DIR", help="the corpus (default: the one beside the package)"
    )
    sp = add(
        "rehearse",
        "Replay saved emails offline, in file-name order, into an empty ledger"
        " (needs TRIPBOX_NO_NETWORK=1 and TRIPBOX_REPLAY_DIR or TRIPBOX_RECORD_DIR)",
        cmd_rehearse,
        dry=False,
    )
    sp.add_argument("dir", help="directory of *.eml files")
    sp.add_argument("--report", required=True, help="where the run report is written (mode 600)")
    sp.add_argument("--env-file", help="KEY=value settings loaded before the settings are read")
    sp.add_argument(
        "--refcache",
        help="SQLite file of saved AirTrail answers (ref_cache rows) that seed AirTrail's ids",
    )
    sp = add(
        "report",
        "Write a redacted copy of an email and what tripbox did with it, to attach to a parse"
        " problem: read all of it before you share it",
        cmd_report,
        dry=False,
    )
    sp.add_argument(
        "message",
        nargs="?",
        metavar="MESSAGE",
        help="a message's number in the ledger, a file, or - for standard input (none: list"
        " the ten newest messages)",
    )
    sp.add_argument(
        "--name",
        action="append",
        default=[],
        metavar="NAME",
        help="a traveller's name to replace too; may be given more than once",
    )
    sp.add_argument(
        "--out", metavar="FILE", help="write the report to FILE, a new file, instead"
    )
    add(
        "check",
        "Ask each feature that is on whether it answers: AirTrail (and what its key may do),"
        " the model, the flight-data service, Telegram",
        cmd_check,
        dry=False,
    )
    add(
        "enrich",
        "Fill in recently landed flights in AirTrail from the flight-data service"
        " (TRIPBOX_ENRICH; budget-guarded)",
        cmd_enrich,
    )
    add(
        "upcoming",
        "Print upcoming trips (plain text, same as Telegram /upcoming)",
        cmd_upcoming,
        dry=False,
    )
    sp = add(
        "trips",
        "Trips in the ledger: list | text ID | feed-token [--rotate]",
        cmd_trips,
        dry=False,
    )
    sp.add_argument("args", nargs=argparse.REMAINDER)
    sp = add(
        "airtrail",
        "AirTrail: backfill [--since YYYY-MM-DD] [--apply] files its flights as trips; status;"
        " enroll --since YYYY-MM-DD [--apply] queues the flights that had left when it was"
        " switched on; forget [--apply] unlinks every flight, for another AirTrail to be kept",
        cmd_airtrail,
        dry=False,
    )
    actions = sp.add_subparsers(dest="airtrail_action", metavar="ACTION", required=True)
    enroll = actions.add_parser(
        "enroll",
        help="Count the flights since a day that had left when AirTrail was switched on;"
        " --apply queues them",
    )
    enroll.add_argument("--since", metavar="YYYY-MM-DD", help="the first day counted")
    enroll.add_argument("--apply", action="store_true", help="queue them for the next sync pass")
    forget = actions.add_parser(
        "forget",
        help="Count the flights linked to the AirTrail tripbox knew; --apply unlinks them, so"
        " another AirTrail, or another user's key, can be kept",
    )
    forget.add_argument("--apply", action="store_true", help="unlink them")
    filing = actions.add_parser(
        "backfill",
        help="File the flights your flight log holds since a day as trips; --apply keeps them",
    )
    filing.add_argument(
        "--since", metavar="YYYY-MM-DD", help="the first day read (default: two years ago)"
    )
    filing.add_argument("--apply", action="store_true", help="keep what is filed")
    filing.add_argument("--verbose", action="store_true", help="also name each trip")
    actions.add_parser(
        "status",
        help="Say where the flight log stands, from the ledger alone: nothing is asked of it",
    )
    sp = add(
        "mail",
        "Mail: backfill --since YYYY-MM-DD [--apply] reads the mail that came before tripbox"
        " started reading the mailbox",
        cmd_mail,
        dry=False,
    )
    actions = sp.add_subparsers(dest="mail_action", metavar="ACTION", required=True)
    backfill = actions.add_parser(
        "backfill",
        help="Count the messages since a day that came before the cursor; --apply reads them",
    )
    backfill.add_argument("--since", metavar="YYYY-MM-DD", help="the first day counted")
    backfill.add_argument("--apply", action="store_true", help="read them as mail")
    add("bot", "Run the Telegram bot (long-polls Telegram for its commands)", cmd_bot, dry=False)
    add(
        "work",
        "Run the worker: read uploads, keep the flight log in step, start poll and enrich when"
        " they are due",
        cmd_work,
        dry=False,
    )
    sp = add("web", "Serve the trips page (uvicorn)", cmd_web, dry=False)
    listen = "default: TRIPBOX_LISTEN's"
    sp.add_argument("--host", default=None, help=f"the address to listen on ({listen})")
    sp.add_argument("--port", type=int, default=None, help=f"the port to listen on ({listen})")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    try:
        return int(ns.fn(ns))
    except ConfigError as exc:
        for line in str(exc).splitlines() or [""]:
            print(f"config error: {line}", file=sys.stderr)
        return 2
    except OutdatedLedger as exc:
        print(f"ledger: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        if not any(text in str(exc) for text in LEDGER_REFUSED):
            raise
        print(f"ledger: {CANNOT_OPEN}", file=sys.stderr)
        # the traceback, for whoever reads the log file: at DEBUG, whatever level the log keeps
        record = log.makeRecord(
            log.name, logging.DEBUG, __file__, 0, "ledger: %s", (CANNOT_OPEN,), sys.exc_info(),
            extra=FILE_ONLY,
        )
        log.handle(record)
        return 2
    except KeyboardInterrupt:
        return 130
