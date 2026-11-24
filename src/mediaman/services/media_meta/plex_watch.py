"""Read-only Plex metadata for the Watch pages and player.

Every call is a GET with the token in the ``X-Plex-Token`` header. Nothing here
writes to Plex (no timeline, scrobble or rating calls). Failures surface as the
three domain errors below, whose messages never carry a URL or a token.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as ET
import requests

from mediaman.core.format import log_snippet
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger(__name__)

ItemType = Literal["movie", "episode", "show", "season"]
_ITEM_TYPES = frozenset({"movie", "episode", "show", "season"})

_XML_MAX_BYTES = 4 * 1024 * 1024
_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_TIMEOUT: tuple[float, float] = (5.0, 15.0)
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_RATING_KEY_RE = re.compile(r"[0-9]{1,18}")
# Plex image paths are plain: no query, fragment, percent-escapes or dot segments.
_IMAGE_PATH_RE = re.compile(r"/library/[A-Za-z0-9_\-/]+")


class PlexError(Exception):
    """Base class for the errors raised by this module."""


class PlexNotConfigured(PlexError):
    """No Plex URL or token is set."""


class PlexUnavailable(PlexError):
    """Plex cannot be reached, answered 5xx, or sent something unreadable."""


class PlexItemNotFound(PlexError):
    """Plex has no such item (or it is not a kind Watch can show)."""


@dataclass(frozen=True)
class PlexItem:
    type: ItemType
    rating_key: str
    title: str
    summary: str
    year: int | None
    duration_s: int
    library_section_id: str
    thumb: str | None  # Plex-relative path; never send it to a browser
    subtitle_selected: bool
    index: int | None = None
    parent_index: int | None = None
    parent_rating_key: str | None = None
    grandparent_rating_key: str | None = None
    grandparent_title: str | None = None


@dataclass(frozen=True)
class PlexSeason:
    rating_key: str
    index: int
    title: str


@dataclass(frozen=True)
class PlexShow:
    rating_key: str
    title: str
    summary: str
    year_first: int | None
    year_last: int | None
    genre: str | None
    library_section_id: str
    thumb: str | None
    seasons: list[PlexSeason]


@dataclass(frozen=True)
class PlexEpisode:
    rating_key: str
    index: int
    title: str
    duration_s: int
    thumb: str | None
    season_index: int | None = None
    season_rating_key: str | None = None


def is_in_libraries(item: PlexItem | PlexShow, allowed_section_ids: Collection[str | int]) -> bool:
    """True when *item* sits in one of the libraries mediaman scans."""
    allowed = {str(s) for s in allowed_section_ids}
    return bool(item.library_section_id) and item.library_section_id in allowed


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _seconds(ms: str | None) -> int:
    return round((_int(ms) or 0) / 1000)


def _check_key(rating_key: str) -> str:
    key = str(rating_key)
    if not _RATING_KEY_RE.fullmatch(key):
        raise PlexItemNotFound("Item not found in Plex")
    return key


def _subtitle_selected(node: Element) -> bool:
    part = node.find("Media/Part")
    if part is None:
        return False
    return any(
        s.get("streamType") == "3" and s.get("selected") == "1" for s in part.findall("Stream")
    )


def _episode(node: Element, container: Element) -> PlexEpisode:
    return PlexEpisode(
        rating_key=node.get("ratingKey", ""),
        index=_int(node.get("index")) or 0,
        title=node.get("title", ""),
        duration_s=_seconds(node.get("duration")),
        thumb=node.get("thumb"),
        season_index=_int(node.get("parentIndex", container.get("parentIndex"))),
        season_rating_key=node.get("parentRatingKey"),
    )


class PlexWatch:
    """Read-only Plex reader. Raises PlexNotConfigured when *url* or *token* is empty.

    *http* is injectable for tests; by default a SafeHTTPClient applies the SSRF
    guard (optionally narrowed by *allowed_hosts*), a body cap and a timeout.
    """

    def __init__(
        self,
        url: str | None,
        token: str | None,
        *,
        allowed_hosts: frozenset[str] | None = None,
        http: SafeHTTPClient | None = None,
    ) -> None:
        if not url or not token:
            raise PlexNotConfigured("Plex is not set up")
        self._base = url.rstrip("/")
        self._token = token
        self._http = http or SafeHTTPClient(
            default_timeout=_TIMEOUT,
            default_max_bytes=_XML_MAX_BYTES,
            allowed_hosts=allowed_hosts,
        )

    def _get(self, path: str, *, max_bytes: int = _XML_MAX_BYTES) -> requests.Response:
        try:
            return self._http.get(
                f"{self._base}{path}",
                headers={"X-Plex-Token": self._token},
                max_bytes=max_bytes,
                retry=False,
            )
        except SafeHTTPError as exc:
            if exc.status_code == 404:
                raise PlexItemNotFound("Item not found in Plex") from None
            logger.warning(
                "plex_watch.request_failed status=%s reason=%s",
                exc.status_code,
                log_snippet(exc.body_snippet.replace(self._token, "***"), 120),
            )
            raise PlexUnavailable("Plex could not be reached") from None
        except requests.RequestException as exc:
            logger.warning("plex_watch.request_failed error=%s", type(exc).__name__)
            raise PlexUnavailable("Plex could not be reached") from None

    def _xml(self, path: str) -> Element:
        resp = self._get(path)
        try:
            root: Element = ET.fromstring(resp.content)
            return root
        except ParseError:
            raise PlexUnavailable("Plex sent an unreadable response") from None
        finally:
            resp.close()

    def ping(self) -> None:
        """Raise PlexUnavailable unless the server answers its identity request."""
        self._get("/identity", max_bytes=_XML_MAX_BYTES).close()

    def _metadata(self, rating_key: str) -> tuple[Element, Element]:
        """Return (container, item node) for one rating key."""
        container = self._xml(f"/library/metadata/{_check_key(rating_key)}")
        node = next((n for n in container if n.tag in ("Video", "Directory")), None)
        if node is None or node.get("type") not in _ITEM_TYPES:
            raise PlexItemNotFound("Item not found in Plex")
        return container, node

    def _children(self, rating_key: str) -> tuple[Element, list[Element]]:
        container = self._xml(f"/library/metadata/{_check_key(rating_key)}/children")
        return container, [n for n in container if n.tag in ("Video", "Directory")]

    def get_item(self, rating_key: str) -> PlexItem:
        container, node = self._metadata(rating_key)
        item_type: ItemType = node.get("type")  # type: ignore[assignment]  # checked in _metadata
        is_episode = item_type == "episode"
        return PlexItem(
            type=item_type,
            rating_key=node.get("ratingKey", str(rating_key)),
            title=node.get("title", ""),
            summary=node.get("summary", ""),
            year=_int(node.get("year")),
            duration_s=_seconds(node.get("duration")),
            library_section_id=node.get("librarySectionID", container.get("librarySectionID", "")),
            thumb=node.get("thumb"),
            subtitle_selected=_subtitle_selected(node),
            index=_int(node.get("index")) if item_type in ("episode", "season") else None,
            parent_index=_int(node.get("parentIndex")) if is_episode else None,
            parent_rating_key=node.get("parentRatingKey") if is_episode else None,
            grandparent_rating_key=node.get("grandparentRatingKey") if is_episode else None,
            grandparent_title=node.get("grandparentTitle") if is_episode else None,
        )

    def _seasons(self, show_key: str) -> list[PlexSeason]:
        """The show's seasons in order, specials (index 0) excluded."""
        _, nodes = self._children(show_key)
        seasons = []
        for n in nodes:
            index = _int(n.get("index"))
            if n.get("type") == "season" and index:
                seasons.append(PlexSeason(n.get("ratingKey", ""), index, n.get("title", "")))
        return sorted(seasons, key=lambda s: s.index)

    def get_show(self, show_key: str) -> PlexShow:
        container, node = self._metadata(show_key)
        if node.get("type") != "show":
            raise PlexItemNotFound("Item not found in Plex")
        seasons = self._seasons(show_key)
        genre = node.find("Genre")
        year_first = _int(node.get("year"))
        year_last = self._last_aired_year(seasons[-1]) if seasons else None
        if year_last is not None and year_first is not None:
            year_last = max(year_last, year_first)
        return PlexShow(
            rating_key=node.get("ratingKey", str(show_key)),
            title=node.get("title", ""),
            summary=node.get("summary", ""),
            year_first=year_first,
            year_last=year_last,
            genre=genre.get("tag") if genre is not None else None,
            library_section_id=node.get("librarySectionID", container.get("librarySectionID", "")),
            thumb=node.get("thumb"),
            seasons=seasons,
        )

    def _last_aired_year(self, season: PlexSeason) -> int | None:
        _, nodes = self._children(season.rating_key)
        years = [
            y for n in nodes if (y := _int((n.get("originallyAvailableAt") or "")[:4])) is not None
        ]
        return max(years) if years else None

    def get_season_episodes(self, season_key: str) -> list[PlexEpisode]:
        container, nodes = self._children(season_key)
        episodes = [_episode(n, container) for n in nodes if n.get("type") == "episode"]
        return sorted(episodes, key=lambda e: e.index)

    def next_episode(self, episode_rating_key: str) -> PlexEpisode | None:
        """The episode after this one, crossing into the next season; None after the last."""
        current = self.get_item(episode_rating_key)
        if (
            current.type != "episode"
            or not current.parent_rating_key
            or not current.grandparent_rating_key
        ):
            raise PlexItemNotFound("Item not found in Plex")
        if current.parent_index == 0:
            # Specials sit outside the show's regular run, so nothing follows them.
            return None
        for ep in self.get_season_episodes(current.parent_rating_key):
            if ep.index > (current.index or 0):
                return ep
        for season in self._seasons(current.grandparent_rating_key):
            if season.index > (current.parent_index or 0):
                following = self.get_season_episodes(season.rating_key)
                if following:
                    return following[0]
        return None

    def fetch_image(self, thumb_path: str) -> tuple[bytes, str]:
        """Fetch a Plex-relative image (``/library/...``); returns (bytes, content_type)."""
        if not _IMAGE_PATH_RE.fullmatch(thumb_path) or "//" in thumb_path:
            raise PlexItemNotFound("Image not found in Plex")
        resp = self._get(thumb_path, max_bytes=_IMAGE_MAX_BYTES)
        try:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return resp.content, content_type if content_type in _IMAGE_TYPES else "image/jpeg"
        finally:
            resp.close()
