"""Tests for scan_items per-item error isolation (§6.4 / rationale boundary).

Verifies that:
  - a single failing item is counted in summary["errors"] and does not abort
    the loop (remaining items are still processed)
  - expected per-item exceptions (ValueError, TypeError, RuntimeError) are
    contained and do not propagate
  - the error counter does not increment for items that succeed
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from mediaman.db import init_db
from mediaman.scanner._scan_library import scan_items
from mediaman.scanner.arr_dates import ArrDateCache
from mediaman.scanner.fetch import PlexItemFetch


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _make_item(rk: str, days_old: int = 60) -> dict:
    now = datetime.now(UTC)
    return {
        "plex_rating_key": rk,
        "title": f"Item {rk}",
        "media_type": "movie",
        "plex_library_id": 1,
        "added_at": (now - timedelta(days=days_old)).isoformat(),
        "file_path": f"/media/{rk}.mkv",
        "file_size_bytes": 1_000_000,
        "last_watched_at": None,
        "show_rating_key": None,
        "season_number": None,
        "episode_count": None,
        "poster_path": None,
        "show_title": None,
    }


def _make_fetch(rk: str, days_old: int = 60) -> PlexItemFetch:
    return PlexItemFetch(
        item=_make_item(rk, days_old),
        library_id="1",
        media_type="movie",
        watch_history=[],
    )


class _StubEngine:
    """Minimal engine-like stub satisfying the attributes scan_items reads."""

    def __init__(self, conn):
        self._conn = conn
        self._arr_cache = ArrDateCache()
        self._dry_run = True  # dry-run avoids needing a real secret_key / HMAC
        self._grace_days = 14
        self._secret_key = "0123456789abcdef" * 4
        self._min_age_days = 30
        self._inactivity_days = 30

    def _resolve_added_at(self, item: dict) -> datetime:
        raw = item.get("added_at")
        if isinstance(raw, datetime):
            return raw
        from mediaman.core.time import parse_iso_utc

        return parse_iso_utc(str(raw)) or datetime.now(UTC)


class TestScanItemsErrorIsolation:
    """Per-item exception must not abort the scan loop."""

    def test_exception_in_one_item_increments_errors(self, conn):
        """A RuntimeError raised during item evaluation is counted as an error
        and does not propagate.
        """
        engine = _StubEngine(conn)
        summary = {"scanned": 0, "skipped": 0, "scheduled": 0, "errors": 0}
        fetched = [_make_fetch("err-1")]

        with patch(
            "mediaman.scanner._scan_library._evaluate_scan_item",
            side_effect=RuntimeError("injected failure"),
        ):
            scan_items(
                engine,
                fetched,
                media_type_fn=lambda f: "movie",
                evaluate_fn=lambda f, added_at, wh: "skip",
                item_label="Movie",
                library_id="1",
                summary=summary,
            )

        assert summary["errors"] == 1
        assert summary["scanned"] == 1

    def test_exception_in_one_item_does_not_abort_remaining_items(self, conn):
        """A failure in the first item must not prevent the second item from
        being processed — both scanned, one error, one skip.
        """
        engine = _StubEngine(conn)
        summary = {"scanned": 0, "skipped": 0, "scheduled": 0, "errors": 0}

        call_count = 0

        def _failing_then_succeeding(engine, f, media_type_fn, evaluate_fn, seen_keys):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("first item dies")
            # Return a sentinel that scan_items treats as skip
            from mediaman.scanner._scan_library import _SKIP

            return (f.item["plex_rating_key"], _SKIP)

        fetched = [_make_fetch("item-1"), _make_fetch("item-2")]

        with patch(
            "mediaman.scanner._scan_library._evaluate_scan_item",
            side_effect=_failing_then_succeeding,
        ):
            scan_items(
                engine,
                fetched,
                media_type_fn=lambda f: "movie",
                evaluate_fn=lambda f, added_at, wh: "skip",
                item_label="Movie",
                library_id="1",
                summary=summary,
            )

        assert summary["scanned"] == 2
        assert summary["errors"] == 1
        assert summary["skipped"] == 1

    def test_no_exception_leaves_error_count_zero(self, conn):
        """Happy-path: when no items raise, errors stays at zero."""
        engine = _StubEngine(conn)
        summary = {"scanned": 0, "skipped": 0, "scheduled": 0, "errors": 0}

        from mediaman.scanner._scan_library import _SKIP

        with patch(
            "mediaman.scanner._scan_library._evaluate_scan_item",
            return_value=("rk-ok", _SKIP),
        ):
            scan_items(
                engine,
                [_make_fetch("ok-1")],
                media_type_fn=lambda f: "movie",
                evaluate_fn=lambda f, added_at, wh: "skip",
                item_label="Movie",
                library_id="1",
                summary=summary,
            )

        assert summary["errors"] == 0
        assert summary["scanned"] == 1
        assert summary["skipped"] == 1
