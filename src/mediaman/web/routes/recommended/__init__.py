"""Recommended-For-You routes package.

Thin aggregator over focused submodules:

* :mod:`.pages`   — GET /recommended page, batch grouping, relative labels.
* :mod:`.api`     — JSON API: list, download trigger, share-token mint.
* :mod:`.refresh` — background refresh thread, status polling.
* :mod:`._query`  — shared DB helper.

Callers should keep importing ``router`` from this package.  Anything else
lives on the submodule that owns it — tests patch the submodule directly.
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
