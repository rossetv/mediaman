"""Tests for show-level keep rules, per-library orphan guard, and unknown library types."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import ParseError

import pytest
import requests
from plexapi.exceptions import PlexApiException

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from mediaman.services.infra import SafeHTTPError
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


@pytest.fixture
def mock_plex():
    client = MagicMock()
    client.get_movie_items.return_value = []
    client.get_show_seasons.return_value = []
    client.get_watch_history.return_value = []
    client.get_season_watch_history.return_value = []
    return client


class TestShowLevelKeep:
    """Tests for show-level keep rules via kept_shows table."""

    def test_show_rating_key_stored_during_tv_scan(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        mock_plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Test Show",
                "show_title": "Test Show",
                "season_number": 1,
                "added_at": now - timedelta(days=5),
                "file_path": "/media/tv/Test Show/Season 01",
                "file_size_bytes": 5_000_000_000,
                "poster_path": "/library/metadata/600/thumb/1",
                "episode_count": 10,
                "show_rating_key": "599",
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT show_rating_key FROM media_items WHERE id='600'").fetchone()
        assert row is not None
        assert row["show_rating_key"] == "599"

    def test_kept_show_skips_all_seasons(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        insert_kept_show(
            conn, show_rating_key="599", show_title="Test Show", action="protected_forever"
        )

        mock_plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Test Show",
                "show_title": "Test Show",
                "season_number": 1,
                "added_at": now - timedelta(days=90),
                "file_path": "/media/tv/Test Show/Season 01",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
                "episode_count": 10,
                "show_rating_key": "599",
            }
        ]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0
        assert result["skipped"] == 1

    def test_expired_show_snooze_allows_scan(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        past = (now - timedelta(days=1)).isoformat()
        insert_kept_show(
            conn, show_rating_key="599", show_title="Test Show", action="snoozed", execute_at=past
        )

        mock_plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Test Show",
                "show_title": "Test Show",
                "season_number": 1,
                "added_at": now - timedelta(days=90),
                "file_path": "/media/tv/Test Show/Season 01",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
                "episode_count": 10,
                "show_rating_key": "599",
            }
        ]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1

        # Expired row should be cleaned up
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='599'").fetchone()
        assert row is None


class TestPerLibraryOrphanGuard:
    """Orphan-removal numerical safeguards must evaluate
    each library independently. A library that returns 0 items must not
    be wiped just because a sibling library returned 1000.
    """

    def _populate_items(self, conn, lib_id, n):
        for i in range(n):
            insert_media_item(
                conn,
                id=f"item-{lib_id}-{i}",
                title=f"t-{i}",
                plex_library_id=lib_id,
                plex_rating_key=f"item-{lib_id}-{i}",
                added_at="2026-01-01",
                file_path=f"/media/{i}",
                file_size_bytes=1,
            )

    def test_empty_library_does_not_wipe_when_sibling_full(
        self,
        conn,
        mock_plex,
        caplog,
        freezer,
    ):
        """Library 7 had 50 items, library 8 had 0 returned by Plex,
        library 9 returned 1000. Pre-fix the union (1000+0=1000 > 0.10
        of 50+1000=1050 = 105) passed, so library 8's 50 items were
        wiped. Per-library evaluation must protect them.
        """
        self._populate_items(conn, 7, 50)
        self._populate_items(conn, 8, 50)

        def get_movies(lib_id):
            if lib_id == "7":
                return [
                    {
                        "plex_rating_key": f"item-7-{i}",
                        "title": f"t-{i}",
                        "added_at": datetime.now(UTC) - timedelta(days=5),
                        "file_path": f"/media/{i}",
                        "file_size_bytes": 1,
                        "poster_path": None,
                    }
                    for i in range(50)
                ]
            if lib_id == "8":
                return []  # auth hiccup — should be skipped/preserved
            if lib_id == "9":
                return [
                    {
                        "plex_rating_key": f"new-9-{i}",
                        "title": f"new-{i}",
                        "added_at": datetime.now(UTC) - timedelta(days=5),
                        "file_path": f"/media/new-{i}",
                        "file_size_bytes": 1,
                        "poster_path": None,
                    }
                    for i in range(1000)
                ]
            return []

        mock_plex.get_movie_items.side_effect = get_movies

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["7", "8", "9"],
                library_types={"7": "movie", "8": "movie", "9": "movie"},
                secret_key="k",
                min_age_days=999_999,
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                engine.run_scan()

        assert (
            conn.execute("SELECT COUNT(*) FROM media_items WHERE plex_library_id=8").fetchone()[0]
            == 50
        )
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_min_items" in msgs


class TestOrphanGuardTwoScanConfirmation:
    """A suspicious item-count drop is no longer refused forever. The first
    such scan records the library as pending and skips; a second consecutive
    suspicious scan confirms the drop is real and prunes. This lets a
    legitimately shrunk library (last show deleted) reconcile, while a one-off
    Plex glitch still gets absorbed.
    """

    def _populate_items(self, conn, lib_id, n):
        for i in range(n):
            insert_media_item(
                conn,
                id=f"item-{lib_id}-{i}",
                title=f"t-{i}",
                plex_library_id=lib_id,
                plex_rating_key=f"item-{lib_id}-{i}",
                added_at="2026-01-01",
                file_path=f"/media/{i}",
                file_size_bytes=1,
            )

    def _plex_movies(self, lib_id, n):
        return [
            {
                "plex_rating_key": f"item-{lib_id}-{i}",
                "title": f"t-{i}",
                "added_at": datetime.now(UTC) - timedelta(days=5),
                "file_path": f"/media/{i}",
                "file_size_bytes": 1,
                "poster_path": None,
            }
            for i in range(n)
        ]

    def _make_engine(self, conn, mock_plex):
        return ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "movie"},
            secret_key="k",
            min_age_days=999_999,
        )

    def _count_lib2(self, conn):
        return conn.execute("SELECT COUNT(*) FROM media_items WHERE plex_library_id=2").fetchone()[
            0
        ]

    def test_first_suspicious_scan_preserves_items(self, conn, mock_plex, caplog, freezer):
        self._populate_items(conn, 2, 10)
        mock_plex.get_movie_items.return_value = []  # Plex returns nothing

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
            caplog.at_level("WARNING", logger="mediaman"),
        ):
            self._make_engine(conn, mock_plex).run_scan()

        assert self._count_lib2(conn) == 10
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_min_items" in msgs
        assert "awaiting a confirming second scan" in msgs

    def test_second_consecutive_suspicious_scan_prunes(self, conn, mock_plex, caplog, freezer):
        self._populate_items(conn, 2, 10)
        mock_plex.get_movie_items.return_value = []

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
            caplog.at_level("WARNING", logger="mediaman"),
        ):
            self._make_engine(conn, mock_plex).run_scan()  # first: pending
            assert self._count_lib2(conn) == 10
            self._make_engine(conn, mock_plex).run_scan()  # second: confirm + prune

        assert self._count_lib2(conn) == 0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "engine.orphan_guard.confirm" in msgs

    def test_recovery_scan_resets_pending(self, conn, mock_plex, freezer):
        """A glitch (empty) followed by a healthy scan must reset the pending
        marker, so a later glitch starts the two-scan cycle afresh rather than
        immediately pruning."""
        self._populate_items(conn, 2, 10)

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            mock_plex.get_movie_items.return_value = []  # glitch
            self._make_engine(conn, mock_plex).run_scan()  # first suspicious → pending
            assert self._count_lib2(conn) == 10

            mock_plex.get_movie_items.return_value = self._plex_movies(2, 10)  # recovered
            self._make_engine(conn, mock_plex).run_scan()  # healthy → clears pending
            assert self._count_lib2(conn) == 10

            mock_plex.get_movie_items.return_value = []  # glitch again
            self._make_engine(conn, mock_plex).run_scan()  # first-after-reset → skip

        # The lone post-recovery glitch must not have pruned anything.
        assert self._count_lib2(conn) == 10


class TestOrphanGuardPerLibraryConfirmation:
    """Confirmation is PER LIBRARY. ``remove_orphans`` must only prune a
    library on a suspicious scan if THAT specific library was already pending
    from the previous run (a second consecutive suspicious look at it). A
    library that is merely *part of* a larger previously-pending set, but is
    seeing its own first suspicious scan, must be deferred — not pruned.
    """

    def _populate(self, conn, lib_id, n):
        for i in range(n):
            insert_media_item(
                conn,
                id=f"item-{lib_id}-{i}",
                title=f"t-{i}",
                plex_library_id=lib_id,
                plex_rating_key=f"item-{lib_id}-{i}",
                added_at="2026-01-01",
                file_path=f"/media/{lib_id}/{i}",
                file_size_bytes=1,
            )

    def _count(self, conn, lib_id):
        return conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE plex_library_id=?", (lib_id,)
        ).fetchone()[0]

    def test_suspicious_scan_of_lib_not_yet_pending_defers_not_prunes(self, conn):
        """Pending = {1, 2}; this run scans only library 1, suspiciously.
        Library 1 was NOT individually pending (only 2 was, and 1 arrived
        via a larger set in a prior run that no longer holds), so its first
        confirming look must defer — items preserved — and library 1 becomes
        pending. The old subset logic wrongly pruned here.
        """
        from mediaman.scanner.phases.delete import (
            _get_pending_guard_libs,
            _set_pending_guard_libs,
            remove_orphans,
        )

        self._populate(conn, 1, 10)
        # Seed library 2 as the only genuinely-pending library.
        _set_pending_guard_libs(conn, {2})
        conn.commit()

        # Suspicious scan of library 1 (Plex returned nothing).
        removed = remove_orphans(conn, seen_keys=set(), scanned_libs={1})

        assert removed == 0
        assert self._count(conn, 1) == 10  # not pruned — deferred
        # Library 1 is now recorded pending alongside the still-pending 2.
        assert _get_pending_guard_libs(conn) == {1, 2}

    def test_second_suspicious_scan_of_already_pending_lib_prunes(self, conn):
        """When the specific library was already pending, a second
        suspicious scan confirms and prunes it.
        """
        from mediaman.scanner.phases.delete import _set_pending_guard_libs, remove_orphans

        self._populate(conn, 1, 10)
        _set_pending_guard_libs(conn, {1})  # already pending from a prior run
        conn.commit()

        removed = remove_orphans(conn, seen_keys=set(), scanned_libs={1})

        assert removed == 10
        assert self._count(conn, 1) == 0


class TestUnknownLibraryType:
    """A library with no type mapping must be skipped
    loudly, not scanned as 'movie' by default.
    """

    def test_unknown_library_type_is_skipped_with_warning(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={},  # no mapping for "1"
                secret_key="k",
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                result = engine.run_scan()

        assert result["scanned"] == 0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "unknown_library_type" in msgs

    def test_music_library_type_is_skipped_with_warning(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["3"],
                library_types={"3": "music"},
                secret_key="k",
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                result = engine.run_scan()

        assert result["scanned"] == 0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "unsupported_library_type" in msgs


class TestSyncWithOneBadLibrary:
    @pytest.mark.parametrize(
        "error",
        [
            SafeHTTPError(status_code=200, body_snippet="too large", url="http://plex"),
            ValueError("invalid literal"),
            ParseError("no element found"),
        ],
        ids=["too_large", "bad_value", "bad_xml"],
    )
    def test_the_other_libraries_still_sync(self, conn, mock_plex, error):
        def movies(library_id):
            if library_id == "1":
                raise error
            return [
                {
                    "plex_rating_key": "20",
                    "title": "Film",
                    "added_at": datetime(2020, 1, 1, tzinfo=UTC),
                    "file_path": "/media/film.mkv",
                    "file_size_bytes": 1,
                }
            ]

        insert_media_item(conn, id="10", plex_rating_key="10", plex_library_id=1)
        conn.commit()
        mock_plex.get_movie_items.side_effect = movies

        summary = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1", "2"],
            library_types={"1": "movie", "2": "movie"},
            secret_key="test-key",
        ).sync_library()

        assert summary["errors"] == 1
        assert {r[0] for r in conn.execute("SELECT id FROM media_items")} == {"10", "20"}


class TestScanWithOneBadLibrary:
    @pytest.fixture(autouse=True)
    def _no_followups(self, monkeypatch):
        monkeypatch.setattr(
            "mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None
        )
        monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)

    @staticmethod
    def _item(key, path):
        return {
            "plex_rating_key": key,
            "title": key,
            "added_at": datetime(2020, 1, 1, tzinfo=UTC),
            "file_path": str(path),
            "file_size_bytes": 4,
            "tracked_files": [{"path": str(path), "size": 4}],
        }

    def _scan(self, conn, mock_plex, listed, error=None):
        """Scan libraries 1, which answers with *error*, and 2, which lists *listed*."""

        def movies(library_id):
            if library_id == "1":
                raise error or PlexApiException("(500) internal_server_error")
            return listed

        mock_plex.get_movie_items.side_effect = movies
        return ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1", "2"],
            library_types={"1": "movie", "2": "movie"},
            secret_key="test-key",
        ).run_scan()

    @staticmethod
    def _scheduled(conn):
        return {
            r[0]
            for r in conn.execute(
                "SELECT media_item_id FROM scheduled_actions WHERE action = 'scheduled_deletion'"
            )
        }

    @staticmethod
    def _due(conn, key, file, library):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"data")
        insert_media_item(
            conn, id=key, plex_rating_key=key, plex_library_id=library, file_path=str(file)
        )
        conn.execute(
            "UPDATE media_items SET tracked_files = ? WHERE id = ?",
            (json.dumps([{"path": str(file), "size": 4}]), key),
        )
        insert_scheduled_action(
            conn,
            media_item_id=key,
            action="scheduled_deletion",
            token=f"tok-{key}",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="pending",
        )
        conn.commit()

    @pytest.mark.parametrize(
        "error",
        [
            SafeHTTPError(status_code=502, body_snippet="bad gateway", url="http://plex"),
            PlexApiException("(500) internal_server_error"),
            requests.ConnectionError("refused"),
            ParseError("no element found"),
        ],
        ids=["safe_http", "plexapi", "connection", "bad_xml"],
    )
    def test_the_other_libraries_are_scanned_and_its_own_items_stay(self, conn, mock_plex, error):
        insert_media_item(conn, id="10", plex_rating_key="10", plex_library_id=1)
        conn.commit()

        summary = self._scan(conn, mock_plex, [self._item("20", "/media/film.mkv")], error)

        assert summary["errors"] == 1
        assert self._scheduled(conn) == {"20"}
        assert {r[0] for r in conn.execute("SELECT id FROM media_items")} == {"10", "20"}

    def test_an_item_at_the_place_of_one_of_its_own_is_not_scheduled(self, conn, mock_plex):
        insert_media_item(
            conn, id="10", plex_rating_key="10", plex_library_id=1, file_path="/media/film.mkv"
        )
        conn.commit()

        self._scan(
            conn,
            mock_plex,
            [self._item("20", "/media/film.mkv"), self._item("21", "/media/other.mkv")],
        )

        assert self._scheduled(conn) == {"21"}

    def test_due_deletions_run_but_for_its_own(self, conn, mock_plex, tmp_path, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            """VALUES ('plex_libraries', '["1", "2"]', '2026')"""
        )
        own, other = tmp_path / "own" / "own.mkv", tmp_path / "other" / "other.mkv"
        self._due(conn, "10", own, 1)
        self._due(conn, "20", other, 2)

        summary = self._scan(conn, mock_plex, [self._item("20", other)])

        assert (summary["deleted"], own.exists(), other.exists()) == (1, True, False)
        assert self._scheduled(conn) == {"10"}

    def test_a_due_folder_of_another_library_that_holds_one_of_its_own_is_left(
        self, conn, mock_plex, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            """VALUES ('plex_libraries', '["1", "2"]', '2026')"""
        )
        outer, inner = tmp_path / "Box" / "outer.mkv", tmp_path / "Box" / "in" / "inner.mkv"
        self._due(conn, "10", inner, 1)
        self._due(conn, "20", outer, 2)
        conn.execute("UPDATE media_items SET file_path = ? WHERE id = '20'", (str(outer.parent),))
        conn.commit()
        item = {**self._item("20", outer), "file_path": str(outer.parent)}

        self._scan(conn, mock_plex, [item])

        assert inner.exists()
        assert self._scheduled(conn) == {"10", "20"}


class TestGoneLibrary:
    """Plex library 1 was removed and made again as 2, the only one ticked."""

    @pytest.fixture(autouse=True)
    def _library(self, conn, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None
        )
        monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            """VALUES ('plex_libraries', '["2"]', '2026')"""
        )
        self.film = tmp_path / "Film" / "film.mkv"
        TestScanWithOneBadLibrary._due(conn, "20", self.film, 2)
        insert_media_item(
            conn, id="10", plex_rating_key="10", plex_library_id=1, file_path=str(self.film)
        )
        conn.commit()

    def _engine(self, conn, mock_plex, listed, **kwargs):
        mock_plex.get_movie_items.return_value = [TestScanWithOneBadLibrary._item("20", self.film)]
        return ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types=dict.fromkeys(listed, "movie"),
            secret_key="test-key",
            **kwargs,
        )

    @staticmethod
    def _ids(conn):
        return {r[0] for r in conn.execute("SELECT id FROM media_items")}

    def test_its_items_go_and_hold_no_deletion_back_once_a_second_run_finds_it_gone_too(
        self, conn, mock_plex
    ):
        first = self._engine(conn, mock_plex, ["2"]).run_scan()

        assert (first["removed"], first["deleted"]) == (0, 0)
        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

        second = self._engine(conn, mock_plex, ["2"]).run_scan()

        assert (second["removed"], second["deleted"]) == (1, 1)
        assert not self.film.exists()

    def test_its_items_go_with_a_second_sync_too(self, conn, mock_plex):
        assert self._engine(conn, mock_plex, ["2"]).sync_library()["removed"] == 0
        assert self._engine(conn, mock_plex, ["2"]).sync_library()["removed"] == 1

        assert self._ids(conn) == {"20"}

    def test_one_listed_again_in_between_starts_over(self, conn, mock_plex):
        for listed in (["2"], ["1", "2"], ["2"]):
            self._engine(conn, mock_plex, listed).run_scan()

        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

    def test_an_empty_list_in_between_is_not_a_run_that_finds_it_gone(self, conn, mock_plex):
        self._engine(conn, mock_plex, ["2"]).run_scan()
        self._engine(conn, mock_plex, []).run_scan()

        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

    def test_a_keep_of_its_item_still_keeps_the_file(self, conn, mock_plex):
        insert_scheduled_action(
            conn,
            media_item_id="10",
            action="protected_forever",
            token="tok-10",
            execute_at=None,
            token_used=True,
        )
        conn.commit()

        for _ in range(2):
            self._engine(conn, mock_plex, ["2"]).run_scan()

        assert self._ids(conn) == {"20"}
        assert self.film.exists()

    def test_a_library_plex_still_lists_keeps_its_hold_though_unticked(self, conn, mock_plex):
        for _ in range(2):
            self._engine(conn, mock_plex, ["1", "2"]).run_scan()

        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

    def test_an_empty_list_of_libraries_prunes_nothing(self, conn, mock_plex):
        engine = self._engine(conn, mock_plex, [])

        engine.run_scan()
        engine.sync_library()

        assert "10" in self._ids(conn)
        assert self.film.exists()

    def test_a_dry_run_prunes_nothing(self, conn, mock_plex):
        for _ in range(2):
            self._engine(conn, mock_plex, ["2"], dry_run=True).run_scan()

        assert self._ids(conn) == {"10", "20"}

    def test_a_large_one_goes_only_when_a_second_run_finds_it_gone_too(self, conn, mock_plex):
        for n in range(11, 20):
            insert_media_item(conn, id=f"{n}", plex_rating_key=f"{n}", plex_library_id=1)
        conn.commit()

        self._engine(conn, mock_plex, ["2"]).run_scan()
        assert len(self._ids(conn)) == 11
        assert self.film.exists()

        self._engine(conn, mock_plex, ["2"]).run_scan()
        assert "10" not in self._ids(conn)
        assert not self.film.exists()
