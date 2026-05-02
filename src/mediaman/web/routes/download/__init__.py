"""Download routes package — token-authenticated download/re-download confirmations.

This package is a thin aggregator.  The actual route handlers live in:

  * :mod:`.confirm`  — GET /download/{token}  (confirmation page)
  * :mod:`.submit`   — POST /download/{token} (trigger download)
  * :mod:`.status`   — GET /api/download/status (progress polling)
  * :mod:`._tokens`  — in-memory single-use token store (shared state)
"""

from __future__ import annotations

from fastapi import APIRouter

from ._tokens import reset_used_tokens
from .confirm import _DOWNLOAD_LIMITER_GET, _reset_arr_cache_for_tests
from .confirm import router as _confirm_router
from .status import _DOWNLOAD_STATUS_LIMITER, _reset_status_cache_for_tests
from .status import router as _status_router
from .submit import _DOWNLOAD_LIMITER_POST
from .submit import router as _submit_router

router = APIRouter()
router.include_router(_confirm_router)
router.include_router(_submit_router)
router.include_router(_status_router)


def reset_download_limiters() -> None:
    """Clear all download route rate-limiter state. Used by tests."""
    _DOWNLOAD_LIMITER_GET.reset()
    _DOWNLOAD_LIMITER_POST.reset()
    _DOWNLOAD_STATUS_LIMITER.reset()


def reset_download_caches() -> None:
    """Clear the per-service Arr-state and status caches. Used by tests."""
    _reset_arr_cache_for_tests()
    _reset_status_cache_for_tests()


__all__ = [
    "reset_download_caches",
    "reset_download_limiters",
    "reset_used_tokens",
    "router",
]
