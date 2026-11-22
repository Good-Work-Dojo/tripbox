# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import json

import httpx2
import pytest
from helpers.airtrail import make_fetch

from tripbox import __version__, log
from tripbox.ledger import Ledger
from tripbox.write.airtrail import (
    ERROR_CODES,
    PICKER_ADDS,
    REQUIRED_SCOPES,
    AirTrailClient,
    AirTrailError,
    AirTrailRefs,
    KeyReport,
    Profile,
    beyond,
    flight_to_input,
    key_report,
    missing_scope,
)


def flight(fid, date, number, origin):
    return {
        "id": fid,
        "date": date,
        "flightNumber": number,
        "from": {"id": 1, "iata": origin},
        "to": {"id": 2, "iata": "PHL"},
    }


def make_client(handler):
    return AirTrailClient("http://airtrail:3000/", "tok", transport=httpx2.MockTransport(handler))


def test_pagination_follows_cursor_and_stops_at_until_date():
    seen = []

    def handler(req):
        seen.append((req.url.path, dict(req.url.params), req.headers.get("Authorization")))
        assert req.headers["User-Agent"] == f"tripbox/{__version__}"
        if req.url.path == "/api/v1/flights" and "cursor" not in req.url.params:
            return httpx2.Response(
                200,
                json={
                    "data": [
                        flight(3, "2027-07-01", "DL1", "MSP"),
                        flight(2, "2027-06-22", "DL1234", "MSP"),
                    ],
                    "page": {"nextCursor": "c2"},
                },
            )
        if req.url.params.get("cursor") == "c2":
            return httpx2.Response(
                200,
                json={
                    "data": [flight(1, "2026-01-01", "UA9", "MSP")],
                    "page": {"nextCursor": None},
                },
            )
        return httpx2.Response(404)

    c = make_client(handler)
    assert [f["id"] for f in c.iter_flights()] == [3, 2, 1]
    assert seen[0][2] == "Bearer tok" and seen[1][1]["cursor"] == "c2"
    seen.clear()
    assert [f["id"] for f in c.iter_flights(until_date="2027-06-23")] == [
        3,
        2,
    ]  # second page never fetched
    assert len(seen) == 1


def test_find_flight_matches_number_variants_and_origin():
    def handler(req):
        return httpx2.Response(
            200,
            json={
                "data": [
                    flight(9, "2027-06-22", "DL11234", "MSP"),  # longer number: not ours
                    flight(2, "2027-06-22", "DL1234", "MSP"),
                    flight(5, "2027-06-22", "1234", "DEN"),
                ],
                "page": {"nextCursor": None},
            },
        )

    c = make_client(handler)
    assert (
        c.find_flight(flight_code="DL1234", date_local="2027-06-22", origin_iata="MSP")["id"] == 2
    )
    assert (
        c.find_flight(flight_code="DL 1234", date_local="2027-06-22", origin_iata="DEN")["id"] == 5
    )
    assert c.find_flight(flight_code="DL1234", date_local="2027-06-23", origin_iata="MSP") is None
    assert (
        c.find_flight(flight_code="UA1234", date_local="2027-06-22", origin_iata="MSP")["id"] == 2
    )  # number match


def test_create_get_update_shapes_and_errors():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "POST":
            return httpx2.Response(201, json={"data": {"id": 42}})
        if req.method == "GET" and req.url.path == "/api/v1/flights/42":
            return httpx2.Response(200, json={"data": {"id": 42, "note": "n"}})
        if req.method == "PUT":
            return httpx2.Response(200, json={"data": {"id": 42}})
        if req.url.path == "/api/v1/me":
            return httpx2.Response(403, json={"message": "Missing scope profile.read"})
        return httpx2.Response(500, text="boom")

    c = make_client(handler)
    assert c.create_flight({"date": "2027-06-22"}) == 42
    assert c.get_flight(42)["note"] == "n"
    c.update_flight(42, {"note": "x"})
    assert calls[2] == ("PUT", "/api/v1/flights/42", {"note": "x"})
    with pytest.raises(AirTrailError) as ei:
        c.me()
    assert ei.value.status == 403 and "profile.read" in ei.value.message
    with pytest.raises(AirTrailError) as ei2:
        c.get_json("/api/v1/airports", {"query": "MSP"})
    assert ei2.value.status == 500 and ei2.value.message == "boom"


