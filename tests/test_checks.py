# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Checks (`validate.checks`): which doubts about a filed booking are hard and which are soft,
which of them are open, what a flight log should hold for a flight and where its sync stands. The
bookings are the corpus's own, validated the way a fresh parse is, each spoiled by one value read
from the corpus (a field blanked, a code replaced by the corpus's own city for that airport, a
confidence lowered, a day moved by a computed number of days)."""

import ast
import copy
import dataclasses
import datetime as dt
import functools
import itertools
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tripbox import corpus
from tripbox.parse.models import Extraction
from tripbox.validate import checks, rules
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.rebuild import rebuild_item
from tripbox.validate.refdata import Airports

AIRPORTS = Airports()
WHOLE = ("passengers", "segments", "seats")
IATA = re.compile(r"[A-Z]{3}")
FLOOR = 0.8  # the model's confidence floor these tests set

HARD = (
    "carrier",
    "flight_number",
    "origin",
    "destination",
    "departure",
    "arrival_before_departure",
    "check_in",
    "check_out",
    "pickup_time",
    "dropoff_time",
    "stations",
    "airtrail_airport",
)
SOFT = (
    "low_confidence",
    "unverified_sender",
    "owner_unknown",
    "date_past",
    "date_far",
    "long_leg",
    "missing_name",
    "unknown_cancellation",
    "airtrail_refused",
    "airtrail_still_lists",
)
STILL_LISTS = "airtrail_still_lists"


@functools.cache
def the_corpus() -> corpus.Corpus:
    return corpus.load(corpus.default_dir())


@functools.cache
def book() -> PassengerBook:
    """The corpus's own travellers."""
    return PassengerBook.from_yaml(the_corpus().root / corpus.TRAVELLERS)


def first(value):
    """A truth value is one reading or a list of acceptable ones: the first."""
    return value[0] if isinstance(value, list) and value else value


def plain(res: dict, leg: int = 0) -> dict:
    """A truth reservation with the first reading of every field that lists several, and its leg
    at place `leg` alone."""
    out = {k: (v if k in WHOLE else first(v)) for k, v in res.items() if k != "segments"}
    legs = res.get("segments") or []
    out["segments"] = (
        [{k: (v if k in WHOLE else first(v)) for k, v in legs[leg].items()}] if legs else []
    )
    return out


def names(res: dict) -> list[str]:
    legs = res.get("segments") or [{}]
    return list(legs[0].get("passengers") or res.get("passengers") or [])


def validated(entry: corpus.Entry, res: dict, *, confidence: float = 1.0, **kw) -> rules.Item:
    """One booking validated the way a fresh parse of its email is."""
    sent = corpus.sent(entry)
    ex = Extraction.model_validate(
        {"document_type": "booking", "reservations": [res], "confidence": confidence}
    )
    source = {"message_id": "<checks>", "email_date": sent.isoformat()}
    (item,) = rules.validate_extraction(
        ex, refdata=AIRPORTS, book=book(), email_date=sent, source=source, **kw
    )
    return item


def rebuilt(kind: str, payload: dict) -> rules.Item:
    """The booking rebuilt from its stored fields as they stand."""
    return rebuild_item(kind, payload, refdata=AIRPORTS, book=book())


def bookings(kind: str) -> Iterator[tuple[corpus.Entry, dict]]:
    """Each booking of `kind` from an email the corpus files it from, one leg at a time."""
    for entry in the_corpus().entries:
        if not any(i["kind"] == kind and i["result"] == "filed" for i in entry.outcome["items"]):
            continue
        for res in entry.truth["reservations"]:
            if res["kind"] == kind:
                for n in range(len(res.get("segments") or []) or 1):
                    yield entry, plain(res, n)


def booking(kind: str, test: Callable[[dict], bool] = lambda res: True):
    """The first booking of `kind` that validates with no finding and passes `test`."""
    for entry, res in bookings(kind):
        if test(res) and not checks.codes(validated(entry, res)):
            return entry, res
    raise AssertionError(f"the corpus files no such {kind}")


def owners(res: dict) -> bool:
    return book().classify(names(res)).me_present


def a_companion_alone(res: dict) -> bool:
    found = book().classify(names(res))
    return not found.me_present and bool(found.guests) and not found.others


def strangers_alone(res: dict) -> bool:
    found = book().classify(names(res))
    return not found.me_present and not found.guests and bool(found.others)


