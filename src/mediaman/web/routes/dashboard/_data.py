from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import sqlite3

from mediaman.core.delete_root_state import STATE_KEY
from mediaman.core.deleted_media import (
    DeletedMedia,
    fetch_deleted_media,
    redownload_tokens,
    was_redownloaded_after,
)
from mediaman.core.format import (
    days_ago,
    format_bytes,
    media_type_badge,
    normalise_media_type,
    relative_day_label,
    rk_from_audit_detail,
    title_from_audit_detail,
)
from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.scanner.repository import read_delete_allowed_roots_setting, read_setting
from mediaman.scanner.runner import read_disk_threshold
from mediaman.services.infra import (
    get_aggregate_disk_usage,
    get_disk_usage_for_paths,
    get_setting,
    is_same_disk,
)
from mediaman.services.infra import get_media_path as _get_media_path
from mediaman.services.infra.storage.disk_reads import library_disk_reads, media_root_reads
from mediaman.services.scheduled_actions.days_left import days_left
from mediaman.web.models import ACTION_SCHEDULED_DELETION
from mediaman.web.repository.dashboard import (
    fetch_deleted_audit_batch,
    fetch_media_type_sizes,
    fetch_redownload_audit_rows,
    fetch_scheduled_deletions,
)
from mediaman.web.repository.library_query import count_titles, title_ref
from mediaman.web.repository.watch_library import pending_deletions, scanned_library_ids

# Bounded scan size so the Deleted tab doesn't degenerate into a
# long table walk on a heavy audit_log. 5 batches × 50 rows = 250
# candidates is enough headroom for any realistic mix of re-downloaded
# and orphan-titled deletions.
_RECENT_DELETED_BATCH = 50
_RECENT_DELETED_MAX_BATCHES = 5

# Cache window for the disk-usage stat. statvfs() is cheap but a busy
# page on a slow filesystem could still spend tens of milliseconds
# per render hitting it; 30s is well below the granularity at which a
# user notices stale "free space" numbers.
_DISK_USAGE_CACHE_TTL_SECONDS = 30.0
_disk_usage_cache: dict[str, tuple[float, dict[str, int]]] = {}
_disk_usage_cache_lock = threading.Lock()


def _days_until(execute_at: datetime | None, now: datetime) -> str:
    """Return 'Deletes in N days', or '' without a date."""
    if execute_at is None:
        return ""
    return relative_day_label(
        execute_at,
        now=now,
        today="Deletes today",
        tomorrow="Deletes tomorrow",
        future=lambda days: f"Deletes in {days} days",
    )


def _due_label(deletions_off: bool, dry_run: bool, waiting_folder: str | None) -> str:
    """Say why a deletion whose date has passed is still to come."""
    if deletions_off:
        return "Due, but deletions are off"
    if dry_run:
        return "Due, but dry run is on"
    return "Due, but a folder was missing" if waiting_folder else "Due, the next scan will try"


def _waiting_folders(conn: sqlite3.Connection) -> dict[int, str]:
    """Return the folder the last deletion pass found missing or empty, by the row it holds back."""
    state = get_setting(conn, STATE_KEY)
    items = state.get("items") if isinstance(state, dict) else None
    if not isinstance(items, dict):
        return {}
    return {
        row_id: folder
        for folder, ids in items.items()
        if isinstance(ids, list)
        for row_id in ids
        if type(row_id) is int
    }


