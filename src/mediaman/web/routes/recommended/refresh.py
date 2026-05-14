"""Background recommendation-refresh thread management.

Owns the module-level ``_refresh_result`` shared state, the start/status
endpoints, and the worker that calls into
:func:`mediaman.services.openai.recommendations.persist.refresh_recommendations`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import (
    finish_refresh_run,
    get_db,
    heartbeat_refresh_run,
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
from mediaman.web.auth.middleware import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter()

# Heartbeat the DB lease every minute while the worker is running so a
# refresh that legitimately takes longer than _JOB_HEARTBEAT_STALE_SECONDS
# (5 min — see db/connection.py) is not mistakenly treated as crashed by
# is_refresh_running(). Without this the status endpoint would return
# "idle" mid-refresh, the JS poll would never resolve, and the user would
# see the button stuck on "Refreshing…" forever.
_HEARTBEAT_INTERVAL_SECONDS = 60

# Module-level mutable state for the background refresh worker. Mediaman
# is single-worker by design (§1.12 in CODE_GUIDELINES.md), so in-process
# state is sufficient; the DB lease (refresh_runs table) is the
# cross-restart truth. ``_refresh_result`` carries the final payload to
# the status endpoint; ``_refresh_thread`` lets the status endpoint
# detect "worker is still alive in this process" when the DB lease check
# disagrees (e.g. a transient DB error skipped a heartbeat). Both
# reads/writes happen under the same lock so the two fields are never
# observed torn against each other.
_refresh_result: dict[str, object] | None = None
_refresh_thread: threading.Thread | None = None
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


def _set_refresh_thread(thread: threading.Thread | None) -> None:
    """Atomically replace the shared worker-thread reference."""
    global _refresh_thread
    with _refresh_result_lock:
        _refresh_thread = thread


def _refresh_thread_alive() -> bool:
    """Return True if a worker thread is alive in this process."""
    with _refresh_result_lock:
        return _refresh_thread is not None and _refresh_thread.is_alive()


def _heartbeat_lease(db_path: str, run_id: int, stop_event: threading.Event) -> None:
    """Renew the DB lease every minute until the worker signals stop.

    Runs on a dedicated daemon thread with its own thread-local DB
    connection (SQLite connections must not cross thread boundaries).
    Keeps the lease alive while the worker is busy; without this,
    refreshes longer than 5 min flip ``is_refresh_running()`` to False,
    the status endpoint returns "idle", and the JS poll never resolves.
    """
    hb_conn = open_thread_connection(db_path)
    try:
        while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
            try:
                heartbeat_refresh_run(hb_conn, run_id)
            except sqlite3.Error:
                # A transient DB hiccup skipping one heartbeat is tolerable —
                # the run lease lasts 5 min so a missed tick is harmless.
                logger.warning(
                    "Heartbeat DB write failed for run_id=%s; skipping tick",
                    run_id,
                    exc_info=True,
                )
    finally:
        hb_conn.close()


def _run_refresh(db_path: str, secret_key: str, run_id: int, stop_event: threading.Event) -> None:
    """Execute the recommendation refresh in a background thread.

    Opens a dedicated thread-local DB connection, calls the refresh pipeline,
    records the run result (done or error), publishes it via
    :func:`_set_refresh_result`, signals the heartbeat thread to stop, and
    closes the connection on exit. The cooldown timestamp is only recorded
    on a successful non-zero result to avoid locking the user out after a
    transient OpenAI failure.
    """
    thread_conn = open_thread_connection(db_path)
    thread_secret_key = secret_key
    result: dict[str, object]
    manual_refresh_recorded = False
    try:
        plex_client = build_plex_from_db(thread_conn, thread_secret_key)
        if plex_client:
            count = refresh_recommendations(
                thread_conn, plex_client, manual=True, secret_key=thread_secret_key
            )
            if count > 0:
                result = {"ok": True, "count": count}
                # Cooldown only counts on success — a failure must not
                # lock the user out for 24h. Record the timestamp here,
                # after the work returned without raising.
                record_manual_refresh(thread_conn, now_utc())
                manual_refresh_recorded = True
                finish_refresh_run(thread_conn, run_id, "done")
            else:
                # Zero rows generated means OpenAI returned nothing
                # usable (quota, key, network, or web-search batch
                # rejected). Treat as failure: don't burn the 24h
                # cooldown, surface a real error so the user can retry.
                result = {
                    "ok": False,
                    "error": (
                        "OpenAI returned no recommendations. Check the OpenAI "
                        "API key, quota, and server logs, then try again."
                    ),
                }
                finish_refresh_run(thread_conn, run_id, "error", "no recommendations generated")
        else:
            result = {"ok": False, "error": "Plex not configured"}
            finish_refresh_run(thread_conn, run_id, "done")
    except Exception as exc:  # rationale: §6.4 site 2 — background job runner; a single bad refresh must not leak a stuck "running" lease or crash the thread.
        logger.exception("Background recommendation refresh failed")
        result = {"ok": False, "error": "Recommendation refresh failed"}
        try:
            finish_refresh_run(thread_conn, run_id, "error", str(exc))
        except sqlite3.Error:
            # A failure here leaves the run lease stuck in "running" until
            # it expires — operationally significant, so log at ERROR with
            # the traceback so the operator knows to check the DB.
            logger.exception(
                "Failed to mark refresh run_id=%s as error; lease may be stuck",
                run_id,
            )
    finally:
        _set_refresh_result(result)
        stop_event.set()
        if not manual_refresh_recorded:
            logger.info(
                "Manual refresh did not complete successfully; "
                "cooldown timestamp NOT recorded so the user can retry."
            )
        thread_conn.close()


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
        next_at = (now_utc() + cooldown).isoformat()
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

    db_path = request.app.state.db_path
    secret_key = config.secret_key

    # Reset stale result from a previous run so the polling endpoint
    # doesn't immediately serve last refresh's payload to a new poll loop.
    _set_refresh_result(None)

    # Heartbeat ticker keeps the DB lease alive while the worker runs;
    # the worker signals it to stop via this shared event in its finally.
    heartbeat_stop = threading.Event()

    # Both threads are named per §8.6 so py-spy / logs identify them.
    # ``daemon=True`` so a process-level shutdown doesn't get blocked on
    # an in-flight OpenAI call; the refresh is best-effort across
    # restarts (the DB lease lapses naturally).
    thread = threading.Thread(
        target=_run_refresh,
        args=(db_path, secret_key, run_id, heartbeat_stop),
        name="recommended-refresh",
        daemon=True,
    )
    hb_thread = threading.Thread(
        target=_heartbeat_lease,
        args=(db_path, run_id, heartbeat_stop),
        name="recommended-refresh-heartbeat",
        daemon=True,
    )
    _set_refresh_thread(thread)
    thread.start()
    hb_thread.start()
    return JSONResponse({"status": "started"})


@router.get("/api/recommended/refresh/status")
def api_refresh_status(admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Poll whether the background refresh is still running.

    Also returns cooldown info so the page can keep the button hidden
    after a successful refresh without needing a full reload.

    The "running" signal combines two sources:

    * ``is_refresh_running(conn)`` — DB-lease check, authoritative across
      processes (e.g. when a second uvicorn worker handles the poll).
    * ``_refresh_thread_alive()`` — in-process check, authoritative when
      the lease has lapsed but the local worker is still doing work.
      Treating the lease as truth here would flip the status to "idle"
      mid-refresh and the JS poll loop, having no "idle" branch, would
      spin forever — the user sees the button stuck on "Refreshing…".
    """
    conn = get_db()
    running = is_refresh_running(conn) or _refresh_thread_alive()
    result = _get_refresh_result()
    cooldown = refresh_cooldown_remaining(conn)
    cooldown_payload: dict[str, object] = {"manual_refresh_available": cooldown is None}
    if cooldown is not None:
        cooldown_payload["cooldown_seconds"] = int(cooldown.total_seconds())
        cooldown_payload["next_available_at"] = (now_utc() + cooldown).isoformat()

    if running:
        return JSONResponse({"status": "running", **cooldown_payload})
    if result is not None:
        return JSONResponse({"status": "done", "result": result, **cooldown_payload})
    return JSONResponse({"status": "idle", **cooldown_payload})
