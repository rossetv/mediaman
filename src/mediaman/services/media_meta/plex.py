"""Plex API client for library scanning and watch history.

Security note — XML hardening
-----------------------------
``plexapi`` parses Plex responses with the standard-library
:mod:`xml.etree.ElementTree`, which historically supports billion-laughs
and external-entity attacks.  Since we cannot patch plexapi's internals,
we install :func:`defusedxml.defuse_stdlib` at module import time.  The
call is idempotent and globally replaces the stdlib parser with the
hardened defusedxml shim, so every plexapi call inherits the protection
even though plexapi itself never imports defusedxml.

Direct ``ET.fromstring`` usage in this module already imports
:mod:`defusedxml.ElementTree` explicitly so it is doubly hardened.
"""

from __future__ import annotations

import logging as _logging
import re as _re
import warnings as _warnings
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

import defusedxml
import defusedxml.ElementTree as ET
import requests as http_requests
from plexapi.exceptions import PlexApiException
from plexapi.server import PlexServer

# Replace the stdlib XML modules with their defusedxml shims so any
# in-process consumer (notably plexapi, which we cannot modify) inherits
# the hardened parser.  ``defuse_stdlib`` is idempotent and safe to call
# from a module import body.
#
# defusedxml itself emits a DeprecationWarning when ``cElementTree`` is
# imported from inside :func:`defuse_stdlib` on Python 3.13+ (the stdlib
# ``cElementTree`` module is gone but defusedxml still ships a shim for
# backwards-compatibility).  The pytest config promotes warnings to
# errors, so we suppress this single deprecation locally.  All real
# stdlib calls still route through the hardened parser.
with _warnings.catch_warnings():
    _warnings.filterwarnings(
        "ignore",
        message=r".*cElementTree.*deprecated.*",
        category=DeprecationWarning,
    )
    defusedxml.defuse_stdlib()

from mediaman.services.infra.http_client import (
    SafeHTTPClient,
    SafeHTTPError,
    pin_dns_for_request,
)
from mediaman.services.infra.url_safety import resolve_safe_outbound_url
from mediaman.services.media_meta.anime_detect import is_anime as _is_anime_show

_logger = _logging.getLogger("mediaman")

# Matches X-Plex-Token query parameter values so they can be redacted from
# exception messages and log lines before they propagate.
_PLEX_TOKEN_RE = _re.compile(r"(X-Plex-Token=)[^&\s\"'>]+", _re.IGNORECASE)

#: Hard cap on a single plexapi response body. Library/season XML is
#: small even on large libraries — 16 MiB is well above any sane limit
#: while still preventing a runaway upstream from filling memory.
_PLEX_MAX_BYTES = 16 * 1024 * 1024

#: ``(connect, read)`` timeout used when plexapi passes us a single int.
#: 5 s connect matches mediaman's other clients; 30 s read is generous
#: enough for a slow library scan but stops a slow-lorris stalling a
#: worker indefinitely.
_PLEX_TIMEOUT: tuple[float, float] = (5.0, 30.0)


def _scrub_plex_token(msg: str) -> str:
    """Replace any ``X-Plex-Token=<value>`` substring in *msg* with ``<redacted>``.

    Applied to exception messages and log lines before they propagate so the
    token never appears in tracebacks, log files, or error responses.
    """
    return _PLEX_TOKEN_RE.sub(r"\1<redacted>", msg)


