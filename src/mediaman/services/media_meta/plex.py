"""Plex API client for library scanning and watch history."""

from __future__ import annotations

import re as _re
from datetime import datetime, timezone
from typing import Literal, TypedDict

import defusedxml.ElementTree as ET
import requests as http_requests
from plexapi.exceptions import PlexApiException
from plexapi.server import PlexServer

from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError
from mediaman.services.media_meta.anime_detect import is_anime as _is_anime_show

# Matches X-Plex-Token query parameter values so they can be redacted from
# exception messages and log lines before they propagate.
_PLEX_TOKEN_RE = _re.compile(r"(X-Plex-Token=)[^&\s\"'>]+", _re.IGNORECASE)


def _scrub_plex_token(msg: str) -> str:
    """Replace any ``X-Plex-Token=<value>`` substring in *msg* with ``<redacted>``.

    Applied to exception messages and log lines before they propagate so the
    token never appears in tracebacks, log files, or error responses.
    """
    return _PLEX_TOKEN_RE.sub(r"\1<redacted>", msg)


class PlexLibrarySection(TypedDict):
    """A Plex library section as returned by :meth:`PlexClient.get_libraries`."""

    id: str
    type: str
    title: str


class PlexMovieItem(TypedDict):
    """A movie item as returned by :meth:`PlexClient.get_movie_items`."""

    plex_rating_key: str
    title: str
    added_at: datetime | None
    updated_at: datetime | None
    file_path: str
    file_size_bytes: int
    poster_path: str


class PlexSeasonItem(TypedDict):
    """A TV season item as returned by :meth:`PlexClient.get_show_seasons`."""

    plex_rating_key: str
    title: str  # show title, kept for DB compat
    show_title: str
    season_number: int
    added_at: datetime | None
    updated_at: datetime | None
    file_path: str
    file_size_bytes: int
    poster_path: str
    episode_count: int
    show_rating_key: str
    is_anime: bool


class PlexWatchEntry(TypedDict, total=False):
    """One view event from :meth:`PlexClient.get_watch_history` / :meth:`get_season_watch_history`."""

    viewed_at: datetime
    account_id: int
    episode_title: str  # only present for season watch history entries


class PlexRatedItem(TypedDict):
    """A user-rated item from :meth:`PlexClient.get_user_ratings`."""

    title: str
    type: Literal["movie", "tv"]
    stars: float


class PlexAccount(TypedDict):
    """A named Plex home/managed account from :meth:`PlexClient.get_accounts`."""

    id: int
    name: str


# Hard cap on a single /status/sessions/history response. Plex history
# XML is small; 4 MiB is orders of magnitude above normal and still
# cheap to hold in memory if a malicious or buggy server starts
# streaming indefinitely.
_HISTORY_MAX_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Module-level private helpers — row-to-item conversion
# ---------------------------------------------------------------------------


def _movie_to_item(movie) -> "PlexMovieItem":
    """Convert a plexapi Movie object to a :class:`PlexMovieItem` dict."""
    file_path = ""
    file_size = 0
    if movie.media:
        for part in movie.media[0].parts:
            file_path = file_path or part.file
            file_size += part.size or 0
    return {
        "plex_rating_key": str(movie.ratingKey),
        "title": movie.title,
        "added_at": movie.addedAt,
        "updated_at": movie.updatedAt,
        "file_path": file_path,
        "file_size_bytes": file_size,
        "poster_path": movie.thumb,
    }


def _season_to_item(show, season) -> "PlexSeasonItem":
    """Convert a plexapi Season object (and its parent Show) to a :class:`PlexSeasonItem` dict."""
    episodes = season.episodes()
    file_size = 0
    file_path = ""
    earliest_added = None
    for ep in episodes:
        if ep.media:
            for part in ep.media[0].parts:
                file_size += part.size or 0
                if not file_path and part.file:
                    file_path = str(part.file).rsplit("/", 1)[0]
        ep_added = ep.addedAt
        if ep_added is not None and (earliest_added is None or ep_added < earliest_added):
            earliest_added = ep_added
    added_at = season.addedAt or earliest_added
    return {
        "plex_rating_key": str(season.ratingKey),
        "title": show.title,
        "show_title": show.title,
        "season_number": season.index,
        "added_at": added_at,
        "updated_at": show.updatedAt,
        "file_path": file_path,
        "file_size_bytes": file_size,
        "poster_path": show.thumb,
        "episode_count": len(episodes),
        "show_rating_key": str(show.ratingKey),
        "is_anime": _is_anime_show(show),
    }


