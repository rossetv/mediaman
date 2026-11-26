"""Sonarr-specific methods for ``ArrClient``.

Mixed into the unified client.  Every method here calls
``self._require_series(...)`` first so a Radarr-flavoured client gets
``ArrKindMismatch`` instead of a confusing 404 from the Sonarr URL
shape.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any, cast

import requests

from mediaman.services.arr._transport import (
    _ARR_TIMEOUT_SECONDS,
    ArrConfigError,
    ArrError,
    ArrUpstreamError,
    SearchUnanswered,
    SeasonsNotListed,
)
from mediaman.services.arr._types import ArrEpisode, ArrEpisodeFile, ArrLookupResult, SonarrSeries
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger(__name__)

# A write sends the whole series back as it was read, so two at once would undo each other.
_series_locks: dict[tuple[str, int], threading.Lock] = {}
#: How long a change somebody waits for (see ``within``) waits for its turn, and then may take.
_STEP_SECONDS = 15.0
#: How long putting such a change back may take, whatever the change used up.
_UNDO_SECONDS = 5.0
#: How long any other change waits for its turn; longer than one somebody waits for takes.
_TURN_SECONDS = 30.0
_READ_AGAIN_AFTER = (0.5, 1.0)
_READ_AGAIN_ON = (0, 429, 502, 503, 504)


#: When every call of a Sonarr client made here must have ended by; None for no such time.
_deadline: ContextVar[float | None] = ContextVar("sonarr_deadline", default=None)


@contextmanager
def _due(deadline: float) -> Iterator[None]:
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def _time_left() -> float:
    deadline = _deadline.get()
    return _STEP_SECONDS if deadline is None else deadline - monotonic()


class _TimedHttp:
    """An HTTP client whose calls end by the time that is set; one begun after it gets no answer."""

    def __init__(self, base: Any) -> None:
        self._base = base

    # Any: the base client's own keyword arguments and response, passed through.
    def _call(self, verb: str, path: str, **kwargs: Any) -> Any:
        left = _time_left()
        if left <= 0:
            raise SafeHTTPError(0, "no time left", path)
        connect, read = kwargs.pop("timeout", None) or _ARR_TIMEOUT_SECONDS
        kwargs["timeout"] = (min(connect, left), min(read, left))
        return getattr(self._base, verb)(path, **kwargs, retry=False)

    def get(self, path: str, *, retry: bool = True, **kwargs: Any) -> Any:
        for pause in _READ_AGAIN_AFTER if retry else ():
            try:
                return self._call("get", path, **kwargs)
            except SafeHTTPError as exc:
                if exc.status_code not in _READ_AGAIN_ON or _time_left() <= pause:
                    raise
            sleep(pause)
        return self._call("get", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._call("put", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._call("post", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._call("delete", path, **kwargs)


def _season_in(series: Mapping[str, Any], season_number: int) -> dict[str, Any]:
    """The season of *series*. Raises ArrUpstreamError when it lists none of that number."""
    seasons = series.get("seasons")
    if not isinstance(seasons, list):
        raise ArrUpstreamError(f"Sonarr series {series.get('id')} has no 'seasons' list")
    for season in seasons:
        if isinstance(season, dict) and season.get("seasonNumber") == season_number:
            return season
    raise ArrUpstreamError(f"Sonarr series {series.get('id')} has no season {season_number}")


def _monitored_in(series: Mapping[str, Any]) -> set[object]:
    return {
        s.get("seasonNumber")
        for s in series.get("seasons") or []
        if isinstance(s, dict) and s.get("monitored")
    }


@dataclass
class SeriesChange:
    """What a re-monitor changed, to put back."""

    season: bool = False
    series: bool = False
    #: Whether Sonarr took it.
    landed: bool = False
    #: The season's episodes may be monitored one by one, which putting the season back undoes.
    lone_episodes: bool = False
    #: The seasons monitored once it was made: one monitored beside them was asked for since.
    monitored: frozenset[object] = frozenset()
    #: Whether the series stays monitored though the season was not added; None for not known.
    left_on: bool | None = False


def _refused(exc: Exception) -> bool:
    """Whether Sonarr answered a change with no to it, rather than gave no answer: a gateway's
    502 or 504 says it did not hear back from Sonarr in time."""
    return isinstance(exc, SafeHTTPError) and exc.status_code not in (0, 502, 504)


def _may_hold_lone_episodes(season: Mapping[str, Any]) -> bool:
    """Whether episodes of an unmonitored season may be monitored: Sonarr counts those that are,
    and those with a file, and dates the next of them."""
    stats = season.get("statistics") or {}
    return not isinstance(stats, dict) or bool(stats.get("episodeCount") or stats.get("nextAiring"))


class _SonarrMixin:
    """Sonarr (``kind="series"``) operations.

    Every method asserts the client kind via
    ``_require_series`` so a
    Radarr-flavoured client raises ``ArrKindMismatch`` rather than
    silently issuing the wrong URL shape.
    """

    def delete_episode_files(self, episode_file_ids: Sequence[int]) -> None:
        """Delete the given episode files from disk via Sonarr.

        Takes ids rather than a season so only files the caller has checked
        are deleted, never one imported since.  Calls
        ``DELETE /api/v3/episodefile/bulk`` with ``{"episodeFileIds": [...]}``
        (one request, supported since Sonarr v3) and falls back to serial
        deletes when that endpoint answers 404 (Sonarr too old).  In the
        fallback a 404 for one file means Sonarr already dropped it, so the
        rest are still deleted.

        Raises ``ArrKindMismatch`` when called on a Radarr client.
        """
        self._require_series("delete_episode_files")  # type: ignore[attr-defined]
        ids = list(episode_file_ids)
        if not ids:
            return
        try:
            self._http.delete(
                "/api/v3/episodefile/bulk",
                headers=self._headers,  # type: ignore[attr-defined]
                json={"episodeFileIds": ids},
            )
        except SafeHTTPError as exc:
            if exc.status_code != 404:
                raise
            logger.debug(
                "sonarr.delete_episode_files: bulk endpoint not available "
                "(HTTP 404), falling back to serial deletes"
            )
            for ef_id in ids:
                try:
                    self._delete(f"/api/v3/episodefile/{ef_id}")  # type: ignore[attr-defined]
                except SafeHTTPError as file_exc:
                    if file_exc.status_code != 404:
                        raise

    def delete_series(self, series_id: int) -> None:
        """Delete a series from Sonarr, its files, and add to exclusion list."""
        self._require_series("delete_series")  # type: ignore[attr-defined]
        # ``self.spec.exclusion_param`` carries the per-flavour spelling
        # (``addImportListExclusion`` for Sonarr, ``addImportExclusion``
        # for Radarr).  Reading through the spec keeps the spelling in
        # one place — see ``mediaman.services.arr.spec.ArrSpec``.
        self._delete(  # type: ignore[attr-defined]
            f"/api/v3/series/{series_id}?deleteFiles=true&{self.spec.exclusion_param}=true"  # type: ignore[attr-defined]
        )

    def get_series(self) -> list[SonarrSeries]:
        self._require_series("get_series")  # type: ignore[attr-defined]
        return cast(list[SonarrSeries], self._get_list("/api/v3/series"))  # type: ignore[attr-defined]

    def get_series_by_id(self, series_id: int) -> SonarrSeries:
        """Return a single series by its Sonarr ID.

        Raises ``ArrUpstreamError`` when Sonarr returns a non-dict response.
        """
        self._require_series("get_series_by_id")  # type: ignore[attr-defined]
        data = self._get(f"/api/v3/series/{series_id}")  # type: ignore[attr-defined]
        if not isinstance(data, dict):
            raise ArrUpstreamError(
                f"Sonarr returned unexpected response type for series {series_id}"
            )
        return cast(SonarrSeries, data)

    def get_series_by_tvdb(self, tvdb_id: int) -> SonarrSeries | None:
        """Find a series in the library by its TVDB ID, or ``None`` if not found.

        Uses Sonarr's ``GET /api/v3/series?tvdbId=`` server-side filter,
        which returns a (possibly empty) list of matching library entries.
        """
        self._require_series("get_series_by_tvdb")  # type: ignore[attr-defined]
        data = self._get_list(f"/api/v3/series?tvdbId={tvdb_id}")  # type: ignore[attr-defined]
        if not data:
            logger.debug("get_series_by_tvdb: no match for tvdb_id=%s", tvdb_id)
            return None
        return cast(SonarrSeries, data[0])

    def get_episodes(self, series_id: int) -> list[ArrEpisode]:
        self._require_series("get_episodes")  # type: ignore[attr-defined]
        data = self._get_list(f"/api/v3/episode?seriesId={series_id}")  # type: ignore[attr-defined]
        return cast(list[ArrEpisode], data)

    def get_episode_files(self, series_id: int) -> list[ArrEpisodeFile]:
        self._require_series("get_episode_files")  # type: ignore[attr-defined]
        data = self._get_list(f"/api/v3/episodefile?seriesId={series_id}")  # type: ignore[attr-defined]
        return cast(list[ArrEpisodeFile], data)

    def unmonitor_season(
        self,
        series_id: int,
        season_number: int,
        *,
        max_retries: int = 3,
        lone_episodes: bool = True,
    ) -> None:
        """Set ``monitored=False`` for *season_number* of *series_id*.

        Sonarr unmonitors a season's episodes with it, so in a season found unmonitored
        already, episodes monitored one by one are unmonitored here unless *lone_episodes*
        is False.

        Sonarr v3 has no PATCH endpoint, so this is a read-modify-write
        on the full series payload.  The retry loop tolerates concurrent
        writers — see ``_unmonitor_with_retry`` for the details.
        """
        self._require_series("unmonitor_season")  # type: ignore[attr-defined]
        was_monitored = False

        def is_already_unmonitored(entity: SonarrSeries) -> bool:
            return not _season_in(entity, season_number).get("monitored", False)

        def apply_unmonitor(entity: SonarrSeries) -> None:
            nonlocal was_monitored
            was_monitored = True
            _season_in(entity, season_number)["monitored"] = False

        with self._series_step(series_id):

            def fetch_entity() -> SonarrSeries:
                series = self.get_series_by_id(series_id)
                _season_in(series, season_number)
                return series

            self._unmonitor_with_retry(  # type: ignore[attr-defined]
                fetch_entity=fetch_entity,
                put_url=f"/api/v3/series/{series_id}",
                is_already_unmonitored=is_already_unmonitored,
                apply_unmonitor=apply_unmonitor,
                log_prefix="sonarr.unmonitor_season",
                log_id=f"series_id={series_id} season={season_number}",
                max_retries=max_retries,
            )
        if was_monitored or not lone_episodes:
            return
        ids = [
            episode["id"]
            for episode in self.get_episodes(series_id)
            if episode.get("seasonNumber") == season_number and episode.get("monitored")
        ]
        if ids:
            self._put(  # type: ignore[attr-defined]
                "/api/v3/episode/monitor", {"episodeIds": ids, "monitored": False}
            )

    def remonitor_season(
        self,
        series_id: int,
        season_number: int,
        *,
        max_retries: int = 3,
        guard: Callable[[SonarrSeries], None] | None = None,
        change: SeriesChange | None = None,
    ) -> None:
        """Set ``monitored=True`` for *season_number* and its series, and trigger a search.

        Sonarr fetches nothing for an unmonitored series, whatever its seasons say. What is
        monitored here is put back when the search cannot be started, the series only if no
        other season was asked for since. It is left as it is when the search may be running all
        the same, or episodes of the season may have been monitored one by one, which putting it
        back would undo (``SearchUnanswered`` for both). *guard* is called with the series
        before it is first changed, and raises to stop that; *change* is told what was.

        Uses the same retry helper as ``unmonitor_season`` so a transient
        PUT failure doesn't silently strand the season in an unmonitored state.

        Raises ``ArrUpstreamError`` when the target season number is absent
        from the series payload — this prevents a silent no-op where the PUT
        succeeds but the season was never remonitored, which would be followed
        by a SeasonSearch for a season Sonarr never re-monitored.
        """
        self._require_series("remonitor_season")  # type: ignore[attr-defined]
        made = change if change is not None else SeriesChange()

        def is_already_remonitored(entity: SonarrSeries) -> bool:
            monitored = bool(_season_in(entity, season_number).get("monitored", False))
            return monitored and entity.get("monitored") is not False

        def apply_remonitor(entity: SonarrSeries) -> None:
            season = _season_in(entity, season_number)
            if not made.season and not season.get("monitored"):
                made.season = True
                made.lone_episodes = _may_hold_lone_episodes(season)
            made.series = made.series or entity.get("monitored") is False
            season["monitored"] = True
            if made.series:
                entity["monitored"] = True
            made.monitored = frozenset(_monitored_in(entity))

        def fetch_entity() -> SonarrSeries:
            series = self.get_series_by_id(series_id)
            _season_in(series, season_number)
            # Read again after a try that may have landed, it would be judged by its own change.
            if guard is not None and not (made.season or made.series):
                guard(series)
            return series

        with self._series_step(series_id):
            try:
                self._unmonitor_with_retry(  # type: ignore[attr-defined]
                    fetch_entity=fetch_entity,
                    put_url=f"/api/v3/series/{series_id}",
                    is_already_unmonitored=is_already_remonitored,
                    apply_unmonitor=apply_remonitor,
                    log_prefix="sonarr.remonitor_season",
                    log_id=f"series_id={series_id} season={season_number}",
                    max_retries=max_retries,
                )
                made.landed = True
                self._start_search(  # type: ignore[attr-defined]
                    {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number}
                )
            except SearchUnanswered:
                raise
            except (SafeHTTPError, requests.RequestException, ValueError, ArrError) as exc:
                lone_episodes = made.season and made.lone_episodes
                if lone_episodes and not made.landed:
                    made.landed = self._was_taken(series_id, is_already_remonitored)
                if made.landed and lone_episodes:
                    raise SearchUnanswered("the season stays monitored, with no search") from exc
                self._put_back(series_id, season_number, made, season_too=not made.lone_episodes)
                raise

    def take_back_season(self, series_id: int, season_number: int, change: SeriesChange) -> None:
        """Put back what a re-monitor changed, for a season deleted meanwhile, in a short time of
        its own."""
        with _due(monotonic() + _UNDO_SECONDS):
            try:
                with self._series_step(series_id):
                    self._put_back(series_id, season_number, change, season_too=True)
            except SafeHTTPError:
                change.left_on = change.series
                logger.warning("No turn to put sonarr series_id=%s back", series_id)

    @property
    def _http(self) -> SafeHTTPClient:
        base: SafeHTTPClient = self._untimed_http
        return base if _deadline.get() is None else cast(SafeHTTPClient, _TimedHttp(base))

    @_http.setter
    def _http(self, http: SafeHTTPClient) -> None:
        self._untimed_http = http

    @contextmanager
    def within(self, seconds: float) -> Iterator[None]:
        """*seconds* for all that is asked here together, for somebody who waits for it: what is
        begun later gets no answer, and a change of a series keeps to a short time of its own."""
        deadline = monotonic() + seconds
        outer = _deadline.get()
        with _due(deadline if outer is None else min(outer, deadline)):
            yield

    @contextmanager
    def _series_step(self, series_id: int) -> Iterator[None]:
        """The turn to change a series. With somebody waiting (``within``) there is a short time
        for it, and none in the end is no answer. Any other change takes what time it needs, and
        goes on without its turn rather than fail for another's."""
        hurried = _deadline.get() is not None
        wait = min(_time_left(), _STEP_SECONDS) if hurried else _TURN_SECONDS
        url = self._url  # type: ignore[attr-defined]
        lock = _series_locks.setdefault((url, series_id), threading.Lock())
        taken = lock.acquire(timeout=max(0.0, wait))
        if hurried and not taken:
            raise SafeHTTPError(0, "being changed for too long", f"/api/v3/series/{series_id}")
        try:
            with self.within(_STEP_SECONDS) if hurried else nullcontext():
                yield
        finally:
            if taken:
                lock.release()

    def _undo_time(self) -> AbstractContextManager[None]:
        """The short time of its own that putting a change somebody waits for back takes."""
        return _due(monotonic() + _UNDO_SECONDS) if _deadline.get() is not None else nullcontext()

    def _was_taken(self, series_id: int, is_made: Callable[[SonarrSeries], bool]) -> bool:
        """Whether a change that got no answer is there all the same; False when that cannot be
        read."""
        try:
            with self._undo_time():
                return is_made(self.get_series_by_id(series_id))
        except (SafeHTTPError, requests.RequestException, ValueError, ArrError):
            logger.warning("Could not read sonarr series_id=%s again", series_id, exc_info=True)
            return False

    def _put_back(
        self, series_id: int, season_number: int, change: SeriesChange, *, season_too: bool
    ) -> None:
        """Put back *change*, the season of it only if *season_too*, and tell it whether that left
        the series on. Best effort: a failure here is logged, so the error being raised is not
        hidden."""
        if not change.series and not (season_too and change.season):
            return
        # A change that got no answer may never have turned the series on.
        change.left_on = change.series and (change.landed or None)
        stays_on = False

        def put_back(entity: SonarrSeries) -> None:
            nonlocal stays_on
            change.left_on = change.series and entity.get("monitored") is not False
            stays_on = change.left_on and bool(_monitored_in(entity) - change.monitored)
            if change.series and not stays_on:
                entity["monitored"] = False
            for season in (entity.get("seasons") or []) if season_too and change.season else ():
                if isinstance(season, dict) and season.get("seasonNumber") == season_number:
                    season["monitored"] = False

        def nothing_to_put_back(entity: SonarrSeries) -> bool:
            after = copy.deepcopy(entity)
            put_back(after)
            return after == entity

        sent = False

        def send_back(entity: SonarrSeries) -> None:
            nonlocal sent
            put_back(entity)
            sent = True

        try:
            with self._undo_time():
                self._unmonitor_with_retry(  # type: ignore[attr-defined]
                    fetch_entity=lambda: self.get_series_by_id(series_id),
                    put_url=f"/api/v3/series/{series_id}",
                    is_already_unmonitored=nothing_to_put_back,
                    apply_unmonitor=send_back,
                    log_prefix="sonarr.remonitor_season",
                    log_id=f"series_id={series_id} season={season_number}",
                    max_retries=1,
                )
        except (SafeHTTPError, requests.RequestException, ValueError, ArrError) as exc:
            logger.exception(
                "Could not put sonarr series_id=%s season=%s back", series_id, season_number
            )
            # Sonarr may have taken a put-back it gave no answer to, and turned the series off.
            if sent and change.series and not _refused(exc):
                change.left_on = stays_on or None
            return
        change.left_on = stays_on

    def search_series(self, series_id: int) -> None:
        self._require_series("search_series")  # type: ignore[attr-defined]
        self._post("/api/v3/command", {"name": "SeriesSearch", "seriesId": series_id})  # type: ignore[attr-defined]

    def rescan_series(self, series_id: int) -> None:
        """Ask Sonarr to look at the series' files on disk again; it does so in the background."""
        self._require_series("rescan_series")  # type: ignore[attr-defined]
        self._post(  # type: ignore[attr-defined]
            "/api/v3/command", {"name": "RescanSeries", "seriesId": series_id}
        )

    def get_missing_series(self) -> dict[int, str]:
        """Return ``{series_id: series_title}`` for every series with at least
        one monitored missing episode, matching Sonarr's Wanted/Missing view.
        """
        self._require_series("get_missing_series")  # type: ignore[attr-defined]
        out: dict[int, str] = {}
        page = 1
        page_size = 250
        max_pages = 100
        for _ in range(max_pages):  # hard cap to prevent runaway paging
            records, total = self._get_page(  # type: ignore[attr-defined]
                f"/api/v3/wanted/missing?page={page}&pageSize={page_size}"
                "&includeSeries=true&monitored=true"
            )
            if not records:
                break
            for rec in records:
                series = rec.get("series") or {}
                sid = series.get("id")
                title = series.get("title", "")
                if sid and sid not in out:
                    out[sid] = title
            if page * page_size >= total:
                break
            page += 1
        else:
            # Exited via the page-count cap, not the totalRecords condition —
            # the wanted/missing list is being silently truncated. Warn so the
            # un-poked series past the cap are observable.
            logger.warning(
                "sonarr.get_missing_series: hit the %d-page cap (%d series) — "
                "wanted/missing list may be truncated",
                max_pages,
                len(out),
            )
        return out

    def add_series(
        self, tvdb_id: int, title: str, quality_profile_id: int | None = None
    ) -> SonarrSeries:
        """Add a TV series by TVDB ID and trigger a search.

        ``quality_profile_id`` is selected via
        ``_choose_quality_profile`` when not specified.  Raises
        ``ValueError`` if ``tvdb_id <= 0`` rather than letting Sonarr
        return an opaque 400.
        """
        self._require_series("add_series")  # type: ignore[attr-defined]
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        root_path = self._choose_root_folder()  # type: ignore[attr-defined]
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()  # type: ignore[attr-defined]
        )

        series_data = {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {"searchForMissingEpisodes": True},
        }
        return cast(SonarrSeries, self._post("/api/v3/series", series_data))  # type: ignore[attr-defined]

    def add_series_with_seasons(
        self,
        tvdb_id: int,
        title: str,
        monitored_seasons: list[int],
        quality_profile_id: int | None = None,
    ) -> SonarrSeries:
        """Add a series with only the given seasons monitored.

        Seasons listed in *monitored_seasons* are added with
        ``monitored=True``; every other season (including season 0) is
        added with ``monitored=False``.  ``monitor: skip`` keeps exactly those
        seasons (``none`` would unmonitor them all after the first scan) and
        Sonarr itself searches the monitored missing episodes once it has
        scanned the series, so no search command is posted here: one sent
        straight after the add finds no episodes yet. What Sonarr searches
        follows *monitored_seasons* alone.
        """
        self._require_series("add_series_with_seasons")  # type: ignore[attr-defined]
        if tvdb_id <= 0:
            raise ValueError(f"tvdb_id must be positive, got {tvdb_id!r}")
        body = self._prepare_series_with_seasons_body(
            tvdb_id, title, monitored_seasons, quality_profile_id
        )
        return cast(SonarrSeries, self._post("/api/v3/series", body))  # type: ignore[attr-defined]

    def _prepare_series_with_seasons_body(
        self,
        tvdb_id: int,
        title: str,
        monitored_seasons: list[int],
        quality_profile_id: int | None,
    ) -> dict[str, object]:
        root_path = self._choose_root_folder()  # type: ignore[attr-defined]
        profile_id = (
            quality_profile_id if quality_profile_id is not None else self._choose_quality_profile()  # type: ignore[attr-defined]
        )

        lookup = cast(
            list[ArrLookupResult],
            self._get_list(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}"),  # type: ignore[attr-defined]
        )
        if not lookup:
            raise ArrConfigError(f"Sonarr lookup returned no results for tvdb:{tvdb_id}")
        meta = lookup[0]

        # A season the lookup omits would be dropped from the add without a word.
        listed = {s["seasonNumber"] for s in meta.get("seasons", [])}
        if missing := set(monitored_seasons) - listed:
            raise SeasonsNotListed(list(missing))

        monitored_set = set(monitored_seasons)
        seasons_payload = [
            {
                "seasonNumber": s["seasonNumber"],
                "monitored": s["seasonNumber"] in monitored_set,
            }
            for s in meta.get("seasons", [])
        ]
        return {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            # Sonarr's default monitors every season announced later; only the ones picked are wanted.
            "monitorNewItems": "none",
            "seasonFolder": True,
            "seasons": seasons_payload,
            "addOptions": {
                "searchForMissingEpisodes": True,
                "monitor": "skip",
            },
        }

    def lookup_series_by_tmdb(self, tmdb_id: int) -> ArrLookupResult | None:
        """Look up a series by TMDB ID via Sonarr's lookup endpoint.

        Returns the first match or ``None`` if Sonarr's metadata provider
        does not know the series.  Network failures propagate so callers
        can distinguish "not found" from "call failed".
        """
        self._require_series("lookup_series_by_tmdb")  # type: ignore[attr-defined]
        results = self.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")  # type: ignore[attr-defined]
        return results[0] if results else None
