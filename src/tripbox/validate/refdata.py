# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Airports for validation, from the table bundled with the app (`airportsdata`)."""

from __future__ import annotations

import functools
from collections.abc import Mapping

import airportsdata


@functools.cache
def bundled_table() -> Mapping[str, Mapping]:
    """Every airport that has an IATA code, keyed by it; read at most once per process."""
    return airportsdata.load("IATA")


class Airports:
    """Airport records in the shape the validator reads (`municipality`, `timezone`). `table`,
    keyed by IATA code in the bundled table's shape, replaces the bundled table whole; left out,
    the bundled one is read on the first lookup."""

    def __init__(self, table: Mapping[str, Mapping] | None = None) -> None:
        self._table = table

    def airport(self, iata: str | None) -> dict | None:
        """A new record for this code, or None for no code or one the table does not hold."""
        code = (iata or "").strip().upper()
        if not code:
            return None
        table = self._table if self._table is not None else bundled_table()
        row = table.get(code)
        if row is None:
            return None
        return {
            "iata": row.get("iata") or code,
            "icao": row.get("icao"),
            "name": row.get("name"),
            "municipality": row.get("city") or None,
            "timezone": row.get("tz") or None,
            "country": row.get("country"),
        }
