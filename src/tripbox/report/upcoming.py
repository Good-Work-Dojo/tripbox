# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Plain-text upcoming view: the next trips from the ledger, rendered by trips.format."""

from __future__ import annotations

from tripbox.pipeline.run import Context
from tripbox.trips.format import upcoming_text
from tripbox.util import today_local


def build_upcoming(ctx: Context, limit: int = 5) -> str:
    return upcoming_text(
        ctx.trips,
        today_local(),
        book=ctx.book,
        home_airports=ctx.settings.home_airports,
        max_trips=limit,
    )
