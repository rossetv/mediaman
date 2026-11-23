"""The Watch pages: the browse page, a show, a movie and the player.

Rows come from what the library sync stored; a show's seasons and episodes,
an item's details and the next episode come live from Plex. Every page
refuses (404) an id outside the scanned libraries, for admins and viewers
alike. Plex failures become the page's ``watch_error`` ("not_set_up" or
"unreachable"), never a Plex address or a stack trace.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Concatenate, ParamSpec, TypeVar, cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.services.infra import allowed_outbound_hosts, get_string_setting
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexItem,
    PlexItemNotFound,
    PlexNotConfigured,
    PlexShow,
    PlexWatch,
    is_in_libraries,
)
from mediaman.services.scheduled_actions import parse_execute_at
from mediaman.services.scheduled_actions.days_left import days_left
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import (
    MIN_RESUME_SECONDS,
    WatchProgress,
    continue_watching,
    get_progress,
    show_progress,
)

router = APIRouter()

T = TypeVar("T")
P = ParamSpec("P")

# Items added within this many days make up the Recently added row.
RECENT_DAYS = 30
# How many already-watched episodes to step over when looking for the next unwatched one.
_NEXT_LOOKAHEAD = 20
# The Continue watching row stops filling at this many cards, and a page load spends at most
# this many Plex HTTP calls on it however long the person's history is.
_CONTINUE_LIMIT = 20
_PLEX_LOOKUP_BUDGET = 60
# next_episode() makes up to four Plex calls (the episode, its season, the show's seasons, the
# next season); it is charged the worst case.
_NEXT_EPISODE_COST = 4
# How long a next-episode answer is reused, so a finished show does not cost Plex calls on every
# load while a newly added episode still shows up within minutes.
_NEXT_EPISODE_TTL_S = 300.0
_NEXT_EPISODE_CACHE_MAX = 500
_next_episode_cache: dict[str, tuple[float, PlexEpisode | None]] = {}
# How long a good reachability answer is reused when a page load makes no other Plex call.
_REACHABLE_TTL_S = 30.0
_reachable_until = [0.0]
_KEY_RE = re.compile(r"[0-9]{1,18}")
# The only places the player's Back may return to; anything else falls back to the item's own page.
_ORIGIN_RE = re.compile(
    r"/watch|/watch/show/[0-9]{1,18}(\?season=[0-9]{1,4})?|/watch/movie/[0-9]{1,18}"
)


def open_plex(request: Request, conn: sqlite3.Connection) -> PlexWatch:
    """A PlexWatch for the configured server; raises PlexNotConfigured when it is not set up."""
    secret_key = request.app.state.config.secret_key
    return PlexWatch(
        get_string_setting(conn, "plex_url"),
        get_string_setting(conn, "plex_token", secret_key=secret_key),
        allowed_hosts=allowed_outbound_hosts(conn),
    )


def plex_error_code(exc: PlexError) -> str:
    return "not_set_up" if isinstance(exc, PlexNotConfigured) else "unreachable"


def is_valid_key(key: str) -> bool:
    return bool(_KEY_RE.fullmatch(key))


def poster_url(key: str) -> str:
    return f"/api/poster/{key}"


def safe_origin(value: str | None) -> str | None:
    """*value* when it is one of the Watch pages a person can come from, else None."""
    return value if value and _ORIGIN_RE.fullmatch(value) else None


def play_href(rating_key: str, *, resume: bool = False, origin: str | None = None) -> str:
    params = ["resume=1"] if resume else []
    if origin:
        params.append(f"from={quote(origin, safe='')}")
    return f"/watch/play/{rating_key}" + (f"?{'&'.join(params)}" if params else "")


def _origin_for(origin: str | None, episode: PlexEpisode) -> str | None:
    """*origin*, with a show page's season changed to the one *episode* is in."""
    if origin and episode.season_index is not None:
        base, sep, _ = origin.partition("?season=")
        if sep:
            return f"{base}?season={episode.season_index}"
    return origin


