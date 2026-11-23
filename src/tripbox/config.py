# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Settings from the environment (.env via compose) — one frozen object passed around explicitly.

Every setting the code reads is a `Key` in `KEYS`: its name, the feature it belongs to, its
default, whether it is a secret and whether the web container may receive it.

Loading never fails for a value: every problem is collected as a `ConfigProblem`, and each
feature is on, off or invalid by its own settings (`Settings.features`). A bad value never widens
what is trusted (see _ON_BAD), and a problem shows a value only when it is short and plain (see
_shown). What a problem stops is the command's decision: a core problem stops every command, a
web problem stops `web`, and any other feature with a problem is left off."""

from __future__ import annotations

import dataclasses
import difflib
import ipaddress
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from tripbox import lists, passwords
from tripbox.validate import refdata

if TYPE_CHECKING:
    from tripbox.validate.passengers import PassengerBook


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigProblem:
    """One thing wrong with the settings. `key` is a setting's name, or TRAVELLERS_FILE for the
    travellers file; `feature` is the feature it stops, or "names" for an environment name that
    is no setting. The text never holds a secret's value."""

    key: str
    what: str
    fix: str
    feature: str

    def __str__(self) -> str:
        return f"{self.key}: {self.what} — {self.fix}"


@dataclass(frozen=True)
class FeatureState:
    """`missing`: the keys the feature needs that are unset."""

    name: str
    state: Literal["on", "off", "invalid"]
    missing: tuple[str, ...]
    problems: tuple[ConfigProblem, ...]


@dataclass(frozen=True)
class Key:
    """One setting: `field` is the `Settings` field it fills (None for a switch read elsewhere);
    `note` and `example` are its lines in the example settings file (`env_example`): one
    sentence of its row in docs/configuration.md, and a value to show in place of the default
    (never a real one: a secret's is empty but for an address the docs already show)."""

    name: str
    feature: str  # core | web | mail | model | airtrail | notifications | enrichment | dev
    default: str = ""
    secret: bool = False
    web: bool = False  # the web container may receive it
    field: str | None = None
    note: str = ""
    example: str = ""


KEYS: tuple[Key, ...] = (
    Key(
        "TZ",
        "core",
        "UTC",
        web=True,
        field="tz",
        note="The time zone tripbox works in, an IANA name.",
        example="America/Chicago",
    ),
    Key(
        "TRIPBOX_HOME_AIRPORTS",
        "core",
        web=True,
        field="home_airports",
        note="Comma-separated codes of airports you live near: a trip is titled by another one.",
        example="MSP",
    ),
    # the page offers "Find photo" only when this is on; the worker is what looks photos up
    Key(
        "TRIPBOX_COVERS",
        "core",
        "off",
        web=True,
        field="covers",
        note="Finds a freely licensed photo of each trip's place on Wikimedia: off or wikimedia.",
        example="wikimedia",
    ),
    Key("TRIPBOX_DATA_DIR", "core", "/app/data", web=True, field="data_dir"),
    Key("TRIPBOX_ARCHIVE_DIR", "core", "/app/archive", field="archive_dir"),
    Key("TRIPBOX_LOGS_DIR", "core", "/app/logs", web=True, field="logs_dir"),
    Key("TRIPBOX_CONFIG_DIR", "core", "/app/config", web=True, field="config_dir"),
    Key("TRIPBOX_BACKUPS_DIR", "core", "/app/backups", field="backups_dir"),
    # set by the image (its runtime stage), never by hand: read by in_image() below
    Key("TRIPBOX_IN_IMAGE", "core", web=True),
    Key(
        "TRIPBOX_MAIL_HOST",
        "mail",
        field="mail_host",
        note="The IMAP server's name alone.",
        example="imap.gmail.com",
    ),
    Key(
        "TRIPBOX_MAIL_USER",
        "mail",
        field="mail_user",
        note="The mailbox's login; an email address is shown on the page as the one to forward to.",
        example="trips@example.com",
    ),
    Key(
        "TRIPBOX_MAIL_PASSWORD",
        "mail",
        secret=True,
        field="mail_password",
        note="The mailbox's password or app password (secret); spaces are removed.",
    ),
    Key(
        "TRIPBOX_MAIL_ALLOWED_SENDERS",
        "mail",
        field="allowed_senders",
        note="Comma-separated addresses whose mail is read; any other mail is rejected unparsed.",
        example="alex@example.com",
    ),
    Key(
        "TRIPBOX_MAIL_TRUST",
        "mail",
        "gmail",
        field="mail_trust",
        note="gmail: a forward is believed only if Google vouches for it; mailbox: other servers.",
    ),
    # unset or empty: the shipped list (lists/senders.yaml); a value that fails its
    # check: no domain at all (_ON_BAD)
    Key(
        "TRIPBOX_MAIL_TRUSTED_SENDER_DOMAINS",
        "mail",
        field="mail_trusted_sender_domains",
        note="Domains whose forwarded flights reach AirTrail unseen; unset, the shipped list.",
    ),
    # unset or empty: labels with TRIPBOX_MAIL_TRUST=gmail, none with mailbox
    Key(
        "TRIPBOX_MAIL_FILING",
        "mail",
        field="mail_filing",
        note="labels: handled mail is copied into tripbox/ folders; none: the mailbox is left.",
    ),
    Key(
        "TRIPBOX_LLM_PROVIDER",
        "model",
        "anthropic",
        field="llm_provider",
        note="Who reads emails with no structured data: anthropic, openai (any such server), none.",
    ),
    Key(
        "TRIPBOX_LLM_API_KEY",
        "model",
        secret=True,
        field="llm_api_key",
        note="The model's key (secret); a server of your own may need none.",
    ),
    Key(
        "TRIPBOX_LLM_MODEL",
        "model",
        "claude-haiku-5-5",
        field="llm_model",
        note="The model's name; with openai, one your server serves.",
    ),
    # an OpenAI-compatible server's address: a secret, as it may carry a login
    Key(
        "TRIPBOX_LLM_BASE_URL",
        "model",
        secret=True,
        field="llm_base_url",
        note="With openai, your server's address as the worker reaches it, not localhost (secret).",
        example="http://models.example:11434/v1",
    ),
    # the cap the model is asked under (parse.budget): a priced model's monthly budget, the calls
    # a day of one with no known price, and the price itself when the shipped table lacks it
    Key(
        "TRIPBOX_LLM_MONTHLY_BUDGET_USD",
        "model",
        "5.00",
        field="llm_monthly_budget_usd",
        note="The most a priced model may cost in a month (UTC), in US dollars; 0: never asked.",
    ),
    Key(
        "TRIPBOX_LLM_MAX_CALLS_PER_DAY",
        "model",
        "100",
        field="llm_max_calls_per_day",
        note="How many calls a day (UTC) a model with no known price may be asked.",
    ),
    Key(
        "TRIPBOX_LLM_PRICE_USD_PER_MTOK",
        "model",
        field="llm_price",
        note="INPUT,OUTPUT in US dollars per million tokens, for a model tripbox has no price for.",
    ),
    Key(
        "TRIPBOX_AIRTRAIL_URL",
        "airtrail",
        field="airtrail_url",
        note="The address AirTrail answers at, as the worker reaches it.",
        example="http://flightlog.example:3000",
    ),
    Key(
        "TRIPBOX_AIRTRAIL_API_KEY",
        "airtrail",
        secret=True,
        field="airtrail_api_key",
        note="An API key made in AirTrail under its security settings (secret).",
    ),
    # not a switch: alone it neither turns AirTrail on nor leaves it half configured
    Key(
        "TRIPBOX_AIRTRAIL_DRY_RUN",
        "airtrail",
        "true",
        field="airtrail_dry_run",
        note="true: nothing is written to AirTrail and flights wait; false sends them.",
    ),
    # the address people open the flight log at, which the page links while one is kept; not a
    # switch either
    Key(
        "TRIPBOX_AIRTRAIL_PUBLIC_URL",
        "airtrail",
        web=True,
        field="airtrail_public_url",
        note="The address you open AirTrail at in a browser: the trips list links it.",
        example="https://flightlog.example",
    ),
    # the feature's one switch: the flight-data service to ask, of which there is one
    Key(
        "TRIPBOX_ENRICH",
        "enrichment",
        "",
        field="enrich",
        note="Fills in landed flights in AirTrail: aeroapi, or empty for none.",
        example="aeroapi",
    ),
    Key(
        "TRIPBOX_AEROAPI_KEY",
        "enrichment",
        secret=True,
        field="aeroapi_key",
        note="The flight-data service key (secret): it fills in AirTrail's flights once landed.",
    ),
    Key(
        "TRIPBOX_AEROAPI_MONTHLY_BUDGET_USD",
        "enrichment",
        "1.00",
        field="aeroapi_monthly_budget_usd",
        note="The most to spend on lookups in a calendar month, in US dollars.",
    ),
    Key(
        "TRIPBOX_AEROAPI_MAX_CALLS_PER_RUN",
        "enrichment",
        "10",
        field="aeroapi_max_calls_per_run",
        note="The most lookups in one run.",
    ),
    Key(
        "TRIPBOX_TELEGRAM_BOT_TOKEN",
        "notifications",
        secret=True,
        field="telegram_bot_token",
        note="Your Telegram bot's token (secret).",
    ),
    Key(
        "TRIPBOX_TELEGRAM_CHAT_ID",
        "notifications",
        field="telegram_chat_id",
        note="The Telegram chat that gets the messages and may command the bot.",
    ),
    # the topic's address is a secret: whoever knows it can read the topic
    Key(
        "TRIPBOX_NTFY_URL",
        "notifications",
        secret=True,
        field="ntfy_url",
        note="With or instead of Telegram: your ntfy topic's whole address (secret).",
        example="https://ntfy.example/trips",
    ),
    Key(
        "TRIPBOX_NTFY_TOKEN",
        "notifications",
        secret=True,
        field="ntfy_token",
        note="An access token for the topic, for a server that asks for one (secret).",
    ),
    Key(
        "TRIPBOX_AUTH",
        "web",
        "password",
        web=True,
        field="tripbox_auth",
        note="Who may open the page: password, proxy (a login proxy), cloudflare (Access) or none.",
    ),
    # the page's own login: one shared password, or the hash line `tripbox passwd` prints
    Key(
        "TRIPBOX_PASSWORD",
        "web",
        secret=True,
        web=True,
        field="password",
        note="The page's password, 10 characters or more (secret).",
    ),
    Key(
        "TRIPBOX_PASSWORD_HASH",
        "web",
        secret=True,
        web=True,
        field="password_hash",
        note="Instead of the password, never with it: the whole line tripbox passwd prints.",
    ),
    Key(
        "TRIPBOX_ACCESS_TEAM",
        "web",
        web=True,
        field="tripbox_access_team",
        note="With cloudflare: the Zero Trust team name, the part before .cloudflareaccess.com.",
        example="example-team",
    ),
    Key(
        "TRIPBOX_ACCESS_AUD",
        "web",
        web=True,
        field="tripbox_access_aud",
        note="With cloudflare: the audience tag of the Access application that protects the page.",
    ),
    Key(
        "TRIPBOX_ALLOWED_EMAILS",
        "web",
        web=True,
        field="tripbox_allowed_emails",
        note="With cloudflare: comma-separated addresses that may open the page.",
        example="alex@example.com",
    ),
    # a login proxy in front of the page: where it connects from, the header it names the user
    # in, and the users it may name
    Key(
        "TRIPBOX_TRUSTED_PROXIES",
        "web",
        web=True,
        field="trusted_proxies",
        note="With proxy: the addresses or networks the login proxy connects from.",
        example="192.0.2.10",
    ),
    Key(
        "TRIPBOX_PROXY_USER_HEADER",
        "web",
        "Remote-User",
        web=True,
        field="proxy_user_header",
        note="With proxy: the header the proxy names the signed-in user in.",
    ),
    Key(
        "TRIPBOX_PROXY_USERS",
        "web",
        web=True,
        field="proxy_users",
        note="With proxy: the user names the proxy may sign in, comma-separated.",
        example="alex",
    ),
    # the address people open the page at: its origin, and the path it is served under
    Key(
        "TRIPBOX_BASE_URL",
        "web",
        "http://localhost:8110",
        web=True,
        field="base_url",
        note="The address people open the page at, a path allowed: every link is built from it.",
        example="https://trips.example.com",
    ),
    # where the page listens (with Docker Compose, where its port is published): no login is
    # refused off loopback (_states)
    Key(
        "TRIPBOX_LISTEN",
        "web",
        "127.0.0.1:8110",
        web=True,
        field="listen",
        note="Where the page listens: 127.0.0.1:8110 for this computer only, 0.0.0.0:8110 for"
        " every network.",
    ),
    # where the page's own source code is: the about line links it, as the licence asks that the
    # people who use the page over a network can find it
    Key(
        "TRIPBOX_SOURCE_URL",
        "web",
        "https://github.com/Good-Work-Dojo/tripbox",
        web=True,
        field="source_url",
        note="The address of the page's source code, which its about line links.",
    ),
    # saved copies of the list and the trips opened, for use with no network: unset, on with the
    # page's own password and with no login, off behind a login it cannot see the sign-out of
    Key(
        "TRIPBOX_OFFLINE",
        "web",
        web=True,
        field="offline",
        note="Saved copies of the list and each trip opened, for use offline: true or false.",
    ),
    # read by offline() below and by pipeline.run's REPLAY_ENV and RECORD_ENV
    Key("TRIPBOX_NO_NETWORK", "dev", web=True),
    Key("TRIPBOX_REPLAY_DIR", "dev"),
    Key("TRIPBOX_RECORD_DIR", "dev"),
)
KEY_BY_NAME: dict[str, Key] = {k.name: k for k in KEYS}
# Keys that only change what is shown: a bad value is a problem of its own (listed, logged, the
# field takes its default), never counted when its feature's state is worked out
DISPLAY_ONLY: tuple[str, ...] = ("TRIPBOX_AIRTRAIL_PUBLIC_URL", "TRIPBOX_SOURCE_URL")
# The folders: the image sets them, so the example settings file leaves them out; they are
# documented for running without Docker
PATH_KEYS: tuple[str, ...] = (
    "TRIPBOX_DATA_DIR",
    "TRIPBOX_ARCHIVE_DIR",
    "TRIPBOX_LOGS_DIR",
    "TRIPBOX_CONFIG_DIR",
    "TRIPBOX_BACKUPS_DIR",
)
# Names the compose file reads from .env and no process needs: each name, its default there and
# its note in the example settings file. The worker receives the whole file, so none of them is
# an unknown name (_unknown_names).
COMPOSE_ONLY: tuple[tuple[str, str, str], ...] = (
    ("TRIPBOX_IMAGE", "ghcr.io/good-work-dojo/tripbox:1", "The image the services run."),
    ("TRIPBOX_UID", "1000", "The user id the services run as: the owner of the install's folders."),
    ("TRIPBOX_GID", "1000", "The group id the services run as."),
)
# 1 in the shipped image, which sets it: the page's socket is then the container's, which compose
# publishes at TRIPBOX_LISTEN, so `tripbox web --host` may name every interface with no login
IMAGE_ENV = "TRIPBOX_IN_IMAGE"
# The port the page listens on inside the image: its services pass it with every interface
# (`tripbox web --host 0.0.0.0 --port 8110`), and the health check asks it
CONTAINER_PORT = 8110
_KEY_BY_FIELD: dict[str, Key] = {k.field: k for k in KEYS if k.field is not None}
_ORDER: dict[str, int] = {k.name: i for i, k in enumerate(KEYS)}
# Settings fields that no key fills
DERIVED_FIELDS: tuple[str, ...] = ("problems", "features", "travellers", "link_base")
# The one travellers file read from the config directory (see validate.passengers): optional, and
# no other name (the shipped example included) is ever read in its place. Its problems are core
# problems.
TRAVELLERS_FILE = "travellers.yaml"


