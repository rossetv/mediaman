"""How loudly a library sync reports itself."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner import engine as engine_module
from mediaman.scanner.engine import ScanEngine


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(engine_module, "_last_sync_summary", None, raising=False)


def _film(key: str) -> dict:
    return {
        "plex_rating_key": key,
        "title": f"Film {key}",
        "added_at": datetime(2020, 1, 1, tzinfo=UTC),
        "file_path": f"/media/{key}.mkv",
        "file_size_bytes": 1,
    }


def _sync_level(conn, films, caplog, *, fail=False) -> int:
    plex = MagicMock()
    plex.get_show_seasons.return_value = []
    plex.get_watch_history.return_value = []
    if fail:
        plex.get_movie_items.side_effect = ValueError("bad reply")
    else:
        plex.get_movie_items.return_value = films
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="mediaman.scanner.engine"):
        ScanEngine(
            conn=conn,
            plex_client=plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        ).sync_library()
    (line,) = [r for r in caplog.records if "Library sync complete" in r.getMessage()]
    return line.levelno


def test_a_sync_that_changed_nothing_logs_at_debug_and_one_that_did_logs_at_info(db_path, caplog):
    conn = init_db(str(db_path))
    assert _sync_level(conn, [_film("1")], caplog) == logging.INFO
    assert _sync_level(conn, [_film("1")], caplog) == logging.DEBUG
    assert _sync_level(conn, [_film("1"), _film("2")], caplog) == logging.INFO


def test_a_sync_with_errors_logs_at_info_every_time(db_path, caplog):
    conn = init_db(str(db_path))
    assert _sync_level(conn, [], caplog, fail=True) == logging.INFO
    assert _sync_level(conn, [], caplog, fail=True) == logging.INFO
