"""TypedDicts for Sonarr/Radarr v3 API response shapes.

Sonarr and Radarr both expose a JSON API at ``/api/v3/...`` that returns
deeply-nested dicts.  Rather than threading ``dict[str, Any]`` through every
caller, this module pins the field names and types of the shapes mediaman
actually reads, per :doc:`CODE_GUIDELINES` §5.3.

Every TypedDict here is ``total=False`` because the *arr APIs habitually
omit optional fields entirely from the response when they are empty; a
required-field TypedDict would force callers to test ``in`` before every
``.get()`` even on universally-present keys.  The trade-off is intentional:
``.get()`` with a default is the project's idiomatic access pattern for
foreign JSON anyway.

These shapes describe a subset of the full *arr response — only the fields
mediaman reads.  Sonarr/Radarr emit dozens of additional keys (history
hashes, custom format ids, etc.) that mediaman never touches; widening this
file to cover them would be busywork without payoff.
"""

from __future__ import annotations

from typing import TypedDict


class ArrImage(TypedDict, total=False):
    """A single ``images[]`` entry on a Sonarr/Radarr release.

    Both services emit ``coverType`` ("poster", "fanart", "banner") plus
    a URL field, but they disagree on the URL field name: Sonarr uses
    ``remoteUrl`` and Radarr uses ``url``.  ``extract_poster_url`` handles
    the divergence; callers should not rely on either being present.
    """

    coverType: str
    url: str
    remoteUrl: str


class ArrSeasonStatistics(TypedDict, total=False):
    """``statistics`` payload on a Sonarr ``seasons[]`` entry.

    Sonarr exposes per-season aggregates here.  ``previousAiring`` is the
    canonical "has aired" signal on v3; ``previousAiringDate`` is a legacy
    spelling kept on the season dict itself on older Sonarr versions.
    """

    episodeFileCount: int
    episodeCount: int
    previousAiring: str
    previousAiringDate: str


class ArrSeason(TypedDict, total=False):
    """A single ``seasons[]`` entry on a Sonarr series payload.

    ``statistics`` is omitted on freshly-created seasons before Sonarr's
    background aggregation has run; ``previousAiringDate`` is the legacy
    placement of the field that now lives under ``statistics``.
    """

    seasonNumber: int
    monitored: bool
    statistics: ArrSeasonStatistics
    previousAiringDate: str


class ArrSeriesStatistics(TypedDict, total=False):
    """``statistics`` payload on a Sonarr series.

    ``episodeFileCount`` is the only field mediaman reads — it is the
    series-level "any episode present" signal used by
    :func:`mediaman.services.arr.state.series_has_files`.
    """

    episodeFileCount: int
    episodeCount: int


class SonarrSeries(TypedDict, total=False):
    """Sonarr series payload (``/api/v3/series`` and ``/api/v3/series/{id}``).

    Mediaman reads the fields below; Sonarr emits many more (network, runtime,
    images on individual episodes, etc.) that this app ignores.
    """

    id: int
    title: str
    tvdbId: int
    tmdbId: int
    imdbId: str
    year: int
    titleSlug: str
    monitored: bool
    seasons: list[ArrSeason]
    statistics: ArrSeriesStatistics
    images: list[ArrImage]
    added: str


class RadarrMovieFile(TypedDict, total=False):
    """``movieFile`` sub-object on a Radarr movie payload.

    Populated only when ``hasFile`` is true.  Mediaman reads ``path`` and
    ``dateAdded`` to anchor file-age computations against the *arr metadata
    rather than the filesystem mtime (which loses fidelity across
    container restarts).
    """

    id: int
    path: str
    dateAdded: str


class RadarrMovie(TypedDict, total=False):
    """Radarr movie payload (``/api/v3/movie`` and ``/api/v3/movie/{id}``)."""

    id: int
    title: str
    tmdbId: int
    imdbId: str
    year: int
    titleSlug: str
    monitored: bool
    hasFile: bool
    isAvailable: bool
    status: str
    images: list[ArrImage]
    added: str
    inCinemas: str
    digitalRelease: str
    physicalRelease: str
    movieFile: RadarrMovieFile


class ArrQueueEpisode(TypedDict, total=False):
    """``episode`` sub-object on a Sonarr queue record."""

    id: int
    seasonNumber: int
    episodeNumber: int
    title: str


class ArrQueueItem(TypedDict, total=False):
    """A single record from ``/api/v3/queue``.

    Sonarr and Radarr share the same envelope; ``series`` / ``episode``
    appear on Sonarr records and ``movie`` on Radarr records.
    """

    id: int
    title: str
    status: str
    trackedDownloadStatus: str
    trackedDownloadState: str
    size: int
    sizeleft: int
    timeleft: str
    downloadId: str
    movie: RadarrMovie
    series: SonarrSeries
    episode: ArrQueueEpisode


class ArrEpisodeFile(TypedDict, total=False):
    """A single record from ``/api/v3/episodefile``."""

    id: int
    seriesId: int
    seasonNumber: int
    path: str
    dateAdded: str


class ArrEpisode(TypedDict, total=False):
    """A single record from ``/api/v3/episode``."""

    id: int
    seriesId: int
    seasonNumber: int
    episodeNumber: int
    title: str
    monitored: bool
    hasFile: bool


class ArrRootFolder(TypedDict, total=False):
    """A single record from ``/api/v3/rootfolder``."""

    id: int
    path: str
    accessible: bool
    freeSpace: int


class ArrQualityProfile(TypedDict, total=False):
    """A single record from ``/api/v3/qualityprofile``."""

    id: int
    name: str


class ArrLookupResult(TypedDict, total=False):
    """A single record from ``/api/v3/{series,movie}/lookup``.

    Lookup endpoints return the same shape as the corresponding library
    list, plus a few search-only fields.  The body of the response is a
    union of fields from :class:`SonarrSeries` / :class:`RadarrMovie`,
    so callers reading specific fields should pick the right TypedDict.
    """

    id: int
    title: str
    tmdbId: int
    tvdbId: int
    imdbId: str
    year: int
    titleSlug: str
    seasons: list[ArrSeason]


__all__ = [
    "ArrEpisode",
    "ArrEpisodeFile",
    "ArrImage",
    "ArrLookupResult",
    "ArrQualityProfile",
    "ArrQueueEpisode",
    "ArrQueueItem",
    "ArrRootFolder",
    "ArrSeason",
    "ArrSeasonStatistics",
    "ArrSeriesStatistics",
    "RadarrMovie",
    "RadarrMovieFile",
    "SonarrSeries",
]