def _rank(problem: ConfigProblem) -> int:
    """Registry order; the travellers file comes after every key."""
    return _ORDER.get(problem.key, len(_ORDER))

# What Settings.features reports, in this order. core and web are on or invalid; the others are
# off until one of their SWITCHES is set, and then on only when every one of their NEEDS is
# (notifications: per channel, CHANNELS).
FEATURES = ("core", "web", "mail", "model", "airtrail", "notifications", "enrichment")
SWITCHES: dict[str, tuple[str, ...]] = {
    "mail": ("TRIPBOX_MAIL_HOST", "TRIPBOX_MAIL_USER", "TRIPBOX_MAIL_PASSWORD"),
    "model": ("TRIPBOX_LLM_API_KEY",),
    "airtrail": ("TRIPBOX_AIRTRAIL_URL", "TRIPBOX_AIRTRAIL_API_KEY"),
    # and AirTrail on: it fills in AirTrail's flights; a key alone leaves it off
    "enrichment": ("TRIPBOX_ENRICH",),
}
NEEDS: dict[str, tuple[str, ...]] = {
    "mail": (*SWITCHES["mail"], "TRIPBOX_MAIL_ALLOWED_SENDERS"),
    "model": SWITCHES["model"],
    "airtrail": SWITCHES["airtrail"],
    "enrichment": (*SWITCHES["enrichment"], "TRIPBOX_AEROAPI_KEY"),
}
TELEGRAM_KEYS = ("TRIPBOX_TELEGRAM_BOT_TOKEN", "TRIPBOX_TELEGRAM_CHAT_ID")
# Notifications go to Telegram, to ntfy or to both: each channel has its own switches and needs,
# and notifications are on when one channel is set up and none is half configured.
CHANNELS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Telegram": (TELEGRAM_KEYS, TELEGRAM_KEYS),
    "ntfy": (("TRIPBOX_NTFY_URL", "TRIPBOX_NTFY_TOKEN"), ("TRIPBOX_NTFY_URL",)),
}
LABELS: dict[str, str] = {
    "mail": "mail",
    "model": "the model",
    "airtrail": "AirTrail",
    "notifications": "notifications",
    "enrichment": "enrichment",
}
# What `bot` says when it has no Telegram chat to answer in.
TELEGRAM_OFF = f"Telegram is off: {', '.join(TELEGRAM_KEYS)} not set"
# The page container's only secrets: its own login pair, which opens the page and nothing else
PAGE_SECRETS = ("TRIPBOX_PASSWORD", "TRIPBOX_PASSWORD_HASH")
# What TRIPBOX_AUTH=password says when neither of the pair is set
PASSWORD_FIX = (
    f"choose a password of {passwords.MIN_LENGTH} characters or more, or set"
    ' TRIPBOX_PASSWORD_HASH to the line "tripbox passwd" prints'
)
# what TRIPBOX_AUTH=cloudflare needs
CLOUDFLARE_NEEDS = ("TRIPBOX_ACCESS_TEAM", "TRIPBOX_ACCESS_AUD", "TRIPBOX_ALLOWED_EMAILS")
# what TRIPBOX_AUTH=proxy needs
PROXY_NEEDS = ("TRIPBOX_TRUSTED_PROXIES", "TRIPBOX_PROXY_USERS")
PROXIES_FIX = "list your proxy's address or network, such as 192.0.2.0/24"
LOCAL_ORIGINS = ("http://localhost", "http://127.0.0.1", "http://[::1]")
# The first segment of every path the page answers, now or reserved for it. A proxy that strips
# the base path hands the page its paths as they are, so a base path that begins with one of
# these would take that path's requests for its own: such a base is refused.
RESERVED_SEGMENTS = (
    "healthz",
    "static",
    "manifest.webmanifest",
    "sw.js",
    "login",
    "logout",
    "feed",
    "s",
    "settings",
    "deleted",
    "add",
    "uploads",
    "trips",
    "items",
    "links",
    "covers",
)


