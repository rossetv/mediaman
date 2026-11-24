"""Shared scan execution logic.

Extracts the "build everything from DB settings and run a scan" logic so it
can be called from both the manual trigger route (scan.py) and the scheduled
lifespan job (main.py) without duplication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast

import requests

from mediaman.scanner import repository
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import (
    build_plex_from_db as _build_plex,
)
from mediaman.services.arr.build import (
    build_radarr_from_db as _build_radarr,
)
from mediaman.services.arr.build import (
    build_sonarr_from_db as _build_sonarr,
)
from mediaman.services.infra import SafeHTTPError, SSRFRefused, get_int_setting

if TYPE_CHECKING:
    from mediaman.services.media_meta.plex import PlexClient


# Lowest and highest value each cleanup day setting may take. The Settings
# page and its API enforce these on input; readers clamp to them as well, so
# a value stored before the bounds existed cannot drive a scan or a query.
DAY_SETTING_RANGES: dict[str, tuple[int, int]] = {
    "min_age_days": (1, 365),
    "inactivity_days": (1, 365),
    "grace_days": (1, 90),
}

# Value used when a day setting is missing or cannot be read.
DAY_SETTING_DEFAULTS: dict[str, int] = {
    "min_age_days": 30,
    "inactivity_days": 30,
    "grace_days": 14,
}


def get_day_setting(conn: sqlite3.Connection, key: str, *, default: int) -> int:
    """Return a cleanup day setting held to its allowed range.

    The stored row is never changed; only the value handed back is clamped.
    """
    low, high = DAY_SETTING_RANGES[key]
    return max(low, min(high, get_int_setting(conn, key, default=default)))


SCAN_SKIPPED_NO_PLEX = "Plex not configured"
SCAN_SKIPPED_PLEX_UNREACHABLE = "Plex could not be reached"


class ScanSummary(TypedDict, total=False):
    """Return type for run_scan_from_db and ScanEngine.run_scan.

    All keys are optional (``total=False``) because the type is also used
    for the lightweight mediaman.scanner.engine.ScanEngine.sync_library
    return value which only populates a subset.  The canonical full-scan
    keys are:

    * ``scanned`` — total items examined across all libraries.
    * ``scheduled`` — items newly scheduled for deletion.
    * ``skipped`` — items that were protected, already scheduled, or ineligible.
    * ``errors`` — items that raised an unexpected exception.
    * ``removed`` — orphaned DB rows whose Plex key no longer exists.
    * ``deleted`` — items deleted from disk this run.
    * ``reclaimed_bytes`` — bytes freed by deletions.
    * ``skipped_reason`` — set alone when the scan did not run at all.

    Do NOT add new keys here without also updating ScanEngine.run_scan
    and any callers that check specific keys — this is the single source of
    truth for the shape of the summary dict.
    """

    scanned: int
    scheduled: int
    skipped: int
    errors: int
    removed: int
    deleted: int
    reclaimed_bytes: int
    skipped_reason: str


class PlexClientBundle(NamedTuple):
    """Return type for _build_plex_client.

    NamedTuple (not TypedDict) because callers use positional tuple unpacking
    (``plex, lib_ids, lib_types, lib_titles = result``), which TypedDict
    doesn't support.
    """

    plex: PlexClient
    lib_ids: list[str]
    lib_types: dict[str, str]
    lib_titles: dict[str, str]


logger = logging.getLogger(__name__)


# Module-level Plex client cache. Rebuilding PlexClient on every
# ``run_library_sync`` call (every 30 min by default) would re-validate the
# URL via the SSRF guard and decrypt the stored token each time. The
# cache reuses the existing client until the underlying settings change, keyed
# on a hash of (raw plex_url, raw encrypted plex_token row). The hash
# deliberately uses the raw encrypted token so we never need to decrypt just
# to check freshness.
_PLEX_CLIENT_CACHE: dict[str, PlexClient] = {}
_PLEX_CLIENT_CACHE_LOCK = threading.Lock()


def _plex_settings_fingerprint(conn: sqlite3.Connection) -> str | None:
    """Return a stable fingerprint of the Plex-related settings.

    The fingerprint is an SHA-256 hash of the raw stored ``plex_url``
    plus the **raw encrypted** ``plex_token`` row (no decryption
    needed). Returns ``None`` when either setting is missing — callers
    treat that as "Plex not configured" and skip the cache.
    """
    url_row = conn.execute("SELECT value FROM settings WHERE key='plex_url'").fetchone()
    tok_row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
    if not url_row or not url_row["value"] or not tok_row or not tok_row["value"]:
        return None
    h = hashlib.sha256()
    h.update(b"plex_url:")
    h.update(str(url_row["value"]).encode("utf-8"))
    h.update(b"\x00plex_token:")
    h.update(str(tok_row["value"]).encode("utf-8"))
    return h.hexdigest()


def _reset_plex_client_cache() -> None:
    """Clear the cached Plex client. Test helper; safe to call any time."""
    with _PLEX_CLIENT_CACHE_LOCK:
        _PLEX_CLIENT_CACHE.clear()


def _load_library_ids(conn: sqlite3.Connection) -> list[str]:
    """Read plex_libraries from settings, returning [] on missing or corrupt JSON."""
    raw = repository.read_setting(conn, "plex_libraries")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
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
    raw = repository.read_setting(conn, "disk_thresholds")
    if not raw:
        return lib_ids

    try:
        thresholds = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return lib_ids

    # Fail open on a corrupt non-dict value (e.g. a JSON array or string):
    # the next line indexes ``thresholds.get(lib_id)``, which would raise
    # AttributeError and abort the whole scan. Mirror the isinstance guard
    # ``_load_library_ids`` applies to its list payload.
    if not isinstance(thresholds, dict):
        logger.warning(
            "disk_thresholds setting is not a JSON object — scanning all "
            "libraries (disk filter disabled for this run)."
        )
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
                cfg["threshold"],
                lib_titles.get(lib_id, lib_id),
            )
            filtered.append(lib_id)
            continue
        if threshold_pct <= 0:
            filtered.append(lib_id)
            continue

        try:
            usage = shutil.disk_usage(cfg["path"])
            total = usage.total
            used = usage.used
            current_pct = (used / total * 100) if total > 0 else 0
        except OSError:
            logger.warning(
                "Disk check failed for library '%s' (path: %s) — scanning anyway",
                lib_titles.get(lib_id, lib_id),
                cfg["path"],
            )
            filtered.append(lib_id)
            continue

        if current_pct >= threshold_pct:
            filtered.append(lib_id)
        else:
            logger.info(
                "Skipped library '%s' — disk at %.1f%%, threshold %d%%",
                lib_titles.get(lib_id, lib_id),
                current_pct,
                threshold_pct,
            )

    return filtered


def _get_or_build_plex(conn: sqlite3.Connection, secret_key: str) -> PlexClient | None:
    """Return a cached PlexClient, rebuilding only when settings change.

    Cache key: SHA-256 of (raw ``plex_url`` value, raw encrypted
    ``plex_token`` value). Any settings change invalidates the entry.

    Returns ``None`` when Plex is unconfigured. Re-raises ``SSRFRefused``
    from the SSRF guard to the caller so it can log + skip.

    Avoids the per-invocation cost of SSRF re-validation and token
    decryption on the hot ``run_library_sync`` path.
    """
    fp = _plex_settings_fingerprint(conn)
    if fp is None:
        # No usable settings — also clear any stale cached client.
        with _PLEX_CLIENT_CACHE_LOCK:
            _PLEX_CLIENT_CACHE.clear()
        return None

    with _PLEX_CLIENT_CACHE_LOCK:
        cached = _PLEX_CLIENT_CACHE.get(fp)
    if cached is not None:
        return cached

    plex = _build_plex(conn, secret_key)
    if plex is None:
        return None

    with _PLEX_CLIENT_CACHE_LOCK:
        # Drop other entries: at most one Plex configuration is in
        # use at a time and we don't want to leak old clients.
        _PLEX_CLIENT_CACHE.clear()
        _PLEX_CLIENT_CACHE[fp] = plex
    return plex


def _build_plex_client(conn: sqlite3.Connection, secret_key: str) -> PlexClientBundle | None:
    """Build a PlexClient and resolve library metadata from DB settings.

    Returns a ``(plex, lib_ids, lib_types, lib_titles)`` tuple, or ``None``
    if the required ``plex_url`` / ``plex_token`` settings are absent
    **or** if the configured Plex URL fails the SSRF guard at use-time.

    The caller is responsible for any filtering or further configuration
    (disk thresholds, *arr clients, etc.) before constructing a ScanEngine.

    PlexClient construction is delegated to
    mediaman.services.arr.build.build_plex_from_db to avoid
    duplicating the URL/token lookup and decrypt logic. The
    ``PlexClient`` constructor itself revalidates the configured URL,
    so a stored URL that has since started resolving to an internal
    or metadata address is refused here rather than at the first
    network call. The constructed client is cached at module scope
    keyed on the settings fingerprint so subsequent calls with the
    same configuration reuse it.
    """
    try:
        plex = _get_or_build_plex(conn, secret_key)
    except SSRFRefused:
        # PlexClient constructor refused the URL (SSRF guard). Log
        # without surfacing the URL itself — it may carry topology
        # information — and skip the scan rather than crash.
        logger.exception(
            "Plex client build refused by SSRF guard — verify plex_url in settings. Scan skipped."
        )
        return None
    if plex is None:
        return None

    lib_ids = _load_library_ids(conn)
    plex_libs = plex.get_libraries()
    lib_types: dict[str, str] = {lib["id"]: lib["type"] for lib in plex_libs}
    lib_titles: dict[str, str] = {lib["id"]: lib["title"].lower() for lib in plex_libs}

    return PlexClientBundle(plex, lib_ids, lib_types, lib_titles)


def run_scan_from_db(
    conn: sqlite3.Connection, secret_key: str, *, skip_disk_check: bool = False
) -> ScanSummary:
    """Build a ScanEngine from DB settings and execute a full scan.

    Reads all required configuration from the ``settings`` table — Plex URL/
    token, library IDs, threshold values, and optional Sonarr/Radarr URLs —
    then constructs the relevant clients and runs the scan.

    Returns the summary dict from ScanEngine.run_scan, or an empty
    dict carrying only ``skipped_reason`` if the minimum required settings
    (plex_url / plex_token) are absent or the Plex client cannot be built.

    Args:
        conn: Open SQLite connection with row_factory set to sqlite3.Row.
        secret_key: Application secret used for decrypting stored tokens and
            signing HMAC keep tokens.
        skip_disk_check: When True, bypass the per-library disk-usage threshold
            check and scan all configured libraries unconditionally.
    """
    from mediaman.scanner.engine import ScanEngine

    plex_url = repository.read_setting(conn, "plex_url")
    plex_token = repository.read_setting(conn, "plex_token")
    result = _build_plex_client(conn, secret_key)
    if result is None:
        # A configured but refused or unbuildable client also lands here.
        if not plex_url or not plex_token:
            logger.warning("Scan skipped — plex_url or plex_token not configured")
            return {"skipped_reason": SCAN_SKIPPED_NO_PLEX}
        logger.warning("Scan skipped — Plex client could not be built")
        return {"skipped_reason": SCAN_SKIPPED_PLEX_UNREACHABLE}
    plex, lib_ids, lib_types, lib_titles = result

    if not skip_disk_check:
        lib_ids = _filter_libraries_by_disk(conn, lib_ids, lib_titles)

    sonarr_client = _build_sonarr(conn, secret_key)
    radarr_client = _build_radarr(conn, secret_key)

    min_age = get_day_setting(conn, "min_age_days", default=DAY_SETTING_DEFAULTS["min_age_days"])
    inactivity = get_day_setting(
        conn, "inactivity_days", default=DAY_SETTING_DEFAULTS["inactivity_days"]
    )
    grace = get_day_setting(conn, "grace_days", default=DAY_SETTING_DEFAULTS["grace_days"])
    dry_run = repository.read_setting(conn, "dry_run") == "true"

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
    return cast(ScanSummary, engine.run_scan())


def _unmonitor_deleted(conn: sqlite3.Connection, secret_key: str) -> None:
    """Tell Radarr/Sonarr of the deletions a run that died did not get to tell them of."""
    if not repository.fetch_pending_unmonitors(conn):
        return
    from mediaman.scanner.deletions import unmonitor_deleted

    try:
        unmonitor_deleted(
            conn,
            radarr_client=_build_radarr(conn, secret_key),
            sonarr_client=_build_sonarr(conn, secret_key),
        )
    except (sqlite3.Error, requests.RequestException, SafeHTTPError, ArrError, ValueError):
        logger.exception("Unmonitoring deleted titles failed — sync unaffected")


def run_library_sync(conn: sqlite3.Connection, secret_key: str) -> ScanSummary:
    """Sync library from Plex without running deletion evaluation.

    A lightweight operation that updates media_items from Plex. No
    eligibility checks, no deletions, no newsletter. Designed to run
    frequently (every N minutes) to keep the Library page current.
    """
    from mediaman.scanner.engine import ScanEngine

    _unmonitor_deleted(conn, secret_key)
    bundle = _build_plex_client(conn, secret_key)
    if bundle is None:
        logger.debug("Library sync skipped — Plex not configured")
        return {}
    plex, lib_ids, lib_types, lib_titles = bundle

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
        from mediaman.services.downloads.notifications import check_download_notifications

        check_download_notifications(conn, secret_key)
    except (sqlite3.Error, requests.RequestException, SafeHTTPError, ArrError):
        logger.exception("Download notification check failed — sync results unaffected")

    return cast(ScanSummary, result)
