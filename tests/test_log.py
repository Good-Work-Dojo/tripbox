# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import time

from tripbox.log import redact


def test_redact_masks_tokens_including_inside_telegram_urls():
    tok = "123456789:" + "A" * 35
    assert "123456789" not in redact(f"GET https://api.telegram.org/bot{tok}/getUpdates failed")
    assert redact(f"Authorization: Bearer {'x' * 40}") == "Authorization: Bearer [REDACTED]"
    assert redact("key sk-ant-api03-" + "q" * 20) == "key [REDACTED]"
    assert redact("client_secret=abc12345&grant_type=x") == "client_secret=[REDACTED]&grant_type=x"
    assert redact("nothing secret here 2027-06-22") == "nothing secret here 2027-06-22"


def test_redact_masks_the_login_in_any_address(monkeypatch):
    """The login an address carries is masked in whatever form a library writes the address,
    and a configured address is masked whole first, so a secret in its path goes too."""
    from tripbox import log

    monkeypatch.setattr(log, "_SECRETS", set())
    line = "HTTP Request: POST http://alex:S3CRET-PASS@models.example/v1/chat/completions 200"
    assert redact(line) == (
        "HTTP Request: POST http://[REDACTED]@models.example/v1/chat/completions 200"
    )
    assert redact("GET HTTPS://S3CRET-TOKEN@models.example/v1") == (
        "GET HTTPS://[REDACTED]@models.example/v1"
    )
    # a password holding an @, a ; a , or a ', or one of any length, is masked whole, to the
    # last @ before the host
    for password in ("S3C@RET", "S3C;RET", "S3C,RET", "S3C'RET", "S3CRET" + "k" * 2_000):
        masked = redact(f"at https://alex:{password}@models.example/v1")
        assert masked == "at https://[REDACTED]@models.example/v1", password[:8]
    # no login, an @ after the host, an email address, and an address beside an email address
    # in quoted text are left as they are
    for plain in (
        "GET http://models.example/v1/models?owner=alex@example.com",
        "from alex@example.com via mail.example",
        '{"url":"https://trips.example","from":"alex@example.com"}',
    ):
        assert redact(plain) == plain
    log.add_secrets(["https://alex:S3CRET-PASS@models.example/S3CRET-PATH/v1"])
    assert redact("GET https://alex:S3CRET-PASS@models.example/S3CRET-PATH/v1/models") == (
        "GET [REDACTED]/models"
    )


def test_an_address_beside_an_email_address_may_lose_its_host(monkeypatch):
    from tripbox import log

    monkeypatch.setattr(log, "_SECRETS", set())
    line = "https://x.example,alex@example.com"
    assert redact(line) == "https://[REDACTED]@example.com"


def test_redact_takes_linear_time_on_a_long_run_of_letters(monkeypatch):
    """A long run of letters with no space (a junk subject decoded from adjacent encoded words
    has none), alone or after an address's scheme, and a long run of schemes, are redacted in
    well under a second: every line logged and every notification passes through `redact`."""
    from tripbox import log

    monkeypatch.setattr(log, "_SECRETS", set())
    for line in ("a" * 600_000, "https://" + "a" * 600_000, "a://" * 100_000):
        started = time.perf_counter()
        assert redact(line) == line
        assert time.perf_counter() - started < 2.0, line[:12]


def test_a_secret_that_holds_another_is_masked_whole(monkeypatch):
    """Configured secrets are masked longest first: one that holds another leaves nothing of
    itself visible, in whatever order they were registered."""
    from tripbox import log

    inner, outer = "S3CRET-INNER", "outer-S3CRET-INNER-tail"
    # a dict keeps the order given: the shorter first, as a set may or may not put it
    monkeypatch.setattr(log, "_SECRETS", dict.fromkeys([inner, outer]))
    assert redact(f"a {outer} b {inner} c") == "a [REDACTED] b [REDACTED] c"


def test_formatter_redacts_tracebacks_too():
    import logging

    from tripbox.log import RedactingFormatter

    tok = "123456789:" + "B" * 35
    fmt = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{tok}/sendMessage timed out")
    except RuntimeError:
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "sending failed", (), True)
        import sys

        rec.exc_info = sys.exc_info()
    out = fmt.format(rec)
    assert "Traceback" in out and "sendMessage" in out and tok not in out and "[REDACTED]" in out


def test_configured_secret_values_are_redacted_too(tmp_path):
    import logging

    from tripbox.log import add_secrets, redact, setup_logging

    add_secrets(["app-passw0rd-xyz", "", "short"])
    assert redact("imap login with app-passw0rd-xyz failed") == "imap login with [REDACTED] failed"
    assert redact("short is too short to mask") == "short is too short to mask"
    root = logging.getLogger()
    root._tripbox_configured = False  # type: ignore[attr-defined]
    saved = list(root.handlers)
    try:
        setup_logging(tmp_path, name="tripbox-bot")
        assert (tmp_path / "tripbox-bot.log").exists()
    finally:
        for h in root.handlers[len(saved) :]:
            root.removeHandler(h)
        root._tripbox_configured = True  # type: ignore[attr-defined]


def test_a_file_only_record_reaches_the_log_file_and_not_the_terminal(tmp_path, capsys):
    import logging

    from tripbox.log import FILE_ONLY, setup_logging

    root = logging.getLogger()
    configured = getattr(root, "_tripbox_configured", False)
    root._tripbox_configured = False  # type: ignore[attr-defined]
    saved = list(root.handlers)
    try:
        setup_logging(tmp_path, name="tripbox")
        log = logging.getLogger("tripbox.test")
        log.info("noted in the file alone", extra=FILE_ONLY)
        log.info("said in both")
    finally:
        for h in root.handlers[len(saved) :]:
            h.close()
            root.removeHandler(h)
        root._tripbox_configured = configured  # type: ignore[attr-defined]
    written = (tmp_path / "tripbox.log").read_text(encoding="utf-8")
    shown = capsys.readouterr().err
    assert "noted in the file alone" in written and "said in both" in written
    assert "noted in the file alone" not in shown and "said in both" in shown


def test_a_log_file_another_user_owns_is_named_with_its_fix(tmp_path, monkeypatch, capsys):
    """A log file this user cannot append to because another user owns it turns file logging
    off, said in one line with the file and the command that gives it back."""
    import logging
    import logging.handlers
    import os

    from tripbox import files
    from tripbox.log import setup_logging

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tripbox.log").write_text("")

    def refused(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(logs / "tripbox.log"))

    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", refused)
    uid, gid = os.getuid(), os.getgid()
    real = files._owner

    def owned_by_another(path):
        if str(path).endswith(".log"):
            return uid + 1, gid + 1, 0o444
        return real(path)

    monkeypatch.setattr(files, "_owner", owned_by_another)
    root = logging.getLogger()
    saved = (getattr(root, "_tripbox_configured", False), list(root.handlers), root.level)
    for h in saved[1]:
        root.removeHandler(h)
    root._tripbox_configured = False
    try:
        setup_logging(logs)
    finally:
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)
        for h in saved[1]:
            root.addHandler(h)
        root.setLevel(saved[2])
        root._tripbox_configured = saved[0]
    said = [ln for ln in capsys.readouterr().err.splitlines() if "file logging" in ln]
    assert len(said) == 1
    assert said[0].endswith(
        f"file logging disabled: {logs}/tripbox.log belongs to another user (uid {uid}, gid {gid};"
        f" it belongs to uid {uid + 1}, gid {gid + 1}, mode 444) — on the host run:"
        f" sudo chown {uid}:{gid} {logs}/tripbox.log"
    )