def is_local_origin(origin: str) -> bool:
    """localhost, 127.0.0.1 or [::1] over http on any port: a page nobody else can reach."""
    return any(origin == o or origin.startswith(o + ":") for o in LOCAL_ORIGINS)


def off_line(state: FeatureState) -> str:
    """The one line a command says when the feature it needs is off."""
    return f"{LABELS[state.name]} is off: {', '.join(state.missing)} not set"


# What `tripbox doctor` says next to a feature that is off.
OFF_HINTS: dict[str, str] = {
    "mail": "set TRIPBOX_MAIL_HOST, TRIPBOX_MAIL_USER, TRIPBOX_MAIL_PASSWORD and"
    " TRIPBOX_MAIL_ALLOWED_SENDERS to read a mailbox",
    "model": "only emails carrying structured data are read; set TRIPBOX_LLM_API_KEY: mail that"
    " needs a model waits and is read once one is set up",
    "airtrail": "flights go to trips only; set TRIPBOX_AIRTRAIL_URL and TRIPBOX_AIRTRAIL_API_KEY"
    " to keep a flight log",
    "notifications": "set TRIPBOX_TELEGRAM_BOT_TOKEN and TRIPBOX_TELEGRAM_CHAT_ID, or"
    " TRIPBOX_NTFY_URL, to get messages",
    "enrichment": "set TRIPBOX_ENRICH=aeroapi and TRIPBOX_AEROAPI_KEY to fill in flight details"
    " in AirTrail",
}
# What `tripbox doctor` says next to enrichment when the service's key is set without the switch.
KEY_WITHOUT_SWITCH = (
    "TRIPBOX_AEROAPI_KEY is set and TRIPBOX_ENRICH is not: enrichment stays off"
)
# What `tripbox doctor` says next to the model when TRIPBOX_LLM_PROVIDER turns it off.
MODEL_NONE_HINT = "TRIPBOX_LLM_PROVIDER=none: only emails carrying structured data are read"
DRY_RUN_HINT = (
    "dry run: flights are filed but not sent; set TRIPBOX_AIRTRAIL_DRY_RUN=false to send them"
)
# Names that were settings once: the unknown-name problem says what replaced each, and a note.
RENAMED: dict[str, tuple[str, str]] = {
    "TRIPBOX_DRY_RUN": (
        "TRIPBOX_AIRTRAIL_DRY_RUN",
        "it only stops writes to AirTrail; bookings are always filed",
    ),
    "TRIPBOX_PORT": ("TRIPBOX_LISTEN", "an address and a port, such as 127.0.0.1:8110"),
}
RENAMED_FIX = "renamed: use {new} ({note})"
NO_TRAVELLERS_HINT = (
    f"{TRAVELLERS_FILE} is absent: nobody is the owner, so no flight reaches AirTrail"
)


def _feature_row(settings: Settings, name: str) -> tuple[str, str]:
    """A feature's state and the hint next to it."""
    state = settings.feature(name).state
    if state == "invalid":
        return state, "see the problems below"
    if state == "off":
        if name == "model" and settings.llm_provider == "none":
            return state, MODEL_NONE_HINT
        if name == "enrichment" and settings.aeroapi_key:
            return state, KEY_WITHOUT_SWITCH
        return state, OFF_HINTS[name]
    if name == "web":
        login = settings.tripbox_auth
        return state, f"login: {'none (this computer only)' if login == 'none' else login}"
    if name == "airtrail":
        return state, _airtrail_hint(settings)
    if name == "model":
        return state, _model_hint(settings)
    return state, ""


# the ledger's `kv` row noting what the flight log's key may do (`write.lifecycle.note_key`)
AIRTRAIL_KEY_KV = "airtrail_key"


def _key_note(settings: Settings):
    """What the last look at the flight log's key found, as the ledger keeps it
    (`write.airtrail.KeyNote`), read without writing (`_kv_rows`); None when nothing is kept or
    the ledger cannot be read."""
    from tripbox.write.airtrail import KeyNote

    rows = _kv_rows(settings, AIRTRAIL_KEY_KV)
    return KeyNote.read(rows.get(AIRTRAIL_KEY_KV)) if isinstance(rows, dict) else None


def _airtrail_hint(settings: Settings) -> str:
    """The flight log's row: the dry run's words, then what `airtrail status` says of the key."""
    from tripbox.write.airtrail import KEY_NOT_CHECKED

    note = _key_note(settings)
    said = note.lines() if note is not None else [KEY_NOT_CHECKED]
    key = "; ".join([said[0], *(line.removeprefix("key: ") for line in said[1:])])
    return f"{DRY_RUN_HINT}; {key}" if settings.airtrail_dry_run else key


def key_problems(settings: Settings) -> list[ConfigProblem]:
    """What the last look at the key found missing or denied, as a problem `doctor` lists and
    counts: never one of `Settings.problems`, so it turns nothing off. Only while AirTrail is
    on."""
    from tripbox.write.airtrail import KEY_FIX, REFUSED_FIX, ROLE_FIX

    if settings.feature("airtrail").state != "on":
        return []
    note = _key_note(settings)
    if note is None:
        return []
    out = []
    name = "TRIPBOX_AIRTRAIL_API_KEY"
    if note.refused is not None:
        what = f"AirTrail refused the key (checked {note.at} UTC)"
        return [ConfigProblem(name, what, REFUSED_FIX, "airtrail")]
    if note.missing:
        what = f"the key lacks {', '.join(note.missing)} (checked {note.at} UTC)"
        out.append(ConfigProblem(name, what, KEY_FIX, "airtrail"))
    if note.denied:
        what = (
            f"your role in AirTrail does not permit {', '.join(note.denied)}"
            f" (checked {note.at} UTC)"
        )
        out.append(ConfigProblem(name, what, ROLE_FIX, "airtrail"))
    return out


def _model_hint(settings: Settings) -> str:
    """The provider, the model and the cap it is asked under: the monthly budget of a model
    whose price is known (or of any while the budget is 0), else its calls a day."""
    from tripbox.parse import budget  # the model's module: never loaded by the page

    if budget.by_month(settings):
        cap = f"budget ${settings.llm_monthly_budget_usd:.2f}/month"
    else:
        cap = f"no known price: at most {settings.llm_max_calls_per_day} calls a day"
    return f"{settings.llm_provider} · {settings.llm_model} · {cap}"


def _travellers_row(settings: Settings) -> tuple[str, str]:
    if any(p.key == TRAVELLERS_FILE and p.feature == "core" for p in settings.problems):
        return "invalid", "see the problems below"
    if not settings.travellers_path.exists():
        return "none", NO_TRAVELLERS_HINT
    book = settings.travellers
    return "on", f"owner: {len(book.me_aliases)} names, companions: {len(book.guests)}"


def _lists_row(settings: Settings) -> tuple[str, str]:
    files = (lists.AIRLINES_FILE, lists.STATES_FILE)
    if any(p.key in files and p.feature == "core" for p in settings.problems):
        return "invalid", "see the problems below"
    used = lists.overrides_in_use(settings.config_dir)
    return ("shipped + " + ",".join(used) if used else "shipped"), ""


# What `tripbox doctor` says of the worker, from its record (`tripbox.status`)
WORKER_NO_LEDGER = "no ledger yet: the first start makes it"
WORKER_NEVER = "no worker has run on this ledger"
WORKER_UNREADABLE = "the ledger could not be read"
_CODE = re.compile(r"[a-z0-9_]{1,40}")  # a code as the worker records one


_UNREADABLE = object()  # what _kv_rows answers for a ledger it could not read


def _kv_rows(settings: Settings, *keys: str) -> dict[str, str] | None | object:
    """These `kv` rows of the ledger (a row that is absent is left out), read as the health check
    reads them: through a connection that writes nothing (and, with no -wal beside the ledger,
    makes nothing beside it either), so `doctor` neither makes nor upgrades a ledger. None with
    no ledger, _UNREADABLE for one that cannot be read."""
    import sqlite3
    from urllib.parse import quote

    path = settings.ledger_path
    if not os.path.lexists(path):
        return None
    busy = any(path.with_name(path.name + end).exists() for end in ("-wal", "-journal"))
    mode = "ro" if busy else "ro&immutable=1"
    marks = ", ".join("?" for _ in keys)
    try:
        conn = sqlite3.connect(f"file:{quote(str(path.absolute()))}?mode={mode}", uri=True)
        try:
            rows = conn.execute(f"SELECT k, v FROM kv WHERE k IN ({marks})", keys).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return _UNREADABLE
    return {str(k): v for k, v in rows}


