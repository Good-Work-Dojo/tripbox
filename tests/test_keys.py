# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Booking keys (`validate.keys`): a real key is made only of the parts a booking really has, and
a booking without them carries a draft key named after its email and its place in it. The
bookings are the corpus's own, validated the way a fresh parse is."""

import hashlib
import itertools
import re
import string

from helpers.sample import TRAVELLERS

from tripbox import corpus
from tripbox.parse.models import Extraction
from tripbox.validate.keys import draft_key, is_draft, real_key, same_leg
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import parse_local, validate_extraction

AIRPORTS = Airports()
BOOK = PassengerBook.from_yaml(TRAVELLERS)
WHOLE = ("passengers", "segments", "seats")
IATA = re.compile(r"[A-Z]{3}")


def readings(value) -> list:
    """A truth value is one reading or a list of acceptable ones."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def plain(res: dict) -> dict:
    """A truth reservation with its first reading chosen for every field that lists several, and
    its first leg alone."""
    out = {k: (v if k in WHOLE else readings(v)[0] if v else v) for k, v in res.items()}
    out["segments"] = [
        {k: (v if k in WHOLE else readings(v)[0] if v else v) for k, v in leg.items()}
        for leg in (res.get("segments") or [])[:1]
    ]
    return out


def stored(entry: corpus.Entry, res: dict) -> dict:
    """The booking's payload as the validator stores it."""
    ex = Extraction.model_validate({"document_type": "booking", "reservations": [plain(res)]})
    (item,) = validate_extraction(
        ex,
        refdata=AIRPORTS,
        book=BOOK,
        email_date=corpus.sent(entry),
        source={"message_id": "<keys>"},
    )
    return item.payload


def filed_keys(entry: corpus.Entry, kind: str) -> set[str]:
    return {i["key"] for i in entry.outcome["items"] if i["kind"] == kind}


def bookings() -> dict[str, tuple[corpus.Entry, dict]]:
    """The first booking of each kind that the corpus files, with its entry."""
    out: dict[str, tuple[corpus.Entry, dict]] = {}
    for entry in corpus.load(corpus.default_dir()).entries:
        for res in entry.truth["reservations"]:
            kind = res["kind"]
            if kind not in out and any(
                i["kind"] == kind and i["result"] == "filed" for i in entry.outcome["items"]
            ):
                out[kind] = (entry, stored(entry, res))
    assert set(out) == {"flight", "lodging", "car", "train"}
    return out


def key(kind: str, payload: dict) -> str | None:
    return real_key(kind, payload, airports=AIRPORTS)


def without(payload: dict, *fields: str) -> dict:
    return {**payload, **dict.fromkeys(fields)}


