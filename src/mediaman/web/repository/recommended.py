from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import date, datetime

from mediaman.services.openai.recommendations.repository import SHARED_ONLY
from mediaman.services.openai.recommendations.user_picks import PICKS_LAST, fetch_picks

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
def fetch_recommendations(conn: sqlite3.Connection, username: str) -> list[dict[str, object]]:
    """Return **all** cached recommendations *username* may see, newest batch first: their
    own, with ``own`` true, and the shared ones.

    Returns ``dict[str, object]`` rather than a dataclass: the caller reads
    rows with ``.get(...)`` and annotates them in place. Callers that need
    pagination should use `fetch_recommendations_page` instead.
    """
    shared = conn.execute(
        f"SELECT {_SUGGESTIONS_COLS} FROM suggestions WHERE {SHARED_ONLY}"
        " ORDER BY batch_id DESC, id ASC"
    ).fetchall()
    rows = [{**row, "own": True} for row in fetch_picks(conn, username)]
    rows += [{**dict(r), "own": False} for r in shared]
    rows.sort(key=_batch_of, reverse=True)
    return rows


def _batch_of(row: dict[str, object]) -> str:
    created = row.get("created_at")
    return str(row.get("batch_id") or (created[:10] if isinstance(created, str) else ""))


def fetch_recommendations_page(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, object]], int]:
    """Return a paginated page of the shared recommendations plus the total row count.

    Pushes ``LIMIT``/``OFFSET`` into SQL so the full table is never loaded
    into memory for a single API request.

    Returns ``(rows, total)`` where *rows* is at most *limit* dicts and
    *total* is the count of all rows matching the query.
    """
    total: int = conn.execute(f"SELECT COUNT(*) FROM suggestions WHERE {SHARED_ONLY}").fetchone()[0]
    rows = conn.execute(
        f"SELECT {_SUGGESTIONS_COLS} FROM suggestions WHERE {SHARED_ONLY} "
        "ORDER BY batch_id DESC, id ASC "
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


def _date_label(batch_id: str) -> str:
    try:
        return datetime.strptime(batch_id, "%Y-%m-%d").strftime("%-d %b")
    except ValueError:
        return batch_id


def group_into_batches(
    recommendations: list[dict[str, object]], today: date
) -> list[dict[str, object]]:
    """Group recommendations by batch, newest first, every batch kept.

    A batch is ``{"id", "label", "date_label", "items"}``; its items are the
    person's own picks followed by the shared ones. The first holds the latest of
    both, unless old, also where one is from an earlier batch, which has them too.
    The first batch's date is the date of the person's own picks when they come from an
    earlier batch. A row without a ``batch_id`` falls back to the date of its ``created_at``.
    """
    groups: OrderedDict[str, dict[str, list[dict[str, object]]]] = OrderedDict()
    for row in recommendations:
        group = groups.setdefault(_batch_of(row), {"personal": [], "trending": []})
        group["trending" if row.get("category") == "trending" else "personal"].append(row)

    oldest = (today - PICKS_LAST).isoformat()
    own_from: str | None = None
    for latest in list(groups.values())[:1]:
        for kind in latest:
            if latest[kind]:
                continue
            for bid, g in groups.items():
                if bid >= oldest and g[kind]:
                    latest[kind] = g[kind]
                    own_from = bid if kind == "personal" else own_from
                    break

    batches: list[dict[str, object]] = []
    for index, (bid, group) in enumerate(groups.items()):
        try:
            batch_date: date | None = datetime.strptime(bid, "%Y-%m-%d").date()
        except ValueError:
            batch_date = None
        # The person's own picks from an earlier batch lead the first, and say their date.
        shown = own_from if index == 0 and own_from else bid
        batches.append(
            {
                "id": bid,
                "label": _relative_label(batch_date, index, today),
                "date_label": _date_label(shown),
                "items": group["personal"] + group["trending"],
            }
        )
    return batches


__all__ = ["fetch_recommendations", "fetch_recommendations_page", "group_into_batches"]
