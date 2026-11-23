"""The "Recommended for you" row of the Watch page as JSON: every batch of picks, each
pick marked when the library holds it or Radarr/Sonarr is fetching it."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db, is_refresh_running
from mediaman.services import watch_catalog as catalog
from mediaman.services.arr.state import (
    ACTION_DOWNLOADING,
    ACTION_QUEUED,
    LazyArrClients,
    attach_download_states,
)
from mediaman.services.infra import get_bool_setting
from mediaman.services.openai.recommendations.throttle import refresh_cooldown_remaining
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.recommended import fetch_recommendations, group_into_batches
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.recommended.refresh import _refresh_thread_alive

router = APIRouter()


def _card(
    row: dict[str, object], index: catalog.LibraryIndex, arr: catalog.ArrView
) -> dict[str, object]:
    rec_id = row["id"]
    kind: Literal["movie", "tv"] = "movie" if row.get("media_type") == "movie" else "tv"
    tmdb_id = row.get("tmdb_id")
    title = str(row.get("title") or "")
    library_ref = None
    pct = None
    state = row.get("download_state")
    if isinstance(tmdb_id, int) and tmdb_id > 0:
        library_ref = index.library_ref(kind, tmdb_id, title, arr)
        if state in (ACTION_QUEUED, ACTION_DOWNLOADING):
            pct = arr.state(kind, tmdb_id)[1]
    if state is None and row.get("downloaded_at"):
        state = ACTION_QUEUED
    year = row.get("year")
    rating = row.get("rating")
    card = catalog.discover_card(
        ref=library_ref or f"rec-{rec_id}",
        kind="movie" if kind == "movie" else "show",
        title=title,
        year=year if isinstance(year, int) else None,
        poster_url=catalog.tmdb_image_url(row.get("poster_url"), "w342"),
        rating=round(float(rating), 1) if isinstance(rating, int | float) and rating else None,
        library_ref=library_ref,
        download_state=state if isinstance(state, str) else None,
        download_pct=pct,
    )
    return {**card, "rec_id": rec_id, "reason": row.get("reason") or None}


def _refresh_block(conn: sqlite3.Connection) -> dict[str, object]:
    cooldown = refresh_cooldown_remaining(conn)
    return {
        "available": cooldown is None,
        "next_available_at": None if cooldown is None else (now_utc() + cooldown).isoformat(),
        "running": is_refresh_running(conn) or _refresh_thread_alive(),
    }


@router.get("/api/watch/recommended")
def api_watch_recommended(
    request: Request, batch: str | None = None, username: str = Depends(get_current_user)
) -> JSONResponse:
    """The picks of one batch (the latest unless *batch* names another), the list of
    batches, and for an admin the refresh cooldown."""
    conn = get_db()
    is_admin = get_user_role(conn, username) == "admin"
    enabled = get_bool_setting(conn, "suggestions_enabled", default=True)
    payload: dict[str, object] = {"enabled": enabled, "batch": None, "batches": [], "items": []}
    if is_admin:
        payload["refresh"] = _refresh_block(conn)
    if not enabled:
        return respond_ok(payload)

    batches = group_into_batches(fetch_recommendations(conn), date.today())
    if batch is None:
        chosen = batches[0] if batches else None
    else:
        chosen = next((b for b in batches if b["id"] == batch), None)
        if chosen is None:
            return respond_err("not_found", status=404)
    payload["batches"] = [{k: b[k] for k in ("id", "label", "date_label")} for b in batches]
    if chosen is None:
        return respond_ok(payload)

    rows: list[dict[str, object]] = chosen["items"]  # type: ignore[assignment]
    attach_download_states(
        [{"trending": [], "personal": rows}],
        LazyArrClients(conn, request.app.state.config.secret_key),
        conn,
    )
    libraries = lib.scanned_library_ids(conn)
    index = catalog.LibraryIndex.build(
        ((m.key, m.title, m.tmdb_id) for m in lib.library_movies(conn, libraries)),
        ((s.key, s.title, s.tvdb_id) for s in lib.library_shows(conn, libraries)),
    )
    arr = catalog.arr_view(conn, request.app.state.config.secret_key)
    payload["batch"] = chosen["id"]
    payload["items"] = [_card(row, index, arr) for row in rows]
    return respond_ok(payload)
