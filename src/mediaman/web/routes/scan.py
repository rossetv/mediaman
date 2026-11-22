from __future__ import annotations

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
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.services.rate_limit.instances import (
    SCAN_TRIGGER_LIMITER as _SCAN_TRIGGER_LIMITER,
)
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.middleware.rate_limit import rate_limit
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

# Each admin-initiated scan-adjacent action gets its own per-actor bucket so the
# three unrelated operations cannot starve one another. Sharing a single bucket
# would let a burst of /api/scan/trigger calls exhaust the budget that the
# docstrings promise independently to clear-scheduled and library/sync. Same
# limits per action: 3 per minute, 20 per day per actor.
_CLEAR_SCHEDULED_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=60, max_per_day=20)
_LIBRARY_SYNC_LIMITER = ActionRateLimiter(max_in_window=3, window_seconds=60, max_per_day=20)

router = APIRouter()


@router.post("/api/scan/trigger", response_model=None)
@rate_limit(_SCAN_TRIGGER_LIMITER, key="actor")
def trigger_scan(
    request: Request, admin: str = Depends(get_current_admin)
) -> Response | dict[str, object]:
    """Trigger a manual scan; returns immediately and the scan runs in a background thread.

    A heartbeat thread renews the scan_runs lease every minute, or a long Plex/*arr
    round-trip would let it lapse and a competing cron scan would fire a duplicate run.
    Rate-limited per admin (3/min, 20/day) so a leaked session cookie cannot chain scans,
    and audit-logged via security_event(scan.triggered).
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

    def run() -> None:
        thread_conn: sqlite3.Connection | None = None
        try:
            thread_conn = open_thread_connection(db_path)
            from mediaman.scanner.runner import run_scan_from_db

            summary = run_scan_from_db(thread_conn, secret_key, skip_disk_check=True)
            reason = summary.get("skipped_reason")
            if reason:
                finish_scan_run(thread_conn, run_id, "skipped", reason)
            else:
                finish_scan_run(thread_conn, run_id, "done")
        except Exception as exc:  # rationale: background job runner; a single bad scan must not leak a stuck "running" lease.
            logger.exception("Background scan failed")
            # Fall back to a fresh connection when the worker one never opened.
            try:
                err_conn = thread_conn or open_thread_connection(db_path)
                try:
                    finish_scan_run(err_conn, run_id, "error", str(exc))
                finally:
                    if err_conn is not thread_conn:
                        err_conn.close()
            except (sqlite3.Error, OSError):
                logger.exception("Could not mark the failed scan run as finished")
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=5)
            if thread_conn is not None:
                thread_conn.close()

    thread = threading.Thread(target=run, name="manual-scan-worker", daemon=True)
    thread.start()
    return {"status": "started"}


@router.get("/api/scan/status")
def scan_status(admin: str = Depends(get_current_admin)) -> dict[str, object]:
    conn = get_db()
    running = is_scan_running(conn)
    if running:
        return {"running": True}
    row = conn.execute("SELECT status, error FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is not None and row["status"] == "skipped":
        return {"running": False, "status": "skipped", "reason": row["error"]}
    if row is not None and row["status"] == "error":
        # The stored error text can be an exception message; show a fixed one.
        return {"running": False, "status": "error", "reason": "see the server log for details"}
    return {"running": False}


@router.post("/api/scan/clear-scheduled", response_model=None)
def clear_scheduled(
    request: Request, admin: str = Depends(get_current_admin)
) -> Response | dict[str, object]:
    """Delete all pending scheduled_deletion actions.

    ``cleared`` counts the deletions that were waiting; ``already_deleting``
    counts those whose removal was already running, whose files may be gone.

    clear_pending_deletions writes the audit row in the same BEGIN IMMEDIATE transaction, so
    a delete without an audit trail is impossible. Rate-limited per admin (3/min, 20/day) so
    a leaked session cookie cannot repeatedly wipe the queue.
    """
    if not _CLEAR_SCHEDULED_LIMITER.check(admin):
        logger.warning("scan.clear_throttled user=%s", _sanitise_log_field(admin))
        return respond_err(
            "too_many_requests", status=429, message="Too many scan triggers — slow down"
        )
    conn = get_db()
    try:
        result = clear_pending_deletions(conn, audit_actor=admin, audit_ip=get_client_ip(request))
    except sqlite3.Error:
        logger.exception("scan.clear failed user=%s", _sanitise_log_field(admin))
        return respond_err("internal_error", status=500)
    logger.info(
        "Cleared %d scheduled deletions (%d already being deleted) by %s",
        result.cleared,
        result.already_deleting,
        admin,
    )
    return {"ok": True, "cleared": result.cleared, "already_deleting": result.already_deleting}


@router.post("/api/library/sync")
def api_library_sync(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    from mediaman.scanner.runner import run_library_sync

    if not _LIBRARY_SYNC_LIMITER.check(admin):
        logger.warning("library.sync_throttled user=%s", _sanitise_log_field(admin))
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
        return respond_err("sync_failed", status=502, message="Library sync failed")
