# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Who is on the ticket: the owner (AirTrail user), a companion (guestName), or someone else.

The travellers come from `travellers.yaml` in the config directory, which is optional: without
it, or without an `owner`, nobody is the owner, so bookings are filed into trips and no flight is
written to AirTrail."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from tripbox.config import TRAVELLERS_FILE, ConfigError, ConfigProblem
from tripbox.util import read_small_file

TITLES = {"MR", "MRS", "MS", "MISS", "DR", "MSTR", "MASTER", "PROF", "JR", "SR", "II", "III", "IV"}


def name_tokens(raw: str) -> list[str]:
    """'SAMPLE/ALEX QUINN MR' -> ['ALEX', 'QUINN', 'SAMPLE']; titles/suffixes dropped."""
    s = raw.strip().upper()
    if "/" in s:
        last, first = s.split("/", 1)
        s = f"{first} {last}"
    elif "," in s:
        last, first = s.split(",", 1)
        s = f"{first} {last}"
    s = re.sub(r"[^A-Z\s'\-]", " ", s)
    return [t for t in s.split() if t not in TITLES]


def first_last(raw: str) -> tuple[str, str] | None:
    toks = name_tokens(raw)
    if len(toks) < 2:
        return None
    return toks[0], toks[-1]


def _split(raw: str) -> tuple[str, str, str] | None:
    """-> (first, middle-joined, last) or None for a single token."""
    toks = name_tokens(raw)
    if len(toks) < 2:
        return None
    return toks[0], "".join(toks[1:-1]), toks[-1]


def names_match(printed: str, alias: str) -> bool:
    """Same last name and the same first name, allowing the airline habit of gluing first name
    and middle name (or its initial) together: SAMPLE/ALEXQ and SAMPLE/ALEXQUINN both match the
    alias 'Alex Quinn Sample'. Middle names are otherwise optional."""
    p, a = _split(printed), _split(alias)
    if p is None or a is None:
        return False
    pf, pm, pl = p
    af, am, al = a
    if pl != al:
        return False
    if pf == af:
        return True
    if pf.startswith(af):
        rest = pf[len(af) :]
        return bool(am) and am.startswith(rest)
    return False


@dataclass(frozen=True)
class PassengerMatch:
    raw: str
    kind: Literal["me", "guest", "other"]
    canonical: str


@dataclass
class Classified:
    me_present: bool
    guests: list[str] = field(default_factory=list)  # canonical guest names (AirTrail spelling)
    others: list[str] = field(default_factory=list)  # names as printed
    matches: list[PassengerMatch] = field(default_factory=list)


@dataclass(frozen=True)
class PassengerBook:
    """`me_aliases`: the owner's names (none: nobody is the owner); `guests`: each companion's
    name as tripbox and AirTrail spell it, with the names printed on tickets."""

    me_aliases: tuple[str, ...]
    guests: tuple[tuple[str, tuple[str, ...]], ...]

    @classmethod
    def empty(cls) -> PassengerBook:
        return cls(me_aliases=(), guests=())

    @classmethod
    def from_yaml(cls, path: Path) -> PassengerBook:
        """The book in a travellers file that must exist (tests and tools); a problem in it
        raises ConfigError with every problem, one per line."""
        if not path.is_file():
            raise FileNotFoundError(path)
        book, problems = load_travellers(path)
        if problems:
            raise ConfigError("\n".join(str(p) for p in problems))
        return book

    @property
    def me_name(self) -> str:
        """The first of the owner's names, the spelling tripbox records; "" when nobody is the
        owner."""
        return next(iter(self.me_aliases), "")

    def match(self, raw: str) -> PassengerMatch:
        if any(names_match(raw, a) for a in self.me_aliases):
            return PassengerMatch(raw=raw, kind="me", canonical=self.me_name)
        for canonical, aliases in self.guests:
            if any(names_match(raw, a) for a in aliases):
                return PassengerMatch(raw=raw, kind="guest", canonical=canonical)
        return PassengerMatch(raw=raw, kind="other", canonical=raw.strip())

    def classify(self, names: list[str]) -> Classified:
        """A single token ("Alex" from a greeting) is recorded but is nobody: not a traveller
        manifest entry, so it neither matches the owner nor becomes an 'other'."""
        out = Classified(me_present=False)
        seen: set[str] = set()
        for n in names:
            if not n or not n.strip():
                continue
            m = self.match(n)
            out.matches.append(m)
            if len(name_tokens(n)) < 2:
                continue
            if m.kind == "me":
                out.me_present = True
            elif m.kind == "guest":
                if m.canonical not in out.guests:
                    out.guests.append(m.canonical)
            else:
                key = " ".join(name_tokens(n)) or n.strip().upper()
                if key not in seen:
                    seen.add(key)
                    out.others.append(m.canonical)
        return out