def _worker_row(settings: Settings) -> tuple[str, str]:
    """The worker's record, read through `_kv_rows`. Never a problem: a stack whose worker has
    not started yet is a sound one."""
    from tripbox import status, util

    rows = _kv_rows(settings, status.KV_WORKER)
    if rows is None:
        return "none", WORKER_NO_LEDGER
    if not isinstance(rows, dict):
        return "unknown", WORKER_UNREADABLE
    row = (rows[status.KV_WORKER],) if status.KV_WORKER in rows else None
    # just enough of a ledger for `status.read`: its `kv.get`, answered from the row read
    kv = SimpleNamespace(get=lambda key: row[0] if row else None)
    record = status.read(SimpleNamespace(kv=kv))
    if record is None:
        return "none", WORKER_NEVER
    now = util.now()
    if status.live(record, now):
        beat = util.parse_iso(str(record.get("beat_at")))
        hint = f"beat {max(0, int((now - beat).total_seconds()))} s ago"
        if record.get("busy") is True:
            hint += ", busy"
        last = record.get("last")
        poll = last.get("poll") if isinstance(last, dict) else None
        if isinstance(poll, dict):
            code = poll.get("code")
            word = code if isinstance(code, str) and _CODE.fullmatch(code) else "error"
            hint += f"; last poll: {word}"
        return "running", hint
    if record.get("stopped_at"):
        return "stopped", f"since {_shown_time(record['stopped_at'])}"
    return "stopped", f"last beat {_shown_time(record.get('beat_at'))}"


def _shown_time(value: object) -> str:
    """A time of the worker's record, a row the page container can write, as `doctor` prints
    it: read as a time and written again (UTC, to the second), else `unknown`; never as stored."""
    import datetime as dt

    from tripbox import util

    if not isinstance(value, str):
        return "unknown"
    try:
        return util.parse_iso(value).astimezone(dt.UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return "unknown"


# The rows of `report`, in order: a label and what makes its state and hint.
ROWS: tuple[tuple[str, Callable[[Settings], tuple[str, str]]], ...] = (
    *((name, lambda s, name=name: _feature_row(s, name)) for name in FEATURES),
    ("travellers", _travellers_row),
    ("lists", _lists_row),
)


def report(
    settings: Settings,
    folders: list[ConfigProblem] | None = None,
    keys: list[ConfigProblem] | None = None,
) -> list[str]:
    """The lines `tripbox doctor` prints: one row per feature, the travellers file and the
    lists, the install's folders (`files`) and the worker, then every problem, the settings',
    the folders' (`folders`, what `files.problems` found of every folder, when the caller has
    it) and the flight log's key's (`keys`, `key_problems`). It holds no secret's value and none
    of an unknown name: a problem's text is built without them."""
    from tripbox import files  # it reads the settings this module defines

    if folders is None:
        folders = files.problems(settings, files.NAMES)
    if keys is None:
        keys = key_problems(settings)
    lines = ["tripbox doctor"]
    rows = (*ROWS, ("files", lambda s: files.row(s, folders)), ("worker", _worker_row))
    for label, make in rows:
        state, hint = make(settings)
        lines.append(f"{label:<15}{state:<9}{hint}".rstrip())
    every = [*settings.problems, *folders, *keys]
    lines.append(f"problems: {len(every)}")
    lines.extend(str(p) for p in every)
    return lines


# The example settings file (.env.example) is written by env_example: its head, then a section
# per feature in this order, then the compose file's own names.
EXAMPLE_HEAD = (
    "# tripbox settings. Copy to .env beside compose.yaml (chmod 600) and set TRIPBOX_PASSWORD.",
    "# Every other line is optional: a feature is on when its settings are set.",
    '# "docker compose run --rm worker tripbox doctor" says what is missing or wrong.',
    "# A commented line shows the default, or an example where one helps. A value is written as it",
    "# is: no quotes and no space before a #; a literal $ is written $$.",
)
EXAMPLE_SECTIONS = (
    ("web", "The page"),
    ("core", "Core"),
    ("mail", "Mail"),
    ("model", "The model"),
    ("airtrail", "AirTrail"),
    ("enrichment", "Enrichment"),
    ("notifications", "Notifications"),
)
# The one line the example sets: the page's password, which a new install must choose
EXAMPLE_LIVE = "TRIPBOX_PASSWORD"


def env_example() -> str:
    """The text of .env.example: every key a settings file holds (no folder and no marker, which
    the image sets, and no development switch) as its note and its line commented out, showing its
    example or else its default; the page's password alone is a live line, first in its
    section."""
    lines = list(EXAMPLE_HEAD)
    for feature, title in EXAMPLE_SECTIONS:
        keys = [k for k in KEYS if k.feature == feature and k.name not in (*PATH_KEYS, IMAGE_ENV)]
        keys.sort(key=lambda k: k.name != EXAMPLE_LIVE)  # stable: the rest keep registry order
        lines += ["", f"# --- {title} ---"]
        for k in keys:
            line = f"{k.name}={k.example or k.default}"
            lines += [f"# {k.note}", line if k.name == EXAMPLE_LIVE else f"# {line}"]
    lines += ["", "# --- Docker Compose ---"]
    for name, default, note in COMPOSE_ONLY:
        lines += [f"# {note}", f"# {name}={default}"]
    lines += [
        "# Runs the Telegram bot as well, with the Telegram settings above.",
        "# COMPOSE_PROFILES=telegram",
    ]
    return "\n".join(lines) + "\n"


# 1: nothing leaves the process (the rehearsal). build_context writes flights to an AirTrail held
# in memory (when one is configured) and builds no notification channel, the model is allowed
# only as recorded answers (or while recording them), and every command and lookup that needs the
# network refuses. Unset or empty, it changes nothing.
OFFLINE_ENV = "TRIPBOX_NO_NETWORK"
# The model key's name: the rehearsal scripts pass the key under it, and a settings file cannot
# replace it (evalrun.ENV_WINS).
MODEL_KEY_ENV = "TRIPBOX_LLM_API_KEY"


def offline() -> bool:
    """TRIPBOX_NO_NETWORK: 1, 0 or unset; anything else is an error, so a typo never means
    online."""
    value = os.environ.get(OFFLINE_ENV, "")
    if value not in ("", "0", "1"):
        raise ConfigError(f"{OFFLINE_ENV} must be 1, 0 or unset")
    return value == "1"


def in_image() -> bool:
    """The process runs in the shipped image (IMAGE_ENV is 1)."""
    return os.environ.get(IMAGE_ENV) == "1"


def offline_refusal(what: str) -> str | None:
    """The one line a network-bound command or lookup answers with while offline, else None."""
    return f"{OFFLINE_ENV} is set: {what} needs the network" if offline() else None


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
# TRIPBOX_MAIL_TRUST: gmail = mail from or forwarded by an allowed sender must also carry Gmail's
# own Authentication-Results pass; mailbox = the allowed-sender check alone
MAIL_TRUSTS = ("gmail", "mailbox")
# The mail hosts that are Gmail's own: only there is the Authentication-Results header that names
# Gmail written by Gmail, so `gmail` trust is for them alone (compared in lower case).
GMAIL_HOSTS = ("imap.gmail.com", "imap.googlemail.com")
# TRIPBOX_MAIL_FILING: labels = each handled message is copied into a tripbox/ folder (a Gmail
# label); none = the mailbox is left as it is
MAIL_FILINGS = ("labels", "none")
# TRIPBOX_LLM_PROVIDER: who reads the emails that carry no structured data; none turns the model
# off whatever else is set
LLM_PROVIDERS = ("anthropic", "openai", "none")
# The flight-data services enrichment can ask (TRIPBOX_ENRICH).
ENRICH_SERVICES = ("aeroapi",)
COVER_SOURCES = ("off", "wikimedia")

AUTHS = ("password", "proxy", "cloudflare", "none")
# The logins under which saved copies are on unless TRIPBOX_OFFLINE says otherwise: the page sees
# their sign-out, so it can empty the store
OFFLINE_BY_DEFAULT = ("password", "none")
_SPACE = re.compile(r"\s")


class _Bad(ValueError):
    """A value that fails its key's check; `args[0]` is the part of it a problem quotes."""


# Each check takes the value of a field's key (unset or blank already replaced by the key's
# default, which always passes) and returns the field's value, or raises _Bad.


def _boolean(v: str) -> bool:
    """Only these spellings: a typo in TRIPBOX_AIRTRAIL_DRY_RUN must not silently mean 'off'."""
    s = v.strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise _Bad(v)


def _whole(minimum: int, maximum: int | None = None) -> Callable[[str], int]:
    def check(v: str) -> int:
        try:
            n = int(v.strip())
        except ValueError:
            raise _Bad(v) from None
        if n < minimum or (maximum is not None and n > maximum):
            raise _Bad(v)
        return n

    return check


def _number(minimum: float) -> Callable[[str], float]:
    def check(v: str) -> float:
        try:
            x = float(v.strip())
        except ValueError:
            raise _Bad(v) from None
        if not x >= minimum:  # NaN fails too
            raise _Bad(v)
        return x

    return check


def _prices(v: str) -> tuple[float, float] | None:
    """Unset or blank: no price given. Set: INPUT,OUTPUT, two numbers of 0 or more."""
    if not v.strip():
        return None
    parts = v.split(",")
    if len(parts) != 2:
        raise _Bad(v)
    try:
        given = (float(parts[0].strip()), float(parts[1].strip()))
    except ValueError:
        raise _Bad(v) from None
    if not all(math.isfinite(x) and x >= 0 for x in given):
        raise _Bad(v)
    return given


def _one_of(words: tuple[str, ...]) -> Callable[[str], str]:
    def check(v: str) -> str:
        s = v.strip().lower()
        if s not in words:
            raise _Bad(v)
        return s

    return check


def _service(v: str) -> str:
    """Unset or blank: "" (enrichment off). Set: one of ENRICH_SERVICES."""
    if not v.strip():
        return ""
    return _one_of(ENRICH_SERVICES)(v)


def _maybe_boolean(v: str) -> bool | None:
    """Unset or blank: None (the login mode decides, `Settings.from_env`). Set: as _boolean."""
    if not v.strip():
        return None
    return _boolean(v)


def _filing(v: str) -> str:
    """Unset or blank: "" (the trust mode decides, `Settings.from_env`). Set: one of
    MAIL_FILINGS."""
    if not v.strip():
        return ""
    return _one_of(MAIL_FILINGS)(v)


def _zone(v: str) -> str:
    name = v.strip()
    try:
        ZoneInfo(name)
    except ValueError, KeyError, OSError:  # ZoneInfoNotFoundError is a KeyError
        raise _Bad(v) from None
    return name


def _absolute(v: str) -> Path:
    path = Path(v.strip())
    if not path.is_absolute():
        raise _Bad(v)
    return path


def _csv(v: str | None) -> tuple[str, ...]:
    return tuple(x.strip() for x in (v or "").split(",") if x.strip())


def _listed(entry_ok: Callable[[str], bool]) -> Callable[[str], tuple[str, ...]]:
    """A comma-separated list, lower-cased, each entry passing `entry_ok`; a value that holds
    only commas and spaces is bad as a whole."""

    def check(v: str) -> tuple[str, ...]:
        entries = tuple(x.lower() for x in _csv(v))
        if not entries and v.strip():
            raise _Bad(v)
        for entry in entries:
            if not entry_ok(entry):
                raise _Bad(entry)
        return entries

    return check


def _airport_codes(v: str) -> tuple[str, ...]:
    codes = tuple(x.upper() for x in _csv(v))
    for code in codes:
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise _Bad(code)
    return codes


def _host(v: str) -> str:
    s = v.strip()
    if "://" in s or _SPACE.search(s):
        raise _Bad(v)
    return s


def _no_space(v: str) -> str:
    s = v.strip()
    if _SPACE.search(s):
        raise _Bad(v)
    return s


def _web_address() -> Callable[[str], str]:
    """http:// or https:// and a host, the trailing / stripped."""

    def check(v: str) -> str:
        s = v.strip().rstrip("/")
        if not s:
            return s  # the default: none configured
        try:
            parts = urlsplit(s)
            parts.port  # noqa: B018 - raises ValueError for a port that is no number
        except ValueError:
            raise _Bad(v) from None
        if parts.scheme not in ("http", "https") or not parts.hostname or _SPACE.search(s):
            raise _Bad(v)
        return s

    return check


# TRIPBOX_BASE_URL: a path segment, and the host and port (a port as a browser writes it: no
# leading zero)
_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_HOST_PORT = re.compile(r"(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[1-9][0-9]{0,4})?")
# The port a scheme has when none is written: a browser's Origin never names it
_DEFAULT_PORT = {"http": 80, "https": 443}


def _base_url(v: str) -> str:
    """http:// or https://, a host, an optional port and an optional path of plain segments (a
    trailing / dropped): no login, query, fragment, space, backslash or encoded character, and
    no `.` or `..` segment, which a browser would resolve away. The host is lower-cased and the
    scheme's own port dropped, so the origin is the one a browser sends."""
    s = v.strip()
    s = s[:-1] if s.endswith("/") else s
    if _SPACE.search(s) or any(ch in s for ch in "?#@%\\"):
        raise _Bad(v)
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        raise _Bad(v) from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or not _HOST_PORT.fullmatch(parts.netloc)
        or port == 0
    ):
        raise _Bad(v)
    if parts.path:
        segments = parts.path.split("/")
        if segments[0] or not all(
            _SEGMENT.fullmatch(seg) and seg not in (".", "..") for seg in segments[1:]
        ):
            raise _Bad(v)
    host = parts.netloc.lower()
    if port is not None and port == _DEFAULT_PORT[parts.scheme]:
        host = host.rsplit(":", 1)[0]
    return f"{parts.scheme}://{host}{parts.path}"


