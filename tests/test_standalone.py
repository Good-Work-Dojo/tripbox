# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""A new install with nothing but the core settings: no mailbox, no model key, no AirTrail, no
notifications. A forwarded booking handed to `ingest` is filed in a trip, with airports from the
bundled table. The model's answer is a recorded one, written here by hand for the invented
family."""

import datetime as dt
import os

from helpers.eml import make_eml
from helpers.sample import CONFIG_DIR

from tripbox import cli
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.parse import pipeline as pl
from tripbox.parse.models import WireExtraction, WireLeg, WireReservation
from tripbox.parse.replay import answer_path
from tripbox.trips.store import TripStore
from tripbox.validate.refdata import Airports

FLIGHT_KEY = "DL|9123|2027-03-12|MSP"


def answer() -> WireExtraction:
    """What the model read from the booking: the owner's flight and the hotel at the far end."""
    return WireExtraction(
        document_type="booking",
        confidence=0.95,
        reservations=[
            WireReservation(
                kind="flight",
                status="confirmed",
                code="QXR987",
                provider="Delta Air Lines",
                passengers=["Alex Sample"],
                legs=[
                    WireLeg(
                        carrier="DL",
                        number="9123",
                        origin="MSP",
                        destination="BOS",
                        departure="2027-03-12T08:20",
                        arrival="2027-03-12T12:10",
                    )
                ],
            ),
            WireReservation(
                kind="lodging",
                status="confirmed",
                code="QXR987",
                provider="Example Hotels",
                passengers=["Alex Sample"],
                place="Example Harbor Hotel",
                address="1 Example Wharf, Boston, MA",
                start="2027-03-12",
                end="2027-03-15",
            ),
        ],
    )


def test_a_new_install_files_forwarded_bookings_with_nothing_but_core_settings(
    tmp_path, monkeypatch, capsys
):
    for name in list(os.environ):
        if name.startswith("TRIPBOX_"):
            monkeypatch.delenv(name)
    answers = tmp_path / "answers"
    answers.mkdir()
    for name, value in {
        "TRIPBOX_DATA_DIR": str(tmp_path / "data"),
        "TRIPBOX_ARCHIVE_DIR": str(tmp_path / "archive"),
        "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        "TRIPBOX_CONFIG_DIR": str(CONFIG_DIR),
        "TRIPBOX_REPLAY_DIR": str(answers),
        "TRIPBOX_HOME_AIRPORTS": "MSP",
        "TRIPBOX_AIRTRAIL_DRY_RUN": "false",
        "TRIPBOX_AUTH": "none",
        "TZ": "America/Chicago",
    }.items():
        monkeypatch.setenv(name, value)
    raw = make_eml(
        from_="alex@example.com",
        to="trips@example.com",
        subject="Fwd: Your trip to Boston",
        date=dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC),
        text=(
            "Confirmation QXR987\nDL 9123 MSP 8:20 AM - BOS 12:10 PM, Fri Mar 12 2027\n"
            "Example Harbor Hotel, 1 Example Wharf, Boston, MA: Mar 12 - Mar 15 2027"
        ),
        message_id="<qxr987@example.com>",
    ).as_bytes()
    eml = tmp_path / "booking.eml"
    eml.write_bytes(raw)
    answer_path(answers, raw).write_text(answer().model_dump_json())
    # kitinerary finds nothing structured in a plain-text forward, so the model's answer is read
    monkeypatch.setattr(pl.kitinerary, "run_extractor", lambda data, suffix, ctx_date: [])

    assert cli.main(["ingest", str(eml), "--quiet"]) == 0
    # saved again with a byte-order mark and an empty first line, it is the same email
    saved = tmp_path / "saved.eml"
    saved.write_bytes(b"\xef\xbb\xbf\r\n" + raw)
    assert cli.main(["ingest", str(saved), "--quiet"]) == 0
    s = Settings.from_env()
    assert not s.on("airtrail") and not s.on("mail") and not s.on("model")
    led = Ledger.open(s.ledger_path)
    assert led.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    store = TripStore(led, s.home_airports)
    (trip,) = store.all_trips()
    filed = store.items(trip.id)
    assert sorted(ti.kind for ti in filed) == ["flight", "lodging"]
    (flight,) = [ti for ti in filed if ti.kind == "flight"]
    assert flight.item_key == FLIGHT_KEY and flight.airtrail_flight_id is None
    (row,) = led.items.by_trip_item(flight.id)
    assert (row["outcome"], row["item_key"]) == ("filed", FLIGHT_KEY)
    assert (flight.sync_state, flight.told) == ("off", None)  # no flight log is kept
    # the flight's city is the bundled table's (the hotel's address names the same city)
    boston = Airports().airport("BOS")["municipality"]
    assert flight.payload["destination_city"] == boston
    led.close()
    capsys.readouterr()

    assert cli.main(["trips", "list"]) == 0
    assert boston in capsys.readouterr().out
    assert cli.main(["doctor"]) == 0
