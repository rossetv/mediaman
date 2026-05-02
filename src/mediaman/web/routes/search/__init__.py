"""Search routes package — TMDB-backed search, discover and download.

This package is a thin aggregator. The actual route handlers live in:

  * :mod:`.page`        — GET /search, GET /api/search, GET /api/search/discover
  * :mod:`.detail`      — GET /api/search/detail/{media_type}/{tmdb_id}
  * :mod:`.download`    — POST /api/search/download
  * :mod:`._enrichment` — shared TMDB normalisation, Arr-state annotation,
                          and the parallel OMDb ratings fan-out used by
                          ``page.py``.

The names re-exported from this ``__init__`` are the ones existing callers
(and tests) reach for via ``from mediaman.web.routes.search import X``;
keeping them here preserves the import surface across the split.
"""

from __future__ import annotations

from fastapi import APIRouter

# Private helpers exposed for the test-suite (see tests/unit/web/test_search_*).
from ._enrichment import (
    _QUERY_LIMITER,
    _discover_cache,
    _enrich_ratings,
)
from .detail import _fetch_sonarr_series_detail
from .detail import router as _detail_router
from .download import (
    _DOWNLOAD_ADMIN_LIMITER,
    _DOWNLOAD_IP_LIMITER,
    _download_dedup,
)
from .download import router as _download_router
from .page import router as _page_router

router = APIRouter()
router.include_router(_page_router)
router.include_router(_detail_router)
router.include_router(_download_router)


__all__ = [
    "_DOWNLOAD_ADMIN_LIMITER",
    "_DOWNLOAD_IP_LIMITER",
    "_QUERY_LIMITER",
    "_discover_cache",
    "_download_dedup",
    "_enrich_ratings",
    "_fetch_sonarr_series_detail",
    "router",
]
