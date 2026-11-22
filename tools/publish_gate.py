#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Publish gate: proves that a tree about to be published holds nothing from a private denylist.

  publish_gate.py export --repo DIR --ref REF --out DIR --manifest FILE --denylist DIR
  publish_gate.py scan --tree DIR --denylist DIR [--parts b,c,d] [--json OUT]
  publish_gate.py images --denylist DIR --export-names FILE [--app-path PREFIX ...]
                         [--upstream GLOB | --own GLOB ...] IMAGE...
  publish_gate.py label --denylist DIR   (tokens on stdin, one per line; prints one label each)

Exit 0 clean, 1 findings, 2 usage or error. Output is one line per finding,
`<part> <kind> <path>[:<line>] [hmac:<label>] [×<count>]`, then `findings: N (accepted: M)`;
warnings go to stderr. A token finding's kind names the first way the token was found (see MODES);
`counts` (in `--json`) gives its occurrences in that file or member for each way, each occurrence
counted once, and `count` their sum (for a generic finding, the matches of that text). `line` is
the line of the first occurrence in a text file of the scanned tree (null elsewhere). Findings are
kept per file on disk, so two files whose names redact alike stay two findings. The `--json`
summary adds occurrences, tokens loaded per class, matches allowed by allow-paths.tsv, accepted
hits and token files read.

A denylist token is never printed, and neither is a part of a combo. A hit is named by its label,
the first 12 hex characters of HMAC-SHA256 of the folded token under the key in
`<denylist>/hmac.key` (32 random bytes, mode 600, created on first use), so a short token cannot be
recovered from its label by guessing. In a printed path each matched stretch of a component is
replaced by `[hmac:<label of the token>]` (a token allowed for that path stays), or the whole
component by `[redacted:hmac:<label of the component>]` when that cannot be done safely, or the
whole path likewise; control characters in a path print escaped, and error messages carry only
fixed text, counts, line numbers, denylist file numbers and redacted paths. An unexpected
exception prints its type, never its message.

Folding is NFKC, case folding and removal of zero-width characters and soft hyphens; a token's
whitespace is also collapsed to one space, while scanned text keeps its whitespace (so that
positions keep their line numbers): a match looks only at alphanumeric runs, and a token without
any is searched in the text with its whitespace collapsed alike. A token matches where the text's
maximal alphanumeric runs equal the token's runs, in order, so any non-alphanumeric characters may
separate them and the match needs a non-alphanumeric neighbour (or an edge) on both sides. Each
text is also read in a split view, where a backslash escape (\\n \\t \\r \\f \\v \\" \\' \\\\ \\/)
reads as a space and a boundary falls between a lowercase letter or digit and an uppercase
letter, and between an uppercase run and an uppercase-lowercase pair (`alexandriaShare`,
`HTMLQuill`), except inside an alphanumeric run longer than MAX_CAMEL_RUN characters (base64 data,
a minified bundle). A name or host token, or the local part or domain of an email token, of at
least 5 alphanumeric characters in one run is also found inside a longer run, and the runs of a
multi-run one joined together anywhere (kind `<class>-substring`). File names are searched for the
name-like classes and for date tokens, a date also in its `YYYYMMDD` form.

Classes: identifier, name, email, host, date, vendor, place and combo. A `place` (a street address
or a property name) is searched like a vendor. A `combo` is `part<US>part[<US>part]` (US is the
unit separator, U+001F): a finding in a file only when every part occurs in that file under the
rule above, one finding per file, labelled by the HMAC of the whole token; nothing is reported for
a single part. A combo is searched in every file and archive member, not in file names and not in
encoded forms, and a part never prints: in a path it is redacted like a token. A combo has two or
three parts of at least 3 characters; `label` takes one on stdin with a literal U+001F or the
four characters `\\x1f`.

Parts:
  a  export: `git archive REF | tar -x` into an empty OUT, then structural checks against a JSON
     manifest with the keys allowed_extensions, allowed_bare_names, top_level, forbidden_paths and
     executables
  b  PNG tEXt, zTXt, iTXt and eXIf chunks
  c  denylist tokens in contents and names: in decoded text (UTF-8, BOM-marked UTF-16/32, cp1252,
     or the UTF-8 and UTF-16 runs of a binary), in its percent-, \\u- and &#-decoded forms, and as
     hex and base64 (standard and URL-safe). Archives (zip, tar, gzip, xz/lzma, bz2, and zstd
     where Python provides it), recognised by magic number (a zip or tar named only by its
     extension is probed; anything else named like an archive is read as it is), are opened and
     their members scanned as `outer!member`, in bounded memory
  d  emails outside the example domains and the top-level labels reserved for tests and
     documentation (test, example, invalid, localhost; not local, lan or internal), RFC1918
     addresses, home-directory paths, and lockfile package sources outside the registries
  e  images: `docker save` into a private temporary directory, read from disk; layers,
     `docker inspect` env and labels and `docker history` are scanned with c and d (d only under
     the --app-path prefixes when given, and in env, labels and history), plus source maps, files
     whose basename is in --export-names, and layers the image manifest lists that are missing or
     are not archives. `--upstream GLOB` and `--own GLOB`, repeated in any order, sort each layer
     member (its path as named in the layer, with no leading `/`, matched with fnmatch, where `*`
     also matches `/`): the last rule that matches it wins, and a member none matches is own. An
     upstream member, and anything inside it, is public upstream bytes the project did not write:
     the matches of its contents and name (tokens, combos and part d) do not count toward the exit
     status, and the scan prints only
     `upstream: N findings in M members (public upstream bytes, not counted)`, never their tokens,
     labels or paths. Every other finding there (part e: a source map, an export file's name; a
     damaged or too deep archive) counts, as does every finding in an own member

Denylist directory: `*.tsv` with lines `class<TAB>token`; `allow-vendors.txt` (one vendor or place
per line, not denied); `allow-domains.txt` (one domain per line: an email address at that domain
or a subdomain of it is not a part d finding); `allow-generic.tsv` with `kind<TAB>glob`, kind
`private-ip` or `home-path` (a finding of that kind in a path matching the glob is dropped);
`allow-paths.tsv` with `class<TAB>token<TAB>glob[<TAB>kind]` (that token is allowed in paths
matching the glob, and is not redacted in them; with a kind, only findings of exactly that kind,
and in a printed path only matches of that kind's file-name form; a combo's token is the whole
combo); `accepted.tsv` with `label<TAB>path` (that hit is reported as accepted; `label` prints a
token's label, for a d finding the token is the matched text, and the path is written as on disk,
or as printed when no other file with a finding prints the same way); `hmac.key`. Anything else in
the directory (another file, a dotfile, a backup copy, a sub-directory) is an error, so a word list
saved under the wrong name cannot be silently skipped. Blank lines and `#` comments are ignored.
Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import bz2
import fnmatch
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import lzma
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import warnings
import zipfile
from bisect import bisect_right
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from itertools import accumulate, chain, compress, islice
from pathlib import Path
from urllib.parse import unquote_plus

try:  # zstd joins the standard library in Python 3.14
    from compression import zstd
except ImportError:
    zstd = None

COMBO = "combo"
SEP = "\x1f"             # joins the parts of a combo token
CLASSES = ("identifier", "name", "email", "host", "date", "vendor", "place", COMBO)
VARIANT_CLASSES = frozenset({"identifier", "name", "email", "host"})
DATA_CLASSES = frozenset({"date"})
NAME_CLASSES = VARIANT_CLASSES | {"vendor", "place"}    # the classes file names are searched for
ALL_CLASSES = frozenset(CLASSES)                         # the classes a printed path is checked for
DATA_EXTS = frozenset({".yaml", ".yml", ".json", ".csv", ".tsv", ".txt", ".eml", ".ics", ".sql"})
LOCK_FILES = frozenset({"uv.lock"})
ARCHIVE_EXTS = {".zip": "zip", ".tar": "tar", ".gz": "gzip", ".tgz": "gzip", ".xz": "xz",
                ".txz": "xz", ".lzma": "xz", ".bz2": "bz2", ".tbz2": "bz2", ".zst": "zstd"}
STEMS = ((".tgz", ".tar"), (".txz", ".tar"), (".tbz2", ".tar"), (".gz", ""), (".xz", ""),
         (".lzma", ""), (".bz2", ""), (".zst", ""))
SUBSTRING_CLASSES = frozenset({"name", "host", "email"})
GENERIC_KINDS = frozenset({"private-ip", "home-path"})   # the kinds allow-generic.tsv may name
# the ways a token can be found, in precedence order: a token finding takes the kind of the first
# it was found in, and each occurrence is counted in the first that finds it
MODES = ("", "percent", "unicode-escape", "html-entity", "hex", "base64", "substring")
MIN_TOKEN = 3
MIN_ENCODED = 6          # shorter encoded forms match random blobs: counted, not searched
MIN_SUBSTRING = 5        # a shorter name is too often a part of an ordinary word
MAX_CAMEL_RUN = 40       # a longer alphanumeric run gets no camel-case boundaries
CACHE_RUNS = 1 << 16     # how many distinct runs the substring search remembers before it restarts
MAX_FILE = 1_048_576
MAX_DEPTH = 8
CHUNK = 1 << 20          # content is read in pieces of this size: memory stays bounded
SEARCH = 4096            # how far back from a cut to look for a separator byte
TAIL = 64 << 10          # how many consumed bytes a stream keeps, for the tar end check
HEAD = 512               # enough to recognise every supported archive
KEY_FILE = "hmac.key"
NOT_TOKEN_FILES = ("allow-paths.tsv", "accepted.tsv", "allow-generic.tsv")
OTHER_FILES = ("allow-vendors.txt", "allow-domains.txt", KEY_FILE)   # the entries besides *.tsv
ALLOWED_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net",
                                   "users.noreply.github.com", "anthropic.com"})
# top-level labels reserved for tests and documentation (RFC 2606, RFC 6761): an address under one
# is no one's, so part d lets it pass; `.local`, `.lan` and `.internal` are real private networks
RESERVED_TLDS = frozenset({"test", "example", "invalid", "localhost"})
LOCK_HOSTS = frozenset({"files.pythonhosted.org", "pypi.org"})
HOME_IGNORED = frozenset({"user", "username", "you", "me", "name", "runner", "app", "node", "root"})
# 10/8, 172.16/12 and 192.168/16, written as integers so that this file passes its own private-ip
# rule
PRIVATE_NETS = tuple(ipaddress.IPv4Network(n) for n in (
    (10 << 24, 8), (172 << 24 | 16 << 16, 12), (192 << 24 | 168 << 16, 16)))
MANIFEST_KEYS = ("allowed_extensions", "allowed_bare_names", "top_level", "forbidden_paths",
                 "executables")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_TEXT = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf"})
INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠­﻿"))
INVISIBLE_CHAR = re.compile("[" + "".join(map(chr, INVISIBLE)) + "]")
URLSAFE = str.maketrans("+/", "-_")
# what a line or a field is stripped of: all whitespace but the unit separator, which ends a combo's
# last part; the second set is the strip of a line whose tabs are columns
EDGE = "".join(c for c in map(chr, range(0x3001)) if c.isspace() and c != SEP)
TAB_EDGE = " \r\n\x0b\x0c"