def cities() -> dict[str, str]:
    """Each airport code that the corpus's truth also reads as a city, with that city."""
    out: dict[str, str] = {}
    for entry in the_corpus().entries:
        for res in entry.truth["reservations"]:
            if res["kind"] != "flight":
                continue
            for leg in res.get("segments") or []:
                for end in ("origin", "destination"):
                    value = leg.get(end)
                    readings = value if isinstance(value, list) else [value]
                    codes = [r for r in readings if r and IATA.fullmatch(r)]
                    places = [r for r in readings if r and not IATA.fullmatch(r)]
                    if codes and places:
                        out.setdefault(codes[0], places[0])
    return out


def edit(res: dict, booking: dict | None = None, **leg) -> dict:
    """A copy of `res` with `booking`'s fields, and `leg`'s fields of its leg, replaced."""
    out = copy.deepcopy(res)
    out.update(booking or {})
    if leg:
        out["segments"][0].update(leg)
    return out


def day(value: str) -> dt.date:
    return rules.parse_local(value).date()


def moved(value: str, days: int) -> str:
    """The printed time `value` moved by `days` days, written as the extractors write one."""
    when = rules.parse_local(value) + dt.timedelta(days=days)
    return when.strftime("%Y-%m-%d" if len(value.strip()) == 10 else "%Y-%m-%dT%H:%M")


def stamps(entry: corpus.Entry, n: int = 3) -> list[str]:
    """`n` times in UTC, an hour apart, from an hour after the entry's email was sent."""
    sent = corpus.sent(entry).astimezone(dt.UTC)
    return [(sent + dt.timedelta(hours=h)).isoformat(timespec="seconds") for h in range(1, n + 1)]


def opened(item: rules.Item, payload: dict, *, in_reconcile: bool = True, **kw) -> tuple:
    who = checks.owner(item.kind, item.passengers, payload, book())
    return checks.open_checks(item, payload, owner=who, in_reconcile=in_reconcile, **kw)


def test_every_code_is_hard_or_soft_and_has_a_sentence():
    assert checks.HARD == HARD and checks.SOFT == SOFT
    assert not set(checks.HARD) & set(checks.SOFT)
    assert checks.ALL == HARD + SOFT
    assert checks.SYNC_STATES == ("off", "blocked", "pending", "synced")
    assert checks.FROM_THE_BOOKING == (
        "owner_unknown",
        "date_past",
        "date_far",
        "long_leg",
        "missing_name",
    )
    assert checks.FROM_AN_EMAIL == ("low_confidence", "unverified_sender", "unknown_cancellation")
    assert set(checks.FROM_THE_BOOKING) | set(checks.FROM_AN_EMAIL) < set(checks.SOFT)
    assert set(checks.LABELS) == set(checks.ALL) and len(checks.LABELS) == len(checks.ALL)
    for code, sentence in checks.LABELS.items():
        assert sentence.strip() and sentence.endswith("."), code
        # one sentence; the flight log's own mark adds what to do about it
        assert sentence.count(". ") == (1 if code == STILL_LISTS else 0), code
        for word in ("airtrail", "telegram", "tripbox"):
            assert word not in sentence.lower(), (code, word)


