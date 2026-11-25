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
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast

import requests

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso
from mediaman.scanner import repository
from mediaman.scanner.repository.scheduled_actions import has_pending_unmonitors
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
    from mediaman.services.arr.base import ArrClient
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
SCAN_FAILED_NO_LIBRARY = "no library could be read from Plex"
SCAN_FAILED_DISK_LIMITS = "no library was scanned: check the disk limits in Settings"


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
    * ``left_out`` — why each library Plex was not read for was left out, by its id;
      absent from what run_scan_from_db returns.
    * ``left_out_notes`` — the same as a sentence each, set by run_scan_from_db.
    * ``failed_reason`` — set when the scan read none of its libraries.

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
    left_out: dict[str, str]
    left_out_notes: list[str]
    failed_reason: str


def scan_end(summary: ScanSummary) -> tuple[str, str | None]:
    """Return the status a scan run ends with, and what there is to say of it."""
    if reason := summary.get("skipped_reason"):
        return "skipped", reason
    if reason := summary.get("failed_reason"):
        return "error", reason
    return "done", "; ".join(summary.get("left_out_notes", [])) or None


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

# Audit rows about a Plex library rather than an item carry this prefix and its id.
_LIBRARY_AUDIT_ID = "_library:"
# Settings keys holding which libraries the last scan to check the disks left out, and which
# the last scan could not read from Plex: the reason of each by its id.
_LEFT_OUT_KEY = "libraries_left_out"
_UNREAD_KEY = "libraries_unread"
_DISK_LIMIT_UNREADABLE = "its disk limit in Settings cannot be read"


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


class DiskThreshold(NamedTuple):
    path: str
    percent: int


def read_disk_threshold(cfg: object) -> DiskThreshold | None:
    """Return a library's entry of ``disk_thresholds``, or None when it is always scanned.

    Raises ValueError when the entry cannot be read: such a library is not scanned.
    """
    if not cfg:
        return None
    if not isinstance(cfg, dict):
        raise ValueError("not an object")
    path, threshold = cfg.get("path"), cfg.get("threshold")
    if not path or not threshold:
        return None
    try:
        percent = int(threshold)
    except (TypeError, OverflowError) as exc:
        raise ValueError("not a percentage") from exc
    if percent <= 0:
        return None
    # No disk has a path with a null byte.
    if not isinstance(path, str) or "\0" in path:
        raise ValueError("not a path")
    return DiskThreshold(path, percent)


def _filter_libraries_by_disk(
    conn: sqlite3.Connection,
    lib_ids: list[str],
    lib_titles: dict[str, str],
    *,
    ignore_usage: bool = False,
) -> tuple[list[str], list[str]]:
    """Return the libraries to scan, and those left out because their threshold cannot be read.

    Neither holds one whose disk usage is below its threshold, unless *ignore_usage*. A library with
    no threshold, a threshold of 0, or any OSError from ``get_disk_usage`` is
    scanned (fail open). One whose threshold cannot be read is left out:
    scanning it may schedule what the threshold was set to keep.
    """
    raw = repository.read_setting(conn, "disk_thresholds")
    if not raw:
        return lib_ids, []

    try:
        thresholds = json.loads(raw)
    except json.JSONDecodeError:
        thresholds = raw
    if not thresholds:
        return lib_ids, []
    if not isinstance(thresholds, dict):
        logger.warning("disk_thresholds setting is not a JSON object — scanning no libraries")
        return [], list(lib_ids)

    filtered: list[str] = []
    left_out: list[str] = []
    for lib_id in lib_ids:
        title = lib_titles.get(lib_id, lib_id)
        try:
            limit = read_disk_threshold(thresholds.get(lib_id))
            if limit is None or ignore_usage:
                filtered.append(lib_id)
                continue
            usage = shutil.disk_usage(limit.path)
        except OSError:
            logger.warning("Disk check failed for library '%s' — scanning anyway", title)
            filtered.append(lib_id)
            continue
        except ValueError:
            logger.warning(
                "Invalid disk threshold %r for library '%s' — not scanning it",
                thresholds.get(lib_id),
                title,
            )
            left_out.append(lib_id)
            continue

        current_pct = (usage.used / usage.total * 100) if usage.total > 0 else 0
        if current_pct >= limit.percent:
            filtered.append(lib_id)
        else:
            logger.info(
                "Skipped library '%s' — disk at %.1f%%, threshold %d%%",
                title,
                current_pct,
                limit.percent,
            )

    return filtered, left_out


def _left_out_note(lib_id: str, reason: str, lib_titles: Mapping[str, str]) -> str:
    return f"Plex library '{lib_titles.get(lib_id, lib_id)}' was not scanned: {reason}"