def test_flight_to_input_round_trips_identity_and_ids():
    from tripbox.write.airtrail import flight_to_input

    f = {
        "id": 42,
        "date": "2027-06-22",
        "datePrecision": "day",
        "departure": "2027-06-22T13:20:00Z",
        "arrival": None,
        "duration": 155,
        "flightNumber": "DL1234",
        "note": "n",
        "aircraftReg": None,
        "from": {"id": 9001, "iata": "MSP"},
        "to": {"id": 2, "iata": "PHL"},
        "airline": {"id": 9},
        "aircraft": None,
        "track": None,
        "passengers": [
            {
                "id": 7,
                "user": {"id": "sampleuser00001", "username": "alex"},
                "guestName": None,
                "seat": None,
                "seatNumber": "14A",
                "seatClass": "economy",
                "flightReason": None,
            },
            {
                "id": 8,
                "user": None,
                "guestName": "Sam Sample",
                "seat": "window",
                "seatNumber": "14B",
                "seatClass": None,
                "flightReason": "leisure",
            },
        ],
    }
    inp = flight_to_input(f)
    assert (
        inp["fromId"] == 9001
        and inp["toId"] == 2
        and inp["airlineId"] == 9
        and inp["aircraftId"] is None
    )
    assert (
        inp["date"] == "2027-06-22"
        and inp["duration"] == 155
        and "id" not in inp
        and "track" not in inp
    )
    me, guest = inp["passengers"]
    assert me == {
        "id": 7,
        "userId": "sampleuser00001",
        "seat": None,
        "seatNumber": "14A",
        "seatClass": "economy",
        "flightReason": None,
    }
    assert guest["guestName"] == "Sam Sample" and "userId" not in guest and guest["id"] == 8


def test_transport_and_json_failures_are_airtrail_errors():
    import pytest

    from tripbox.write.airtrail import AirTrailClient, AirTrailError

    def down(request):
        raise httpx2.ConnectError("[Errno -3] Temporary failure in name resolution")

    c = AirTrailClient("http://airtrail:3000", "tok", transport=httpx2.MockTransport(down))
    with pytest.raises(AirTrailError) as e:
        c.me()
    assert e.value.status == 0 and "ConnectError" in str(e.value)

    def garbage(request):
        return httpx2.Response(200, content=b"<html>proxy error</html>")

    c = AirTrailClient("http://airtrail:3000", "tok", transport=httpx2.MockTransport(garbage))
    with pytest.raises(AirTrailError) as e:
        c.me()
    assert e.value.status == 0 and "JSON" in str(e.value)


def test_the_user_id_comes_from_me_and_is_asked_once():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx2.Response(200, json={"data": {"id": "sampleuser00001", "username": "alex"}})

    c = make_client(handler)
    assert c.user_id() == "sampleuser00001"
    assert c.user_id() == "sampleuser00001"
    assert seen == ["/api/v1/me"]


def test_a_me_answer_without_an_id_is_an_airtrail_error():
    c = make_client(lambda req: httpx2.Response(200, json={"data": {}}))
    with pytest.raises(AirTrailError, match="profile.read"):
        c.user_id()


def ref_client(calls):
    """A client whose AirTrail answers reference lookups from the shared fixture records."""
    fetch = make_fetch(calls)
    return make_client(
        lambda req: httpx2.Response(200, json=fetch(req.url.path, dict(req.url.params)))
    )


def ref_rows(led, code):
    return led.conn.execute("SELECT kind FROM ref_cache WHERE code=?", (code,)).fetchall()


