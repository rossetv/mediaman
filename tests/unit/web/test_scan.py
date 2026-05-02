"""Tests for manual scan trigger and scan status API routes."""

from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.scan import router as scan_router


def _make_app(conn, secret_key: str, db_path: str) -> FastAPI:
    app = FastAPI()
    app.include_router(scan_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    app.state.db_path = db_path
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


class TestScanTrigger:
    def test_trigger_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/scan/trigger")
        assert resp.status_code == 401

    def test_trigger_starts_scan(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        with patch("mediaman.scanner.runner.run_scan_from_db"):
            resp = client.post("/api/scan/trigger")
        assert resp.status_code == 200
        assert resp.json() == {"status": "started"}

    def test_trigger_spawns_heartbeat_thread(self, db_path, secret_key):
        """D05 finding 9: the manual scan must start a heartbeat thread
        alongside the scan worker so the lease is renewed during long
        Plex / *arr round-trips. Pre-fix the manual route only had the
        scan worker, so a long scan would let the lease lapse and a
        cron scan would (correctly) treat the row as stale and fire a
        duplicate run.
        """
        import threading

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)

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

    def test_trigger_already_running(self, db_path, secret_key):
        """A scan already in the DB blocks a second trigger."""
        from mediaman.db import start_scan_run

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        # Insert a running scan row directly.
        start_scan_run(conn)
        resp = client.post("/api/scan/trigger")
        assert resp.json() == {"status": "already_running"}

    def test_trigger_crashed_run_eventually_releases(self, db_path, secret_key):
        """A scan that crashed (no finish_scan_run called) is released after the
        sanity timeout. We simulate this by inserting a stale row."""
        from datetime import datetime, timedelta, timezone

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        # Insert a row older than the 2-hour sanity timeout.
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
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
    def test_status_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/scan/status")
        assert resp.status_code == 401

    def test_status_returns_running_false(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        resp = client.get("/api/scan/status")
        assert resp.status_code == 200
        assert resp.json() == {"running": False}

    def test_status_returns_running_true(self, db_path, secret_key):
        """A running scan row is reflected in the status endpoint."""
        from mediaman.db import start_scan_run

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        start_scan_run(conn)
        resp = client.get("/api/scan/status")
        assert resp.json() == {"running": True}


class TestClearScheduled:
    def _insert_scheduled(
        self, conn, media_item_id: str, action: str = "scheduled_deletion"
    ) -> None:
        from datetime import datetime, timedelta, timezone

        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (
                media_item_id,
                action,
                datetime.now(timezone.utc).isoformat(),
                (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                f"tok-{media_item_id}-{action}",
            ),
        )
        conn.commit()

    def _insert_media_item(self, conn, media_id: str) -> None:
        from datetime import datetime, timezone

        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, plex_rating_key, "
            "added_at, file_path, file_size_bytes) VALUES (?, ?, 'movie', 1, 'rk1', ?, '/f', 0)",
            (media_id, f"Item {media_id}", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    def test_clear_scheduled_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/scan/clear-scheduled")
        assert resp.status_code == 401

    def test_clear_scheduled_deletes_pending_rows(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
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

    def test_clear_scheduled_with_no_rows(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        resp = client.post("/api/scan/clear-scheduled")
        assert resp.json() == {"ok": True, "cleared": 0}


class TestLibrarySync:
    def test_library_sync_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/library/sync")
        assert resp.status_code == 401

    def test_library_sync_calls_run_library_sync(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        with patch("mediaman.scanner.runner.run_library_sync", return_value={"synced": 42}):
            resp = client.post("/api/library/sync")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "synced": 42}

    def test_library_sync_returns_error_on_exception(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key, str(db_path))
        client = _auth_client(app, conn)
        with patch(
            "mediaman.scanner.runner.run_library_sync", side_effect=RuntimeError("plex down")
        ):
            resp = client.post("/api/library/sync")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
