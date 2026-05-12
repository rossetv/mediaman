"""Manual scan trigger API."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from mediaman.core.audit import security_event
from mediaman.db import (
    finish_scan_run,
    get_db,
    heartbeat_scan_run,
    is_scan_running,
    open_thread_connection,
    start_scan_run,
)
from mediaman.scanner.repository.scheduled_actions import clear_pending_deletions
from mediaman.services.infra.http import SafeHTTPError
from mediaman.services.infra.settings_reader import ConfigDecryptError
from mediaman.services.rate_limit import get_client_ip
from mediaman.services.rate_limit.instances import (
    SCAN_TRIGGER_LIMITER as _SCAN_TRIGGER_LIMITER,
)
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.middleware.rate_limit import rate_limit
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/scan/trigger", response_model=None)
@rate_limit(_SCAN_TRIGGER_LIMITER, key="actor")
def trigger_scan(
    request: Request, admin: str = Depends(get_current_admin)
) -> Response | dict[str, object]:
    """Trigger a manual scan. Returns immediately; scan runs in background thread.

    Spawns a heartbeat thread alongside the scan worker so the
    ``scan_runs`` lease is renewed every minute. The previous code only
    renewed via the manual scan thread itself, so a long Plex / *arr
    round-trip would let the lease lapse and a competing cron scan would
    (correctly) consider the row stale and fire a duplicate run.

    Rate-limited per-admin (3/min, 20/day) so a leaked session cookie
    cannot be used to chain scans against Plex / Sonarr / Radarr.

    Audit-logged via ``security_event(scan.triggered)`` so a compromised
    admin account cannot silently drive scan activity.
    """
    conn = get_db()
    run_id = start_scan_run(conn)
    if run_id is None:
        return {"status": "already_running"}

    security_event(
        conn,
        event="scan.triggered",
        actor=admin,
        ip=get_client_ip(request),
        detail={"run_id": run_id},
    )

    db_path = request.app.state.db_path
    secret_key = request.app.state.config.secret_key

    stop_heartbeat = threading.Event()

    def _heartbeat_loop() -> None:
        try:
            hb_conn = open_thread_connection(db_path)
        except sqlite3.Error:
            logger.exception("manual scan heartbeat thread could not open DB")
            return
        try:
            while not stop_heartbeat.wait(60):
                heartbeat_scan_run(hb_conn, run_id)
        finally:
            try:
                hb_conn.close()
            except sqlite3.Error:  # pragma: no cover — best-effort close
                logger.debug("manual scan heartbeat close failed", exc_info=True)

    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, name="manual-scan-heartbeat", daemon=True
    )
    heartbeat_thread.start()

    def run():
        """Execute the scan in a background thread with its own DB connection.

        Opens a fresh thread-local connection, invokes the full scan pipeline,
        marks the run as done or errored, and always closes the connection on exit.
        """
        thread_conn = open_thread_connection(db_path)
        try:
            from mediaman.scanner.runner import run_scan_from_db

            run_scan_from_db(thread_conn, secret_key, skip_disk_check=True)
            finish_scan_run(thread_conn, run_id, "done")
        except Exception as exc:  # rationale: §6.4 site 2 — background job runner; a single bad scan must not leak a stuck "running" lease.
            with contextlib.suppress(sqlite3.Error):
                finish_scan_run(thread_conn, run_id, "error", str(exc))
            logger.exception("Background scan failed")
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=5)
            thread_conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return {"status": "started"}


@router.get("/api/scan/status")
def scan_status(admin: str = Depends(get_current_admin)) -> dict[str, object]:
    """Return whether a scan is currently running."""
    conn = get_db()
    return {"running": is_scan_running(conn)}


@router.post("/api/scan/clear-scheduled", response_model=None)
def clear_scheduled(
    request: Request, admin: str = Depends(get_current_admin)
) -> Response | dict[str, object]:
    """Delete all pending scheduled_deletion actions.

    Destructive admin action — :func:`clear_pending_deletions` opens
    ``BEGIN IMMEDIATE`` and writes the audit row in the same transaction,
    so a "rows deleted but no audit trail" outcome is impossible. If the
    audit blows up, the delete rolls back.

    Rate-limited per-admin (3/min, 20/day) so a leaked session cookie
    cannot be used to repeatedly nuke the scheduled queue.
    """
    if not _SCAN_TRIGGER_LIMITER.check(admin):
        logger.warning("scan.clear_throttled user=%s", admin)
        return respond_err(
            "too_many_requests", status=429, message="Too many scan triggers — slow down"
        )
    conn = get_db()
    try:
        cleared = clear_pending_deletions(conn, audit_actor=admin, audit_ip=get_client_ip(request))
    except sqlite3.Error:
        logger.exception("scan.clear failed user=%s", admin)
        return respond_err("internal_error", status=500)
    logger.info("Cleared %d scheduled deletions by %s", cleared, admin)
    return {"ok": True, "cleared": cleared}


@router.post("/api/library/sync")
def api_library_sync(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Trigger a manual library sync from Plex.

    Rate-limited per-admin (3/min, 20/day) and audit-logged.
    """
    from mediaman.scanner.runner import run_library_sync

    if not _SCAN_TRIGGER_LIMITER.check(admin):
        logger.warning("library.sync_throttled user=%s", admin)
        return respond_err(
            "too_many_requests", status=429, message="Too many sync triggers — slow down"
        )

    conn = get_db()
    config = request.app.state.config
    try:
        result = run_library_sync(conn, config.secret_key)
        security_event(
            conn,
            event="library.sync",
            actor=admin,
            ip=get_client_ip(request),
            detail={"synced": result.get("synced", 0)},
        )
        return respond_ok({"synced": result.get("synced", 0)})
    except (
        SafeHTTPError,
        requests.RequestException,
        ConfigDecryptError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        logger.exception("Library sync failed")
        security_event(
            conn,
            event="library.sync.failed",
            actor=admin,
            ip=get_client_ip(request),
            detail={"error": str(exc)[:200]},
        )
        return respond_err("sync_failed", status=200, message="Library sync failed")
