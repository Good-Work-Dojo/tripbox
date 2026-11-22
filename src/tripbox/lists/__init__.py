# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Lists that ship as data files: the sender domains the mail gate trusts, the airlines (name,
short name and the words an email uses for them) and the US state names the address parser must
not take for a city.

The files sit next to this module. A user adds to or replaces two of them from the config
directory: `airlines.yaml` (entries; an entry with a shipped `iata` replaces that airline) and
`us_states.txt` (names added to the shipped ones). A broken override raises `ListFileError`, which
`Settings.from_env` turns into a core problem; an absent file is simply absent. The override files
are read as strictly as the travellers file: a regular file of at most 64 KiB, and no message
quotes anything from the file beyond a well-formed IATA code.

Loaders are cached per config directory, so two directories never share a read; `clear_caches`
drops them after a file changes."""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from tripbox.util import read_small_file

AIRLINES_FILE = "airlines.yaml"
STATES_FILE = "us_states.txt"
_IATA = re.compile(r"[A-Za-z0-9]{2}")
_ENTRY_KEYS = frozenset({"iata", "name", "short", "match"})
_IATA_NOT_TEXT = "iata must be text in quotes (YAML reads NO and ON as true or false)"


class ListFileError(ValueError):
    """A list file that cannot be used: `file` is its name, `what` a fixed description of the
    mistake. Neither quotes the file's content (an entry's well-formed IATA code aside)."""

    def __init__(self, file: str, what: str) -> None:
        super().__init__(f"{file}: {what}")
        self.file = file
        self.what = what


@dataclass(frozen=True)
class Airline:
    iata: str
    name: str
    short: str | None
    match: tuple[str, ...]


def config_dir() -> Path:
    return Path(os.environ.get("TRIPBOX_CONFIG_DIR") or "/app/config")


def _shipped(name: str) -> str:
    return resources.files("tripbox.lists").joinpath(name).read_text(encoding="utf-8")


def _override(directory: Path, name: str) -> bytes | None:
    """The override file's bytes; None when there is none. A link to nothing, a directory, a pipe,
    a file over the limit or one that cannot be read is a ListFileError."""
    try:
        return read_small_file(directory / name)
    except OSError:
        raise ListFileError(
            name, "cannot be read (not a regular file of at most 64 KiB, or not readable)"
        ) from None


# ----------------------------------------------------------------------------- sender domains
@functools.cache
def trusted_sender_domains() -> tuple[str, ...]:
    """The shipped sender domains, lower-cased, in file order."""
    data = yaml.safe_load(_shipped("senders.yaml"))
    return tuple(str(d).strip().lower() for d in data["trusted_domains"])


# ----------------------------------------------------------------------------- airlines
def _parse_airlines(text: str | bytes, file: str) -> list[dict]:
    """The entries of an airlines file as dicts with `iata`, `name` and optional `short` and
    `match`, checked."""
    try:
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - YAMLError, or nested too deep (RecursionError)
        raise ListFileError(file, "not valid YAML") from None
    if data is None:
        return []
    if not isinstance(data, list):
        raise ListFileError(file, "is not a list of entries (each starts with - iata:)")
    out = []
    for n, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            raise ListFileError(file, f"entry {n} is not a mapping")
        iata = entry.get("iata")
        if iata is not None and not isinstance(iata, str):
            # unquoted, NO (Neos) or ON (Nauru Airlines) is a boolean to YAML, 10 a number
            raise ListFileError(file, f"entry {n}: {_IATA_NOT_TEXT}")
        if not isinstance(iata, str) or not _IATA.fullmatch(iata):
            raise ListFileError(file, f"entry {n} has no iata of two letters or digits")
        iata = iata.upper()
        if any(k not in _ENTRY_KEYS for k in entry):
            raise ListFileError(file, f"entry {iata} has a key other than iata, name, short, match")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ListFileError(file, f"entry {iata} has no name")
        short = entry.get("short")
        if short is not None and not isinstance(short, str):
            raise ListFileError(file, f"entry {iata} short is not text")
        words = entry.get("match")
        if words is not None and (
            not isinstance(words, list)
            or not all(isinstance(w, str) and w.strip() for w in words)
        ):
            raise ListFileError(file, f"entry {iata} match is not a list of words")
        out.append(
            {
                "iata": iata,
                "name": name.strip(),
                "short": short.strip() or None if short is not None else None,
                "match": None if words is None else tuple(w.strip().lower() for w in words),
            }
        )
    return out


@functools.cache
def _airlines(directory: Path) -> dict[str, Airline]:
    table: dict[str, Airline] = {}
    for e in _parse_airlines(_shipped(AIRLINES_FILE), AIRLINES_FILE):
        table[e["iata"]] = Airline(e["iata"], e["name"], e["short"], e["match"] or ())
    raw = _override(directory, AIRLINES_FILE)
    for e in _parse_airlines(raw, AIRLINES_FILE) if raw is not None else ():
        old = table.get(e["iata"])
        # a replacement that leaves out `short` or `match` keeps the shipped airline's
        short = e["short"] if e["short"] is not None else (old.short if old else None)
        match = e["match"] if e["match"] is not None else (old.match if old else ())
        table[e["iata"]] = Airline(e["iata"], e["name"], short, match)
    return table


def airlines(directory: Path | None = None) -> dict[str, Airline]:
    """Airlines by IATA code: the shipped ones, then the config directory's `airlines.yaml`."""
    return _airlines(directory or config_dir())


def airline_words(directory: Path | None = None) -> list[tuple[str, str]]:
    """(word, IATA code) for every `match` word, the longest word first (ties keep file order),
    so "air france" is tried before "air"."""
    words = [(w, a.iata) for a in airlines(directory).values() for w in a.match]
    return sorted(words, key=lambda wc: -len(wc[0]))


# ----------------------------------------------------------------------------- US states
def _names(text: str) -> list[str]:
    lines = (line.strip() for line in text.splitlines())
    return [line.lower() for line in lines if line and not line.startswith("#")]


@functools.cache
def _us_states(directory: Path) -> frozenset[str]:
    names = _names(_shipped(STATES_FILE))
    raw = _override(directory, STATES_FILE)
    if raw is not None:
        try:
            names += _names(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise ListFileError(STATES_FILE, "is not UTF-8 text") from None
    return frozenset(names)


def us_states(directory: Path | None = None) -> frozenset[str]:
    """Lower-cased state names: the shipped ones plus the config directory's `us_states.txt`."""
    return _us_states(directory or config_dir())


# ----------------------------------------------------------------------------- bookkeeping
def overrides_in_use(directory: Path | None = None) -> tuple[str, ...]:
    """The override files present in the config directory; it only looks, never reads them."""
    base = directory or config_dir()
    return tuple(n for n in (AIRLINES_FILE, STATES_FILE) if os.path.lexists(base / n))


def clear_caches() -> None:
    _airlines.cache_clear()
    _us_states.cache_clear()
    trusted_sender_domains.cache_clear()
