# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Logging: stdout + rotating file, with secret redaction applied to the whole formatted record
(message and traceback alike)."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    # telegram bot token, also inside "https://api.telegram.org/bot<token>/...": no leading \b,
    # "bot1234" has no word boundary before the digits
    re.compile(r"\d{8,10}:[A-Za-z0-9_\-]{30,}"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=]{12,}"),
    re.compile(r"(client_secret=)[^&\s]+"),
    # the login an address carries (scheme://user:password@host), in whatever form a library
    # writes the address: up to the last @ before the host, stopping only where a login cannot
    # go unencoded (whitespace, / ? # " < >), so a password holding ' , ; or @, or one of any
    # length, is masked whole; masking more than the login is harmless. The scheme is bounded,
    # which keeps a long run of letters (a junk subject) linear: after a real :// the scan ends
    # once, at the next stop.
    re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^\s/?#\"<>]+(?=@)"),
]


_SECRETS: set[str] = set()


def add_secrets(values) -> None:
    """The configured secret values themselves (IMAP password, API keys): patterns cannot know
    their shape, so they are masked verbatim. Values under 8 characters are skipped — masking
    'a' everywhere would eat the log."""
    for v in values:
        if isinstance(v, str) and len(v) >= 8:
            _SECRETS.add(v)


def redact(text: str) -> str:
    """The configured values first, whole (a secret address goes with its path) and longest
    first (a secret that holds another goes whole), then the patterns."""
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
    return text


# A record logged with `extra=FILE_ONLY` is written to the log file and not to the terminal: a
# note kept for later that adds no line to a command's own output.
FILE_ONLY = {"file_only": True}


class _NotFileOnly(logging.Filter):
    """Lets through every record but one marked FILE_ONLY."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


class RedactingFormatter(logging.Formatter):
    """Redacts the fully formatted line, so exception text and tracebacks are covered too (a
    filter only sees the message; httpx errors carry the request URL, i.e. the bot token)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(
    logs_dir: Path | None = None, level: int = logging.INFO, name: str = "tripbox"
) -> logging.Logger:
    """`name` picks the file: the long-lived bot, web and worker processes get their own
    (tripbox-bot.log, tripbox-web.log, tripbox-worker.log), while the commands the worker
    starts share tripbox.log. A RotatingFileHandler shared by several processes loses the
    long-lived ones' lines after the first rotation — they keep writing to the file that was
    renamed away."""
    root = logging.getLogger()
    if getattr(root, "_tripbox_configured", False):
        return logging.getLogger("tripbox")
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    terminal = logging.StreamHandler()
    terminal.addFilter(_NotFileOnly())
    handlers: list[logging.Handler] = [terminal]
    no_file = None
    if logs_dir is not None:
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    logs_dir / f"{name}.log",
                    maxBytes=5 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
            )
        except OSError as exc:  # read-only or missing mount: stdout still works
            no_file = _why_no_file(logs_dir, name, exc)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    root._tripbox_configured = True  # type: ignore[attr-defined]
    if no_file is not None:  # said once the terminal's handler is there to say it
        logging.getLogger("tripbox").warning("file logging disabled: %s", no_file)
    return logging.getLogger("tripbox")


def _why_no_file(logs_dir: Path, name: str, exc: OSError) -> str:
    """Why the log file cannot be written, as `doctor` says it of the logs folder (the folder
    as compose mounts it, never a file's path inside it); a log file another user owns, as its
    own line with its fix; the error's own words when the folder shows nothing wrong."""
    from tripbox import files  # it reads the settings, which log nothing at import

    found = files.folder_problems("logs", logs_dir)
    if found:
        what, fix = found[0]
        return f"{what} — {fix}"
    shown = files.shown("logs", logs_dir)
    owned = files.another_owner(logs_dir / f"{name}.log")
    if owned is not None:
        who, uid, gid = owned
        relative = shown.removeprefix("./")
        return (
            f"{shown}/{name}.log belongs to another user ({who}) — on the host run:"
            f" sudo chown {uid}:{gid} {relative}/{name}.log"
        )
    return f"{shown}: {exc.strerror or type(exc).__name__}"
