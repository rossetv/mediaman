"""Deep-link and search-hint helpers for the downloads queue."""

from __future__ import annotations

import logging
import sqlite3

from mediaman.services.download_format import fmt_relative_time

logger = logging.getLogger("mediaman")


def build_search_hint(
    search_count: int,
    last_search_ts: float,
    added_at: float,
    now: float,
) -> str:
    """Build the "Last searched 12m ago" subline shown under the pill.

    Falls back to "Added Xm ago" when mediaman hasn't fired a search yet
    — either the item is still inside the 5-min staleness window, or the
    process was restarted so the in-memory trigger log is empty. Returns
    "" only when we genuinely have nothing to say.
    """
    if search_count > 0 and last_search_ts > 0:
        rel = fmt_relative_time(last_search_ts, now)
        if not rel:
            return ""
        if search_count == 1:
            return f"Searched once · last attempt {rel}"
        return f"Searched {search_count}× · last attempt {rel}"
    if added_at > 0:
        rel = fmt_relative_time(added_at, now)
        if rel:
            return f"Added {rel} · waiting for first search"
    return ""


def arr_base_urls(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{"radarr": url, "sonarr": url}`` for deep-link building.

    Prefers the **public** URL (``radarr_public_url``, ``sonarr_public_url``)
    when configured, because the value set in ``*_url`` is usually the
    in-cluster hostname (e.g. ``http://radarr:7878``) used by mediaman to
    reach the container directly — that URL is meaningless to a user's
    browser. Falls back to ``*_url`` when the public variant is empty
    so the default single-URL setup keeps working.

    Values have any trailing slash stripped. Missing settings (or a
    missing/invalid SECRET_KEY in a test fixture) map to ``""`` so
    callers can safely skip link rendering when the service isn't
    configured.
    """
    from mediaman.config import load_config as _load_config
    from mediaman.services.settings_reader import get_string_setting as _get_string_setting

    try:
        secret_key = _load_config().secret_key
        out = {}
        for service in ("radarr", "sonarr"):
            public = _get_string_setting(
                conn, f"{service}_public_url", secret_key=secret_key,
            ) or ""
            internal = _get_string_setting(
                conn, f"{service}_url", secret_key=secret_key,
            ) or ""
            chosen = public.strip() or internal.strip()
            out[service] = chosen.rstrip("/")
        return out
    except Exception:
        logger.warning("Failed to load arr base URLs for deep links", exc_info=True)
        return {"radarr": "", "sonarr": ""}


def build_arr_link(arr: dict, base_urls: dict[str, str]) -> str:
    """Build a deep-link URL into Radarr/Sonarr for a stalled item.

    Returns ``""`` when the base URL isn't configured or the item has no
    title slug — we'd rather render nothing than a broken link.
    """
    slug = arr.get("title_slug") or ""
    if not slug:
        return ""
    kind = arr.get("kind")
    if kind == "movie" and base_urls.get("radarr"):
        return f"{base_urls['radarr'].rstrip('/')}/movie/{slug}"
    if kind == "series" and base_urls.get("sonarr"):
        return f"{base_urls['sonarr'].rstrip('/')}/series/{slug}"
    return ""
