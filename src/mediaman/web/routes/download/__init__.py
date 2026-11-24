"""Download routes package: token-authenticated download confirmations.

A thin aggregator over .confirm (GET page), .submit (POST), .status (progress polling) and
._tokens (single-use token store).
"""

from __future__ import annotations

from fastapi import APIRouter

from ._tokens import reset_used_tokens
from .confirm import _DOWNLOAD_LIMITER_GET, _reset_arr_cache_for_tests, drop_arr_cache
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
    _DOWNLOAD_LIMITER_GET.reset()
    _DOWNLOAD_LIMITER_POST.reset()
    _DOWNLOAD_STATUS_LIMITER.reset()


def reset_download_caches() -> None:
    _reset_arr_cache_for_tests()
    _reset_status_cache_for_tests()


__all__ = [
    "drop_arr_cache",
    "reset_download_caches",
    "reset_download_limiters",
    "reset_used_tokens",
    "router",
]
