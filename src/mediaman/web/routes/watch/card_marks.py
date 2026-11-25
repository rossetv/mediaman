"""What a card says of one person's own rating and mark of its title."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from mediaman.core.time import now_utc
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.web.repository.seen_titles import PersonMarks, TitleKey

logger = logging.getLogger(__name__)

# (TMDB title, its library ref, whether the library's own rows say watched) -> a card's fields
CardMarks = Callable[[TitleKey | None, str | None, bool], dict[str, object]]

#: Run the background asks inline (tests).
sync = False
_lock = threading.Lock()
_asking: set[int] = set()
# More marked shows than this of unknown seasons on one page are not asked of at once.
_ASKING_MAX = 60
_pool: ThreadPoolExecutor | None = None


def title_key(media_type: object, tmdb_id: object) -> TitleKey | None:
    """A title by a card's or row's fields; None without a TMDB id."""
    if media_type not in ("movie", "tv") or not isinstance(tmdb_id, int) or tmdb_id <= 0:
        return None
    return str(media_type), tmdb_id


def tmdb_ref(key: TitleKey | None) -> str | None:
    return f"tmdb-{key[0]}-{key[1]}" if key is not None else None


def latest_season_out(fields: Mapping[str, Any], today: date) -> int | None:
    """The number of a show's latest season out on *today*, by TMDB's *fields* of it; None when
    they do not say."""
    seasons = [s for s in fields["tmdb_seasons"] if "air_date" in s]
    dated = [s for s in seasons if s["air_date"]]
    if dated:
        out = [s["number"] for s in dated if s["air_date"] <= today.isoformat()]
    else:
        out = [s["number"] for s in seasons if s["episode_count"]]
    return max(out, default=None)


def card_marks(marks: PersonMarks, client: TmdbClient | None) -> CardMarks:
    """The fields that *marks* give the cards of one reply. The library's own rows say it of a
    title it holds, the marks included. A show outside it, marked up to a season, is watched
    until TMDB, as last heard, has a newer one out."""
    today = now_utc().date()

    def fields(key: TitleKey | None, library_ref: str | None, watched: bool) -> dict[str, object]:
        if key is not None and library_ref is None and key in marks.seen:
            through = marks.seen[key]
            watched = key not in marks.partly and (
                through is None or not _has_newer_season(client, key[1], through, today)
            )
        return {
            "tmdb_ref": tmdb_ref(key),
            "level": marks.levels.get(key) if key is not None else None,
            "watched": watched,
        }

    return fields


def marked(card: Mapping[str, Any], marks: CardMarks) -> dict[str, object]:
    """*card*, of a TMDB title as its ref says, with its marks."""
    _, media_type, tmdb_id = str(card["ref"]).split("-")
    key = (media_type, int(tmdb_id))
    return {**card, **marks(key, card["library_ref"], bool(card["watched"]))}


def _has_newer_season(client: TmdbClient | None, tmdb_id: int, through: int, today: date) -> bool:
    if client is None:
        return False
    try:
        fields = catalog.details(client, "tv", tmdb_id, allow_fetch=lambda: False, brief=True)
    except catalog.TmdbUnavailable:
        return False
    except disk_cache.Refused:
        _ask_later(client, tmdb_id)
        return False
    latest = latest_season_out(fields, today)
    return latest is not None and latest > through


def _ask_later(client: TmdbClient, tmdb_id: int) -> None:
    global _pool
    with _lock:
        if tmdb_id in _asking or len(_asking) >= _ASKING_MAX:
            return
        _asking.add(tmdb_id)

    def run() -> None:
        try:
            catalog.details(client, "tv", tmdb_id, background=True)
        except (catalog.TmdbUnavailable, disk_cache.Refused):
            logger.info("watch: the seasons of a marked show could not be read")
        finally:
            with _lock:
                _asking.discard(tmdb_id)

    if sync:
        run()
        return
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-seasons")
    try:
        _pool.submit(run)
    except RuntimeError:  # the pool is shut down (interpreter exit)
        with _lock:
            _asking.discard(tmdb_id)


__all__ = ["CardMarks", "card_marks", "latest_season_out", "marked", "title_key", "tmdb_ref"]
