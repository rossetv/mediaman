"""Spec-driven unified client for *arr-family APIs (Sonarr + Radarr).

``ArrClient`` is built from a thin chain of mixins so each concern
lives in its own module:

* ``_TransportMixin`` — raw HTTP
  helpers (GET/PUT/POST/DELETE) and the shared
  ``_unmonitor_with_retry`` loop.
* ``_LookupsMixin`` — lookup-by-id
  helpers and the ``is_reachable`` probe.
* ``_AddFlowMixin`` — root folder /
  quality profile pickers cached on the instance.
* ``_SonarrMixin`` —
  ``kind="series"`` operations.
* ``_RadarrMixin`` —
  ``kind="movie"`` operations.

Each kind-specific method asserts the client kind via
``ArrClient._require_series`` / ``ArrClient._require_movie`` so
calling a Sonarr method on a Radarr client (or vice versa) raises
``ArrKindMismatch`` rather than silently issuing the wrong URL shape.

All outbound calls route through ``SafeHTTPClient`` for SSRF
re-validation, size capping, redirect refusal, and retry/backoff on
transient errors (429/502/503/504 on GETs; see ``SafeHTTPClient``).

``last_error`` is ``None`` when the last call succeeded and is set
to the exception string on failure so UI layers can display a banner
instead of silently showing a stale queue.

Callers import ``_ARR_TIMEOUT_SECONDS`` and the exception classes from
this module path.  Each name is re-exported below.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

import requests

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
    SearchUnanswered,
    SeasonsNotListed,
    _TransportMixin,
)
from mediaman.services.arr._types import ArrQueueItem
from mediaman.services.arr.spec import ArrSpec
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)

# The first version of each that honours ``skipRedownload``, by the client's kind.
_SKIPS_REDOWNLOAD_FROM = {"series": (4,), "movie": (4, 7, 2)}

__all__ = [
    "_ARR_TIMEOUT_SECONDS",
    "ArrClient",
    "ArrConfigError",
    "ArrError",
    "ArrKindMismatch",
    "ArrUpstreamError",
    "SearchUnanswered",
    "SeasonsNotListed",
]


class ArrClient(
    _TransportMixin,
    _LookupsMixin,
    _AddFlowMixin,
    _SonarrMixin,
    _RadarrMixin,
):
    """Spec-driven unified client for Sonarr and Radarr v3 APIs.

    Pass a ``ArrSpec`` (typically
    ``SONARR_SPEC`` or
    ``RADARR_SPEC``) as the first
    argument.  The spec determines which service this client speaks to.

    All methods from both Sonarr and Radarr are present on this class.
    Methods that are specific to one service kind raise
    ``ArrKindMismatch`` when called on the wrong variant, e.g.
    calling ``delete_episode_files`` on a Radarr client.
    """

    def __init__(self, spec: ArrSpec, url: str, api_key: str):
        super().__init__(url, api_key)
        #: The spec that controls this client's service-specific behaviour.
        self.spec = spec
        self._searches_when_removed: bool | None = None

    def _require_series(self, method: str) -> None:
        if self.spec.kind != "series":
            raise ArrKindMismatch(
                f"{method} is only available on series (Sonarr) clients; "
                f"this client has kind={self.spec.kind!r}"
            )

    def _require_movie(self, method: str) -> None:
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
        max_pages = 20
        if self.spec.kind == "series":
            extra = "&includeSeries=true&includeEpisode=true"
        else:
            extra = "&includeMovie=true"
        for _ in range(max_pages):  # hard cap to prevent runaway paging
            records, total = self._get_page(
                f"/api/v3/queue?page={page}&pageSize={page_size}{extra}"
            )
            if not records:
                break
            out.extend(cast(list[ArrQueueItem], records))
            if page * page_size >= total:
                break
            page += 1
        else:
            # Exited via the page-count cap, not the totalRecords condition —
            # the queue is being silently truncated. Warn so the orphaned
            # records (every NZB past the cap) are at least observable.
            logger.warning(
                "%s.get_queue: hit the %d-page cap (%d records) — queue may be "
                "truncated; records beyond this point are not being processed",
                self.spec.kind,
                max_pages,
                len(out),
            )
        return out

    def remove_from_queue(self, queue_id: int) -> bool:
        """Remove a queue record and its download from the download client, and blocklist its
        release so it is not grabbed again; no search is started, the caller starts its own.
        A record already gone is no failure. Whether this call removed it."""
        try:
            self._delete(
                f"/api/v3/queue/{queue_id}?removeFromClient=true&blocklist=true&skipRedownload=true"
            )
        except SafeHTTPError as exc:
            if exc.status_code != 404:
                raise
            return False
        return True

    def searches_when_removed(self) -> bool:
        """Whether the service searches again by itself for what ``remove_from_queue`` removes:
        Sonarr before 4 and Radarr before 4.7.2 ignore ``skipRedownload`` and do, with their
        "Redownload failed" on. False when that cannot be read; once read, kept."""
        if self._searches_when_removed is None:
            try:
                status = self._get("/api/v3/system/status", quick=True)
                said = status.get("version") if isinstance(status, dict) else None
                version = tuple(int(n) for n in str(said).split("."))
                older = version < _SKIPS_REDOWNLOAD_FROM[self.spec.kind]
                config = self._get("/api/v3/config/downloadclient", quick=True) if older else {}
            except (SafeHTTPError, requests.RequestException, ArrError, ValueError):
                logger.warning("Could not read whether %s searches again by itself", self.spec.kind)
                return False
            redownloads = isinstance(config, dict) and config.get("autoRedownloadFailed") is True
            self._searches_when_removed = older and redownloads
        return self._searches_when_removed

    def send_command(self, command: Mapping[str, Any]) -> None:
        """Post a command, such as a search, for Radarr or Sonarr to run in the background."""
        self._post("/api/v3/command", command)