# TRIPBOX_LISTEN: an IPv4 address, or an IPv6 address in brackets, then a port with no leading
# zero
_LISTEN = re.compile(r"(?:([0-9.]+)|\[([0-9A-Fa-f:.]+)\]):([1-9][0-9]{0,4})")


def _listen(v: str) -> tuple[str, int]:
    """ADDRESS:PORT, an address read as one (never a name) and a port from 1 to 65535; the
    address is given back without its brackets."""
    m = _LISTEN.fullmatch(v.strip())
    if not m:
        raise _Bad(v)
    v4, v6, port = m.groups()
    try:
        address = ipaddress.IPv4Address(v4) if v4 else ipaddress.IPv6Address(v6)
    except ValueError:
        raise _Bad(v) from None
    if int(port) > 65535:
        raise _Bad(v)
    return str(address), int(port)


def _published_beyond(listen: tuple[str, int]) -> bool:
    """A listen address other than loopback: any machine that reaches it reaches the page."""
    return not ipaddress.ip_address(listen[0]).is_loopback


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _is_address(entry: str) -> bool:
    return entry.count("@") == 1


def _is_domain(entry: str) -> bool:
    return "." in entry and "@" not in entry and not _SPACE.search(entry)


def _trusted_domains(v: str) -> tuple[str, ...]:
    """Unset or blank: the shipped list. Set: exactly the domains it lists."""
    if not v.strip():
        return lists.trusted_sender_domains()
    return _listed(_is_domain)(v)


Network = ipaddress.IPv4Network | ipaddress.IPv6Network
# Where an IPv6 socket puts the IPv4 addresses it is reached from (::ffff:a.b.c.d)
_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")


def _as_ipv4(network: Network) -> Network:
    """An IPv4-mapped IPv6 network read as the IPv4 network it maps; any other as it is."""
    if network.version == 6 and network.prefixlen >= 96 and network.subnet_of(_MAPPED):
        mapped = network.network_address.ipv4_mapped
        return ipaddress.IPv4Network((mapped, network.prefixlen - 96))
    return network


def _networks(v: str) -> tuple[Network, ...]:
    """Comma-separated addresses and networks (an address is a network of one, and a network is
    written with no address bits past its prefix), each read as an address, never as text; a
    value that holds only commas and spaces is bad as a whole."""
    entries = _csv(v)
    if not entries and v.strip():
        raise _Bad(v)
    out: list[Network] = []
    for entry in entries:
        try:
            out.append(_as_ipv4(ipaddress.ip_network(entry)))
        except ValueError:
            raise _Bad(entry) from None
    return tuple(dict.fromkeys(out))


def _header_name(v: str) -> str:
    """Letters, digits and `-`: no `_`, which some proxies drop and some read as `-`."""
    s = v.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", s):
        raise _Bad(v)
    return s


def _is_user_name(entry: str) -> bool:
    """Printable ASCII with no space (a header's value is read as such)."""
    return bool(re.fullmatch(r"[!-~]+", entry))


@dataclass(frozen=True)
class _Check:
    """A field's format check and the problem it gives: `shown` with `{}` where the value goes
    (None: a value of this key is never shown, as a URL can carry a password), `hidden` for
    when the value is not shown, and `fix` (`{default}` = the key's default)."""

    check: Callable[[str], object]
    shown: str | None
    hidden: str
    fix: str