def test_the_validators_findings_are_the_codes():
    """One invalid input for each reason the validator finds a booking doubtful, numbered by
    reason, and exactly the codes it raises; a flight naming only other people raises none."""
    place = cities()
    entry, flight = booking("flight", owners)
    leg = flight["segments"][0]
    dep, arr = leg["departure_local"], leg["arrival_local"]
    sent = corpus.sent(entry).date()
    a_from, origin_flight = booking(
        "flight",
        lambda r: owners(r)
        and r["segments"][0]["origin"] in place
        and bool(r["segments"][0].get("departure_offset")),
    )
    city = place[origin_flight["segments"][0]["origin"]]
    a_to, destination_flight = booking(
        "flight", lambda r: owners(r) and r["segments"][0]["destination"] in place
    )
    town = place[destination_flight["segments"][0]["destination"]]
    a_stay, stay = booking("lodging")
    a_car, car = booking("car")
    a_train, train = booking("train")
    a_stranger, strangers = booking("flight", strangers_alone)
    past = (sent - day(dep)).days - rules.PAST_SLACK.days - 7
    far = (sent - day(dep)).days + rules.FUTURE_LIMIT.days + 7
    early = (day(dep) - day(arr)).days - 2
    out_before_in = (day(stay["check_in"]) - day(stay["check_out"])).days - 1
    back_before_out = (day(car["pickup_time"]) - day(car["dropoff_time"])).days - 1
    nobody = {"passengers": []}
    haiku = {"parser": "haiku", "min_confidence": FLOOR}

    def shifted(days: int) -> dict:
        return edit(flight, departure_local=moved(dep, days), arrival_local=moved(arr, days))

    cases = [
        (1, entry, edit(flight, {"provider": None}, carrier_iata=None, operator=None), {}),
        (2, entry, edit(flight, flight_number=None), {}),
        (3, a_from, edit(origin_flight, origin=city), {}),
        (4, a_to, edit(destination_flight, destination=town), {}),
        (5, entry, edit(flight, departure_local=None), {}),
        (6, entry, shifted(past), {}),
        (7, entry, shifted(far), {}),
        (8, entry, edit(flight, arrival_local=moved(arr, early)), {}),
        (9, entry, edit(flight, arrival_local=moved(arr, 1)), {}),
        (10, entry, edit(flight, nobody, passengers=[]), {}),
        (11, a_stranger, strangers, {}),
        (12, a_stay, edit(stay, {"property_name": None}), {}),
        (13, a_stay, edit(stay, {"check_in": None}), {}),
        (14, a_stay, edit(stay, {"check_out": None}), {}),
        (14, a_stay, edit(stay, {"check_out": moved(stay["check_out"], out_before_in)}), {}),
        (15, a_car, edit(car, {"provider": None}), {}),
        (16, a_car, edit(car, {"pickup_time": None}), {}),
        (17, a_car, edit(car, {"dropoff_time": None}), {}),
        (17, a_car, edit(car, {"dropoff_time": moved(car["dropoff_time"], back_before_out)}), {}),
        (18, a_train, edit(train, destination=None), {}),
        (19, a_train, edit(train, departure_local=None), {}),
        (20, entry, flight, {**haiku, "confidence": FLOOR / 2}),
        (21, entry, flight, {"trusted_sender": False}),
    ]
    expected = {
        1: ("carrier",),
        2: ("flight_number",),
        3: ("origin",),
        4: ("destination",),
        5: ("departure",),
        6: ("date_past",),
        7: ("date_far",),
        8: ("arrival_before_departure",),
        9: ("long_leg",),
        10: ("owner_unknown",),
        11: (),
        12: ("missing_name",),
        13: ("check_in",),
        14: ("check_out",),
        15: ("missing_name",),
        16: ("pickup_time",),
        17: ("dropoff_time",),
        18: ("stations",),
        19: ("departure",),
        20: ("low_confidence",),
        21: ("unverified_sender",),
    }
    assert sorted({row for row, *_ in cases}) == sorted(expected) == list(range(1, 22))
    wrong = []
    for row, at, res, kw in cases:
        got = checks.codes(validated(at, res, **kw))
        if got != expected[row]:
            wrong.append((row, got, expected[row]))
    # an origin that is no airport, in an email that prints no zone, leaves the departure
    # without one too
    zoneless = edit(origin_flight, origin=city, departure_offset=None, departure_tz=None)
    got = checks.codes(validated(a_from, zoneless))
    if got != ("origin", "departure"):
        wrong.append((3, got, ("origin", "departure")))
    assert wrong == []
    # each booking was right before it was spoiled, the model's answer at full confidence too
    for at, res in ((entry, flight), (a_from, origin_flight), (a_to, destination_flight)):
        assert checks.codes(validated(at, res, **haiku)) == ()


def test_codes_are_each_once_in_order_and_an_unknown_one_fails():
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)

    def given(*found: str) -> rules.Item:
        return dataclasses.replace(item, problems=[rules.Problem(code, "") for code in found])

    assert checks.codes(given(*reversed(checks.ALL), *checks.ALL)) == checks.ALL
    some = given("long_leg", "origin", "long_leg", "carrier", "origin")
    assert checks.codes(some) == ("carrier", "origin", "long_leg")
    with pytest.raises(ValueError, match="not_a_check"):
        checks.codes(given("origin", "not_a_check"))
    with pytest.raises(ValueError, match="not_a_check"):
        checks.open_checks(given("not_a_check"), item.payload, owner="on", in_reconcile=True)


