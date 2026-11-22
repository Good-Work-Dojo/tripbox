# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""AirTrail's reference data as the fakes serve it: real airports and airlines, invented ids."""

AIRTRAIL_AIRPORTS = {
    "MSP": [
        {
            "id": 9001,
            "iata": "MSP",
            "type": "large_airport",
            "timezone": "America/Chicago",
            "name": "Minneapolis-St Paul",
            "municipality": "Minneapolis",
        },
    ],
    "PHL": [
        {
            "id": 2,
            "iata": "PHL",
            "type": "large_airport",
            "timezone": "America/New_York",
            "name": "Philadelphia Intl",
            "municipality": "Philadelphia",
        }
    ],
    "DEN": [
        {
            "id": 1,
            "iata": "DEN",
            "type": "closed",
            "timezone": "America/Denver",
            "name": "A closed airport",
        },
        {
            "id": 3,
            "iata": "DEN",
            "type": "large_airport",
            "timezone": "America/Denver",
            "name": "Denver Intl",
            "municipality": "Denver",
        },
    ],
}
AIRTRAIL_AIRLINES = {
    "DL": [{"id": 9, "iata": "DL", "icao": "DAL", "name": "Delta Air Lines"}],
    "WN": [{"id": 10, "iata": "WN", "icao": "SWA", "name": "Southwest Airlines"}],
    "SY": [{"id": 11, "iata": "SY", "icao": "SCX", "name": "Sun Country Airlines"}],
}


def make_fetch(calls):
    def fetch(path, params):
        calls.append((path, params["query"]))
        if path.endswith("/airports"):
            return {"data": AIRTRAIL_AIRPORTS.get(params["query"], [])}  # wrapped shape
        if path.endswith("/airlines"):
            return AIRTRAIL_AIRLINES.get(params["query"], [])  # bare list shape
        return []

    return fetch


class ServesReferenceData:
    """A fake AirTrail's reference-data lookups (`get_json`), answered like `make_fetch`; each
    lookup is recorded in `ref_calls` as (path, code)."""

    def get_json(self, path, params):
        return make_fetch(vars(self).setdefault("ref_calls", []))(path, params)