_YES_NO = ("{} is not true or false", "not true or false", "use true/false, 1/0, yes/no or on/off")
_ABSOLUTE = (
    "{} is not an absolute path",
    "not an absolute path",
    "use a path that starts with /, such as {default}",
)
_ADDRESSES = (
    "{} is not an email address",
    "not a list of email addresses",
    "list addresses such as alex@example.com, comma-separated",
)
# The format check of every field that has one (the registry's "what is checked"). A secret's
# value is never shown, whatever its check (the ntfy address's included).
_CHECKS: dict[str, _Check] = {
    "tz": _Check(
        _zone,
        "unknown time zone {}",
        "an unknown time zone",
        "use an IANA name such as America/Chicago",
    ),
    "home_airports": _Check(
        _airport_codes,
        "{} is not a three-letter airport code",
        "not a list of three-letter airport codes",
        "list IATA codes such as MSP,RST",
    ),
    "airtrail_dry_run": _Check(_boolean, *_YES_NO),
    "offline": _Check(_maybe_boolean, *_YES_NO),
    "data_dir": _Check(_absolute, *_ABSOLUTE),
    "archive_dir": _Check(_absolute, *_ABSOLUTE),
    "logs_dir": _Check(_absolute, *_ABSOLUTE),
    "config_dir": _Check(_absolute, *_ABSOLUTE),
    "backups_dir": _Check(_absolute, *_ABSOLUTE),
    "mail_host": _Check(
        _host,
        "{} is not a host name",
        "not a host name",
        "use the server's name alone, such as imap.gmail.com",
    ),
    "allowed_senders": _Check(_listed(_is_address), *_ADDRESSES),
    "mail_trust": _Check(
        _one_of(MAIL_TRUSTS),
        "{} is not gmail or mailbox",
        "not gmail or mailbox",
        "use gmail or mailbox",
    ),
    "mail_trusted_sender_domains": _Check(
        _trusted_domains,
        "{} is not a domain name",
        "not a list of domain names",
        "list domains such as example.com, comma-separated",
    ),
    "mail_filing": _Check(
        _filing,
        "{} is not labels or none",
        "not labels or none",
        "use labels or none",
    ),
    "llm_provider": _Check(
        _one_of(LLM_PROVIDERS),
        "{} is not anthropic, openai or none",
        "not anthropic, openai or none",
        "use anthropic, openai or none",
    ),
    "llm_model": _Check(
        _no_space,
        "{} is not a model name",
        "not a model name",
        "use a name with no spaces, such as {default}",
    ),
    "llm_base_url": _Check(
        _web_address(),
        None,
        "not an http:// or https:// address with a host",
        "use the server's address as the worker reaches it (a model server in another container"
        " on the same Docker network by its service name, or this machine's address on your"
        " network; not localhost), such as http://models.example:11434/v1",
    ),
    "llm_monthly_budget_usd": _Check(
        _number(0.0),
        "{} is not a number of 0 or more",
        "not a number of 0 or more",
        "use a number such as {default}",
    ),
    "llm_max_calls_per_day": _Check(
        _whole(1),
        "{} is not a whole number of 1 or more",
        "not a whole number of 1 or more",
        "use a whole number such as {default}",
    ),
    "llm_price": _Check(
        _prices,
        "{} is not two prices",
        "not two prices",
        "use INPUT,OUTPUT in dollars per million tokens, such as 0.10,0.50",
    ),
    "airtrail_url": _Check(
        _web_address(),
        None,
        "not an http:// or https:// address with a host",
        "use the address AirTrail answers at, such as http://flightlog.example:3000",
    ),
    "airtrail_public_url": _Check(
        _web_address(),
        None,
        "not an http:// or https:// address with a host",
        "use the address you open your flight log at, such as https://flightlog.example",
    ),
    "ntfy_url": _Check(
        _web_address(),
        None,
        "not an http:// or https:// address with a host",
        "use your topic's address, such as https://ntfy.example/trips",
    ),
    "covers": _Check(
        _one_of(COVER_SOURCES),
        "{} is not off or wikimedia",
        "not off or wikimedia",
        "use wikimedia to find photos, or off",
    ),
    "enrich": _Check(
        _service,
        "{} is not a flight-data service",
        "not a flight-data service",
        "use aeroapi, or remove the line",
    ),
    "aeroapi_monthly_budget_usd": _Check(
        _number(0.0),
        "{} is not a number of 0 or more",
        "not a number of 0 or more",
        "use a number such as {default}",
    ),
    "aeroapi_max_calls_per_run": _Check(
        _whole(1),
        "{} is not a whole number of 1 or more",
        "not a whole number of 1 or more",
        "use a whole number such as {default}",
    ),
    "tripbox_auth": _Check(
        _one_of(AUTHS),
        "{} is not password, proxy, cloudflare or none",
        "not password, proxy, cloudflare or none",
        "use password, proxy, cloudflare or none",
    ),
    "tripbox_allowed_emails": _Check(_listed(_is_address), *_ADDRESSES),
    "trusted_proxies": _Check(
        _networks,
        "{} is not an address or a network",
        "not a list of addresses or networks",
        PROXIES_FIX,
    ),
    "proxy_user_header": _Check(
        _header_name,
        "{} is not a header name",
        "not a header name",
        "use letters, digits and -, such as {default}",
    ),
    "proxy_users": _Check(
        _listed(_is_user_name),
        "{} is not a user name",
        "not a list of user names",
        "list the names your proxy signs people in as, comma-separated, such as alex",
    ),
    "base_url": _Check(
        _base_url,
        None,
        "not an address the page can live at",
        "use the address people open, such as https://trips.example.com or"
        " https://example.com/tripbox",
    ),
    "listen": _Check(
        _listen,
        "{} is not an address and a port",
        "not an address and a port",
        "use 127.0.0.1:8110 for this computer only, or 0.0.0.0:8110 for every network it is on",
    ),
    "source_url": _Check(
        _web_address(),
        None,
        "not an http:// or https:// address with a host",
        "use the address of the page's source code, such as {default}",
    ),
}
# A value that fails its check falls back to its key's default only because every default here is
# the strict side, or is never used while its problem stands: no mailbox, AirTrail or allowed
# address; no trusted proxy and no proxy user; the page's address on localhost, which no notice
# links (Settings.link_base); the page listening on loopback; dry run on; Gmail's own check on; the
# password login on; and a core, web or enrichment problem stops the commands that would read the
# rest. A DISPLAY_ONLY key's default is only what the page shows (no link to the flight log, the
# project's own source address) and its problem turns nothing off. The one exception is the trusted
# sender domains, whose default is the whole shipped list: a value that fails trusts no
# auto-forwarded sender, so a bad edit never widens trust.
_ON_BAD: dict[str, object] = {"mail_trusted_sender_domains": ()}
# What a problem may show of a value: short and plain, or nothing at all. A secret pasted into
# the wrong line is long or holds other characters, and a value is never cut to fit.
_PLAIN = re.compile(r"[A-Za-z0-9 /_.:+-]{1,16}")


def _shown(part: str) -> str | None:
    return repr(part) if _PLAIN.fullmatch(part) else None


class _Reader:
    """One environment, read field by field: unset or blank, a field takes its key's default; a
    value that fails its check is a problem, and the field takes its fallback (_ON_BAD)."""

    def __init__(self, environ: Mapping[str, str]):
        self.env = environ
        self.problems: list[ConfigProblem] = []

    def is_set(self, name: str) -> bool:
        return bool((self.env.get(name) or "").strip())

    def raw(self, field: str) -> str:
        key = _KEY_BY_FIELD[field]
        if self.is_set(key.name):
            return self.env[key.name]
        return key.default

    def value(self, field: str):
        if field not in _CHECKS:
            return self.raw(field)
        c = _CHECKS[field]
        key = _KEY_BY_FIELD[field]
        try:
            return c.check(self.raw(field))
        except _Bad as bad:
            shown = None if key.secret or c.shown is None else _shown(str(bad.args[0]))
            what = c.hidden if shown is None else c.shown.format(shown)
            fix = c.fix.format(default=key.default)
            self.problems.append(ConfigProblem(key.name, what, fix, key.feature))
            return _ON_BAD[field] if field in _ON_BAD else c.check(key.default)


def _gmail_trust_needs_gmail(r: _Reader, trust: str, host: str) -> None:
    """`gmail` trust (set or by default) with a mail host that is set and is not Gmail's is a
    problem, which leaves mail off: another server may stamp no header of its own above one the
    sender typed. Nothing more is said of a trust value that is already a problem."""
    if trust != "gmail" or not host or host.lower() in GMAIL_HOSTS:
        return
    key = KEY_BY_NAME["TRIPBOX_MAIL_TRUST"]
    if any(p.key == key.name for p in r.problems):
        return
    what, fix = "gmail trust needs Gmail's own mailbox", f"set {key.name}=mailbox for {host}"
    r.problems.append(ConfigProblem(key.name, what, fix, key.feature))


def _no_network_readable(r: _Reader) -> None:
    """TRIPBOX_NO_NETWORK as offline() reads it: 1, 0 or unset. Anything else is a core problem
    (offline() raises for it too), so a typo never means online and doctor says so."""
    if r.env.get(OFFLINE_ENV, "") not in ("", "0", "1"):
        what, fix = "not 1, 0 or unset", "use 1, or remove the line"
        r.problems.append(ConfigProblem(OFFLINE_ENV, what, fix, "core"))


def _home_airports(r: _Reader, codes: tuple[str, ...]) -> tuple[str, ...]:
    """Each home airport must be in the airport table, which is read only when one is set: every
    code it lacks is a problem of its own, and the setting then takes its default, as a value that
    fails its format check does."""
    if not codes:
        return codes
    key = _KEY_BY_FIELD["home_airports"]
    try:
        table = refdata.Airports()
        unknown = [c for c in dict.fromkeys(codes) if table.airport(c) is None]
    except Exception:  # noqa: BLE001 - a damaged install must not stop every command
        what = "the airport table could not be read"
        r.problems.append(ConfigProblem(key.name, what, "reinstall tripbox", key.feature))
        return _CHECKS["home_airports"].check(key.default)
    if not unknown:
        return codes
    for code in unknown:  # three letters, as the format check let through: short and plain
        what = f"{code!r} is not in the airport table"
        r.problems.append(ConfigProblem(key.name, what, "check the code", key.feature))
    return _CHECKS["home_airports"].check(key.default)


