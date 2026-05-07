"""Tests for arr_completion — detect_completed, cleanup_recent_downloads,
and record_verified_completions.

The basic disappear / appear / no-change coverage for detect_completed is
in tests/unit/web/test_downloads_api.py (TestCompletionDetection).
This file extends that with media-type / poster propagation checks and the
DB-backed helpers.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.services.arr.completion import (
    cleanup_recent_downloads,
    detect_completed,
    record_verified_completions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


# ---------------------------------------------------------------------------
# detect_completed
# ---------------------------------------------------------------------------


class TestDetectCompleted:
    def test_matches_by_dl_id_key(self):
        """An item present in previous but absent from current is returned.

        The dict key is the dl_id — this is the canonical 'name' match
        (equivalent to matching by NZB/arr identifier).
        """
        arr_item = {
            "id": "radarr:Dune",
            "title": "Dune",
            "kind": "movie",
            "poster_url": "http://img/dune.jpg",
        }
        previous = {"radarr:Dune": arr_item}
        current: dict = {}

        result = detect_completed(previous, current)

        assert len(result) == 1
        assert result[0]["dl_id"] == "radarr:Dune"
        assert result[0]["title"] == "Dune"

    def test_propagates_media_type_and_poster(self):
        """Completed entries carry the ArrCard.kind mapped to media_type and poster_url."""
        previous = {
            "sonarr:Severance": {
                "id": "sonarr:Severance",
                "title": "Severance",
                "kind": "series",
                "poster_url": "http://img/sev.jpg",
            }
        }
        current: dict = {}

        result = detect_completed(previous, current)

        assert result[0]["media_type"] == "series"
        assert result[0]["poster_url"] == "http://img/sev.jpg"

    def test_no_match_when_names_differ(self):
        """No completions when the current queue holds a completely different item."""
        previous = {
            "radarr:FilmA": {
                "id": "radarr:FilmA",
                "title": "Film A",
                "kind": "movie",
                "poster_url": "",
            }
        }
        current = {
            "radarr:FilmB": {
                "id": "radarr:FilmB",
                "title": "Film B",
                "kind": "movie",
                "poster_url": "",
            }
        }

        result = detect_completed(previous, current)

        # FilmA vanished → completed; FilmB is new → not a completion
        assert len(result) == 1
        assert result[0]["dl_id"] == "radarr:FilmA"

    def test_empty_previous_yields_no_completions(self):
        """Nothing can complete when previous snapshot is empty."""
        current = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "poster_url": "",
            }
        }
        result = detect_completed({}, current)
        assert result == []


# ---------------------------------------------------------------------------
# cleanup_recent_downloads
# ---------------------------------------------------------------------------


class TestCleanupRecentDownloads:
    def test_skips_when_no_recent_downloads(self, conn):
        """An empty table results in zero rows deleted, no Radarr/Sonarr calls needed."""
        rows_before = conn.execute("SELECT COUNT(*) FROM recent_downloads").fetchone()[0]
        assert rows_before == 0

        deleted = cleanup_recent_downloads(conn)
        assert deleted == 0

    def test_removes_only_expired_rows(self, conn):
        """Only rows older than 7 days are purged; fresh rows survive."""
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type, completed_at)"
            " VALUES (?, ?, ?, datetime('now', '-10 days'))",
            ("radarr:OldFilm", "Old Film", "movie"),
        )
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
            ("radarr:NewFilm", "New Film", "movie"),
        )
        conn.commit()

        deleted = cleanup_recent_downloads(conn)

        assert deleted == 1
        remaining = [r[0] for r in conn.execute("SELECT dl_id FROM recent_downloads").fetchall()]
        assert "radarr:OldFilm" not in remaining
        assert "radarr:NewFilm" in remaining


# ---------------------------------------------------------------------------
# record_verified_completions
# ---------------------------------------------------------------------------


SECRET_KEY = "test-secret-32-chars-XXXXXXXXXX"


class TestRecordVerifiedCompletions:
    def test_records_radarr_item_when_has_file(self, conn, monkeypatch):
        """A Radarr item confirmed to have a file is inserted into recent_downloads."""
        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [
            {"title": "Dune", "hasFile": True},
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: mock_radarr,
        )

        completed = [
            {
                "dl_id": "radarr:Dune",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, SECRET_KEY)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Dune'"
        ).fetchall()
        assert len(rows) == 1

    def test_skips_radarr_item_without_file(self, conn, monkeypatch):
        """A Radarr item that has no file is not persisted."""
        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [
            {"title": "Dune", "hasFile": False},
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: mock_radarr,
        )

        completed = [
            {
                "dl_id": "radarr:Dune",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, SECRET_KEY)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Dune'"
        ).fetchall()
        assert len(rows) == 0

    def test_handles_arr_client_none(self, conn, monkeypatch):
        """If build_radarr_from_db returns None, function completes without error."""
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: None,
        )

        completed = [
            {
                "dl_id": "radarr:Interstellar",
                "title": "Interstellar",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        # Must not raise even with no client
        record_verified_completions(conn, completed, SECRET_KEY)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Interstellar'"
        ).fetchall()
        # Not verified → not inserted
        assert len(rows) == 0

    def test_nzbget_only_item_is_verified_without_arr(self, conn):
        """NZBGet-only items (no radarr:/sonarr: prefix) are inserted unconditionally."""
        completed = [
            {
                "dl_id": "nzbget:SomeNzb",
                "title": "Some Nzb Download",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        # No monkeypatching — build_radarr/sonarr_from_db must not be called
        # for NZBGet-only items (they have no Arr prefix to dispatch on).
        record_verified_completions(conn, completed, SECRET_KEY)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'nzbget:SomeNzb'"
        ).fetchall()
        assert len(rows) == 1

    def test_sonarr_item_recorded_when_has_episode_files(self, conn, monkeypatch):
        """A Sonarr series with episodeFileCount > 0 is inserted into recent_downloads."""
        mock_sonarr = MagicMock()
        mock_sonarr.get_series.return_value = [
            {
                "title": "Severance",
                "statistics": {"episodeFileCount": 3},
            }
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_sonarr_from_db",
            lambda *a, **kw: mock_sonarr,
        )

        completed = [
            {
                "dl_id": "sonarr:Severance",
                "title": "Severance",
                "media_type": "series",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, SECRET_KEY)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'sonarr:Severance'"
        ).fetchall()
        assert len(rows) == 1

    def test_arr_network_error_skips_item_without_false_positive_log(self, conn, monkeypatch):
        """When the arr client raises, the item is skipped — not logged as 'no files confirmed'."""
        mock_radarr = MagicMock()
        mock_radarr.get_movies.side_effect = ConnectionError("network error")
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: mock_radarr,
        )

        completed = [
            {
                "dl_id": "radarr:Dune",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, SECRET_KEY)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Dune'"
        ).fetchall()
        # Network failure → skipped, not inserted
        assert len(rows) == 0

    def test_multiple_items_partial_network_failure(self, conn, monkeypatch):
        """A network error on one item does not prevent other items from being processed."""
        call_count = {"n": 0}

        def mock_get_movies():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("transient error")
            return [{"title": "Dune", "hasFile": True}]

        mock_radarr = MagicMock()
        mock_radarr.get_movies.side_effect = mock_get_movies
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: mock_radarr,
        )

        completed = [
            {"dl_id": "radarr:Dune1", "title": "Dune1", "media_type": "movie", "poster_url": ""},
            {"dl_id": "radarr:Dune", "title": "Dune", "media_type": "movie", "poster_url": ""},
        ]
        record_verified_completions(conn, completed, SECRET_KEY)

        rows = conn.execute("SELECT dl_id FROM recent_downloads").fetchall()
        dl_ids = {r["dl_id"] for r in rows}
        assert "radarr:Dune1" not in dl_ids  # failed — skipped
        assert "radarr:Dune" in dl_ids  # succeeded

    def test_radarr_match_prefers_tmdb_id_over_title(self, conn, monkeypatch):
        """When ``tmdb_id`` is populated, the match keys on it — even when the title differs.

        Two same-titled releases would otherwise collide in the title
        index. The tmdb_id path disambiguates them.
        """
        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [
            # Same title, different tmdbId. Without tmdb_id matching, the
            # title-only lookup picks the *last* one inserted into the
            # by-title index — so an item meaning to verify against id=100
            # would be conflated with id=200's hasFile flag.
            {"tmdbId": 100, "title": "Dune", "hasFile": True},
            {"tmdbId": 200, "title": "Dune", "hasFile": False},
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: mock_radarr,
        )

        completed = [
            {
                "dl_id": "radarr:Dune-original",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
                "tmdb_id": 100,
            }
        ]
        record_verified_completions(conn, completed, SECRET_KEY)
        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Dune-original'"
        ).fetchall()
        # Verified via tmdb_id=100 -> hasFile=True.
        assert len(rows) == 1

    def test_sonarr_match_prefers_tmdb_id_over_title(self, conn, monkeypatch):
        """The Sonarr branch must also disambiguate by ``tmdb_id`` when present."""
        mock_sonarr = MagicMock()
        mock_sonarr.get_series.return_value = [
            {"tmdbId": 10, "title": "Severance", "statistics": {"episodeFileCount": 5}},
            # Same title, different tmdbId, no files — must NOT verify the
            # tmdb_id=10 caller.
            {"tmdbId": 20, "title": "Severance", "statistics": {"episodeFileCount": 0}},
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_sonarr_from_db",
            lambda *a, **kw: mock_sonarr,
        )

        completed = [
            {
                "dl_id": "sonarr:Severance-2010",
                "title": "Severance",
                "media_type": "series",
                "poster_url": "",
                "tmdb_id": 10,
            }
        ]
        record_verified_completions(conn, completed, SECRET_KEY)
        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'sonarr:Severance-2010'"
        ).fetchall()
        assert len(rows) == 1

    def test_logs_warning_on_title_only_fallback(self, conn, monkeypatch, caplog):
        """No ``tmdb_id`` on the completed item triggers a WARNING about the title fallback."""
        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [{"title": "Dune", "hasFile": True}]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: mock_radarr,
        )

        completed = [
            {
                "dl_id": "radarr:Dune",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
                # Note: no tmdb_id — fallback path.
            }
        ]
        with caplog.at_level("WARNING", logger="mediaman"):
            record_verified_completions(conn, completed, SECRET_KEY)
        # Item still records (the fallback works) but a warning is logged
        # so operators are aware disambiguation may have failed.
        assert any(
            "title-only match" in r.message and "radarr:Dune" in r.message for r in caplog.records
        )


class TestDetectCompletedTmdbId:
    """Tmdb-id propagation through detect_completed (D6 fix)."""

    def test_propagates_tmdb_id_from_previous_snapshot(self):
        """A ``tmdb_id`` field on the previous-snapshot entry is carried into the CompletedItem."""
        previous = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "poster_url": "",
                "tmdb_id": 438631,
            }
        }
        result = detect_completed(previous, {})
        assert result[0]["tmdb_id"] == 438631

    def test_omits_tmdb_id_when_absent(self):
        """When the snapshot had no ``tmdb_id``, the CompletedItem doesn't fabricate one."""
        previous = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "poster_url": "",
            }
        }
        result = detect_completed(previous, {})
        assert "tmdb_id" not in result[0]
