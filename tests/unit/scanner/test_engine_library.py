"""Tests for show-level keep rules, per-library orphan guard, and unknown library types."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import ParseError

import pytest
import requests
from plexapi.exceptions import PlexApiException

from mediaman.db import init_db
from mediaman.scanner import engine as engine_module
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


class TestScanWithAnUnreadableLibrary:
    """Library 1 is ticked, but Plex does not list it now, or lists it as another type."""

    @pytest.fixture(autouse=True, params=[{}, {"1": "photo"}], ids=["missing", "unsupported"])
    def _types(self, request, conn, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None
        )
        monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            """VALUES ('plex_libraries', '["1", "2"]', '2026')"""
        )
        conn.commit()
        self.types = {**request.param, "2": "movie"}

    def _engine(self, conn, mock_plex, listed=()):
        mock_plex.get_movie_items.side_effect = lambda lib: list(listed) if lib == "2" else []
        return ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1", "2"],
            library_types=self.types,
            secret_key="test-key",
        )

    def test_its_due_deletions_wait_and_the_others_run(self, conn, mock_plex, tmp_path):
        own, other = tmp_path / "own" / "own.mkv", tmp_path / "other" / "other.mkv"
        TestScanWithOneBadLibrary._due(conn, "10", own, 1)
        TestScanWithOneBadLibrary._due(conn, "20", other, 2)

        summary = self._engine(
            conn, mock_plex, [TestScanWithOneBadLibrary._item("20", other)]
        ).run_scan()

        assert (own.exists(), other.exists()) == (True, False)
        assert TestScanWithOneBadLibrary._scheduled(conn) == {"10"}
        assert (summary["errors"], list(summary["left_out"])) == (1, ["1"])

    def test_an_item_at_the_place_of_one_of_its_own_is_not_scheduled(self, conn, mock_plex):
        insert_media_item(
            conn, id="10", plex_rating_key="10", plex_library_id=1, file_path="/media/film.mkv"
        )
        conn.commit()

        self._engine(
            conn,
            mock_plex,
            [
                TestScanWithOneBadLibrary._item("20", "/media/film.mkv"),
                TestScanWithOneBadLibrary._item("21", "/media/other.mkv"),
            ],
        ).run_scan()

        assert TestScanWithOneBadLibrary._scheduled(conn) == {"21"}

    def test_a_sync_does_not_read_it_as_films(self, conn, mock_plex):
        insert_media_item(conn, id="10", plex_rating_key="10", plex_library_id=1)
        conn.commit()

        summary = self._engine(conn, mock_plex).sync_library()

        assert [call.args for call in mock_plex.get_movie_items.call_args_list] == [("2",)]
        assert summary["errors"] == 1

    def test_a_removal_it_finished_is_booked_and_removes_nothing_more(
        self, conn, mock_plex, tmp_path
    ):
        film = tmp_path / "own" / "own.mkv"
        TestScanWithOneBadLibrary._due(conn, "10", film, 1)
        film.unlink()
        sidecar = film.with_suffix(".srt")
        sidecar.write_bytes(b"s")
        conn.execute(
            "UPDATE scheduled_actions SET removed_files = ?",
            (json.dumps({"paths": [str(film)], "bytes": 4}),),
        )
        conn.commit()

        self._engine(conn, mock_plex).run_scan()

        assert TestScanWithOneBadLibrary._scheduled(conn) == set()
        assert sidecar.exists()


_HOUR = timedelta(hours=1)


class TestGoneLibrary:
    """Plex library 1 was removed and made again as 2, the only one ticked."""

    @pytest.fixture(autouse=True)
    def _library(self, conn, tmp_path, monkeypatch, freezer):
        freezer.move_to(datetime(2026, 11, 24, 12, tzinfo=UTC))
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

    def test_its_items_go_and_hold_no_deletion_back_once_a_run_an_hour_later_finds_it_gone_too(
        self, conn, mock_plex, freezer
    ):
        first = self._engine(conn, mock_plex, ["2"]).run_scan()

        assert (first["removed"], first["deleted"]) == (0, 0)
        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

        freezer.tick(_HOUR)
        second = self._engine(conn, mock_plex, ["2"]).run_scan()

        assert (second["removed"], second["deleted"]) == (1, 1)
        assert not self.film.exists()

    def test_its_items_go_with_a_sync_an_hour_later_too(self, conn, mock_plex, freezer):
        assert self._engine(conn, mock_plex, ["2"]).sync_library()["removed"] == 0
        freezer.tick(_HOUR)
        assert self._engine(conn, mock_plex, ["2"]).sync_library()["removed"] == 1

        assert self._ids(conn) == {"20"}

    @pytest.mark.parametrize("run", ["sync_library", "run_scan"])
    def test_its_items_stay_however_many_runs_find_it_gone_within_the_hour(
        self, conn, mock_plex, freezer, run
    ):
        for _ in range(3):
            getattr(self._engine(conn, mock_plex, ["2"]), run)()
            freezer.tick(timedelta(minutes=29))

        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

    def test_a_run_begun_within_the_hour_does_not_confirm_it_however_long_it_takes(
        self, conn, mock_plex, freezer
    ):
        self._engine(conn, mock_plex, ["2"]).run_scan()
        freezer.tick(timedelta(minutes=30))
        engine = self._engine(conn, mock_plex, ["2"])
        mock_plex.get_watch_history.side_effect = lambda key: freezer.tick(_HOUR) and []

        engine.run_scan()

        assert self._ids(conn) == {"10", "20"}

    def test_the_hour_begins_when_the_run_that_first_found_it_gone_says_so(
        self, conn, mock_plex, freezer
    ):
        engine = self._engine(conn, mock_plex, ["2"])
        mock_plex.get_watch_history.side_effect = lambda key: freezer.tick(_HOUR) and []
        engine.run_scan()
        mock_plex.get_watch_history.side_effect = None

        self._engine(conn, mock_plex, ["2"]).run_scan()

        assert self._ids(conn) == {"10", "20"}

    @pytest.mark.parametrize(
        "stored",
        [
            "",
            "not json",
            "5",
            "[1]",
            "{}",
            '{"1": 5}',
            '{"x": "2020-01-01"}',
            '{"1": "2099-01-01"}',
        ],
    )
    def test_a_record_that_cannot_be_read_is_a_first_look(self, conn, mock_plex, freezer, stored):
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('gone_libraries', ?, '2026')",
            (stored,),
        )
        conn.commit()

        self._engine(conn, mock_plex, ["2"]).run_scan()
        assert self._ids(conn) == {"10", "20"}

        freezer.tick(_HOUR)
        self._engine(conn, mock_plex, ["2"]).run_scan()
        assert self._ids(conn) == {"20"}

    def test_nobody_can_write_between_the_read_and_the_write_of_when_it_was_found_gone(
        self, conn, db_path, mock_plex, monkeypatch
    ):
        other = sqlite3.connect(db_path, timeout=0, isolation_level=None)
        real, refused = engine_module.fetch_gone_library_ids, []

        def reading(*args):
            try:
                other.execute("DELETE FROM settings WHERE key = 'gone_libraries'")
            except sqlite3.OperationalError as exc:
                refused.append(str(exc))
            return real(*args)

        monkeypatch.setattr(engine_module, "fetch_gone_library_ids", reading)

        self._engine(conn, mock_plex, ["2"]).sync_library()

        assert refused == ["database is locked"]

    def test_one_listed_again_in_between_starts_over(self, conn, mock_plex, freezer):
        for listed in (["2"], ["1", "2"], ["2"]):
            self._engine(conn, mock_plex, listed).run_scan()
            freezer.tick(_HOUR)

        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

    def test_an_empty_list_in_between_is_not_a_run_that_finds_it_gone(
        self, conn, mock_plex, freezer
    ):
        self._engine(conn, mock_plex, ["2"]).run_scan()
        freezer.tick(_HOUR)
        self._engine(conn, mock_plex, []).run_scan()

        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

    def test_a_keep_of_its_item_still_keeps_the_file(self, conn, mock_plex, freezer):
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
            freezer.tick(_HOUR)

        assert self._ids(conn) == {"20"}
        assert self.film.exists()

    def test_a_library_plex_still_lists_keeps_its_hold_though_unticked(
        self, conn, mock_plex, freezer
    ):
        for _ in range(2):
            self._engine(conn, mock_plex, ["1", "2"]).run_scan()
            freezer.tick(_HOUR)

        assert self._ids(conn) == {"10", "20"}
        assert self.film.exists()

    def test_an_empty_list_of_libraries_prunes_nothing(self, conn, mock_plex):
        engine = self._engine(conn, mock_plex, [])

        engine.run_scan()
        engine.sync_library()

        assert "10" in self._ids(conn)
        assert self.film.exists()

    def test_a_dry_run_prunes_nothing(self, conn, mock_plex, freezer):
        for _ in range(2):
            self._engine(conn, mock_plex, ["2"], dry_run=True).run_scan()
            freezer.tick(_HOUR)

        assert self._ids(conn) == {"10", "20"}

    def test_a_large_one_goes_only_when_a_run_an_hour_later_finds_it_gone_too(
        self, conn, mock_plex, freezer
    ):
        for n in range(11, 20):
            insert_media_item(conn, id=f"{n}", plex_rating_key=f"{n}", plex_library_id=1)
        conn.commit()

        self._engine(conn, mock_plex, ["2"]).run_scan()
        assert len(self._ids(conn)) == 11
        assert self.film.exists()

        freezer.tick(_HOUR)
        self._engine(conn, mock_plex, ["2"]).run_scan()
        assert "10" not in self._ids(conn)
        assert not self.film.exists()


class TestTwinInAGoneLibrary:
    """A due title of library 2 and a kept one of library 1 may be one; Plex lists 1 no more."""

    @pytest.fixture(autouse=True)
    def _library(self, conn, tmp_path, monkeypatch, freezer):
        freezer.move_to(datetime(2026, 11, 24, 12, tzinfo=UTC))
        monkeypatch.setattr(
            "mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None
        )
        monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            """VALUES ('plex_libraries', '["1", "2"]', '2026')"""
        )
        self.due, self.twin = tmp_path / "Two" / "film.mkv", tmp_path / "One" / "film.mkv"
        TestScanWithOneBadLibrary._due(conn, "20", self.due, 2)
        insert_media_item(
            conn, id="10", plex_rating_key="10", plex_library_id=1, file_path=str(self.twin)
        )
        insert_scheduled_action(
            conn,
            media_item_id="10",
            action="protected_forever",
            token="tok-10",
            execute_at=None,
            token_used=True,
        )
        conn.commit()

    def _twins(self, conn, by):
        if by == "ids":
            conn.execute("UPDATE media_items SET tmdb_id = 7")
        elif by == "file":
            conn.execute(
                "UPDATE media_items SET tracked_files = ? WHERE id = '10'",
                (json.dumps([{"path": str(self.due), "size": 4}]),),
            )
        elif by == "film and season":
            conn.execute("UPDATE media_items SET tmdb_id = 7")
            conn.execute("UPDATE media_items SET media_type = 'tv_season' WHERE id = '10'")
        conn.commit()

    def _scan(self, conn, mock_plex, listed=("2",), library_ids=("2",), **kwargs):
        item = TestScanWithOneBadLibrary._item("20", self.due)
        tmdb_id = conn.execute("SELECT tmdb_id FROM media_items WHERE id = '20'").fetchone()
        item["tmdb_id"] = tmdb_id[0] if tmdb_id else None
        own = {**item, "plex_rating_key": "10", "file_path": str(self.twin), "tracked_files": []}
        mock_plex.get_movie_items.side_effect = lambda lib: [item if lib == "2" else own]
        return ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=list(library_ids),
            library_types=dict.fromkeys(listed, "movie"),
            secret_key="test-key",
            **kwargs,
        ).run_scan()

    @pytest.mark.parametrize("library_ids", [["2"], ["1", "2"]], ids=["unticked", "ticked"])
    def test_it_waits_and_is_kept_once_the_library_is_confirmed_gone(
        self, conn, mock_plex, freezer, library_ids
    ):
        self._twins(conn, "ids")

        self._scan(conn, mock_plex, library_ids=library_ids)
        assert TestScanWithOneBadLibrary._scheduled(conn) == {"20"}
        freezer.tick(_HOUR)
        self._scan(conn, mock_plex, library_ids=library_ids)

        assert self.due.exists()
        assert TestScanWithOneBadLibrary._scheduled(conn) == set()

    def test_one_that_shares_a_file_waits(self, conn, mock_plex):
        self._twins(conn, "file")

        self._scan(conn, mock_plex)

        waits = conn.execute(
            "SELECT action FROM audit_log WHERE media_item_id = '20' ORDER BY id"
        ).fetchall()
        assert [row[0] for row in waits] == ["delete_awaiting_scan"]

    def test_the_late_watch_of_a_copy_with_other_files_is_not_said_to_hold_it(
        self, conn, mock_plex
    ):
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = '10'")
        conn.execute(
            "UPDATE media_items SET last_watched_at = ? WHERE id = '10'",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
        )
        self._twins(conn, "ids")

        self._scan(conn, mock_plex)

        said = conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id = '20' "
            "AND action = 'delete_awaiting_scan'"
        ).fetchall()
        assert len(said) == 1
        assert "until that is certain" in said[0][0]

    def test_the_wait_is_told_once(self, conn, mock_plex):
        self._twins(conn, "ids")

        for _ in range(2):
            self._scan(conn, mock_plex)

        told = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'delete_awaiting_scan'"
        ).fetchone()
        assert told[0] == 1

    def test_a_dry_run_does_not_list_it(self, conn, mock_plex):
        self._twins(conn, "ids")

        self._scan(conn, mock_plex, dry_run=True)

        assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'dry_run_skip'").fetchone()

    @pytest.mark.parametrize("by", ["nothing", "film and season"])
    def test_one_that_is_another_title_is_deleted(self, conn, mock_plex, by):
        self._twins(conn, by)

        self._scan(conn, mock_plex)

        assert not self.due.exists()

    def test_it_is_deleted_once_the_library_is_listed_again(self, conn, mock_plex, freezer):
        self._twins(conn, "ids")
        both = ["1", "2"]
        self._scan(conn, mock_plex, listed=both, library_ids=both, dry_run=True)
        self._scan(conn, mock_plex)
        assert self.due.exists()

        freezer.tick(60)
        self._scan(conn, mock_plex, listed=both, library_ids=both)

        assert not self.due.exists()

    def test_a_folder_that_holds_it_waits_too(self, conn, mock_plex, tmp_path):
        self._twins(conn, "ids")
        outer = self.due.parent / "other.mkv"
        TestScanWithOneBadLibrary._due(conn, "21", outer, 2)
        conn.execute("UPDATE media_items SET file_path = ? WHERE id = '21'", (str(outer.parent),))
        conn.commit()

        from mediaman.scanner.deletions import DeletionExecutor

        DeletionExecutor(conn=conn).execute(gone_libraries=[1])

        assert (self.due.exists(), outer.exists()) == (True, True)
        told = conn.execute("SELECT action FROM audit_log WHERE media_item_id = '21'").fetchall()
        assert [row[0] for row in told] == ["delete_awaiting_scan"]


class TestKeptTitleOfAGoneLibraryListedAgainAsAnother:
    """Plex lists library 1 no more; a due title of 2 or 4 has a file of a kept one of 1."""

    @pytest.fixture(autouse=True)
    def _library(self, conn, tmp_path, monkeypatch, freezer):
        freezer.move_to(datetime(2026, 11, 24, 12, tzinfo=UTC))
        monkeypatch.setattr(
            "mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None
        )
        monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            """VALUES ('plex_libraries', '["2", "4"]', '2026')"""
        )
        conn.commit()
        self.folder = tmp_path / "Title"
        self.folder.mkdir()
        self.listed = {"2": [], "4": []}

    def _file(self, name):
        file = self.folder / name
        file.write_bytes(b"data")
        return file

    def _store(self, conn, key, library, path, files, **fields):
        """Store what Plex listed as *key*; in library 1 it is kept, elsewhere due and still listed."""
        tracked = [{"path": str(file), "size": 4} for file in files]
        insert_media_item(
            conn,
            id=key,
            plex_rating_key=key,
            plex_library_id=library,
            file_path=str(path),
            **fields,
        )
        conn.execute(
            "UPDATE media_items SET tracked_files = ? WHERE id = ?", (json.dumps(tracked), key)
        )
        if library == 1:
            return
        insert_scheduled_action(
            conn,
            media_item_id=key,
            action="scheduled_deletion",
            token=f"tok-{key}",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="pending",
        )
        item = TestScanWithOneBadLibrary._item(key, path) | {"tracked_files": tracked}
        if "season_number" in fields:
            item |= {"show_title": key, **fields}
            del item["media_type"]
        self.listed[str(library)].append(item)

    @staticmethod
    def _keep(conn, key):
        insert_scheduled_action(
            conn,
            media_item_id=key,
            action="protected_forever",
            token=f"keep-{key}",
            execute_at=None,
            token_used=True,
        )

    def _scan_four_times(self, conn, mock_plex, freezer, *, ticked=("2", "4"), lists_1=False):
        """Scan the *ticked* libraries; Plex lists 2 and 4, and 1 with no title if *lists_1*."""
        mock_plex.get_movie_items.side_effect = lambda library: self.listed.get(library, [])
        mock_plex.get_show_seasons.side_effect = self.listed.__getitem__
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'plex_libraries'", (json.dumps(ticked),)
        )
        conn.commit()
        for _ in range(4):
            ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=list(ticked),
                library_types={"2": "movie", "4": "show"} | ({"1": "movie"} if lists_1 else {}),
                secret_key="test-key",
            ).run_scan()
            freezer.tick(timedelta(hours=2))

    @staticmethod
    def _schedule(conn, key):
        insert_scheduled_action(
            conn,
            media_item_id=key,
            action="scheduled_deletion",
            token=f"due-{key}",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="pending",
        )

    def test_the_own_due_deletion_of_one_plex_lists_no_more_in_a_listed_library_is_not_carried_out(
        self, conn, mock_plex, freezer
    ):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._store(
            conn, "10", 1, own, [own, shared], last_watched_at=datetime.now(UTC) - timedelta(days=1)
        )
        self._schedule(conn, "10")
        self._store(conn, "20", 2, other, [other, shared])
        conn.commit()
        freezer.tick(60)

        self._scan_four_times(conn, mock_plex, freezer, ticked=("1", "2", "4"), lists_1=True)

        assert own.exists()
        assert not conn.execute("SELECT 1 FROM media_items WHERE id = '10'").fetchone()

    @pytest.mark.parametrize("ticked", [("2", "4"), ("1", "2", "4")], ids=["unticked", "ticked"])
    @pytest.mark.parametrize("held_by", ["keep", "watch"])
    def test_the_own_due_deletion_of_one_held_in_a_gone_library_is_not_carried_out(
        self, conn, mock_plex, freezer, ticked, held_by
    ):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        watched = datetime.now(UTC) - timedelta(days=1) if held_by == "watch" else None
        self._store(conn, "10", 1, own, [own, shared], last_watched_at=watched)
        self._schedule(conn, "10")
        if held_by == "keep":
            insert_kept_show(
                conn, show_rating_key="s1", show_title="Title", action="protected_forever"
            )
            conn.execute("UPDATE media_items SET show_rating_key = 's1' WHERE id = '10'")
        self._store(conn, "20", 2, other, [other, shared])
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer, ticked=ticked)

        assert conn.execute("SELECT 1 FROM media_items WHERE id = '10'").fetchone()
        assert (own.exists(), shared.exists()) == (True, True)

    def test_a_deletion_that_waits_for_a_kept_title_of_a_gone_library_says_so(
        self, conn, mock_plex, freezer
    ):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._store(conn, "10", 1, own, [own, shared])
        self._store(conn, "20", 2, other, [other, shared])
        self._keep(conn, "10")
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer)

        said = conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id = '20' "
            "AND action = 'delete_awaiting_scan'"
        ).fetchall()
        assert len(said) == 1
        assert "is kept" in said[0][0]
        assert "certain" not in said[0][0]

    def test_a_deletion_that_waits_for_a_title_watched_lately_in_a_gone_library_says_so(
        self, conn, mock_plex, freezer
    ):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._store(
            conn, "10", 1, own, [own, shared], last_watched_at=datetime.now(UTC) - timedelta(days=1)
        )
        self._store(conn, "20", 2, other, [other, shared])
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer)

        said = conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id = '20' "
            "AND action = 'delete_awaiting_scan'"
        ).fetchall()
        assert len(said) == 1
        assert "until that watch is too long ago" in said[0][0]
        assert "certain" not in said[0][0]

    @pytest.mark.parametrize("kept", [True, False])
    def test_a_file_of_a_kept_film_stays_though_a_film_with_other_files_has_it_too(
        self, conn, mock_plex, freezer, kept
    ):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._store(conn, "10", 1, own, [own, shared])
        self._store(conn, "20", 2, other, [other, shared])
        if kept:
            self._keep(conn, "10")
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer)

        assert shared.exists() is kept

    def test_a_kept_film_listed_again_as_the_same_film_goes_and_gives_it_its_keep(
        self, conn, mock_plex, freezer
    ):
        film = self._file("film.mkv")
        self._store(conn, "10", 1, film, [film])
        self._store(conn, "20", 2, film, [film])
        self._keep(conn, "10")
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer)

        assert not conn.execute("SELECT 1 FROM media_items WHERE id = '10'").fetchone()
        keeps = conn.execute("SELECT action FROM scheduled_actions WHERE media_item_id = '20'")
        assert [row[0] for row in keeps] == ["protected_forever"]
        assert film.exists()

    def test_a_file_of_a_film_watched_lately_stays_until_the_watch_is_too_long_ago(
        self, conn, mock_plex, freezer
    ):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._store(
            conn, "10", 1, own, [own, shared], last_watched_at=datetime.now(UTC) - timedelta(days=1)
        )
        self._store(conn, "20", 2, other, [other, shared])
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer)
        assert shared.exists()

        freezer.tick(timedelta(days=40))
        self._scan_four_times(conn, mock_plex, freezer)
        assert not shared.exists()

    @pytest.mark.parametrize("kept", ["season", "show"])
    def test_the_files_of_a_kept_season_stay_though_listed_as_another_season(
        self, conn, mock_plex, freezer, kept
    ):
        episode = self._file("E1.mkv")
        season = {"media_type": "tv_season", "show_title": "Title"}
        self._store(
            conn, "10", 1, self.folder, [episode], season_number=1, show_rating_key="s1", **season
        )
        self._store(
            conn, "20", 4, self.folder, [episode], season_number=2, show_rating_key="s2", **season
        )
        if kept == "show":
            insert_kept_show(
                conn, show_rating_key="s1", show_title="Title", action="protected_forever"
            )
        else:
            self._keep(conn, "10")
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer)

        assert episode.exists()

    def test_the_file_of_a_kept_film_stays_though_listed_as_a_season(
        self, conn, mock_plex, freezer
    ):
        film = self._file("film.mkv")
        self._store(conn, "10", 1, film, [film])
        self._store(
            conn,
            "20",
            4,
            self.folder,
            [film],
            media_type="tv_season",
            season_number=1,
            show_rating_key="s2",
        )
        self._keep(conn, "10")
        conn.commit()

        self._scan_four_times(conn, mock_plex, freezer)

        assert film.exists()