RUN = re.compile(r"[^\W_]+")                     # maximal runs of str.isalnum() characters
SPACE = re.compile(r"\s+")
LINE_SPACE = re.compile(r"[^\S\n]+")             # whitespace other than a newline
_LOWER = "".join(re.escape(chr(c)) for c in range(0x2000) if chr(c).islower())
_UPPER = "".join(re.escape(chr(c)) for c in range(0x2000) if chr(c).isupper())
# where the split view puts a space: over a backslash escape, and at a camel-case boundary (a
# lowercase letter or digit then an uppercase letter; an uppercase letter then an
# uppercase-lowercase pair)
SPLIT = re.compile(
    rf"""\\[ntrfv"'\\/]|(?=[{_UPPER}])(?:(?<=[{_LOWER}0-9])|(?<=[{_UPPER}])(?=.[{_LOWER}]))"""
)
SPLIT_CUT = re.compile(f"({SPLIT.pattern})")         # the same, kept by re.split
ISO_DATE = re.compile(r"([0-9]{4})[-/.]([0-9]{2})[-/.]([0-9]{2})")
LONG_RUN = re.compile(rf"[^\W_]{{{MAX_CAMEL_RUN + 1},}}")
LONG_SPLIT = re.compile(f"({LONG_RUN.pattern})")
BLANK = re.compile(rb"[\x00\t\n\r ]")
SEPARATOR = re.compile(rb"[\x00-\x2f\x3a-\x40\x5b-\x60\x7b-\x7f]")
# A binary is searched as runs of printable ASCII and, separately, of wider alphabets, so a stray
# byte that decodes to a letter next to a token cannot hide it.
ASCII_RUN = re.compile(rb"[\t\x20-\x7e]{4,}")
UTF16LE_ASCII = re.compile(rb"(?:[\t\x20-\x7e]\x00){4,}")
UTF16BE_ASCII = re.compile(rb"(?:\x00[\t\x20-\x7e]){4,}")
UTF8_RUN = re.compile(
    rb"(?:[\t\x20-\x7e]|[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}"
    rb"|[\xf0-\xf4][\x80-\xbf]{3}){4,}"
)
# UTF-16 code units for printable ASCII, Latin-1, Latin Extended, Greek, Cyrillic and general
# punctuation
UTF16LE_RUN = re.compile(
    rb"(?:[\t\x20-\x7e\xa0-\xff]\x00|[\x00-\xff][\x01\x04]|[\x00-\x4f]\x02|[\x70-\xff]\x03"
    rb"|[\x10-\x27]\x20){4,}"
)
UTF16BE_RUN = re.compile(
    rb"(?:\x00[\t\x20-\x7e\xa0-\xff]|[\x01\x04][\x00-\xff]|\x02[\x00-\x4f]|\x03[\x70-\xff]"
    rb"|\x20[\x10-\x27]){4,}"
)
HEX_RUN = re.compile(rf"[0-9a-f]{{{MIN_ENCODED},}}")
B64_RUN = re.compile(rf"[A-Za-z0-9+/_-]{{{MIN_ENCODED},}}")
PCT = re.compile(r"%[0-9A-Fa-f]{2}")
UESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
ENTITY = re.compile(r"&#(?:([0-9]{1,7})|[xX]([0-9a-fA-F]{1,6}));?")
# the T of an ISO timestamp, so a date matches inside one
ISO_T = re.compile(r"(?<=[0-9])t(?=[0-9])")
# the local part starts where a run of its characters starts, so a long run of dots is not
# rescanned per character
EMAIL = re.compile(r"(?<![a-z0-9._%+-])[a-z0-9._%+-]+@((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)
IPV4 = re.compile(r"(?<![0-9.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![0-9]|\.[0-9])")
# a path that starts at /home, /users or a WSL mount's /mnt/<drive>/users (not ./home, a relative
# directory), or a Windows profile directory
HOME = re.compile(
    r"(?:(?<![a-z0-9_.])/(?:home|users|mnt/[a-z]/users)/|\b[a-z]:(?:\\+|/)users(?:\\+|/))"
    r"([a-z0-9._-]+)",
    re.I,
)
UV_SOURCE = re.compile(r"""\b(?:url|registry|git)\s*=\s*["']([^"']*)["']""")
URL_HOST = re.compile(r"[a-z][a-z0-9+.-]*://([^/\s?#\\]+)", re.I)
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")


class GateError(Exception):
    """A failure the CLI reports with exit 2. Messages hold fixed text, counts, line numbers and
    redacted paths."""


class _Damage(Exception):
    """A read failure inside the archive at `owner` (None: a file of the scanned tree)."""

    def __init__(self, owner: str | None) -> None:
        super().__init__("damaged archive")
        self.owner = owner


@dataclass(frozen=True)
class Finding:
    part: str
    kind: str
    path: str
    token_hmac: str | None = None
    count: int = 1
    line: int | None = None
    # occurrences by mode; their sum is count
    counts: dict[str, int] = field(default_factory=lambda: {"": 1})

    def render(self) -> str:
        return (f"{self.part} {self.kind} {self.path}" + (f":{self.line}" if self.line else "")
                + (f" hmac:{self.token_hmac}" if self.token_hmac else "")
                + (f" ×{self.count}" if self.count > 1 else ""))


def _normal(s: str) -> str:
    """fold() without the whitespace collapse: it keeps every newline, so a position in the result
    has a line"""
    if s.isascii():                          # NFKC and invisible characters leave ASCII as it is
        return s.lower()
    if INVISIBLE_CHAR.search(s):
        s = s.translate(INVISIBLE)
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", s).casefold())


def fold(s: str) -> str:
    return SPACE.sub(" ", _normal(s))


def _fold_token(token: str) -> str:
    """The folded form of a token; each part of a combo folds on its own, because the unit
    separator is whitespace to a regular expression and must stay a separator. Whitespace beside
    the separator is not part of a combo's part."""
    if SEP not in token:
        return fold(token)
    return SEP.join(fold(part).strip() for part in token.split(SEP))


def _hmac12(key: bytes, s: str) -> str:
    return hmac.new(key, s.encode("utf-8", "surrogateescape"), hashlib.sha256).hexdigest()[:12]


def _basename(path: str) -> str:
    return re.split(r"[/!]", path)[-1]


def _escape(path: str) -> str:
    """`path` with backslashes, control, format and line-separator characters escaped, so it stays
    one output line"""
    if path.isprintable() and "\\" not in path:
        return path
    out = []
    for ch in path:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif 0xDC80 <= o <= 0xDCFF:                  # an undecodable byte of a file name
            out.append(f"\\x{o - 0xDC00:02x}")
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch) or
                       (f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}"))
        else:
            out.append(ch)
    return "".join(out)


DROPPED = "dropped:"       # marks a generic match that allow-generic.tsv drops


def _dropped(kind: str, dropped: set[str]) -> str:
    return DROPPED + kind if kind in dropped else kind


def _email_allowed(domain: str, extra: tuple[str, ...] = ()) -> bool:
    """Whether an address at `domain` is no finding: the built-in domains exactly, any domain under
    a top-level label reserved for tests and documentation, and each of the `extra` ones
    (allow-domains.txt) or any subdomain of one"""
    return (domain in ALLOWED_EMAIL_DOMAINS or domain.rpartition(".")[2] in RESERVED_TLDS
            or any(domain == d or domain.endswith(f".{d}") for d in extra))


# --- matchers ------------------------------------------------------------------------------------

Spans = dict[tuple[str, str], list[tuple[int, int]]]    # (class, folded token) -> where it occurs


def _run_starts(text: str, indices: set[int]) -> dict[int, int]:
    """Where each run of `text` whose index is in `indices` starts, in one pass that stops at the
    last of them"""
    out, runs, last = {}, RUN.finditer(text), 0
    for i in sorted(indices):
        out[i] = next(islice(runs, i - last, None)).start()
        last = i + 1
    return out


class Trie:
    """Tokens indexed by their alphanumeric runs. A token occurs wherever the text's maximal runs
    equal its runs in order, so one walk over the text's runs finds every hit; consecutive maximal
    runs always have a separator between them, and a maximal run always has a non-alphanumeric
    neighbour or an edge on each side."""

    def __init__(self) -> None:
        self.root: dict = {}
        self.bare: dict[str, set] = {}           # tokens with no alphanumeric character: substrings

    def add(self, token: str, payload: tuple[str, str]) -> None:
        runs = RUN.findall(token)
        if not runs:
            self.bare.setdefault(token, set()).add(payload)
            return
        node = self.root
        for r in runs:
            node = node.setdefault(r, {})
        node.setdefault(None, set()).add(payload)

    def find(self, text: str, runs: list[str] | None = None) -> Spans:
        """Every occurrence of every token in `text`; `runs` is RUN.findall(text) when the caller
        has it"""
        hits: Spans = {}
        if self.root:
            runs = RUN.findall(text) if runs is None else runs
            if not self.root.keys().isdisjoint(runs):
                found, n = [], len(runs)
                for i, r in enumerate(runs):
                    node, k = self.root.get(r), i
                    while node is not None:
                        if None in node:
                            found.append((node[None], i, k))
                        k += 1
                        node = node.get(runs[k]) if k < n else None
                if found:
                    at = _run_starts(text, {i for _, i, _ in found} | {k for _, _, k in found})
                    for payloads, i, k in found:
                        for p in payloads:
                            hits.setdefault(p, []).append((at[i], at[k] + len(runs[k])))
        if self.bare:
            text = LINE_SPACE.sub(" ", text)      # a token's whitespace folds to one space
            for tok, payloads in self.bare.items():
                at = text.find(tok)
                while at >= 0:
                    for p in payloads:
                        hits.setdefault(p, []).append((at, at + len(tok)))
                    at = text.find(tok, at + len(tok))
        return hits


class Grams:
    """Encoded forms, found anywhere inside a run of their alphabet: an encoded token normally sits
    inside a longer encoded blob, so no boundary applies. Each form is indexed by its first
    MIN_ENCODED characters, and the scan looks each window up in place, so memory does not grow
    with the run."""

    def __init__(self, run_re: re.Pattern) -> None:
        self.run_re, self.index = run_re, {}

    def add(self, form: str, payload: tuple[str, str]) -> None:
        self.index.setdefault(form[:MIN_ENCODED], {}).setdefault(form, set()).add(payload)

    def find(self, text: str) -> Spans:
        hits: dict[tuple[str, str], dict[int, int]] = {}
        index, w = self.index, MIN_ENCODED
        if not index:
            return {}
        for m in self.run_re.finditer(text):
            for k in range(m.start(), m.end() - w + 1):
                forms = index.get(text[k:k + w])
                if forms:
                    for form, payloads in forms.items():
                        if text.startswith(form, k):
                            for p in payloads:      # one occurrence per position, whichever form
                                at = hits.setdefault(p, {})
                                at[k] = max(at.get(k, 0), k + len(form))
        return {p: list(at.items()) for p, at in hits.items()}


