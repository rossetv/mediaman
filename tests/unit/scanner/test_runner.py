"""Tests for run_scan_from_db disk-threshold filtering logic."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db


# ── helpers ──────────────────────────────────────────────────────────────────


def _set_setting(conn, key, value):
    str_value = json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
    now = datetime.now(timezone.utc).isoformat()
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


# ── tests ─────────────────────────────────────────────────────────────────────


class TestDiskThresholdFiltering:
    def test_filters_libraries_below_threshold(self, conn):
        """Library below threshold is skipped; library above threshold is scanned."""
        _seed_plex_settings(conn, ["1", "2"])
        _set_setting(conn, "disk_thresholds", {
            "1": {"path": "/movies", "threshold": "80"},
            "2": {"path": "/tv", "threshold": "80"},
        })

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
                return {"total_bytes": total, "used_bytes": used, "free_bytes": total - used}
            if path == "/tv":
                # 85% used — above 80% threshold
                total = 1_000_000_000_000
                used = int(total * 0.85)
                return {"total_bytes": total, "used_bytes": used, "free_bytes": total - used}
            raise FileNotFoundError(path)

        with (
            patch("mediaman.services.plex.PlexClient", return_value=_make_plex_mock(plex_libs)) as MockPlex,
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.get_disk_usage", side_effect=fake_disk_usage),
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
        _set_setting(conn, "disk_thresholds", {
            "1": {"path": "/movies", "threshold": "80"},
            "2": {"path": "/tv", "threshold": "80"},
        })

        plex_libs = [
            {"id": "1", "title": "Movies", "type": "movie"},
            {"id": "2", "title": "TV Shows", "type": "show"},
        ]

        engine_instance = _make_engine_mock()

        def fake_disk_usage(path):
            # Both well below threshold — they'd be skipped without the bypass
            total = 1_000_000_000_000
            used = int(total * 0.10)
            return {"total_bytes": total, "used_bytes": used, "free_bytes": total - used}

        with (
            patch("mediaman.services.plex.PlexClient", return_value=_make_plex_mock(plex_libs)),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.get_disk_usage", side_effect=fake_disk_usage),
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db
            run_scan_from_db(conn, "test-secret", skip_disk_check=True)

        call_kwargs = MockEngine.call_args.kwargs
        assert set(call_kwargs["library_ids"]) == {"1", "2"}

    def test_threshold_zero_means_always_scan(self, conn):
        """A threshold of 0 means the library is always included regardless of disk usage."""
        _seed_plex_settings(conn, ["1"])
        _set_setting(conn, "disk_thresholds", {
            "1": {"path": "/movies", "threshold": "0"},
        })

        plex_libs = [{"id": "1", "title": "Movies", "type": "movie"}]
        engine_instance = _make_engine_mock()

        with (
            patch("mediaman.services.plex.PlexClient", return_value=_make_plex_mock(plex_libs)),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.get_disk_usage") as mock_disk,
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db
            run_scan_from_db(conn, "test-secret")

        # get_disk_usage should not have been called — threshold=0 short-circuits
        mock_disk.assert_not_called()
        call_kwargs = MockEngine.call_args.kwargs
        assert call_kwargs["library_ids"] == ["1"]

    def test_missing_path_fails_open(self, conn):
        """If get_disk_usage raises, the library is still included (fail open)."""
        _seed_plex_settings(conn, ["1"])
        _set_setting(conn, "disk_thresholds", {
            "1": {"path": "/nonexistent", "threshold": "80"},
        })

        plex_libs = [{"id": "1", "title": "Movies", "type": "movie"}]
        engine_instance = _make_engine_mock()

        with (
            patch("mediaman.services.plex.PlexClient", return_value=_make_plex_mock(plex_libs)),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch(
                "mediaman.scanner.runner.get_disk_usage",
                side_effect=FileNotFoundError("/nonexistent"),
            ),
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db
            run_scan_from_db(conn, "test-secret")

        call_kwargs = MockEngine.call_args.kwargs
        assert call_kwargs["library_ids"] == ["1"]

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
            patch("mediaman.services.plex.PlexClient", return_value=_make_plex_mock(plex_libs)),
            patch("mediaman.scanner.engine.ScanEngine", return_value=engine_instance) as MockEngine,
            patch("mediaman.scanner.runner.get_disk_usage") as mock_disk,
            patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
        ):
            from mediaman.scanner.runner import run_scan_from_db
            run_scan_from_db(conn, "test-secret")

        mock_disk.assert_not_called()
        call_kwargs = MockEngine.call_args.kwargs
        assert set(call_kwargs["library_ids"]) == {"1", "2"}
