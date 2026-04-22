"""Shared scan execution logic.

Extracts the "build everything from DB settings and run a scan" logic so it
can be called from both the manual trigger route (scan.py) and the scheduled
lifespan job (main.py) without duplication.
"""

import json
import logging
import sqlite3

from mediaman.services.arr_build import (
    build_radarr_from_db as _build_radarr,
    build_sonarr_from_db as _build_sonarr,
)
from mediaman.services.settings_reader import get_int_setting as _get_int_setting
from mediaman.services.storage import get_disk_usage

logger = logging.getLogger("mediaman")


def _load_library_ids(conn: sqlite3.Connection) -> list[str]:
    """Read plex_libraries from settings, returning [] on missing or corrupt JSON."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_libraries'"
    ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        logger.warning("plex_libraries setting contains invalid JSON — scanning no libraries")
        return []



def _filter_libraries_by_disk(
    conn: sqlite3.Connection,
    lib_ids: list[str],
    lib_titles: dict[str, str],
) -> list[str]:
    """Remove libraries whose disk usage is below their configured threshold.

    Libraries with no threshold, threshold of 0, or any exception from
    ``get_disk_usage`` are always included (fail open).
    """
    raw = conn.execute(
        "SELECT value FROM settings WHERE key='disk_thresholds'"
    ).fetchone()
    if not raw:
        return lib_ids

    try:
        thresholds = json.loads(raw["value"])
    except (json.JSONDecodeError, TypeError):
        return lib_ids

    filtered = []
    for lib_id in lib_ids:
        cfg = thresholds.get(lib_id)
        if not cfg or not cfg.get("path") or not cfg.get("threshold"):
            filtered.append(lib_id)
            continue

        try:
            threshold_pct = int(cfg["threshold"])
        except (ValueError, TypeError):
            logger.warning(
                "Invalid threshold '%s' for library '%s' — scanning anyway",
                cfg["threshold"], lib_titles.get(lib_id, lib_id),
            )
            filtered.append(lib_id)
            continue
        if threshold_pct <= 0:
            filtered.append(lib_id)
            continue

        try:
            usage = get_disk_usage(cfg["path"])
            total = usage["total_bytes"]
            used = usage["used_bytes"]
            current_pct = (used / total * 100) if total > 0 else 0
        except Exception:
            logger.warning(
                "Disk check failed for library '%s' (path: %s) — scanning anyway",
                lib_titles.get(lib_id, lib_id), cfg["path"],
            )
            filtered.append(lib_id)
            continue

        if current_pct >= threshold_pct:
            filtered.append(lib_id)
        else:
            logger.info(
                "Skipped library '%s' — disk at %.1f%%, threshold %d%%",
                lib_titles.get(lib_id, lib_id), current_pct, threshold_pct,
            )

    return filtered


def _build_plex_client(
    conn: sqlite3.Connection, secret_key: str
) -> "tuple | None":
    """Build a PlexClient and resolve library metadata from DB settings.

    Returns a ``(plex, lib_ids, lib_types, lib_titles)`` tuple, or ``None``
    if the required ``plex_url`` / ``plex_token`` settings are absent.

    The caller is responsible for any filtering or further configuration
    (disk thresholds, *arr clients, etc.) before constructing a ScanEngine.
    """
    from mediaman.crypto import decrypt_value
    from mediaman.services.plex import PlexClient

    plex_url_row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_url'"
    ).fetchone()
    plex_token_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='plex_token'"
    ).fetchone()

    if not plex_url_row or not plex_token_row:
        return None

    token_val = plex_token_row["value"]
    if plex_token_row["encrypted"]:
        token_val = decrypt_value(token_val, secret_key, conn=conn, aad=b"plex_token")

    lib_ids = _load_library_ids(conn)
    plex = PlexClient(plex_url_row["value"], token_val)
    plex_libs = plex.get_libraries()
    lib_types: dict[str, str] = {lib["id"]: lib["type"] for lib in plex_libs}
    lib_titles: dict[str, str] = {lib["id"]: lib["title"].lower() for lib in plex_libs}

    return plex, lib_ids, lib_types, lib_titles


def run_scan_from_db(conn: sqlite3.Connection, secret_key: str, *, skip_disk_check: bool = False) -> dict:
    """Build a ScanEngine from DB settings and execute a full scan.

    Reads all required configuration from the ``settings`` table — Plex URL/
    token, library IDs, threshold values, and optional Sonarr/Radarr URLs —
    then constructs the relevant clients and runs the scan.

    Returns the summary dict from :meth:`ScanEngine.run_scan`, or an empty
    dict if the minimum required settings (plex_url / plex_token) are absent.

    Args:
        conn: Open SQLite connection with row_factory set to sqlite3.Row.
        secret_key: Application secret used for decrypting stored tokens and
            signing HMAC keep tokens.
        skip_disk_check: When True, bypass the per-library disk-usage threshold
            check and scan all configured libraries unconditionally.
    """
    from mediaman.scanner.engine import ScanEngine

    result = _build_plex_client(conn, secret_key)
    if result is None:
        logger.warning("Scan skipped — plex_url or plex_token not configured")  # noqa: S105 — no token value logged, only the string "plex_token"
        return {}
    plex, lib_ids, lib_types, lib_titles = result

    # ── Disk threshold filtering ────────────────────────────────────────────
    if not skip_disk_check:
        lib_ids = _filter_libraries_by_disk(conn, lib_ids, lib_titles)

    # ── Optional *arr clients ────────────────────────────────────────────────
    sonarr_client = _build_sonarr(conn, secret_key)
    radarr_client = _build_radarr(conn, secret_key)

    # ── Thresholds ───────────────────────────────────────────────────────────
    min_age = _get_int_setting(conn, "min_age_days", default=30)
    inactivity = _get_int_setting(conn, "inactivity_days", default=30)
    grace = _get_int_setting(conn, "grace_days", default=14)
    dry_run_row = conn.execute(
        "SELECT value FROM settings WHERE key='dry_run'"
    ).fetchone()
    dry_run = bool(dry_run_row and dry_run_row["value"] == "true")

    # ── Run ──────────────────────────────────────────────────────────────────
    engine = ScanEngine(
        conn=conn,
        plex_client=plex,
        library_ids=lib_ids,
        library_types=lib_types,
        library_titles=lib_titles,
        secret_key=secret_key,
        min_age_days=min_age,
        inactivity_days=inactivity,
        grace_days=grace,
        dry_run=dry_run,
        sonarr_client=sonarr_client,
        radarr_client=radarr_client,
    )
    return engine.run_scan()


def run_library_sync(conn: sqlite3.Connection, secret_key: str) -> dict:
    """Sync library from Plex without running deletion evaluation.

    A lightweight operation that updates media_items from Plex. No
    eligibility checks, no deletions, no newsletter. Designed to run
    frequently (every N minutes) to keep the Library page current.
    """
    from mediaman.scanner.engine import ScanEngine

    result = _build_plex_client(conn, secret_key)
    if result is None:
        logger.debug("Library sync skipped — Plex not configured")
        return {}
    plex, lib_ids, lib_types, lib_titles = result

    engine = ScanEngine(
        conn=conn,
        plex_client=plex,
        library_ids=lib_ids,
        library_types=lib_types,
        library_titles=lib_titles,
        secret_key=secret_key,
        min_age_days=0,
        inactivity_days=0,
        grace_days=0,
        dry_run=True,
    )
    result = engine.sync_library()

    # Check for completed downloads and notify requester by email
    try:
        from mediaman.services.download_notifications import check_download_notifications
        check_download_notifications(conn, secret_key)
    except Exception:
        logger.exception("Download notification check failed — sync results unaffected")

    return result