def codes_in_rules() -> tuple[set[str], list[str]]:
    """Every code `validate/rules.py` can give a Problem, read from its source, and the source of
    each Problem whose code cannot be read there. A code is read when it is a string literal, a
    loop variable unpacked from a literal tuple of literal tuples, or a parameter of the function
    (never assigned in it) that every call of that function in the module passes as a string
    literal."""
    tree = ast.parse(Path(rules.__file__).read_text(encoding="utf-8"))
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def text(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def around(node: ast.AST, kind: type) -> Iterator:
        while node in parent:
            node = parent[node]
            if isinstance(node, kind):
                yield node

    def from_a_loop(name: str, at: ast.AST) -> set[str] | None:
        for loop in around(at, ast.For):
            targets = loop.target.elts if isinstance(loop.target, ast.Tuple) else []
            names = [t.id if isinstance(t, ast.Name) else None for t in targets]
            if name not in names:
                continue
            if not isinstance(loop.iter, ast.Tuple | ast.List):
                return None
            n = names.index(name)
            values = {
                text(row.elts[n]) if isinstance(row, ast.Tuple) else None for row in loop.iter.elts
            }
            return None if None in values else values
        return None

    def from_a_parameter(name: str, at: ast.AST) -> set[str] | None:
        func = next(around(at, ast.FunctionDef), None)
        if func is None:
            return None
        params = [a.arg for a in (*func.args.posonlyargs, *func.args.args)]
        stored = [
            n for n in ast.walk(func) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        ]
        if name not in params or any(n.id == name for n in stored):
            return None
        n = params.index(name)
        calls = [
            c
            for c in ast.walk(tree)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == func.name
        ]
        values = set()
        for call in calls:
            given = call.args[n] if n < len(call.args) else None
            given = given or next((k.value for k in call.keywords if k.arg == name), None)
            values.add(text(given))
        return None if not calls or None in values else values

    found: set[str] = set()
    unread: list[str] = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "Problem"):
            continue
        code = call.args[0] if call.args else None
        code = code or next((k.value for k in call.keywords if k.arg == "code"), None)
        values = {text(code)} if text(code) is not None else None
        if values is None and isinstance(code, ast.Name):
            values = from_a_loop(code.id, call) or from_a_parameter(code.id, call)
        if values is None:
            unread.append(ast.unparse(call))
        else:
            found |= values
    return found, unread


def test_every_code_the_validator_can_raise_is_a_check():
    """Read from the validator's source, so a code added there that is no check fails here."""
    found, unread = codes_in_rules()
    assert unread == []
    assert found <= set(checks.ALL), sorted(found - set(checks.ALL))
    # the other four are raised by the planner, by the sync with a flight log and by settle
    elsewhere = {"unknown_cancellation", "airtrail_airport", "airtrail_refused", STILL_LISTS}
    assert set(checks.ALL) - found == elsewhere


def test_a_hard_check_is_open_until_the_field_is_right():
    place = cities()
    entry, flight = booking("flight", lambda r: owners(r) and r["segments"][0]["origin"] in place)
    code = flight["segments"][0]["origin"]
    spoiled = validated(entry, edit(flight, origin=place[code])).payload
    stands = rebuilt("flight", spoiled)
    assert "origin" in opened(stands, spoiled)
    assert "origin" in opened(stands, spoiled, in_reconcile=False)
    (later,) = stamps(entry, 1)
    checks.looked(spoiled, later)
    assert spoiled["looked_at"] == later
    assert "origin" in opened(stands, spoiled)
    fixed = {**spoiled, "origin": code}
    right = rebuilt("flight", fixed)
    assert opened(right, fixed) == ()
    # an airport the flight log does not know is open, looked at or not, until the sync succeeds
    unknown = {"sync_error": "airport", "sync_error_at": later}
    assert opened(right, fixed, **unknown) == ("airtrail_airport",)
    assert opened(right, fixed, sync_error=None) == ()


def test_looking_closes_soft_checks_and_only_a_newer_email_reopens_them():
    entry, flight = booking("flight", owners)
    t0, t1, t2, t3 = stamps(entry, 4)
    item = validated(entry, flight)
    p = item.payload
    checks.raise_soft(p, ["low_confidence"], t0)
    assert p["raised"] == {"low_confidence": t0}
    assert opened(item, p) == ("low_confidence",)
    checks.looked(p, t1)
    assert opened(item, p) == ()
    checks.raise_soft(p, ["low_confidence"], t0)  # an older copy, replayed
    assert opened(item, p) == ()
    checks.raise_soft(p, ["low_confidence"], t2)
    assert opened(item, p) == ("low_confidence",)
    # a stamp never moves back, and a hard code is never stamped
    checks.raise_soft(p, ["low_confidence", "origin"], t1)
    assert p["raised"] == {"low_confidence": t2}
    # a look made before the last one (a card older than the page) never moves the look back
    checks.looked(p, t2)
    checks.looked(p, t1)
    assert p["looked_at"] == t2 and opened(item, p) == ()
    # a refusal by the flight log closes on a look, and only a later refusal reopens it
    assert opened(item, p, sync_error="refused", sync_error_at=t1) == ()
    assert opened(item, p, sync_error="refused", sync_error_at=t2) == ()
    assert opened(item, p, sync_error="refused", sync_error_at=t3) == ("airtrail_refused",)
    assert opened(item, p, sync_error="outage", sync_error_at=t3) == ()
    fresh = validated(entry, flight).payload  # never looked at
    assert opened(item, fresh, sync_error="refused", sync_error_at=t0) == ("airtrail_refused",)


