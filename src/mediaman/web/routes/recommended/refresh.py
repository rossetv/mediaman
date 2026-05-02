"""Background recommendation-refresh thread management.

Owns the module-level ``_refresh_result`` shared state, the start/status
endpoints, and the worker that calls into
:func:`mediaman.services.openai.recommendations.persist.refresh_recommendations`.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.db import (
    finish_refresh_run,
    get_db,
    is_refresh_running,
    open_thread_connection,
    start_refresh_run,
)
from mediaman.services.arr.build import build_plex_from_db
from mediaman.services.openai.recommendations.persist import refresh_recommendations
from mediaman.services.openai.recommendations.throttle import (
    RECOMMENDATION_REFRESH_COOLDOWN_HOURS,
    record_manual_refresh,
    refresh_cooldown_remaining,
)

logger = logging.getLogger("mediaman")

router = APIRouter()

# Shared state for the background worker (finding 28). The
# ``_refresh_result`` dict is mutated by the background thread and read
# by the polling endpoint; without a lock the read could observe a
# half-built dict mid-assignment. The lock is fine-grained — held only
# for the swap, not for the work itself.
_refresh_result: dict[str, object] | None = None
_refresh_result_lock = threading.Lock()


def _set_refresh_result(value: dict[str, object] | None) -> None:
    """Atomically replace the shared refresh result."""
    global _refresh_result
    with _refresh_result_lock:
        _refresh_result = value


def _get_refresh_result() -> dict[str, object] | None:
    """Atomically read the shared refresh result."""
    with _refresh_result_lock:
        return _refresh_result


@router.post("/api/recommended/refresh")
def api_refresh_recommendations(
    request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Start a manual recommendation refresh in the background.

    Rate-limited to once per 24 hours to keep OpenAI spend bounded.
    The cooldown is enforced server-side (the UI also hides the button)
    so direct POSTs from a script can't bypass it.
    """
    conn = get_db()

    # Cooldown — enforced before we touch OpenAI / Plex / the lock.
    cooldown = refresh_cooldown_remaining(conn)
    if cooldown is not None:
        next_at = (datetime.now(UTC) + cooldown).isoformat()
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Recommendations were already refreshed in the last "
                    f"{RECOMMENDATION_REFRESH_COOLDOWN_HOURS} hours."
                ),
                "cooldown_seconds": int(cooldown.total_seconds()),
                "next_available_at": next_at,
            },
            status_code=429,
        )

    run_id = start_refresh_run(conn)
    if run_id is None:
        return JSONResponse({"status": "already_running"})

    config = request.app.state.config
    plex = build_plex_from_db(conn, config.secret_key)
    if not plex:
        finish_refresh_run(conn, run_id, "error", "Plex not configured")
        return JSONResponse({"ok": False, "error": "Plex not configured"})

    _db_path = request.app.state.db_path
    _secret_key = config.secret_key

    def run():
        thread_conn = open_thread_connection(_db_path)
        thread_secret_key = _secret_key
        result: dict[str, object]
        manual_refresh_recorded = False
        try:
            plex_client = build_plex_from_db(thread_conn, thread_secret_key)
            if plex_client:
                count = refresh_recommendations(
                    thread_conn, plex_client, manual=True, secret_key=thread_secret_key
                )
                result = {"ok": True, "count": count}
                # Finding 29: cooldown only counts on success — a
                # failure must not lock the user out for 24h. Record
                # the timestamp here, after the work returned without
                # raising.
                record_manual_refresh(thread_conn, datetime.now(UTC))
                manual_refresh_recorded = True
            else:
                result = {"ok": False, "error": "Plex not configured"}
            finish_refresh_run(thread_conn, run_id, "done")
        except Exception as exc:
            logger.exception("Background recommendation refresh failed")
            result = {"ok": False, "error": "Recommendation refresh failed"}
            try:
                finish_refresh_run(thread_conn, run_id, "error", str(exc))
            except Exception:
                pass
        finally:
            _set_refresh_result(result)
            if not manual_refresh_recorded:
                logger.info(
                    "Manual refresh did not complete successfully; "
                    "cooldown timestamp NOT recorded so the user can retry."
                )
            thread_conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return JSONResponse({"status": "started"})


@router.get("/api/recommended/refresh/status")
def api_refresh_status(admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Poll whether the background refresh is still running.

    Also returns cooldown info so the page can keep the button hidden
    after a successful refresh without needing a full reload.
    """
    conn = get_db()
    running = is_refresh_running(conn)
    result = _get_refresh_result()
    cooldown = refresh_cooldown_remaining(conn)
    cooldown_payload: dict[str, object] = {"manual_refresh_available": cooldown is None}
    if cooldown is not None:
        cooldown_payload["cooldown_seconds"] = int(cooldown.total_seconds())
        cooldown_payload["next_available_at"] = (datetime.now(UTC) + cooldown).isoformat()

    if running:
        return JSONResponse({"status": "running", **cooldown_payload})
    if result is not None:
        return JSONResponse({"status": "done", "result": result, **cooldown_payload})
    return JSONResponse({"status": "idle", **cooldown_payload})
