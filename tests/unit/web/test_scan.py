"""Tests for manual scan trigger and scan status API routes."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mediaman.web.routes.scan import router as scan_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action


@pytest.fixture(autouse=True)
def _reset_scan_trigger_limiter():
    """Reset the per-admin scan-trigger limiter so suite ordering does
    not cause the second / third test in a class to hit the daily cap.
    """
    from mediaman.services.rate_limit.instances import SCAN_TRIGGER_LIMITER

    SCAN_TRIGGER_LIMITER.reset()
    yield
    SCAN_TRIGGER_LIMITER.reset()


def _app(app_factory, conn, db_path):
    """scan routes look up ``state.db_path`` for the manual scan worker."""
    return app_factory(scan_router, conn=conn, state_extras={"db_path": str(db_path)})


class TestScanTrigger:
    def test_trigger_requires_auth(self, app_factory, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/scan/trigger")
        assert resp.status_code == 401

    def test_trigger_starts_scan(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        with patch("mediaman.scanner.runner.run_scan_from_db"):
            resp = client.post("/api/scan/trigger")
        assert resp.status_code == 200
        assert resp.json() == {"status": "started"}

    def test_trigger_spawns_heartbeat_thread(self, app_factory, authed_client, conn, db_path):
        """D05 finding 9: the manual scan must start a heartbeat thread
        alongside the scan worker so the lease is renewed during long
        Plex / *arr round-trips. Pre-fix the manual route only had the
        scan worker, so a long scan would let the lease lapse and a
        cron scan would (correctly) treat the row as stale and fire a
        duplicate run.
        """
        import threading

        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)

        before = {t.name for t in threading.enumerate()}
        # Block the scan inside run_scan_from_db so the heartbeat is
        # observable before the scan worker exits.
        scan_running = threading.Event()
        scan_proceed = threading.Event()

        def fake_scan(*args, **kwargs):
            scan_running.set()
            # Wait until the test releases us.
            scan_proceed.wait(timeout=5)
            return {}

        with patch("mediaman.scanner.runner.run_scan_from_db", side_effect=fake_scan):
            resp = client.post("/api/scan/trigger")
            assert resp.status_code == 200

            # Wait for the scan worker to start so the heartbeat
            # thread is guaranteed to have been started too.
            assert scan_running.wait(timeout=5), "scan worker never started"

            # The manual scan heartbeat thread must be alive while
            # the scan is running.
            heartbeat_threads = [
                t for t in threading.enumerate() if t.name == "manual-scan-heartbeat"
            ]
            assert heartbeat_threads, (
                "manual scan must spawn a heartbeat thread named 'manual-scan-heartbeat'"
            )

            # Release the scan worker so it can finish and stop the
            # heartbeat (clean shutdown).
            scan_proceed.set()

        # Best-effort wait for the heartbeat thread to clean up.
        for t in threading.enumerate():
            if t.name in {"manual-scan-heartbeat"} and t.ident not in {
                u.ident for u in [threading.current_thread()]
            }:
                t.join(timeout=5)
        # Ensure no leftover heartbeat threads from this test pollute
        # the rest of the suite.
        leftover = {t.name for t in threading.enumerate()} - before
        # The heartbeat name should NOT be in leftover (clean shutdown).
        assert "manual-scan-heartbeat" not in leftover

    def test_trigger_already_running(self, app_factory, authed_client, conn, db_path):
        """A scan already in the DB blocks a second trigger."""
        from mediaman.db import start_scan_run

        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        # Insert a running scan row directly.
        start_scan_run(conn)
        resp = client.post("/api/scan/trigger")
        assert resp.json() == {"status": "already_running"}

    def test_trigger_crashed_run_eventually_releases(
        self, app_factory, authed_client, conn, db_path
    ):
        """A scan that crashed (no finish_scan_run called) is released after the
        sanity timeout. We simulate this by inserting a stale row."""
        from datetime import datetime, timedelta

        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        # Insert a row older than the 2-hour sanity timeout.
        stale_time = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        conn.execute(
            "INSERT INTO scan_runs (started_at, status) VALUES (?, 'running')",
            (stale_time,),
        )
        conn.commit()
        # The route should treat the stale row as expired and allow a new run.
        with patch("mediaman.scanner.runner.run_scan_from_db"):
            resp = client.post("/api/scan/trigger")
        assert resp.json()["status"] == "started"


class TestScanStatus:
    def test_status_requires_auth(self, app_factory, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/scan/status")
        assert resp.status_code == 401

    def test_status_returns_running_false(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        resp = client.get("/api/scan/status")
        assert resp.status_code == 200
        assert resp.json() == {"running": False}

    def test_status_returns_running_true(self, app_factory, authed_client, conn, db_path):
        """A running scan row is reflected in the status endpoint."""
        from mediaman.db import start_scan_run

        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        start_scan_run(conn)
        resp = client.get("/api/scan/status")
        assert resp.json() == {"running": True}


class TestClearScheduled:
    def _insert_scheduled(
        self, conn, media_item_id: str, action: str = "scheduled_deletion"
    ) -> None:
        from datetime import datetime, timedelta

        insert_scheduled_action(
            conn,
            media_item_id=media_item_id,
            action=action,
            scheduled_at=datetime.now(UTC).isoformat(),
            execute_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
            token=f"tok-{media_item_id}-{action}",
        )

    def _insert_media_item(self, conn, media_id: str) -> None:
        insert_media_item(
            conn,
            id=media_id,
            title=f"Item {media_id}",
            media_type="movie",
            plex_rating_key="rk1",
            file_path="/f",
            file_size_bytes=0,
        )

    def test_clear_scheduled_requires_auth(self, app_factory, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/scan/clear-scheduled")
        assert resp.status_code == 401

    def test_clear_scheduled_deletes_pending_rows(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        self._insert_media_item(conn, "m1")
        self._insert_media_item(conn, "m2")
        self._insert_media_item(conn, "m3")
        self._insert_scheduled(conn, "m1", "scheduled_deletion")
        self._insert_scheduled(conn, "m2", "scheduled_deletion")
        self._insert_scheduled(conn, "m3", "snoozed")

        resp = client.post("/api/scan/clear-scheduled")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["cleared"] == 2

        remaining = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0"
        ).fetchone()[0]
        assert remaining == 0
        snoozed = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE action='snoozed'"
        ).fetchone()[0]
        assert snoozed == 1

    def test_clear_scheduled_with_no_rows(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        resp = client.post("/api/scan/clear-scheduled")
        assert resp.json() == {"ok": True, "cleared": 0}


class TestLibrarySync:
    def test_library_sync_requires_auth(self, app_factory, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/library/sync")
        assert resp.status_code == 401

    def test_library_sync_calls_run_library_sync(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        with patch("mediaman.scanner.runner.run_library_sync", return_value={"synced": 42}):
            resp = client.post("/api/library/sync")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "synced": 42}

    def test_library_sync_returns_error_on_exception(
        self, app_factory, authed_client, conn, db_path
    ):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        import requests as _requests

        with patch(
            "mediaman.scanner.runner.run_library_sync",
            side_effect=_requests.ConnectionError("plex down"),
        ):
            resp = client.post("/api/library/sync")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Audit logging + rate limiting (Domain 03 findings 15-18)
# ---------------------------------------------------------------------------


class TestScanTriggerRateLimit:
    """Manual scan triggers must be rate-limited per admin so a leaked
    session cookie cannot chain scans against Plex / Sonarr / Radarr."""

    def test_fourth_trigger_in_window_is_429(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        # Limiter is 3/min — three OK, fourth gets 429.
        for _ in range(3):
            with patch("mediaman.scanner.runner.run_scan_from_db"):
                resp = client.post("/api/scan/trigger")
            # First "started", subsequent "already_running" since the
            # first lease is still held — but neither should be 429.
            assert resp.status_code == 200
        resp = client.post("/api/scan/trigger")
        assert resp.status_code == 429


class TestScanTriggerAuditLog:
    """A manual scan trigger must write a sec:scan.triggered audit row
    so a compromised admin cannot silently drive scan activity."""

    def test_trigger_writes_security_event(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        with patch("mediaman.scanner.runner.run_scan_from_db"):
            resp = client.post("/api/scan/trigger")
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:scan.triggered'"
        ).fetchall()
        assert rows, "audit row missing for sec:scan.triggered"


class TestClearScheduledAuditLog:
    """The destructive clear-scheduled action must be wrapped in a
    transaction with an audit row that lands atomically with the
    delete — and roll back the delete if the audit insert fails."""

    def test_clear_writes_security_event(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)

        resp = client.post("/api/scan/clear-scheduled")
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:scan.cleared'"
        ).fetchall()
        assert rows, "audit row missing for sec:scan.cleared"
        assert '"count":' in rows[0]["detail"]

    def test_audit_failure_rolls_back_delete(
        self, app_factory, authed_client, conn, db_path, monkeypatch
    ):
        from datetime import datetime, timedelta

        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)

        # Insert a media item + scheduled deletion row.
        insert_media_item(
            conn,
            id="mx",
            title="Item mx",
            media_type="movie",
            plex_rating_key="rk-mx",
            file_path="/f",
            file_size_bytes=0,
        )
        insert_scheduled_action(
            conn,
            media_item_id="mx",
            action="scheduled_deletion",
            scheduled_at=datetime.now(UTC).isoformat(),
            execute_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
            token="tok-mx",
        )

        import sqlite3 as _sqlite3

        from mediaman.core import audit as audit_module

        def boom(*_a, **_k):
            raise _sqlite3.OperationalError("simulated audit failure")

        # Audit is called via lazy ``from mediaman.core.audit import
        # security_event_or_raise`` inside clear_pending_deletions —
        # patch the source so the patched function intercepts the call.
        monkeypatch.setattr(audit_module, "security_event_or_raise", boom)

        resp = client.post("/api/scan/clear-scheduled")
        assert resp.status_code == 500
        # The scheduled row must still be present — the transaction
        # rolled back when the audit insert blew up.
        row = conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id = 'mx'").fetchone()
        assert row is not None


class TestLibrarySyncAuditLog:
    """Manual library sync writes a sec:library.sync row on success."""

    def test_sync_writes_security_event(self, app_factory, authed_client, conn, db_path):
        app = _app(app_factory, conn, db_path)
        client = authed_client(app, conn)
        with patch("mediaman.scanner.runner.run_library_sync", return_value={"synced": 5}):
            resp = client.post("/api/library/sync")
        assert resp.status_code == 200

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:library.sync'"
        ).fetchall()
        assert rows, "audit row missing for sec:library.sync"