def test_a_stamp_that_is_not_a_time_is_refused_where_it_is_written():
    entry, flight = booking("flight", owners)
    (t0,) = stamps(entry, 1)
    item = validated(entry, flight)
    p = item.payload
    checks.raise_soft(p, ["low_confidence"], t0)
    before = copy.deepcopy(p)
    for bad in ("", "not a time", t0[:10] + "T99:99", None):
        with pytest.raises(ValueError):
            checks.raise_soft(p, ["low_confidence"], bad)
        with pytest.raises(ValueError):
            checks.looked(p, bad)
        assert p == before, bad
    assert opened(item, p) == ("low_confidence",)


def test_each_soft_check_is_open_while_raised_and_closes_on_a_look():
    entry, flight = booking("flight", owners)
    t0, t1, t2 = stamps(entry)
    leg = flight["segments"][0]
    dep, arr = leg["departure_local"], leg["arrival_local"]
    past = (corpus.sent(entry).date() - day(dep)).days - rules.PAST_SLACK.days - 7
    a_stay, stay = booking("lodging")
    a_car, car = booking("car")
    from_the_booking = [
        ("long_leg", entry, flight, edit(flight, arrival_local=moved(arr, 1))),
        (
            "date_past",
            entry,
            flight,
            edit(flight, departure_local=moved(dep, past), arrival_local=moved(arr, past)),
        ),
        ("missing_name", a_stay, stay, edit(stay, {"property_name": None})),
        ("missing_name", a_car, car, edit(car, {"provider": None})),
    ]
    for code, at, right, spoiled in from_the_booking:
        kind = right["kind"]
        flies = kind == "flight"
        p = validated(at, spoiled).payload
        checks.raise_soft(p, [code], t0)
        assert opened(rebuilt(kind, p), p, in_reconcile=flies) == (code,), code
        checks.looked(p, t1)
        assert opened(rebuilt(kind, p), p, in_reconcile=flies) == (), code
        checks.raise_soft(p, [code], t2)  # a newer email shows it again
        assert opened(rebuilt(kind, p), p, in_reconcile=flies) == (code,), code
        # the booking corrected: the check goes without a look, though its stamp stays
        fixed = {**validated(at, right).payload, "raised": p["raised"], "looked_at": t1}
        assert opened(rebuilt(kind, fixed), fixed, in_reconcile=flies) == (), code
    # a cancellation for a booking never filed is raised by its email alone
    for at, res, flies in ((entry, flight, True), (entry, flight, False), (a_stay, stay, False)):
        item = validated(at, res)
        p = item.payload
        checks.raise_soft(p, ["unknown_cancellation"], t0)
        assert opened(item, p, in_reconcile=flies) == ("unknown_cancellation",)
        checks.looked(p, t1)
        assert opened(item, p, in_reconcile=flies) == ()
        checks.raise_soft(p, ["unknown_cancellation"], t0)  # an older copy, replayed
        assert opened(item, p, in_reconcile=flies) == ()
        checks.raise_soft(p, ["unknown_cancellation"], t2)
        assert opened(item, p, in_reconcile=flies) == ("unknown_cancellation",)


def test_a_check_from_the_booking_goes_when_the_booking_no_longer_shows_it():
    entry, flight = booking("flight", owners)
    (t0,) = stamps(entry, 1)
    leg = flight["segments"][0]
    shift = (corpus.sent(entry).date() - day(leg["departure_local"])).days
    shift += rules.FUTURE_LIMIT.days + 7
    far = edit(
        flight,
        departure_local=moved(leg["departure_local"], shift),
        arrival_local=moved(leg["arrival_local"], shift),
    )
    item = validated(entry, far)
    p = item.payload
    checks.raise_soft(p, checks.codes(item), t0)
    assert opened(rebuilt("flight", p), p) == ("date_far",)
    right = validated(entry, flight).payload
    p.update(dep_local=right["dep_local"], arr_local=right["arr_local"])
    assert "looked_at" not in p and "date_far" in p["raised"]
    assert opened(rebuilt("flight", p), p) == ()


