"""Since when each Radarr/Sonarr title has been searching without a break.

Radarr and Sonarr keep the date a title was first added through every unmonitor and
re-monitor, so how long the current search has run is recorded here: a row appears when
a title is first seen searching and goes when it is seen not searching.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from mediaman.core.time import now_iso, parse_iso_utc
from mediaman.services.arr.fetcher._base import ArrCard

logger = logging.getLogger(__name__)


def _keys(service: str, card: ArrCard) -> list[str]:
    """One key per season searched: a season re-monitored today restarts its show's clock."""
    base = f"{service}:{card['arr_id']}"
    return [f"{base}:{s['number']}" for s in card.get("stuck_seasons", [])] or [base]


def stamp_searching_since(
    conn: sqlite3.Connection, service: str, cards: Sequence[ArrCard], *, complete: bool
) -> None:
    """Set searching_since on each searching card of *service* and bring the rows up to date.

    *complete* is False when the service did not give its whole list: a title missing
    from it may well still be searching, so no row is dropped.
    """
    searching = [(c, _keys(service, c)) for c in cards if c.get("arr_id") and not c["is_upcoming"]]
    current = {key for _, keys in searching for key in keys}
    try:
        seen: dict[str, str] = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT key, since FROM arr_searching_since WHERE key LIKE ?", (f"{service}:%",)
            )
        }
        added = {key: now_iso() for key in current - seen.keys()}
        gone = seen.keys() - current if complete else set()
        if added or gone:
            conn.executemany(
                "INSERT OR IGNORE INTO arr_searching_since (key, since) VALUES (?, ?)",
                added.items(),
            )
            conn.executemany(
                "DELETE FROM arr_searching_since WHERE key=?", [(key,) for key in gone]
            )
            conn.commit()
    except sqlite3.DatabaseError:
        logger.warning("Failed to record what %s is searching for", service, exc_info=True)
        return
    seen |= added
    for card, keys in searching:
        times = [dt.timestamp() for key in keys if (dt := parse_iso_utc(seen[key])) is not None]
        if len(times) == len(keys):
            # A reinstalled Arr hands out its ids again; the add date is then the later one.
            card["searching_since"] = max(*times, card["added_at"])


def forget_searching_since(conn: sqlite3.Connection, service: str, arr_id: int) -> None:
    """Drop the rows of a title just abandoned, so a re-monitor starts a new search."""
    base = f"{service}:{arr_id}"
    try:
        conn.execute(
            "DELETE FROM arr_searching_since WHERE key=? OR key LIKE ?", (base, f"{base}:%")
        )
        conn.commit()
    except sqlite3.DatabaseError:
        logger.warning("Failed to forget the search for %s", base, exc_info=True)