def _password_problems(r: _Reader) -> list[ConfigProblem]:
    """TRIPBOX_AUTH=password takes exactly one of the pair: a password of MIN_LENGTH to MAX_TYPED
    characters that is neither the instructions' placeholder pasted as it is nor a line `tripbox
    passwd` printed (one set there by mistake would be taken as the password), or such a line.
    Neither value is ever shown."""
    plain, hashed = r.is_set("TRIPBOX_PASSWORD"), r.is_set("TRIPBOX_PASSWORD_HASH")
    if plain and hashed:
        what, fix = "set together with TRIPBOX_PASSWORD", "keep one of them"
        return [ConfigProblem("TRIPBOX_PASSWORD_HASH", what, fix, "web")]
    if hashed:
        try:
            passwords.parse_hash(r.raw("password_hash"))
        except ValueError:
            what = 'not a line "tripbox passwd" printed'
            fix = "run it again and paste the whole line"
            return [ConfigProblem("TRIPBOX_PASSWORD_HASH", what, fix, "web")]
        return []
    if not plain:
        return [ConfigProblem("TRIPBOX_PASSWORD", "not set", PASSWORD_FIX, "web")]
    plain_value = r.raw("password")
    if passwords.is_example(plain_value):
        what = "still the example from the instructions"
        fix = f"choose your own password of {passwords.MIN_LENGTH} characters or more"
        return [ConfigProblem("TRIPBOX_PASSWORD", what, fix, "web")]
    if len(plain_value) < passwords.MIN_LENGTH:
        what = f"shorter than {passwords.MIN_LENGTH} characters"
        fix = f"use {passwords.MIN_LENGTH} or more"
        return [ConfigProblem("TRIPBOX_PASSWORD", what, fix, "web")]
    if len(plain_value) > passwords.MAX_TYPED:  # the page never checks a longer typed value
        what = f"longer than {passwords.MAX_TYPED} characters"
        fix = f"use {passwords.MAX_TYPED} or fewer"
        return [ConfigProblem("TRIPBOX_PASSWORD", what, fix, "web")]
    try:
        passwords.parse_hash(plain_value)
    except ValueError:
        return []
    what = 'holds a line "tripbox passwd" printed'
    fix = "put it in TRIPBOX_PASSWORD_HASH instead"
    return [ConfigProblem("TRIPBOX_PASSWORD", what, fix, "web")]


def _base_path_free(r: _Reader, url: str) -> str:
    """A base path whose first segment is one the page uses (RESERVED_SEGMENTS) is a problem of
    its own, and the setting then takes its default, as a value that fails its format check
    does. The segment is one of the page's names, so the problem shows it."""
    path = urlsplit(url).path
    segment = path.split("/")[1] if path else ""
    if segment not in RESERVED_SEGMENTS:
        return url
    key = _KEY_BY_FIELD["base_url"]
    what = f'its path begins with "{segment}", a name the page uses'
    fix = "choose another path, such as /tripbox"
    r.problems.append(ConfigProblem(key.name, what, fix, key.feature))
    return _CHECKS["base_url"].check(key.default)


def _proxies_bounded(r: _Reader, networks: tuple[Network, ...]) -> tuple[Network, ...]:
    """A trusted network of prefix length 0 holds every address, so a header any client sends
    would be believed: a problem of its own, and the setting then trusts nobody, as a value
    that fails its format check does."""
    if not any(network.prefixlen == 0 for network in networks):
        return networks
    key = _KEY_BY_FIELD["trusted_proxies"]
    r.problems.append(ConfigProblem(key.name, "trusts every address", PROXIES_FIX, key.feature))
    return ()


def _list_problems(config_dir: Path) -> tuple[ConfigProblem, ...]:
    """What is wrong with the list override files; the text is the loader's fixed one."""
    out = []
    for load in (lists.airlines, lists.us_states):
        try:
            load(config_dir)
        except lists.ListFileError as bad:
            out.append(ConfigProblem(bad.file, bad.what, "fix the file or remove it", "core"))
    return tuple(out)


def _states(
    r: _Reader,
    auth: str,
    origin: str,
    listen: tuple[str, int],
    travellers_problems: tuple[ConfigProblem, ...],
    has_owner: bool,
    llm_provider: str,
) -> tuple[FeatureState, ...]:
    """Each feature's state from what is set, the format problems already found and the
    travellers file (its problems are core's; AirTrail needs an owner); the model's also from its
    provider (none: off, whatever else is set; openai: on with a model named and a key or an
    address; anthropic: no address). No login takes a local base URL and a loopback listen
    address, each a problem of its own. A problem of a DISPLAY_ONLY key is the feature's, listed
    with the rest, but leaves its state as the others make it."""
    states: dict[str, FeatureState] = {}
    for name in FEATURES:
        problems = [p for p in r.problems if p.feature == name]
        switched: list[str] = []
        missing: tuple[str, ...] = ()
        if name == "core":
            problems += travellers_problems
        elif name == "web":
            if auth == "password":
                problems += _password_problems(r)
            elif auth == "proxy":
                missing = tuple(k for k in PROXY_NEEDS if not r.is_set(k))
                problems += [
                    ConfigProblem(k, "not set", "TRIPBOX_AUTH=proxy needs it", name)
                    for k in missing
                ]
            elif auth == "cloudflare":
                missing = tuple(k for k in CLOUDFLARE_NEEDS if not r.is_set(k))
                problems += [
                    ConfigProblem(k, "not set", "TRIPBOX_AUTH=cloudflare needs it", name)
                    for k in missing
                ]
            else:
                if not is_local_origin(origin):
                    problems.append(
                        ConfigProblem(
                            "TRIPBOX_AUTH",
                            "none with a public TRIPBOX_BASE_URL would publish the page"
                            " unauthenticated",
                            "use password, proxy or cloudflare, or TRIPBOX_AUTH=none (the local"
                            " mode) with TRIPBOX_BASE_URL on localhost",
                            name,
                        )
                    )
                # the page's Host check is no wall: a client that reaches the port can send
                # localhost, so with no login nothing but this computer may reach it
                if _published_beyond(listen):
                    problems.append(
                        ConfigProblem(
                            "TRIPBOX_LISTEN",
                            "publishes the page beyond this computer while TRIPBOX_AUTH=none",
                            "use 127.0.0.1:8110, or choose a login: password, proxy or"
                            " cloudflare",
                            name,
                        )
                    )
        elif name == "model" and llm_provider == "none":
            problems = []  # none of the model's other settings is read
        elif name == "model" and llm_provider == "openai":
            # naming the provider switches it on: it needs a model named for the server (the
            # Anthropic default is never sent to another) and a key, an address or both
            switched = ["TRIPBOX_LLM_PROVIDER"]
            if not (r.is_set("TRIPBOX_LLM_API_KEY") or r.is_set("TRIPBOX_LLM_BASE_URL")):
                missing += ("TRIPBOX_LLM_API_KEY", "TRIPBOX_LLM_BASE_URL")
                problems.append(
                    ConfigProblem(
                        "TRIPBOX_LLM_PROVIDER",
                        "openai needs TRIPBOX_LLM_API_KEY or TRIPBOX_LLM_BASE_URL",
                        "set the key of a hosted service, or the address of your own server",
                        name,
                    )
                )
            # the Anthropic default, written out or not, names no model another server serves
            anthropic_default = KEY_BY_NAME["TRIPBOX_LLM_MODEL"].default
            if r.raw("llm_model").strip() == anthropic_default:
                missing += ("TRIPBOX_LLM_MODEL",)
                problems.append(
                    ConfigProblem(
                        "TRIPBOX_LLM_MODEL", "not set", "name the model your server serves", name
                    )
                )
        else:
            if name == "model" and r.is_set("TRIPBOX_LLM_BASE_URL"):
                # Anthropic takes no address: one problem, whatever its format
                problems = [p for p in problems if p.key != "TRIPBOX_LLM_BASE_URL"]
                problems.append(
                    ConfigProblem(
                        "TRIPBOX_LLM_BASE_URL",
                        "only the openai provider takes an address",
                        "remove it, or set TRIPBOX_LLM_PROVIDER=openai",
                        name,
                    )
                )
            # each way of switching the feature on (notifications: a channel) that is switched
            # needs all of its own keys; with none switched, what is missing is all they need
            ways = (
                CHANNELS
                if name == "notifications"
                else {LABELS[name]: (SWITCHES[name], NEEDS[name])}
            )
            for label, (keys, needs) in ways.items():
                set_here = [k for k in keys if r.is_set(k)]
                if set_here:
                    unset = tuple(k for k in needs if not r.is_set(k))
                    switched += set_here
                    missing += unset
                    fix = f"{label} is half configured; set it, or remove {', '.join(set_here)}"
                    problems += [ConfigProblem(k, "not set", fix, name) for k in unset]
            if not switched:
                missing = tuple(k for _, needs in ways.values() for k in needs if not r.is_set(k))
            if name == "airtrail" and not missing and not has_owner:
                # only the owner's flights are written to AirTrail: with nobody named, none is
                problems.append(
                    ConfigProblem(
                        TRAVELLERS_FILE,
                        "AirTrail is set up but no owner is named",
                        "add owner: names: so tripbox knows which flights are yours",
                        name,
                    )
                )
            if name == "enrichment" and switched and states["airtrail"].state != "on":
                problems.append(
                    ConfigProblem(
                        SWITCHES[name][0],
                        "enrichment fills in AirTrail flights",
                        f"set up AirTrail first, or remove {SWITCHES[name][0]}",
                        name,
                    )
                )
        problems.sort(key=_rank)
        if any(p.key not in DISPLAY_ONLY for p in problems):
            state = "invalid"
        elif name in ("core", "web") or switched:
            state = "on"
        else:
            state = "off"
        states[name] = FeatureState(name, state, missing, tuple(problems))
    return tuple(states.values())


