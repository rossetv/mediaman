"""Scan engine — orchestrates a full Plex library scan.

For each configured library the engine:
1. Fetches all items from Plex.
2. Upserts each item into ``media_items``.
3. Skips items that are protected (forever or active snooze).
4. Skips items already awaiting deletion.
5. Evaluates eligibility via ``evaluate_movie`` / ``evaluate_season``.
6. Schedules eligible items: inserts into ``scheduled_actions`` with an HMAC
   token and marks re-entries where a prior snooze has expired.
7. Writes an ``audit_log`` entry for every scheduled action.
8. Sends a newsletter to all active subscribers via Mailgun.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from mediaman.auth.audit import log_audit
from mediaman.crypto import generate_keep_token
from mediaman.scanner.movies import evaluate_movie
from mediaman.scanner.tv import evaluate_season
from mediaman.services.format import ensure_tz as _ensure_tz
from mediaman.services.format import parse_iso_utc as _parse_iso_utc
from mediaman.services.storage import delete_path

logger = logging.getLogger("mediaman")

# Actions that mean "this item is actively protected — do not touch"
_PROTECTION_ACTIONS = {"protected_forever", "snoozed"}

# The action that means deletion is already lined up
_DELETION_ACTION = "scheduled_deletion"

# Default token TTL: 30 days from now
_TOKEN_TTL_DAYS = 30


@dataclass
class _PlexItemFetch:
    """Network-read handoff between the scanner's fetch and write phases.

    The scanner fetches a library's full contents (items + watch history)
    from Plex into a list of these in phase 1, then phase 2 consumes the
    list with no further network calls. Keeps the SQLite write lock off
    the critical path of any HTTP round-trip.
    """

    item: dict
    library_id: str
    media_type: str
    watch_history: list[dict]


class ScanEngine:
    """Orchestrates a full library scan across one or more Plex library sections.

    Args:
        conn: Open SQLite connection (with row_factory set to sqlite3.Row).
        plex_client: Object providing ``get_movie_items``, ``get_show_seasons``,
            ``get_watch_history``, and ``get_season_watch_history``.
        library_ids: Ordered list of Plex section IDs to scan.
        library_types: Mapping of library_id → ``"movie"`` or ``"show"``.
        library_titles: Mapping of library_id → lowercase library title (e.g. ``"anime"``).
        secret_key: HMAC secret used to sign keep tokens.
        min_age_days: Minimum days since added before eligibility is assessed.
        inactivity_days: Days without a watch event before deletion is triggered.
        grace_days: Days from *now* until the scheduled deletion executes.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        plex_client: Any,
        library_ids: list[str],
        library_types: dict[str, str],
        library_titles: dict[str, str] | None = None,
        secret_key: str,
        min_age_days: int = 30,
        inactivity_days: int = 30,
        grace_days: int = 14,
        dry_run: bool = False,
        sonarr_client: Any = None,
        radarr_client: Any = None,
    ) -> None:
        self._conn = conn
        self._plex = plex_client
        self._library_ids = library_ids
        self._library_types = library_types
        self._library_titles = library_titles or {}
        self._secret_key = secret_key
        self._min_age_days = min_age_days
        self._inactivity_days = inactivity_days
        self._grace_days = grace_days
        self._dry_run = dry_run
        self._sonarr = sonarr_client
        self._radarr = radarr_client
        self._arr_dates: dict[str, str] = {}  # normalised_path → ISO date
        self._build_arr_date_cache()

    def _load_delete_allowed_roots(self) -> list[str]:
        """Read the filesystem roots under which deletions are permitted.

        Pulled from the ``delete_allowed_roots`` setting (JSON list) or
        the ``MEDIAMAN_DELETE_ROOTS`` env var (colon-separated). When
        neither is configured, an empty list is returned — the caller is
        expected to treat this as fail-closed (``delete_path`` will raise
        ``ValueError``) so we never rmtree without an allowlist.
        """
        import json
        import os

        row = self._conn.execute(
            "SELECT value FROM settings WHERE key='delete_allowed_roots'"
        ).fetchone()
        roots: list[str] = []
        if row and row["value"]:
            try:
                parsed = json.loads(row["value"])
                if isinstance(parsed, list):
                    roots = [str(r) for r in parsed if r]
            except (ValueError, TypeError):
                pass
        if not roots:
            env_val = os.environ.get("MEDIAMAN_DELETE_ROOTS", "")
            roots = [r.strip() for r in env_val.split(":") if r.strip()]
        if not roots:
            logger.error(
                "delete_allowed_roots is not configured — all deletions "
                "will be refused. Set the delete_allowed_roots setting "
                "(JSON list) or the MEDIAMAN_DELETE_ROOTS env var "
                "(colon-separated) to re-enable deletions."
            )
        return roots

    @staticmethod
    def _normalise_path(path: str) -> str:
        """Strip container-specific root prefixes for cross-container matching.

        Plex, Radarr, and Sonarr each mount the same directories under
        different roots (e.g. ``/data/movies/...``, ``/movies/...``).
        This strips the first path component so matching works regardless
        of container mount point.
        """
        # "/data/movies/Film (2020)/Film.mkv" → "movies/Film (2020)/Film.mkv"
        # "/movies/Film (2020)/Film.mkv"      → "movies/Film (2020)/Film.mkv"
        parts = path.strip("/").split("/", 1)
        if len(parts) < 2:
            return path
        # If first component is a generic root like "data", strip it too
        if parts[0] in ("data", "media", "share"):
            return parts[1]
        return path.strip("/")

    def _build_arr_date_cache(self) -> None:
        """Build a lookup of normalised file paths → download dates from Radarr/Sonarr."""
        # Radarr: movieFile.dateAdded keyed by movie file path
        if self._radarr:
            try:
                for movie in self._radarr.get_movies():
                    mf = movie.get("movieFile")
                    if mf and mf.get("path") and mf.get("dateAdded"):
                        key = self._normalise_path(mf["path"])
                        self._arr_dates[key] = mf["dateAdded"]
            except Exception:
                logger.warning("Failed to fetch Radarr dates — falling back to Plex")

        # Sonarr: episodefile.dateAdded keyed by season directory → latest date
        if self._sonarr:
            try:
                for series in self._sonarr.get_series():
                    try:
                        efs = self._sonarr._get(f"/api/v3/episodefile?seriesId={series['id']}")
                        for ef in efs:
                            path = ef.get("path", "")
                            date_added = ef.get("dateAdded", "")
                            if path and date_added:
                                season_dir = path.rsplit("/", 1)[0]
                                key = self._normalise_path(season_dir)
                                existing = self._arr_dates.get(key, "")
                                if date_added > existing:
                                    self._arr_dates[key] = date_added
                    except Exception:
                        logger.warning(
                            "Failed to fetch episode files for series %s", series.get("id"), exc_info=True
                        )
            except Exception:
                logger.warning("Failed to fetch Sonarr dates — falling back to Plex")

        if self._arr_dates:
            logger.info("Cached %d download dates from Radarr/Sonarr", len(self._arr_dates))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_library(self) -> dict[str, int]:
        """Sync media items from Plex without evaluating for deletion.

        A lightweight alternative to :meth:`run_scan` that only fetches
        current library state from Plex and updates the ``media_items``
        table. No eligibility checks, no deletions, no newsletter.

        Also removes orphaned entries whose ``plex_rating_key`` no longer
        exists in Plex (e.g. after a delete-and-redownload cycle where
        Plex assigns a new key).

        Split into two phases so network I/O never overlaps with an open
        SQLite write transaction:

        1. **Fetch phase** — pull every library's items and their watch
           history from Plex into an in-memory buffer. No DB writes.
        2. **Write phase** — tight loop of UPSERTs, one ``commit()`` at
           the end. Lock is held for milliseconds regardless of library
           size, so concurrent session-validation writes never stall.
        """
        summary = {"synced": 0, "errors": 0, "removed": 0}

        # Phase 1: network reads. No DB writes, no lock.
        buffered: list[_PlexItemFetch] = []
        scanned_libs: set[int] = set()
        for lib_id in self._library_ids:
            try:
                buffered.extend(self._fetch_library_items(lib_id))
                scanned_libs.add(int(lib_id))
            except Exception:
                logger.exception("Library sync failed for library %s", lib_id)
                summary["errors"] += 1

        seen_keys: set[str] = {f.item["plex_rating_key"] for f in buffered}

        # Phase 2: single short write transaction covering every UPSERT
        # plus the orphan cleanup. No network calls happen past this
        # point, so the write lock is held only for the DB work itself.
        for f in buffered:
            self._upsert_media_item(f.item, f.library_id, f.media_type)
            if f.watch_history:
                self._update_last_watched(
                    f.item["plex_rating_key"], f.watch_history
                )
            summary["synced"] += 1
        summary["removed"] = self._remove_orphaned_items(
            seen_keys, scanned_libs
        )

        self._conn.commit()
        logger.info(
            "Library sync complete: %d synced, %d orphans removed, %d errors",
            summary["synced"], summary["removed"], summary["errors"],
        )
        return summary

    def _fetch_library_items(self, library_id: str) -> list["_PlexItemFetch"]:
        """Fetch items + watch history for a library from Plex.

        Pure network-read helper; touches no DB. Returns one
        :class:`_PlexItemFetch` per movie or per season. A failed
        watch-history lookup yields an empty list (same semantics as the
        pre-split code's ``except Exception: pass``).
        """
        lib_type = self._library_types.get(library_id, "movie")
        out: list[_PlexItemFetch] = []
        if lib_type == "show":
            seasons = self._plex.get_show_seasons(library_id)
            lib_title = self._library_titles.get(library_id, "")
            default_anime = "anime" in lib_title
            for season in seasons:
                media_type = (
                    "anime_season"
                    if season.get("is_anime", default_anime)
                    else "tv_season"
                )
                try:
                    watch_history = self._plex.get_season_watch_history(
                        season["plex_rating_key"]
                    )
                except Exception:
                    watch_history = []
                out.append(_PlexItemFetch(
                    item=season,
                    library_id=library_id,
                    media_type=media_type,
                    watch_history=watch_history,
                ))
        else:
            items = self._plex.get_movie_items(library_id)
            for item in items:
                try:
                    watch_history = self._plex.get_watch_history(
                        item["plex_rating_key"]
                    )
                except Exception:
                    watch_history = []
                out.append(_PlexItemFetch(
                    item=item,
                    library_id=library_id,
                    media_type="movie",
                    watch_history=watch_history,
                ))
        return out

    def run_scan(self) -> dict[str, int]:
        """Execute a full scan and return a summary dict.

        Returns:
            Dict with the following integer keys:

            - ``scanned``: total items examined across all libraries.
            - ``scheduled``: items newly scheduled for deletion this run.
            - ``skipped``: items skipped (protected, already scheduled, or ineligible).
            - ``errors``: items that raised an unexpected exception during processing.
            - ``removed``: orphaned DB rows whose Plex rating key no longer exists.
            - ``deleted``: items whose grace period elapsed and were deleted from disk.
            - ``reclaimed_bytes``: total bytes freed by deletions this run.
        """
        summary = {"scanned": 0, "scheduled": 0, "skipped": 0, "errors": 0}
        seen_keys: set[str] = set()

        for lib_id in self._library_ids:
            lib_type = self._library_types.get(lib_id, "movie")
            if lib_type == "show":
                self._scan_tv_library(lib_id, summary, seen_keys)
            else:
                self._scan_movie_library(lib_id, summary, seen_keys)

        # All libraries scanned — clean up orphans
        all_libs = {int(lid) for lid in self._library_ids}
        summary["removed"] = self._remove_orphaned_items(seen_keys, all_libs)

        self._conn.commit()

        deletion_result = self.execute_deletions()
        summary["deleted"] = deletion_result["deleted"]
        summary["reclaimed_bytes"] = deletion_result["reclaimed_bytes"]

        try:
            from mediaman.services.newsletter import send_newsletter
            send_newsletter(
                conn=self._conn,
                secret_key=self._secret_key,
                dry_run=self._dry_run,
                grace_days=self._grace_days,
            )
        except Exception:
            logger.exception("Newsletter sending failed — scan results unaffected")

        # Refresh AI recommendations if enabled
        rec_row = self._conn.execute(
            "SELECT value FROM settings WHERE key='suggestions_enabled'"
        ).fetchone()
        if not rec_row or rec_row["value"] != "false":
            try:
                from mediaman.services.openai_recommendations import refresh_recommendations
                refresh_recommendations(self._conn, self._plex)
            except Exception:
                logger.exception("Recommendation generation failed — scan results unaffected")

        return summary

    def execute_deletions(self) -> dict:
        """Execute pending deletions where the grace period has passed.

        Also cleans up expired snooze rows so those items re-enter the scan
        pipeline on the next run.

        Returns:
            Dict with ``deleted`` count and ``reclaimed_bytes`` total.
        """
        now = datetime.now(timezone.utc)
        deleted_count = 0
        reclaimed_bytes = 0

        allowed_roots = self._load_delete_allowed_roots()

        rows = self._conn.execute(
            "SELECT sa.id, sa.media_item_id, mi.file_path, mi.file_size_bytes, "
            "mi.radarr_id, mi.sonarr_id, mi.season_number, mi.title, mi.plex_rating_key "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.action = 'scheduled_deletion' AND sa.execute_at < ?",
            (now.isoformat(),),
        ).fetchall()

        for row in rows:
            if self._dry_run:
                log_audit(self._conn, row["media_item_id"], "dry_run_skip", f"Would delete: {row['title']}")
                continue

            # Remove files from disk
            if not allowed_roots:
                logger.error(
                    "Skipping deletion of '%s': delete_allowed_roots not "
                    "configured. Set the setting or `MEDIAMAN_DELETE_ROOTS` "
                    "env var.", row["file_path"],
                )
                continue
            try:
                delete_path(row["file_path"], allowed_roots=allowed_roots)
            except ValueError as exc:
                logger.error(
                    "Refusing to delete '%s' — path is outside configured "
                    "delete_allowed_roots: %s", row["file_path"], exc
                )
                continue

            # Record the deletion and close the transaction *before*
            # the Radarr/Sonarr unmonitor HTTP calls. The unmonitor is
            # best-effort housekeeping — a failure (or slow response)
            # must not keep the SQLite write lock open.
            log_audit(
                self._conn,
                row["media_item_id"],
                "deleted",
                "Deleted: {}{}".format(row["title"], f" [rk:{row['plex_rating_key']}]" if row["plex_rating_key"] else ""),
                space_bytes=row["file_size_bytes"],
            )
            self._conn.execute(
                "DELETE FROM scheduled_actions WHERE id = ?", (row["id"],)
            )
            self._conn.commit()

            # Unmonitor in *arr clients — failures are non-fatal and
            # happen outside any open transaction.
            if row["radarr_id"] and self._radarr:
                try:
                    self._radarr.unmonitor_movie(row["radarr_id"])
                except Exception:
                    logger.warning(
                        "Failed to unmonitor movie %s after deletion", row["radarr_id"], exc_info=True
                    )

            if row["sonarr_id"] and row["season_number"] is not None and self._sonarr:
                try:
                    self._sonarr.unmonitor_season(row["sonarr_id"], row["season_number"])
                except Exception:
                    logger.warning(
                        "Failed to unmonitor season %s of series %s after deletion",
                        row["season_number"], row["sonarr_id"], exc_info=True
                    )

            deleted_count += 1
            reclaimed_bytes += row["file_size_bytes"] or 0

        # Remove expired snoozes so items re-enter the scan pipeline
        self._conn.execute(
            "DELETE FROM scheduled_actions WHERE action = 'snoozed' AND execute_at < ?",
            (now.isoformat(),),
        )

        self._conn.commit()
        return {"deleted": deleted_count, "reclaimed_bytes": reclaimed_bytes}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _scan_movie_library(
        self, library_id: str, summary: dict, seen_keys: set[str] | None = None,
    ) -> None:
        # Phase 1: pull items + watch histories from Plex (no DB writes,
        # no lock). See :meth:`sync_library` for rationale.
        fetched = self._fetch_library_items(library_id)

        # Phase 2: DB-only work. All reads and writes now happen without
        # any intervening network calls, so the write transaction that
        # opens on the first UPSERT closes within milliseconds at the
        # caller's ``commit()``.
        for f in fetched:
            summary["scanned"] += 1
            item = f.item
            watch_history = f.watch_history
            try:
                media_id = item["plex_rating_key"]
                if seen_keys is not None:
                    seen_keys.add(media_id)
                self._upsert_media_item(item, library_id, "movie")
                self._update_last_watched(media_id, watch_history)

                if self._is_protected(media_id):
                    summary["skipped"] += 1
                    continue

                if self._is_already_scheduled(media_id):
                    summary["skipped"] += 1
                    continue

                # Use updated_at (same as what's stored in DB) for age check
                arr_date_str = self._arr_dates.get(self._normalise_path(item.get("file_path", "")))
                if arr_date_str:
                    raw_added = _parse_iso_utc(arr_date_str) or datetime.now(timezone.utc)
                else:
                    raw_added = item.get("updated_at") or item.get("added_at")
                added_at = _ensure_tz(raw_added)
                decision = evaluate_movie(
                    added_at=added_at,
                    watch_history=watch_history,
                    min_age_days=self._min_age_days,
                    inactivity_days=self._inactivity_days,
                )

                if decision == "schedule_deletion":
                    is_reentry = self._has_expired_snooze(media_id)
                    self._schedule_deletion(media_id, is_reentry)
                    summary["scheduled"] += 1
                else:
                    summary["skipped"] += 1
            except Exception:
                summary["errors"] += 1
                logger.exception(
                    "Movie scan item failed (plex_rating_key=%s)",
                    item.get("plex_rating_key", "?"),
                )

    def _scan_tv_library(
        self, library_id: str, summary: dict, seen_keys: set[str] | None = None,
    ) -> None:
        # Phase 1: network fetch (see :meth:`sync_library`).
        fetched = self._fetch_library_items(library_id)

        # Phase 2: DB-only work inside a single write transaction.
        for f in fetched:
            summary["scanned"] += 1
            season = f.item
            watch_history = f.watch_history
            try:
                media_id = season["plex_rating_key"]
                if seen_keys is not None:
                    seen_keys.add(media_id)
                self._upsert_media_item(season, library_id, f.media_type)
                self._update_last_watched(media_id, watch_history)

                show_rk = season.get("show_rating_key")
                if self._is_show_kept(show_rk):
                    summary["skipped"] += 1
                    continue

                if self._is_protected(media_id):
                    summary["skipped"] += 1
                    continue

                if self._is_already_scheduled(media_id):
                    summary["skipped"] += 1
                    continue

                arr_date_str = self._arr_dates.get(self._normalise_path(season.get("file_path", "")))
                if arr_date_str:
                    raw_added = _parse_iso_utc(arr_date_str) or datetime.now(timezone.utc)
                else:
                    raw_added = season.get("updated_at") or season.get("added_at")
                added_at = _ensure_tz(raw_added)
                decision = evaluate_season(
                    added_at=added_at,
                    episode_count=season.get("episode_count", 0),
                    watch_history=watch_history,
                    has_future_episodes=False,
                    min_age_days=self._min_age_days,
                    inactivity_days=self._inactivity_days,
                )

                if decision == "schedule_deletion":
                    is_reentry = self._has_expired_snooze(media_id)
                    self._schedule_deletion(media_id, is_reentry)
                    summary["scheduled"] += 1
                else:
                    summary["skipped"] += 1
            except Exception:
                summary["errors"] += 1
                logger.exception(
                    "TV scan item failed (plex_rating_key=%s)",
                    season.get("plex_rating_key", "?"),
                )

    def _remove_orphaned_items(
        self, seen_keys: set[str], scanned_libs: set[int],
    ) -> int:
        """Remove media_items entries whose plex_rating_key no longer exists in Plex.

        Only considers items belonging to libraries that were successfully
        scanned (so we don't accidentally delete items from a library that
        was unreachable during this sync).
        """
        if not scanned_libs:
            return 0

        placeholders = ",".join("?" * len(scanned_libs))
        rows = self._conn.execute(
            f"SELECT id FROM media_items WHERE plex_library_id IN ({placeholders})",  # noqa: S608 — placeholders are '?' only, not user input
            tuple(scanned_libs),
        ).fetchall()

        orphans = [r["id"] for r in rows if r["id"] not in seen_keys]
        if not orphans:
            return 0

        # Batch deletes in chunks so we don't hit sqlite's parameter limit.
        for start in range(0, len(orphans), 500):
            chunk = orphans[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            self._conn.execute(
                f"DELETE FROM scheduled_actions WHERE media_item_id IN ({placeholders})",  # noqa: S608 — placeholders are '?' only, not user input
                tuple(chunk),
            )
            self._conn.execute(
                f"DELETE FROM media_items WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are '?' only, not user input
                tuple(chunk),
            )

        logger.info(
            "Removed %d orphaned media items no longer in Plex", len(orphans),
        )
        return len(orphans)

    def _upsert_media_item(
        self, item: dict, library_id: str, media_type: str
    ) -> None:
        """Insert or update a media item record.

        Uses the download date from Radarr/Sonarr when available (most
        accurate), falling back to Plex's ``addedAt``. The ``added_at``
        column is always updated to reflect the best known date.
        """
        now = datetime.now(timezone.utc).isoformat()
        file_path = item.get("file_path", "")

        # Prefer Radarr/Sonarr download date (exact), fall back to Plex
        arr_date = self._arr_dates.get(self._normalise_path(file_path))
        if arr_date:
            _parsed = _parse_iso_utc(arr_date)
            added_at = _parsed.isoformat() if _parsed else arr_date
        else:
            added_at = item.get("added_at")
            if isinstance(added_at, datetime):
                added_at = _ensure_tz(added_at).isoformat()
            elif added_at is None:
                added_at = now

        self._conn.execute(
            """
            INSERT INTO media_items (
                id, title, media_type, show_title, season_number,
                plex_library_id, plex_rating_key, show_rating_key,
                added_at, file_path, file_size_bytes, poster_path, last_scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                media_type = excluded.media_type,
                show_rating_key = excluded.show_rating_key,
                added_at = excluded.added_at,
                file_path = excluded.file_path,
                file_size_bytes = excluded.file_size_bytes,
                poster_path = excluded.poster_path,
                last_scanned_at = excluded.last_scanned_at
            """,
            (
                item["plex_rating_key"],
                item["title"],
                media_type,
                item.get("show_title"),
                item.get("season_number"),
                int(library_id),
                item["plex_rating_key"],
                item.get("show_rating_key"),
                added_at,
                item.get("file_path", ""),
                item.get("file_size_bytes", 0),
                item.get("poster_path"),
                now,
            ),
        )

    def _is_protected(self, media_id: str) -> bool:
        """Return True if the item has an active protection action.

        An item is protected if it has a ``protected_forever`` action (regardless
        of ``token_used``) or a ``snoozed`` action whose ``execute_at`` is still
        in the future.
        """
        now = datetime.now(timezone.utc).isoformat()
        row = self._conn.execute(
            """
            SELECT action, execute_at FROM scheduled_actions
            WHERE media_item_id = ?
              AND action IN ('protected_forever', 'snoozed')
            ORDER BY id DESC LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            return False
        if row["action"] == "protected_forever":
            return True
        # Snoozed — only protected if execute_at is in the future
        return row["execute_at"] is not None and row["execute_at"] > now

    def _is_already_scheduled(self, media_id: str) -> bool:
        """Return True if deletion is already pending for this item."""
        row = self._conn.execute(
            """
            SELECT id FROM scheduled_actions
            WHERE media_item_id = ? AND action = 'scheduled_deletion' AND token_used = 0
            LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        return row is not None

    def _has_expired_snooze(self, media_id: str) -> bool:
        """Return True if the item has a prior snoozed action that was consumed."""
        row = self._conn.execute(
            """
            SELECT id FROM scheduled_actions
            WHERE media_item_id = ? AND action = 'snoozed' AND token_used = 1
            LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        return row is not None

    def _is_show_kept(self, show_rating_key: str | None) -> bool:
        """Return True if the show has an active keep rule in kept_shows."""
        if not show_rating_key:
            return False
        now = datetime.now(timezone.utc).isoformat()
        row = self._conn.execute(
            """
            SELECT id, action, execute_at FROM kept_shows
            WHERE show_rating_key = ?
            LIMIT 1
            """,
            (show_rating_key,),
        ).fetchone()
        if row is None:
            return False
        if row["action"] == "protected_forever":
            return True
        if row["execute_at"] and row["execute_at"] > now:
            return True
        # Expired snooze — clean up
        self._conn.execute("DELETE FROM kept_shows WHERE id = ?", (row["id"],))
        return False

    def _update_last_watched(self, media_id: str, watch_history: list[dict]) -> None:
        """Store the most recent watch timestamp for a media item."""
        if not watch_history:
            return
        latest = max(
            (h["viewed_at"] for h in watch_history if h.get("viewed_at")),
            default=None,
        )
        if latest is None:
            return
        latest = _ensure_tz(latest)
        self._conn.execute(
            "UPDATE media_items SET last_watched_at = ? WHERE id = ?",
            (latest.isoformat(), media_id),
        )

    def _schedule_deletion(self, media_id: str, is_reentry: bool) -> None:
        """Insert a scheduled_deletion row and write an audit entry.

        Uses a unique random placeholder token for the initial insert so
        the ``token`` unique index can't collide between concurrent
        scheduler runs, then swaps in the real HMAC-signed keep token
        once we know the row id.
        """
        import secrets as _secrets

        now = datetime.now(timezone.utc)
        execute_at = now + timedelta(days=self._grace_days)
        expires_at = int((now + timedelta(days=_TOKEN_TTL_DAYS)).timestamp())

        placeholder = f"pending-{_secrets.token_urlsafe(16)}"

        cursor = self._conn.execute(
            """
            INSERT INTO scheduled_actions
                (media_item_id, action, scheduled_at, execute_at, token, token_used, is_reentry)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                media_id,
                _DELETION_ACTION,
                now.isoformat(),
                execute_at.isoformat(),
                placeholder,
                1 if is_reentry else 0,
            ),
        )
        action_id = cursor.lastrowid

        token = generate_keep_token(
            media_item_id=media_id,
            action_id=action_id,
            expires_at=expires_at,
            secret_key=self._secret_key,
        )
        self._conn.execute(
            "UPDATE scheduled_actions SET token = ? WHERE id = ?",
            (token, action_id),
        )

        log_audit(
            self._conn,
            media_id,
            _DELETION_ACTION,
            "scheduled by scan engine" + (" (re-entry)" if is_reentry else ""),
        )


