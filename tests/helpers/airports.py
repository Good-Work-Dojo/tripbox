# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The validator's airport table as tests inject it: real airports in the bundled table's shape
(the fields `Airports` reads), with the cities and zones AirTrail's fakes give them."""

from tripbox.validate.refdata import Airports

SAMPLE_AIRPORTS = {
    "MSP": {
        "iata": "MSP",
        "icao": "KMSP",
        "name": "Minneapolis-St Paul",
        "city": "Minneapolis",
        "country": "US",
        "tz": "America/Chicago",
    },
    "PHL": {
        "iata": "PHL",
        "icao": "KPHL",
        "name": "Philadelphia Intl",
        "city": "Philadelphia",
        "country": "US",
        "tz": "America/New_York",
    },
    "DEN": {
        "iata": "DEN",
        "icao": "KDEN",
        "name": "Denver Intl",
        "city": "Denver",
        "country": "US",
        "tz": "America/Denver",
    },
    "BOS": {
        "iata": "BOS",
        "icao": "KBOS",
        "name": "Logan International",
        "city": "Boston",
        "country": "US",
        "tz": "America/New_York",
    },
}


def airports() -> Airports:
    return Airports(SAMPLE_AIRPORTS)