def person(name: str, book: PassengerBook) -> tuple[str, str] | None:
    """Who a printed name is, for telling whether two bookings name the same person: the owner
    or a companion by the name the book gives them, anyone else by the words of their name. A
    name of one word is nobody (None)."""
    words = name_tokens(name)
    if len(words) < 2:
        return None
    match = book.match(name)
    if match.kind == "other":
        return "other", " ".join(words)
    return "known", match.canonical


def people(names: Iterable[str], book: PassengerBook) -> set[tuple[str, str]]:
    """The people `names` print (`person`), each once."""
    return {who for n in names if n and (who := person(n, book)) is not None}


# ----------------------------------------------------------------------------- travellers.yaml
_TOP_KEYS = ("owner", "companions")
_OWNER_KEYS = ("names",)
_COMPANION_KEYS = ("name", "names")
_NAMES_FIX = 'list them as names: ["First Last", …]'
# What a problem may show of a key it does not know: short and plain, or nothing.
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_-]{1,24}")
# A travellers file is a few lines; anything over 64 KiB is not one, and is never read whole
# (util.read_small_file).
_UNREADABLE = (
    "cannot be read",
    "it must be a regular file of at most 64 KiB that tripbox may read",
)


class _KeyTwice(Exception):
    def __init__(self, key: object):
        super().__init__()
        self.key = key


class _UniqueKeyLoader(yaml.SafeLoader):
    """yaml.SafeLoader, except that a key given twice in one mapping is refused rather than the
    last one silently winning."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen: set = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue  # merged keys may be overridden; that is what a merge is for
                key = self.construct_object(key_node, deep=deep)
                try:
                    if key in seen:
                        raise _KeyTwice(key)
                    seen.add(key)
                except TypeError:
                    pass  # an unhashable key: the base constructor refuses it
        return super().construct_mapping(node, deep=deep)


def _problem(what: str, fix: str) -> ConfigProblem:
    return ConfigProblem(TRAVELLERS_FILE, what, fix, "core")


def _key(key: object) -> str:
    """" 'me'" for a short, plain key, else "": a problem shows nothing else of a key."""
    return f" {key!r}" if isinstance(key, str) and _PLAIN_KEY.fullmatch(key) else ""


def _yaml_line(exc: yaml.YAMLError) -> int | None:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    return mark.line + 1 if mark is not None else None


def _names(value: object) -> list[str] | None:
    """A non-empty list of non-blank strings, or None."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(n, str) and n.strip() for n in value):
        return None
    return [n.strip() for n in value]


