"""Recommended-For-You routes package.

A thin aggregator over .pages (GET /recommended), .api (list, download trigger, share-token
mint) and .refresh (background refresh thread, status polling). The shared suggestions
SELECT lives in mediaman.web.repository.recommended.fetch_recommendations. Import router
from this package; tests patch the submodule that owns anything else.
"""

from __future__ import annotations

from fastapi import APIRouter

from .api import router as _api_router
from .pages import router as _pages_router
from .refresh import router as _refresh_router

router = APIRouter()
router.include_router(_pages_router)
router.include_router(_api_router)
router.include_router(_refresh_router)

__all__ = ["router"]