def _fetch_scheduled(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = fetch_scheduled_deletions(conn, ACTION_SCHEDULED_DELETION)
    now = now_utc()
    # Watch's own rule, so the count of these is always the length of its Leaving soon row.
    leaving = {p.action_id for p in pending_deletions(conn, scanned_library_ids(conn), now)}

    waiting = _waiting_folders(conn)
    deletions_off = not read_delete_allowed_roots_setting(conn, quiet=True)
    dry_run = read_setting(conn, "dry_run") == "true"

    items = []
    for row in rows:
        media_type = row.media_type
        badge_class, type_label = media_type_badge(media_type)
        is_movie = normalise_media_type(media_type) == "movie"
        if not is_movie and row.season_number:
            type_label = f"{type_label} · S{row.season_number}"
        execute_at = parse_iso_utc(row.execute_at)
        state = "leaving" if row.sa_id in leaving else "deleting" if row.deleting else "overdue"

        items.append(
            {
                "sa_id": row.sa_id,
                "media_item_id": row.media_item_id,
                "title": row.title,
                "plex_rating_key": row.plex_rating_key,
                "badge_class": badge_class,
                "media_type": media_type,
                "type_label": type_label,
                "ref": (
                    title_ref("movie", row.media_item_id)
                    if is_movie
                    else title_ref("show", row.show_rating_key)
                ),
                "season_number": None if is_movie else row.season_number,
                "countdown": (
                    _due_label(deletions_off, dry_run, waiting.get(row.sa_id))
                    if execute_at is not None and execute_at < now
                    else _days_until(execute_at, now)
                ),
                "days_left": days_left(execute_at, now) if execute_at else None,
                "state": state,
                "waiting_folder": waiting.get(row.sa_id),
                "added_ago": days_ago(row.added_at),
                "file_size": format_bytes(row.file_size_bytes),
                "file_size_bytes": row.file_size_bytes,
            }
        )
    return items


def _scheduled_summary(items: list[dict[str, object]]) -> dict[str, object]:
    """Return the counts and sizes of _fetch_scheduled's *items*, by their state."""

    def total_bytes(rows: list[dict[str, object]]) -> int:
        return sum(cast(int, item["file_size_bytes"]) for item in rows)

    leaving = [item for item in items if item["state"] == "leaving"]
    return {
        "total": len(items),
        "total_bytes": total_bytes(items),
        "total_size": format_bytes(total_bytes(items)),
        "leaving": len(leaving),
        "leaving_bytes": total_bytes(leaving),
        "leaving_size": format_bytes(total_bytes(leaving)),
        # Leaving rows no scan has sized yet: leaving_bytes is at least what it says.
        "unsized_count": sum(not item["file_size_bytes"] for item in leaving),
        "overdue": sum(item["state"] == "overdue" for item in items),
        "deleting": sum(item["state"] == "deleting" for item in items),
    }


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
            if was_redownloaded_after(
                row.created_at,
                [title.lower(), *redownload_tokens(stored)],
                by_title_lower,
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


def _library_folders(
    conn: sqlite3.Connection,
) -> tuple[list[tuple[str, str, int | None]], set[str]]:
    """Return ``(library id, folder, threshold)`` of each library with a folder in Settings.

    With them, the ids of the libraries the scanner leaves out, as it cannot
    read their threshold.

    ``disk_thresholds`` is a JSON dict keyed by Plex library id, each value
    an object with a ``path`` (e.g. ``{"1": {"path": "/media/movies", ...}}``).
    These paths are the authoritative set of mount points the libraries live
    on — a movies drive and a TV drive may be entirely separate disks not
    reachable from a single base directory.  The threshold is ``None`` where the
    scanner would read none.  Returns no folder when the setting
    is missing or malformed so the caller can fall back to the media root.
    """
    thresholds = get_setting(conn, "disk_thresholds")
    if not isinstance(thresholds, dict):
        return [], set()
    folders: list[tuple[str, str, int | None]] = []
    left_out: set[str] = set()
    for library, cfg in thresholds.items():
        limit = None
        try:
            limit = read_disk_threshold(cfg)
        except ValueError:
            left_out.add(library)
        path = cfg.get("path") if isinstance(cfg, dict) else None
        if isinstance(path, str) and path:
            folders.append((library, path, limit.percent if limit else None))
    return folders, left_out


def _some_disk(usage: dict[str, int]) -> dict[str, int]:
    """Return *usage*; raise OSError when it is of no disk, so that it counts as a failed read."""
    if not usage["total_bytes"]:
        raise OSError("no disk could be read")
    return usage


def _read_media_root(base_path: str) -> dict[str, int]:
    return _some_disk(get_aggregate_disk_usage(base_path))


def _cached_disk_usage(base_path: str) -> dict[str, int]:
    """Return the usage of the disks under *base_path*, cached for 30s.

    Raises OSError when no disk could be read; that is not kept here.
    """
    now = time.monotonic()
    with _disk_usage_cache_lock:
        entry = _disk_usage_cache.get(base_path)
        if entry is not None and now - entry[0] < _DISK_USAGE_CACHE_TTL_SECONDS:
            return entry[1]
    fresh = media_root_reads.read(base_path, _read_media_root)
    if fresh is None:
        raise OSError(f"No disk could be read under {base_path}")
    with _disk_usage_cache_lock:
        # Plain assignment so an expired entry is replaced, not kept.
        _disk_usage_cache[base_path] = (now, fresh)
    return fresh


@dataclass(frozen=True)
class _Disk:
    folders: list[str]  # the library folders on it
    usage: dict[str, int] | None  # None when it could not be read


_disks_cache: dict[tuple[str, ...], tuple[float, list[_Disk]]] = {}


def _read_usage(path: str) -> dict[str, int]:
    """Return the usage of the disk behind *path*; raise OSError when it cannot be read."""
    return _some_disk(get_disk_usage_for_paths([path]))


def _read_disks(paths: Iterable[str]) -> list[_Disk]:
    """Return one entry per device behind *paths*, and one per path that cannot be read."""
    disks: list[_Disk] = []
    for path, usage in library_disk_reads.read_all(paths, _read_usage).items():
        for disk in disks:
            if (
                usage
                and disk.usage
                and is_same_disk(
                    disk.usage["total_bytes"],
                    disk.usage["free_bytes"],
                    usage["total_bytes"],
                    usage["free_bytes"],
                )
            ):
                disk.folders.append(path)
                break
        else:
            disks.append(_Disk([path], usage))
    return disks


def _cached_disks(paths: list[str]) -> list[_Disk]:
    """Return _read_disks(paths), cached for 30s once every path has been read."""
    now = time.monotonic()
    key = tuple(paths)
    with _disk_usage_cache_lock:
        entry = _disks_cache.get(key)
        if entry is not None and now - entry[0] < _DISK_USAGE_CACHE_TTL_SECONDS:
            return entry[1]
    fresh = _read_disks(paths)
    if all(disk.usage for disk in fresh):
        with _disk_usage_cache_lock:
            _disks_cache[key] = (now, fresh)
    return fresh


def _fetch_disks(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return the devices the library folders set in Settings are on, with their thresholds.

    Only a library ticked in Settings whose threshold can be read is scanned, so only its
    threshold counts.
    """
    folders, left_out = _library_folders(conn)
    ticked = set(scanned_library_ids(conn)) - left_out
    # A ticked library with no folder is scanned whatever its disk, which nothing here knows.
    all_placed = ticked <= {library for library, _, _ in folders}
    result: list[dict[str, object]] = []
    for disk in _cached_disks(list(dict.fromkeys(path for _, path, _ in folders))):
        thresholds = [
            threshold
            for library, path, threshold in folders
            if library in ticked and path in disk.folders
        ]
        lowest = min((t for t in thresholds if t is not None), default=None)
        usage = disk.usage or {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0}
        used_pct = usage["used_bytes"] / usage["total_bytes"] * 100 if disk.usage else 0.0
        result.append(
            {
                "path": disk.folders[0],
                "readable": disk.usage is not None,
                **usage,
                "total": format_bytes(usage["total_bytes"]),
                "free": format_bytes(usage["free_bytes"]),
                "used_pct": round(used_pct, 1),
                "threshold_pct": lowest,
                "threshold_shared": lowest is not None
                and all_placed
                and all(t == lowest for t in thresholds),
                "over_threshold": lowest is not None
                and disk.usage is not None
                and used_pct >= lowest,
            }
        )
    return result


def _fetch_storage_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return storage stats for the storage panel. Disk usage spans every configured
    library drive; falls back to the media root when none can be read, and to zeroes on a
    filesystem error. ``readable`` is False for a fallback from drives that are set.
    """
    disks = _fetch_disks(conn)
    disk_read = any(disk["readable"] for disk in disks)
    if disk_read:
        total, used, free = (
            sum(cast(int, disk[key]) for disk in disks)
            for key in ("total_bytes", "used_bytes", "free_bytes")
        )
    else:
        try:
            usage = _cached_disk_usage(_get_media_path())
            total, used, free = usage["total_bytes"], usage["used_bytes"], usage["free_bytes"]
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

    # The sizes also count titles on a disk that could not be read, and extra copies.
    fit = min(1.0, used / known_bytes) if known_bytes else 1.0

    def pct(val: int) -> float:
        return round(val * fit / total * 100, 1) if total else 0.0

    return {
        "used": format_bytes(used),
        "total": format_bytes(total),
        "total_bytes": total,
        "free": format_bytes(free),
        "used_bytes": used,
        "free_bytes": free,
        "readable": disk_read if disks else total > 0,
        "counts": count_titles(conn),
        "disks": disks,
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
