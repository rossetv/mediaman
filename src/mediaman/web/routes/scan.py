"""Manual scan trigger API."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.db import finish_scan_run, get_db, is_scan_running, start_scan_run

logger = logging.getLogger("mediaman")

router = APIRouter()


@router.post("/api/scan/trigger")
def trigger_scan(request: Request, admin: str = Depends(get_current_admin)) -> dict:
    """Trigger a manual scan. Returns immediately; scan runs in background thread."""
    conn = get_db()
    run_id = start_scan_run(conn)
    if run_id is None:
        return {"status": "already_running"}

    db_path = request.app.state.db_path

    def run():
        import sqlite3

        from mediaman.db import _configure_connection

        thread_conn = sqlite3.connect(db_path)
        _configure_connection(thread_conn)
        try:
            from mediaman.config import load_config
            from mediaman.scanner.runner import run_scan_from_db

            config = load_config()
            run_scan_from_db(thread_conn, config.secret_key, skip_disk_check=True)
            finish_scan_run(thread_conn, run_id, "done")
        except Exception as exc:
            try:
                finish_scan_run(thread_conn, run_id, "error", str(exc))
            except Exception:
                pass
            logger.exception("Background scan failed")
        finally:
            thread_conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return {"status": "started"}


@router.get("/api/scan/status")
def scan_status(admin: str = Depends(get_current_admin)) -> dict:
    """Return whether a scan is currently running."""
    conn = get_db()
    return {"running": is_scan_running(conn)}


@router.post("/api/scan/clear-scheduled")
def clear_scheduled(admin: str = Depends(get_current_admin)) -> dict:
    """Delete all pending scheduled_deletion actions."""
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0"
    ).fetchone()[0]
    conn.execute(
        "DELETE FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0"
    )
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