class _SafePlexSession(http_requests.Session):
    """``requests.Session`` subclass enforcing mediaman's outbound rules.

    Injected into :class:`plexapi.server.PlexServer` via the ``session=``
    constructor kwarg so every plexapi call — library enumeration,
    section scanning, raw queries — inherits:

    * Per-call SSRF re-validation, including IDN normalisation and
      DNS-rebind defence (the validated address is pinned for the
      duration of the request).
    * ``allow_redirects=False`` — a 302 to ``169.254.169.254`` would
      otherwise leak the X-Plex-Token into cloud metadata.
    * Streamed body capped at :data:`_PLEX_MAX_BYTES`, so a malicious
      or buggy upstream cannot pin a worker's memory.
    * ``(connect, read)`` timeout split, so a slow-lorris read cannot
      hold a connection indefinitely.

    The class deliberately does NOT inherit from ``SafeHTTPClient``;
    plexapi calls ``self._session.get(...)``-style methods, and
    ``requests.Session`` is the base contract those expect. We hook
    ``request()`` because every verb method routes through it.
    """

    def __init__(self, *, strict_egress: bool | None = None) -> None:
        super().__init__()
        self._strict_egress = strict_egress

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> http_requests.Response:
        # 1. SSRF re-validation. Re-runs at every request so DNS-rebind
        #    cannot slip past a one-off check at PlexServer construction
        #    time, and so a configured Plex URL pointing at an internal
        #    service is refused even if the operator persisted it before
        #    the check existed.
        safe, hostname, pinned_ip = resolve_safe_outbound_url(
            url, strict_egress=self._strict_egress
        )
        if not safe:
            # Match the SafeHTTPError shape for consistency with
            # SafeHTTPClient — the scrubbed URL keeps any token out of
            # the exception message.
            raise SafeHTTPError(
                status_code=0,
                body_snippet="refused by SSRF guard",
                url=_scrub_plex_token(url),
            )

        # 2. Force redirect refusal. A 302 to a metadata endpoint would
        #    take the X-Plex-Token header along for the ride, so we
        #    refuse to follow ANY redirect from a Plex URL.
        kwargs["allow_redirects"] = False

        # 3. Always stream so we control the body cap.
        kwargs["stream"] = True

        # 4. Normalise the timeout. plexapi defaults to a single int —
        #    convert to (connect, read) for stable behaviour. A caller
        #    passing a tuple already is honoured untouched.
        timeout = kwargs.get("timeout")
        if timeout is None or isinstance(timeout, (int, float)):
            kwargs["timeout"] = _PLEX_TIMEOUT

        # 5. DNS pin + dispatch. The pin closes the rebind window
        #    between the SSRF check above and the actual connect.
        if hostname and pinned_ip:
            with pin_dns_for_request(hostname, pinned_ip):
                response = super().request(method, url, **kwargs)
        else:
            response = super().request(method, url, **kwargs)

        # 6. Body cap — read up to the limit and re-attach so plexapi's
        #    .text / .content access works as it expects.
        try:
            body = self._read_capped(response, _PLEX_MAX_BYTES)
        except _PlexBodyTooLarge as exc:
            response.close()
            raise SafeHTTPError(
                status_code=response.status_code,
                body_snippet=str(exc),
                url=_scrub_plex_token(url),
            ) from None
        response._content = body
        response._content_consumed = True
        return response

    @staticmethod
    def _read_capped(response: http_requests.Response, max_bytes: int) -> bytes:
        """Read the response body up to *max_bytes*, raising if the cap is hit.

        Mirrors ``http_client._read_capped`` but kept private to this
        module so the Plex session has no compile-time dependency on
        SafeHTTPClient internals.
        """
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise _PlexBodyTooLarge(
                        f"Plex response body too large: declared {declared} > cap {max_bytes}"
                    )
            except ValueError:
                pass
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise _PlexBodyTooLarge(f"Plex response body exceeded cap of {max_bytes} bytes")
        return bytes(buf)


class _PlexBodyTooLarge(Exception):
    """Internal signal that a Plex response breached the body cap."""


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


