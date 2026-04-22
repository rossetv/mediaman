"""Manual scan trigger API."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("mediaman")

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.db import get_db

router = APIRouter()

_scan_lock = threading.Lock()
_scan_running = False


@router.post("/api/scan/trigger")
def trigger_scan(admin: str = Depends(get_current_admin)) -> dict:
    """Trigger a manual scan. Returns immediately; scan runs in background thread."""
    global _scan_running
    with _scan_lock:
        if _scan_running:
            return {"status": "already_running"}
        _scan_running = True

    def run():
        global _scan_running
        try:
            from mediaman.config import load_config
            from mediaman.db import get_db
            from mediaman.scanner.runner import run_scan_from_db

            conn = get_db()
            config = load_config()
            run_scan_from_db(conn, config.secret_key, skip_disk_check=True)
        finally:
            with _scan_lock:
                _scan_running = False

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return {"status": "started"}


@router.get("/api/scan/status")
def scan_status(admin: str = Depends(get_current_admin)) -> dict:
    """Return whether a scan is currently running."""
    return {"running": _scan_running}


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
