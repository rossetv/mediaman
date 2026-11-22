from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

from mediaman.core.deleted_media import DeletedMedia, fetch_deleted_media
from mediaman.core.format import (
    days_ago,
    format_bytes,
    media_type_badge,
    relative_day_label,
    rk_from_audit_detail,
    title_from_audit_detail,
)
from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.services.infra import (
    get_aggregate_disk_usage,
    get_disk_usage_for_paths,
    get_string_setting,
)
from mediaman.services.infra import get_media_path as _get_media_path
from mediaman.web.models import ACTION_SCHEDULED_DELETION
from mediaman.web.repository.dashboard import (
    fetch_deleted_audit_batch,
    fetch_media_type_sizes,
    fetch_redownload_audit_rows,
    fetch_scheduled_deletions,
)

# Bounded scan size so the dashboard render doesn't degenerate into a
# long table walk on a heavy audit_log. 5 batches × 50 rows = 250
# candidates is enough headroom for any realistic mix of re-downloaded
# and orphan-titled deletions.
_RECENT_DELETED_BATCH = 50
_RECENT_DELETED_MAX_BATCHES = 5

# Cache window for the disk-usage stat. statvfs() is cheap but a busy
# dashboard on a slow filesystem could still spend tens of milliseconds
# per render hitting it; 30s is well below the granularity at which a
# user notices stale "free space" numbers.
_DISK_USAGE_CACHE_TTL_SECONDS = 30.0
_disk_usage_cache: dict[str, tuple[float, dict[str, int]]] = {}
_disk_usage_cache_lock = threading.Lock()


def _days_until(dt_str: str | None) -> str:
    """Return 'Deletes in N days' given an ISO datetime string, or ''."""
    execute_at = parse_iso_utc(dt_str)
    if execute_at is None:
        return ""
    return relative_day_label(
        execute_at,
        now=now_utc(),
        today="Deletes today",
        tomorrow="Deletes tomorrow",
        future=lambda days: f"Deletes in {days} days",
    )