def load_travellers(path: Path) -> tuple[PassengerBook, tuple[ConfigProblem, ...]]:
    """The travellers in `path` and what is wrong with the file. An absent file is no problem:
    nobody is the owner. With any problem the book is empty and every problem is returned, each
    keyed by the file's name; a problem quotes a traveller's name or a short key, never more of
    the file. It never raises: Settings.from_env, and so `doctor`, reads the file."""
    try:
        raw = read_small_file(path)
    except OSError:
        return PassengerBook.empty(), (_problem(*_UNREADABLE),)
    if raw is None:
        return PassengerBook.empty(), ()
    try:
        data = yaml.load(raw, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except _KeyTwice as twice:
        return PassengerBook.empty(), (_problem(f"key{_key(twice.key)} appears twice", "keep one"),)
    except yaml.YAMLError as exc:
        line = _yaml_line(exc)
        what = f"not valid YAML (line {line})" if line else "not valid YAML"
        return PassengerBook.empty(), (_problem(what, "fix the file or remove it"),)
    except Exception:  # noqa: BLE001 - nested too deep (RecursionError), or anything else
        return PassengerBook.empty(), (_problem("not valid YAML", "fix the file or remove it"),)
    problems: list[ConfigProblem] = []
    book = _read_travellers(data, problems.append)
    return (PassengerBook.empty(), tuple(problems)) if problems else (book, ())


def _read_travellers(data: object, report: Callable[[ConfigProblem], None]) -> PassengerBook:
    """The book `data` describes; each problem goes to `report` (the book is then unused)."""
    if data is None:  # empty, or comments only
        return PassengerBook.empty()
    if not isinstance(data, dict):
        report(_problem("not a mapping", "the file holds owner: and companions:"))
        return PassengerBook.empty()
    for key in data:
        if key not in _TOP_KEYS:
            report(_problem(f"unknown key{_key(key)}", "the file holds owner: and companions:"))

    def words(names: list[str]) -> None:
        for name in names:
            if len(name_tokens(name)) < 2:
                report(
                    _problem(
                        f"{name!r} has one word",
                        "write each name as printed on a ticket, first and last",
                    )
                )

    people: list[list[str]] = []  # every name of each person, for the listed-twice check
    owner: list[str] = []
    if "owner" in data:
        o = data["owner"]
        for key in o if isinstance(o, dict) else ():
            if key not in _OWNER_KEYS:
                report(_problem(f"unknown key{_key(key)} under owner", "owner: holds names:"))
        names = _names(o.get("names")) if isinstance(o, dict) else None
        if names is None:
            report(_problem("owner names: is not a list of names", _NAMES_FIX))
        else:
            words(list(dict.fromkeys(names)))
            owner = names
            people.append(names)
    guests: list[tuple[str, tuple[str, ...]]] = []
    companions = data.get("companions")
    if companions is not None and not isinstance(companions, list):
        report(_problem("companions: is not a list", "write each companion as - name: …"))
        companions = None
    for n, c in enumerate(companions or (), start=1):
        name = c.get("name") if isinstance(c, dict) else None
        if not isinstance(name, str) or not name.strip():
            report(_problem(f"companion {n} has no name", "give each companion a name:"))
            continue
        name = name.strip()
        for key in c:
            if key not in _COMPANION_KEYS:
                report(
                    _problem(
                        f"companion {n} has an unknown key{_key(key)}",
                        "a companion holds name: and names:",
                    )
                )
        names = _names(c["names"]) if c.get("names") is not None else [name]
        if names is None:
            report(_problem(f"companion {n} names: is not a list of names", _NAMES_FIX))
            continue
        words(list(dict.fromkeys([name, *names])))
        guests.append((name, tuple(names)))
        people.append([name, *names])
    for i, first in enumerate(people):
        for second in people[i + 1 :]:
            clash = _clash(first, second)
            if clash is not None:
                report(clash)
    return PassengerBook(me_aliases=tuple(owner), guests=tuple(guests))


def _clash(first: list[str], second: list[str]) -> ConfigProblem | None:
    """The one problem with two people's names, if the matcher could read one as the other: the
    same name twice, or names that share a first and a last name (a middle name, or an initial
    glued to the first name, does not tell two people apart). One person's own names may match
    each other freely."""
    pairs = [(a, b) for a in first for b in second]
    for a, b in pairs:
        if len(name_tokens(a)) >= 2 and name_tokens(a) == name_tokens(b):
            return _problem(f"{b!r} is listed twice", "each name belongs to one person")
    for a, b in pairs:
        if names_match(a, b) or names_match(b, a):
            return _problem(
                f"{a!r} and {b!r} read as one person (same first and last name)",
                "tripbox cannot tell two travellers apart by a middle name; list only one of them",
            )
    return None
