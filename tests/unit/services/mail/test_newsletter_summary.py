"""Tests for mediaman.services.mail.newsletter.summary._load_deleted_items.

Focuses on the §13.3 N+1 fix: the batch tmdb_id lookup must issue a single
query regardless of the number of deleted rows, handle duplicate (title,
media_type) pairs, and gracefully handle rows with None titles.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from mediaman.db import init_db
from tests.helpers.factories import insert_audit_log, insert_media_item, insert_suggestion

_SECRET_KEY = "0123456789abcdef" * 4
_BASE_URL = "http://mediaman.local"
_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
# Deletion timestamp within the 7-day window
_DELETED_AT = (_NOW - timedelta(days=2)).isoformat()


def _make_conn(db_path):
    return init_db(str(db_path))


def _insert_deleted_item(conn, *, title: str, media_type: str = "movie", mi_id: str) -> None:
    """Insert a media_item + deleted audit_log entry."""
    insert_media_item(
        conn,
        id=mi_id,
        title=title,
        plex_rating_key=f"rk-{mi_id}",
        file_path=f"/media/{mi_id}.mkv",
        file_size_bytes=1_000_000,
        media_type=media_type,
    )
    insert_audit_log(
        conn,
        media_item_id=mi_id,
        action="deleted",
        space_reclaimed_bytes=1_000_000,
        created_at=_DELETED_AT,
    )


class _QueryCountingConnection:
    """Thin wrapper around sqlite3.Connection that records SQL strings.

    sqlite3.Connection is a C-extension type and cannot be monkeypatched
    via unittest.mock; this delegation wrapper provides the recording hook.
    Only the methods called by _load_deleted_items are delegated.
    """

    def __init__(self, real_conn: sqlite3.Connection) -> None:
        self._conn = real_conn
        self.suggestion_query_count = 0

    def execute(self, sql: str, *args, **kwargs):
        if "suggestions" in sql.lower() and "tmdb_id" in sql.lower():
            self.suggestion_query_count += 1
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestLoadDeletedItemsBatchQuery:
    """§13.3 — the suggestions lookup must be a single batched query, never N per row."""

    def test_single_query_for_multiple_rows(self, db_path):
        """A recording wrapper must see exactly one suggestions query
        regardless of how many deleted rows are processed."""
        real_conn = _make_conn(db_path)

        _insert_deleted_item(real_conn, title="Movie A", mi_id="mi1")
        _insert_deleted_item(real_conn, title="Movie B", mi_id="mi2")
        _insert_deleted_item(real_conn, title="Movie C", mi_id="mi3")

        insert_suggestion(real_conn, title="Movie A", media_type="movie", tmdb_id=101)
        insert_suggestion(real_conn, title="Movie B", media_type="movie", tmdb_id=202)

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        conn = _QueryCountingConnection(real_conn)
        items = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)  # type: ignore[arg-type]

        # Exactly one batched query against suggestions — never one per deleted row.
        assert conn.suggestion_query_count == 1, (
            f"Expected 1 suggestions query, got {conn.suggestion_query_count}"
        )

        titles_with_tmdb = {item["title"]: item["tmdb_id"] for item in items}
        assert titles_with_tmdb.get("Movie A") == 101
        assert titles_with_tmdb.get("Movie B") == 202
        assert titles_with_tmdb.get("Movie C") is None

    def test_duplicate_title_media_type_pairs_deduplicated(self, db_path):
        """When two deleted rows share the same (title, media_type), the batch query
        must still be issued exactly once and both cards must receive the same tmdb_id."""
        real_conn = _make_conn(db_path)

        # Two deletions of the same title (e.g. re-added then deleted again)
        _insert_deleted_item(real_conn, title="Shared Title", media_type="movie", mi_id="mi10")
        _insert_deleted_item(real_conn, title="Shared Title", media_type="movie", mi_id="mi11")
        insert_suggestion(real_conn, title="Shared Title", media_type="movie", tmdb_id=999)

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        conn = _QueryCountingConnection(real_conn)
        items = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)  # type: ignore[arg-type]

        assert conn.suggestion_query_count == 1
        for item in items:
            if item["title"] == "Shared Title":
                assert item["tmdb_id"] == 999, "Both duplicate-title items must get tmdb_id=999"

    def test_none_title_rows_get_tmdb_id_via_detail_extraction(self, db_path):
        """Rows where the media_item title is None use the detail field for title extraction.

        The extracted title must participate in the batch query and receive a
        tmdb_id when a matching suggestions row exists.
        """
        real_conn = _make_conn(db_path)

        # Audit log row where media_item is deleted from DB (LEFT JOIN returns NULL title)
        # but the detail field carries the title for _extract_title_from_detail.
        real_conn.execute(
            "INSERT INTO audit_log"
            " (media_item_id, action, detail, space_reclaimed_bytes, created_at, actor)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("nonexistent_mi", "deleted", "Extracted Title", 500_000, _DELETED_AT, None),
        )
        real_conn.commit()

        insert_suggestion(real_conn, title="Extracted Title", media_type="movie", tmdb_id=888)

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        conn = _QueryCountingConnection(real_conn)
        items = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)  # type: ignore[arg-type]

        # Still one batched query — not two.
        assert conn.suggestion_query_count == 1

        extracted = next((i for i in items if i["title"] == "Extracted Title"), None)
        assert extracted is not None, "Row with title from detail must appear in results"

    def test_no_deleted_rows_returns_empty_list(self, db_path):
        """When there are no deleted items in the past 7 days, return an empty list."""
        conn = _make_conn(db_path)

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        result = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)
        assert result == []
