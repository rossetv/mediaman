"""Copies asked for from Watch for another audio language, followed until Plex has them."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mediaman.core.time import now_iso


@dataclass(frozen=True)
class Asked:
    id: int
    username: str
    rating_key: str
    season_key: str | None
    scope: str
    language: str
    service: str
    arr_id: int
    season_number: int | None
    episode_id: int | None
    part_id: str | None
    release_languages: tuple[str, ...]
    quality: str
    size_bytes: int | None
    asked_at: str
    state: str


def _asked(row: sqlite3.Row) -> Asked:
    return Asked(
        id=row["id"],
        username=row["username"],
        rating_key=row["rating_key"],
        season_key=row["season_key"],
        scope=row["scope"],
        language=row["language"],
        service=row["service"],
        arr_id=row["arr_id"],
        season_number=row["season_number"],
        episode_id=row["episode_id"],
        part_id=row["part_id"],
        release_languages=tuple(json.loads(row["release_languages"])),
        quality=row["quality"],
        size_bytes=row["size_bytes"],
        asked_at=row["asked_at"],
        state=row["state"],
    )


def add(
    conn: sqlite3.Connection,
    *,
    username: str,
    rating_key: str,
    season_key: str | None,
    scope: str,
    language: str,
    service: str,
    arr_id: int,
    season_number: int | None,
    episode_id: int | None,
    part_id: str | None,
    release_languages: tuple[str, ...],
    quality: str,
    size_bytes: int | None,
) -> None:
    """Keep a copy asked for; an earlier one of the item in the same language that came
    without it is no longer shown."""
    with conn:
        conn.execute(
            "UPDATE language_copies SET state = 'gone', done_at = ?"
            " WHERE rating_key = ? AND language = ? AND state = 'lacks'",
            (now_iso(), rating_key, language),
        )
        conn.execute(
            "INSERT INTO language_copies (username, rating_key, season_key, scope, language,"
            " service, arr_id, season_number, episode_id, part_id, release_languages, quality,"
            " size_bytes, asked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                username,
                rating_key,
                season_key,
                scope,
                language,
                service,
                arr_id,
                season_number,
                episode_id,
                part_id,
                json.dumps(list(release_languages)),
                quality,
                size_bytes,
                now_iso(),
            ),
        )


def for_item(conn: sqlite3.Connection, rating_key: str, season_key: str | None) -> list[Asked]:
    """The copies asked for of the item, or of the whole of its season, not yet in or that came
    without their language; the latest first."""
    rows = conn.execute(
        "SELECT * FROM language_copies WHERE state IN ('asked', 'lacks')"
        " AND (rating_key = ? OR (scope = 'season' AND season_key = ?)) ORDER BY id DESC",
        (rating_key, season_key),
    ).fetchall()
    return [_asked(row) for row in rows]


def settle(conn: sqlite3.Connection, copy_id: int, state: str) -> None:
    with conn:
        conn.execute(
            "UPDATE language_copies SET state = ?, done_at = ? WHERE id = ? AND state = 'asked'",
            (state, now_iso(), copy_id),
        )
