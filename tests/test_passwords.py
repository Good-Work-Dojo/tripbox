# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page's password: the hash line `tripbox passwd` prints, its check of a typed password, and
the command itself, which needs no settings and no ledger."""

import base64
import getpass
import hashlib
import io
import re
import sys

import pytest

from tripbox import cli, passwords

# Obviously a stand-in: no real password is ever written in a test
FAKE = "plainly-a-test-password"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def cheap_hash(password: str, n: int = 16) -> str:
    """The hash line of `password` at a cost the suite can afford (scrypt n=`n`, r=1, p=1): valid
    input to `parse_hash`, as a line `tripbox passwd` printed is. The salt comes from the
    password, so one password gives one line."""
    salt = hashlib.sha256(password.encode()).digest()[:16]
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=1, p=1, dklen=32)
    return f"scrypt:{n}:1:1:{b64(salt)}:{b64(key)}"


def test_a_hash_round_trips_and_holds_no_dollar_sign():
    line = passwords.make_hash(FAKE)
    assert "$" not in line and "\n" not in line
    scheme, n, r, p, salt, key = line.split(":")
    assert (scheme, n, r, p) == ("scrypt", "32768", "8", "1")
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", salt) and re.fullmatch(r"[A-Za-z0-9_-]{43}", key)
    parsed = passwords.parse_hash(line)
    assert (parsed.n, parsed.r, parsed.p) == (32768, 8, 1)
    assert (len(parsed.salt), len(parsed.key)) == (16, 32)
    assert parsed.line == line
    assert passwords.matches(parsed, FAKE)
    assert not passwords.matches(parsed, FAKE + "!")
    # the same salt gives the same line, which is how a restart keeps its sessions
    assert passwords.make_hash(FAKE, parsed.salt) == line


def test_a_malformed_or_costly_hash_is_refused():
    good = cheap_hash(FAKE)
    _, _, _, _, salt, key = good.split(":")
    assert passwords.parse_hash(good).n == 16
    assert passwords.parse_hash(f"  {good}\n").line == good  # white space around it is no part
    # the dearest costs a line may name are taken (and never hashed here)
    assert passwords.parse_hash(f"scrypt:131072:16:4:{salt}:{key}").r == 16
    short_salt, short_key = b64(b"s" * 8), b64(b"k" * 16)
    for bad in (
        "",
        "scrypt",
        good.replace("scrypt:", "bcrypt:", 1),
        good.replace("scrypt:", "SCRYPT:", 1),
        f"TRIPBOX_PASSWORD_HASH={good}",  # the variable's name pasted in with it
        f"scrypt:16:1:{salt}:{key}",  # five parts
        f"{good}:extra",  # seven parts
        f"scrypt:16:1:1:{salt}:{key}=",  # padding
        f"scrypt:16:1:1:{salt}$:{key}",
        f"scrypt:16:1:1:{salt}:{key[:-1]}+",  # not the url-safe alphabet
        f"scrypt:16:1:1:{short_salt}:{key}",
        f"scrypt:16:1:1:{salt}:{short_key}",
        f"scrypt:0:1:1:{salt}:{key}",
        f"scrypt:1:1:1:{salt}:{key}",
        f"scrypt:24:1:1:{salt}:{key}",  # not a power of two
        f"scrypt:016:1:1:{salt}:{key}",
        f"scrypt:+16:1:1:{salt}:{key}",
        f"scrypt:１６:1:1:{salt}:{key}",
        f"scrypt:262144:8:1:{salt}:{key}",  # too costly
        f"scrypt:65536:1:1:{salt}:{key}",  # more than scrypt allows at r=1
        f"scrypt:16:0:1:{salt}:{key}",
        f"scrypt:16:17:1:{salt}:{key}",
        f"scrypt:16:1:0:{salt}:{key}",
        f"scrypt:16:1:5:{salt}:{key}",
        "scrypt:16:1:1::",
    ):
        with pytest.raises(ValueError):
            passwords.parse_hash(bad)


def test_an_overlong_typed_password_is_wrong_without_hashing(monkeypatch):
    stored = passwords.parse_hash(cheap_hash(FAKE))
    real = hashlib.scrypt
    hashed: list[int] = []

    def counted(*args, **kwargs):
        hashed.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(passwords.hashlib, "scrypt", counted)
    assert not passwords.matches(stored, "x" * (passwords.MAX_TYPED + 1))
    assert not passwords.matches(stored, FAKE + "x" * passwords.MAX_TYPED)
    assert hashed == []
    assert not passwords.matches(stored, "x" * passwords.MAX_TYPED) and hashed == [1]
    assert passwords.matches(stored, FAKE) and hashed == [1, 1]


# ----------------------------------------------------------------------------- tripbox passwd
class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def nothing_configured(tmp_path, monkeypatch):
    """Settings no command could start with, and where a ledger would go: passwd reads none of
    them and writes nothing."""
    monkeypatch.setenv("TZ", "Mars/Base")
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    return tmp_path


def test_passwd_prints_the_line_of_a_password_from_a_pipe_or_typed_twice(
    nothing_configured, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "stdin", io.StringIO(FAKE + "\r\nnot this line\n"))
    assert cli.main(["passwd"]) == 0
    seen = capsys.readouterr()
    (line,) = seen.out.splitlines()
    name, _, value = line.partition("=")
    assert name == "TRIPBOX_PASSWORD_HASH" and value.startswith("scrypt:32768:8:1:")
    assert passwords.matches(passwords.parse_hash(value), FAKE)
    assert FAKE not in seen.out + seen.err
    asked: list[str] = []
    typed = iter([FAKE, FAKE])

    def at_the_terminal(prompt: str = "Password: ", stream=None) -> str:
        asked.append(prompt)
        return next(typed)

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(getpass, "getpass", at_the_terminal)
    assert cli.main(["passwd"]) == 0
    seen = capsys.readouterr()
    (again,) = seen.out.splitlines()
    assert again.startswith("TRIPBOX_PASSWORD_HASH=scrypt:") and again != line  # a new salt
    assert len(asked) == 2 and FAKE not in seen.out + seen.err
    assert not (nothing_configured / "data").exists()
    assert not (nothing_configured / "logs").exists()


def test_passwd_refuses_a_short_password_or_two_that_differ(
    nothing_configured, monkeypatch, capsys
):
    for piped in ("", "\n", "nine-char\n", " " * 9 + "\n"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(piped))
        assert cli.main(["passwd"]) == 2, piped
        seen = capsys.readouterr()
        assert (seen.out, seen.err) == ("", "passwd: use 10 characters or more\n"), piped
    # longer than the page takes when it is typed: it could never sign in
    for piped in ("x" * 1025 + "\n", FAKE * 200 + "\n"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(piped))
        assert cli.main(["passwd"]) == 2, len(piped)
        seen = capsys.readouterr()
        assert (seen.out, seen.err) == ("", "passwd: use 1024 characters or fewer\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 1024 + "\n"))
    assert cli.main(["passwd"]) == 0
    assert capsys.readouterr().out.startswith("TRIPBOX_PASSWORD_HASH=scrypt:")
    for first, second, said in (
        (FAKE, FAKE + "x", "passwd: the two did not match"),
        ("short", "short", "passwd: use 10 characters or more"),
        ("y" * 1025, "y" * 1025, "passwd: use 1024 characters or fewer"),
    ):
        typed = iter([first, second])
        monkeypatch.setattr(sys, "stdin", Terminal())
        monkeypatch.setattr(
            getpass, "getpass", lambda prompt="", stream=None, typed=typed: next(typed)
        )
        assert cli.main(["passwd"]) == 2, said
        seen = capsys.readouterr()
        assert (seen.out, seen.err) == ("", said + "\n"), said
    assert not (nothing_configured / "data").exists()


def test_passwd_refuses_the_example_from_the_instructions(nothing_configured, monkeypatch, capsys):
    example = "<choose a password of 10 characters or more>"
    refused = "passwd: choose your own password, not the example\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(example + "\n"))
    assert cli.main(["passwd"]) == 2
    seen = capsys.readouterr()
    assert (seen.out, seen.err) == ("", refused)
    typed = iter([example, example])
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(getpass, "getpass", lambda prompt="", stream=None: next(typed))
    assert cli.main(["passwd"]) == 2
    seen = capsys.readouterr()
    assert (seen.out, seen.err) == ("", refused)
