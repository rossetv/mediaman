"""Download status polling endpoint."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Literal, TypedDict, cast

import requests
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from mediaman.core.format import format_bytes
from mediaman.crypto import validate_poll_token
from mediaman.db import get_db
from mediaman.services.arr import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.download_format import (
    build_episode_summary,
    build_item,
    extract_poster_url,
    format_episode_label,
    map_arr_status,
)
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.downloads.download_queue import build_episode_dicts
from mediaman.services.infra.http import SafeHTTPError
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.auth.middleware import get_optional_admin
from mediaman.web.repository.download import fetch_recent_download
from mediaman.web.responses import respond_err

logger = logging.getLogger(__name__)

router = APIRouter()

_DOWNLOAD_STATUS_LIMITER = RateLimiter(max_attempts=120, window_seconds=60)

# ---------------------------------------------------------------------------
# Per-service per-tmdb status cache.
#
# The status endpoint is polled by the confirm page every ~4 seconds while
# the user watches a download progress bar. Each poll fans out to Radarr or
# Sonarr (a queue lookup plus a movie / series lookup), so a single open
# tab on a 4-second poll already keeps a sustained outbound load on the
# operator's home Arr boxes; multiple tabs multiply it. Cache the
# per-(service, tmdb_id) status for a short window so back-to-back polls
# from the same client share one upstream round-trip.
#
# TTL is intentionally short (2.5s) so the user still sees fresh progress
# on the 4-second poll cadence — half the poll interval ensures every
# poll either hits a fresh fetch or a result that was at most one beat
# stale. Anything longer is still bounded but reduces apparent
# responsiveness; anything shorter does not meaningfully reduce load.
# ---------------------------------------------------------------------------

_STATUS_CACHE_TTL_SECONDS = 2.5
_STATUS_CACHE_LOCK = threading.Lock()


class _SonarrEpEntry(TypedDict):
    """Intermediate per-episode accumulator used inside ``_sonarr_status``."""

    label: str
    title: str
    progress: int
    size: int
    sizeleft: int
    status: str
    tracked_state: str
    timeleft: str


# (service, tmdb_id, secret_key_fingerprint) -> (timestamp, status_dict)
_STATUS_CACHE: dict[tuple[str, int, str], tuple[float, DownloadItem]] = {}


def _key_fingerprint(secret_key: str) -> str:
    """Short fingerprint of *secret_key* for use as a cache key."""
    import hashlib

    return hashlib.sha256(secret_key.encode()).hexdigest()[:16]


def _reset_status_cache_for_tests() -> None:
    """Clear the status cache. Test helper; never call in production."""
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.clear()


def _format_timeleft(timeleft: str) -> str:
    """Convert HH:MM:SS timeleft string to a human-readable eta string."""
    if not timeleft:
        return ""
    parts = timeleft.split(":")
    if len(parts) != 3:
        return ""
    try:
        hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return ""
    if hours > 0:
        return f"~{hours} hr {mins:02d} min remaining"
    if mins > 0:
        return f"~{mins} min remaining"
    return f"~{max(1, secs)} sec remaining"


def _safe_int(value: object) -> int:
    """Coerce *value* to a non-negative int, defaulting to 0.

    Defends against Arr responses that return ``size`` / ``sizeleft`` as
    strings or null. Previously ``size_total > 0`` raised ``TypeError``
    on a string operand and crashed the handler.

    Accepts ``int``/``float`` directly and parses ``str`` numerals;
    everything else (including ``None`` and ``bool``) resolves to 0.
    """
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` so ``int(True)`` is valid,
        # but treating ``True`` as a 1-byte size makes no sense.
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        try:
            n = int(value)
        except ValueError:
            return 0
        return n if n > 0 else 0
    return 0


def _safe_progress(size_total: int, size_left: int) -> int:
    """Return a download progress percentage clamped to ``[0, 100]``.

    Without the clamp a misreported ``sizeleft`` larger than ``size``
    (or a negative ``sizeleft``) would yield an out-of-range progress
    value that breaks the progress-bar template — a UI hazard rather
    than a data corruption issue, but still worth defending against.
    """
    if size_total <= 0:
        return 0
    raw = round((1 - size_left / size_total) * 100)
    return max(0, min(100, raw))


_UNKNOWN_ITEM: DownloadItem = build_item(
    dl_id="",
    title="",
    media_type="movie",
    poster_url="",
    state="unknown",
    progress=0,
    eta="",
    size_done="",
    size_total="",
)


def _radarr_status(conn: sqlite3.Connection, secret_key: str, tmdb_id: int) -> DownloadItem:
    """Return the download-status item dict for a Radarr movie by TMDB ID."""
    client = build_radarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    movie = client.get_movie_by_tmdb(tmdb_id)
    if movie and movie.get("hasFile"):
        title = cast(str, movie.get("title", ""))
        # rationale: ``RadarrMovie.images`` is typed as ``list[ArrImage]``,
        # but ``extract_poster_url`` accepts ``Sequence[Mapping[str, object]]``
        # to support the parallel Sonarr branches that pass through raw
        # queue dicts.  The runtime values are identical; the cast narrows
        # the mypy view without a runtime conversion.
        poster_url = extract_poster_url(cast("list[dict[Any, Any]] | None", movie.get("images")))
        return build_item(
            dl_id=f"radarr:{title}",
            title=title,
            media_type="movie",
            poster_url=poster_url,
            state="ready",
            progress=100,
            eta="",
            size_done="",
            size_total="",
        )

    queue = client.get_queue()
    for item in queue:
        item_movie = item.get("movie") or {}
        if item_movie.get("tmdbId") == tmdb_id:
            size_left = _safe_int(item.get("sizeleft"))
            size_total = _safe_int(item.get("size"))
            progress = _safe_progress(size_total, size_left)
            state = map_arr_status(
                item.get("status") or "",
                item.get("trackedDownloadState") or "",
            )
            eta = _format_timeleft(item.get("timeleft", ""))
            if state == "almost_ready":
                eta = "Post-processing…"
            title = item_movie.get("title", "")
            poster_url = extract_poster_url(item_movie.get("images"))
            return build_item(
                dl_id=f"radarr:{title}",
                title=title,
                media_type="movie",
                poster_url=poster_url,
                state=state,
                progress=progress,
                eta=eta,
                size_done=format_bytes(size_total - size_left),
                size_total=format_bytes(size_total),
            )

    title = cast(str, (movie or {}).get("title", ""))
    if title:
        recent = fetch_recent_download(conn, f"radarr:{title}")
        if recent is not None:
            return build_item(
                dl_id=recent.dl_id,
                title=recent.title,
                media_type="movie",
                poster_url=recent.poster_url,
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )

    return build_item(
        dl_id=f"radarr:{title}" if title else "",
        title=title,
        media_type="movie",
        poster_url="",
        state="searching",
        progress=0,
        eta="",
        size_done="",
        size_total="",
    )


# rationale: queue traversal, episode-file cross-reference, and status
# classification are coupled — each queue item requires an episode-file lookup
# to determine whether it is downloading, missing, or complete; splitting the
# traversal from the classification would double the Sonarr API surface without
# reducing the essential coupling.
def _sonarr_status(conn: sqlite3.Connection, secret_key: str, tmdb_id: int) -> DownloadItem:
    """Return the download-status item dict for a Sonarr series by TMDB ID."""
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    queue = client.get_queue()
    series_title = ""
    series_poster = ""
    ep_entries: list[_SonarrEpEntry] = []

    for item in queue:
        item_series = item.get("series") or {}
        if item_series.get("tmdbId") != tmdb_id:
            continue

        if not series_title:
            series_title = item_series.get("title", "")
        if not series_poster:
            series_poster = extract_poster_url(item_series.get("images"))

        episode = item.get("episode") or {}
        size = _safe_int(item.get("size"))
        sizeleft = _safe_int(item.get("sizeleft"))
        ep_progress = _safe_progress(size, sizeleft) if size else 0
        season_num = episode.get("seasonNumber")
        ep_num = episode.get("episodeNumber")
        ep_label = format_episode_label(season_num, ep_num)

        ep_entries.append(
            {
                "label": ep_label,
                "title": episode.get("title", ""),
                "progress": ep_progress,
                "size": size,
                "sizeleft": sizeleft,
                "status": item.get("status") or "",
                "tracked_state": item.get("trackedDownloadState") or "",
                "timeleft": item.get("timeleft", ""),
            }
        )

    if ep_entries:
        ep_entries.sort(key=lambda e: e["label"])
        episodes = build_episode_dicts(cast("list[dict[str, object]]", ep_entries))
        total_size = sum(e["size"] for e in ep_entries)
        total_left = sum(e["sizeleft"] for e in ep_entries)
        overall_progress = _safe_progress(total_size, total_left) if total_size else 0
        raw_statuses = [e["status"] for e in ep_entries]
        raw_tracked = [e["tracked_state"] for e in ep_entries]
        combined_status = next(
            (s for s in raw_statuses if s.lower() in ("downloading", "completed")),
            raw_statuses[0] if raw_statuses else "",
        )
        combined_tracked = next(
            (s for s in raw_tracked if s.lower() in ("downloading", "importing", "importpending")),
            raw_tracked[0] if raw_tracked else "",
        )
        state = map_arr_status(combined_status, combined_tracked)
        eta = _format_timeleft(
            max((e["timeleft"] for e in ep_entries if e["timeleft"]), default="")
        )
        if state == "almost_ready":
            eta = "Post-processing…"
        episode_summary = build_episode_summary(episodes)
        return build_item(
            dl_id=f"sonarr:{series_title}",
            title=series_title,
            media_type="series",
            poster_url=series_poster,
            state=state,
            progress=overall_progress,
            eta=eta,
            size_done=format_bytes(total_size - total_left),
            size_total=format_bytes(total_size),
            episodes=episodes,
            episode_summary=episode_summary,
        )

    all_series = client.get_series()
    matched = next(
        (s for s in all_series if s.get("tmdbId") == tmdb_id),
        None,
    )
    if matched:
        stats = matched.get("statistics") or {}
        s_title = matched.get("title", "")
        if stats.get("episodeFileCount", 0) > 0:
            return build_item(
                dl_id=f"sonarr:{s_title}",
                title=s_title,
                media_type="series",
                poster_url="",
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )

        recent = fetch_recent_download(conn, f"sonarr:{s_title}")
        if recent is not None:
            return build_item(
                dl_id=recent.dl_id,
                title=recent.title,
                media_type="series",
                poster_url=recent.poster_url,
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )

        return build_item(
            dl_id=f"sonarr:{s_title}",
            title=s_title,
            media_type="series",
            poster_url="",
            state="searching",
            progress=0,
            eta="",
            size_done="",
            size_total="",
        )

    return build_item(
        dl_id="",
        title="",
        media_type="series",
        poster_url="",
        state="searching",
        progress=0,
        eta="",
        size_done="",
        size_total="",
    )


def _cached_status(
    *, service: str, tmdb_id: int, conn: sqlite3.Connection, secret_key: str
) -> DownloadItem:
    """Return cached or freshly-fetched status for ``(service, tmdb_id)``.

    See module-level docstring for cache semantics. Cache misses fan
    out to the underlying ``_radarr_status`` / ``_sonarr_status``
    implementation; cache hits short-circuit the upstream call entirely.
    """
    key = (service, tmdb_id, _key_fingerprint(secret_key))
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        hit = _STATUS_CACHE.get(key)
        if hit and now - hit[0] < _STATUS_CACHE_TTL_SECONDS:
            return hit[1]
    if service == "radarr":
        result = _radarr_status(conn, secret_key, tmdb_id)
    else:
        result = _sonarr_status(conn, secret_key, tmdb_id)
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[key] = (now, result)
    return result


@router.get("/api/download/status")
def download_status(
    request: Request,
    service: Literal["radarr", "sonarr"],
    tmdb_id: int = Query(..., gt=0),
    poll_token: str | None = None,
    admin: str | None = Depends(get_optional_admin),
) -> JSONResponse:
    """Poll the download progress for a recently-requested item.

    Finding 14: unauthenticated callers must supply a ``poll_token``
    (short-lived, service/tmdb-bound) returned by the submit endpoint.
    Authenticated admins may poll without a token.

    ``service`` is constrained to the literal set ``{"radarr", "sonarr"}``
    and ``tmdb_id`` must be a positive integer (TMDB IDs are 1-indexed),
    so malformed callers receive a 422 from FastAPI rather than reaching
    the handler with garbage values that would silently fall through to
    a placeholder response.
    """
    config = request.app.state.config

    if not _DOWNLOAD_STATUS_LIMITER.check(get_client_ip(request)):
        return respond_err("too_many_requests", status=429)

    if not admin:
        # Finding 14: unauthenticated polling must use a short-lived
        # poll_token.  The long-lived download token is no longer accepted
        # for status polling — it is single-use and only valid for the
        # /download/{token} POST.  Clients receive a poll_token in the
        # submit response and must use it exclusively for polling.
        authenticated = False

        if poll_token is not None and len(poll_token) <= 4096:
            poll_payload = validate_poll_token(poll_token, config.secret_key)
            if (
                poll_payload is not None
                and poll_payload.get("svc") == service
                and poll_payload.get("tmdb") == tmdb_id
            ):
                authenticated = True

        if not authenticated:
            return respond_err("not_authenticated", status=401)

    conn = get_db()

    try:
        # The cache exists to bound the request-amplification attack
        # surface of unauthenticated polls (a public link with a
        # poll_token times the rate limit). Admins are already trusted
        # and rate-limited; bypassing the cache for them keeps the
        # admin UI showing fresh data without the slight staleness an
        # admin tab would otherwise see, and avoids stale-cache
        # surprises in admin-driven test suites.
        if admin:
            if service == "radarr":
                result = _radarr_status(conn, config.secret_key, tmdb_id)
            else:
                result = _sonarr_status(conn, config.secret_key, tmdb_id)
        else:
            result = _cached_status(
                service=service, tmdb_id=tmdb_id, conn=conn, secret_key=config.secret_key
            )
        return JSONResponse(result)

    except (requests.RequestException, SafeHTTPError, ArrError):
        # Both transport (RequestException) and Arr-level non-2xx
        # (SafeHTTPError) failures are bounded behaviours we report as
        # "unknown" rather than 500. Without SafeHTTPError in this clause
        # a Radarr/Sonarr 500 would propagate as an unhandled exception
        # and surface as a 500 to the polling client.
        logger.exception("download_status error (service=%s tmdb_id=%s)", service, tmdb_id)
        return JSONResponse(_UNKNOWN_ITEM)
