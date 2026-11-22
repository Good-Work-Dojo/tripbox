# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Two runs of the pipeline over saved emails, each into an empty ledger. The corpus evaluation
(`run_corpus`) files a corpus's invented emails, in order, under the corpus's own settings and
scores what was read from each and what filing it did against the entry's truth (`Scorecard`).
The rehearsal (`run_rehearsal`) replays saved emails offline through the live pipeline, with the
settings file it reads (`read_env_file`, `apply_env_file`)."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from tripbox import corpus
from tripbox.config import KEY_BY_NAME, MODEL_KEY_ENV, OFFLINE_ENV, ConfigError, Settings, offline
from tripbox.ledger import Ledger
from tripbox.mail.normalize import normalize
from tripbox.parse.pipeline import parse_message
from tripbox.parse.provider import ModelError
from tripbox.parse.replay import (
    REPLAY_FAILURES,
    WIRE_SCHEMA_VERSION,
    RecordingExtractor,
    ReplayExtractor,
    answer_path,
)
from tripbox.pipeline.run import (
    RECORD_ENV,
    REPLAY_ENV,
    Context,
    MessageResult,
    build_context,
    process_raw,
)
from tripbox.scorecard import FieldResult, compare_outcome, outcome_view, score_extraction
from tripbox.write.airtrail import MemoryAirTrail
from tripbox.write.executor import Outcome

log = logging.getLogger("tripbox.eval")


# ----------------------------------------------------------------------------- the corpus
SCORECARD_SCHEMA = 2
# The difference a result with attached emails of its own adds: an entry is exactly one email.
ONE_EMAIL = "children: one entry is one email"
_UNDER = "      "  # what a failing email's detail lines are indented by


@dataclass(frozen=True)
class EmailScore:
    """One corpus email as scored: who read it (`kitinerary`, `model` or None), every compared
    field, each way its filing differs from the truth, the entry's known gap, what the model's
    answer cost, and the error that stopped it."""

    name: str
    read_by: str | None
    fields: tuple[FieldResult, ...]
    outcome_diffs: tuple[str, ...]
    gap: str | None
    cost_usd: float
    error: str | None

    @property
    def right(self) -> bool:
        return self.error is None and all(f.right for f in self.fields) and not self.outcome_diffs