def episode_label(season: int | None, episode: int | None) -> str:
    if season is None:
        return f"E{episode or 0}"
    return f"S{season} · E{episode or 0}"


def _minutes(seconds: float) -> int | None:
    return round(seconds / 60) or None


def resumable(progress: WatchProgress | None) -> bool:
    return (
        progress is not None and not progress.watched and progress.position_s > MIN_RESUME_SECONDS
    )


def duration_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def progress_fields(progress: WatchProgress) -> tuple[int | None, int | None]:
    """(progress percent, minutes left) for an item in progress."""
    if progress.duration_s <= 0:
        return None, None
    pct = min(99, max(1, int(progress.position_s * 100 / progress.duration_s)))
    left = max(1, math.ceil((progress.duration_s - progress.position_s) / 60))
    return pct, left


def leaving_info(pending: lib.PendingDeletion | None, now: datetime) -> dict[str, Any] | None:
    if pending is None:
        return None
    execute_at = parse_execute_at(pending.execute_at, default=now)
    return {"days": days_left(execute_at, now), "keep_id": pending.media_item_id}


def card(
    kind: str,
    title: str,
    subtitle: str,
    poster: str,
    href: str,
    *,
    progress_pct: int | None = None,
    minutes_left: int | None = None,
    watched: bool = False,
    leaving: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "poster_url": poster,
        "href": href,
        "progress_pct": progress_pct,
        "minutes_left": minutes_left,
        "watched": watched,
        "leaving_days": leaving["days"] if leaving else None,
        "keep_id": leaving["keep_id"] if leaving else None,
    }


def movie_card(
    movie: lib.LibraryMovie,
    *,
    watched: bool = False,
    leaving: dict[str, Any] | None = None,
    progress_pct: int | None = None,
) -> dict[str, Any]:
    return card(
        "movie",
        movie.title,
        "Movie",
        poster_url(movie.key),
        f"/watch/movie/{movie.key}",
        watched=watched,
        leaving=leaving,
        progress_pct=progress_pct,
    )


def show_subtitle(show: lib.LibraryShow) -> str:
    kind = "Anime" if show.anime else "TV"
    if not show.season_count:
        return kind
    seasons = "1 season" if show.season_count == 1 else f"{show.season_count} seasons"
    return f"{kind} · {seasons}"


def show_card(show: lib.LibraryShow) -> dict[str, Any]:
    return card(
        "show", show.title, show_subtitle(show), poster_url(show.key), f"/watch/show/{show.key}"
    )


def movie_cards(
    conn: sqlite3.Connection,
    username: str,
    movies: list[lib.LibraryMovie],
    pending: list[lib.PendingDeletion],
    now: datetime,
) -> list[dict[str, Any]]:
    """Movie cards marked with this person's Watched state and any scheduled deletion."""
    progress = get_progress(conn, username, [m.key for m in movies])
    leaving = {p.media_item_id: leaving_info(p, now) for p in pending if p.season_number is None}
    return [
        movie_card(
            m,
            watched=bool(progress.get(m.key) and progress[m.key].watched),
            leaving=leaving.get(m.key),
            progress_pct=(
                progress_fields(progress[m.key])[0] if resumable(progress.get(m.key)) else None
            ),
        )
        for m in movies
    ]


def _render(request: Request, template: str, ctx: dict[str, Any]) -> Response:
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(request, template, {"nav_active": "watch", **ctx})


def _is_admin(conn: sqlite3.Connection, username: str) -> bool:
    return get_user_role(conn, username) == "admin"


def _next_unwatched(
    next_episode: Callable[[str], PlexEpisode | None],
    episode_key: str,
    progress: dict[str, WatchProgress],
) -> PlexEpisode | None:
    """The first episode after *episode_key* this person has not watched; None if there is none."""
    episode = next_episode(episode_key)
    for _ in range(_NEXT_LOOKAHEAD):
        if episode is None:
            return None
        seen = progress.get(episode.rating_key)
        if seen is None or not seen.watched:
            return episode
        episode = next_episode(episode.rating_key)
    return None