class Substrings:
    """Forms found inside alphanumeric runs: a form marked `whole` also where it is the whole run,
    the others only inside a longer one. Each form is indexed by its first MIN_SUBSTRING
    characters. A scan looks at each distinct run once and remembers the answer, up to CACHE_RUNS
    runs of at most 64 characters, so a word that recurs costs one set lookup and memory stays
    bounded."""

    def __init__(self) -> None:
        self.index: dict[str, dict[str, dict[tuple[str, str], bool]]] = {}
        self.clean: set[str] = set()
        self.dirty: dict[str, list] = {}

    def add(self, form: str, payload: tuple[str, str], whole: bool) -> None:
        payloads = self.index.setdefault(form[:MIN_SUBSTRING], {}).setdefault(form, {})
        payloads[payload] = payloads.get(payload, False) or whole

    def _search(self, run: str) -> list[tuple[tuple[str, str], int, int]]:
        out, index, w = [], self.index, MIN_SUBSTRING
        for k in range(len(run) - w + 1):
            forms = index.get(run[k:k + w])
            if forms:
                for form, payloads in forms.items():
                    if run.startswith(form, k):
                        inside = len(form) < len(run)
                        out += [(p, k, k + len(form))
                                for p, whole in payloads.items() if whole or inside]
        return out

    def find(self, text: str, runs: list[str]) -> Spans:
        """Every occurrence in `text`, whose runs are `runs`"""
        if not self.index:
            return {}
        distinct = set(runs)
        found = {r: self.dirty[r] for r in distinct.intersection(self.dirty)}
        new = distinct - self.clean
        new.difference_update(found)
        if len(self.clean) > CACHE_RUNS or len(self.dirty) > CACHE_RUNS:
            self.clean.clear()
            self.dirty.clear()
        for r in new:
            hits = self._search(r) if len(r) >= MIN_SUBSTRING else []
            if hits:
                found[r] = hits
            if len(r) <= 64:
                if hits:
                    self.dirty[r] = hits
                else:
                    self.clean.add(r)
        if not found:
            return {}
        # the runs that hold a hit
        indices = list(compress(range(len(runs)), map(found.__contains__, runs)))
        at = _run_starts(text, set(indices))
        out: Spans = {}
        for i in indices:
            for p, a, b in found[runs[i]]:
                out.setdefault(p, []).append((at[i] + a, at[i] + b))
        return out