def _fetch_scheduled(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = fetch_scheduled_deletions(conn, ACTION_SCHEDULED_DELETION)

    items = []
    for row in rows:
        media_type = row.media_type
        badge_class, type_label = media_type_badge(media_type)
        if media_type in ("tv", "anime") and row.season_number:
            type_label = f"{type_label} · S{row.season_number}"

        items.append(
            {
                "sa_id": row.sa_id,
                "media_item_id": row.media_item_id,
                "title": row.title,
                "plex_rating_key": row.plex_rating_key,
                "badge_class": badge_class,
                "media_type": media_type,
                "type_label": type_label,
                "countdown": _days_until(row.execute_at),
                "added_ago": days_ago(row.added_at),
                "file_size": format_bytes(row.file_size_bytes),
                "file_size_bytes": row.file_size_bytes,
            }
        )
    return items


def _build_redownload_index(
    conn: sqlite3.Connection,
) -> dict[str, str]:
    """Return ``by_title_lower`` mapping of re-download timestamps.

    The audit_log ``media_item_id`` column carries different content
    depending on the action:

    * ``deleted`` rows: a stable UUID matching ``media_items.id``.
    * ``re_downloaded`` rows: a stable ``tmdb:<id>`` / ``tvdb:<id>`` token, matched against
      the deleted item's stored ids (``tvdb:<id>:s<season>`` for a season; an older bare
      ``tvdb:<id>`` matches the whole series).
    * ``downloaded`` rows: the resolved title, lower-cased for matching.

    Title matches stay lower-cased so "Dune" matches "dune".
    """
    by_title_lower: dict[str, str] = {}
    rows = fetch_redownload_audit_rows(conn)
    for rd in rows:
        raw = rd.media_item_id
        ts = rd.created_at
        key = raw.lower()
        if not key:
            continue
        prev_t = by_title_lower.get(key)
        if prev_t is None or ts > prev_t:
            by_title_lower[key] = ts
    return by_title_lower


def _was_redownloaded_after(
    deletion_created_at: str,
    *,
    title: str,
    stored: DeletedMedia | None,
    by_title_lower: dict[str, str],
) -> bool:
    last_redownload = by_title_lower.get(title.lower())
    tokens: list[str] = []
    if stored is not None:
        if stored.media_type == "movie" and stored.tmdb_id:
            tokens.append(f"tmdb:{stored.tmdb_id}")
        elif stored.media_type != "movie" and stored.tvdb_id:
            tokens.append(f"tvdb:{stored.tvdb_id}")  # recorded before seasons were tracked
            if stored.season_number is not None:
                tokens.append(f"tvdb:{stored.tvdb_id}:s{stored.season_number}")
    for token in tokens:
        by_id = by_title_lower.get(token)
        if by_id and (last_redownload is None or by_id > last_redownload):
            last_redownload = by_id
    return bool(last_redownload and last_redownload > deletion_created_at)


def _redownload_payload(stored: DeletedMedia | None, title: str) -> dict[str, object] | None:
    """Return the re-download request body, or ``None`` when the stored row lacks the id."""
    if stored is None:
        return None
    if stored.media_type == "movie":
        if stored.tmdb_id:
            return {"title": title, "tmdb_id": stored.tmdb_id}
        return None
    if stored.tvdb_id and stored.season_number is not None:
        return {"title": title, "tvdb_id": stored.tvdb_id, "season_number": stored.season_number}
    return None


def _fetch_recently_deleted(conn: sqlite3.Connection, secret_key: str) -> list[dict[str, object]]:
    """Return up to 10 recent deleted audit_log entries, skipping ones with a newer
    re-download. Pages through audit_log in batches up to a hard cap so the filtering cannot
    leave the result short.
    """
    from mediaman.web.routes.dashboard._poster_fanout import _fill_tmdb_posters

    by_title_lower = _build_redownload_index(conn)

    items: list[dict[str, object]] = []
    titles_needing_poster: list[tuple[int, str]] = []
    seen_ids: set[int] = set()

    for batch in range(_RECENT_DELETED_MAX_BATCHES):
        offset = batch * _RECENT_DELETED_BATCH
        rows = fetch_deleted_audit_batch(conn, limit=_RECENT_DELETED_BATCH, offset=offset)

        if not rows:
            break

        stored_by_audit = fetch_deleted_media(conn, [r.audit_id for r in rows])

        for row in rows:
            if row.audit_id in seen_ids:
                continue
            seen_ids.add(row.audit_id)

            stored = stored_by_audit.get(row.audit_id)
            title = row.title or (stored.title if stored else None)
            if not title:
                title = title_from_audit_detail(row.detail)
            if _was_redownloaded_after(
                row.created_at,
                title=title,
                stored=stored,
                by_title_lower=by_title_lower,
            ):
                continue

            rk = row.plex_rating_key or rk_from_audit_detail(row.detail)
            poster_url = f"/api/poster/{rk}" if rk else ""
            idx = len(items)
            items.append(
                {
                    "id": row.audit_id,
                    "media_item_id": row.media_item_id,
                    "title": title,
                    "media_type": row.media_type or "",
                    "poster_url": poster_url,
                    "deleted_ago": days_ago(row.created_at),
                    "reclaimed": format_bytes(row.space_reclaimed_bytes),
                    "redownload": _redownload_payload(stored, title),
                }
            )
            if not poster_url:
                titles_needing_poster.append((idx, title))
            if len(items) >= 10:
                break

        if len(items) >= 10:
            break

    # Fall back to TMDB poster for items without a Plex rating key
    if titles_needing_poster:
        _fill_tmdb_posters(conn, items, titles_needing_poster, secret_key)

    return items


def _configured_library_paths(conn: sqlite3.Connection) -> list[str]:
    """Return the distinct media paths from the ``disk_thresholds`` setting.

    ``disk_thresholds`` is a JSON dict keyed by Plex library id, each value
    an object with a ``path`` (e.g. ``{"1": {"path": "/media/movies", ...}}``).
    These paths are the authoritative set of mount points the libraries live
    on — a movies drive and a TV drive may be entirely separate disks not
    reachable from a single base directory.  Returns ``[]`` when the setting
    is missing or malformed so the caller can fall back to the media root.
    """
    raw_value = get_string_setting(conn, "disk_thresholds")
    if not raw_value:
        return []
    try:
        thresholds = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(thresholds, dict):
        return []
    paths: list[str] = []
    for cfg in thresholds.values():
        path = cfg.get("path") if isinstance(cfg, dict) else None
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


def _cached_disk_usage(cache_key: str, paths: list[str] | None = None) -> dict[str, int]:
    """Return aggregate disk usage cached for 30s and keyed on cache_key. With paths, usage is
    aggregated across those mount points (each physical disk counted once); otherwise
    cache_key is a single base path scanned for sub-mounts. A change of configured paths
    yields a new key.
    """
    now = time.monotonic()
    with _disk_usage_cache_lock:
        entry = _disk_usage_cache.get(cache_key)
        if entry is not None and now - entry[0] < _DISK_USAGE_CACHE_TTL_SECONDS:
            return entry[1]
    fresh = get_disk_usage_for_paths(paths) if paths else get_aggregate_disk_usage(cache_key)
    with _disk_usage_cache_lock:
        # Plain assignment so an expired entry is replaced, not kept.
        _disk_usage_cache[cache_key] = (now, fresh)
    return fresh


def _fetch_storage_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return storage stats for the dashboard template. Disk usage spans every configured
    library drive; falls back to the media root without paths and to zeroes on a filesystem
    error.
    """
    # Disk-level stats — aggregate across every configured library drive
    try:
        lib_paths = _configured_library_paths(conn)
        if lib_paths:
            disk = _cached_disk_usage("|".join(sorted(lib_paths)), lib_paths)
        else:
            disk = _cached_disk_usage(_get_media_path())
        total = disk["total_bytes"]
        used = disk["used_bytes"]
        free = disk["free_bytes"]
    except (OSError, KeyError):
        total = used = free = 0

    # Per-type breakdown from DB
    type_rows = fetch_media_type_sizes(conn)

    type_sizes = {row.media_type: row.total for row in type_rows}
    movies_bytes = type_sizes.get("movie", 0)
    tv_bytes = (
        type_sizes.get("tv_season", 0) + type_sizes.get("tv", 0) + type_sizes.get("season", 0)
    )
    anime_bytes = type_sizes.get("anime_season", 0) + type_sizes.get("anime", 0)
    known_bytes = movies_bytes + tv_bytes + anime_bytes
    other_bytes = max(0, used - known_bytes) if used else 0

    def pct(val: int) -> float:
        return round(val / total * 100, 1) if total else 0.0

    return {
        "used": format_bytes(used),
        "total": format_bytes(total),
        "free": format_bytes(free),
        "movies_bytes": movies_bytes,
        "tv_bytes": tv_bytes,
        "anime_bytes": anime_bytes,
        "other_bytes": other_bytes,
        "movies_label": format_bytes(movies_bytes),
        "tv_label": format_bytes(tv_bytes),
        "anime_label": format_bytes(anime_bytes),
        "other_label": format_bytes(other_bytes),
        "movies_pct": pct(movies_bytes),
        "tv_pct": pct(tv_bytes),
        "anime_pct": pct(anime_bytes),
        "other_pct": pct(other_bytes),
    }
