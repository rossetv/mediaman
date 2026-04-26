"""Library routes package.

This package is a thin aggregator.  The actual route handlers live in:

  * :mod:`.pages`  — GET /library (library page)
  * :mod:`.api`    — GET/POST /api/library, /api/media/* (JSON API)
  * :mod:`._query` — shared query helpers (fetch_library, fetch_stats, etc.)
"""

from __future__ import annotations

from fastapi import APIRouter

from .api import router as _api_router
from .pages import router as _pages_router

router = APIRouter()
router.include_router(_pages_router)
router.include_router(_api_router)

__all__ = ["router"]