def _audit_left_out(
    conn: sqlite3.Connection,
    key: str,
    left_out: Mapping[str, str],
    lib_titles: Mapping[str, str],
) -> None:
    """Record in the history each library left out that was not, for the same reason, the last time.

    *left_out* gives the reason of each; the settings row *key* holds those of the last time.
    """
    before = _read_left_out(conn, key)
    for lib_id, reason in left_out.items():
        if before.get(lib_id) != reason:
            log_audit(
                conn,
                _LIBRARY_AUDIT_ID + lib_id,
                "library_left_out",
                _left_out_note(lib_id, reason, lib_titles),
            )
    if left_out != before:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(left_out), now_iso()),
        )
    conn.commit()


def _read_left_out(conn: sqlite3.Connection, key: str) -> dict[str, object]:
    """Return what _audit_left_out stored, nothing when it cannot be read."""
    try:
        stored = json.loads(repository.read_setting(conn, key) or "{}")
    except ValueError:
        return {}
    return stored if isinstance(stored, dict) else {}


def _say_what_was_left_out(
    conn: sqlite3.Connection,
    summary: ScanSummary,
    lib_ids: Collection[str],
    lib_titles: Mapping[str, str],
    unreadable_limits: Mapping[str, str],
    ticked: Collection[str],
) -> ScanSummary:
    """Audit the libraries a scan of *lib_ids* could not read from Plex, and name them in its summary.

    *unreadable_limits*, whose disk limit could not be read, are named too. The
    scan failed if it read none: of those it was to scan that Plex lists, or of
    any, where Plex lists none of the *ticked* ones. With none to scan that Plex
    lists it failed only for a disk limit that cannot be read: the others are
    below theirs.
    """
    left_out = summary.pop("left_out", {})
    _audit_left_out(conn, _UNREAD_KEY, left_out, lib_titles)
    noted = {**unreadable_limits, **left_out}
    if not noted:
        return summary
    summary["left_out_notes"] = [
        _left_out_note(lib_id, reason, lib_titles) for lib_id, reason in noted.items()
    ]
    listed = [lib_id for lib_id in ticked if lib_id in lib_titles]
    if listed and not any(lib_id in lib_titles for lib_id in lib_ids):
        if unreadable_limits:
            summary["failed_reason"] = SCAN_FAILED_DISK_LIMITS
    elif left_out.keys() >= set(lib_ids):
        summary["failed_reason"] = SCAN_FAILED_NO_LIBRARY
    return summary


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
    lib_titles: dict[str, str] = {lib["id"]: lib["title"] for lib in plex_libs}

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
        skip_disk_check: When True, scan a library however little of its disk is used. One
            whose threshold cannot be read is still left out.
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
    ticked = lib_ids

    # One Plex does not list is left out by the scan, however full its disk is.
    listed = [lib_id for lib_id in lib_ids if lib_id in lib_types]
    unlisted = [lib_id for lib_id in lib_ids if lib_id not in lib_types]
    to_scan, left_out = _filter_libraries_by_disk(
        conn, listed, lib_titles, ignore_usage=skip_disk_check
    )
    unreadable_limits = dict.fromkeys(left_out, _DISK_LIMIT_UNREADABLE)
    _audit_left_out(conn, _LEFT_OUT_KEY, unreadable_limits, lib_titles)
    lib_ids = to_scan + unlisted

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
    return _say_what_was_left_out(
        conn, engine.run_scan(), lib_ids, lib_titles, unreadable_limits, ticked
    )


def _unmonitor_deleted(conn: sqlite3.Connection, secret_key: str) -> None:
    """Tell Radarr/Sonarr of the deletions a run that died did not get to tell them of."""
    if repository.read_setting(conn, "dry_run") == "true":
        return
    if not has_pending_unmonitors(conn):
        return
    from mediaman.scanner.deletions import ArrTurn, rescan_deleted, unmonitor_deleted

    clients: dict[str, ArrClient | None] = {}
    turn = ArrTurn()
    for service, build in (("Radarr", _build_radarr), ("Sonarr", _build_sonarr)):
        try:
            clients[service] = build(conn, secret_key)
        # rationale: one service's unreadable setting must not stop the other's, nor the sync.
        except Exception:
            logger.exception("Building the %s client failed — sync unaffected", service)
            turn.unusable.add(service)
    radarr, sonarr = clients.get("Radarr"), clients.get("Sonarr")
    try:
        unmonitor_deleted(conn, radarr_client=radarr, sonarr_client=sonarr, turn=turn)
        rescan_deleted(conn, radarr_client=radarr, sonarr_client=sonarr, turn=turn)
    # rationale: an unreadable setting or a payload of the wrong shape must not stop the sync.
    except Exception:
        logger.exception("Unmonitoring deleted titles failed — sync unaffected")


def run_library_sync(conn: sqlite3.Connection, secret_key: str) -> ScanSummary:
    """Sync library from Plex without running deletion evaluation.

    A lightweight operation that updates media_items from Plex. No
    eligibility checks, no deletions, no newsletter. Designed to run
    frequently (every N minutes) to keep the library current.
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
