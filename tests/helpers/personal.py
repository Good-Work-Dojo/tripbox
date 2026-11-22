# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What must not survive a report of a corpus email: the values its truth names (who travels,
under which code, to which address) and the Sample family's own constants, typed here once from
the corpus's `FAMILY.md`. Every value is invented."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from tripbox import corpus
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.pipeline.run import build_context, process_raw

# The family's four addresses of mail.
MAIL = ("alex@example.com", "alex.sample@example.org", "trips@example.com", "pat@example.net")
# Its phone number, as printed and as digits.
PHONE = ("612-555-0142", "6125550142")
# One loyalty number per person, the same in every programme.
LOYALTY = ("9000000001", "9000000002", "9000000003", "9000000004")
# The first line of its home address.
HOME = "100 Sample Lane"
# What every ticket number of the corpus starts with.
TICKET_STEM = "00012345"


def fold(text: str) -> str:
    """One spelling of a text: NFKC, case folded, white space collapsed."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def bare(text: str) -> str:
    """The letters and digits of a text alone, folded."""
    return "".join(ch for ch in fold(text) if ch.isalnum())


def _strings(value) -> list[str]:
    """A truth value's strings: itself, or each of its acceptable readings."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def personal(entry: corpus.Entry) -> dict[str, tuple[str, ...]]:
    """The values of one corpus entry that must not survive its report, by what they are: from
    its truth, every passenger (`names`) and every part of one of three or more letters
    (`parts`), every confirmation code (`references`) and every address (`addresses`: an
    `address`, and a pick-up or drop-off place that holds a digit); and the family's constants
    (`mail`, `phone`, `numbers`, `home`)."""
    names: list[str] = []
    references: list[str] = []
    addresses: list[str] = []
    for res in entry.truth["reservations"]:
        for holder in (res, *res.get("segments", [])):
            names += _strings(holder.get("passengers"))
        references += _strings(res.get("confirmation_code"))
        addresses += _strings(res.get("address"))
        for key in ("pickup_location", "dropoff_location"):
            addresses += [p for p in _strings(res.get(key)) if any(ch.isdigit() for ch in p)]
    parts = {w for name in names for w in re.findall(r"[^\W\d_]+", name) if len(w) >= 3}
    return {
        "names": tuple(dict.fromkeys(names)),
        "parts": tuple(sorted(parts)),
        "references": tuple(dict.fromkeys(references)),
        "addresses": tuple(dict.fromkeys(addresses)),
        "mail": MAIL,
        "phone": PHONE,
        "numbers": (*LOYALTY, TICKET_STEM),
        "home": (HOME,),
    }


def survivors(values: dict[str, tuple[str, ...]], report: str) -> list[tuple[str, str]]:
    """Which of `values` a report still holds, as (what it is, the value). A value survives
    when its folded form stands anywhere in the folded report, or its letters and digits alone
    anywhere in the report's; a part of a name survives when it stands there as a word (`Sam`
    is no part of `same`: the redactor replaces words, never the inside of one)."""
    folded, letters = fold(report), bare(report)
    out = []
    for what, held in values.items():
        for value in held:
            if what == "parts":
                word = rf"(?<![^\W\d_]){re.escape(fold(value))}(?![^\W\d_])"
                if re.search(word, folded):
                    out.append((what, value))
            elif fold(value) in folded or bare(value) in letters:
                out.append((what, value))
    return out


def file_corpus(
    c: corpus.Corpus, s: Settings, names: tuple[str, ...] | None = None, sent=None
) -> dict[str, int]:
    """The corpus's entries (those of `names`, else all) filed in order into the ledger of `s`,
    as `tripbox eval --recorded` files them: the corpus's own answers stand in for the model.
    `sent` makes of each email's bytes the bytes that arrive (a mailbox's own copy, say). The
    number of each entry's message, by the entry's name."""
    filed: dict[str, int] = {}
    with corpus.process_as(s):
        Path(s.data_dir).mkdir(parents=True, exist_ok=True)
        with Ledger.open(s.ledger_path) as ledger:
            ctx = build_context(s, ledger=ledger, with_model=False, with_notifier=False)
            ctx.model = corpus.CorpusAnswers(c, s.llm_model)
            for entry in c.entries:
                if names is not None and entry.name not in names:
                    continue
                raw = entry.raw if sent is None else sent(entry.raw)
                res = process_raw(
                    ctx, raw, source="eval", received=corpus.sent(entry), notify=False
                )
                filed[entry.name] = res.message_row_id
    return filed
