"""Plex API client for library scanning and watch history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, TypedDict

import defusedxml.ElementTree as ET
import requests as http_requests
from plexapi.server import PlexServer

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
    title: str          # show title, kept for DB compat
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


def _is_anime(show) -> bool:
    """Detect anime by Plex genre tags.

    Returns True if the show has the ``Anime`` genre, or has ``Animation``
    combined with a Japanese studio. This avoids false positives for
    Western animation (SpongeBob, etc.).
    """
    genres = {g.tag.lower() for g in getattr(show, "genres", [])}
    if "anime" in genres:
        return True
    if "animation" not in genres:
        return False
    # Animation + Japanese studio = anime
    studio = (show.studio or "").lower()
    _JP_STUDIOS = {
        "a-1 pictures", "bones", "cloverworks", "david production",
        "doga kobo", "j.c.staff", "kyoto animation", "lerche",
        "madhouse", "mappa", "o.l.m.", "olm", "orange",
        "p.a. works", "pierrot", "production i.g", "science saru",
        "shaft", "silver link.", "studio deen", "sunrise",
        "tms entertainment", "toei animation", "trigger",
        "ufotable", "white fox", "wit studio", "remow",
        "the answerstudio", "ezóla", "g&g entertainment",
    }
    return studio in _JP_STUDIOS


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
        items = []
        for movie in section.all():
            file_path = ""
            file_size = 0
            if movie.media:
                for part in movie.media[0].parts:
                    file_path = file_path or part.file
                    file_size += part.size or 0
            items.append({
                "plex_rating_key": str(movie.ratingKey),
                "title": movie.title,
                "added_at": movie.addedAt,
                "updated_at": movie.updatedAt,
                "file_path": file_path,
                "file_size_bytes": file_size,
                "poster_path": movie.thumb,
            })
        return items

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
                    if ep_added is not None and (
                        earliest_added is None or ep_added < earliest_added
                    ):
                        earliest_added = ep_added
                added_at = season.addedAt or earliest_added
                results.append({
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
                    "is_anime": _is_anime(show),
                })
        return results

    def get_watch_history(self, rating_key: str) -> list[PlexWatchEntry]:
        """Return watch history for a single item (movie) via the raw Plex API.

        Uses /status/sessions/history/all which is more reliable than
        plexapi's .history() method on individual items.
        """
        base_url = self.server._baseurl
        token = self.server._token
        url = (
            f"{base_url}/status/sessions/history/all"
            f"?metadataItemID={rating_key}"
            f"&sort=viewedAt:desc"
        )
        resp = http_requests.get(
            url, timeout=15,
            headers={"X-Plex-Token": token},
            stream=True,
        )
        try:
            resp.raise_for_status()
            # Reject by Content-Length when the server announces one.
            declared = resp.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > _HISTORY_MAX_BYTES:
                        raise ValueError(
                            f"Plex history response too large: {declared} bytes"
                        )
                except ValueError as exc:
                    raise ValueError(str(exc))
            # Stream in and abort if we exceed the cap — defends against
            # servers that omit Content-Length or lie about it.
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _HISTORY_MAX_BYTES:
                    raise ValueError("Plex history response exceeded size cap")
            body = bytes(buf)
        finally:
            resp.close()
        root = ET.fromstring(body)
        entries = []
        for v in root.findall(".//Video"):
            viewed_at_ts = v.get("viewedAt")
            if viewed_at_ts:
                entries.append({
                    "viewed_at": datetime.fromtimestamp(int(viewed_at_ts), tz=timezone.utc),
                    "account_id": int(v.get("accountID", 0)),
                })
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
                        rated.append({
                            "title": movie.title,
                            "type": "movie",
                            "stars": round(movie.userRating / 2, 1),
                        })
            elif section.type == "show":
                for show in section.all():
                    if show.userRating:
                        rated.append({
                            "title": show.title,
                            "type": "tv",
                            "stars": round(show.userRating / 2, 1),
                        })
        return rated

    def test_connection(self) -> bool:
        """Return True if the Plex server responds and lists at least one library."""
        try:
            self.server.library.sections()
            return True
        except Exception:
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
