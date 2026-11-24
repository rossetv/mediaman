"""Raw HTTP transport layer and shared retry helper for *arr clients.

Owns the construction of the ``SafeHTTPClient`` plus the four
authenticated verbs (``_get`` / ``_put`` / ``_post`` / ``_delete``)
and the optimistic-concurrency ``_unmonitor_with_retry`` loop
shared by ``unmonitor_season`` and ``unmonitor_movie``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

import requests

from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger(__name__)

#: Split timeout: 5 s to establish a TCP connection, 30 s to read the body.
#: Radarr/Sonarr responses are usually under 1 s on the LAN; the 30 s read
#: budget covers the rare case of a large library dump (tens of thousands of
#: items) on a slow NAS.
_ARR_TIMEOUT_SECONDS: tuple[float, float] = (5.0, 30.0)

#: Maximum response body size for arr GET requests (e.g. ``get_movies()`` /
#: ``get_series()``). The default 8 MiB cap is too small for a large library —
#: a Radarr library of ~30 000 entries at ~300 bytes/item alone exceeds 8 MiB.
#: 64 MiB is sized for a library of tens of thousands of items (the same
#: upper bound the 30 s read-timeout docstring refers to) while still rejecting
#: pathologically large responses that could pin worker memory.
_ARR_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class ArrError(Exception):
    pass


class ArrConfigError(ArrError):
    """Raised when the *arr instance cannot be reached or is misconfigured (no root folder, no quality profile)."""


class ArrKindMismatch(ArrError):
    """Raised when a series-shaped operation is invoked on a Radarr client (or vice versa).

    For example, calling ``delete_episode_files``
    on a client built with ``RADARR_SPEC``
    (``kind="movie"``) raises this exception.
    """


class ArrUpstreamError(ArrError):
    """Raised when Radarr/Sonarr returned a malformed or unexpected response.

    Distinct from ``ArrConfigError`` (which means the upstream is misconfigured)
    and from ``SafeHTTPError`` (which means transport-layer failure). Use this
    when the response was successfully received but doesn't match the API
    contract — null JSON, missing required fields, unexpected schema.
    """


class SearchUnanswered(ArrError):
    """A search command got no answer, and whether it runs could not be read back."""


class SeasonsNotListed(ArrError):
    """Seasons asked for that Sonarr's lookup does not list (yet), so none can be added."""

    def __init__(self, seasons: list[int]) -> None:
        self.seasons = sorted(seasons)
        names = [str(n) for n in self.seasons]
        if len(names) == 1:
            text = f"Season {names[0]} isn't in Sonarr's listing."
        else:
            text = f"Seasons {', '.join(names[:-1])} and {names[-1]} aren't in Sonarr's listing."
        super().__init__(text)


#: How far an *arr's clock may be behind this one.
_CLOCK_DIFFERENCE = timedelta(minutes=1)


def _is_command(entry: Mapping[str, Any], command: Mapping[str, Any], posted_at: datetime) -> bool:
    """Whether *entry* of the *arr's command list is *command*: waiting, running, or done since
    it was posted. The list keeps what was done before too.
    """
    body = entry.get("body") or {}
    if entry.get("name") != command["name"] or any(
        body.get(key) != value for key, value in command.items() if key != "name"
    ):
        return False
    if entry.get("status") != "completed":
        return entry.get("status") in ("queued", "started")
    queued_at = parse_iso_utc(entry.get("queued"))
    return queued_at is not None and queued_at >= posted_at - _CLOCK_DIFFERENCE