def test_refs_ask_airtrail_once_and_cache_only_hits(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    calls = []
    refs = AirTrailRefs(ref_client(calls), led)
    assert refs.airport_id("msp") == 9001
    assert refs.airport_id(" MSP ") == 9001
    assert refs.airline_id("DL") == 9
    assert refs.airport_id("XXX") is None
    assert refs.airport_id("XXX") is None  # a miss is asked again
    assert refs.aircraft("b738") is None
    assert refs.airport_id(None) is None and refs.airline_id("") is None  # nothing to ask
    assert calls == [
        ("/api/v1/airports", "MSP"),
        ("/api/v1/airlines", "DL"),
        ("/api/v1/airports", "XXX"),
        ("/api/v1/airports", "XXX"),
        ("/api/v1/aircraft", "B738"),
    ]
    assert ref_rows(led, "XXX") == [] and ref_rows(led, "B738") == []
    assert led.refcache.get("airport", "MSP")["id"] == 9001
    assert refs.airport_id("DEN") == 3  # the open airport, not the closed one with the code
    # a cached empty record (an old miss) counts as absent
    led.refcache.put("airport", "PHL", {})
    assert refs.airport_id("PHL") == 2 and calls[-1] == ("/api/v1/airports", "PHL")
    # a failed lookup raises and stores nothing
    down = AirTrailRefs(make_client(lambda req: httpx2.Response(500, text="boom")), led)
    with pytest.raises(AirTrailError):
        down.airline_id("WN")
    assert ref_rows(led, "WN") == []


def test_refs_survive_a_new_instance_through_the_ledger(tmp_ledger_path):
    led = Ledger.open(tmp_ledger_path)
    calls = []
    assert AirTrailRefs(ref_client(calls), led).airport_id("PHL") == 2
    assert AirTrailRefs(ref_client(calls), led).airport_id("PHL") == 2
    assert AirTrailRefs(ref_client(calls), led).airline_id("SY") == 11
    assert AirTrailRefs(ref_client(calls), led).airline_id("SY") == 11
    assert calls == [("/api/v1/airports", "PHL"), ("/api/v1/airlines", "SY")]
    # a record older than 90 days is asked for again
    led.conn.execute("UPDATE ref_cache SET fetched_at='2020-01-01T00:00:00+00:00'")
    assert AirTrailRefs(ref_client(calls), led).airport_id("PHL") == 2
    assert len(calls) == 3


def test_an_error_carries_airtrails_code_and_a_validation_errors_first_details(monkeypatch):
    """AirTrail's error body is `{"error": {"code", "message", "details"}}`: the code is kept
    when it is one of AirTrail's nine, a validation error's message ends with its first two
    details, and the key never shows, whoever echoes it."""
    key = "the-key-this-client-sends"
    monkeypatch.setattr(log, "_SECRETS", set())
    log.add_secrets([key])
    assert len(ERROR_CODES) == len(set(ERROR_CODES)) == 9
    details = [
        {"code": "invalid_type", "path": ["fromId"], "message": f"expected a number, not {key}"},
        {"code": "too_big", "path": ["passengers", "0", "guestName"], "message": "too long"},
        {"code": "too_big", "path": ["note"], "message": "the third is left out"},
    ]
    refused = {
        "error": {
            "code": "validation_failed",
            "message": "Request validation failed",
            "details": details,
        }
    }
    c = AirTrailClient(
        "http://airtrail:3000/",
        key,
        transport=httpx2.MockTransport(lambda req: httpx2.Response(422, json=refused)),
    )
    with pytest.raises(AirTrailError) as told:
        c.create_flight({"date": "2027-06-22"})
    err = told.value
    assert (err.status, err.code) == (422, "validation_failed")
    assert err.message == (
        "Request validation failed: fromId: expected a number, not [REDACTED];"
        " passengers.0.guestName: too long"
    )
    assert "the third" not in err.message and len(err.message) <= 300
    # a body with no code, and one whose code is none of AirTrail's: no code is kept
    uncoded = ({"message": f"no such key as {key}"}, {"error": {"code": "teapot", "message": key}})
    for body in uncoded:
        c = AirTrailClient(
            "http://airtrail:3000/",
            key,
            transport=httpx2.MockTransport(lambda req, body=body: httpx2.Response(403, json=body)),
        )
        with pytest.raises(AirTrailError) as plain:
            c.get_flight(42)
        assert (plain.value.status, plain.value.code) == (403, None)
        assert key not in plain.value.message and key not in str(plain.value)
    assert key not in err.message and key not in str(err)
    # each of the nine is kept as it is
    for code in ERROR_CODES:
        body = {"error": {"code": code, "message": "said so"}}
        c = make_client(lambda req, body=body: httpx2.Response(400, json=body))
        with pytest.raises(AirTrailError) as coded:
            c.get_flight(42)
        assert (coded.value.code, coded.value.message) == (code, "said so")


def refusal(code: str | None, message: str, status: int = 403) -> AirTrailError:
    return AirTrailError(status, message, code=code)


def test_the_scope_a_refusal_names_is_read_from_its_message():
    for scope in (*REQUIRED_SCOPES, "flight.read.any", "flight.update.any"):
        said = refusal("insufficient_scope", f"The credential requires {scope}")
        assert missing_scope(said) == scope
    role = refusal("forbidden", "The current role does not permit this action")
    assert missing_scope(role) is None
    # the sentence under another code, and anything that is no scope's name, names none
    assert missing_scope(refusal("forbidden", "The credential requires flight.read.any")) is None
    assert missing_scope(refusal(None, "The credential requires flight.read.any")) is None
    for no_scope in ("$(x)", "flight", "flight.read.any now", "a.b.c.d.e", "Flight.Read.Any", ""):
        said = refusal("insufficient_scope", f"The credential requires {no_scope}")
        assert missing_scope(said) is None, no_scope
    assert missing_scope(refusal("insufficient_scope", "requires flight.read.any")) is None


def test_the_profile_gives_the_user_the_keys_scopes_and_the_roles_permissions():
    seen = []
    answers = [
        httpx2.Response(500, text="boom"),
        httpx2.Response(
            200,
            json={
                "data": {
                    "id": "sampleuser00001",
                    "username": "alex",
                    "displayName": "Alex",
                    "role": {"id": "role1", "name": "Member"},
                    "isOwner": False,
                    "permissions": ["flight.read.own", "flight.create.own"],
                    "credential": {"kind": "apiKey", "scopes": ["profile.read", "flight.read.own"]},
                }
            },
        ),
    ]

    def handler(req):
        seen.append(req.url.path)
        return answers.pop(0)

    c = make_client(handler)
    with pytest.raises(AirTrailError):
        c.profile()  # only a success is kept
    assert c.profile() == Profile(
        user_id="sampleuser00001",
        kind="apiKey",
        scopes=frozenset({"profile.read", "flight.read.own"}),
        permissions=frozenset({"flight.read.own", "flight.create.own"}),
        is_owner=False,
    )
    assert c.profile() is c.profile() and c.user_id() == "sampleuser00001"
    assert seen == ["/api/v1/me"] * 2
    # an answer that names no credential: nothing is known of the key, and nothing concluded
    bare = make_client(
        lambda req: httpx2.Response(
            200, json={"data": {"id": "sampleuser00001", "permissions": ["flight.read.own"]}}
        )
    )
    assert bare.profile() == Profile("sampleuser00001", "", frozenset(), frozenset(), False)
    assert key_report(bare.profile()) is None


def test_a_key_report_names_what_is_missing_denied_and_more():
    six = frozenset(REQUIRED_SCOPES)
    assert REQUIRED_SCOPES == (
        "profile.read",
        "reference_data.read",
        "flight.read.own",
        "flight.create.own",
        "flight.update.own",
        "flight.passengers.manage.own",
    )
    flights = frozenset(s for s in REQUIRED_SCOPES if s.startswith("flight."))

    def report(scopes, permissions=flights, is_owner=False) -> KeyReport | None:
        return key_report(Profile("sampleuser00001", "apiKey", scopes, permissions, is_owner))

    assert report(six) == KeyReport(missing=(), denied=(), more=())
    last = REQUIRED_SCOPES[-1]
    assert report(six - {last}) == KeyReport(missing=(last,), denied=(), more=())
    assert report(frozenset()).missing == REQUIRED_SCOPES
    # the wider scope covers the narrower one, and is itself more than tripbox needs
    wider = (six - {"flight.update.own"}) | {"flight.update.any"}
    assert report(wider) == KeyReport(missing=(), denied=(), more=("flight.update.any",))
    # a role that does not grant what the key holds; the instance's owner is granted everything
    lesser = flights - {"flight.create.own"}
    assert report(six, lesser) == KeyReport(missing=(), denied=("flight.create.own",), more=())
    assert report(six, lesser | {"flight.create.any"}).denied == ()
    assert report(six, frozenset(), is_owner=True).denied == ()
    assert report(six, frozenset()).denied == tuple(sorted(flights, key=REQUIRED_SCOPES.index))
    # what writes, deletes or reaches other people's flights is more; what only reads is not,
    # but for the two AirTrail's scope picker adds to the six, which are more and harmless: a
    # narrower key holds none of the rest (`beyond`)
    extra = six | {"flight.delete.own", "tracks.write", "tracks.read", "flight.export.own"}
    assert report(extra).more == ("flight.delete.own", "flight.export.own", "tracks.write")
    assert beyond(report(extra).more) == ("tracks.write",)
    assert PICKER_ADDS == ("flight.export.own", "flight.delete.own")
    assert beyond(report(six | set(PICKER_ADDS)).more) == ()
    every = ("flight.delete.any", "flight.read.any", "preferences.write", "roles.manage")
    every += ("shares.write", "visited_countries.write")
    assert report(six | set(every)).more == tuple(sorted(every))


def test_a_flights_input_never_carries_a_track_or_custom_fields():
    """A `track` key in a write, `null` included, is a write of the flight's track, and a custom
    field is the owner's own: neither is ever in a body, whatever the flight read back holds."""
    held = {
        **flight(42, "2027-06-22", "DL1234", "MSP"),
        "track": {"source": "by hand", "points": [[0, 0]]},
        "customFields": {"a field of the owner's": "kept"},
        "passengers": [],
    }
    inp = flight_to_input(held)
    assert not {"track", "customFields"} & inp.keys()
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx2.Response(201 if req.method == "POST" else 200, json={"data": {"id": 42}})

    c = make_client(handler)
    carried = {**inp, "track": None, "customFields": {}}
    c.create_flight(carried)
    c.update_flight(42, carried)
    assert sent == [inp, inp] and "track" in carried  # the caller's own dict is left as it was
