"""Search routes package: TMDB-backed search, discover and download.

A thin aggregator over .page, .detail, .download and ._enrichment (shared TMDB
normalisation, Arr-state annotation and the parallel OMDb ratings fan-out). The names
re-exported here are the import surface callers and tests use.
"""

from __future__ import annotations

from fastapi import APIRouter

# Private helpers exposed for the test-suite (see tests/unit/web/test_search_*).
from ._enrichment import (
    _QUERY_LIMITER,
    _stamp_omdb_ratings,
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
    "_download_dedup",
    "_fetch_sonarr_series_detail",
    "_stamp_omdb_ratings",
    "router",
]
