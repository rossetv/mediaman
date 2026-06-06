"""Tests for scan engine orphan-cleanup phase and concurrent-scan guard."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from mediaman.db import finish_scan_run, init_db, is_scan_running, start_scan_run
from mediaman.scanner.engine import ScanEngine
from tests.helpers.factories import insert_media_item, insert_scheduled_action


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


class TestOrphanGuard:
    """C31: a scan returning zero (or near-zero) items must not be
    trusted as authoritative — refuse orphan removal and log why."""

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

    def test_empty_scan_against_populated_lib_refuses_orphan_removal(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        self._populate_items(conn, 7, 20)
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["7"],
            library_types={"7": "movie"},
            secret_key="k",
        )
        with caplog.at_level("WARNING", logger="mediaman"):
            removed = engine._remove_orphaned_items(
                seen_keys=set(),
                scanned_libs={7},
            )
        assert removed == 0
        # DB untouched — all 20 items still present.
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 20
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_min_items" in msgs

    def test_huge_drop_triggers_ratio_guard(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        self._populate_items(conn, 8, 200)
        # Only 5 items "found" — that's above the 5-item floor but below
        # the 10 % ratio floor (200 * 0.10 = 20).
        seen = {f"item-8-{i}" for i in range(5)}
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["8"],
            library_types={"8": "movie"},
            secret_key="k",
        )
        with caplog.at_level("WARNING", logger="mediaman"):
            removed = engine._remove_orphaned_items(
                seen_keys=seen,
                scanned_libs={8},
            )
        assert removed == 0
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 200
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_ratio" in msgs

    def test_normal_small_drop_still_removes_orphans(self, conn, mock_plex):
        """A modest drop (e.g. one item removed from a 30-item library)
        must still trigger orphan cleanup — guard only catches collapse."""
        self._populate_items(conn, 9, 30)
        seen = {f"item-9-{i}" for i in range(30) if i != 5}
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["9"],
            library_types={"9": "movie"},
            secret_key="k",
        )
        removed = engine._remove_orphaned_items(
            seen_keys=seen,
            scanned_libs={9},
        )
        assert removed == 1
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 29

    def test_fresh_db_with_tiny_scan_is_allowed(self, conn, mock_plex):
        """If the previous count was zero / tiny (genuine first run), the
        min-items floor must not block first-time orphan cleanup."""
        # No prior items at all → previous_count == 0 → guard inactive.
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["10"],
            library_types={"10": "movie"},
            secret_key="k",
        )
        removed = engine._remove_orphaned_items(
            seen_keys={"x"},
            scanned_libs={10},
        )
        assert removed == 0  # nothing to remove, but not blocked either


class TestHistoryFetchFailureNeverPrunes:
    """R7-H1: a transient watch-history fetch failure must NEVER cause an
    item's ``media_items`` row or its pending ``scheduled_actions`` to be
    pruned by orphan removal.

    The item is still present in Plex — the scan merely failed to fetch its
    history this run. Its rating key is unioned into ``seen_keys`` so orphan
    removal treats it as "seen, just not evaluated", not "gone from Plex".
    """

    def _movie(self, rk):
        return {
            "plex_rating_key": rk,
            "title": f"Film {rk}",
            "added_at": "2026-01-01T00:00:00Z",
            "file_path": f"/media/{rk}.mkv",
        }

    def test_history_fetch_failure_protects_row_and_scheduled_action(
        self, conn, mock_plex, monkeypatch
    ):
        # A library Plex still fully lists: six movies are present in
        # get_movie_items, so the orphan guard's item-count floors are not
        # tripped — this proves the *skip protection*, not the guard, is
        # what saves the failing item.
        lib_id = "42"
        movies = [self._movie(str(i)) for i in range(1, 7)]
        mock_plex.get_movie_items.return_value = movies

        # Pre-populate the DB so orphan removal has rows to consider, and
        # give the soon-to-fail item a pending scheduled deletion that an
        # erroneous prune would cancel (losing its keep-token + grace).
        for movie in movies:
            insert_media_item(
                conn,
                id=movie["plex_rating_key"],
                title=movie["title"],
                plex_library_id=int(lib_id),
                plex_rating_key=movie["plex_rating_key"],
                added_at=movie["added_at"],
                file_path=movie["file_path"],
                file_size_bytes=1,
            )
        insert_scheduled_action(
            conn,
            media_item_id="3",
            action="scheduled_deletion",
            token="tok-3",
            execute_at="2099-01-01T00:00:00Z",
            token_used=False,
            delete_status="pending",
        )
        conn.commit()

        # Item "3" fails its history fetch; every other item succeeds.
        def _history(rating_key):
            if rating_key == "3":
                raise requests.ConnectionError("Plex history unavailable")
            return []

        mock_plex.get_watch_history.side_effect = _history

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[lib_id],
            library_types={lib_id: "movie"},
            secret_key="k",
        )
        engine.run_scan()

        # The failing item's media_items row survives orphan removal.
        row = conn.execute("SELECT id FROM media_items WHERE id='3'").fetchone()
        assert row is not None, "history-fetch failure must not prune the media row"
        # Its pending scheduled deletion is intact (not cancelled).
        action = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='3'"
        ).fetchone()
        assert action is not None, "history-fetch failure must not cancel the scheduled deletion"
        assert action["delete_status"] == "pending"


class TestConcurrentScanGuard:
    """H60: manual and cron scans cannot both run simultaneously.

    The DB-backed ``scan_runs`` table is the single concurrency gate.
    ``start_scan_run`` uses ``BEGIN IMMEDIATE`` so only one caller
    wins the lock; the second gets ``None`` back and must abort.
    """

    def test_concurrent_manual_and_cron_does_not_double_fire(self, conn):
        """Simulates a manual scan already running when the cron fires.

        Only one scan run should be active at a time. The second call to
        ``start_scan_run`` must return ``None``, indicating the cron path
        should skip execution.
        """
        # Simulate the manual scan acquiring the lock first.
        run_id = start_scan_run(conn)
        assert run_id is not None, "First (manual) caller must acquire the lock"
        assert is_scan_running(conn), "Scan should be marked running after start"

        # Simulate the cron path arriving while the manual scan is active.
        cron_run_id = start_scan_run(conn)
        assert cron_run_id is None, (
            "Second (cron) caller must receive None — another scan is already running"
        )

        # Clean up: finish the manual scan run.
        finish_scan_run(conn, run_id, "done")
        assert not is_scan_running(conn), "Scan should no longer be running after finish"

    def test_second_scan_can_start_after_first_finishes(self, conn):
        """After the first scan completes, a new one can acquire the lock."""
        run_id_1 = start_scan_run(conn)
        assert run_id_1 is not None
        finish_scan_run(conn, run_id_1, "done")

        run_id_2 = start_scan_run(conn)
        assert run_id_2 is not None, "New scan must be startable after the previous one finished"
        assert run_id_2 != run_id_1
        finish_scan_run(conn, run_id_2, "done")
