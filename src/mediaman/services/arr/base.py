"""Spec-driven unified client for *arr-family APIs (Sonarr + Radarr).

:class:`ArrClient` is built from a thin chain of mixins so each concern
lives in its own module:

* :class:`~mediaman.services.arr._transport._TransportMixin` — raw HTTP
  helpers (GET/PUT/POST/DELETE) and the shared
  :func:`_unmonitor_with_retry` loop.
* :class:`~mediaman.services.arr._lookups._LookupsMixin` — lookup-by-id
  helpers and the ``is_reachable`` probe.
* :class:`~mediaman.services.arr._add._AddFlowMixin` — root folder /
  quality profile pickers cached on the instance.
* :class:`~mediaman.services.arr._sonarr_methods._SonarrMixin` —
  ``kind="series"`` operations.
* :class:`~mediaman.services.arr._radarr_methods._RadarrMixin` —
  ``kind="movie"`` operations.

Each kind-specific method asserts the client kind via
:meth:`ArrClient._require_series` / :meth:`ArrClient._require_movie` so
calling a Sonarr method on a Radarr client (or vice versa) raises
:exc:`ArrKindMismatch` rather than silently issuing the wrong URL shape.

All outbound calls route through :class:`SafeHTTPClient` for SSRF
re-validation, size capping, redirect refusal, and retry/backoff on
transient errors (429/502/503/504 on GETs; see :class:`SafeHTTPClient`).

:attr:`last_error` is ``None`` when the last call succeeded and is set
to the exception string on failure so UI layers can display a banner
instead of silently showing a stale queue.

Back-compat: callers import :data:`_ARR_TIMEOUT_SECONDS` and the
exception classes from this module path.  Each name is re-exported
below.
"""

from __future__ import annotations

import logging
from typing import cast

from mediaman.services.arr._add import _AddFlowMixin
from mediaman.services.arr._lookups import _LookupsMixin
from mediaman.services.arr._radarr_methods import _RadarrMixin
from mediaman.services.arr._sonarr_methods import _SonarrMixin
from mediaman.services.arr._transport import (
    _ARR_TIMEOUT_SECONDS,
    ArrConfigError,
    ArrError,
    ArrKindMismatch,
    ArrUpstreamError,
    _TransportMixin,
)
from mediaman.services.arr._types import ArrQueueItem
from mediaman.services.arr.spec import ArrSpec

logger = logging.getLogger(__name__)


__all__ = [
    "_ARR_TIMEOUT_SECONDS",
    "ArrClient",
    "ArrConfigError",
    "ArrError",
    "ArrKindMismatch",
    "ArrUpstreamError",
]


class ArrClient(
    _TransportMixin,
    _LookupsMixin,
    _AddFlowMixin,
    _SonarrMixin,
    _RadarrMixin,
):
    """Spec-driven unified client for Sonarr and Radarr v3 APIs.

    Pass a :class:`~mediaman.services.arr.spec.ArrSpec` (typically
    :data:`~mediaman.services.arr.spec.SONARR_SPEC` or
    :data:`~mediaman.services.arr.spec.RADARR_SPEC`) as the first
    argument.  The spec determines which service this client speaks to.

    All methods from both Sonarr and Radarr are present on this class.
    Methods that are specific to one service kind raise
    :exc:`ArrKindMismatch` when called on the wrong variant, e.g.
    calling :meth:`delete_episode_files` on a Radarr client.
    """

    def __init__(self, spec: ArrSpec, url: str, api_key: str):
        super().__init__(url, api_key)
        #: The spec that controls this client's service-specific behaviour.
        self.spec = spec

    def _require_series(self, method: str) -> None:
        """Raise :exc:`ArrKindMismatch` if this client is not a Sonarr (series) client."""
        if self.spec.kind != "series":
            raise ArrKindMismatch(
                f"{method} is only available on series (Sonarr) clients; "
                f"this client has kind={self.spec.kind!r}"
            )

    def _require_movie(self, method: str) -> None:
        """Raise :exc:`ArrKindMismatch` if this client is not a Radarr (movie) client."""
        if self.spec.kind != "movie":
            raise ArrKindMismatch(
                f"{method} is only available on movie (Radarr) clients; "
                f"this client has kind={self.spec.kind!r}"
            )

    def get_queue(self) -> list[ArrQueueItem]:
        """Return the current download queue.

        Paginates through all pages — otherwise long queues get silently
        truncated at the default page size, orphaning every NZB whose
        queue record sits past the first page.  The query string differs
        between Sonarr (``includeSeries`` + ``includeEpisode``) and
        Radarr (``includeMovie``).
        """
        out: list[ArrQueueItem] = []
        page = 1
        page_size = 500
        if self.spec.kind == "series":
            extra = "&includeSeries=true&includeEpisode=true"
        else:
            extra = "&includeMovie=true"
        for _ in range(20):  # hard cap to prevent runaway paging
            data = self._get(f"/api/v3/queue?page={page}&pageSize={page_size}{extra}")
            if not isinstance(data, dict):
                break
            records = cast(list[ArrQueueItem], data.get("records") or [])
            if not records:
                break
            out.extend(records)
            total = data.get("totalRecords") or 0
            if page * page_size >= total:
                break
            page += 1
        return out
