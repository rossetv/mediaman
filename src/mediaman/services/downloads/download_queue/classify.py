"""State-derivation helpers for the downloads queue.

WHAT: Builds human-readable search-hint copy ("Searched 3× · next attempt in ~4h"),
      renders countdown bands, and constructs deep-link URLs into Radarr/Sonarr.

WHY: These helpers sit between raw timestamp/count data and the UI strings that
     represent item state — they are independent of queue orchestration and item
     building, so isolating them here keeps the other modules focused.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.services.downloads.download_format import format_relative_time
from mediaman.services.infra import ConfigDecryptError

logger = logging.getLogger(__name__)


def build_search_hint(
    search_count: int,
    last_search_ts: float,
    added_at: float,
    now: float,
    dl_id: str = "",
) -> str:
    """Build the "Searched 12× · next attempt in ~4h" subline shown under the pill.

    Falls back to "Added Xm ago · waiting for first search" when mediaman
    hasn't fired a search yet — either the item is still inside the 5-min
    staleness window, or the process was restarted so the in-memory
    trigger log is empty. Returns "" only when we genuinely have nothing
    to say.

    The next-attempt countdown is derived from the same
    :func:`~mediaman.services.arr.search_trigger._search_backoff_seconds` helper
    that gates the actual fire, so what the UI shows matches when the search
    will really run. The deterministic jitter inside that helper means the
    displayed time stays stable across polls within a single waiting window.
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
    """Return ``{"radarr": url, "sonarr": url}`` for deep-link building.

    ``secret_key`` is required to decrypt any stored URLs.

    Prefers the **public** URL (``radarr_public_url``, ``sonarr_public_url``)
    when configured, because the value set in ``*_url`` is usually the
    in-cluster hostname (e.g. ``http://radarr:7878``) used by mediaman to
    reach the container directly — that URL is meaningless to a user's
    browser. Falls back to ``*_url`` when the public variant is empty
    so the default single-URL setup keeps working.

    Values have any trailing slash stripped. Missing settings map to ``""``
    so callers can safely skip link rendering when the service isn't configured.
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