def _to_utc(dt: datetime | None) -> datetime | None:
    """Promote a plexapi datetime to a tz-aware UTC value.

    PlexAPI parses Plex's XML ``addedAt`` / ``updatedAt`` (which are
    POSIX timestamps in seconds) via ``datetime.fromtimestamp(int(...))``
    — that yields a NAIVE local-time datetime even though the underlying
    instant is UTC.  Downstream code uses
    :func:`mediaman.services.infra.format.ensure_tz`, which now treats
    naive inputs as already-UTC.  Without explicit conversion here the
    stored timestamp would jump by the local UTC offset.

    On Python 3.12, ``naive.astimezone(tz)`` interprets the naive value
    as local time, so this round-trips correctly back to UTC for the
    actual Plex server's wall clock.  Aware inputs are pass-through-
    converted; ``None`` stays ``None``.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.astimezone(UTC)
    return dt.astimezone(UTC)


def _movie_to_item(movie) -> PlexMovieItem:
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
        "added_at": _to_utc(movie.addedAt),
        "updated_at": _to_utc(movie.updatedAt),
        "file_path": file_path,
        "file_size_bytes": file_size,
        "poster_path": movie.thumb,
    }


def _season_to_item(show, season) -> PlexSeasonItem:
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
        "added_at": _to_utc(added_at),
        "updated_at": _to_utc(show.updatedAt),
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

        The configured URL is **revalidated at construction time** —
        an admin URL persisted in the DB before SSRF checks tightened,
        or a host that has since started resolving to an internal
        address, is refused here. ``ValueError`` is raised in that
        case; the caller is expected to surface it as a configuration
        error rather than silently fall back to insecure behaviour.
        """
        # Construction-time validation: cheap, and catches a stale or
        # newly-malicious Plex URL before any token-bearing request goes
        # out. Per-call validation in :class:`_SafePlexSession` catches
        # rebinding attempts that happen between construction and use.
        safe, _hostname, _ip = resolve_safe_outbound_url(url)
        if not safe:
            # Use a generic message — the URL itself may be sensitive
            # (LAN hostname / port topology) but we still want operators
            # to see the failure at startup.
            raise ValueError(
                "Refusing to construct PlexClient: configured plex_url "
                "failed the SSRF guard. Verify the URL points to a "
                "reachable Plex server and is not an internal admin / "
                "metadata endpoint."
            )

        # Hardened session for everything plexapi does internally —
        # library enumeration, section scanning, raw queries. Without
        # this, plexapi's own ``requests.Session`` would not enforce
        # SSRF re-validation, redirect refusal, or body caps.
        self._safe_session = _SafePlexSession()
        self.server = PlexServer(url, token, session=self._safe_session)
        # Raw HTTP calls that bypass plexapi (e.g. /status/sessions/history)
        # still route through SafeHTTPClient so the same controls apply
        # to those endpoints too.
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
        entries: list[PlexWatchEntry] = []
        for v in root.findall(".//Video"):
            viewed_at_ts = v.get("viewedAt")
            if not viewed_at_ts:
                continue
            # A single malformed ``viewedAt`` / ``accountID`` (non-numeric,
            # negative, garbage) used to abort the entire history fetch
            # because both ``int(...)`` and ``datetime.fromtimestamp``
            # raise ``ValueError``/``OSError`` on bad input.  Skip the
            # offending row instead so a single corrupt record doesn't
            # silently zero out everyone's watch history.
            try:
                viewed_at = datetime.fromtimestamp(int(viewed_at_ts), tz=UTC)
                account_id = int(v.get("accountID", 0))
            except (ValueError, TypeError, OSError, OverflowError):
                _logger.debug(
                    "plex.history.skip_malformed viewedAt=%r accountID=%r",
                    viewed_at_ts,
                    v.get("accountID"),
                )
                continue
            entries.append({"viewed_at": viewed_at, "account_id": account_id})
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

        Pushes filtering to the Plex server via ``section.search`` with a
        ``userRating>>0`` filter so we only fetch the rated items rather
        than every movie/show in the library.  The previous full-iteration
        path was O(library size) per recommendation refresh — for a 10k+
        item library that meant minutes of network and response parsing
        even when nothing was rated.

        Plex stores user ratings on a 0–10 scale.  This converts them to
        a 1–5 star scale (rounded to nearest half).

        Returns:
            List of ``{"title": str, "type": "movie"|"tv", "stars": float}`` dicts.
        """
        rated: list[PlexRatedItem] = []
        for section in self.server.library.sections():
            if section.type not in ("movie", "show"):
                continue
            try:
                items = section.search(filters={"userRating>>": 0})
            except (PlexApiException, http_requests.RequestException) as exc:
                # Older Plex servers may reject the filter — fall back to
                # the slow path so the recommendation flow still works.
                _logger.warning(
                    "plex.user_ratings.filter_unsupported section=%s — falling back to "
                    "full enumeration: %s",
                    section.title,
                    _scrub_plex_token(str(exc)),
                )
                items = section.all()
            entry_type: Literal["movie", "tv"] = "movie" if section.type == "movie" else "tv"
            for item in items:
                if not item.userRating:
                    continue
                rated.append(
                    {
                        "title": item.title,
                        "type": entry_type,
                        "stars": round(item.userRating / 2, 1),
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