@dataclass(frozen=True)
class Scorecard:
    """A corpus run: the corpus it read, whose answers stood in for the model (`recorded`: the
    corpus's own; `model`: an extractor's; `none`: no model at all), that model's name, every
    email in order, how many entries hold each kind of answer, and what the ledger holds once
    every email is filed: the trips the page would list (not deleted, and holding at least one
    booking that is not removed), the bookings in them that are not removed, and the bookings
    with no trip (no date yet) that are not removed."""

    root: Path
    answers: str
    model: str | None
    emails: tuple[EmailScore, ...]
    answer_sources: dict[str, int]
    trips: int
    bookings: int
    unsorted: int

    @property
    def right(self) -> bool:
        return bool(self.emails) and all(e.right for e in self.emails)

    def _totals(self) -> dict:
        fields = [f for e in self.emails for f in e.fields]
        readers = [e.read_by for e in self.emails]
        return {
            "emails": len(self.emails),
            "emails_right": sum(e.right for e in self.emails),
            "fields": len(fields),
            "fields_right": sum(f.right for f in fields),
            "parsers": {
                "kitinerary": readers.count("kitinerary"),
                "model": readers.count("model"),
                "none": readers.count(None),
            },
            "answers": {s: self.answer_sources.get(s, 0) for s in corpus.ANSWER_SOURCES},
            "known_gaps": sum(e.gap is not None for e in self.emails),
            "trips": self.trips,
            "bookings": self.bookings,
            "unsorted": self.unsorted,
            "cost_usd": sum(e.cost_usd for e in self.emails),
        }

    def lines(self) -> list[str]:
        """The scorecard as printed: a heading, a line per email (with what is wrong under a
        failing one), the totals (the ledger's trips and bookings, in a trip or none, among
        them), and last how many emails are right."""
        t = self._totals()
        out = [
            f"tripbox eval: {t['emails']} emails from {self.root} "
            f"(wire schema {WIRE_SCHEMA_VERSION}, answers: {self.answers})"
        ]
        width = max((len(e.name) for e in self.emails), default=0)
        for e in self.emails:
            score = f"{sum(f.right for f in e.fields)}/{len(e.fields)}" if e.fields else "-"
            mark = "ok" if e.right else "FAIL"
            line = f"{mark:<4}  {e.name:<{width}}  {e.read_by or '-':<10}  {score}"
            out.append(line + (f"  known gap: {e.gap}" if e.gap else ""))
            if e.right:
                continue
            out += [
                f"{_UNDER}extraction {f.path}: want {f.want!r}, got {f.got!r}"
                for f in e.fields
                if not f.right
            ]
            out += [f"{_UNDER}outcome {d}" for d in e.outcome_diffs]
            if e.error:
                out.append(f"{_UNDER}error: {e.error}")
        p, a = t["parsers"], t["answers"]
        out.append(f"parsers: kitinerary: {p['kitinerary']} model: {p['model']} none: {p['none']}")
        out.append(
            f"answers: hand-written: {a['hand-written']} recorded: {a['recorded']}"
            f" not asked: {a['not-asked']}"
        )
        n, right = t["fields"], t["fields_right"]
        share = f" ({100 * right / n:.1f}%)" if n else ""
        out.append(f"fields: {right} of {n} right{share}")
        out.append(f"known gaps: {t['known_gaps']}")
        out.append(
            f"ledger: {t['trips']} trips, {t['bookings']} bookings, {t['unsorted']} unsorted"
        )
        if t["cost_usd"] > 0:
            out.append(f"model cost: ${t['cost_usd']:.4f}")
        out.append(f"eval: {t['emails_right']} of {t['emails']} emails right")
        return out

    def as_json(self) -> dict:
        """The scorecard as saved: no time stamp and no path, so two runs that score alike give
        equal files."""
        return {
            "schema": SCORECARD_SCHEMA,
            "wire_schema": WIRE_SCHEMA_VERSION,
            "answers": self.answers,
            "model": self.model,
            "emails": [
                {
                    "name": e.name,
                    "read_by": e.read_by,
                    "fields": [
                        {"path": f.path, "want": f.want, "got": f.got, "right": f.right}
                        for f in e.fields
                    ],
                    "right": e.right,
                    "outcome": list(e.outcome_diffs),
                    "gap": e.gap,
                    "cost_usd": e.cost_usd,
                    "error": e.error,
                }
                for e in self.emails
            ],
            "totals": self._totals(),
        }


def _score(ctx: Context, entry: corpus.Entry) -> EmailScore:
    res: MessageResult | None = None
    view: dict | None = None
    error: str | None = None
    try:
        res = process_raw(
            ctx, entry.raw, source="eval", received=corpus.sent(entry), notify=False
        )
        view = outcome_view(res, ctx)
    except Exception as exc:  # noqa: BLE001 - as in a poll: this email failed, the run goes on
        log.exception("corpus entry %s: unhandled error", entry.name)
        error = f"{type(exc).__name__}: {exc}"
    parsed = res.parsed if res is not None else None
    if res is not None and res.status == "rejected" and entry.outcome["status"] == "rejected":
        fields: tuple[FieldResult, ...] = ()  # never read, as meant: nothing to compare
    else:
        fields = score_extraction(entry.truth, parsed.extraction if parsed else None)
    diffs = compare_outcome(entry.outcome, view) if view is not None else ()
    if res is not None and res.children:
        diffs += (ONE_EMAIL,)
    return EmailScore(
        name=entry.name,
        read_by=view["read_by"] if view is not None else None,
        fields=fields,
        outcome_diffs=diffs,
        gap=entry.gap,
        cost_usd=parsed.usage.cost_usd if parsed is not None and parsed.usage else 0.0,
        error=error,
    )


