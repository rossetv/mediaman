"""Plex API client for library scanning and watch history.

plexapi parses Plex responses with the stdlib xml.etree, which is exposed to
billion-laughs and external-entity attacks. defusedxml.defuse_stdlib is installed at
import time (idempotent, global) so every plexapi call is protected.

The hardened session and the data types live in _plex_session and _plex_types.
"""

from __future__ import annotations

import logging
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
# imported from inside defuse_stdlib on Python 3.13+ (the stdlib
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

logger = logging.getLogger(__name__)


class PlexResponseTooLarge(ValueError):
    """Raised when a Plex API response body exceeds the size cap.

    Subclasses ValueError so existing ValueError handlers keep working. The scanner's
    fetch catch-all includes it so body-cap events on watch-history fetches do not
    propagate uncaught.
    """


class PlexClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        """Connect to Plex, revalidating the URL against the SSRF rules.

        A URL persisted before the checks tightened, or a host that now resolves to an
        internal address, is refused here with SSRFRefused; the caller should surface it
        as a configuration error.

        allowed_hosts is an optional SSRF allowlist applied to every outbound call
        (PINNED_EXTERNAL_HOSTS are always allowed). None keeps deny-list-only behaviour.
        """
        # Construction-time validation: cheap, and catches a stale or
        # newly-malicious Plex URL before any token-bearing request goes
        # out. Per-call validation in _SafePlexSession catches
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
        return [
            {"id": str(s.key), "type": s.type, "title": s.title}
            for s in self.server.library.sections()
        ]

    def get_movie_items(self, library_id: str) -> list[PlexMovieItem]:
        section = self.server.library.sectionByID(int(library_id))
        return [_movie_to_item(movie) for movie in section.all()]

    def get_show_seasons(self, library_id: str) -> list[PlexSeasonItem]:
        """Return all non-special seasons across every show in a TV library.

        The season directory is the parent of the first episode's file; if the season
        has no addedAt, the earliest episode's is used.
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
        """Return watch history for one item via /status/sessions/history/all, which is
        more reliable than plexapi's .history().
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
            if (
                exc.status_code == 0
                or "cap" in exc.body_snippet.lower()
                or "too large" in exc.body_snippet.lower()
            ):
                # Body-cap events surface as a domain exception so callers can
                # distinguish them from generic errors. PlexResponseTooLarge
                # inherits from ValueError, preserving compatibility with any
                # catcher that already uses ``except ValueError``.
                raise PlexResponseTooLarge(_scrub_plex_token(exc.body_snippet)) from exc
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
                logger.debug(
                    "plex.history.skip_malformed viewedAt=%r accountID=%r",
                    viewed_at_ts,
                    v.get("accountID"),
                )
                continue
            entries.append({"viewed_at": viewed_at, "account_id": account_id})
        return entries

    def get_season_watch_history(self, season_rating_key: str) -> list[PlexWatchEntry]:
        """Aggregate watch history across all episodes in a season.

        If any episode's history cannot be fetched, the whole season aborts by
        re-raising, so the scanner records it as skipped and protects it from orphan
        removal instead of building a partial history that could read as fully watched
        or never watched.
        """
        season = self.server.fetchItem(int(season_rating_key))  # type: ignore[no-untyped-call]  # rationale: plexapi has no stubs
        entries: list[PlexWatchEntry] = []
        for ep in season.episodes():
            try:
                ep_history = self.get_watch_history(str(ep.ratingKey))
            except (PlexResponseTooLarge, http_requests.RequestException, PlexApiException):
                # One bad episode must not crash the aggregation loop, but a
                # partial season history is unsafe — abort the whole season so
                # fetch.py marks it skipped (conservative; never misclassify).
                logger.debug(
                    "plex.season_history.episode_fetch_failed season=%s episode=%s — "
                    "aborting season aggregation to avoid a partial history",
                    season_rating_key,
                    getattr(ep, "ratingKey", "?"),
                    exc_info=True,
                )
                raise
            for h in ep_history:
                h["episode_title"] = ep.title
                entries.append(h)
        return entries

    def get_user_ratings(self) -> list[PlexRatedItem]:
        """Return all user-rated items across movie and TV libraries.

        Filters on the server (userRating>>0) instead of iterating every item, which was
        O(library size) per refresh. Plex's 0-10 ratings are converted to 1-5 stars,
        rounded to the nearest half.
        """
        rated: list[PlexRatedItem] = []
        for section in self.server.library.sections():
            if section.type not in ("movie", "show"):
                continue
            try:
                items = section.search(filters={"userRating>>": 0})
            except (PlexApiException, http_requests.RequestException):
                # Older Plex servers may reject the filter — fall back to
                # the slow path so the recommendation flow still works.
                logger.warning(
                    "plex.user_ratings.filter_unsupported section=%s — falling back to "
                    "full enumeration",
                    section.title,
                    exc_info=True,
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

        Catches only API and transport errors, not the broad Exception, so SystemExit,
        KeyboardInterrupt and programming errors are not swallowed.
        """
        try:
            self.server.library.sections()
            return True
        except (PlexApiException, http_requests.RequestException):
            return False

    def get_accounts(self) -> list[PlexAccount]:
        """Return named Plex accounts from /accounts; the home admin (id=1, empty name) is
        excluded.
        """
        response = self.server.query("/accounts")  # type: ignore[no-untyped-call]  # rationale: plexapi has no stubs
        accounts: list[PlexAccount] = []
        for account in response.findall(".//Account"):
            name = account.get("name", "")
            if name:
                accounts.append({"id": int(account.get("id", 0)), "name": name})
        return accounts