def test_the_sender_check_follows_the_owner_not_the_email():
    entry, flight = booking("flight", a_companion_alone)
    (t0,) = stamps(entry, 1)
    item = validated(entry, flight, trusted_sender=False)
    # the validator finds it on every flight of the email; whose flight it holds is decided here
    assert checks.codes(item) == ("unverified_sender",)
    p = item.payload
    checks.raise_soft(p, checks.codes(item), t0)
    assert "unverified_sender" not in opened(rebuilt("flight", p), p)
    p["passengers"] = [*p["passengers"], book().me_name]
    owners_leg = rebuilt("flight", p)
    assert checks.owner("flight", owners_leg.passengers, p, book()) == "on"
    assert "unverified_sender" in opened(owners_leg, p)
    # with no flight log the check stays shut, and it opens by itself when one is on
    assert "unverified_sender" not in opened(owners_leg, p, in_reconcile=False)
    assert "unverified_sender" in opened(owners_leg, p)
    stay_entry, stay = booking("lodging", owners)
    item = validated(stay_entry, stay, trusted_sender=False)
    assert checks.codes(item) == ()  # nothing but a flight carries it
    s = item.payload
    checks.raise_soft(s, ["unverified_sender"], t0)
    assert opened(item, s) == ()
    assert checks.open_checks(item, s, owner="on", in_reconcile=True) == ()


def test_the_checks_only_a_flight_log_explains_follow_the_reconcile():
    """Nobody named, an unverified sender, an airport the flight log lacks, a write it refused
    and a flight it still lists as the owner's are open only while the flight is in the
    reconcile; out of it they stay shut whatever the last sync attempt said, and open again by
    themselves when it is back in."""
    entry, flight = booking("flight", owners)
    t0, t1 = stamps(entry, 2)
    mine = validated(entry, flight)
    p = mine.payload
    checks.raise_soft(p, ["unverified_sender"], t0)
    nobody = validated(entry, edit(flight, {"passengers": []}, passengers=[]))
    q = nobody.payload
    checks.raise_soft(q, checks.codes(nobody), t0)
    only_with_a_log = {"owner_unknown", "unverified_sender", "airtrail_airport", "airtrail_refused"}
    only_with_a_log.add(STILL_LISTS)
    told = checks.desired(p, mine.passengers, "confirmed", book())
    left = {**p, "owner_answer": "no", "raised": {**p["raised"], STILL_LISTS: t0}}
    held = ((mine, p), (nobody, q), (mine, left))
    for (item, payload), error, at in itertools.product(
        held, ("airport", "refused", "outage", None), (None, t0, t1)
    ):
        shut = opened(
            item, payload, in_reconcile=False, sync_error=error, sync_error_at=at, told=told
        )
        assert not set(shut) & only_with_a_log, (error, at)
    assert opened(mine, left, told=told) == (STILL_LISTS,)
    refused = {"sync_error": "refused", "sync_error_at": t0}
    unknown = {"sync_error": "airport", "sync_error_at": t0}
    assert opened(mine, p, **unknown) == ("airtrail_airport", "unverified_sender")
    assert opened(mine, p, **refused) == ("unverified_sender", "airtrail_refused")
    assert opened(nobody, q, **unknown) == ("airtrail_airport", "owner_unknown")
    assert opened(nobody, q, **refused) == ("owner_unknown", "airtrail_refused")


def test_nobody_named_is_unknown_until_answered():
    entry, flight = booking("flight", owners)
    t0, t1 = stamps(entry, 2)
    nobody = validated(entry, edit(flight, {"passengers": []}, passengers=[]))
    p = nobody.payload
    checks.raise_soft(p, checks.codes(nobody), t0)
    assert checks.owner("flight", nobody.passengers, p, book()) == "unknown"
    assert opened(nobody, p) == ("owner_unknown",)
    assert opened(nobody, p, in_reconcile=False) == ()
    checks.looked(p, t1)
    assert opened(nobody, p) == ("owner_unknown",)
    assert opened(nobody, p, in_reconcile=False) == ()
    answered = {**p, "owner_answer": "no"}
    assert checks.owner("flight", nobody.passengers, answered, book()) == "off"
    assert opened(nobody, answered) == ()
    named = {**p, "passengers": [book().me_name]}
    mine = rebuilt("flight", named)
    assert checks.owner("flight", mine.passengers, named, book()) == "on"
    assert opened(mine, named) == ()
    # a single word is no traveller, and nobody's flight is a flight
    alone = {**p, "passengers": [book().me_name.split()[0]]}
    assert checks.owner("flight", alone["passengers"], alone, book()) == "unknown"
    assert checks.owner("lodging", [], p, book()) == "off"


def flight_and_what_it_should_hold() -> tuple[rules.Item, dict]:
    entry, flight = booking("flight", owners)
    item = validated(entry, flight)
    return item, checks.desired(item.payload, item.passengers, item.status, book())


