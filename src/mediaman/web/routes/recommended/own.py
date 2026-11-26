"""A person has their own picks made again, in the background: start and status."""

from __future__ import annotations

import logging
import sqlite3
import threading

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db, open_thread_connection
from mediaman.services.arr.build import build_plex_from_db
from mediaman.services.infra import ConfigDecryptError, SSRFRefused, get_bool_setting
from mediaman.services.openai.client import OpenAIUnavailable, get_openai_key, key_refused
from mediaman.services.openai.recommendations import user_picks
from mediaman.services.openai.recommendations.personal import has_enough_plays, make_picks
from mediaman.services.openai.recommendations.user_picks import RefreshState
from mediaman.services.plex_link.sync import read_plays
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

router = APIRouter()


def _state(conn: sqlite3.Connection, username: str, secret_key: str) -> RefreshState:
    """Whether *username* may have their picks made now, with every reason there may be not."""
    if not get_bool_setting(conn, "suggestions_enabled", default=True):
        return RefreshState("disabled")
    try:
        get_openai_key(conn, secret_key)
    except ValueError:
        return RefreshState("not_configured")
    if key_refused(conn, secret_key):
        return RefreshState("unavailable")
    state = user_picks.refresh_state(conn, username, now_utc())
    if state.reason is None and not has_enough_plays(conn, username):
        return RefreshState("not_enough_plays")
    return state


def my_refresh(conn: sqlite3.Connection, username: str, secret_key: str) -> dict[str, object]:
    state = _state(conn, username, secret_key)
    return {
        "available": state.reason is None,
        "reason": state.reason,
        "next_available_at": state.next_available_at,
        "running": state.reason == "running",
    }


def _refused(state: RefreshState) -> JSONResponse:
    return respond_err(
        state.reason or "",
        status=429 if state.reason in ("wait", "daily_limit") else 409,
        next_available_at=state.next_available_at,
    )


def _run(db_path: str, secret_key: str, username: str, run_id: int) -> None:
    conn = open_thread_connection(db_path)
    try:
        try:
            read_plays(conn, secret_key, username=username)
        except Exception:  # rationale: without new plays from Plex the picks can still be made
            logger.exception("Own picks: reading Plex plays failed")
        plex = None
        try:
            plex = build_plex_from_db(conn, secret_key)
        except (SSRFRefused, ConfigDecryptError, requests.RequestException, ValueError):
            logger.warning("Own picks: Plex is unavailable, its stars are left out", exc_info=True)
        make_picks(conn, username, plex, run_id, secret_key=secret_key)
    except OpenAIUnavailable:
        pass  # the client has said why in one line, and the run says it too
    except Exception:  # rationale: background job runner; a failed run is recorded by make_picks
        logger.exception("Making a person's own picks failed")
    finally:
        conn.close()


@router.post("/api/watch/picks/refresh")
def api_refresh_own_picks(
    request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """Start making the person's own picks; once per 7 days, and within a daily limit for
    everyone together."""
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    state = _state(conn, username, secret_key)
    if state.reason is not None:
        return _refused(state)
    run = user_picks.start_run(conn, username, "person", now_utc())
    if isinstance(run, RefreshState):
        return _refused(run)
    threading.Thread(
        target=_run,
        args=(request.app.state.db_path, secret_key, username, run),
        name="own-picks-refresh",
        daemon=True,
    ).start()
    return respond_ok({"status": "started"}, status=202)


@router.get("/api/watch/picks/refresh")
def api_own_picks_status(
    request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """How the person's refresh goes: running, done or failed (the last they started), or idle.
    A failed one says "unavailable" when OpenAI refused the key."""
    conn = get_db()
    mine = my_refresh(conn, username, request.app.state.config.secret_key)
    status = "running" if mine["running"] else user_picks.last_status(conn, username) or "idle"
    if status == "unavailable":
        return respond_ok({"status": "failed", "my_refresh": mine, "error": "unavailable"})
    return respond_ok({"status": status, "my_refresh": mine})