def _unknown_names(environ: Mapping[str, str]) -> tuple[ConfigProblem, ...]:
    """TRIPBOX_* names that no key has (a typo silently disables what it meant to set). The
    value is never shown: it may be a secret under a misspelt name."""
    known = list(KEY_BY_NAME)
    composed = {name for name, _, _ in COMPOSE_ONLY}
    out = []
    names = (n for n in environ if n.startswith("TRIPBOX_") and n not in KEY_BY_NAME)
    for name in sorted(n for n in names if n not in composed):
        match = difflib.get_close_matches(name, known, n=1, cutoff=0.8)
        if name in RENAMED:
            new, note = RENAMED[name]
            fix = RENAMED_FIX.format(new=new, note=note)
        elif match:
            fix = f"did you mean {match[0]}?"
        else:
            fix = "check the spelling against docs/configuration.md"
        out.append(ConfigProblem(name, "not a tripbox setting", fix, "names"))
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    tz: str
    mail_host: str
    mail_user: str
    mail_password: str
    allowed_senders: tuple[str, ...]
    mail_trust: str  # one of MAIL_TRUSTS
    mail_trusted_sender_domains: tuple[str, ...]
    mail_filing: str  # one of MAIL_FILINGS, unset resolved by the trust mode
    airtrail_url: str
    airtrail_api_key: str
    airtrail_dry_run: bool  # true: flights are filed and nothing is written to AirTrail
    airtrail_public_url: str  # where the page links the flight log; "" for no link
    llm_provider: str  # one of LLM_PROVIDERS
    llm_api_key: str
    llm_model: str
    llm_base_url: str  # an OpenAI-compatible server's address, which may carry a login
    llm_monthly_budget_usd: float  # 0: the model is never asked
    llm_max_calls_per_day: int  # the cap of a model with no known price
    llm_price: tuple[float, float] | None  # input and output, US dollars per million tokens
    enrich: str  # the flight-data service enrichment asks (ENRICH_SERVICES), "" for none
    aeroapi_key: str
    aeroapi_monthly_budget_usd: float
    aeroapi_max_calls_per_run: int
    telegram_bot_token: str
    telegram_chat_id: str
    ntfy_url: str  # the topic's whole address, a secret
    ntfy_token: str
    home_airports: tuple[str, ...]
    # the trips page (served at TRIPBOX_BASE_URL)
    tripbox_auth: str  # one of AUTHS (none only on localhost)
    # the page's own login (TRIPBOX_AUTH=password): one of the pair, secrets that no repr shows
    password: str = dataclasses.field(repr=False)
    password_hash: str = dataclasses.field(repr=False)
    tripbox_access_team: str  # Zero Trust team name, e.g. sample-family
    tripbox_access_aud: str  # the Access application's AUD tag
    tripbox_allowed_emails: tuple[str, ...]
    # a login proxy in front of the page (TRIPBOX_AUTH=proxy): the networks it connects from (an
    # IPv4-mapped one read as IPv4), the header it names the user in and the users it may name
    # (lower-cased)
    trusted_proxies: tuple[Network, ...]
    proxy_user_header: str
    proxy_users: tuple[str, ...]
    # the address people open the page at, such as https://example.com/tripbox: its origin is
    # the one a form post must come from, and every link and redirect is built from it
    base_url: str
    # where the page listens (the address without brackets, the port); with Docker Compose,
    # where its port is published, while inside the container it listens on every interface
    listen: tuple[str, int]
    source_url: str  # where the page's source code is, which its about line links
    covers: Literal["off", "wikimedia"]  # where automatic cover photos come from
    # saved copies for offline use (TRIPBOX_OFFLINE); unset: on with the page's own password and
    # with no login, off behind a login proxy or Cloudflare, whose sign-out the page cannot see
    offline: bool
    data_dir: Path
    archive_dir: Path
    logs_dir: Path
    config_dir: Path
    backups_dir: Path
    # what is wrong with the settings, each feature's state, and who travels (see from_env)
    problems: tuple[ConfigProblem, ...]
    features: tuple[FeatureState, ...]
    travellers: PassengerBook
    # TRIPBOX_BASE_URL when it is set and sound, else None: a notice carries a link only then
    link_base: str | None

    def feature(self, name: str) -> FeatureState:
        for state in self.features:
            if state.name == name:
                return state
        raise KeyError(name)

    def on(self, name: str) -> bool:
        return self.feature(name).state == "on"

    def stopping(self, *features: str) -> list[ConfigProblem]:
        """The problems of `features` that stop what needs them: every one but a DISPLAY_ONLY
        key's, which is only listed (its field takes its default)."""
        return [p for p in self.problems if p.feature in features and p.key not in DISPLAY_ONLY]

    @property
    def telegram_on(self) -> bool:
        """Notifications are on and go to a Telegram chat."""
        return self.on("notifications") and bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ntfy_on(self) -> bool:
        """Notifications are on and go to an ntfy topic."""
        return self.on("notifications") and bool(self.ntfy_url)

    @property
    def require_google_auth(self) -> bool:
        """Mail must carry Gmail's own Authentication-Results pass (see MAIL_TRUSTS)."""
        return self.mail_trust == "gmail"

    @property
    def origin(self) -> str:
        """The page's scheme, host and port: TRIPBOX_BASE_URL without its path."""
        return _origin_of(self.base_url)

    @property
    def base_path(self) -> str:
        """The path the page is served under: "" or such as /a/b."""
        return urlsplit(self.base_url).path

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def travellers_path(self) -> Path:
        return self.config_dir / TRAVELLERS_FILE

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Never raises for a value: what is wrong is in `problems` (registry order, then the
        travellers file, unknown names last) and each feature's state in `features`. The files
        it reads are the travellers file, if there is one, and the bundled airport table, when a
        home airport is set."""
        # imported here: passengers imports this module for ConfigProblem
        from tripbox.validate.passengers import load_travellers

        e = dict(os.environ if environ is None else environ)
        lists.clear_caches()  # a fresh read of the list files on every load
        r = _Reader(e)
        v = {field: r.value(field) for field in _KEY_BY_FIELD}
        v["home_airports"] = _home_airports(r, v["home_airports"])
        v["base_url"] = _base_path_free(r, v["base_url"])
        v["trusted_proxies"] = _proxies_bounded(r, v["trusted_proxies"])
        sound = r.is_set("TRIPBOX_BASE_URL") and all(
            p.key != "TRIPBOX_BASE_URL" for p in r.problems
        )
        _gmail_trust_needs_gmail(r, v["mail_trust"], v["mail_host"])
        _no_network_readable(r)
        travellers, travellers_problems = load_travellers(v["config_dir"] / TRAVELLERS_FILE)
        travellers_problems += _list_problems(v["config_dir"])
        features = _states(
            r,
            v["tripbox_auth"],
            _origin_of(v["base_url"]),
            v["listen"],
            travellers_problems,
            bool(travellers.me_aliases),
            v["llm_provider"],
        )
        return cls(
            tz=v["tz"],
            mail_host=v["mail_host"],
            mail_user=v["mail_user"],
            mail_password=v["mail_password"].replace(" ", ""),
            allowed_senders=v["allowed_senders"],
            mail_trust=v["mail_trust"],
            mail_trusted_sender_domains=v["mail_trusted_sender_domains"],
            # a Gmail mailbox files handled mail under labels; another is left untouched
            mail_filing=v["mail_filing"]
            or ("labels" if v["mail_trust"] == "gmail" else "none"),
            airtrail_url=v["airtrail_url"],
            airtrail_api_key=v["airtrail_api_key"],
            airtrail_dry_run=v["airtrail_dry_run"],
            airtrail_public_url=v["airtrail_public_url"],
            llm_provider=v["llm_provider"],
            llm_api_key=v["llm_api_key"],
            llm_model=v["llm_model"],
            llm_base_url=v["llm_base_url"],
            llm_monthly_budget_usd=v["llm_monthly_budget_usd"],
            llm_max_calls_per_day=v["llm_max_calls_per_day"],
            llm_price=v["llm_price"],
            enrich=v["enrich"],
            aeroapi_key=v["aeroapi_key"],
            aeroapi_monthly_budget_usd=v["aeroapi_monthly_budget_usd"],
            aeroapi_max_calls_per_run=v["aeroapi_max_calls_per_run"],
            telegram_bot_token=v["telegram_bot_token"],
            telegram_chat_id=v["telegram_chat_id"],
            ntfy_url=v["ntfy_url"],
            ntfy_token=v["ntfy_token"],
            home_airports=v["home_airports"],
            tripbox_auth=v["tripbox_auth"],
            password=v["password"],
            password_hash=v["password_hash"],
            tripbox_access_team=v["tripbox_access_team"].strip(),
            tripbox_access_aud=v["tripbox_access_aud"].strip(),
            tripbox_allowed_emails=v["tripbox_allowed_emails"],
            trusted_proxies=v["trusted_proxies"],
            proxy_user_header=v["proxy_user_header"],
            proxy_users=v["proxy_users"],
            base_url=v["base_url"],
            listen=v["listen"],
            source_url=v["source_url"],
            covers=v["covers"],
            offline=v["tripbox_auth"] in OFFLINE_BY_DEFAULT
            if v["offline"] is None
            else v["offline"],
            data_dir=v["data_dir"],
            archive_dir=v["archive_dir"],
            logs_dir=v["logs_dir"],
            config_dir=v["config_dir"],
            backups_dir=v["backups_dir"],
            problems=tuple(sorted((p for f in features for p in f.problems), key=_rank))
            + _unknown_names(e),
            features=features,
            travellers=travellers,
            link_base=v["base_url"] if sound else None,
        )