def _encodings(form: str) -> Iterator[tuple[str, str]]:
    """Hex of the UTF-8 bytes (lower case: scanned text is case-folded, which also covers
    upper-case hex), and the base64 characters fixed by the token alone at each of the three byte
    alignments, standard and URL-safe."""
    data = form.encode("utf-8")
    yield "hex", data.hex()
    for align in range(3):
        enc = base64.b64encode(b"\0" * align + data).decode("ascii")
        core = enc[-(-8 * align // 6):8 * (align + len(data)) // 6]
        yield "base64", core
        yield "base64", core.translate(URLSAFE)


# --- text views ----------------------------------------------------------------------------------

def _unescape_u(text: str) -> str:
    s = UESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _unescape_entities(text: str) -> str:
    def char(m: re.Match) -> str:
        cp = int(m.group(1)) if m.group(1) else int(m.group(2), 16)
        return chr(cp) if 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF else "�"
    return ENTITY.sub(char, text)


@dataclass
class Reading:
    """One decoded text of a piece and the views made from it: `folded`, the text folded; `split`,
    its split view folded (backslash escapes read as spaces, a space at each camel-case boundary),
    None when that adds nothing; and `decoded`, (mode, text, folded text) for its percent-, \\u-
    and &#-decoded forms. Folding keeps newlines, so a position in any view gives a line number.
    The split view is matched against the denylist only."""
    raw: str
    folded: str
    split: str | None
    decoded: list[tuple[str, str, str]]
    parts: list[str] = field(default_factory=list)      # _split_parts(raw), kept for _split_map


def _readings(texts: list[str]) -> list[Reading]:
    out = []
    for t in texts:
        decoded = []
        if PCT.search(t):
            decoded.append(("percent", unquote_plus(t)))
        if "\\u" in t:
            decoded.append(("unicode-escape", _unescape_u(t)))
        if "&#" in t:
            decoded.append(("html-entity", _unescape_entities(t)))
        parts = _split_parts(t) if SPLIT.search(t) else []
        split = _split_text(t, parts) if parts else t
        out.append(Reading(t, _normal(t), None if split is t else _normal(split),
                           [(mode, d, _normal(d)) for mode, d in decoded], parts))
    return out


def _split_parts(text: str) -> list[str]:
    """`text` cut around its alphanumeric runs longer than MAX_CAMEL_RUN (base64 data, a minified
    bundle): the odd items are those runs, which the split view leaves whole, since camel-case
    boundaries there would only chop noise into short words. An escape whose letter starts such a
    run is left alone too."""
    return LONG_SPLIT.split(text) if LONG_RUN.search(text) else [text]


def _split_text(text: str, parts: list[str]) -> str:
    """The split view of `text`, unfolded, from its _split_parts: each boundary becomes one space
    (`text` itself when there is none)"""
    split = SPLIT.sub(" ", text) if len(parts) == 1 else "".join(
        p if i % 2 else SPLIT.sub(" ", p) for i, p in enumerate(parts))
    return text if split == text else split


def _split_map(text: str, parts: list[str], folded: str, split: str) -> Callable[[int], int] | None:
    """A function from a position in `split` (the folded split view of `text`) to the position in
    `folded` (the folded `text`) of the same character; None when folding the stretches between
    boundaries one by one does not give both texts back (a boundary changed how a neighbouring
    character folds)"""
    segs, bounds = [""], []
    for i, part in enumerate(parts):
        if i % 2:
            segs[-1] += part
            continue
        items = SPLIT_CUT.split(part)             # stretch, boundary, stretch, ..., stretch
        segs[-1] += items[0]
        segs += items[2::2]
        bounds += items[1::2]
    fold_one = str.lower if text.isascii() else _normal
    segs, bounds = list(map(fold_one, segs)), list(map(fold_one, bounds))
    rebuilt = "".join(chain.from_iterable(zip(segs, [*bounds, ""], strict=True)))
    if rebuilt != folded or " ".join(segs) != split:
        return None
    lengths = list(map(len, segs))
    s_at = list(accumulate(map((1).__add__, lengths[:-1]), initial=0))
    f_at = list(accumulate((n + len(b) for n, b in zip(lengths[:-1], bounds, strict=True)),
                           initial=0))

    def to_folded(q: int) -> int:
        i = bisect_right(s_at, q) - 1
        return f_at[i] + q - s_at[i]
    return to_folded


def _bom(head: bytes) -> str | None:
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
                     (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if head.startswith(bom):
            return enc
    return None


def _decode(piece: bytes, encoding: str | None) -> list[str]:
    """The texts of a piece: as its BOM says; as UTF-8; when not UTF-8, as UTF-8 with replacement
    and as cp1252; and for a binary (it holds NUL bytes), its runs of UTF-8 and of UTF-16 in either
    byte order, one text per kind of run. The texts of a piece are alternative readings of the same
    bytes, so a count takes the largest of them."""
    if encoding:
        return [piece.decode(encoding, "replace")]
    if b"\0" not in piece:
        try:
            return [piece.decode("utf-8")]
        except UnicodeDecodeError:
            return [piece.decode("utf-8", "replace"), piece.decode("cp1252", "replace")]
    texts = []
    for pattern, encoding in ((ASCII_RUN, "ascii"), (UTF8_RUN, "utf-8"),
                              (UTF16LE_ASCII, "utf-16-le"), (UTF16LE_RUN, "utf-16-le"),
                              (UTF16BE_ASCII, "utf-16-be"), (UTF16BE_RUN, "utf-16-be")):
        text = "\n".join(m.group().decode(encoding, "replace") for m in pattern.finditer(piece))
        if text:
            texts.append(text)
    return texts


# --- the denylist --------------------------------------------------------------------------------

def _lines(path: Path, where: str, tabs: bool = False) -> Iterator[tuple[int, str]]:
    """(line number, stripped line) for each line that is not blank or a # comment; `where` names
    the file in errors. With `tabs`, a tab at either end is kept, so that an empty last column
    still counts as a column. A unit separator at either end is always kept."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise GateError(f"cannot read {where}") from None
    for n, line in enumerate(text.split("\n"), 1):
        kept = line.strip(TAB_EDGE if tabs else EDGE)
        if line.strip() and not line.strip().startswith("#"):
            yield n, kept


def _fields(line: str) -> list[str]:
    return [x.strip(EDGE) for x in line.split("\t")]


def _load_key(d: Path) -> bytes:
    """The label key in `d`, created (32 random bytes, mode 600) when absent"""
    d = Path(d)
    if not d.is_dir():
        raise GateError("denylist directory not found")
    p = d / KEY_FILE
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    except OSError:
        raise GateError(f"cannot create {KEY_FILE} in the denylist directory") from None
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    try:
        mode, key = os.stat(p).st_mode, p.read_bytes()
    except OSError:
        raise GateError(f"cannot read {KEY_FILE}") from None
    if mode & 0o077:
        raise GateError(f"{KEY_FILE} must be readable by its owner only (chmod 600)")
    if len(key) != 32:
        raise GateError(f"{KEY_FILE} must hold exactly 32 bytes")
    return key


def _kind(cls: str, mode: str, filename: bool = False) -> str:
    """The kind of a token finding: its class, the mode it was found in (none when plain),
    `filename` for a name"""
    return "-".join([cls, *([mode] if mode else []), *(["filename"] if filename else [])])


def _kinds(cls: str) -> set[str]:
    return {_kind(cls, mode, filename) for mode in MODES for filename in (False, True)}


def _substring_forms(cls: str, tok: str) -> Iterator[tuple[str, bool]]:
    """(form, whole) for each form of a folded token that is also searched inside runs: a single
    run of at least MIN_SUBSTRING characters inside a longer run (equal to the whole run it is an
    ordinary match), and the runs of a multi-run token joined together, there also as the whole
    run. The local part and the domain of an email each count as such a token."""
    for part in tok.rsplit("@", 1) if cls == "email" else [tok]:
        runs = RUN.findall(part)
        if len("".join(runs)) >= MIN_SUBSTRING:
            yield "".join(runs), len(runs) > 1


def _combo_parts(spelling: str) -> list[str] | None:
    """The folded parts of a combo spelling; None when it is too short to use: fewer than two
    distinct parts, or a part under MIN_TOKEN characters"""
    parts = [fold(part).strip() for part in spelling.split(SEP)]
    if len(set(parts)) < 2 or any(len(p) < MIN_TOKEN for p in parts):
        return None
    return parts


class Denylist:
    def __init__(self, entries: list[tuple[str, str]], key: bytes, allow_paths=(), accepted=(),
                 short: int = 0, files: int = 0, allow_domains=(), allow_generic=()) -> None:
        self.key, self.short, self.skipped, self.files = key, short, 0, files
        self.accepted = set(accepted)
        self.allow_domains = tuple(allow_domains)
        self.allow_generic = tuple(allow_generic)
        self.allow_paths: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
        for cls, tok, glob, *kind in allow_paths:
            self.allow_paths.setdefault((cls, tok), []).append((glob, kind[0] if kind else None))
        # contents: everything but data classes, data classes; file names add the YYYYMMDD form of
        # each date
        self.everywhere, self.data, self.compact = Trie(), Trie(), Trie()
        self.hex, self.b64 = Grams(HEX_RUN), Grams(B64_RUN)
        self.substrings = Substrings()
        # folded combo token -> its distinct folded parts, in order. The parts are searched like
        # tokens of class combo; a finding needs all of them in one file.
        self.combos: dict[str, tuple[str, ...]] = {}
        self._redacted: dict[str, str] = {}
        longest = 0
        spellings: dict[tuple[str, str], set[str]] = {}
        loaded: set[tuple[str, str]] = set()
        for cls, spelling in entries:
            if cls == COMBO:
                parts = _combo_parts(spelling)
                if not parts:
                    self.short += 1          # an unusable combo is ignored, as when loaded
                    continue
                tok = _fold_token(spelling)
                self.combos[tok] = tuple(dict.fromkeys(parts))
                for part in self.combos[tok]:
                    self.everywhere.add(part, (COMBO, part))
                loaded.add((cls, tok))
                longest = max(longest, *map(len, parts))
                continue
            tok = fold(spelling)
            loaded.add((cls, tok))
            (self.data if cls in DATA_CLASSES else self.everywhere).add(tok, (cls, tok))
            if cls == "date" and (m := ISO_DATE.fullmatch(tok)):
                self.compact.add("".join(m.groups()), (cls, tok))
            if cls in SUBSTRING_CLASSES:
                for form, whole in _substring_forms(cls, tok):
                    self.substrings.add(form, (cls, tok), whole)
            longest = max(longest, len(tok))
            if cls in VARIANT_CLASSES:
                spellings.setdefault((cls, tok), set()).add(spelling)
        self.tokens_by_class = {c: sum(1 for cls, _ in loaded if cls == c) for c in CLASSES}
        for payload, forms in spellings.items():
            encoded = {e for s in forms for f in (s, s.lower(), s.upper(), s.title())
                       for e in _encodings(f)}
            for mode, form in encoded:
                if len(form) < MIN_ENCODED:
                    self.skipped += 1
                else:
                    (self.hex if mode == "hex" else self.b64).add(form, payload)
                    longest = max(longest, len(form))
        # pieces of a large member overlap by more than the longest form in its widest encoding
        # (UTF-32)
        self.overlap = min(4 * longest + 64, CHUNK // 4)

    @classmethod
    def load(cls, d: Path) -> Denylist:
        d = Path(d)
        key = _load_key(d)
        try:
            listing = list(os.scandir(d))
            names = sorted(e.name for e in listing if e.is_file() and not e.name.startswith(".")
                           and (e.name.endswith(".tsv") or e.name in OTHER_FILES))
            unexpected = len(listing) - len(names)
        except OSError:
            raise GateError("cannot list the denylist directory") from None
        if unexpected:
            # counted, never named: a file name could itself be a token
            raise GateError(
                f"denylist directory holds {unexpected} unexpected entries (only *.tsv, "
                "allow-vendors.txt, allow-domains.txt and hmac.key belong there)")
        files = [d / n for n in names if n.endswith(".tsv") and n not in NOT_TOKEN_FILES]
        if not files:
            raise GateError("denylist directory holds no token files (*.tsv)")
        entries, short = [], 0
        for i, f in enumerate(files, 1):
            where = f"denylist file #{i}"        # a file name could itself be a token: by number
            for n, line in _lines(f, where):
                fields = _fields(line)
                if len(fields) != 2 or not fields[1]:
                    raise GateError(f"{where}, line {n}: expected class<TAB>token")
                if fields[0] not in CLASSES:
                    raise GateError(f"{where}, line {n}: unknown class")
                token = unicodedata.normalize("NFKC", fields[1])
                if fields[0] == COMBO:
                    if token.count(SEP) > 2:
                        raise GateError(f"{where}, line {n}: a combo has two or three parts")
                    usable = _combo_parts(token) is not None
                else:
                    usable = len(fold(token)) >= MIN_TOKEN
                if usable:
                    entries.append((fields[0], token))
                else:
                    short += 1
        vendors = d / "allow-vendors.txt"
        allowed = set()
        if vendors.exists():
            allowed = {fold(line) for _, line in _lines(vendors, vendors.name)}
        entries = [(c, s) for c, s in entries
                   if not (c in ("vendor", "place") and fold(s) in allowed)]
        if not entries:
            raise GateError("denylist holds no usable tokens")
        domains = []
        if (d / "allow-domains.txt").exists():
            for n, line in _lines(d / "allow-domains.txt", "allow-domains.txt"):
                if not DOMAIN.fullmatch(fold(line)):
                    raise GateError(f"allow-domains.txt, line {n}: expected one domain")
                domains.append(fold(line))
        generic = []
        if (d / "allow-generic.tsv").exists():
            for n, line in _lines(d / "allow-generic.tsv", "allow-generic.tsv", tabs=True):
                fields = _fields(line)
                if len(fields) != 2 or not all(fields):
                    raise GateError(f"allow-generic.tsv, line {n}: expected kind<TAB>glob")
                if fields[0] not in GENERIC_KINDS:
                    raise GateError(
                        f"allow-generic.tsv, line {n}: the kind must be private-ip or home-path")
                generic.append((fields[0], fields[1]))
        allow_paths = []
        if (d / "allow-paths.tsv").exists():
            for n, line in _lines(d / "allow-paths.tsv", "allow-paths.tsv", tabs=True):
                fields = _fields(line)
                if len(fields) not in (3, 4) or fields[0] not in CLASSES or not all(fields[1:]):
                    raise GateError(f"allow-paths.tsv, line {n}: "
                                    "expected class<TAB>token<TAB>glob[<TAB>kind]")
                if len(fields) == 4 and fields[3] not in _kinds(fields[0]):
                    raise GateError(f"allow-paths.tsv, line {n}: unknown kind for that class")
                allow_paths.append((fields[0], _fold_token(fields[1]), *fields[2:]))
        accepted = []
        if (d / "accepted.tsv").exists():
            for n, line in _lines(d / "accepted.tsv", "accepted.tsv"):
                fields = _fields(line)
                if (len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{12}", fields[0])
                        or not fields[1]):
                    raise GateError(f"accepted.tsv, line {n}: expected label<TAB>path")
                accepted.append((fields[0].lower(), fields[1]))
        return cls(entries, key, allow_paths, accepted, short, len(files), domains, generic)

    def label(self, token: str) -> str:
        return _hmac12(self.key, _fold_token(token))

    def warnings(self) -> list[str]:
        out = []
        if self.short:
            out.append(f"{self.short} denylist tokens shorter than {MIN_TOKEN} characters "
                       "are ignored")
        if self.skipped:
            out.append(f"{self.skipped} encoded forms shorter than {MIN_ENCODED} characters "
                       "are not searched")
        return out

    def _data(self, folded: str, filename: bool) -> Spans:
        """Data-class hits in a folded text: a T between digits (an ISO timestamp) is a boundary;
        in a file name a date also matches as YYYYMMDD. Every step keeps positions."""
        text = ISO_T.sub(" ", folded)
        hits = self.data.find(text)
        if filename:
            for p, spans in self.compact.find(text).items():
                hits.setdefault(p, []).extend(spans)
        return hits

    def _plain(self, folded: str, classes: frozenset, filename: bool,
               runs: list[str] | None = None) -> Spans:
        """The plain hits of the tokens of `classes` in one folded text"""
        hits = {p: spans for p, spans in self.everywhere.find(folded, runs).items()
                if p[0] in classes}
        if classes & DATA_CLASSES:
            for p, spans in self._data(folded, filename).items():
                if p[0] in classes:
                    hits.setdefault(p, []).extend(spans)
        return hits

    def _occurrences(self, r: Reading, classes: frozenset, filename: bool) -> tuple[dict, dict]:
        """(spans, extra) for one reading. spans: token -> mode -> set of (start, end), in the
        folded text for the plain, substring and hex modes and in the raw text for base64; the
        split view's hits join the plain ones through _split_map, or stay apart under "split" (in
        the split view's own positions) when it cannot map them. extra: token -> decoded mode ->
        (how many more plain hits that decoded text holds than the folded text, the decoded text,
        the start of its first hit). The parts of a combo are not looked for in decoded texts of
        file contents (only in names, where they are redacted)."""
        folded = r.folded
        runs = RUN.findall(folded)
        plain = self._plain(folded, classes, filename, runs)
        spans: dict = {p: {"": set(ss)} for p, ss in plain.items()}
        if r.split is not None:
            split = self._plain(r.split, classes, filename)
            to_folded = _split_map(r.raw, r.parts, folded, r.split) if split else None
            for p, ss in split.items():
                if to_folded is None:
                    spans.setdefault(p, {})["split"] = set(ss)
                else:
                    spans.setdefault(p, {}).setdefault("", set()).update(
                        (to_folded(a), to_folded(b - 1) + 1) for a, b in ss)
        found = []
        if classes & VARIANT_CLASSES:
            found += [("hex", self.hex.find(folded)), ("base64", self.b64.find(r.raw))]
        if classes & SUBSTRING_CLASSES:
            found.append(("substring", self.substrings.find(folded, runs)))
        for mode, hits in found:
            for p, ss in hits.items():
                if p[0] in classes:
                    spans.setdefault(p, {})[mode] = set(ss)
        extra: dict = {}
        decoded_classes = classes if filename else classes - {COMBO}
        for mode, _, text in r.decoded:
            for p, ss in self._plain(text, decoded_classes, filename).items():
                more = len({a for a, _ in ss}) - len({a for a, _ in plain.get(p, ())})
                if more > 0:
                    extra.setdefault(p, {})[mode] = (more, text, min(ss)[0])
        return spans, extra

    def detect(self, readings: list[Reading], classes: frozenset, filename: bool = False,
               lines: bool = False) -> dict[tuple[str, str], dict[str, list]]:
        """(class, folded token) -> mode -> [occurrences, newlines before the first] for each token
        of `classes` in the readings of one piece; the newline count only when `lines` is set, and
        `filename` adds the file-name forms of data tokens. Each occurrence counts once, in the
        first mode of MODES that finds it: the plain and split views count their distinct starts
        together, a substring hit at a start they have is not counted again, and a decoded text
        counts only the plain hits it holds beyond the folded text. The readings are alternative
        decodings of the same bytes, so each mode keeps the largest count any of them gives. A
        combo's parts come back as tokens of class combo."""
        out: dict = {}

        def put(p: tuple[str, str], mode: str, n: int, text: str, at: int) -> None:
            rec = out.setdefault(p, {}).setdefault(mode, [0, None])
            rec[0] = max(rec[0], n)
            if lines:
                nl = text.count("\n", 0, at)
                rec[1] = nl if rec[1] is None else min(rec[1], nl)
        for r in readings:
            spans, extra = self._occurrences(r, classes, filename)
            for p, modes in spans.items():
                starts = {m: sorted({a for a, _ in ss}) for m, ss in modes.items()}
                plain, split = starts.get("", []), starts.get("split", [])
                for text, at in ((r.folded, plain), (r.split, split)):
                    if at:
                        put(p, "", max(len(plain), len(split)), text, at[0])
                seen = set(plain)
                for mode, text in (("substring", r.folded), ("hex", r.folded), ("base64", r.raw)):
                    at = [a for a in starts.get(mode, ()) if mode != "substring" or a not in seen]
                    if at:
                        put(p, mode, len(at), text, at[0])
            for p, decoded in extra.items():
                for mode, (n, text, at) in decoded.items():
                    put(p, mode, n, text, at)
        return out

    def allowed(self, cls: str, tok: str, path: str, kind: str) -> bool:
        """Whether allow-paths.tsv allows a finding of `kind` for this token in `path`: a line
        without a kind allows every kind, a line with one only that kind"""
        return any((k is None or k == kind) and fnmatch.fnmatchcase(path, g)
                   for g, k in self.allow_paths.get((cls, tok), ()))

    def generic_allowed(self, kind: str, path: str) -> bool:
        """Whether allow-generic.tsv drops a part d finding of `kind` in `path`"""
        return any(k == kind and fnmatch.fnmatchcase(path, g) for k, g in self.allow_generic)

    def _shown(self, cls: str, tok: str, path: str, mode: str) -> bool:
        """Whether a match found in `mode` may print in `path`: a path component is a file name,
        so the match needs an allowance for that mode's file-name kind (or one without a kind)"""
        return self.allowed(cls, tok, path, _kind(cls, mode, True))

    def _denied(self, text: str, path: str) -> bool:
        """Whether `text`, read as a file name, holds a token that may not print in `path`"""
        return any(not self._shown(c, t, path, mode)
                   for (c, t), modes in
                   self.detect(_readings([text]), ALL_CLASSES, filename=True).items()
                   for mode in modes)

    def _component(self, part: str, path: str) -> str:
        """One path component as printed: each stretch that matched a token replaced by that
        token's label, or the whole component by its own label when a match has no stretch here
        (it was found only through a percent-, \\u- or &#-decoded reading, so cutting other
        stretches could leave part of it standing), two tokens overlap, or the result still
        matches"""
        whole = f"[redacted:hmac:{_hmac12(self.key, part)}]"
        if not part.isascii():
            return whole          # folding could move positions; an ASCII name folds to lower case
        spans, extra = self._occurrences(_readings([part])[0], ALL_CLASSES, True)
        if any(not self._shown(c, t, path, mode)
               for (c, t), decoded in extra.items() for mode in decoded):
            return whole
        stretches = set()
        for (c, t), modes in spans.items():
            for mode, ss in modes.items():
                if self._shown(c, t, path, "" if mode == "split" else mode):
                    continue
                if mode == "split":
                    return whole                  # split-view positions did not map onto the name
                stretches |= {(a, b, self.label(t)) for a, b in ss}
        merged: list[list] = []
        for a, b, label in sorted(stretches):
            if merged and a < merged[-1][1]:
                if merged[-1][2] != label:
                    return whole                  # two tokens overlap: no one label fits
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b, label])
        out, pos = [], 0
        for a, b, label in merged:
            out += [part[pos:a], f"[hmac:{label}]"]
            pos = b
        shown = "".join(out) + part[pos:]
        return whole if self._denied(shown, path) else shown

    def redact(self, path: str) -> str:
        """`path` as printed: each component that holds a token not allowed for this path
        redacted (see _component), and the whole path replaced by its label when a token still
        shows across components"""
        if path not in self._redacted:
            shown = path
            if self._denied(path, path):
                shown = "".join(
                    p if p in ("", "/", "!") or not self._denied(p, path)
                    else self._component(p, path)
                    for p in re.split(r"([/!])", path))
                if self._denied(shown, path):
                    shown = f"[redacted:hmac:{_hmac12(self.key, path)}]"
            self._redacted[path] = shown
        return self._redacted[path]


# --- streams and archives ------------------------------------------------------------------------

def _open(p: Path):
    return open(p, "rb")


class _Stream:
    """A forward-only reader over `f`: it can peek ahead, seeks forward by reading, keeps the last
    TAIL bytes it returned (for the tar end check), and turns any read failure into damage to the
    archive named `owner`."""

    def __init__(self, f, owner: str | None) -> None:
        self.f, self.owner, self.head, self.pos, self.tail = f, owner, b"", 0, b""

    def _raw(self, n: int) -> bytes:
        try:
            return self.f.read(n)
        except _Damage:
            raise
        except Exception:
            raise _Damage(self.owner) from None

    def peek(self, n: int) -> bytes:
        while len(self.head) < n:
            more = self._raw(n - len(self.head))
            if not more:
                break
            self.head += more
        return self.head[:n]

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self.head = self.head + self._raw(-1), b""
        elif self.head:
            data, self.head = self.head[:n], self.head[n:]
            if len(data) < n:
                data += self._raw(n - len(data))
        else:
            data = self._raw(n)
        self.pos += len(data)
        self.tail = data[-TAIL:] if len(data) >= TAIL else (self.tail + data)[-TAIL:]
        return data

    def since(self, offset: int) -> bytes:
        """The bytes returned from `offset` on, while they are still in the tail"""
        n = self.pos - offset
        if n > len(self.tail):
            raise _Damage(self.owner)
        return self.tail[len(self.tail) - n:] if n > 0 else b""

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        target = offset if whence == 0 else self.pos + offset if whence == 1 else None
        if target is None:
            raise io.UnsupportedOperation("seek from the end")
        if target < self.pos:
            back = self.pos - target
            if back > len(self.tail):
                raise io.UnsupportedOperation("seek back beyond the kept tail")
            self.head = self.tail[-back:] + self.head
            self.tail, self.pos = self.tail[:-back], target
        while self.pos < target and self.read(min(CHUNK, target - self.pos)):
            pass
        return self.pos

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True


def _cut(buf: bytes, pos: int, unit: int) -> int:
    """A cut at or before `pos`, aligned to `unit`: just after the last whitespace or NUL byte
    within SEARCH bytes, which no generic pattern spans (a multi-word token that does is whole in
    the overlap), else after any other separator byte, so that a piece's edge does not end a number
    or an address early"""
    window = buf[max(0, pos - SEARCH):pos][::-1]
    m = BLANK.search(window) or SEPARATOR.search(window)
    cut = pos - m.start() if m else pos
    return max(unit, cut - cut % unit)


def _pieces(f: _Stream, overlap: int, unit: int = 1) -> Iterator[tuple[bytes, int | None]]:
    """(piece, where the next piece starts in it) for the stream in pieces of at most CHUNK bytes,
    each overlapping the next by at least `overlap` bytes; the last piece has None"""
    buf = b""
    while True:
        while len(buf) <= CHUNK:
            more = f.read(CHUNK + 1 - len(buf))
            if not more:
                break
            buf += more
        if len(buf) <= CHUNK:
            if buf:
                yield buf, None
            return
        cut = _cut(buf, CHUNK, unit)
        keep = _cut(buf, cut - overlap, unit)
        yield buf[:cut], keep
        buf = buf[keep:]


def _spool(f: _Stream) -> tempfile.SpooledTemporaryFile:
    """A seekable copy of the stream: in memory up to CHUNK bytes, then in a private temporary
    file"""
    spool = tempfile.SpooledTemporaryFile(max_size=CHUNK)
    shutil.copyfileobj(f, spool, CHUNK)
    spool.seek(0)
    return spool


def _archive_kind(head: bytes) -> str | None:
    """The archive format by magic number; None for anything else"""
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    if head[:6] == b"\xfd7zXZ\x00" or head[:3] == b"\x5d\x00\x00":
        return "xz"
    if head[:3] == b"BZh" and len(head) > 3 and head[3:4] in b"123456789":
        return "bz2"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if head[257:262] == b"ustar":
        return "tar"
    return None


def _opens(kind: str, spool) -> bool:
    """Whether a zip or tar without its magic number at the start opens as one"""
    try:
        if kind == "zip":
            zipfile.ZipFile(spool).close()
        else:
            tarfile.open(fileobj=spool, mode="r:").close()
        return True
    except Exception:
        return False
    finally:
        spool.seek(0)


def _decompressor(kind: str, f: _Stream):
    return gzip.GzipFile(fileobj=f, mode="rb") if kind == "gzip" else _Decompressed(kind, f)


class _Decompressed:
    """The decompressed bytes of one or more concatenated xz/lzma, bz2 or zstd streams, read in
    bounded steps. Between and after streams only NUL padding may follow; anything else raises,
    where the standard readers would stop quietly at the end of the last stream and never look at
    what follows it."""

    def __init__(self, kind: str, f: _Stream) -> None:
        self.kind, self.f, self.out, self.done = kind, f, b"", False
        self.d = self._new()

    def _new(self):
        if self.kind == "xz":
            return lzma.LZMADecompressor()
        return bz2.BZ2Decompressor() if self.kind == "bz2" else zstd.ZstdDecompressor()

    def read(self, n: int = -1) -> bytes:
        while not self.done and (n is None or n < 0 or len(self.out) < n):
            self._step()
        if n is None or n < 0:
            data, self.out = self.out, b""
        else:
            data, self.out = self.out[:n], self.out[n:]
        return data

    def _step(self) -> None:
        if self.d.eof:
            data = self.d.unused_data.lstrip(b"\0")
            while not data:
                more = self.f.read(64 << 10)
                if not more:
                    self.done = True
                    return
                data = more.lstrip(b"\0")
            self.d = self._new()
        elif self.d.needs_input:
            data = self.f.read(64 << 10)
            if not data:
                raise EOFError("the compressed data ends inside a stream")
        else:
            data = b""
        self.out += self.d.decompress(data, CHUNK)


def _gzip_header(data: bytes) -> str:
    """The optional original file name and comment of a gzip header (ISO 8859-1)"""
    flags, pos, out = data[3], 10, []
    if flags & 4:
        pos += 2 + int.from_bytes(data[10:12], "little")
    for bit in (8, 16):
        if flags & bit:
            end = data.index(b"\0", pos)
            out.append(data[pos:end].decode("latin-1"))
            pos = end + 1
    return "\n".join(out)


def _stem(base: str) -> str:
    low = base.lower()
    for ext, repl in STEMS:
        if low.endswith(ext) and len(base) > len(ext):
            return base[:len(base) - len(ext)] + repl
    return base


def _member_dir(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith(("./", "/")):
        name = name[2:] if name.startswith("./") else name[1:]
    return name.rstrip("/")


# --- the scan ------------------------------------------------------------------------------------

# (part, class or kind, token) -> [kind, occurrences by mode, line]; part "allowed" holds the
# matches allow-paths.tsv allows, "c" a token finding (keyed by class: its kind follows from its
# modes), "d" a generic one (keyed by kind), "combo-part" a part of a combo found in a piece
Tally = dict[tuple[str, str, str], list]


UPSTREAM, OWN = "upstream", "own"     # the kinds of an image's member rules


class Scanner:
    """Collects findings for one run. `deny` may be None only for part a. With `app_paths`, part d
    applies inside an image only to layer members under those prefixes (and to env, labels and
    history). `rules` are (UPSTREAM or OWN, glob) pairs in order: the last one matching a layer
    member decides, and the content matches in an upstream member (tokens, combos and part d) are
    kept apart from the results; every other finding there counts."""

    def __init__(self, deny: Denylist | None, parts: set[str], export_names=frozenset(),
                 app_paths=None, rules=()) -> None:
        self.deny, self.parts, self.export_names = deny, set(parts), frozenset(export_names)
        self.app_paths = None if app_paths is None else tuple(map(_member_dir, app_paths))
        self.rules = tuple(rules)
        if any(kind not in (UPSTREAM, OWN) for kind, _ in self.rules):
            raise GateError("a member rule is upstream or own")
        # path -> the upstream layer member it lies in (the member itself, or an archive member
        # inside it), and the upstream member being scanned now
        self.upstream: dict[str, str] = {}
        self._in_upstream: str | None = None
        # (part, kind, path on disk, label) -> [occurrences by mode, line of the first
        # occurrence]. Keyed by the path on disk: two files can print alike once their names are
        # redacted.
        self.found: dict[tuple, list] = {}
        self.allowed = 0                          # matches that allow-paths.tsv allows
        # path -> folded combo part -> line of its first occurrence (None: not a text file); a
        # combo is a finding once every one of its parts is in one path's entry
        self.seen_parts: dict[str, dict[str, int | None]] = {}
        self._combo_done: set[tuple[str, str]] = set()

    def display(self, path: str) -> str:
        return _escape(self.deny.redact(path) if self.deny else path)

    def add(self, part: str, kind: str, path: str, token: str | None = None,
            counts: dict[str, int] | None = None, line: int | None = None) -> None:
        label = self.deny.label(token) if token is not None and self.deny else None
        self._mark(path)
        rec = self.found.setdefault((part, kind, path, label), [{}, None])
        for mode, n in (counts or {"": 1}).items():
            rec[0][mode] = rec[0].get(mode, 0) + n
        if rec[1] is None:                        # pieces arrive in order: first seen is first
            rec[1] = line

    def _combos(self) -> None:
        """The combos whose every part has been seen in one path: one finding each, or an allowed
        match when allow-paths.tsv allows the combo there"""
        if self.deny is None:
            return
        for path, seen in self.seen_parts.items():
            for combo, parts in self.deny.combos.items():
                if len(parts) < 2 or (path, combo) in self._combo_done:
                    continue                  # a combo of one part is never a finding
                if not all(p in seen for p in parts):
                    continue
                self._combo_done.add((path, combo))
                if self.deny.allowed(COMBO, combo, path, COMBO):
                    self.allowed += 1
                else:
                    self.add("c", COMBO, path, combo, None, seen[parts[0]])

    def results(self) -> tuple[list[Finding], list[Finding]]:
        """(findings, accepted). An accepted.tsv line names a path on disk, which accepts that
        file's hit, or a printed path, which accepts it only when no other file with a finding
        prints the same way. A count can end at 0 only when a piece's shared tail was reported
        under another kind by the next piece, which counted it there."""
        self._combos()
        shown = {path: self.display(path) for part, _, path, label in self.found
                 if not self._exempt(part, path, label)}
        owners: dict[str, set[str]] = {}
        for path, printed in shown.items():
            owners.setdefault(printed, set()).add(path)
        accepted = self.deny.accepted if self.deny else set()
        found, ok = [], []
        for (part, kind, path, label), (counts, line) in self.found.items():
            if self._exempt(part, path, label):
                continue
            counts = {m: n for m, n in counts.items() if n > 0}
            f = Finding(part, kind, shown[path], label, sum(counts.values()), line, counts)
            hit = label is not None and (
                (label, path) in accepted
                or ((label, shown[path]) in accepted and len(owners[shown[path]]) == 1))
            # a keyed label of the path on disk orders findings that print alike without
            # revealing it
            order = (part, shown[path], kind, label or "",
                     _hmac12(self.deny.key, path) if self.deny else path)
            (ok if hit else found).append((order, f))

        def first(item):
            return item[0]
        return [f for _, f in sorted(found, key=first)], [f for _, f in sorted(ok, key=first)]

    def upstream_counts(self) -> tuple[int, int]:
        """(findings, members): the content matches in upstream members, which results leaves
        out, and how many layer members hold them"""
        self._combos()
        paths = [path for part, _, path, label in self.found if self._exempt(part, path, label)]
        return len(paths), len({self.upstream[p] for p in paths})

    def _exempt(self, part: str, path: str, label: str | None) -> bool:
        """Whether a finding is a match of an upstream member's contents: a token or combo (part c
        with a label) or a generic pattern (part d). What the image checks find (a source map, an
        export file's name, a layer that is not an archive) and what the archive reader finds (a
        damaged or too deep archive, a special file) always count."""
        return path in self.upstream and (part == "d" or (part == "c" and label is not None))

    def _upstream_member(self, name: str) -> bool:
        """Whether a layer member is upstream: the last rule matching its name decides"""
        name, upstream = _member_dir(name), False
        for kind, glob in self.rules:
            if fnmatch.fnmatchcase(name, glob):
                upstream = kind == UPSTREAM
        return upstream

    def _mark(self, path: str) -> None:
        if self._in_upstream is not None:
            self.upstream.setdefault(path, self._in_upstream)

    def _generic_here(self, name: str) -> bool:
        if self.app_paths is None:
            return True
        name = _member_dir(name)
        return any(name == p or name.startswith(p + "/") for p in self.app_paths)

    def scan_tree(self, tree: Path) -> list[Finding]:
        tree = Path(tree)
        if not tree.is_dir():
            raise GateError("tree is not a directory")

        def fail(err: OSError) -> None:
            where = Path(err.filename or tree).relative_to(tree).as_posix()
            raise GateError(f"cannot list {self.display(where)}")
        for root, dirs, files in os.walk(tree, onerror=fail):
            dirs.sort()
            for name in dirs + sorted(files):
                p = Path(root, name)
                rel = p.relative_to(tree).as_posix()
                self.scan_name(name, rel)
                mode = os.lstat(p).st_mode
                if stat.S_ISLNK(mode):
                    self.scan_text(os.readlink(p), rel, True)
                elif stat.S_ISREG(mode):
                    try:
                        with _open(p) as fh:
                            self.scan_stream(_Stream(fh, None), rel, 0, True, lines=True)
                    except (OSError, _Damage) as e:
                        if isinstance(e, _Damage) and e.owner is not None:
                            raise
                        raise GateError(f"cannot read {self.display(rel)}") from None
                elif not stat.S_ISDIR(mode):
                    self.add("c", "special-file", rel)
        return self.results()[0]

    def scan_name(self, name: str, path: str) -> None:
        """The token findings in a file name; a combo is not searched for in names"""
        if "c" in self.parts:
            classes = NAME_CLASSES | DATA_CLASSES
            for (cls, tok), modes in self.deny.detect(_readings([name]), classes,
                                                      filename=True).items():
                kept = {}
                for mode, (n, _) in modes.items():
                    if self.deny.allowed(cls, tok, path, _kind(cls, mode, True)):
                        self.allowed += n
                    else:
                        kept[mode] = n
                if kept:
                    kind = _kind(cls, next(m for m in MODES if m in kept), True)
                    self.add("c", kind, path, tok, kept)

    def scan_text(self, raw: str, path: str, generic: bool | None) -> None:
        self.scan_texts([raw], path, generic)

    def scan_texts(self, texts: list[str], path: str, generic: bool | None) -> None:
        self._record(self._tally(_readings(texts), path, generic, None), path)

    def _tally(self, readings: list[Reading], path: str, generic: bool | None,
               line0: int | None) -> Tally:
        """The hits in one piece's readings; with `line0` (the newlines before the piece), each
        with its first line. A token is reported under the kind of the first of its modes that
        allow-paths.tsv does not allow, with its occurrences in each such mode. A part of a combo
        is an entry of part "combo-part" with the line of its first occurrence; _record keeps
        those per path, and _combos decides which combos are complete."""
        out: Tally = {}
        base = _basename(path)
        if "c" in self.parts:
            data = os.path.splitext(base)[1].lower() in DATA_EXTS and base not in LOCK_FILES
            classes = NAME_CLASSES | {COMBO} | (DATA_CLASSES if data else frozenset())
            for (cls, tok), modes in self.deny.detect(readings, classes,
                                                      lines=line0 is not None).items():
                if cls == COMBO:
                    nls = [rec[1] for rec in modes.values() if rec[1] is not None]
                    out[("combo-part", cls, tok)] = [
                        None, {"": 1}, line0 + min(nls) + 1 if nls and line0 is not None else None]
                    continue
                allowed = {m: rec for m, rec in modes.items()
                           if self.deny.allowed(cls, tok, path, _kind(cls, m))}
                kept = {m: rec for m, rec in modes.items() if m not in allowed}
                if allowed:
                    out[("allowed", cls, tok)] = [None, {m: rec[0] for m, rec in allowed.items()},
                                                  None]
                if kept:
                    nls = [rec[1] for rec in kept.values() if rec[1] is not None]
                    out[("c", cls, tok)] = [
                        _kind(cls, next(m for m in MODES if m in kept)),
                        {m: rec[0] for m, rec in kept.items()},
                        line0 + min(nls) + 1 if nls and line0 is not None else None]
        if "d" in self.parts and (self.app_paths is None if generic is None else generic):
            self._generic(readings, path, out, line0)
        return out

    def _record(self, tally: Tally, path: str) -> None:
        if tally:
            self._mark(path)                      # a combo completed later belongs here too
        for (part, _, token), (kind, counts, line) in tally.items():
            if part == "allowed":
                self.allowed += sum(counts.values())
            elif part == "combo-part":
                seen = self.seen_parts.setdefault(path, {})
                if token not in seen:
                    seen[token] = line
                elif line is not None and (seen[token] is None or line < seen[token]):
                    seen[token] = line
            else:
                self.add(part, kind, path, token, counts, line)

    def scan_stream(self, f: _Stream, path: str, depth: int, generic: bool | None,
                    layer: bool = False, lines: bool = False) -> None:
        """One file or member: opened when it is an archive, otherwise scanned in overlapping
        pieces. `generic` None means each member of the archive decides by its own name (the
        contents of an image layer); `lines` asks for line numbers (a file of the scanned tree)."""
        base = _basename(path)
        kind = _archive_kind(f.peek(HEAD))
        named = ARCHIVE_EXTS.get(os.path.splitext(base.lower())[1])
        # a zip with data before it, or an old tar, opens without the magic number, and so does an
        # image's empty layer (all zero blocks); anything else named like an archive holds no
        # compressed data and is read as it is
        probe = named if named in ("zip", "tar") else "tar" if layer else None
        if kind is None and probe:
            with _spool(f) as spool:
                self._dispatch(probe if _opens(probe, spool) else None, _Stream(spool, f.owner),
                               path, depth, generic, layer, lines)
            return
        self._dispatch(kind, f, path, depth, generic, layer, lines)

    def _dispatch(self, kind: str | None, f: _Stream, path: str, depth: int,
                  generic: bool | None, layer: bool, lines: bool = False) -> None:
        base = _basename(path)
        if layer and kind is None:
            self.add("e", "archive-unsupported", path)
        if kind:
            self._archive(kind, f, path, depth, generic)
            return
        if "b" in self.parts and base.lower().endswith(".png"):
            with _spool(f) as spool:
                self._png(spool, path)
                spool.seek(0)
                self._content(_Stream(spool, f.owner), path, generic, lines)
            return
        self._content(f, path, generic, lines)

    def _content(self, f: _Stream, path: str, generic: bool | None, lines: bool = False) -> None:
        """Content in overlapping pieces. A match wholly inside the stretch two pieces share is
        read by both, so each piece but the last subtracts what that shared stretch alone holds:
        every match is counted once."""
        encoding = _bom(f.peek(4))
        unit = 4 if encoding in ("utf-32-le", "utf-32-be") else 2 if encoding else 1
        line0 = 0
        for piece, keep in _pieces(f, self.deny.overlap if self.deny else 64, unit):
            text = encoding is not None or b"\0" not in piece          # a binary has no lines
            tally = self._tally(_readings(_decode(piece, encoding)), path, generic,
                                line0 if lines and text else None)
            if keep is not None:
                tail = self._tally(_readings(_decode(piece[keep:], encoding)), path, generic, None)
                for k, (_, counts, _) in tail.items():
                    if k in tally:                # per mode: the next piece counts these too
                        for mode, n in counts.items():
                            tally[k][1][mode] = tally[k][1].get(mode, 0) - n
                if lines:
                    head = piece[:keep]
                    if encoding:
                        line0 += head.decode(encoding, "replace").count("\n")
                    else:
                        line0 += head.count(b"\n")
            self._record(tally, path)

    def _archive(self, kind: str, f: _Stream, path: str, depth: int, generic: bool | None,
                 layers=None) -> None:
        if depth >= MAX_DEPTH:
            self.add("c", "archive-too-deep", path)
            return
        if kind == "zstd" and zstd is None:
            self.add("c", "archive-unsupported", path)
            return
        try:
            if kind == "zip":
                self._zip(f, path, depth, generic)
            elif kind == "tar":
                self._tar(f, path, depth, generic, layers)
            else:
                if kind == "gzip":
                    try:
                        self.scan_text(_gzip_header(f.peek(64 << 10)), path, generic)
                    except (IndexError, ValueError):
                        raise _Damage(path) from None
                payload = _Stream(_decompressor(kind, f), path)
                if payload.peek(HEAD)[257:262] == b"ustar":
                    self._tar(payload, path, depth, generic)
                else:
                    self._entry(_stem(_basename(path)), payload, "", path, depth, generic,
                                named=False)
        except _Damage as e:
            if e.owner != path:
                raise
            self.add("c", "archive-error", path)

    def _tar(self, f: _Stream, path: str, depth: int, generic: bool | None, layers=None) -> None:
        try:
            tf = tarfile.open(fileobj=f, mode="r:")
        except _Damage:
            raise
        except Exception:
            raise _Damage(path) from None
        while True:
            try:
                m = tf.next()
                member = tf.extractfile(m) if m is not None and m.isfile() else None
            except _Damage:
                raise
            except Exception:
                raise _Damage(path) from None
            if m is None:
                break
            tf.members = []                          # a layer can hold many members; keep none
            meta = "\n".join([m.linkname, m.uname, m.gname,
                              *(f"{k}={v}" for k, v in m.pax_headers.items())])
            self._entry(m.name, _Stream(member, path) if member else None, meta, path, depth,
                        generic, layer=layers is not None and m.name in layers)
        # tarfile stops quietly at a corrupt header after the first member, so all that follows
        # must be padding
        if f.since(tf.offset).strip(b"\0"):
            raise _Damage(path)
        while block := f.read(CHUNK):
            if block.strip(b"\0"):
                raise _Damage(path)

    def _zip(self, f: _Stream, path: str, depth: int, generic: bool | None) -> None:
        with _spool(f) as spool:
            try:
                zf = zipfile.ZipFile(spool)
            except Exception:
                raise _Damage(path) from None
            with zf:
                self.scan_text("\n".join(_decode(zf.comment, None)), path, generic)
                for info in zf.infolist():
                    meta = "\n".join(_decode(info.comment, None) + _decode(info.extra, None))
                    if info.is_dir():
                        self._entry(info.filename, None, meta, path, depth, generic)
                        continue
                    try:
                        member = zf.open(info)
                    except Exception:
                        raise _Damage(path) from None
                    with member:
                        self._entry(info.filename, _Stream(member, path), meta, path, depth,
                                    generic)

    def _entry(self, name: str, f: _Stream | None, meta: str, apath: str, depth: int,
               generic: bool | None, layer: bool = False, named: bool = True) -> None:
        path = f"{apath}!{name}"
        outer = self._in_upstream
        # `generic` None: a member of an image layer, which the member rules sort
        if outer is None and generic is None and named and self._upstream_member(name):
            self._in_upstream = path
        try:
            self._scan_entry(name, f, meta, path, depth, generic, layer, named)
        finally:
            self._in_upstream = outer

    def _scan_entry(self, name: str, f: _Stream | None, meta: str, path: str, depth: int,
                    generic: bool | None, layer: bool, named: bool) -> None:
        if layer:
            own = self.app_paths is None
        else:
            own = self._generic_here(name) if generic is None else generic
        if named:
            self.scan_name(name, path)
        # the image rules apply inside layers, not to the save archive's own manifest and config
        # files
        if "e" in self.parts and depth >= 1:
            if name.endswith(".map"):
                self.add("e", "source-map", path)
            if name.rstrip("/").rsplit("/", 1)[-1] in self.export_names:
                self.add("e", "export-json", path)
        if meta.strip():
            self.scan_text(meta, path, own)
        if f is not None:
            self.scan_stream(f, path, depth + 1, None if layer else own, layer)

    def _png(self, f, path: str) -> None:
        if f.read(len(PNG_SIG)) != PNG_SIG:
            self.add("b", "png-malformed", path)
            return
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            ctype, left = header[4:], int.from_bytes(header[:4], "big") + 4
            while left and (got := len(f.read(min(left, CHUNK)))):
                left -= got
            if left:
                break
            if ctype in PNG_TEXT:
                self.add("b", "png-text", path)
            if ctype == b"IEND":
                if f.read(1):
                    self.add("b", "png-malformed", path)
                return
        self.add("b", "png-malformed", path)

    def _generic(self, readings: list[Reading], path: str, out: Tally,
                 line0: int | None) -> None:
        """Part d's hits, into `out`: per text (each reading and its decoded forms) a count and the
        newlines before the first, then over those texts (other readings of the same bytes) the
        largest count and the earliest line. Addresses at a domain of allow-domains.txt, and
        addresses and home paths in a path allow-generic.tsv names, are dropped."""
        best: dict[tuple[str, str], list] = {}
        base = _basename(path)
        extra = self.deny.allow_domains if self.deny else ()
        dropped = {kind for kind in GENERIC_KINDS
                   if self.deny and self.deny.generic_allowed(kind, path)}

        def take(kind: str, value: str, text: str, at: int, seen: dict) -> None:
            rec = seen.setdefault((kind, value), [0, None])
            rec[0] += 1
            if rec[1] is None and line0 is not None:
                rec[1] = text.count("\n", 0, at)

        def merge(seen: dict) -> None:
            for key, (n, nl) in seen.items():
                rec = best.setdefault(key, [0, None])
                rec[0] = max(rec[0], n)
                rec[1] = nl if rec[1] is None or (nl is not None and nl < rec[1]) else rec[1]
        texts = [("", r.raw, r.folded) for r in readings] + [d for r in readings for d in r.decoded]
        for mode, raw, folded in texts:
            seen: dict = {}
            for m in EMAIL.finditer(folded):
                if not _email_allowed(m.group(1), extra):
                    take("email", m.group(0), folded, m.start(), seen)
            for m in IPV4.finditer(folded):
                octets = [int(g) for g in m.groups()]
                if max(octets) <= 255 and any(
                        ipaddress.IPv4Address(".".join(map(str, octets))) in net
                        for net in PRIVATE_NETS):
                    take(_dropped("private-ip", dropped), m.group(0), folded, m.start(), seen)
            for m in HOME.finditer(folded):
                if m.group(1) not in HOME_IGNORED:
                    take(_dropped("home-path", dropped), m.group(0), folded, m.start(), seen)
            if base in LOCK_FILES and not mode:
                # only the package sources: funding, homepage and bug-tracker URLs are metadata
                for m in UV_SOURCE.finditer(raw):
                    url = URL_HOST.match(m.group(1).strip())
                    host = fold(url.group(1)).rsplit("@", 1)[-1].split(":", 1)[0] if url else None
                    if host and host not in LOCK_HOSTS:
                        take("lock-host", host, raw, m.start(), seen)
            merge(seen)
        for (kind, value), (n, nl) in best.items():
            if kind.startswith(DROPPED):          # counted with the matches allowed by path
                out[("allowed", kind, value)] = [None, {"": n}, None]
            else:
                out[("d", kind, value)] = [kind, {"": n}, None if nl is None else line0 + nl + 1]


def scan_tree(tree: Path, deny: Denylist, parts: set[str]) -> list[Finding]:
    return Scanner(deny, parts).scan_tree(tree)


# --- part a: export ------------------------------------------------------------------------------

def _manifest(doc) -> dict[str, set[str]]:
    if not isinstance(doc, dict) or set(doc) != set(MANIFEST_KEYS):
        raise GateError("manifest must be a JSON object with exactly the keys "
                        + ", ".join(MANIFEST_KEYS))
    m = {}
    for k in MANIFEST_KEYS:
        listed = isinstance(doc[k], list | tuple | set | frozenset)
        if not listed or not all(isinstance(x, str) and x for x in doc[k]):
            raise GateError(f"manifest {k} must be a list of non-empty strings")
        m[k] = set(doc[k])
    m["allowed_extensions"] = {e if e.startswith(".") else "." + e for e in m["allowed_extensions"]}
    m["forbidden_paths"] = {p.removeprefix("./").strip("/") for p in m["forbidden_paths"]}
    return m


def load_manifest(path: Path) -> dict[str, set[str]]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GateError("manifest is unreadable or not JSON") from None
    return _manifest(doc)


def check_export_tree(out: Path, manifest, deny: Denylist | None = None,
                      scanner: Scanner | None = None) -> list[Finding]:
    """The structural checks on an extracted tree; `manifest` is the parsed JSON document or
    load_manifest's result"""
    m = _manifest(manifest)
    sc = scanner or Scanner(deny, {"a"})
    out = Path(out)
    top = set(os.listdir(out))
    for name in sorted(top - m["top_level"]):
        sc.add("a", "top-level-extra", name)
    for name in sorted(m["top_level"] - top):
        sc.add("a", "top-level-missing", name)

    def fail(err: OSError) -> None:
        raise GateError("cannot list the exported tree")
    executables = set()
    for root, dirs, files in os.walk(out, onerror=fail):
        dirs.sort()
        for name in sorted(dirs + files):
            p = Path(root, name)
            rel, st = p.relative_to(out).as_posix(), os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                continue
            if any(rel == f or rel.startswith(f + "/") for f in m["forbidden_paths"]):
                sc.add("a", "forbidden-path", rel)
            if stat.S_ISLNK(st.st_mode):
                sc.add("a", "symlink", rel)
                continue
            if not stat.S_ISREG(st.st_mode):
                sc.add("a", "special-file", rel)
                continue
            if name == ".gitmodules":
                sc.add("a", "gitmodules", rel)
            if name == ".gitattributes" and b"export-subst" in p.read_bytes():
                sc.add("a", "export-subst", rel)
            if st.st_size > MAX_FILE:
                sc.add("a", "large-file", rel)
            ext = os.path.splitext(name)[1]
            if ext and ext not in m["allowed_extensions"]:
                sc.add("a", "extension", rel)
            if not ext and name not in m["allowed_bare_names"]:
                sc.add("a", "bare-name", rel)
            if st.st_mode & 0o111:
                executables.add(rel)
    for rel in sorted(executables - m["executables"]):
        sc.add("a", "executable-extra", rel)
    for rel in sorted(m["executables"] - executables):
        sc.add("a", "executable-missing", rel)
    return sc.results()[0]


def _git(repo: Path, *args: str) -> bytes:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    except OSError:
        raise GateError("cannot run git") from None
    if p.returncode != 0:
        raise GateError(f"git {args[0]} failed (exit {p.returncode})")
    return p.stdout


def export(repo: Path, ref: str, out: Path, manifest_path: Path, deny: Denylist | None) -> Scanner:
    m = load_manifest(manifest_path)
    if ref.startswith("-"):
        raise GateError("the ref must not start with '-'")
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise GateError("the output directory must be empty or absent")
    _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{tree}")
    out.mkdir(parents=True, exist_ok=True)
    try:
        git = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", ref],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        tar = subprocess.Popen(["tar", "-x", "-C", str(out)], stdin=git.stdout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        raise GateError("cannot run git or tar") from None
    git.stdout.close()
    if tar.wait() != 0 or git.wait() != 0:
        raise GateError(f"git archive | tar -x failed (exit {git.returncode}, {tar.returncode})")
    sc = Scanner(deny, {"a"})
    # export-subst expands commit metadata at archive time; an export-ignored .gitattributes still
    # applies it
    for name in _git(repo, "ls-tree", "-r", "-z", "--name-only", ref).split(b"\0"):
        if name.rsplit(b"/", 1)[-1] == b".gitattributes":
            rel = name.decode("utf-8", "surrogateescape")
            if b"export-subst" in _git(repo, "cat-file", "blob", f"{ref}:{rel}"):
                sc.add("a", "export-subst", rel)
    info = Path(repo) / _git(repo, "rev-parse", "--git-path", "info/attributes").decode().strip()
    if info.is_file() and b"export-subst" in info.read_bytes():
        sc.add("a", "export-subst", ".git/info/attributes")
    check_export_tree(out, m, scanner=sc)
    return sc


# --- part e: images ------------------------------------------------------------------------------

def _run_docker(argv: list[str]) -> bytes:
    p = subprocess.run(argv, capture_output=True)
    if p.returncode != 0:
        raise GateError(f"{argv[0]} {argv[1]} exited {p.returncode}")
    return p.stdout


def scan_images(images: list[str], deny: Denylist, export_names,
                run: Callable[[list[str]], bytes], app_paths=None, rules=()) -> Scanner:
    """`run(argv)` runs one docker command and returns its stdout; for `docker save -o PATH IMAGE`
    it must leave the image archive at PATH, inside a mode-700 temporary directory that is removed
    afterwards. `rules` are the member rules, (UPSTREAM or OWN, glob) in order."""
    sc = Scanner(deny, {"c", "d", "e"}, export_names, app_paths, rules)

    def docker(*args: str) -> bytes:
        try:
            out = run(["docker", *args])
        except Exception:
            raise GateError(f"docker {args[0]} failed for image {sc.display(args[-1])}") from None
        if not isinstance(out, bytes):
            raise GateError(
                f"docker {args[0]} returned no bytes for image {sc.display(args[-1])}")
        return out
    work = tempfile.mkdtemp(prefix="publish-gate-")
    try:
        for image in images:
            save = os.path.join(work, "image.tar")
            docker("save", "-o", save, image)
            try:
                os.chmod(save, 0o600)
                with tarfile.open(save, "r:") as tf:
                    names = set(tf.getnames())
                    doc = json.load(tf.extractfile("manifest.json"))
                layers = {layer for entry in doc for layer in entry["Layers"]}
            except (OSError, ValueError, LookupError, TypeError, AttributeError, tarfile.TarError):
                raise GateError(f"the saved archive of image {sc.display(image)} "
                                "has no readable manifest.json") from None
            for layer in sorted(layers - names):
                sc.add("e", "layer-missing", f"{image}!{layer}")
            with open(save, "rb") as fh:
                sc._archive("tar", _Stream(fh, image), image, 0, sc.app_paths is None, layers)
            os.unlink(save)
            try:
                config = json.loads(docker("inspect", image))[0]["Config"]
                labels = config.get("Labels") or {}
                text = "\n".join([*(config.get("Env") or []),
                                  *(f"{k}={v}" for k, v in labels.items())])
            except (ValueError, LookupError, TypeError, AttributeError):
                raise GateError(f"docker inspect output for image {sc.display(image)} "
                                "is not understood") from None
            sc.scan_text(text, f"{image}!docker-inspect", True)
            history = docker("history", "--no-trunc", image)
            sc.scan_texts(_decode(history, None), f"{image}!docker-history", True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return sc


# --- CLI -----------------------------------------------------------------------------------------

def _report(sc: Scanner, json_out: str | None) -> int:
    findings, accepted = sc.results()
    for f in findings:
        print(f.render())
    for f in accepted:
        print(f"accepted {f.render()}")
    if sc.rules:
        n, members = sc.upstream_counts()
        print(f"upstream: {n} findings in {members} members (public upstream bytes, not counted)")
    print(f"findings: {len(findings)} (accepted: {len(accepted)})")
    if json_out:
        doc = {"findings": [asdict(f) for f in findings], "accepted": [asdict(f) for f in accepted],
               "warnings": sc.deny.warnings() if sc.deny else [],
               "summary": {"findings": len(findings), "accepted": len(accepted),
                           "occurrences": sum(f.count for f in findings),
                           "tokens_by_class": sc.deny.tokens_by_class if sc.deny else {},
                           "allowed": sc.allowed,
                           "denylist_files": sc.deny.files if sc.deny else 0}}
        # owner-only whatever the umask: it holds paths and keyed labels of a private tree
        fd = os.open(json_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(json.dumps(doc, indent=2) + "\n")
    return 1 if findings else 0


def _load(path: str) -> Denylist:
    deny = Denylist.load(Path(path))
    for w in deny.warnings():
        print(f"warning: {w}", file=sys.stderr)
    return deny


def _cmd_export(args, run) -> int:
    deny = _load(args.denylist)
    return _report(export(Path(args.repo), args.ref, Path(args.out), Path(args.manifest), deny),
                   None)


def _cmd_scan(args, run) -> int:
    parts = set(args.parts.split(","))
    if not parts or not parts <= {"b", "c", "d"}:
        raise GateError("--parts takes a comma-separated subset of b, c, d")
    sc = Scanner(_load(args.denylist), parts)
    sc.scan_tree(Path(args.tree))
    return _report(sc, args.json)


def _cmd_images(args, run) -> int:
    deny = _load(args.denylist)
    try:
        listing = Path(args.export_names).read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        raise GateError("cannot read the export names file") from None
    names = {line.strip() for line in listing if line.strip() and not line.strip().startswith("#")}
    return _report(scan_images(args.image, deny, names, run, args.app_path, args.rules or ()),
                   None)


def _cmd_label(args, run) -> int:
    key = _load_key(Path(args.denylist))
    for line in sys.stdin:
        token = line.strip(EDGE).replace("\\x1f", SEP)       # a combo: U+001F, or the escape
        if token.strip():
            print(_hmac12(key, _fold_token(token)))
    return 0


class _Rule(argparse.Action):
    """--upstream and --own share one list, so their order on the command line is kept"""

    def __call__(self, parser, namespace, values, option_string=None):
        rules = getattr(namespace, self.dest) or []
        setattr(namespace, self.dest, [*rules, (self.const, values)])


class _Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors never repeat a value from the command line, which
    could be a token: only the messages that name options alone keep their text."""

    SAFE = re.compile(r"the following arguments are required: [-\w, /]+"
                      r"|argument [-\w/]+: expected (?:one|at least one) argument")

    def error(self, message: str):
        self.print_usage(sys.stderr)
        shown = message if self.SAFE.fullmatch(message) else (
            "invalid arguments (their values are not repeated)")
        self.exit(2, f"{self.prog}: error: {shown}\n")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="publish_gate.py",
                description="Prove a tree to be published holds nothing from a private denylist. "
                            "Exit 0 clean, 1 findings, 2 usage or error.")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="git archive REF into an empty OUT and check its structure")
    e.add_argument("--repo", required=True)
    e.add_argument("--ref", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--manifest", required=True)
    e.add_argument("--denylist", required=True, help="the denylist, so printed paths are redacted")
    e.set_defaults(func=_cmd_export)
    s = sub.add_parser("scan", help="scan a tree for PNG text, denylist tokens and generic leaks")
    s.add_argument("--tree", required=True)
    s.add_argument("--denylist", required=True)
    s.add_argument("--parts", default="b,c,d")
    s.add_argument("--json")
    s.set_defaults(func=_cmd_scan)
    i = sub.add_parser("images", help="scan docker images")
    i.add_argument("--denylist", required=True)
    i.add_argument("--export-names", required=True)
    i.add_argument("--app-path", action="append",
                   help="limit the generic rules to layer members under this prefix")
    i.add_argument("--upstream", dest="rules", action=_Rule, const=UPSTREAM, metavar="GLOB",
                   help="layer members matching this are public upstream bytes, not counted "
                        "(the last matching --upstream or --own decides)")
    i.add_argument("--own", dest="rules", action=_Rule, const=OWN, metavar="GLOB",
                   help="layer members matching this are the project's own, counted")
    i.add_argument("image", nargs="+")
    i.set_defaults(func=_cmd_images)
    lb = sub.add_parser("label", help="print the label of each token read from stdin")
    lb.add_argument("--denylist", required=True)
    lb.set_defaults(func=_cmd_label)
    return p


def _withheld(message, category, filename, lineno, file=None, line=None) -> None:
    print(f"warning: {category.__name__} (details withheld)", file=sys.stderr)


def main(argv: list[str] | None = None,
         run: Callable[[list[str]], bytes] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    with warnings.catch_warnings():
        warnings.showwarning = _withheld
        try:
            return args.func(args, run or _run_docker)
        except GateError as e:
            print(f"error: {e}", file=sys.stderr)
        except Exception as e:  # the message of an unexpected error can quote scanned content
            print(f"error: unexpected {type(e).__name__}; details withheld", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
