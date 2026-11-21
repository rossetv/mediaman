from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests

from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)


#: TMDB poster image base URL — w200 thumbnail size used for dashboard tiles.
#: Callers elsewhere that use w300 keep their own URL deliberately distinct.
_TMDB_POSTER_BASE_URL = "https://image.tmdb.org/t/p/w200"

# Outer wall-clock budget for the parallel poster fan-out. 5s timeout × 10
# misses serially would be a 50s page render in the worst case. We fan out to a small thread pool and bound the whole
# batch to 6s; anything slower drops to "" so the page still renders promptly.
_POSTER_FANOUT_BUDGET_SECONDS = 6.0
_POSTER_FANOUT_WORKERS = 4


@lru_cache(maxsize=1)
def _get_poster_executor() -> ThreadPoolExecutor:
    """Return the shared poster-fanout executor, created lazily and never shut down so repeated
    renders share one thread pool.
    """
    return ThreadPoolExecutor(
        max_workers=_POSTER_FANOUT_WORKERS,
        thread_name_prefix="dashboard_poster",
    )


def _fill_tmdb_posters(
    conn: sqlite3.Connection,
    items: list[dict[str, object]],
    needed: list[tuple[int, str]],
    secret_key: str,
) -> None:
    """Look up TMDB poster URLs for deleted items missing a Plex poster.

    Runs in a small worker pool under a wall-clock budget so a flaky TMDB cannot stall the
    page, and is deduplicated by title so repeat deletions cost one API call. secret_key is
    passed in to avoid a load_config() per request.
    """
    from mediaman.services.media_meta.tmdb import TmdbClient

    # timeout=5.0 overrides TmdbClient's default (10s read) so each per-title
    # lookup cannot eat more than 5s — keeping the total wall-clock budget
    # (_POSTER_FANOUT_BUDGET_SECONDS = 6s) meaningful across the worker pool.
    # TmdbClient routes through SafeHTTPClient, so SSRF allowlist and retry
    # behaviour are still governed by services/infra/http/client.py.
    client = TmdbClient.from_db(conn, secret_key, timeout=5.0)
    if client is None:
        return

    # Collect each unique title once; preserve the (idx, title)
    # reverse mapping so we can write the result back to the right
    # rows after the futures resolve.
    unique_titles: dict[str, list[int]] = {}
    for idx, title in needed:
        unique_titles.setdefault(title, []).append(idx)

    if not unique_titles:
        return

    def _lookup(title: str) -> tuple[str, str]:
        try:
            best = client.search_multi(title)
        except (SafeHTTPError, requests.RequestException, ValueError):
            logger.debug("dashboard.poster_lookup_failed title=%r", title, exc_info=True)
            return title, ""
        if best and best.get("poster_path"):
            return title, f"{_TMDB_POSTER_BASE_URL}{best['poster_path']}"
        return title, ""

    pool = _get_poster_executor()
    futures = {pool.submit(_lookup, title): title for title in unique_titles}
    try:
        for fut in as_completed(futures, timeout=_POSTER_FANOUT_BUDGET_SECONDS):
            try:
                title, url = fut.result()
            except (SafeHTTPError, requests.RequestException, ValueError, CancelledError):
                continue
            if not url:
                continue
            for idx in unique_titles.get(title, []):
                items[idx]["poster_url"] = url
    except TimeoutError:
        logger.debug(
            "dashboard.poster_fanout_timeout budget=%.1fs (%d/%d done)",
            _POSTER_FANOUT_BUDGET_SECONDS,
            sum(1 for f in futures if f.done()),
            len(futures),
        )
