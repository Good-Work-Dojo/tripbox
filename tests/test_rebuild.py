# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Rebuilding a booking from its stored fields validates it as a fresh parse would."""

import copy
import datetime as dt

import pytest
from helpers.sample import TRAVELLERS

from tripbox import corpus
from tripbox.parse.models import Extraction
from tripbox.validate.keys import draft_key, is_draft
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rebuild import NothingToFile, rebuild_item
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import validate_extraction

BOOK = PassengerBook.from_yaml(TRAVELLERS)
REFDATA = Airports()
WHOLE = ("passengers", "segments", "seats")


def first(readings):
    return readings[0] if isinstance(readings, list) else readings


def plain(res: dict) -> dict:
    """A truth reservation with one reading chosen for every field that lists several."""
    out = {k: (v if k in WHOLE else first(v)) for k, v in res.items() if k != "segments"}
    out["segments"] = [
        {k: (v if k in WHOLE else first(v)) for k, v in leg.items()}
        for leg in res.get("segments") or []
    ]
    return out


def truth_reservation(kind: str) -> tuple[corpus.Entry, dict]:
    for entry in corpus.load(corpus.default_dir()).entries:
        for res in entry.truth["reservations"]:
            if res["kind"] == kind:
                return entry, plain(res)
    raise AssertionError(f"the corpus holds no {kind}")


def validated(entry: corpus.Entry, res: dict, **kw):
    """One booking validated the way a fresh parse is, its first leg alone."""
    res = copy.deepcopy(res)
    res["segments"] = res["segments"][:1]
    when = corpus.sent(entry)
    source = {"message_id": "<m1>", "email_date": when.isoformat(), "subject": "s", "from": "f"}
    ex = Extraction.model_validate({"document_type": "booking", "reservations": [res]})
    items = validate_extraction(
        ex, refdata=REFDATA, book=BOOK, email_date=when, source=source, **kw
    )
    return items[0]


def rebuilt(item):
    return rebuild_item(item.kind, item.payload, refdata=REFDATA, book=BOOK)


def codes(item):
    return [(p.code, p.detail) for p in item.problems]


def test_a_rebuilt_booking_equals_the_one_first_validated():
    for kind in ("flight", "lodging", "car", "train"):
        entry, res = truth_reservation(kind)
        item = validated(entry, res)
        again = rebuilt(item)
        assert again.kind == kind
        assert again.key == item.key
        assert again.payload == item.payload
        assert codes(again) == codes(item)


def test_a_changed_field_is_validated_like_a_fresh_parse():
    entry, res = truth_reservation("flight")
    item = validated(entry, res)
    old = dt.datetime.fromisoformat(item.payload["dep_local"])
    moved_dep = (old + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    fresh_res = copy.deepcopy(res)
    fresh_res["segments"][0]["departure_local"] = moved_dep
    fresh = validated(entry, fresh_res)

    again = rebuild_item(
        "flight",
        {**item.payload, "dep_local": moved_dep},
        refdata=REFDATA,
        book=BOOK,
    )
    assert again.payload["dep_utc"] != item.payload["dep_utc"]
    assert again.payload["date_local"] == moved_dep[:10]
    assert moved_dep[:10] in again.key
    assert (again.payload["dep_utc"], again.payload["date_local"], again.key) == (
        fresh.payload["dep_utc"],
        fresh.payload["date_local"],
        fresh.key,
    )

    leg = res["segments"][0]
    city_entry = corpus.load(corpus.default_dir()).entry("06-city-names-receipt")
    printed = city_entry.truth["reservations"][0]["segments"][0]["origin"]
    city = printed[-1]
    assert city != leg["origin"]
    fresh_res = copy.deepcopy(res)
    fresh_res["segments"][0]["origin"] = city
    # a stored booking keeps what was printed beside its times, so it reads them as the fresh
    # parse of the same email does
    fresh = validated(entry, fresh_res)
    again = rebuild_item(
        "flight", {**item.payload, "origin": city}, refdata=REFDATA, book=BOOK
    )
    assert any(code == "origin" for code, _ in codes(again))
    assert codes(again) == codes(fresh)


def test_a_booking_that_cannot_be_keyed_keeps_the_key_it_is_filed_under():
    entry, res = truth_reservation("flight")
    item = validated(entry, res)
    digest = item.payload["message_hash"]
    undated = {**item.payload, "dep_local": None, "arr_local": None}
    held = draft_key(digest, 3)
    kept = rebuild_item("flight", undated, refdata=REFDATA, book=BOOK, key=held)
    assert kept.key == held
    alone = rebuild_item("flight", undated, refdata=REFDATA, book=BOOK)
    assert alone.key == draft_key(digest, 0)
    # once its fields make a real key, that key wins over the one it was held under
    whole = rebuild_item(
        "flight", item.payload, refdata=REFDATA, book=BOOK, key=held
    )
    assert whole.key == item.key and not is_draft(whole.key)


def test_fields_the_validator_files_nothing_from_are_nothing_to_file(monkeypatch):
    entry, res = truth_reservation("flight")
    item = validated(entry, res)
    monkeypatch.setattr("tripbox.validate.rebuild.validate_extraction", lambda *a, **k: [])
    with pytest.raises(NothingToFile, match="nothing left to write after re-validation"):
        rebuilt(item)
