"""The "Recommended for you" row of the Watch page as JSON: every batch of picks, each
pick marked when the library holds it or Radarr/Sonarr is fetching it."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db, is_refresh_running
from mediaman.services import watch_catalog as catalog
from mediaman.services.arr.state import ACTION_QUEUED, clear_stale_downloaded_at
from mediaman.services.infra import get_bool_setting
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations import user_picks
from mediaman.services.openai.recommendations.throttle import refresh_cooldown_remaining
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.recommended import fetch_recommendations, group_into_batches
from mediaman.web.repository.seen_titles import person_marks
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.recommended.own import my_refresh
from mediaman.web.routes.recommended.refresh import _refresh_thread_alive
from mediaman.web.routes.watch.card_marks import CardMarks, card_marks, title_key
from mediaman.web.routes.watch.catalog_api import held_fields

router = APIRouter()


def _attach_states(
    conn: sqlite3.Connection, rows: list[dict[str, object]], arr: catalog.ArrView, username: str
) -> None:
    """Mark each pick with its download state and percent done. A pick marked as sent
    for download that the service, set up and answering, no longer tracks loses the
    mark here and in the database, so it stops showing as queued. A view that an
    invalidate has outdated heals nothing: the flag may have been set since."""
    stale: list[object] = []
    stale_own: list[object] = []
    for row in rows:
        tmdb_id = row.get("tmdb_id")
        media_type = row.get("media_type")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0 or media_type not in ("movie", "tv"):
            continue
        state, pct = arr.state(media_type, tmdb_id)
        if state is not None:
            row["download_state"] = state
            row["download_pct"] = pct
        elif row.get("downloaded_at") and arr.answers_for(media_type) and arr.is_current():
            row["downloaded_at"] = None
            (stale_own if row["own"] else stale).append(row["id"])
    clear_stale_downloaded_at(conn, stale)
    user_picks.clear_downloaded(conn, username, stale_own)


def _card(
    row: dict[str, object],
    index: catalog.LibraryIndex,
    arr: catalog.ArrView,
    held: Callable[[str | None], dict[str, object]],
    marks: CardMarks,
) -> dict[str, object]:
    own = bool(row["own"])
    pick_ref = f"{'pick' if own else 'rec'}-{row['id']}"
    kind: Literal["movie", "tv"] = "movie" if row.get("media_type") == "movie" else "tv"
    tmdb_id = row.get("tmdb_id")
    title = str(row.get("title") or "")
    library_ref = None
    pct = row.get("download_pct")
    state = row.get("download_state")
    year = row.get("year")
    if isinstance(tmdb_id, int) and tmdb_id > 0:
        library_ref = index.library_ref(
            kind, tmdb_id, title, arr, year=year if isinstance(year, int) else None
        )
    if state is None and row.get("downloaded_at"):
        state = ACTION_QUEUED
    rating = row.get("rating")
    card = catalog.discover_card(
        ref=library_ref or pick_ref,
        kind="movie" if kind == "movie" else "show",
        title=title,
        year=year if isinstance(year, int) else None,
        poster_url=catalog.tmdb_image_url(row.get("poster_url"), "w342"),
        rating=round(float(rating), 1) if isinstance(rating, int | float) and rating else None,
        library_ref=library_ref,
        download_state=state if isinstance(state, str) else None,
        download_pct=pct if isinstance(pct, int) else None,
    )
    fields = held(library_ref)
    return {
        **card,
        **fields,
        **marks(title_key(kind, tmdb_id), library_ref, bool(fields["watched"])),
        "tmdb_id": tmdb_id if isinstance(tmdb_id, int) and tmdb_id > 0 else None,
        "rec_id": None if own else row["id"],
        "pick_ref": pick_ref,
        "own": own,
        "ai_pick": True,
        "reason": row.get("reason") or None,
    }


def _unseen(
    batches: list[dict[str, object]], start: int, chosen: int
) -> tuple[list[dict[str, object]], str | None]:
    """The picks of batch *chosen* that a row starting at batch *start* does not show yet,
    and the id of the next older batch that adds a title, or None when none does. Batches
    are named by their place in *batches*, newest first."""
    seen = catalog.SeenTitles()
    items: list[dict[str, object]] = []
    for place in range(min(start, chosen), len(batches)):
        picks: list[dict[str, object]] = batches[place]["items"]  # type: ignore[assignment]
        fresh = [pick for pick in picks if _add_pick(seen, pick)]
        if place == chosen:
            items = fresh
        elif place > chosen and fresh:
            return items, str(batches[place]["id"])
    return items, None


def _add_pick(seen: catalog.SeenTitles, pick: dict[str, object]) -> bool:
    tmdb_id, year = pick.get("tmdb_id"), pick.get("year")
    return seen.add(
        "movie" if pick.get("media_type") == "movie" else "tv",
        tmdb_id if isinstance(tmdb_id, int) and tmdb_id > 0 else None,
        str(pick.get("title") or ""),
        year if isinstance(year, int) else None,
    )


def _refresh_block(conn: sqlite3.Connection) -> dict[str, object]:
    cooldown = refresh_cooldown_remaining(conn)
    return {
        "available": cooldown is None,
        "next_available_at": None if cooldown is None else (now_utc() + cooldown).isoformat(),
        "running": is_refresh_running(conn) or _refresh_thread_alive(),
    }


@router.get("/api/watch/recommended")
def api_watch_recommended(
    request: Request,
    batch: str | None = None,
    start: str | None = None,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """The person's own picks and the shared ones of one batch (the latest of both unless
    *batch* names another), the list of batches, whether they may have their own made again, and for
    an admin the cooldown of the shared ones. No title comes twice: *start* names
    the batch the row began with, and what the batches from it to this one hold is left
    out. No title they rated a strong no or marked watched is among them, and one rated down
    comes last in its batch. ``next_batch`` is the older batch to go on with, or None when the picks are
    finished."""
    conn = get_db()
    is_admin = get_user_role(conn, username) == "admin"
    enabled = get_bool_setting(conn, "suggestions_enabled", default=True)
    payload: dict[str, object] = {"enabled": enabled, "batch": None, "batches": [], "items": []}
    payload["next_batch"] = None
    if is_admin:
        payload["refresh"] = _refresh_block(conn)
    payload["my_refresh"] = my_refresh(conn, username, request.app.state.config.secret_key)
    if not enabled:
        return respond_ok(payload)

    own = person_marks(conn, username)
    batches = group_into_batches(
        fetch_recommendations(conn, username), date.today(), own.levels, own.seen
    )
    places = {b["id"]: place for place, b in enumerate(batches)}
    if any(b is not None and b not in places for b in (batch, start)):
        return respond_err("not_found", status=404)
    payload["batches"] = [
        {k: b[k] for k in ("id", "label", "date_label")} for b in batches if b["items"]
    ]
    # A batch with every title rated a strong no or marked watched gives way to the next older one.
    chosen = next(
        (
            p
            for p in range(places[batch] if batch is not None else 0, len(batches))
            if batches[p]["items"]
        ),
        None,
    )
    if chosen is None:
        return respond_ok(payload)

    rows, payload["next_batch"] = _unseen(
        batches, places[start] if start is not None else chosen, chosen
    )
    arr = catalog.arr_view(conn, request.app.state.config.secret_key)
    _attach_states(conn, rows, arr, username)
    libraries = lib.scanned_library_ids(conn)
    client = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    index = catalog.library_index(conn, client, libraries, lib.library_movies, lib.library_shows)
    payload["batch"] = batches[chosen]["id"]
    held = held_fields(conn, username, libraries)
    marks = card_marks(own, client)
    payload["items"] = [_card(row, index, arr, held, marks) for row in rows]
    return respond_ok(payload)
