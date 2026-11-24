"""Since when each Radarr/Sonarr title has been searching without a break.

Radarr and Sonarr keep the date a title was first added through every unmonitor and
re-monitor, so how long the current search has run is recorded here: a row appears when
a title is first seen searching and goes when it is seen not searching. A title that was
not looked at for a day, with mediaman or the Arr down, starts again.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import timedelta

from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.services.arr.fetcher._base import ArrCard

logger = logging.getLogger(__name__)

# Unseen this long, a title may have stopped searching and started again meanwhile.
_GAP = timedelta(hours=24)
# The page asks every two seconds; recording each look would be a write per poll.
_SEEN_EVERY = timedelta(minutes=5)


def _keys(service: str, card: ArrCard) -> list[str]:
    """One key per season monitored: a season re-monitored today restarts its show's clock."""
    base = f"{service}:{card['arr_id']}"
    seasons = [s for s in card.get("stuck_seasons", []) if s.get("monitored", True)]
    return [f"{base}:{s['number']}" for s in seasons] or [base]


def stamp_searching_since(
    conn: sqlite3.Connection, service: str, cards: Sequence[ArrCard], *, complete: bool
) -> None:
    """Set searching_since on each searching card of *service* and bring the rows up to date.

    *complete* is False when the service did not give its whole list: a title missing
    from it may well still be searching, so no row is dropped.
    """
    searching = [(c, _keys(service, c)) for c in cards if c.get("arr_id") and not c["is_upcoming"]]
    current = {key for _, keys in searching for key in keys}
    now_dt = now_utc()
    now = now_dt.isoformat()
    try:
        rows: dict[str, tuple[str, str | None]] = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT key, since, last_seen FROM arr_searching_since WHERE key LIKE ?",
                (f"{service}:%",),
            )
        }
        since = {key: rows[key][0] if key in rows else now for key in current}
        seen_again: list[tuple[str, str, str]] = []
        for key in current & rows.keys():
            last_seen = parse_iso_utc(rows[key][1])
            unseen_for = now_dt - last_seen if last_seen else None
            if unseen_for is not None and unseen_for >= _GAP:
                since[key] = now
            if unseen_for is None or not timedelta() <= unseen_for < _SEEN_EVERY:
                seen_again.append((since[key], now, key))
        added = [(key, now, now) for key in current - rows.keys()]
        gone = [(key,) for key in rows.keys() - current] if complete else []
        if added or seen_again or gone:
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO arr_searching_since (key, since, last_seen)"
                    " VALUES (?, ?, ?)",
                    added,
                )
                conn.executemany(
                    "UPDATE arr_searching_since SET since=?, last_seen=? WHERE key=?", seen_again
                )
                conn.executemany("DELETE FROM arr_searching_since WHERE key=?", gone)
    except sqlite3.DatabaseError:
        logger.warning("Failed to record what %s is searching for", service, exc_info=True)
        return
    for card, keys in searching:
        times = [dt.timestamp() for key in keys if (dt := parse_iso_utc(since[key])) is not None]
        if len(times) == len(keys):
            # A reinstalled Arr hands out its ids again; the add date is then the later one.
            card["searching_since"] = max(*times, card["added_at"])


def forget_searching_since(conn: sqlite3.Connection, service: str, arr_id: int) -> None:
    """Drop the rows of a title just abandoned, so a re-monitor starts a new search."""
    base = f"{service}:{arr_id}"
    try:
        with conn:
            conn.execute(
                "DELETE FROM arr_searching_since WHERE key=? OR key LIKE ?", (base, f"{base}:%")
            )
    except sqlite3.DatabaseError:
        logger.warning("Failed to forget the search for %s", base, exc_info=True)
