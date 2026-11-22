# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page's password, kept as one line: `scrypt:<n>:<r>:<p>:<salt>:<key>`, the salt (16 bytes)
and the key (32) in base64url without padding, so the line holds no `$` and a settings file
needs no quoting. `tripbox passwd` prints it; the page checks a typed password against it.
Standard library only: the page container loads it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

MIN_LENGTH = 10  # the shortest password the page takes
MAX_TYPED = 1024  # a typed value longer than this is wrong, and is never hashed
SCHEME = "scrypt"
# What a new hash costs: 32 MiB and a noticeable fraction of a second a check, one at a time
N, R, P = 32768, 8, 1
SALT_BYTES = 16
KEY_BYTES = 32
MAXMEM = 64 * 1024 * 1024
# The dearest line `parse_hash` takes: a check at most 256 MiB
MAX_N, MAX_R, MAX_P = 131072, 16, 4
_WHOLE = re.compile(r"[1-9][0-9]*")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Hash:
    """A parsed hash line: scrypt's costs, the salt and the key; `line` as it was written."""

    n: int
    r: int
    p: int
    salt: bytes
    key: bytes
    line: str

    def __repr__(self) -> str:  # never the salt or the key
        return f"Hash(n={self.n}, r={self.r}, p={self.p})"


def is_example(value: str) -> bool:
    """Whether `value` is written as the instructions' placeholder, `<…>`: pasted as it is, it is
    long enough, yet printed in every copy of the instructions."""
    value = value.strip()
    return value.startswith("<") and value.endswith(">")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str, size: int) -> bytes:
    """`size` bytes written in base64url without padding, and written only that way."""
    if not _BASE64URL.fullmatch(text):
        raise ValueError("not base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) != size or _encode(raw) != text:
        raise ValueError("not the expected size")
    return raw


def _bytes(text: str) -> bytes:
    """UTF-8, never failing: a lone surrogate (an environment value that was no UTF-8) is kept."""
    return text.encode("utf-8", "surrogatepass")


def _memory(n: int, r: int, p: int) -> int:
    """What scrypt needs for these costs (OpenSSL's count), and room above it."""
    return max(MAXMEM, 128 * r * (n + p + 2) + 1024 * 1024)


def make_hash(password: str, salt: bytes | None = None) -> str:
    """The hash line of `password`, under `salt` (16 bytes) or a new random one."""
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    if len(salt) != SALT_BYTES:
        raise ValueError(f"a salt is {SALT_BYTES} bytes")
    key = hashlib.scrypt(
        _bytes(password), salt=salt, n=N, r=R, p=P, maxmem=MAXMEM, dklen=KEY_BYTES
    )
    return f"{SCHEME}:{N}:{R}:{P}:{_encode(salt)}:{_encode(key)}"


def _whole(text: str, low: int, high: int) -> int:
    if not _WHOLE.fullmatch(text):
        raise ValueError("not a whole number")
    value = int(text)
    if not low <= value <= high:
        raise ValueError("out of range")
    return value


def parse_hash(text: str) -> Hash:
    """The hash a line `make_hash` wrote holds (white space around it ignored), else ValueError:
    six parts, scrypt, n a power of two from 2 to MAX_N (and one scrypt takes at that r), r and p
    from 1 to MAX_R and MAX_P, a 16-byte salt and a 32-byte key."""
    line = text.strip()
    parts = line.split(":")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise ValueError("not a hash line")
    n = _whole(parts[1], 2, MAX_N)
    r = _whole(parts[2], 1, MAX_R)
    p = _whole(parts[3], 1, MAX_P)
    if n & (n - 1) or n >= 2 ** (16 * r):
        raise ValueError("not a cost scrypt takes")
    return Hash(n, r, p, _decode(parts[4], SALT_BYTES), _decode(parts[5], KEY_BYTES), line)


def matches(hash: Hash, typed: str) -> bool:
    """Whether `typed` is the password of `hash`; a value over MAX_TYPED characters is not, and
    is not hashed. The keys are compared in constant time."""
    if len(typed) > MAX_TYPED:
        return False
    key = hashlib.scrypt(
        _bytes(typed),
        salt=hash.salt,
        n=hash.n,
        r=hash.r,
        p=hash.p,
        maxmem=_memory(hash.n, hash.r, hash.p),
        dklen=len(hash.key),
    )
    return hmac.compare_digest(key, hash.key)
