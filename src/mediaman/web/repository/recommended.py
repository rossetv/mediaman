from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import date, datetime

_SUGGESTIONS_COLS = (
    "id, title, year, media_type, category, tmdb_id, description, reason, "
    "poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres, cast_json, "
    "director, trailer_key, imdb_rating, metascore, batch_id, downloaded_at, created_at"
)


# rationale: a repository may return ``list[dict]`` at a documented
# boundary. ``mediaman.web.routes.watch.recommended_api`` groups these rows by
# ``batch_id``, adds each pick's download state to the row and shapes the
# Watch page's cards from it with ``.get(...)``; a dataclass would add ceremony
# without removing that column coupling.
def fetch_recommendations(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return **all** cached recommendations from the DB, newest batch first.

    Returns ``dict[str, object]`` rather than a dataclass: the caller reads
    rows with ``.get(...)`` and annotates them in place. Callers that need
    pagination should use `fetch_recommendations_page` instead.
    """
    rows = conn.execute(
        f"SELECT {_SUGGESTIONS_COLS} FROM suggestions ORDER BY batch_id DESC, category DESC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_recommendations_page(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, object]], int]:
    """Return a paginated page of recommendations plus the total row count.

    Pushes ``LIMIT``/``OFFSET`` into SQL so the full table is never loaded
    into memory for a single API request.

    Returns ``(rows, total)`` where *rows* is at most *limit* dicts and
    *total* is the count of all rows matching the query.
    """
    total: int = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
    rows = conn.execute(
        f"SELECT {_SUGGESTIONS_COLS} FROM suggestions "
        "ORDER BY batch_id DESC, category DESC, id ASC "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(r) for r in rows], total


def _relative_label(batch_date: date | None, index: int, today: date) -> str:
    if index == 0:
        return "Latest picks"
    if batch_date is None:
        return "Earlier picks"
    days = (today - batch_date).days
    if days <= 0:
        return "Earlier today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "Last week"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks} weeks ago"
    months = max(1, days // 30)
    return "A month ago" if months == 1 else f"{months} months ago"


def group_into_batches(
    recommendations: list[dict[str, object]], today: date
) -> list[dict[str, object]]:
    """Group recommendations by batch, newest first, every batch kept.

    A batch is ``{"id", "label", "date_label", "items"}``; its items are the
    watch-history picks followed by the trending picks. A row without a
    ``batch_id`` falls back to the date of its ``created_at``.
    """
    groups: OrderedDict[str, dict[str, list[dict[str, object]]]] = OrderedDict()
    for row in recommendations:
        created = row.get("created_at")
        bid = str(row.get("batch_id") or (created[:10] if isinstance(created, str) else ""))
        group = groups.setdefault(bid, {"personal": [], "trending": []})
        group["trending" if row.get("category") == "trending" else "personal"].append(row)

    batches: list[dict[str, object]] = []
    for index, (bid, group) in enumerate(groups.items()):
        try:
            batch_date: date | None = datetime.strptime(bid, "%Y-%m-%d").date()
        except ValueError:
            batch_date = None
        batches.append(
            {
                "id": bid,
                "label": _relative_label(batch_date, index, today),
                "date_label": batch_date.strftime("%-d %b") if batch_date else bid,
                "items": group["personal"] + group["trending"],
            }
        )
    return batches


__all__ = ["fetch_recommendations", "fetch_recommendations_page", "group_into_batches"]
