"""Tests for mediaman.services.mail.newsletter.summary._load_deleted_items."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mediaman.core.deleted_media import record_deleted_media
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


class TestDeletedItemIdentity:
    """A card's type and ids are the ones recorded with the deletion."""

    def _cards(self, conn):
        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        return _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)

    def test_a_season_is_not_taken_for_a_movie_of_the_same_title(self, db_path):
        conn = _make_conn(db_path)
        audit_id = insert_audit_log(
            conn,
            media_item_id="gone",
            action="deleted",
            detail="Deleted: Fargo [rk:1]",
            created_at=_DELETED_AT,
        )
        record_deleted_media(
            conn,
            audit_id,
            media_type="tv_season",
            tmdb_id=60622,
            tvdb_id=269613,
            season_number=2,
            title="Fargo",
        )
        insert_suggestion(conn, title="Fargo", media_type="movie", tmdb_id=275)

        (card,) = self._cards(conn)

        assert (card["title"], card["media_type"], card["tmdb_id"]) == ("Fargo", "tv", 60622)
        assert (card["audit_id"], card["season_number"]) == (audit_id, 2)

    def test_a_deletion_with_no_recorded_identity_gets_no_id_from_its_title(self, db_path):
        conn = _make_conn(db_path)
        _insert_deleted_item(conn, title="Heat", mi_id="mi1")
        insert_suggestion(conn, title="Heat", media_type="movie", tmdb_id=949)

        (card,) = self._cards(conn)

        assert card["tmdb_id"] is None

    def test_no_deleted_rows_returns_empty_list(self, db_path):
        conn = _make_conn(db_path)

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        result = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)
        assert result == []


class TestBuildRedownloadIndexWindowBound:
    """_build_redownload_index must only return records within the 7-day window.

    The index is used to exclude re-downloaded items from the deleted-items
    list. If it scans the full audit log (no ``created_at >=`` bound), it
    would exclude items deleted long ago and re-downloaded before the current
    window — items the newsletter should correctly show as deleted.
    """

    def test_old_redownload_outside_window_not_included(self, db_path):
        """A re-download older than 7 days must NOT appear in the index."""
        conn = _make_conn(db_path)
        week_ago = (_NOW - timedelta(days=7)).isoformat()

        # Insert a re_downloaded row that is 8 days old — outside the 7-day window.
        old_ts = (_NOW - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT INTO audit_log"
            " (media_item_id, action, detail, space_reclaimed_bytes, created_at, actor)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("item-old", "re_downloaded", "", 0, old_ts, None),
        )
        conn.commit()

        from mediaman.services.mail.newsletter.summary import _build_redownload_index

        index = _build_redownload_index(conn, week_ago)
        assert "item-old" not in index, (
            "re-download outside the 7-day window must not appear in the index"
        )

    def test_recent_redownload_inside_window_is_included(self, db_path):
        """A re-download within the 7-day window must appear in the index."""
        conn = _make_conn(db_path)
        week_ago = (_NOW - timedelta(days=7)).isoformat()

        recent_ts = (_NOW - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO audit_log"
            " (media_item_id, action, detail, space_reclaimed_bytes, created_at, actor)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("item-recent", "re_downloaded", "", 0, recent_ts, None),
        )
        conn.commit()

        from mediaman.services.mail.newsletter.summary import _build_redownload_index

        index = _build_redownload_index(conn, week_ago)
        assert "item-recent" in index, (
            "re-download within the 7-day window must appear in the index"
        )
        assert index["item-recent"] == recent_ts


class TestRedownloadedItemsDropOut:
    def _delete_with_identity(
        self, conn, *, mi_id, media_type, tmdb_id=None, tvdb_id=None, season=None
    ):
        _insert_deleted_item(conn, title="Dune", media_type=media_type, mi_id=mi_id)
        audit_id = conn.execute(
            "SELECT id FROM audit_log WHERE media_item_id=?", (mi_id,)
        ).fetchone()[0]
        conn.execute("DELETE FROM media_items WHERE id=?", (mi_id,))
        conn.execute(
            "INSERT INTO deleted_media (audit_id, media_type, tmdb_id, tvdb_id, season_number, title) "
            "VALUES (?, ?, ?, ?, ?, 'Dune')",
            (audit_id, media_type, tmdb_id, tvdb_id, season),
        )
        conn.commit()

    def _redownload(self, conn, token):
        insert_audit_log(
            conn,
            media_item_id=token,
            action="re_downloaded",
            created_at=(_NOW - timedelta(days=1)).isoformat(),
        )

    def _titles(self, conn):
        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        return [i["title"] for i in _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)]

    def test_movie_redownloaded_by_tmdb_id_is_dropped(self, db_path):
        conn = _make_conn(db_path)
        self._delete_with_identity(conn, mi_id="m1", media_type="movie", tmdb_id=438631)
        assert self._titles(conn) == ["Dune"]
        self._redownload(conn, "tmdb:438631")
        assert self._titles(conn) == []

    def test_other_movie_redownload_keeps_the_item(self, db_path):
        conn = _make_conn(db_path)
        self._delete_with_identity(conn, mi_id="m1", media_type="movie", tmdb_id=438631)
        self._redownload(conn, "tmdb:1")
        assert self._titles(conn) == ["Dune"]

    def test_season_redownload_matches_only_its_season(self, db_path):
        conn = _make_conn(db_path)
        self._delete_with_identity(conn, mi_id="s1", media_type="tv_season", tvdb_id=77, season=2)
        self._redownload(conn, "tvdb:77:s1")
        assert self._titles(conn) == ["Dune"]
        self._redownload(conn, "tvdb:77:s2")
        assert self._titles(conn) == []

    def test_search_readd_recorded_by_title_is_dropped(self, db_path):
        conn = _make_conn(db_path)
        self._delete_with_identity(conn, mi_id="m1", media_type="movie", tmdb_id=438631)
        conn.execute("UPDATE deleted_media SET title='Dune'")
        conn.commit()
        insert_audit_log(
            conn,
            media_item_id="DUNE",
            action="downloaded",
            created_at=(_NOW - timedelta(days=1)).isoformat(),
        )
        # the Plex-less deleted row carries no title of its own; the stored one is used
        assert self._titles(conn) == []

    def test_redownload_before_the_deletion_keeps_the_item(self, db_path):
        conn = _make_conn(db_path)
        self._delete_with_identity(conn, mi_id="m1", media_type="movie", tmdb_id=438631)
        insert_audit_log(
            conn,
            media_item_id="tmdb:438631",
            action="re_downloaded",
            created_at=(_NOW - timedelta(days=4)).isoformat(),
        )
        assert self._titles(conn) == ["Dune"]
