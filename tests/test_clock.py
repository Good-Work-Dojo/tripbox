# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The configured zone is the process's zone: what "today" means, and what a subprocess sees."""

import datetime as dt
import os
import subprocess
import sys
import time

from helpers.sample import CONFIG_DIR, TRAVELLERS
from helpers.web import client_for
from test_web_views import flight

from tripbox import cli, util
from tripbox.parse import kitinerary
from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.trips import grouping
from tripbox.validate.passengers import PassengerBook
from tripbox.validate.refdata import Airports
from tripbox.validate.rules import validate_extraction

JAN_15 = 1799971200  # 2027-01-15T00:00:00Z, summer time in Auckland (+13:00)


def test_the_configured_zone_becomes_the_process_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()  # the process runs in UTC, as the image starts it
    monkeypatch.setenv("TRIPBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRIPBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TZ", "Pacific/Auckland")  # what the settings name; nothing applied it yet
    assert time.localtime(JAN_15).tm_gmtoff == 0
    assert cli.main(["trips", "list"]) == 0
    assert os.environ["TZ"] == "Pacific/Auckland"
    assert time.localtime(JAN_15).tm_gmtoff == 46800
    probe = "import time; print(time.strftime('%z', time.localtime(1799971200)))"
    child = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert child.stdout.strip() == "+1300"


def test_the_extractor_inherits_the_process_environment(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(kitinerary.subprocess, "run", run)
    assert kitinerary.run_extractor(b"x", ".eml", None) == []
    [kwargs] = calls
    assert "env" not in kwargs  # so it reads the zone use_zone exported


def test_today_follows_the_configured_zone(monkeypatch):
    monkeypatch.setattr(util, "now", lambda: dt.datetime(2027, 11, 13, 20, 0, tzinfo=dt.UTC))
    util.use_zone("Pacific/Auckland")
    assert util.today_local() == dt.date(2027, 11, 14)
    for zone in ("UTC", "America/Chicago"):
        util.use_zone(zone)
        assert util.today_local() == dt.date(2027, 11, 13), zone


BOOK = PassengerBook.from_yaml(TRAVELLERS)
EMAIL = dt.datetime(2027, 3, 1, 12, 0, tzinfo=dt.UTC)


def one_flight(carrier, number, origin, dest, dep, arr):
    ex = Extraction(
        document_type="booking",
        confidence=1.0,
        reservations=[
            Reservation(
                kind="flight",
                status="confirmed",
                confirmation_code="QXR987",
                provider="Air New Zealand",
                passengers=["Alex Sample"],
                segments=[
                    Segment(
                        carrier_iata=carrier,
                        flight_number=number,
                        origin=origin,
                        destination=dest,
                        departure_local=dep,
                        arrival_local=arr,
                    )
                ],
            )
        ],
    )
    (it,) = validate_extraction(
        ex, refdata=Airports(), book=BOOK, email_date=EMAIL, source={"message_id": "<m1>"}
    )
    return it


def test_a_date_line_crossing_keeps_its_local_dates_and_true_duration():
    it = one_flight("NZ", "9010", "AKL", "HNL", "2027-03-13T19:30", "2027-03-13T06:40")
    assert it.problems == []
    p = it.payload
    assert p["dep_utc"] == "2027-03-13T06:30:00Z"
    assert p["arr_utc"] == "2027-03-13T16:40:00Z"
    assert p["duration_min"] == 610
    assert it.key == "NZ|9010|2027-03-13|AKL"
    assert grouping.item_dates(it) == (dt.date(2027, 3, 13), dt.date(2027, 3, 13))


def test_a_flight_in_a_zone_ahead_of_utc_keys_on_its_local_date():
    for zone in ("UTC", "America/Chicago"):
        util.use_zone(zone)
        it = one_flight("NZ", "9101", "AKL", "SYD", "2027-03-14T00:30", "2027-03-14T02:00")
        assert it.payload["dep_utc"] == "2027-03-13T11:30:00Z", zone
        assert it.payload["date_local"] == "2027-03-14", zone
        assert it.key == "NZ|9101|2027-03-14|AKL", zone


def test_the_page_lists_a_trip_by_the_configured_date(tmp_path, monkeypatch):
    client, ctx = client_for(tmp_path, monkeypatch)  # served at the host's root
    item = flight(dep="2027-11-13T08:00", arr="2027-11-13T11:35")
    ctx.trips.upsert_item(ctx.trips.find_or_create(item), item)
    monkeypatch.setattr(util, "now", lambda: dt.datetime(2027, 11, 13, 20, 0, tzinfo=dt.UTC))
    where = {}
    for zone in ("UTC", "Pacific/Auckland"):
        util.use_zone(zone)
        html = client.get("/").text
        upcoming, _, past = html.partition("Past trips")
        where[zone] = ("Philadelphia" in upcoming, "Philadelphia" in past)
    assert where["UTC"] == (True, False)
    assert where["Pacific/Auckland"] == (False, True)
