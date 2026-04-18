"""Shared scan execution logic.

Extracts the "build everything from DB settings and run a scan" logic so it
can be called from both the manual trigger route (scan.py) and the scheduled
lifespan job (main.py) without duplication.
"""

import json
import logging
import sqlite3

from mediaman.services.storage import get_disk_usage

logger = logging.getLogger("mediaman")


def _get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Read a plain-text setting from the DB, falling back to *default*."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row else default


def _get_int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    """Read an integer setting from the DB, falling back to *default*."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row:
        try:
            return int(row["value"])
        except (ValueError, TypeError):
            pass
    return default


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
    from mediaman.crypto import decrypt_value
    from mediaman.scanner.engine import ScanEngine
    from mediaman.services.plex import PlexClient

    # ── Required Plex settings ───────────────────────────────────────────────
    plex_url_row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_url'"
    ).fetchone()
    plex_token_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='plex_token'"
    ).fetchone()

    if not plex_url_row or not plex_token_row:
        logger.warning("Scan skipped — plex_url or plex_token not configured")
        return {}

    token_val = plex_token_row["value"]
    if plex_token_row["encrypted"]:
        token_val = decrypt_value(token_val, secret_key, conn=conn)

    # ── Library IDs ──────────────────────────────────────────────────────────
    libraries_row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_libraries'"
    ).fetchone()
    lib_ids: list[str] = json.loads(libraries_row["value"]) if libraries_row else []

    plex = PlexClient(plex_url_row["value"], token_val)

    # Derive library types from Plex
    plex_libs = plex.get_libraries()
    lib_types: dict[str, str] = {lib["id"]: lib["type"] for lib in plex_libs}
    lib_titles: dict[str, str] = {lib["id"]: lib["title"].lower() for lib in plex_libs}

    # ── Disk threshold filtering ────────────────────────────────────────────
    if not skip_disk_check:
        lib_ids = _filter_libraries_by_disk(conn, lib_ids, lib_titles)

    # ── Optional *arr clients ────────────────────────────────────────────────
    sonarr_client = None
    radarr_client = None

    sonarr_url = _get_setting(conn, "sonarr_url")
    sonarr_key_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='sonarr_api_key'"
    ).fetchone()
    if sonarr_url and sonarr_key_row:
        from mediaman.services.sonarr import SonarrClient
        sonarr_key = sonarr_key_row["value"]
        if sonarr_key_row["encrypted"]:
            sonarr_key = decrypt_value(sonarr_key, secret_key, conn=conn)
        sonarr_client = SonarrClient(sonarr_url, sonarr_key)

    radarr_url = _get_setting(conn, "radarr_url")
    radarr_key_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='radarr_api_key'"
    ).fetchone()
    if radarr_url and radarr_key_row:
        from mediaman.services.radarr import RadarrClient
        radarr_key = radarr_key_row["value"]
        if radarr_key_row["encrypted"]:
            radarr_key = decrypt_value(radarr_key, secret_key, conn=conn)
        radarr_client = RadarrClient(radarr_url, radarr_key)

    # ── Thresholds ───────────────────────────────────────────────────────────
    min_age = _get_int_setting(conn, "min_age_days", 30)
    inactivity = _get_int_setting(conn, "inactivity_days", 30)
    grace = _get_int_setting(conn, "grace_days", 14)
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
    from mediaman.crypto import decrypt_value
    from mediaman.scanner.engine import ScanEngine
    from mediaman.services.plex import PlexClient

    plex_url_row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_url'"
    ).fetchone()
    plex_token_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='plex_token'"
    ).fetchone()

    if not plex_url_row or not plex_token_row:
        logger.debug("Library sync skipped — Plex not configured")
        return {}

    token_val = plex_token_row["value"]
    if plex_token_row["encrypted"]:
        token_val = decrypt_value(token_val, secret_key, conn=conn)

    libraries_row = conn.execute(
        "SELECT value FROM settings WHERE key='plex_libraries'"
    ).fetchone()
    lib_ids: list[str] = json.loads(libraries_row["value"]) if libraries_row else []

    plex = PlexClient(plex_url_row["value"], token_val)
    plex_libs = plex.get_libraries()
    lib_types: dict[str, str] = {lib["id"]: lib["type"] for lib in plex_libs}
    lib_titles: dict[str, str] = {lib["id"]: lib["title"].lower() for lib in plex_libs}

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