def run_corpus(
    c: corpus.Corpus,
    *,
    recorded: bool,
    environ: Mapping[str, str] | None = None,
    extractor=None,
) -> Scorecard:
    """File every entry of `c`, in order, into one empty ledger through the pipeline
    (`process_raw`, dated by the email's own Date) and score each against its truth. The settings
    are the corpus's own plus `environ`, never the process's. The model is `extractor` when given,
    else the corpus's own answers when `recorded`, else the configured one, or none. Data, archive
    and logs go to a temporary directory that is removed; the process's zone and
    TRIPBOX_CONFIG_DIR are as before afterwards. An email that raises is scored with its error and
    the run goes on. Once every email is filed, the trips the page would list, their bookings and
    the bookings with no trip are counted."""
    with tempfile.TemporaryDirectory(prefix="tripbox-eval-") as tmp:
        base = Path(tmp)
        s = corpus.settings(
            c,
            data_dir=base / "data",
            archive_dir=base / "archive",
            logs_dir=base / "logs",
            environ=environ,
        )
        with corpus.process_as(s):
            ledger = Ledger.memory()
            try:
                ctx = build_context(
                    s,
                    ledger=ledger,
                    with_model=not recorded and extractor is None,
                    with_notifier=False,
                )
                if extractor is not None:
                    ctx.model = extractor
                elif recorded:
                    ctx.model = corpus.CorpusAnswers(c, s.llm_model)
                emails = tuple(_score(ctx, entry) for entry in c.entries)
                trips = ctx.trips.all_trips()
                bookings = sum(len(ctx.trips.items(t.id)) for t in trips)
                unsorted = len(ctx.trips.unsorted())
            finally:
                ledger.close()
    if recorded:
        answers = "recorded"
    else:
        answers = "model" if ctx.model is not None else "none"
    return Scorecard(
        root=c.root,
        answers=answers,
        model=getattr(ctx.model, "model", None) if answers == "model" else None,
        emails=emails,
        answer_sources={
            src: sum(e.answer_source == src for e in c.entries) for src in corpus.ANSWER_SOURCES
        },
        trips=len(trips),
        bookings=bookings,
        unsorted=unsorted,
    )


# ----------------------------------------------------------------------------- rehearsal
REPORT_SCHEMA = 2
# Settings the rehearsal scripts pass themselves: when the environment holds one, the settings
# file cannot replace it, so no file points a run at another ledger, answers directory or network
# mode, and the model key handed to a recording beats the file's placeholder.
ENV_WINS = (
    OFFLINE_ENV,
    REPLAY_ENV,
    RECORD_ENV,
    "TRIPBOX_DATA_DIR",
    "TRIPBOX_ARCHIVE_DIR",
    "TRIPBOX_LOGS_DIR",
    "TRIPBOX_CONFIG_DIR",
    MODEL_KEY_ENV,
)
_ENV_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")
_ENV_COMMENT = re.compile(r"\s#")


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines in compose's .env shape: blank lines and # comments are skipped, `export `
    is allowed, a value in matching quotes is the text between them, and an unquoted value ends at
    a ` #` comment. An error names the line, never a value."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ConfigError("cannot read the settings file") from None
    out: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_LINE.fullmatch(stripped)
        if not m:
            raise ConfigError(f"settings file line {number} is not KEY=value")
        key, value = m.group(1), m.group(2)
        lead = value.lstrip()
        if lead[:1] in ("'", '"'):
            end = lead.find(lead[0], 1)
            if end < 0:
                raise ConfigError(f"settings file line {number} has an unclosed quote")
            value = lead[1:end]
        else:
            comment = _ENV_COMMENT.search(value)
            value = (value[: comment.start()] if comment else value).strip()
        out[key] = value
    return out


