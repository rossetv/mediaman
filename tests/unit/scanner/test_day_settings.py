"""Stored cleanup day settings are held to the range the Settings page allows."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from tests.unit.scanner.test_runner import _seed_plex_settings, _set_setting


def _engine_kwargs_for(conn, **stored: int) -> dict:
    _seed_plex_settings(conn, ["1"])
    for key, value in stored.items():
        _set_setting(conn, key, value)
    plex = MagicMock()
    plex.get_libraries.return_value = [{"id": "1", "title": "Movies", "type": "movie"}]
    with (
        patch("mediaman.services.media_meta.plex.PlexClient", return_value=plex),
        patch("mediaman.scanner.engine.ScanEngine") as engine_cls,
        patch("mediaman.crypto.decrypt_value", return_value="fake-token"),
    ):
        from mediaman.scanner.runner import run_scan_from_db

        run_scan_from_db(conn, "test-secret", skip_disk_check=True)
    return engine_cls.call_args.kwargs


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


@pytest.mark.parametrize(
    ("key", "stored", "expected"),
    [
        ("grace_days", 10**12, 90),
        ("grace_days", -365, 1),
        ("grace_days", 0, 1),
        ("min_age_days", 10**12, 365),
        ("min_age_days", -5, 1),
        ("inactivity_days", 10**12, 365),
        ("inactivity_days", 0, 1),
        ("grace_days", 45, 45),
    ],
)
def test_scan_clamps_a_stored_day_setting(conn, key, stored, expected):
    kwargs = _engine_kwargs_for(conn, **{key: stored})

    assert kwargs[key] == expected
    # The scheduling arithmetic accepts what the scan was handed.
    timedelta(days=kwargs["grace_days"])


def test_scan_uses_the_defaults_when_nothing_is_stored(conn):
    kwargs = _engine_kwargs_for(conn)

    assert (kwargs["min_age_days"], kwargs["inactivity_days"], kwargs["grace_days"]) == (30, 30, 14)


def test_clamping_leaves_the_stored_row_alone(conn):
    _engine_kwargs_for(conn, grace_days=10**12)

    row = conn.execute("SELECT value FROM settings WHERE key = 'grace_days'").fetchone()
    assert row["value"] == str(10**12)
