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

Security infrastructure and data types are in the sub-modules
:mod:`._plex_session` and :mod:`._plex_types` respectively.
"""

from __future__ import annotations

import logging as _logging
import warnings as _warnings
from datetime import UTC, datetime
from typing import Literal

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

from mediaman.core.scrub_filter import ScrubFilter, register_secret
from mediaman.services.infra import (
    SafeHTTPClient,
    SafeHTTPError,
    SSRFRefused,
    resolve_safe_outbound_url,
)
from mediaman.services.media_meta._plex_session import (  # noqa: F401
    _PLEX_MAX_BYTES,
    _PLEX_TIMEOUT_SECONDS,
    _PLEX_TOKEN_RE,
    _PlexBodyTooLarge,
    _SafePlexSession,
    _scrub_plex_token,
)
from mediaman.services.media_meta._plex_types import (  # noqa: F401
    _HISTORY_MAX_BYTES,
    PlexAccount,
    PlexLibrarySection,
    PlexMovieItem,
    PlexSeasonItem,
    PlexWatchEntry,
    _movie_to_item,
    _season_to_item,
    _to_utc,
)
from mediaman.services.media_meta._plex_types import (
    PlexRatedItem as PlexRatedItem,
)

_logger = _logging.getLogger(__name__)


class PlexClient:
    """Wraps plexapi to provide the specific queries mediaman needs.

    Handles library enumeration, item scanning (movies and TV seasons),
    watch history retrieval, and account listing.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        """Create a PlexServer connection.

        Args:
            url: Base URL of the Plex Media Server, e.g. ``http://plex:32400``.
            token: Plex authentication token (X-Plex-Token).
            allowed_hosts: Optional SSRF allowlist (W1.32). When supplied,
                every outbound call from the hardened plexapi session and
                the raw history HTTP client refuses any host whose
                IDN-normalised form is not in the set (or in
                :data:`~mediaman.services.infra.url_safety.PINNED_EXTERNAL_HOSTS`).
                Construction-time validation also enforces the allowlist.
                Callers compose the set via
                :func:`~mediaman.services.infra.url_safety.allowed_outbound_hosts`.
                ``None`` (default) keeps deny-list-only behaviour.

        The configured URL is **revalidated at construction time** —
        an admin URL persisted in the DB before SSRF checks tightened,
        or a host that has since started resolving to an internal
        address, is refused here. ``SSRFRefused`` is raised in that
        case; the caller is expected to surface it as a configuration
        error rather than silently fall back to insecure behaviour.
        """
        # Construction-time validation: cheap, and catches a stale or
        # newly-malicious Plex URL before any token-bearing request goes
        # out. Per-call validation in :class:`_SafePlexSession` catches
        # rebinding attempts that happen between construction and use.
        # ``allowed_hosts`` is only forwarded when supplied so that test
        # monkeypatches of ``resolve_safe_outbound_url`` taking ``url``
        # alone keep working.
        if allowed_hosts is None:
            safe, _hostname, _ip = resolve_safe_outbound_url(url)
        else:
            safe, _hostname, _ip = resolve_safe_outbound_url(url, allowed_hosts=allowed_hosts)
        if not safe:
            # Use a generic message — the URL itself may be sensitive
            # (LAN hostname / port topology) but we still want operators
            # to see the failure at startup.
            raise SSRFRefused(
                "Refusing to construct PlexClient: configured plex_url "
                "failed the SSRF guard. Verify the URL points to a "
                "reachable Plex server and is not an internal admin / "
                "metadata endpoint."
            )

        # Attach log scrubbing for the token so it is never emitted in
        # DEBUG output from urllib3, requests, or mediaman itself.
        # Idempotent — safe to call at construction time; repeated calls
        # with the same token do not stack filters.
        ScrubFilter.attach("urllib3.connectionpool", secrets=[token])
        register_secret(token)

        # Hardened session for everything plexapi does internally —
        # library enumeration, section scanning, raw queries. Without
        # this, plexapi's own ``requests.Session`` would not enforce
        # SSRF re-validation, redirect refusal, or body caps.
        self._safe_session = _SafePlexSession(allowed_hosts=allowed_hosts)
        self.server = PlexServer(url, token, session=self._safe_session)  # type: ignore[no-untyped-call]  # rationale: plexapi has no stubs
        # Raw HTTP calls that bypass plexapi (e.g. /status/sessions/history)
        # still route through SafeHTTPClient so the same controls apply
        # to those endpoints too.
        self._http = SafeHTTPClient(
            default_max_bytes=_HISTORY_MAX_BYTES,
            allowed_hosts=allowed_hosts,
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
        season = self.server.fetchItem(int(season_rating_key))  # type: ignore[no-untyped-call]  # rationale: plexapi has no stubs
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
                    str(exc),
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

    def is_reachable(self) -> bool:
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
        response = self.server.query("/accounts")  # type: ignore[no-untyped-call]  # rationale: plexapi has no stubs
        accounts: list[PlexAccount] = []
        for account in response.findall(".//Account"):
            name = account.get("name", "")
            if name:
                accounts.append({"id": int(account.get("id", 0)), "name": name})
        return accounts