def test_the_sync_state_is_off_whenever_the_owner_is_not_on_the_leg():
    _, want = flight_and_what_it_should_hold()
    other = {**want, "departure": None}
    opens = [(), *((code,) for code in checks.ALL), checks.ALL]
    for open_, told, cancelled, on, hold in itertools.product(
        opens, (None, want, other), (False, True), (True, False), (None, "past", "deleted")
    ):
        kw = dict(
            airtrail_on=on, hold=hold, cancelled=cancelled, told=told, open=open_, desired=want
        )
        assert checks.sync_state(kind="flight", owner="off", **kw) == "off", kw
        for kind, who in itertools.product(("lodging", "car", "train"), ("on", "off", "unknown")):
            assert checks.sync_state(kind=kind, owner=who, **kw) == "off", (kind, who, kw)


def test_the_sync_state_table():
    item, want = flight_and_what_it_should_hold()
    gone = checks.desired(item.payload, item.passengers, "cancelled", book())
    base = dict(
        kind="flight",
        airtrail_on=True,
        hold=None,
        owner="on",
        cancelled=False,
        told=None,
        open=(),
        desired=want,
    )

    def state(**kw) -> str:
        return checks.sync_state(**{**base, **kw})

    # 1. not a flight, or no flight log; only True is on (the ledger stores "on" and "off")
    assert state(kind="lodging") == "off" and state(airtrail_on=False, open=("origin",)) == "off"
    for stored in ("off", "on", "", None, 1):
        assert state(airtrail_on=stored, told=want) == "off", stored
    # 2. held out of the reconcile, whatever is open
    assert state(hold="past", open=("origin",)) == "off"
    assert state(hold="deleted", owner="unknown") == "off"
    # 3. a cancelled flight never sent, whatever is open: no answer could send it, so nobody
    #    named asks nothing
    assert state(cancelled=True, desired=gone, open=("low_confidence",)) == "off"
    assert state(owner="unknown", cancelled=True, desired=gone) == "off"
    # 4. nobody named, even with no check open
    assert state(owner="unknown") == "blocked"
    assert state(owner="unknown", cancelled=True, desired=gone, told=want) == "blocked"
    # 5. someone else's flight
    assert state(owner="off", open=("origin",)) == "off"
    # 6. any open check
    assert state(open=("low_confidence",)) == "blocked"
    assert state(open=("airtrail_airport",), told=want) == "blocked"
    # 7. told what it should hold
    assert state(told=want) == "synced"
    # 8. otherwise
    assert state() == "pending"
    assert state(told={**want, "departure": None}) == "pending"
    # a cancelled flight that was sent is pending until the cancellation is told
    assert state(cancelled=True, desired=gone, told=want) == "pending"
    assert state(cancelled=True, desired=gone, told=gone) == "synced"


def test_what_airtrail_should_hold_names_the_owner_and_known_companions_only():
    entry, flight = booking("flight", owners)
    _, theirs = booking("flight", a_companion_alone)
    _, strangers = booking("flight", strangers_alone)
    companion, stranger = names(theirs)[0], names(strangers)[0]
    usual = book().classify([companion]).guests[0]
    printed = [stranger, companion, book().me_name]  # the owner last, as printed
    seats = {name: f"seat {n}" for n, name in enumerate(printed)}
    leg = edit(flight, {"passengers": printed}, passengers=[], seat_by_passenger=seats)
    item = validated(entry, leg)
    p = item.payload
    want = checks.desired(p, item.passengers, item.status, book())
    assert want["people"] == [
        {"who": "me", "seat": seats[book().me_name], "class": p["cabin"]},
        {"who": usual, "seat": seats[companion], "class": p["cabin"]},
    ]
    assert {k: v for k, v in want.items() if k != "people"} == {
        "date": p["date_local"],
        "departure": p["dep_utc"],
        "arrival": p["arr_utc"],
        "flight": p["flight_code"],
        "from": p["origin"],
        "to": p["destination"],
        "airline": p["carrier"],
        "cancelled": False,
    }
    assert json.loads(json.dumps(want)) == want
    assert checks.desired(p, item.passengers, "cancelled", book())["cancelled"] is True
    # every companion the book knows, in name order after the owner, whatever order they print in
    found = book().classify(names(flight))
    assert len(found.guests) > 1
    for order in itertools.permutations(names(flight)):
        everyone = validated(entry, edit(flight, {"passengers": list(order)}, passengers=[]))
        held = checks.desired(everyone.payload, everyone.passengers, everyone.status, book())
        assert [x["who"] for x in held["people"]] == ["me", *sorted(found.guests)], order


