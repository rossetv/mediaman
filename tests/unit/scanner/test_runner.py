"""Tests for run_scan_from_db disk-threshold filtering logic."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.scanner import runner
from mediaman.scanner.runner import _filter_libraries_by_disk
from mediaman.services.infra import SSRFRefused

# Capture the namedtuple type ``shutil.disk_usage`` returns ONCE at import
# time. Calling ``shutil.disk_usage(path)`` from inside a side_effect would
# recurse into the patched mock and raise ``OSError`` for any path the
# fake table doesn't recognise, exercising the fail-open path instead of
# the threshold logic the test wants to assert.
_DiskUsage = type(shutil.disk_usage("/"))


def _set_setting(conn, key, value):
    str_value = json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str_value, now),
    )
    conn.commit()


def _make_plex_mock(libraries: list[dict]) -> MagicMock:
    """Return a PlexClient mock whose get_libraries() returns *libraries*."""
    plex = MagicMock()
    plex.get_libraries.return_value = libraries
    return plex


def _make_engine_mock() -> MagicMock:
    engine = MagicMock()
    engine.run_scan.return_value = {"scheduled": 0, "deleted": 0}
    return engine


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _seed_plex_settings(conn, lib_ids: list[str]):
    """Insert the minimum required Plex settings into the DB."""
    _set_setting(conn, "plex_url", "http://localhost:32400")
    _set_setting(conn, "plex_token", "fake-token")
    _set_setting(conn, "plex_libraries", lib_ids)


class TestDiskThresholdFiltering:
    def test_filters_libraries_below_threshold(self, conn):
        """Library below threshold is skipped; library above threshold is scanned."""
        _seed_plex_settings(conn, ["1", "2"])
        _set_setting(
            conn,
            "disk_thresholds",
            {
                "1": {"path": "/movies", "threshold": "80"},
                "2": {"path": "/tv", "threshold": "80"},
            },
        )

        plex_libs = [
            {"id": "1", "title": "Movies", "type": "movie"},
            {"id": "2", "title": "TV Shows", "type": "show"},
        ]

        engine_instance = _make_engine_mock()

        def fake_disk_usage(path):
            if path == "/movies":
                # 38% used — below 80% threshold
                total = 1_000_000_000_000
                used = int(total * 0.38)
                free = total - used
                return _DiskUsage(total, used, free)
            if path == "/tv":
                # 85% used — above 80% threshold
                total = 1_000_000_000_000
                used = int(total * 0.85)
                free = total - used
                return _DiskUsage(total, used, free)
            raise FileNotFoundError(path)

        with (
            patch(
                "mediaman.services.media_meta.plex.PlexClient",
                return_value=_make_plex_mock(plex_libs),
            ),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.shutil.disk_usage", side_effect=fake_disk_usage),
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db

            run_scan_from_db(conn, "test-secret")

        call_kwargs = MockEngine.call_args.kwargs
        assert call_kwargs["library_ids"] == ["2"], (
            "Only library '2' (TV, 85% used) should pass the 80% threshold"
        )

    def test_skip_disk_check_passes_all_libraries(self, conn):
        """When skip_disk_check=True, both libraries are passed through regardless of disk."""
        _seed_plex_settings(conn, ["1", "2"])
        _set_setting(
            conn,
            "disk_thresholds",
            {
                "1": {"path": "/movies", "threshold": "80"},
                "2": {"path": "/tv", "threshold": "80"},
            },
        )

        plex_libs = [
            {"id": "1", "title": "Movies", "type": "movie"},
            {"id": "2", "title": "TV Shows", "type": "show"},
        ]

        engine_instance = _make_engine_mock()

        def fake_disk_usage(path):
            # Both well below threshold — they'd be skipped without the bypass
            total = 1_000_000_000_000
            used = int(total * 0.10)
            free = total - used
            return _DiskUsage(total, used, free)

        with (
            patch(
                "mediaman.services.media_meta.plex.PlexClient",
                return_value=_make_plex_mock(plex_libs),
            ),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.shutil.disk_usage", side_effect=fake_disk_usage),
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db

            run_scan_from_db(conn, "test-secret", skip_disk_check=True)

        call_kwargs = MockEngine.call_args.kwargs
        assert set(call_kwargs["library_ids"]) == {"1", "2"}

    def test_threshold_zero_means_always_scan(self, conn):
        """A threshold of 0 means the library is always included regardless of disk usage."""
        _seed_plex_settings(conn, ["1"])
        _set_setting(
            conn,
            "disk_thresholds",
            {
                "1": {"path": "/movies", "threshold": "0"},
            },
        )

        plex_libs = [{"id": "1", "title": "Movies", "type": "movie"}]
        engine_instance = _make_engine_mock()

        with (
            patch(
                "mediaman.services.media_meta.plex.PlexClient",
                return_value=_make_plex_mock(plex_libs),
            ),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.shutil.disk_usage") as mock_disk,
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db

            run_scan_from_db(conn, "test-secret")

        # shutil.disk_usage should not have been called — threshold=0 short-circuits
        mock_disk.assert_not_called()
        call_kwargs = MockEngine.call_args.kwargs
        assert call_kwargs["library_ids"] == ["1"]

    def test_missing_path_fails_open(self, conn):
        """If get_disk_usage raises, the library is still included (fail open)."""
        _seed_plex_settings(conn, ["1"])
        _set_setting(
            conn,
            "disk_thresholds",
            {
                "1": {"path": "/nonexistent", "threshold": "80"},
            },
        )

        plex_libs = [{"id": "1", "title": "Movies", "type": "movie"}]
        engine_instance = _make_engine_mock()

        with (
            patch(
                "mediaman.services.media_meta.plex.PlexClient",
                return_value=_make_plex_mock(plex_libs),
            ),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch(
                "mediaman.scanner.runner.shutil.disk_usage",
                side_effect=FileNotFoundError("/nonexistent"),
            ),
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db

            run_scan_from_db(conn, "test-secret")

        call_kwargs = MockEngine.call_args.kwargs
        assert call_kwargs["library_ids"] == ["1"]

    @pytest.mark.parametrize(
        "setting",
        [
            {"1": "x"},
            {"1": 5},
            {"1": ["/movies", 80]},
            {"1": {"path": 5, "threshold": 80}},
            {"1": {"path": ["/movies"], "threshold": 80}},
            {"1": {"path": "/mo\0vies", "threshold": 80}},
            {"1": {"path": "/movies", "threshold": "x"}},
            {"1": {"path": "/movies", "threshold": [80]}},
            {"1": {"path": "/movies", "threshold": float("inf")}},
        ],
        ids=str,
    )
    def test_a_library_whose_threshold_cannot_be_read_is_not_scanned(self, conn, setting):
        _set_setting(conn, "disk_thresholds", setting)

        assert _filter_libraries_by_disk(conn, ["1", "2"], {}) == (["2"], ["1"])

    @pytest.mark.parametrize("setting", ['{"1": ', '["1"]', '"x"', "5"])
    def test_no_library_is_scanned_when_no_threshold_can_be_read(self, conn, setting):
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('disk_thresholds', ?, '2026')",
            (setting,),
        )

        assert _filter_libraries_by_disk(conn, ["1", "2"], {}) == ([], ["1", "2"])

    @pytest.mark.parametrize(
        "setting",
        [{}, {"1": None}, {"1": {}}, {"1": {"path": "/movies"}}, {"1": {"threshold": 80}}],
        ids=str,
    )
    def test_a_library_with_no_threshold_is_scanned(self, conn, setting):
        _set_setting(conn, "disk_thresholds", setting)

        assert _filter_libraries_by_disk(conn, ["1"], {}) == (["1"], [])

    def test_a_library_left_out_is_told_once_in_the_history(self, conn):
        _seed_plex_settings(conn, ["1", "2"])
        _set_setting(conn, "disk_thresholds", {"1": {"path": "/movies", "threshold": "x"}})
        plex = _make_plex_mock(
            [{"id": i, "title": f"Library {i}", "type": "movie"} for i in ("1", "2")]
        )
        engine = _make_engine_mock()

        with (
            patch("mediaman.services.media_meta.plex.PlexClient", return_value=plex),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine) as made,
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db

            for _ in range(2):
                run_scan_from_db(conn, "test-secret")

        assert made.call_args.kwargs["library_ids"] == ["2"]
        [told] = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'library_left_out'"
        ).fetchall()
        assert "'Library 1'" in told[0]

    def test_a_library_left_out_again_after_it_was_scanned_is_told_again(self, conn):
        for left_out in (["1"], ["1", "2"], [], ["2"]):
            runner._audit_left_out(conn, left_out, {"1": "Movies", "2": "TV"})

        told = conn.execute(
            "SELECT media_item_id FROM audit_log WHERE action = 'library_left_out' ORDER BY id"
        )
        assert [row[0] for row in told] == ["_library:1", "_library:2", "_library:2"]

    def test_a_library_named_anime_holds_anime_whatever_the_case_of_its_name(self, conn):
        _seed_plex_settings(conn, ["1"])
        plex = _make_plex_mock([{"id": "1", "title": "Anime", "type": "show"}])
        plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "10",
                "title": "Season 1",
                "show_title": "Show",
                "season_number": 1,
                "file_path": "/anime/Show/Season 1",
            }
        ]
        plex.get_season_watch_history.return_value = []
        with (
            patch("mediaman.services.media_meta.plex.PlexClient", return_value=plex),
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            runner.run_library_sync(conn, "test-secret")

        assert [r[0] for r in conn.execute("SELECT media_type FROM media_items")] == [
            "anime_season"
        ]

    def test_no_thresholds_configured_scans_all(self, conn):
        """When no disk_thresholds setting exists, all libraries are passed through."""
        _seed_plex_settings(conn, ["1", "2"])
        # Deliberately do NOT set disk_thresholds

        plex_libs = [
            {"id": "1", "title": "Movies", "type": "movie"},
            {"id": "2", "title": "TV Shows", "type": "show"},
        ]
        engine_instance = _make_engine_mock()

        with (
            patch(
                "mediaman.services.media_meta.plex.PlexClient",
                return_value=_make_plex_mock(plex_libs),
            ),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.shutil.disk_usage") as mock_disk,
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db

            run_scan_from_db(conn, "test-secret")

        mock_disk.assert_not_called()
        call_kwargs = MockEngine.call_args.kwargs
        assert set(call_kwargs["library_ids"]) == {"1", "2"}


class TestPlexSSRFRefusalAtScan:
    """When PlexClient construction is refused by the SSRF guard at
    use-time, the runner must skip the scan rather than crash, and no
    ScanEngine must be built. This is the use-time revalidation safety
    net for a stored URL that has since started resolving to a metadata
    or internal endpoint."""

    def test_run_scan_skips_with_reason_when_plex_url_refused(self, conn):
        """A stored Plex URL whose host has been rebound to an internal
        address must cause the scan to be skipped."""
        _seed_plex_settings(conn, ["1"])

        with (
            patch(
                "mediaman.scanner.runner._build_plex",
                side_effect=SSRFRefused("SSRF guard refused plex_url"),
            ),
            patch("mediaman.scanner.engine.ScanEngine") as MockEngine,
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db

            result = run_scan_from_db(conn, "test-secret")

        # No engine was built, no scan executed.
        MockEngine.assert_not_called()
        assert result == {"skipped_reason": "Plex could not be reached"}

    def test_run_library_sync_returns_empty_when_plex_url_refused(self, conn):
        """Same use-time guard for the lighter ``run_library_sync`` path."""
        _seed_plex_settings(conn, ["1"])

        with (
            patch(
                "mediaman.scanner.runner._build_plex",
                side_effect=SSRFRefused("SSRF guard refused plex_url"),
            ),
            patch("mediaman.scanner.engine.ScanEngine") as MockEngine,
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_library_sync

            result = run_library_sync(conn, "test-secret")

        MockEngine.assert_not_called()
        assert result == {}


class TestPlexClientUnbuildable:
    def test_configured_plex_without_client_skips_with_reason(self, conn):
        _seed_plex_settings(conn, ["1"])
        with (
            patch("mediaman.scanner.runner._build_plex_client", return_value=None),
            patch("mediaman.scanner.engine.ScanEngine") as MockEngine,
        ):
            from mediaman.scanner.runner import run_scan_from_db

            result = run_scan_from_db(conn, "test-secret")

        MockEngine.assert_not_called()
        assert result == {"skipped_reason": "Plex could not be reached"}
