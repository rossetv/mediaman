"""Raw HTTP transport layer and shared retry helper for *arr clients.

Owns the construction of the :class:`SafeHTTPClient` plus the four
authenticated verbs (``_get`` / ``_put`` / ``_post`` / ``_delete``)
and the optimistic-concurrency :func:`_unmonitor_with_retry` loop
shared by :meth:`unmonitor_season` and :meth:`unmonitor_movie`.

Split from the original monolithic ``base.py`` so the unified
:class:`~mediaman.services.arr.base.ArrClient` can compose the
transport, lookup, add-flow, and per-service mixins.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import requests

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger(__name__)

#: Split timeout: 5 s to establish a TCP connection, 30 s to read the body.
#: Radarr/Sonarr responses are usually under 1 s on the LAN; the 30 s read
#: budget covers the rare case of a large library dump (tens of thousands of
#: items) on a slow NAS.
_ARR_TIMEOUT_SECONDS: tuple[float, float] = (5.0, 30.0)


class ArrError(Exception):
    """Base for all Sonarr/Radarr-specific failures."""


class ArrConfigError(ArrError):
    """Raised when the *arr instance cannot be reached or is misconfigured (no root folder, no quality profile)."""


class ArrKindMismatch(ArrError):
    """Raised when a series-shaped operation is invoked on a Radarr client (or vice versa).

    For example, calling :meth:`~mediaman.services.arr.base.ArrClient.delete_episode_files`
    on a client built with :data:`~mediaman.services.arr.spec.RADARR_SPEC`
    (``kind="movie"``) raises this exception.
    """


class ArrUpstreamError(ArrError):
    """Raised when Radarr/Sonarr returned a malformed or unexpected response.

    Distinct from ``ArrConfigError`` (which means the upstream is misconfigured)
    and from ``SafeHTTPError`` (which means transport-layer failure). Use this
    when the response was successfully received but doesn't match the API
    contract — null JSON, missing required fields, unexpected schema.
    """


class _TransportMixin:
    """Authenticated HTTP verbs + the shared :func:`_unmonitor_with_retry`.

    Mixed into :class:`~mediaman.services.arr.base.ArrClient`.  Not
    intended for direct instantiation.

    :attr:`last_error` is ``None`` when the last call succeeded and is
    set to the exception string on failure.  UI layers read it to
    surface fetch failures without silently rendering stale data.
    """

    def __init__(self, url: str, api_key: str):
        self._url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._session = requests.Session()
        self._http = SafeHTTPClient(
            self._url,
            session=self._session,
            default_timeout=_ARR_TIMEOUT_SECONDS,
        )
        #: Set to the error string of the last failed call; ``None`` on success.
        self.last_error: str | None = None

    def _get(self, path: str) -> dict | list:
        """Perform an authenticated GET.  Sets :attr:`last_error` on failure.

        Raises :exc:`ArrUpstreamError` if the response body is null (empty
        or explicitly null JSON).
        """
        try:
            resp = self._http.get(path, headers=self._headers)
            self.last_error = None
            result = resp.json()
            if result is None:
                raise ArrUpstreamError(f"Arr returned null for {path}")
            return result
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — record the failure string so the UI can
            # surface "last_error" without losing the exception type.
            self.last_error = str(exc)
            raise

    def _put(self, path: str, data: Mapping[str, Any]) -> None:
        """Perform an authenticated PUT.  Sets :attr:`last_error` on failure."""
        try:
            self._http.put(path, headers=self._headers, json=data)
            self.last_error = None
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — see _get.
            self.last_error = str(exc)
            raise

    def _post(self, path: str, data: Mapping[str, Any]) -> dict | list:
        """Perform an authenticated POST.  Sets :attr:`last_error` on failure."""
        try:
            resp = self._http.post(path, headers=self._headers, json=data)
            self.last_error = None
            return resp.json()
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — see _get.
            self.last_error = str(exc)
            raise

    def _delete(self, path: str) -> None:
        """Perform an authenticated DELETE.  Sets :attr:`last_error` on failure."""
        try:
            self._http.delete(path, headers=self._headers)
            self.last_error = None
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — see _get.
            self.last_error = str(exc)
            raise

    # rationale: 63-line retry loop carries the ``last_observed`` /
    # ``attempt`` state through every branch (already-unmonitored, success,
    # transient failure, final failure). Extracting a per-attempt helper
    # would thread three out-parameters through every call and the
    # exception-vs-return distinction the loop relies on cannot be expressed
    # as a sentinel return without making the orchestrator harder to read.
    def _unmonitor_with_retry(
        self,
        *,
        fetch_entity: Callable[[], Mapping[str, Any]],
        put_url: str,
        is_already_unmonitored: Callable[[Mapping[str, Any]], bool],
        apply_unmonitor: Callable[[Mapping[str, Any]], None],
        log_prefix: str,
        log_id: str,
        max_retries: int = 3,
    ) -> None:
        """Optimistic-concurrency read-modify-write to set ``monitored=False``.

        Both :meth:`~mediaman.services.arr.base.ArrClient.unmonitor_season` and
        :meth:`~mediaman.services.arr.base.ArrClient.unmonitor_movie` use this
        helper.  Raises :exc:`ArrError` when ``max_retries`` rounds all hit a
        concurrent write.
        """
        last_observed: bool | None = None
        for attempt in range(max_retries):
            entity = fetch_entity()
            if is_already_unmonitored(entity):
                # Already unmonitored — desired state achieved either on
                # the first attempt (nothing to do) or on a retry where
                # a concurrent writer beat us to the punch.
                if last_observed is True:
                    logger.warning(
                        "%s: concurrent writer set monitored=False "
                        "on %s while we were retrying — exiting cleanly",
                        log_prefix,
                        log_id,
                    )
                return
            apply_unmonitor(entity)
            logger.debug(
                "%s: issuing full-payload PUT for %s "
                "(attempt %d) — a concurrent write to this record would "
                "be silently overwritten",
                log_prefix,
                log_id,
                attempt + 1,
            )
            try:
                self._put(put_url, entity)
                return
            except (SafeHTTPError, requests.RequestException, ValueError):
                # retry-on-transport-failure — the unmonitor flow is a
                # read-modify-write loop; any transport failure on this attempt
                # yields to a fresh re-read on the next pass.
                if attempt + 1 >= max_retries:
                    raise
                logger.warning(
                    "%s: PUT failed for %s (attempt %d/%d) — re-reading and retrying",
                    log_prefix,
                    log_id,
                    attempt + 1,
                    max_retries,
                )
                last_observed = True
        raise ArrError(
            f"{log_prefix}: gave up after {max_retries} retries for "
            f"{log_id} — concurrent writes kept interleaving"
        )