class PlexClient:
    """Wraps plexapi to provide the specific queries mediaman needs.

    Handles library enumeration, item scanning (movies and TV seasons),
    watch history retrieval, and account listing.
    """

    def __init__(self, url: str, token: str) -> None:
        """Create a PlexServer connection.

        Args:
            url: Base URL of the Plex Media Server, e.g. ``http://plex:32400``.
            token: Plex authentication token (X-Plex-Token).
        """
        self.server = PlexServer(url, token)
        # Raw HTTP calls that bypass plexapi (e.g. /status/sessions/history)
        # route through SafeHTTPClient so SSRF/size/redirect rules apply
        # even though the host was validated once by plexapi.
        self._http = SafeHTTPClient(
            default_max_bytes=_HISTORY_MAX_BYTES,
        )

    def get_libraries(self) -> list[PlexLibrarySection]:
        """Return all library sections as minimal dicts.

        Returns:
            List of ``{"id", "type", "title"}`` dicts where ``id`` is the
            section key as a string.
        """
        return [
            {"id": str(s.key), "type": s.type, "title": s.title}
            for s in self.server.library.sections()
        ]

    def get_movie_items(self, library_id: str) -> list[PlexMovieItem]:
        """Return all movies in a library section.

        Each dict contains:
            - ``plex_rating_key`` (str)
            - ``title`` (str)
            - ``added_at`` (datetime or None)
            - ``file_path`` (str) — first file part's path
            - ``file_size_bytes`` (int) — sum of all parts
            - ``poster_path`` (str) — Plex thumb URL fragment

        Args:
            library_id: The section key (as returned by :meth:`get_libraries`).
        """
        section = self.server.library.sectionByID(int(library_id))
        return [_movie_to_item(movie) for movie in section.all()]

    def get_show_seasons(self, library_id: str) -> list[PlexSeasonItem]:
        """Return all non-special seasons across every show in a TV library.

        Season 0 / Specials are skipped. The season directory is derived from
        the first episode's file path (parent directory). If the season's own
        ``addedAt`` is absent, the earliest episode ``addedAt`` is used instead.

        Each dict contains:
            - ``plex_rating_key`` (str) — season's rating key
            - ``title`` (str) — show title (kept as ``title`` for DB compat)
            - ``show_title`` (str)
            - ``season_number`` (int)
            - ``added_at`` (datetime or None)
            - ``file_path`` (str) — parent directory of first episode file
            - ``file_size_bytes`` (int) — sum across all episode parts
            - ``poster_path`` (str) — show thumb URL fragment
            - ``episode_count`` (int)
            - ``show_rating_key`` (str)

        Args:
            library_id: The section key (as returned by :meth:`get_libraries`).
        """
        section = self.server.library.sectionByID(int(library_id))
        results = []
        for show in section.all():
            for season in show.seasons():
                if season.index == 0:
                    continue
                results.append(_season_to_item(show, season))
        return results

    def get_watch_history(self, rating_key: str) -> list[PlexWatchEntry]:
        """Return watch history for a single item (movie) via the raw Plex API.

        Uses /status/sessions/history/all which is more reliable than
        plexapi's .history() method on individual items.
        """
        base_url = self.server._baseurl
        token = self.server._token
        # Use params= so metadataItemID is properly URL-encoded and cannot
        # be manipulated via a crafted rating_key containing query separators.
        url = f"{base_url}/status/sessions/history/all"
        try:
            resp = self._http.get(
                url,
                params={"metadataItemID": rating_key, "sort": "viewedAt:desc"},
                headers={"X-Plex-Token": token},
                max_bytes=_HISTORY_MAX_BYTES,
            )
        except SafeHTTPError as exc:
            # Preserve the original ValueError shape for callers that
            # treated oversize responses as a ValueError.
            if (
                exc.status_code == 0
                or "cap" in exc.body_snippet.lower()
                or "too large" in exc.body_snippet.lower()
            ):
                raise ValueError(_scrub_plex_token(exc.body_snippet)) from exc
            raise http_requests.HTTPError(
                _scrub_plex_token(f"Plex history returned {exc.status_code}")
            ) from exc
        body = resp.content
        resp.close()
        root = ET.fromstring(body)
        entries = []
        for v in root.findall(".//Video"):
            viewed_at_ts = v.get("viewedAt")
            if viewed_at_ts:
                entries.append(
                    {
                        "viewed_at": datetime.fromtimestamp(int(viewed_at_ts), tz=timezone.utc),
                        "account_id": int(v.get("accountID", 0)),
                    }
                )
        return entries

    def get_season_watch_history(self, season_rating_key: str) -> list[PlexWatchEntry]:
        """Aggregate watch history across all episodes in a season.

        Fetches each episode's rating key, then queries the raw Plex history
        API for each. This is more reliable than plexapi's .history() method.
        """
        season = self.server.fetchItem(int(season_rating_key))
        entries = []
        for ep in season.episodes():
            ep_history = self.get_watch_history(str(ep.ratingKey))
            for h in ep_history:
                h["episode_title"] = ep.title
                entries.append(h)
        return entries

    def get_user_ratings(self) -> list[PlexRatedItem]:
        """Return all user-rated items across movie and TV libraries.

        Iterates every movie and show across all libraries and collects items
        where ``userRating`` is set. Ratings are on a 0–10 scale in Plex;
        this converts them to a 1–5 star scale (rounded to nearest half).

        Returns:
            List of ``{"title": str, "type": "movie"|"tv", "stars": float}`` dicts.
        """
        rated = []
        for section in self.server.library.sections():
            if section.type == "movie":
                for movie in section.all():
                    if movie.userRating:
                        rated.append(
                            {
                                "title": movie.title,
                                "type": "movie",
                                "stars": round(movie.userRating / 2, 1),
                            }
                        )
            elif section.type == "show":
                for show in section.all():
                    if show.userRating:
                        rated.append(
                            {
                                "title": show.title,
                                "type": "tv",
                                "stars": round(show.userRating / 2, 1),
                            }
                        )
        return rated

    def test_connection(self) -> bool:
        """Return True if the Plex server responds and lists at least one library.

        Catches :exc:`PlexApiException` (API-level errors from plexapi) and
        :exc:`~requests.RequestException` (network/transport errors) only — not
        the broad ``Exception`` which would swallow ``SystemExit``,
        ``KeyboardInterrupt``, and programming errors.
        """
        try:
            self.server.library.sections()
            return True
        except (PlexApiException, http_requests.RequestException):
            return False

    def get_accounts(self) -> list[PlexAccount]:
        """Return named Plex accounts from the /accounts XML endpoint.

        The home admin account (id=1, name="") is excluded — only named
        managed/home accounts are returned.

        Returns:
            List of ``{"id": int, "name": str}`` dicts.
        """
        response = self.server.query("/accounts")
        accounts = []
        for account in response.findall(".//Account"):
            name = account.get("name", "")
            if name:
                accounts.append({"id": int(account.get("id", 0)), "name": name})
        return accounts