def apply_env_file(path: Path, environ: MutableMapping[str, str] | None = None) -> int:
    """Load a settings file's tripbox settings into the environment; their values replace
    whatever the image set, except an ENV_WINS name the environment already holds. A line that
    names no setting (PATH, LD_PRELOAD, a name from before the TRIPBOX_ prefix) is skipped and
    only counted. Returns how many values it took."""
    env = os.environ if environ is None else environ
    taken = skipped = 0
    for key, value in read_env_file(path).items():
        if key not in KEY_BY_NAME:
            skipped += 1
            continue
        if key in ENV_WINS and env.get(key):
            continue
        env[key] = value
        taken += 1
    if skipped:
        log.info("settings file: %d line(s) are not tripbox settings", skipped)
    return taken


def _load_refcache(ledger: Ledger, path: Path) -> int:
    """AirTrail's answers from a saved reference cache (the ids a flight is written with), each
    stamped now so that none counts as expired. Validation never reads them: its airports come
    from the bundled table."""
    try:
        src = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
        try:
            rows = src.execute("SELECT kind, code, json FROM ref_cache").fetchall()
        finally:
            src.close()
    except sqlite3.Error:
        raise ConfigError("cannot read the reference cache") from None
    for kind, code, text in rows:
        try:
            record = json.loads(text)
        except (TypeError, ValueError):
            raise ConfigError("the reference cache holds a record that is not JSON") from None
        ledger.refcache.put(kind, code, record)
    return len(rows)


def _outcome_view(o: Outcome) -> dict:
    """One booking of a message as the report holds it: what became of it (`result`), where its
    sync with the flight log stood once the message's pass was done (`sync`; None for a booking
    that was not filed), who the email named on it, and the checks open on it."""
    item = o.action.item
    cls = item.classified
    return {
        "kind": item.kind,
        "key": item.key,
        "result": o.result,
        "sync": o.sync,
        "people": {"me": int(cls.me_present), "guests": len(cls.guests), "others": len(cls.others)},
        "checks": sorted(o.checks),
    }


def _result_view(res: MessageResult) -> dict:
    parsed = res.parsed
    view: dict = {
        "status": res.status,
        "parser": parsed.parser if parsed else None,
        "document_type": parsed.extraction.document_type if parsed else None,
        "items": [_outcome_view(o) for o in res.outcomes],
    }
    code = REPLAY_FAILURES.get(parsed.error) if parsed is not None and parsed.error else None
    if code is not None:
        # the replay had no usable answer for it (none, unreadable, or not of the wire schema):
        # whatever the pipeline then did with kitinerary's partial reading, this email was not
        # tested, so it fails the rehearsal
        view.update(status="failed", items=[], problems=[code])
    if res.children:  # further emails attached to this one, each processed as its own message
        view["children"] = [_result_view(c) for c in res.children]
    return view


def _answer_unasked(
    ctx: Context, recorder: RecordingExtractor, raw: bytes, row_id: int | None
) -> None:
    """While recording: a fixture the pipeline settled without the model (kitinerary read all of
    it, or it was set aside as a copy of an earlier email) is asked once anyway, with kitinerary's
    reading as the hint, so every fixture holds an answer and a later change that sends it to the
    model replays instead of failing. Mail the origin check rejects is never shown to the model."""
    if answer_path(recorder.root, raw).exists():
        return
    s = ctx.settings
    try:
        nm = normalize(
            raw,
            s.allowed_senders,
            s.tz,
            require_auth=s.require_google_auth,
            trusted_domains=s.mail_trusted_sender_domains,
        )
        if not nm.origin_ok:
            return
        parsed = parse_message(nm, None)
        hint = parsed.extraction if parsed.extraction.reservations else None
        _, usage = recorder.extract(nm, hint)
    except ModelError as exc:
        log.error("recording failed: %s", exc)
        return
    except Exception:  # noqa: BLE001 - the pipeline already recorded what went wrong here
        log.exception("recording skipped a fixture it could not read")
        return
    ctx.ledger.llm.add(
        message_id=row_id,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
    )


