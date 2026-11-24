"""One account's play history, read from the Plex server with GET requests only."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from xml.etree.ElementTree import Element

from mediaman.services.media_meta.plex_watch import PlexUnavailable, PlexWatch
from mediaman.services.plex_link.repository import PlexPlay

PAGE_SIZE = 200
# Above one person's limit, in case the server sends everyone's plays.
MAX_PAGES = 50
_TEXT_MAX = 300
_KEY_MAX = 120
_NUMBER_MAX = 99_999
_CLOCK_SLACK = timedelta(days=1)


def _number(value: str | None) -> int | None:
    """A season or episode number, or None for one that cannot be real."""
    try:
        number = int(value) if value else None
    except ValueError:
        return None
    return number if number is not None and 0 <= number <= _NUMBER_MAX else None


def _text(value: str | None) -> str | None:
    return (value or "").strip()[:_TEXT_MAX] or None


def _key_id(value: str | None) -> str | None:
    """A rating key, also from the end of a key path."""
    key = (value or "").rpartition("/")[2]
    return key if key.isascii() and key.isdigit() and len(key) <= 18 else None


@dataclass(frozen=True, slots=True)
class Plays:
    found: list[PlexPlay]
    # False when the pages ran out before all the plays asked for were seen.
    complete: bool


class PlexHistory(PlexWatch):
    def _xml(self, path: str) -> Element:
        try:
            return super()._xml(path)
        except (ValueError, LookupError):
            # Entities and the like, which the hardened parser refuses, or an unknown encoding.
            raise PlexUnavailable("Plex sent an unreadable response") from None

    def server_id(self) -> str:
        """The server's machine identifier; raises PlexUnavailable when it sends none."""
        server_id = self._xml("/identity").get("machineIdentifier")
        if not server_id:
            raise PlexUnavailable("Plex did not say which server it is")
        return server_id

    def plays(
        self,
        account_id: int,
        *,
        since: datetime,
        now: datetime,
        libraries: set[str],
        limit: int,
        deadline: float,
    ) -> Plays:
        """The newest *limit* plays of *account_id* in *libraries* from *since* on.

        What a play does not say counts against it: without the account, the library or the
        time, it is left out. The server's own filters are asked for and then applied again
        here, since a server that ignored them would send other people's plays. Raises
        PlexUnavailable once the monotonic clock passes *deadline*, whatever was found.
        """
        found: dict[str, PlexPlay] = {}
        seen: set[str] = set()
        start = 0
        for _ in range(MAX_PAGES):
            if time.monotonic() > deadline:
                raise PlexUnavailable("Plex took too long to send the plays")
            query = urlencode(
                {
                    "accountID": account_id,
                    "sort": "viewedAt:desc",
                    "viewedAt>": int(since.timestamp()),
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": PAGE_SIZE,
                }
            )
            rows = self._xml(f"/status/sessions/history/all?{query}").findall("Video")
            keys = {row.get("historyKey", "") for row in rows}
            if keys <= seen:
                break
            seen |= keys
            start += len(rows)
            times = [_viewed_at(row) for row in rows]
            if all(viewed is not None and viewed < since for viewed in times):
                break
            for row, viewed in zip(rows, times, strict=True):
                if viewed is None or not since <= viewed <= now + _CLOCK_SLACK:
                    continue
                if row.get("accountID") != str(account_id):
                    continue
                if row.get("librarySectionID") not in libraries:
                    continue
                play = _play(row, viewed)
                if play is not None:
                    found[play.history_key] = play
                    if len(found) >= limit:
                        return Plays(list(found.values()), complete=True)
        else:
            return Plays(list(found.values()), complete=False)
        return Plays(list(found.values()), complete=True)


def _viewed_at(row: Element) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(row.get("viewedAt", "")), tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _play(row: Element, viewed: datetime) -> PlexPlay | None:
    kind, title, history_key = row.get("type"), _text(row.get("title")), row.get("historyKey")
    if kind not in ("movie", "episode") or not title or not history_key:
        return None
    if len(history_key) > _KEY_MAX:
        return None
    show_title = _text(row.get("grandparentTitle")) if kind == "episode" else None
    if kind == "episode" and show_title is None:
        return None
    return PlexPlay(
        history_key=history_key,
        media_type=kind,
        title=title,
        viewed_at=viewed.isoformat(),
        library_id=row.get("librarySectionID"),
        rating_key=_key_id(row.get("ratingKey")),
        show_title=show_title,
        show_rating_key=_key_id(row.get("grandparentRatingKey") or row.get("grandparentKey")),
        season_number=_number(row.get("parentIndex")) if kind == "episode" else None,
        episode_number=_number(row.get("index")) if kind == "episode" else None,
    )
