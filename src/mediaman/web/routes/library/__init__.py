"""Library routes package.

This package is a thin aggregator.  The actual route handlers live in:

  * :mod:`.pages`       — GET /library (library page)
  * :mod:`.api`         — GET /api/library, POST /api/media/{id}/keep
  * :mod:`._intent`     — POST /api/media/{id}/delete + reconcile helper
  * :mod:`._redownload` — POST /api/media/redownload
  * :mod:`._query`      — shared query helpers (fetch_library, fetch_stats, etc.)
"""

from __future__ import annotations

from fastapi import APIRouter

from ._intent import router as _intent_router
from ._redownload import router as _redownload_router
from .api import router as _api_router
from .pages import router as _pages_router

router = APIRouter()
router.include_router(_pages_router)
router.include_router(_api_router)
router.include_router(_intent_router)
router.include_router(_redownload_router)

__all__ = ["router"]