def _write_private(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(doc, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def _close(ledger: Ledger) -> None:
    """Fold the write-ahead log back into the file before closing, so the finished ledger is one
    plain file that the host opens read-only."""
    try:
        ledger.conn.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error:
        log.warning("the rehearsal ledger is left in WAL mode")
    ledger.close()


def _counts(messages: list[dict]) -> dict[str, int]:
    counts = {"messages": len(messages), "processed": 0, "rejected": 0, "failed": 0, "items": 0}

    def add(m: dict) -> None:
        if m["status"] in ("processed", "rejected", "failed"):
            counts[m["status"]] += 1
        counts["items"] += len(m["items"])
        for child in m.get("children", []):
            add(child)

    for m in messages:
        add(m)
    return counts


def run_rehearsal(
    settings: Settings, fixtures_dir: Path, report: Path, *, refcache: Path | None = None
) -> dict[str, int]:
    """Process every `*.eml` in `fixtures_dir` in file-name order, in this one process, through
    the live path (`process_raw`, as a poll hands it new mail) into the empty ledger the settings
    name, with the dry run off: every booking is filed, and after each message the flight log,
    the in-memory one, is brought in step as it is for a real message. Then write `report` (mode
    600): per fixture its ordinal `n`, its status, parser and document type, and per booking
    what became of it and where its sync stood; a message the replay had no usable answer for is
    `failed` with no items and one problem: `no_recorded_answer`, `unreadable_recorded_answer` or
    `recorded_answer_off_schema`. Item keys appear in it as the ledger
    holds them, so it is as private as the ledger. Runs only offline and with recorded answers to
    replay or record; `refcache` (a SQLite file with a ref_cache table of AirTrail's answers) is
    copied into the ledger first, and seeds the ids the in-memory AirTrail writes flights with."""
    if not offline():
        raise ConfigError(f"rehearse runs only with {OFFLINE_ENV}=1")
    files = sorted(fixtures_dir.glob("*.eml"))
    if not files:
        raise ConfigError("there are no *.eml fixtures to rehearse")
    ctx = build_context(dataclasses.replace(settings, airtrail_dry_run=False), with_notifier=False)
    try:
        if not isinstance(ctx.model, ReplayExtractor | RecordingExtractor):
            raise ConfigError(f"rehearse needs {REPLAY_ENV} or {RECORD_ENV}")
        if ctx.notifier.channels or not isinstance(ctx.airtrail, MemoryAirTrail | None):
            raise ConfigError("rehearse found a networked client in its context")
        if ctx.ledger.messages.all():
            raise ConfigError("the rehearsal ledger already holds messages")
        if refcache is not None:
            log.info("reference cache: %d records", _load_refcache(ctx.ledger, refcache))
        recorder = ctx.model if isinstance(ctx.model, RecordingExtractor) else None
        messages: list[dict] = []
        for n, f in enumerate(files, 1):
            raw = f.read_bytes()
            row_id: int | None = None
            view: dict = {"status": "failed", "parser": None, "document_type": None, "items": []}
            try:
                res = process_raw(ctx, raw, source="imap", notify=False)
            except Exception:  # noqa: BLE001 - as in a poll: this message failed, the run goes on
                log.exception("fixture %d: unhandled error", n)
            else:
                row_id, view = res.message_row_id, _result_view(res)
            if recorder is not None:
                _answer_unasked(ctx, recorder, raw, row_id)
            messages.append({"n": n, **view})
        _write_private(report, {"schema": REPORT_SCHEMA, "messages": messages})
    finally:
        _close(ctx.ledger)
    return _counts(messages)
