# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import airportsdata
from helpers.airports import SAMPLE_AIRPORTS

from tripbox.validate import refdata
from tripbox.validate.refdata import Airports


def test_the_bundled_table_gives_the_reference_record_shape():
    a = Airports().airport("msp")
    assert set(a) == {"iata", "icao", "name", "municipality", "timezone", "country"}
    assert a["iata"] == "MSP" and a["icao"] == "KMSP" and a["country"] == "US"
    assert a["timezone"] == "America/Chicago"
    assert isinstance(a["municipality"], str) and a["municipality"]


def test_codes_outside_the_table_are_unknown():
    table = Airports()
    assert table.airport(None) is None
    assert table.airport("") is None
    assert table.airport("Z9Z") is None


def test_the_table_is_read_once_per_process(monkeypatch):
    calls = []

    def load(code_type):
        calls.append(code_type)
        return dict(SAMPLE_AIRPORTS)

    refdata.bundled_table.cache_clear()
    monkeypatch.setattr(airportsdata, "load", load)
    try:
        first, second = Airports(), Airports()
        assert first.airport("MSP")["municipality"] == "Minneapolis"
        assert first.airport("PHL")["timezone"] == "America/New_York"
        assert second.airport("DEN")["icao"] == "KDEN"
        assert calls == ["IATA"]
    finally:
        refdata.bundled_table.cache_clear()  # the next caller reads the real table


def test_an_injected_table_replaces_the_bundled_one():
    only = {
        "PHL": {**SAMPLE_AIRPORTS["PHL"], "city": "", "tz": ""},
    }
    table = Airports(only)
    assert table.airport("phl") == {
        "iata": "PHL",
        "icao": "KPHL",
        "name": "Philadelphia Intl",
        "municipality": None,
        "timezone": None,
        "country": "US",
    }
    assert table.airport("MSP") is None  # in the bundled table, not in this one
    assert Airports({}).airport("MSP") is None
    # each call hands out a record of its own
    table.airport("PHL")["name"] = "changed"
    assert table.airport("PHL")["name"] == "Philadelphia Intl"
