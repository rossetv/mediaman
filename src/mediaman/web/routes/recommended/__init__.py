"""Recommended-For-You routes package.

A thin aggregator over .pages (GET /recommended), .api (list, download trigger, share-token
mint), .refresh (the shared picks' background refresh thread, status polling) and .own (each
person's refresh of their own picks). The suggestions SELECT lives in mediaman.web.repository.recommended.fetch_recommendations. Import router
from this package; tests patch the submodule that owns anything else.
"""

from __future__ import annotations

from fastapi import APIRouter

from .api import router as _api_router
from .own import router as _own_router
from .pages import router as _pages_router
from .refresh import router as _refresh_router

router = APIRouter()
router.include_router(_pages_router)
router.include_router(_api_router)
router.include_router(_refresh_router)
router.include_router(_own_router)

__all__ = ["router"]