class _TransportMixin:
    """Authenticated HTTP verbs + the shared ``_unmonitor_with_retry``.

    Mixed into ``ArrClient``.  Not
    intended for direct instantiation.

    ``last_error`` is ``None`` when the last call succeeded and is
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
            default_max_bytes=_ARR_MAX_RESPONSE_BYTES,
        )
        #: Set to the error string of the last failed call; ``None`` on success.
        self.last_error: str | None = None

    # Any: raw resp.json() of an arbitrary *arr endpoint; callers cast() to the right _types TypedDict.
    def _get(self, path: str) -> dict[Any, Any] | list[Any]:
        """Perform an authenticated GET.  Sets ``last_error`` on failure.

        Raises ``ArrUpstreamError`` if the response body is null (empty
        or explicitly null JSON).
        """
        try:
            resp = self._http.get(path, headers=self._headers)
            self.last_error = None
            result: dict[Any, Any] | list[Any] = resp.json()
            if result is None:
                raise ArrUpstreamError(f"Arr returned null for {path}")
            return result
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — record the failure string so the UI can
            # surface "last_error" without losing the exception type.
            self.last_error = str(exc)
            raise

    # Any: entries of an arbitrary *arr list endpoint; callers cast() to the right _types TypedDict.
    def _get_list(self, path: str) -> list[dict[str, Any]]:
        """GET a list of objects, raising ``ArrUpstreamError`` for anything else.

        Read as "holds nothing", a wrong answer would add, delete or close what is there.
        """
        return self._object_list(self._get(path), path)

    def _get_page(self, path: str) -> tuple[list[dict[str, Any]], int]:
        """GET one page of a paged endpoint as ``(records, totalRecords)``; raises like ``_get_list``."""
        data = self._get(path)
        total = data.get("totalRecords") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(total, int):
            raise ArrUpstreamError(f"Arr returned no page of records for {path}")
        return self._object_list(data.get("records"), path), total

    @staticmethod
    def _object_list(data: object, path: str) -> list[dict[str, Any]]:
        if not isinstance(data, list) or not all(isinstance(entry, dict) for entry in data):
            raise ArrUpstreamError(f"Arr did not return a list of objects for {path}")
        return data

    def _put(self, path: str, data: Mapping[str, Any]) -> None:
        """Perform an authenticated PUT.  Sets ``last_error`` on failure."""
        try:
            self._http.put(path, headers=self._headers, json=data)
            self.last_error = None
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — see _get.
            self.last_error = str(exc)
            raise

    # Any: raw resp.json() of an arbitrary *arr endpoint; callers cast() to the right _types TypedDict.
    def _post(self, path: str, data: Mapping[str, Any]) -> dict[Any, Any] | list[Any]:
        """Perform an authenticated POST.  Sets ``last_error`` on failure.

        Raises ``ArrUpstreamError`` if the response body is null (empty
        or explicitly null JSON) — every add-flow caller reads a field off
        the POST result (e.g. ``new_series.get("id")``), so a ``None`` body
        must fail closed here rather than surface as a bare ``AttributeError``.
        """
        try:
            resp = self._http.post(path, headers=self._headers, json=data)
            self.last_error = None
            result: dict[Any, Any] | list[Any] = resp.json()
            if result is None:
                raise ArrUpstreamError(f"Arr returned null for {path}")
            return result
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — see _get.
            self.last_error = str(exc)
            raise

    def _start_search(self, command: Mapping[str, Any]) -> None:
        """Post a search command; one that got no answer has started if the *arr lists it.

        Raises ``SearchUnanswered`` when that list cannot be read either.
        """
        posted_at = now_utc()
        try:
            self._post("/api/v3/command", command)
        except (SafeHTTPError, requests.RequestException, ValueError, ArrUpstreamError) as exc:
            if isinstance(exc, SafeHTTPError) and 0 < exc.status_code < 500:
                raise
            try:
                listed = self._get_list("/api/v3/command")
            except (SafeHTTPError, requests.RequestException, ValueError, ArrError) as unread:
                raise SearchUnanswered(f"{command['name']} got no answer") from unread
            if not any(_is_command(entry, command, posted_at) for entry in listed):
                raise
            logger.info("%s got no answer but was started", command["name"])

    def _delete(self, path: str) -> None:
        """Perform an authenticated DELETE.  Sets ``last_error`` on failure."""
        try:
            self._http.delete(path, headers=self._headers)
            self.last_error = None
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            # preserve-and-rethrow — see _get.
            self.last_error = str(exc)
            raise

    # rationale: the retry loop carries the ``last_observed`` /
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
        """Read-modify-write to set ``monitored=False``, retrying on transport failures.

        Both ``unmonitor_season`` and
        ``unmonitor_movie`` use this
        helper.

        Each attempt re-reads the entity, applies ``apply_unmonitor``, and
        PUTs the full payload. The retry loop covers *transport* failures
        only: a failed PUT (network/HTTP error) yields to a fresh re-read on
        the next pass. It does NOT detect or recover from write-write races —
        Sonarr/Radarr expose no ETag/version, so a PUT that a sibling later
        clobbers is indistinguishable from success and the loop returns after
        the first successful PUT without re-reading to confirm. Under the
        single-worker model genuine concurrent re-monitors are rare,
        so transport-retry is the only guarantee offered. Raises
        ``ArrError`` when ``max_retries`` consecutive PUTs all fail at the
        transport layer.
        """
        last_observed: bool | None = None
        for attempt in range(max_retries):
            entity = fetch_entity()
            if is_already_unmonitored(entity):
                # Already unmonitored — desired state achieved either on the
                # first attempt (nothing to do) or on a retry after a prior
                # transport-level PUT failure, where the re-read now shows the
                # item unmonitored (our earlier PUT may in fact have landed
                # despite the failed response, or another writer set it).
                if last_observed is True:
                    logger.warning(
                        "%s: %s already unmonitored on re-read after a prior "
                        "transport failure — exiting cleanly",
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
