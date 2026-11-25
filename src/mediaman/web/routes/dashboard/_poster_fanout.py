from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.watch_catalog import tmdb_image_url

logger = logging.getLogger(__name__)


# Outer wall-clock budget for the parallel poster fan-out. 3s timeout × 10
# misses serially would be a 30s page render in the worst case. We fan out to a
# small thread pool and bound the whole batch to 6s; anything slower drops to ""
# so the page still renders promptly.
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

    # Short and never retried, so that a TMDB that hangs holds each worker for 3 s and no
    # more, and what is still waiting when the budget ends can be dropped.
    client = TmdbClient.from_db(conn, secret_key, timeout=3.0, retry=False)
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
        if best:
            return title, tmdb_image_url(best.get("poster_path"), "w185") or ""
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
        for fut in futures:
            fut.cancel()
        logger.debug(
            "dashboard.poster_fanout_timeout budget=%.1fs (%d/%d done)",
            _POSTER_FANOUT_BUDGET_SECONDS,
            sum(1 for f in futures if f.done()),
            len(futures),
        )
