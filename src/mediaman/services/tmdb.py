"""Unified TMDB client — search, details, and shape helpers.

Collapses the four previously-duplicated TMDB integrations (download
confirmation, OpenAI recommendation enrichment, search detail modal,
deleted-item poster fill) into a single class. All callers read the
bearer token through :func:`services.settings_reader.get_string_setting`
so token decrypt behaviour stays consistent.

The shape helpers are pure functions over the TMDB JSON payload — no
network calls, no settings access — so they're trivially testable and
reusable by callers that have already fetched the raw data.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

import requests

from mediaman.services.http_client import SafeHTTPClient
from mediaman.services.settings_reader import get_string_setting

logger = logging.getLogger("mediaman")

_BASE = "https://api.themoviedb.org/3"
_POSTER_BASE_W300 = "https://image.tmdb.org/t/p/w300"


class TmdbClient:
    """Thin wrapper around ``api.themoviedb.org/3``.

    Uses a Bearer read-token fetched from settings (key
    ``'tmdb_read_token'``, may be encrypted). Constructor fails closed —
    :meth:`from_db` returns ``None`` when no token is configured or
    decryption fails.
    """

    def __init__(self, read_token: str, *, timeout: float = 10.0) -> None:
        self._token = read_token
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {read_token}",
            "Accept": "application/json",
        }
        self._session = requests.Session()
        # Per-call timeout is kept small — TMDB is expected to be snappy.
        self._http = SafeHTTPClient(
            _BASE,
            session=self._session,
            default_timeout=(5.0, float(timeout)),
        )

    @property
    def headers(self) -> dict[str, str]:
        """Return a copy of the auth headers for use with raw requests calls."""
        return dict(self._headers)

    @classmethod
    def from_db(
        cls,
        conn: sqlite3.Connection,
        secret_key: str,
        *,
        timeout: float = 10.0,
    ) -> "TmdbClient | None":
        """Factory that reads the read-token from the settings table.

        Returns ``None`` if the token is missing or cannot be decrypted —
        callers must handle the absence gracefully, not raise.

        ``timeout`` is the default request timeout in seconds — the
        default (10.0) is the upper bound of the previous inline
        timeouts; call sites that historically used 5s can still pass a
        lower value if they need to stay snappy.
        """
        token = get_string_setting(
            conn, "tmdb_read_token", secret_key=secret_key, default=""
        )
        if not token:
            return None
        return cls(token, timeout=timeout)

    # ------------------------------------------------------------------
    # Network calls
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        """Perform an authenticated GET against *path* and return the parsed JSON.

        Centralises the ``self._http.get(..., headers=self._headers, params=...)``
        repetition shared by every public network method.  Propagates all
        exceptions to the caller — each public method has its own ``except``
        clause that decides whether to return ``None`` or ``[]``.
        """
        return self._http.get(path, headers=self._headers, params=params or {}).json()

    def test_connection(self) -> bool:
        """Return True if the TMDB API is reachable and the token is valid."""
        try:
            self._get("/configuration")
            return True
        except Exception:
            return False

    def search(
        self,
        title: str,
        *,
        year: int | None = None,
        media_type: str = "movie",
    ) -> dict | None:
        """Search TMDB for the best matching movie or TV show.

        Returns the first result dict from ``/search/{movie|tv}``, or
        ``None`` on error / no matches. The caller chooses what to do
        with the raw payload (pass it through :meth:`shape_card`, merge
        specific fields, etc.).
        """
        endpoint = "movie" if media_type == "movie" else "tv"
        params: dict[str, Any] = {"query": title}
        if year:
            params["year" if endpoint == "movie" else "first_air_date_year"] = year
        try:
            results = self._get(f"/search/{endpoint}", params).get("results") or []
        except Exception:
            return None
        return results[0] if results else None

    def search_multi(self, title: str) -> dict | None:
        """Search TMDB across movies, TV, and people — returns the raw
        first result.

        Used by callers (e.g. the dashboard deleted-items panel) that
        only know the title and don't care about media_type. The first
        result may be a ``person`` entry with no poster — the caller
        is responsible for checking ``poster_path``.
        """
        try:
            results = self._get("/search/multi", {"query": title}).get("results") or []
        except Exception:
            return None
        return results[0] if results else None

    def search_multi_paged(self, query: str, page: int = 1) -> list[dict]:
        """Return one page of ``/search/multi`` results as a raw list.

        Unlike :meth:`search_multi` (which returns only the first hit),
        this returns the full page so callers can merge multiple pages
        themselves. Returns an empty list on error or when TMDB returns
        no hits — never raises.
        """
        try:
            return self._get(
                "/search/multi",
                {"query": query, "include_adult": False, "page": page},
            ).get("results") or []
        except Exception:
            return []

    def trending(self, page: int = 1) -> list[dict]:
        """Return one page of ``/trending/all/week`` results.

        TMDB recomputes this daily so the caller may cache aggressively.
        Returns an empty list on error — never raises.
        """
        try:
            return self._get("/trending/all/week", {"page": page}).get("results") or []
        except Exception:
            return []

    def popular_movies(self, page: int = 1) -> list[dict]:
        """Return one page of ``/movie/popular`` results.

        Items do not include a ``media_type`` field — callers that need
        it must inject ``"movie"`` themselves.
        Returns an empty list on error — never raises.
        """
        try:
            return self._get("/movie/popular", {"page": page}).get("results") or []
        except Exception:
            return []

    def popular_tv(self, page: int = 1) -> list[dict]:
        """Return one page of ``/tv/popular`` results.

        Items do not include a ``media_type`` field — callers that need
        it must inject ``"tv"`` themselves.
        Returns an empty list on error — never raises.
        """
        try:
            return self._get("/tv/popular", {"page": page}).get("results") or []
        except Exception:
            return []

    def details(self, media_type: str, tmdb_id: int) -> dict | None:
        """Return the raw TMDB details payload with videos + credits appended.

        On HTTP or JSON errors returns ``None`` — callers should skip
        enrichment rather than raise.
        """
        endpoint = "movie" if media_type == "movie" else "tv"
        try:
            return self._get(
                f"/{endpoint}/{tmdb_id}",
                {"append_to_response": "videos,credits"},
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Shaping helpers — pure functions, no side effects
    # ------------------------------------------------------------------

    @staticmethod
    def shape_card(data: dict) -> dict:
        """Return the compact-card shape for a TMDB search / details payload.

        Fields: ``year`` (int | None), ``poster_url`` (str | None, w300),
        ``rating`` (float rounded to 1dp | None), ``description`` (str,
        may be empty), ``tmdb_id`` (int | None).

        Matches the shape produced inline by download.py and the
        recommendations enrichment path — both used w300 posters and
        rounded vote_average to 1dp.
        """
        date = data.get("release_date") or data.get("first_air_date") or ""
        year: int | None = None
        if date[:4].isdigit():
            try:
                year = int(date[:4])
            except ValueError:
                year = None

        poster_path = data.get("poster_path")
        poster_url = f"{_POSTER_BASE_W300}{poster_path}" if poster_path else None

        rating: float | None = None
        vote = data.get("vote_average")
        if vote:
            try:
                rating = round(float(vote), 1)
            except (TypeError, ValueError):
                rating = None

        return {
            "tmdb_id": data.get("id"),
            "year": year,
            "poster_url": poster_url,
            "rating": rating,
            "description": data.get("overview") or "",
        }

    @staticmethod
    def shape_detail(data: dict, *, media_type: str) -> dict:
        """Return the rich-detail shape for a TMDB ``details`` payload.

        Fields: ``tagline``, ``runtime``, ``genres`` (JSON string or
        None), ``cast_json`` (JSON string of top-8 cast with ``name`` +
        ``character`` — None when empty), ``director`` (name string or
        None — director for movies, first creator for TV),
        ``trailer_key`` (YouTube key or None), ``description`` (overview
        string, may be empty).

        The JSON-encoded ``genres`` and ``cast_json`` shapes match the
        ``suggestions`` table columns and the ``items`` dict passed to
        the download template — callers merge them directly.
        """
        out: dict[str, Any] = {
            "tagline": data.get("tagline") or None,
            "description": data.get("overview") or "",
        }

        endpoint = "movie" if media_type == "movie" else "tv"
        if endpoint == "movie":
            out["runtime"] = data.get("runtime")
        else:
            ert = data.get("episode_run_time") or []
            out["runtime"] = ert[0] if ert else None

        genres = [g["name"] for g in data.get("genres") or []]
        out["genres"] = json.dumps(genres) if genres else None

        director: str | None = None
        if endpoint == "movie":
            credits = data.get("credits") or {}
            for crew in credits.get("crew") or []:
                if crew.get("job") == "Director":
                    director = crew.get("name")
                    break
        else:
            creators = data.get("created_by") or []
            if creators:
                director = creators[0].get("name")
        out["director"] = director

        credits = data.get("credits") or {}
        cast = (credits.get("cast") or [])[:8]
        if cast:
            out["cast_json"] = json.dumps(
                [
                    {"name": c.get("name"), "character": c.get("character", "")}
                    for c in cast
                ]
            )
        else:
            out["cast_json"] = None

        trailer_key: str | None = None
        for v in (data.get("videos") or {}).get("results") or []:
            if (
                v.get("site") == "YouTube"
                and v.get("type") == "Trailer"
                and v.get("key")
            ):
                trailer_key = v["key"]
                break
        out["trailer_key"] = trailer_key

        return out
