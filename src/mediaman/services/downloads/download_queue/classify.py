"""State-derivation helpers for the downloads queue: search-hint copy, countdown bands and
deep links into Radarr/Sonarr. Independent of queue orchestration and item building.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from mediaman.services.downloads.download_format import format_relative_time
from mediaman.services.infra import ConfigDecryptError

if TYPE_CHECKING:
    from mediaman.services.arr.fetcher._base import ArrCard

logger = logging.getLogger(__name__)


def build_search_hint(
    search_count: int,
    last_search_ts: float,
    added_at: float,
    now: float,
    dl_id: str = "",
) -> str:
    """Build the "Searched 12× · next attempt in ~4h" subline.

    Falls back to "Added Xm ago · waiting for first search" before mediaman has fired a
    search (inside the 5-minute staleness window, or after a restart); "" when there is
    nothing to say.

    The countdown comes from the same _search_backoff_seconds helper that gates the real
    search, with deterministic jitter, so the display matches and stays stable across
    polls.
    """
    # Late import: arr.search_trigger imports _format_next_attempt from this
    # module; hoisting would create a circular dependency at module load time.
    from mediaman.services.arr.search_trigger import _search_backoff_seconds

    if search_count > 0 and last_search_ts > 0:
        next_in = (
            last_search_ts + _search_backoff_seconds(search_count, dl_id, last_search_ts) - now
        )
        nxt = _format_next_attempt(next_in)
        if search_count == 1:
            return f"Searched once · {nxt}"
        return f"Searched {search_count}× · {nxt}"
    if added_at > 0:
        rel = format_relative_time(added_at, now)
        if rel:
            return f"Added {rel} · waiting for first search"
    return ""


def _format_next_attempt(next_in_seconds: float) -> str:
    """Render the countdown in one of four bands.

    * ``<= 0`` → "firing now" (race between gate and poll cadence).
    * ``< 60 m`` → "next attempt in 14m" (floor to integer minutes, minimum 1m).
    * ``< 24 h`` → "next attempt in ~4h" (rounded to nearest hour).
    * ``>= 24 h`` → "next attempt in ~24h" (cap edge under jitter).
    """
    if next_in_seconds <= 0:
        return "firing now"
    if next_in_seconds < 3600:
        minutes = max(1, int(next_in_seconds // 60))
        return f"next attempt in {minutes}m"
    if next_in_seconds < 24 * 3600:
        hours = round(next_in_seconds / 3600)
        return f"next attempt in ~{hours}h"
    return "next attempt in ~24h"


def arr_base_urls(conn: sqlite3.Connection, secret_key: str) -> dict[str, str]:
    """Return {"radarr": url, "sonarr": url} for deep links.

    Prefers the public URL (radarr_public_url, sonarr_public_url) because *_url is
    usually an in-cluster hostname the browser cannot reach; falls back to *_url.
    Trailing slashes are stripped and unconfigured services map to "". *secret_key*
    decrypts stored URLs.
    """
    from mediaman.services.infra import get_string_setting as _get_string_setting

    try:
        out = {}
        for service in ("radarr", "sonarr"):
            public = (
                _get_string_setting(
                    conn,
                    f"{service}_public_url",
                    secret_key=secret_key,
                )
                or ""
            )
            internal = (
                _get_string_setting(
                    conn,
                    f"{service}_url",
                    secret_key=secret_key,
                )
                or ""
            )
            chosen = public.strip() or internal.strip()
            out[service] = chosen.rstrip("/")
        return out
    except (sqlite3.Error, ConfigDecryptError):
        logger.warning("Failed to load arr base URLs for deep links", exc_info=True)
        return {"radarr": "", "sonarr": ""}


def build_arr_link(arr: ArrCard | dict[str, object], base_urls: dict[str, str]) -> str:
    """Build a Radarr/Sonarr deep link for a stalled item; "" when the base URL is unset or
    the item has no title slug, rather than a broken link.
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