def test_what_a_flight_log_should_hold_for_a_flight_printed_as_a_day_alone_has_no_instant():
    """A flight whose email prints its days and no times: the flight log is to hold its day and
    no departure or arrival instant, and nothing is sent while the missing time is marked."""
    entry, flight = booking("flight", owners)
    leg = flight["segments"][0]
    days = {end: day(leg[end]).isoformat() for end in ("departure_local", "arrival_local")}
    item = validated(entry, edit(flight, **days))
    p = item.payload
    want = checks.desired(p, item.passengers, item.status, book())
    assert want["date"] == days["departure_local"]
    assert want["departure"] is None and want["arrival"] is None
    assert want["flight"] == p["flight_code"] and json.loads(json.dumps(want)) == want
    marked = opened(item, p)
    assert marked == ("departure",)
    state = checks.sync_state(
        kind="flight",
        airtrail_on=True,
        hold=None,
        owner=checks.owner("flight", item.passengers, p, book()),
        cancelled=False,
        told=None,
        open=marked,
        desired=want,
    )
    assert state == "blocked"


def test_an_unknown_airport_check_goes_when_the_owner_is_off_the_flight():
    """An airport the flight log does not know and a write it refused are about a flight that is
    to be sent. Once the owner is off the flight nothing is sent and no pass asks again, so
    neither could ever close: both are shut, and open again by themselves with the owner back
    on it. A flight that names nobody may yet be the owner's, and keeps them."""
    entry, flight = booking("flight", owners)
    (t0,) = stamps(entry, 1)
    mine = validated(entry, flight)
    c_entry, theirs = booking("flight", a_companion_alone)
    other = validated(c_entry, theirs)
    nobody = validated(entry, edit(flight, {"passengers": []}, passengers=[]))
    not_me = {**mine.payload, "owner_answer": "no"}
    assert checks.owner("flight", mine.passengers, not_me, book()) == "off"
    for error, code in (("airport", "airtrail_airport"), ("refused", "airtrail_refused")):
        last = {"sync_error": error, "sync_error_at": t0}
        assert opened(mine, mine.payload, **last) == (code,)
        assert opened(mine, not_me, **last) == ()
        assert opened(other, other.payload, **last) == ()
        assert code in opened(nobody, nobody.payload, **last)
        assert opened(mine, {**not_me, "owner_answer": "yes"}, **last) == (code,)


def test_a_flight_the_flight_log_still_lists_as_the_owners_is_marked_until_looked_at():
    """The flight log was told the owner is on a flight, and the owner is on it no more:
    nothing is ever written there, so the booking is marked from when that began until the
    owner looks; with the owner back on it there is nothing to mark."""
    entry, flight = booking("flight", owners)
    t0, t1, t2 = stamps(entry, 3)
    mine = validated(entry, flight)
    told = checks.desired(mine.payload, mine.passengers, "confirmed", book())
    assert "me" in [who["who"] for who in told["people"]]
    companions = {**told, "people": [who for who in told["people"] if who["who"] != "me"]}
    assert checks.LABELS[STILL_LISTS] == (
        "Your flight log still lists this flight as yours. Remove it there if you did not fly."
    )
    assert checks.still_listed("off", told)
    for who, held in (("on", told), ("unknown", told), ("off", None), ("off", companions)):
        assert not checks.still_listed(who, held), (who, held)
    assert not checks.still_listed("off", {}) and not checks.still_listed("off", {"people": 1})
    left = {**mine.payload, "owner_answer": "no", "raised": {STILL_LISTS: t1}}
    assert opened(mine, left, told=told) == (STILL_LISTS,)
    assert opened(mine, left, told=companions) == () and opened(mine, left) == ()
    # the owner on the flight: nothing to mark, whatever is stamped
    assert opened(mine, {**mine.payload, "raised": {STILL_LISTS: t1}}, told=told) == ()
    # a look before it began leaves it open; a look after it closes it
    assert opened(mine, {**left, "looked_at": t0}, told=told) == (STILL_LISTS,)
    assert opened(mine, {**left, "looked_at": t2}, told=told) == ()
    assert checks.actions((STILL_LISTS,), hold=None, flight_log=True) == {"looks_right"}
    # it never holds a flight back by itself: the flight is the owner's no more, and reads off
    state = checks.sync_state(
        kind="flight",
        airtrail_on=True,
        hold=None,
        owner="off",
        cancelled=False,
        told=told,
        open=(STILL_LISTS,),
        desired=companions,
    )
    assert state == "off"
