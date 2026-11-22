# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Moving what an email says by whole days, once it is read (`run.Context.shift`): the demo files
invented emails as if they arrived that many days later. A local date or time keeps its form and
its time of day; the zone it is in is worked out afresh for the day it lands on."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from tripbox.mail.normalize import NormalizedMessage
from tripbox.parse.models import Extraction
from tripbox.validate.rules import LOCAL, parse_local

_HAS_TIME = re.compile(r"\d{1,2}:\d{2}")


@dataclass(frozen=True)
class DateShift:
    days: int

    def text(self, value: str | None) -> str | None:
        """One local date or date-time moved by `days`: `YYYY-MM-DD` stays a date and
        `YYYY-MM-DDTHH:MM` a date-time (only the day of a value written that way changes); a
        value in a printed form comes back in the first of those two forms that holds it. A value
        `rules.parse_local` cannot read is returned as it is."""
        if value is None or not self.days:
            return value
        when = parse_local(value)
        if when is None:
            return value
        day = (when + dt.timedelta(days=self.days)).date().isoformat()
        written = value.strip()
        if LOCAL.match(written):
            return day + written[10:]
        return f"{day}T{when:%H:%M}" if _HAS_TIME.search(written) else day

    def message(self, nm: NormalizedMessage) -> None:
        """The email's own date and its forwarder's, each keeping its offset."""
        step = dt.timedelta(days=self.days)
        if nm.date is not None:
            nm.date = nm.date + step
        if nm.outer_date is not None:
            nm.outer_date = nm.outer_date + step

    def extraction(self, ex: Extraction) -> None:
        """Every leg's departure and arrival and every booking's check-in, check-out, pick-up and
        drop-off. A printed offset belongs to the printed day, so a move drops it and the
        airport's zone gives the offset of the day the leg lands on."""
        for res in ex.reservations:
            for leg in res.segments:
                leg.departure_local = self.text(leg.departure_local)
                leg.arrival_local = self.text(leg.arrival_local)
                if self.days:
                    leg.departure_offset = None
                    leg.arrival_offset = None
            res.check_in = self.text(res.check_in)
            res.check_out = self.text(res.check_out)
            res.pickup_time = self.text(res.pickup_time)
            res.dropoff_time = self.text(res.dropoff_time)

    def apply(self, nm: NormalizedMessage, ex: Extraction) -> None:
        self.message(nm)
        self.extraction(ex)