def norm(text: str) -> str:
    """Names, places and stations in a key: lower-case letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def test_a_real_key_needs_every_part_it_is_made_of():
    found = bookings()
    for kind, (entry, payload) in found.items():
        assert key(kind, payload) in filed_keys(entry, kind), kind

    _, flight = found["flight"]
    for part in ("carrier", "flight_number", "dep_local", "origin"):
        assert key("flight", without(flight, part)) is None, part
    assert key("flight", {**flight, "dep_local": "no day"}) is None

    _, stay = found["lodging"]
    assert stay["confirmation_code"]
    coded = key("lodging", stay)
    assert key("lodging", without(stay, "property_name", "check_in", "check_out")) == coded
    uncoded = without(stay, "confirmation_code")
    day = stay["check_in"][:10]
    assert key("lodging", uncoded) == f"lodging|{norm(stay['property_name'])}|{day}"
    for part in ("property_name", "check_in"):
        assert key("lodging", without(uncoded, part)) is None, part
    assert key("lodging", {**uncoded, "check_in": "no day"}) is None

    _, car = found["car"]
    assert car["confirmation_code"]
    coded = key("car", car)
    assert key("car", without(car, "provider", "pickup_time", "dropoff_time")) == coded
    uncoded = without(car, "confirmation_code")
    day, place = car["pickup_time"][:10], norm(car["pickup_location"])
    assert key("car", uncoded) == f"car|{norm(car['provider'])}|{day}|{place}"
    for part in ("provider", "pickup_time"):
        assert key("car", without(uncoded, part)) is None, part
    assert key("car", {**uncoded, "pickup_time": "no day"}) is None

    _, train = found["train"]
    # the train's confirmation code is printed: it keys nothing without a number or an operator
    assert train["confirmation_code"]
    day, station = train["dep_local"][:10], norm(train["origin"])
    by_operator = key("train", without(train, "train_number"))
    assert by_operator == f"train|{norm(train['provider'])}|{day}|{station}"
    for parts in (("train_number", "provider"), ("origin",), ("dep_local",)):
        assert key("train", without(train, *parts)) is None, parts
    assert key("train", {**train, "dep_local": "no day"}) is None


def test_a_key_takes_the_day_a_printed_date_reads_as():
    """However the day is printed, the key holds it as YYYY-MM-DD, so one booking read once as
    ISO and once as printed has one key."""
    for printed in ("2031-03-13", "2031-03-13T15:00", "Mar 13, 2031", "Thu Mar 13 2031 9:40 AM"):
        stay = {"property_name": "Example Inn", "check_in": printed}
        assert key("lodging", stay) == "lodging|exampleinn|2031-03-13", printed
        car = {"provider": "Example Cars", "pickup_time": printed, "pickup_location": "BOS"}
        assert key("car", car) == "car|examplecars|2031-03-13|bos", printed
        train = {"train_number": "9031", "origin": "Example Station", "dep_local": printed}
        assert key("train", train) == "train|9031|2031-03-13|examplestation", printed
        flight = {"carrier": "DL", "flight_number": "1234", "origin": "BOS", "dep_local": printed}
        assert key("flight", flight) == "DL|1234|2031-03-13|BOS", printed


def legs_from_a_city():
    """Each flight leg of the corpus whose origin reads as a city as well as a code: its entry,
    its reservation, the leg, the code and the city."""
    for entry in corpus.load(corpus.default_dir()).entries:
        for res in entry.truth["reservations"]:
            if res["kind"] != "flight":
                continue
            for leg in res.get("segments") or []:
                origins = readings(leg.get("origin"))
                codes = [o for o in origins if IATA.fullmatch(o)]
                cities = [o for o in origins if not IATA.fullmatch(o)]
                if codes and cities:
                    yield entry, res, leg, codes[0], cities[0]


def test_a_city_name_or_an_unknown_code_is_no_origin():
    entry, res, leg, code, city = next(legs_from_a_city())
    payload = stored(entry, {**res, "segments": [{**leg, "origin": code}]})
    assert key("flight", payload) in filed_keys(entry, "flight")
    assert key("flight", {**payload, "origin": city}) is None
    unknown = next(
        code
        for code in map("".join, itertools.product(string.ascii_uppercase[::-1], repeat=3))
        if AIRPORTS.airport(code) is None
    )
    assert key("flight", {**payload, "origin": unknown}) is None


def test_a_draft_key_is_stable_for_the_same_email_and_place():
    entries = corpus.load(corpus.default_dir()).entries
    one, other = (hashlib.sha256(e.raw).hexdigest() for e in entries[:2])
    assert one != other
    draft = draft_key(one, 0)
    assert draft == draft_key(one, 0)
    assert draft != draft_key(other, 0) and draft != draft_key(one, 1)
    assert re.fullmatch(r"draft\|[0-9a-f]{16}\|0", draft) and draft.split("|")[1] == one[:16]
    assert is_draft(draft) and is_draft(draft_key(other, 3))
    real: dict[str, str] = {}
    for e in entries:
        for i in e.outcome["items"]:
            if i["result"] == "filed":
                real.setdefault(i["kind"], i["key"])
    assert set(real) == {"flight", "lodging", "car", "train"}
    assert not any(is_draft(k) for k in real.values())


def flight_payloads() -> list[dict]:
    """Every flight leg of the corpus that prints a carrier, a number and a day, as stored."""
    out = []
    for entry in corpus.load(corpus.default_dir()).entries:
        for res in entry.truth["reservations"]:
            if res["kind"] != "flight":
                continue
            for leg in res.get("segments") or []:
                payload = stored(entry, {**res, "segments": [leg]})
                if payload["carrier"] and payload["flight_number"] and payload["dep_local"]:
                    out.append(payload)
    return out


def test_two_legs_are_the_same_only_when_carrier_number_day_and_origin_agree():
    flights = flight_payloads()
    one = flights[0]
    city = next(legs_from_a_city())[-1]
    assert same_leg(one, dict(one))
    # the day is what a key takes of the departure, and the origin is reduced as a key reduces
    # a place: another time that day, or the city spelled otherwise, is the same leg
    assert same_leg(one, {**one, "dep_local": one["dep_local"][:10]})
    respelled = city.title().replace("/", " ")
    assert same_leg({**one, "origin": city}, {**one, "origin": respelled})

    def differs(field, value):
        return next(p for p in flights if value(p) != value(one))[field]

    for field, value in (
        ("carrier", lambda p: p["carrier"]),
        ("flight_number", lambda p: p["flight_number"]),
        ("dep_local", lambda p: p["dep_local"][:10]),
        ("origin", lambda p: p["origin"]),
    ):
        assert not same_leg(one, {**one, field: differs(field, value)}), field
    for field in ("carrier", "flight_number", "dep_local"):
        gone = {**one, field: None}
        assert not same_leg(gone, dict(gone)), field
        assert not same_leg(one, gone) and not same_leg(gone, one), field
    unread = {**one, "dep_local": "no day"}
    assert not same_leg(unread, dict(unread))
    # an origin is part of what a leg says: two legs that print none, or none that a key keeps,
    # are not the same leg (two legs of one through-flight share carrier, number and day)
    for origin in (None, "", "-/-"):
        nowhere = {**one, "origin": origin}
        assert not same_leg(nowhere, dict(nowhere)), origin
    # the day is compared as a date: the same day printed otherwise is the same leg, and a printed
    # day whose first ten characters agree but whose year differs is not
    leaves = parse_local(one["dep_local"])
    printed = {**one, "dep_local": leaves.strftime("%b %d, %Y")}
    assert same_leg(one, printed)
    next_year = {**one, "dep_local": leaves.replace(year=leaves.year + 1).strftime("%b %d, %Y")}
    assert printed["dep_local"][:10] == next_year["dep_local"][:10]
    assert not same_leg(printed, next_year)
