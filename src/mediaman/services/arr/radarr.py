"""Radarr v3 API client — back-compat shim.

All Radarr logic now lives in :class:`~mediaman.services.arr.base.ArrClient`,
driven by :data:`~mediaman.services.arr.spec.RADARR_SPEC`.  This module
exists solely so that existing imports such as::

    from mediaman.services.arr.radarr import RadarrClient

continue to work without modification.
"""

from __future__ import annotations

from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import RADARR_SPEC


class RadarrClient(ArrClient):
    """Radarr v3 API client.

    A thin subclass of :class:`~mediaman.services.arr.base.ArrClient`
    pre-bound to :data:`~mediaman.services.arr.spec.RADARR_SPEC`.
    Callers need only pass ``url`` and ``api_key``.
    """

    def __init__(self, url: str, api_key: str) -> None:
        super().__init__(RADARR_SPEC, url, api_key)