def _get_item(plex: PlexWatch, rating_key: str) -> PlexItem:
    return plex.get_item(rating_key)


class _OptionalPlex:
    """Plex for a page that can be built without it: opened on first use, and after the
    first failure (not set up, unreachable) every later call is skipped and answers None."""

    def __init__(self, request: Request, conn: sqlite3.Connection, error: str | None) -> None:
        self._request = request
        self._conn = conn
        self._plex: PlexWatch | None = None
        self._spent = 0
        self.error = error

    @property
    def exhausted(self) -> bool:
        return self._spent >= _PLEX_LOOKUP_BUDGET

    def _open(self, cost: int) -> PlexWatch | None:
        """Charge *cost* Plex calls and return the client; None when Plex is out of reach or
        the budget is spent."""
        if self.error is not None or self.exhausted:
            return None
        self._spent += cost
        if self._plex is None:
            try:
                self._plex = open_plex(self._request, self._conn)
            except PlexError as exc:
                self.error = plex_error_code(exc)
                return None
        return self._plex

    def call(
        self, fn: Callable[Concatenate[PlexWatch, P], T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        plex = self._open(1)
        if plex is None:
            return None
        try:
            return fn(plex, *args, **kwargs)
        except PlexItemNotFound:
            return None
        except PlexError as exc:
            self.error = plex_error_code(exc)
            return None

    def next_episode(self, rating_key: str) -> PlexEpisode | None:
        """The episode after *rating_key*, reused for a few minutes; a cached answer costs nothing."""
        now = time.monotonic()
        hit = _next_episode_cache.get(rating_key)
        if hit is not None and hit[0] > now:
            return hit[1]
        plex = self._open(_NEXT_EPISODE_COST)
        if plex is None:
            return None
        try:
            episode = plex.next_episode(rating_key)
        except PlexItemNotFound:
            episode = None
        except PlexError as exc:
            self.error = plex_error_code(exc)
            return None
        if len(_next_episode_cache) >= _NEXT_EPISODE_CACHE_MAX:
            _next_episode_cache.clear()
        _next_episode_cache[rating_key] = (now + _NEXT_EPISODE_TTL_S, episode)
        return episode

    def check_reachable(self) -> None:
        """When the page made no Plex call, ping Plex so one that is down is still reported.
        Only a good answer is reused (for 30 s), so Try again sees a Plex that came back."""
        if self.error is not None or self._spent:
            return
        now = time.monotonic()
        if _reachable_until[0] > now:
            return
        self.call(lambda plex: plex.ping())
        if self.error is None:
            _reachable_until[0] = now + _REACHABLE_TTL_S


def _added(value: str) -> datetime:
    return parse_iso_strict_utc(value) or datetime.min.replace(tzinfo=UTC)


def _continue_watching(
    conn: sqlite3.Connection,
    username: str,
    plex: _OptionalPlex,
    movies: dict[str, lib.LibraryMovie],
    shows: dict[str, lib.LibraryShow],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """This person's Continue watching cards, and the banner for the most recent item in progress."""
    cards: list[dict[str, Any]] = []
    banner: dict[str, Any] | None = None
    for entry in continue_watching(conn, username):
        if len(cards) >= _CONTINUE_LIMIT:
            break
        progress = entry.progress
        key = progress.rating_key
        if progress.show_rating_key is None:
            movie = movies.get(key)
            if movie is None:
                continue
            pct, left = progress_fields(progress)
            left_text = f"{duration_text(left)} left" if left else ""
            href = play_href(key, resume=True, origin="/watch")
            cards.append(
                card(
                    "movie",
                    movie.title,
                    " · ".join(p for p in ("Movie", left_text) if p),
                    poster_url(movie.key),
                    href,
                    progress_pct=pct,
                    minutes_left=left,
                )
            )
            if banner is None:
                banner = _banner(movie.title, left_text, pct, href, movie.key)
            continue

        show = shows.get(progress.show_rating_key)
        if show is None:
            continue
        if entry.find_next:
            watched = show_progress(conn, username, show.key)
            episode = _next_unwatched(plex.next_episode, key, watched)
            if episode is not None:
                cards.append(
                    card(
                        "show",
                        show.title,
                        episode_label(episode.season_index, episode.index),
                        poster_url(show.key),
                        play_href(episode.rating_key, origin="/watch"),
                    )
                )
            continue

        item = plex.call(_get_item, key)
        if item is None and plex.error is None and not plex.exhausted:
            continue  # the episode is gone from Plex
        pct, left = progress_fields(progress)
        left_text = f"{duration_text(left)} left" if left else ""
        label = episode_label(item.parent_index, item.index) if item else ""
        cards.append(
            card(
                "show",
                show.title,
                " · ".join(p for p in (label, left_text) if p),
                poster_url(show.key),
                play_href(key, resume=True, origin="/watch"),
                progress_pct=pct,
                minutes_left=left,
            )
        )
        if banner is None:
            if item is not None:
                line = f"Season {item.parent_index} · Episode {item.index}"
                if item.title:
                    line += f' — "{item.title}"'
                line = " · ".join(p for p in (line, left_text) if p)
                line_short = " · ".join(p for p in (label, left_text) if p)
                episodes_href = f"/watch/show/{show.key}?season={item.parent_index}"
            else:
                line = line_short = left_text
                episodes_href = f"/watch/show/{show.key}"
            banner = _banner(
                show.title, line, pct, play_href(key, resume=True, origin="/watch"), show.key
            )
            banner["line_short"] = line_short
            banner["episodes_href"] = episodes_href
    return cards, banner


def _banner(title: str, line: str, pct: int | None, href: str, poster_key: str) -> dict[str, Any]:
    return {
        "title": title,
        "line": line,
        "progress_pct": pct,
        "primary": {"label": "Resume", "href": href},
        "episodes_href": None,
        "backdrop_url": poster_url(poster_key),
    }


def _leaving_card(pending: lib.PendingDeletion, now: datetime) -> dict[str, Any]:
    leaving = leaving_info(pending, now)
    if pending.season_number is None:
        key = pending.media_item_id
        return card(
            "movie", pending.title, "Movie", poster_url(key), f"/watch/movie/{key}", leaving=leaving
        )
    show_key = pending.show_key or pending.media_item_id
    return card(
        "season",
        pending.title,
        f"Season {pending.season_number}",
        poster_url(show_key),
        f"/watch/show/{show_key}?season={pending.season_number}",
        leaving=leaving,
    )


@router.get("/watch", response_class=HTMLResponse)
def watch_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    libraries = lib.scanned_library_ids(conn)
    now = now_utc()
    set_up = plex_is_configured(conn)
    plex = _OptionalPlex(request, conn, None if set_up else "not_set_up")

    movies = lib.library_movies(conn, libraries)
    shows = lib.library_shows(conn, libraries)
    pending = lib.pending_deletions(conn, libraries, now)
    movie_row = movie_cards(conn, username, movies, pending, now)
    show_row = [show_card(s) for s in shows]

    continue_row, banner = _continue_watching(
        conn, username, plex, {m.key: m for m in movies}, {s.key: s for s in shows}
    )
    plex.check_reachable()
    leaving_row = [_leaving_card(p, now) for p in pending]

    # (added, card, where the banner's Play goes): a movie plays, a show opens its page.
    newest = sorted(
        [
            (_added(m.added_at), c, play_href(m.key, origin="/watch"))
            for m, c in zip(movies, movie_row, strict=True)
        ]
        + [(_added(s.added_at), c, c["href"]) for s, c in zip(shows, show_row, strict=True)],
        key=lambda entry: entry[0],
        reverse=True,
    )
    cutoff = now - timedelta(days=RECENT_DAYS)
    recent_row = [c for added, c, _ in newest if added >= cutoff]
    if banner is None and newest:
        _, latest, play = newest[0]
        banner = {
            "title": latest["title"],
            "line": latest["subtitle"],
            "progress_pct": None,
            "primary": {"label": "Play", "href": play},
            "episodes_href": None,
            "backdrop_url": latest["poster_url"],
        }

    rows = [
        ("Continue watching", continue_row),
        ("Leaving soon", leaving_row),
        ("Recently added", recent_row),
        ("Movies", movie_row),
        ("TV", [c for s, c in zip(shows, show_row, strict=True) if not s.anime]),
        ("Anime", [c for s, c in zip(shows, show_row, strict=True) if s.anime]),
    ]
    return _render(
        request,
        "watch.html",
        {
            "username": username,
            "banner": banner,
            "rows": [{"title": title, "items": items} for title, items in rows if items],
            # Not set up blanks the page (nothing can play); a failure part way only drops
            # what came from Plex and shows a notice above the rows.
            "watch_error": None if set_up else "not_set_up",
            "plex_unreachable": set_up and plex.error is not None,
            "is_admin": _is_admin(conn, username),
        },
    )


def _show_meta_line(listed: lib.LibraryShow, show: PlexShow) -> str:
    parts = ["Anime" if listed.anime else "TV"]
    if show.year_first and show.year_last and show.year_last != show.year_first:
        parts.append(f"{show.year_first}–{show.year_last}")
    elif show.year_first:
        parts.append(str(show.year_first))
    if show.seasons:
        count = len(show.seasons)
        parts.append("1 season" if count == 1 else f"{count} seasons")
    if show.genre:
        parts.append(show.genre)
    return " · ".join(parts)


def _primary_episode(
    plex: PlexWatch,
    show: PlexShow,
    progress: dict[str, WatchProgress],
    episodes_of: Callable[[str], list[PlexEpisode]],
    origin: str,
) -> tuple[dict[str, str], int | None]:
    """The show page's main button, and the season of the episode it plays."""
    last = max(progress.values(), key=lambda p: p.updated_at, default=None)
    if last is not None:
        try:
            if not last.watched:
                item = plex.get_item(last.rating_key)
                label = episode_label(item.parent_index, item.index)
                if resumable(last):
                    href = play_href(last.rating_key, resume=True, origin=origin)
                    return {"label": f"Resume {label}", "href": href}, item.parent_index
                return {
                    "label": f"Play {label}",
                    "href": play_href(last.rating_key, origin=origin),
                }, (item.parent_index)
            episode = _next_unwatched(plex.next_episode, last.rating_key, progress)
            if episode is not None:
                label = episode_label(episode.season_index, episode.index)
                return {
                    "label": f"Play {label}",
                    "href": play_href(episode.rating_key, origin=origin),
                }, (episode.season_index)
        except PlexItemNotFound:
            pass  # the last-played episode is gone; start from the beginning
    for season in show.seasons:
        episodes = episodes_of(season.rating_key)
        if episodes:
            label = episode_label(season.index, episodes[0].index)
            return {
                "label": f"Play {label}",
                "href": play_href(episodes[0].rating_key, origin=origin),
            }, (season.index)
    return {"label": "Play", "href": f"/watch/show/{show.rating_key}"}, None


def _episode_row(
    episode: PlexEpisode, progress: WatchProgress | None, origin: str
) -> dict[str, Any]:
    if progress is not None and progress.watched:
        state = "watched"
    elif resumable(progress):
        state = "in_progress"
    else:
        state = "none"
    pct, left = progress_fields(progress) if state == "in_progress" and progress else (None, None)
    return {
        "number": episode.index,
        "title": episode.title,
        "minutes": _minutes(episode.duration_s),
        "thumb_url": f"/api/watch/thumb/{episode.rating_key}",
        "state": state,
        "progress_pct": pct,
        "minutes_left": left,
        "href": play_href(episode.rating_key, resume=state == "in_progress", origin=origin),
    }


@router.get("/watch/show/{show_key}", response_class=HTMLResponse)
def show_page(request: Request, show_key: str, season: int | None = None) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    libraries = lib.scanned_library_ids(conn)
    listed = lib.find_show(conn, libraries, show_key) if is_valid_key(show_key) else None
    if listed is None:
        raise HTTPException(status_code=404)
    ctx: dict[str, Any] = {
        "username": username,
        "is_admin": _is_admin(conn, username),
        "back_href": "/watch",
        "watch_error": None,
        "not_available": False,
        "show": None,
        "primary": None,
        "seasons": [],
        "leaving": None,
        "season_leaving": False,
        "other_leaving": None,
        "episodes": [],
    }
    episodes_cache: dict[str, list[PlexEpisode]] = {}
    try:
        plex = open_plex(request, conn)

        def episodes_of(season_key: str) -> list[PlexEpisode]:
            if season_key not in episodes_cache:
                episodes_cache[season_key] = plex.get_season_episodes(season_key)
            return episodes_cache[season_key]

        show = plex.get_show(show_key)
        if not is_in_libraries(show, libraries):
            raise HTTPException(status_code=404)
        progress = show_progress(conn, username, show_key)
        primary, primary_season = _primary_episode(
            plex, show, progress, episodes_of, f"/watch/show/{show_key}"
        )
        wanted = season if season is not None else primary_season
        selected = next((s for s in show.seasons if s.index == wanted), None)
        if selected is None and show.seasons:
            selected = show.seasons[0]
        episodes = episodes_of(selected.rating_key) if selected else []
    except PlexItemNotFound:
        ctx["not_available"] = True
        return _render(request, "watch_show.html", ctx)
    except PlexError as exc:
        ctx["watch_error"] = plex_error_code(exc)
        return _render(request, "watch_show.html", ctx)

    leaving = None
    now = now_utc()
    # Any leaving season reserves the line's slot, so a switch to one never moves the controls.
    seasons_leaving = [
        p
        for p in lib.pending_deletions(conn, libraries, now)
        if p.show_key == show_key and p.season_number is not None
    ]
    if selected is not None:
        season_id = lib.season_item_id(conn, libraries, show_key, selected.index)
        if season_id is not None:
            leaving = leaving_info(lib.pending_deletion(conn, libraries, season_id, now), now)
    # On a season that is not leaving, the slot names the soonest one that is.
    other_leaving = None
    if leaving is None:
        other = next(
            (p for p in seasons_leaving if selected is None or p.season_number != selected.index),
            None,
        )
        if other is not None:
            ends = parse_execute_at(other.execute_at, default=now)
            other_leaving = {"season": other.season_number, "days": days_left(ends, now)}
    season_origin = f"/watch/show/{show_key}" + (f"?season={selected.index}" if selected else "")
    ctx.update(
        {
            "show": {
                "title": show.title or listed.title,
                "meta_line": _show_meta_line(listed, show),
                "summary": show.summary,
                "poster_url": poster_url(show_key),
            },
            "primary": primary,
            "seasons": [
                {
                    "number": s.index,
                    "href": f"/watch/show/{show_key}?season={s.index}",
                    "selected": s is selected,
                }
                for s in show.seasons
            ],
            "leaving": leaving,
            "season_leaving": bool(seasons_leaving),
            "other_leaving": other_leaving,
            "episodes": [
                _episode_row(e, progress.get(e.rating_key), season_origin) for e in episodes
            ],
        }
    )
    return _render(request, "watch_show.html", ctx)


def _movie_ctx(username: str, is_admin: bool) -> dict[str, Any]:
    return {
        "username": username,
        "is_admin": is_admin,
        "back_href": "/watch",
        "watch_error": None,
        "not_available": False,
        "movie": None,
        "primary": None,
        "leaving": None,
    }


@router.get("/watch/movie/{key}", response_class=HTMLResponse)
def movie_page(request: Request, key: str) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    libraries = lib.scanned_library_ids(conn)
    if not is_valid_key(key) or lib.find_movie(conn, libraries, key) is None:
        raise HTTPException(status_code=404)
    ctx = _movie_ctx(username, _is_admin(conn, username))
    try:
        item = open_plex(request, conn).get_item(key)
    except PlexItemNotFound:
        ctx["not_available"] = True
        return _render(request, "watch_movie.html", ctx)
    except PlexError as exc:
        ctx["watch_error"] = plex_error_code(exc)
        return _render(request, "watch_movie.html", ctx)
    if item.type != "movie" or not is_in_libraries(item, libraries):
        raise HTTPException(status_code=404)

    progress = get_progress(conn, username, [key]).get(key)
    if progress is not None and resumable(progress):
        _, left = progress_fields(progress)
        primary = {"label": f"Resume ({left} min left)", "href": play_href(key, resume=True)}
    else:
        primary = {"label": "Play", "href": play_href(key)}
    now = now_utc()
    ctx.update(
        {
            "movie": {
                "title": item.title,
                "year": item.year,
                "minutes": _minutes(item.duration_s),
                "summary": item.summary,
                "poster_url": poster_url(key),
            },
            "primary": primary,
            "leaving": leaving_info(lib.pending_deletion(conn, libraries, key, now), now),
        }
    )
    return _render(request, "watch_movie.html", ctx)


@router.get("/watch/play/{rating_key}", response_class=HTMLResponse)
def play_page(
    request: Request,
    rating_key: str,
    resume: str = "",
    origin: str | None = Query(None, alias="from"),
) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    if not is_valid_key(rating_key):
        raise HTTPException(status_code=404)
    libraries = lib.scanned_library_ids(conn)
    try:
        plex = open_plex(request, conn)
        item = plex.get_item(rating_key)
    except PlexError as exc:
        # The player has no state of its own for these; a plain page says it.
        ctx = {
            "username": username,
            "is_admin": _is_admin(conn, username),
            "back_href": "/watch",
            "watch_error": None if isinstance(exc, PlexItemNotFound) else plex_error_code(exc),
        }
        return _render(request, "watch_unavailable.html", ctx)
    if item.type not in ("movie", "episode") or not is_in_libraries(item, libraries):
        raise HTTPException(status_code=404)

    origin = safe_origin(origin)
    now = now_utc()
    next_episode: dict[str, str] | None = None
    show_href: str | None = None
    if item.type == "movie":
        if lib.find_movie(conn, libraries, rating_key) is None:
            raise HTTPException(status_code=404)
        pending = lib.pending_deletion(conn, libraries, rating_key, now)
        title, subtitle = item.title, (str(item.year) if item.year else None)
        back_href = f"/watch/movie/{rating_key}"
    else:
        show_key = item.grandparent_rating_key or ""
        listed = lib.find_show(conn, libraries, show_key)
        if listed is None:
            raise HTTPException(status_code=404)
        season_id = (
            lib.season_item_id(conn, libraries, show_key, item.parent_index)
            if item.parent_index is not None
            else None
        )
        pending = lib.pending_deletion(conn, libraries, season_id, now) if season_id else None
        title = item.grandparent_title or listed.title
        subtitle = f"{episode_label(item.parent_index, item.index)} — {item.title}"
        show_href = f"/watch/show/{show_key}"
        if item.parent_index is not None:
            show_href += f"?season={item.parent_index}"
        back_href = show_href
        try:
            following = plex.next_episode(rating_key)
        except PlexError:
            following = None
        if following is not None:
            label = episode_label(following.season_index, following.index)
            next_episode = {
                "href": play_href(following.rating_key, origin=_origin_for(origin, following)),
                "label": f"{label} — {following.title}" if following.title else label,
            }

    back_href = origin or back_href
    progress = get_progress(conn, username, [rating_key]).get(rating_key)
    start_s = (
        progress.position_s
        if resume == "1" and progress is not None and not progress.watched
        else 0
    )
    return _render(
        request,
        "watch_player.html",
        {
            "username": username,
            "item": {
                "rating_key": rating_key,
                "title": title,
                "subtitle": subtitle,
                "duration_s": item.duration_s,
            },
            "start_s": start_s,
            "back_href": back_href,
            "next": next_episode,
            "show_href": show_href,
            "leaving": leaving_info(pending, now),
        },
    )


__all__ = ["router"]
