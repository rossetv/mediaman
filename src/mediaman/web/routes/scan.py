"""Manual scan trigger API."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.db import (
    finish_scan_run,
    get_db,
    heartbeat_scan_run,
    is_scan_running,
    open_thread_connection,
    start_scan_run,
)

logger = logging.getLogger("mediaman")

router = APIRouter()


@router.post("/api/scan/trigger")
def trigger_scan(request: Request, admin: str = Depends(get_current_admin)) -> dict[str, object]:
    """Trigger a manual scan. Returns immediately; scan runs in background thread.

    Spawns a heartbeat thread alongside the scan worker so the
    ``scan_runs`` lease is renewed every minute (D05 finding 9). The
    previous code only renewed via the manual scan thread itself, so a
    long Plex / *arr round-trip would let the lease lapse and a
    competing cron scan would (correctly) consider the row stale and
    fire a duplicate run.
    """
    conn = get_db()
    run_id = start_scan_run(conn)
    if run_id is None:
        return {"status": "already_running"}

    db_path = request.app.state.db_path
    secret_key = request.app.state.config.secret_key

    stop_heartbeat = threading.Event()

    def _heartbeat_loop() -> None:
        try:
            hb_conn = open_thread_connection(db_path)
        except Exception:
            logger.warning("manual scan heartbeat thread could not open DB", exc_info=True)
            return
        try:
            while not stop_heartbeat.wait(60):
                heartbeat_scan_run(hb_conn, run_id)
        finally:
            try:
                hb_conn.close()
            except Exception:  # pragma: no cover — best-effort close
                logger.debug("manual scan heartbeat close failed", exc_info=True)

    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, name="manual-scan-heartbeat", daemon=True
    )
    heartbeat_thread.start()

    def run():
        thread_conn = open_thread_connection(db_path)
        try:
            from mediaman.scanner.runner import run_scan_from_db

            run_scan_from_db(thread_conn, secret_key, skip_disk_check=True)
            finish_scan_run(thread_conn, run_id, "done")
        except Exception as exc:
            try:
                finish_scan_run(thread_conn, run_id, "error", str(exc))
            except Exception:
                pass
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


@router.post("/api/scan/clear-scheduled")
def clear_scheduled(admin: str = Depends(get_current_admin)) -> dict[str, object]:
    """Delete all pending scheduled_deletion actions."""
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0"
    ).fetchone()[0]
    conn.execute("DELETE FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0")
    conn.commit()
    logger.info("Cleared %d scheduled deletions by %s", count, admin)
    return {"ok": True, "cleared": count}


@router.post("/api/library/sync")
def api_library_sync(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Trigger a manual library sync from Plex."""
    from mediaman.scanner.runner import run_library_sync

    conn = get_db()
    config = request.app.state.config
    try:
        result = run_library_sync(conn, config.secret_key)
        return JSONResponse({"ok": True, "synced": result.get("synced", 0)})
    except Exception as exc:
        logger.warning("Library sync failed: %s", exc)
        return JSONResponse({"ok": False, "error": "Library sync failed"})
