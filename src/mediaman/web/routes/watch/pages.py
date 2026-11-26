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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Concatenate, ParamSpec, TypeVar, cast
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.services import watch_catalog as catalog
from mediaman.services.arr import build as arr_build
from mediaman.services.downloads import strip
from mediaman.services.infra import allowed_outbound_hosts, get_bool_setting, get_string_setting
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.services.media_meta.plex_quality import quality_rows
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexItem,
    PlexItemNotFound,
    PlexNotConfigured,
    PlexSeason,
    PlexShow,
    PlexWatch,
    is_in_libraries,
)
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.scheduled_actions import parse_execute_at
from mediaman.services.scheduled_actions.days_left import days_left
from mediaman.services.tv.players import get_players
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import seen_titles
from mediaman.web.repository import tv as tvs
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.asked import has_any
from mediaman.web.repository.library_query import count_titles
from mediaman.web.repository.watch_progress import (
    MIN_RESUME_SECONDS,
    WatchProgress,
    continue_watching,
    get_progress,
    show_progress,
)
from mediaman.web.routes import older_play
from mediaman.web.routes.downloads_strip import payload_for
from mediaman.web.routes.poster.title_art import title_art
from mediaman.web.routes.watch import all_titles, list_page
from mediaman.web.routes.watch.card_marks import title_key, tmdb_ref
from mediaman.web.routes.watch.library_order import library_page

router = APIRouter()

T = TypeVar("T")
P = ParamSpec("P")

# Items added within this many days make up the Recently added row.
RECENT_DAYS = 30
# Titles of a library row the page draws at first; the rest come from the row's own route.
ROW_PAGE_SIZE = 30

# How many a library row shows at first when it is opened as a grid (?all=).
GRID_PAGE_SIZE = 60

# All titles, which the rows of the library open too, each with its own filter.
_LIBRARY_TITLE = "All titles"
_LIBRARY_GRIDS = ("library", "movies", "tv", "anime")

# The rows that load after the page, so do their grids.
_RECOMMENDED_ROW = ("recommended", "Recommended for you")
_ASKED_ROW = ("asked", "You asked for these")
# The Continue watching row stops filling at this many cards, and a page load spends at most
# this many Plex HTTP calls on it however long the person's history is.
_CONTINUE_LIMIT = 20
_PLEX_LOOKUP_BUDGET = 60
# next_episode() makes up to four Plex calls (the episode, its season, the show's seasons, the
# next season), get_show() three; each is charged the worst case.
_NEXT_EPISODE_COST = 4
_SHOW_COST = 3
# How long an answer about what follows an episode is reused, so a finished show does not cost
# Plex calls on every load while a newly added episode still shows up within minutes.
_NEXT_EPISODE_TTL_S = 300.0
_NEXT_EPISODE_CACHE_MAX = 500
# Next episodes by episode key, seasons of shows and episodes of seasons by prefixed key.
_next_episode_cache: dict[str, tuple[float, Any]] = {}
# How long a good reachability answer is reused when a page load makes no other Plex call.
_REACHABLE_TTL_S = 30.0
_reachable_until = [0.0]
_KEY_RE = re.compile(r"[0-9]{1,18}")
# The only places the player's Back may return to; anything else falls back to the item's own page.
# Watch's old address is still read, as a way back saved before Watch moved to the root.
_ORIGIN_RE = re.compile(
    r"/(?P<old>watch)?(\?(?P<query>[^#]*))?"
    r"(?P<hash>#title=(movie-[0-9]{1,18}|show-[0-9]{1,18}(&season=[0-9]{1,4})?"
    r"|tmdb-(movie|tv)-[0-9]{1,10}|(rec|pick)-[0-9]{1,10}))?"
    r"|/watch/show/[0-9]{1,18}(\?season=[0-9]{1,4})?|/watch/movie/[0-9]{1,18}"
)
_SEASON_RE = re.compile(r"([?&]season=)[0-9]+$")
# What a Watch page's own address may carry, and how each value looks.
_ORIGIN_PARAMS = {
    "all": r"[a-z_]{1,40}",
    "view": r"list",
    "genre": r"[a-z0-9-]{1,40}",
    "person": r"[0-9]{1,18}",
    "like": r"(movie|show)-[0-9]{1,18}|tmdb-(movie|tv)-[0-9]{1,10}",
    "type": r"[a-z_]{1,20}",
    "sort": r"[a-z_]{1,20}",
    "where": r"[a-z]{1,20}",
    "decade": r"(19|20)[0-9]0s",
    "page": r"[0-9]{1,9}",
    "per_page": r"[0-9]{1,9}",
}
_ORIGIN_FIELDS_MAX = 50
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# A search typed into the page's field is never this long; a longer ?q= is cut.
_QUERY_MAX = 200


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


def can_download(conn: sqlite3.Connection, secret_key: str) -> dict[str, bool]:
    """Whether films and shows can be downloaded: Radarr fetches the one and Sonarr the other."""
    return {
        "movie": arr_build.build_radarr_from_db(conn, secret_key) is not None,
        "show": arr_build.build_sonarr_from_db(conn, secret_key) is not None,
    }


def safe_origin(value: str | None) -> str | None:
    """*value* when it is one of the Watch pages a person can come from, without what such a
    page's address does not carry; else None."""
    match = _ORIGIN_RE.fullmatch(value) if value else None
    if value is None or match is None:
        return None
    if match["query"] is None:
        return "/" + value[len("/watch") :] if match["old"] else value
    try:
        pairs = parse_qsl(match["query"], max_num_fields=_ORIGIN_FIELDS_MAX)
    except ValueError:
        return None
    kept: dict[str, str] = {}
    for key, given in pairs:
        if _CONTROL_RE.search(key + given):
            return None
        # Cut as the page cuts it; of two of a kind the page reads the first.
        value = given[:_QUERY_MAX] if key == "q" else given
        if key not in kept and (
            key == "q" or re.fullmatch(_ORIGIN_PARAMS.get(key, r"(?!)"), value)
        ):
            kept[key] = value
    query = f"?{urlencode(kept)}" if kept else ""
    return f"/{query}{match['hash'] or ''}"


def _banner_art(key: str) -> dict[str, str]:
    """The banner's pictures, in the order a title's details try them: its own backdrop, then
    TMDB's, then the poster as a poster."""
    art = title_art(key)
    return {**art, "next_backdrop_url": f"{art['backdrop_url']}&source=tmdb"}


def play_href(rating_key: str, *, resume: bool = False, origin: str | None = None) -> str:
    params = ["resume=1"] if resume else []
    if origin:
        params.append(f"from={quote(origin, safe='')}")
    return f"/watch/play/{rating_key}" + (f"?{'&'.join(params)}" if params else "")


def _origin_for(origin: str | None, episode: PlexEpisode) -> str | None:
    """*origin*, with a show page's season changed to the one *episode* is in."""
    if origin and episode.season_index is not None:
        return _SEASON_RE.sub(rf"\g<1>{episode.season_index}", origin)
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


def seasons_leaving(
    pending: Sequence[lib.PendingDeletion], now: datetime
) -> dict[str, list[dict[str, Any]]]:
    """By show key, the seasons of the show that are leaving, soonest first as *pending* is."""
    found: dict[str, list[dict[str, Any]]] = {}
    for p in pending:
        info = leaving_info(p, now)
        if info is not None and p.show_key is not None and p.season_number is not None:
            season = {"season": p.season_number, "days": info["days"], "id": info["keep_id"]}
            found.setdefault(p.show_key, []).append(season)
    return found


def season_fields(seasons: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What a show's card says of its leaving seasons: the first, the item whose Keep ends it,
    and them all, for the next one once that is kept."""
    if not seasons:
        return {}
    first = seasons[0]
    return {
        "leaving_season": first["season"],
        "leaving_id": first["id"],
        "leaving_seasons": seasons,
    }


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
    season_leaving: list[dict[str, Any]] | None = None,
    mark: tuple[str, str] | None = None,
    dismiss_ref: str | None = None,
    title_fields: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """One card; *mark* is the (kind, key) its Watched toggle marks, *season_leaving* the
    seasons of a show that are leaving, which the card says and offers no Keep of, and
    *title_fields* is ``_title_fields`` of the film or show."""
    if season_leaving:
        leaving = {"days": season_leaving[0]["days"], "keep_id": None}
    mark_kind, mark_key = mark or (None, None)
    return {
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "poster_url": poster,
        "href": href,
        "progress_pct": progress_pct,
        "minutes_left": minutes_left,
        "watched": watched,
        "mark_kind": mark_kind,
        "mark_key": mark_key,
        "dismiss_ref": dismiss_ref,
        **(title_fields or {"tmdb_ref": None, "level": None}),
        "leaving_days": leaving["days"] if leaving else None,
        "keep_id": leaving["keep_id"] if leaving else None,
        **season_fields(season_leaving),
    }


def _title_fields(
    marks: seen_titles.PersonMarks, title: lib.LibraryMovie | lib.LibraryShow | None
) -> dict[str, object]:
    """What a library title's card is rated by, and how this person rated it."""
    key = title_key(
        "movie" if isinstance(title, lib.LibraryMovie) else "tv", title.tmdb_id if title else None
    )
    return {"tmdb_ref": tmdb_ref(key), "level": marks.levels.get(key) if key else None}


def movie_subtitle(movie: lib.LibraryMovie) -> str:
    return f"Movie · {movie.year}" if movie.year else "Movie"


def movie_card(
    movie: lib.LibraryMovie,
    *,
    watched: bool = False,
    leaving: dict[str, Any] | None = None,
    progress_pct: int | None = None,
    title_fields: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    return card(
        "movie",
        movie.title,
        movie_subtitle(movie),
        poster_url(movie.key),
        f"/watch/movie/{movie.key}",
        watched=watched,
        leaving=leaving,
        progress_pct=progress_pct,
        mark=("movie", movie.key),
        title_fields=title_fields,
    )


def show_subtitle(show: lib.LibraryShow, seen: lib.ShowSeen | None = None) -> str:
    parts = ["Anime" if show.anime else "TV"]
    if show.season_count:
        parts.append("1 season" if show.season_count == 1 else f"{show.season_count} seasons")
    if seen is not None and seen.left:
        parts.append(f"{seen.left} left")
    return " · ".join(parts)


def show_card(
    show: lib.LibraryShow,
    seen: lib.ShowSeen | None = None,
    season_leaving: list[dict[str, Any]] | None = None,
    title_fields: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """A show's card; *seen* is how much of it this person has seen."""
    return card(
        "show",
        show.title,
        show_subtitle(show, seen),
        poster_url(show.key),
        f"/watch/show/{show.key}",
        watched=seen is not None and seen.watched,
        season_leaving=season_leaving,
        mark=("show", show.key),
        title_fields=title_fields,
    )


def show_cards(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    shows: list[lib.LibraryShow],
    pending: list[lib.PendingDeletion],
    now: datetime,
    marks: seen_titles.PersonMarks,
) -> list[dict[str, Any]]:
    """Show cards marked with what this person has seen of each and any season leaving."""
    seen = lib.show_seen(conn, username, libraries)
    leaving = seasons_leaving(pending, now)
    return [
        show_card(s, seen.get(s.key), leaving.get(s.key), _title_fields(marks, s)) for s in shows
    ]


def movie_cards(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    movies: list[lib.LibraryMovie],
    pending: list[lib.PendingDeletion],
    now: datetime,
    marks: seen_titles.PersonMarks,
) -> list[dict[str, Any]]:
    """Movie cards marked with this person's Watched state and any scheduled deletion."""
    progress = get_progress(conn, username, [m.key for m in movies])
    covered = seen_titles.seen_films(conn, username, libraries)
    leaving = {p.media_item_id: leaving_info(p, now) for p in pending if p.season_number is None}
    return [
        movie_card(
            m,
            watched=is_seen(progress, m.key) or m.key in covered,
            leaving=leaving.get(m.key),
            progress_pct=(
                progress_fields(progress[m.key])[0] if resumable(progress.get(m.key)) else None
            ),
            title_fields=_title_fields(marks, m),
        )
        for m in movies
    ]


def title_cards(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    titles: Sequence[lib.LibraryMovie | lib.LibraryShow],
    now: datetime,
) -> list[dict[str, Any]]:
    """The cards of library *titles*, in their order."""
    movies = [t for t in titles if isinstance(t, lib.LibraryMovie)]
    shows = [t for t in titles if isinstance(t, lib.LibraryShow)]
    pending = lib.pending_deletions(conn, libraries)
    marks = seen_titles.person_marks(conn, username)
    cards: dict[lib.LibraryMovie | lib.LibraryShow, dict[str, Any]] = {}
    if movies:
        films = movie_cards(conn, username, libraries, movies, pending, now, marks)
        cards.update(zip(movies, films, strict=True))
    if shows:
        held = show_cards(conn, username, libraries, shows, pending, now, marks)
        cards.update(zip(shows, held, strict=True))
    return [cards[title] for title in titles]


def _render(request: Request, template: str, ctx: dict[str, Any]) -> Response:
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(request, template, {"nav_active": "watch", **ctx})


def _is_admin(conn: sqlite3.Connection, username: str) -> bool:
    return get_user_role(conn, username) == "admin"


def is_seen(progress: Mapping[str, WatchProgress], rating_key: str) -> bool:
    row = progress.get(rating_key)
    return row is not None and row.seen


def _first_unseen(
    seasons: Sequence[PlexSeason],
    episodes_of: Callable[[str], list[PlexEpisode] | None],
    progress: Mapping[str, WatchProgress],
    after: tuple[int, int] = (0, 0),
) -> tuple[PlexSeason, PlexEpisode] | None:
    """The first episode past *after* (season and episode number) this person has not seen,
    reading one season's list at a time. None too when a season on the way cannot be read (its
    list is None): it may hold that episode."""
    for season in seasons:
        if season.index < after[0]:
            continue
        episodes = episodes_of(season.rating_key)
        if episodes is None:
            return None
        for episode in episodes:
            past = season.index > after[0] or episode.index > after[1]
            if past and not is_seen(progress, episode.rating_key):
                return season, episode
    return None


def _next_unwatched(
    plex: _OptionalPlex,
    show_key: str,
    episode_key: str,
    progress: Mapping[str, WatchProgress],
) -> PlexEpisode | None:
    """The first episode after *episode_key* this person has not seen; None if there is none or
    Plex cannot say."""
    episode = plex.next_episode(episode_key)
    if episode is None or not is_seen(progress, episode.rating_key):
        return episode
    found = _first_unseen(
        plex.seasons(show_key),
        plex.season_episodes,
        progress,
        (episode.season_index or 0, episode.index),
    )
    return found[1] if found else None


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

    def _cached(self, key: str, cost: int, fetch: Callable[[PlexWatch], T]) -> T | None:
        """*fetch*'s answer, reused for a few minutes; a cached answer costs nothing."""
        now = time.monotonic()
        hit = _next_episode_cache.get(key)
        if hit is not None and hit[0] > now:
            return cast(T | None, hit[1])
        plex = self._open(cost)
        if plex is None:
            return None
        answer: T | None
        try:
            answer = fetch(plex)
        except PlexItemNotFound:
            answer = None
        except PlexError as exc:
            self.error = plex_error_code(exc)
            return None
        if len(_next_episode_cache) >= _NEXT_EPISODE_CACHE_MAX:
            _next_episode_cache.clear()
        _next_episode_cache[key] = (now + _NEXT_EPISODE_TTL_S, answer)
        return answer

    def next_episode(self, rating_key: str) -> PlexEpisode | None:
        """The episode after *rating_key*."""
        return self._cached(rating_key, _NEXT_EPISODE_COST, lambda p: p.next_episode(rating_key))

    def seasons(self, show_key: str) -> list[PlexSeason]:
        """The show's regular seasons in order."""
        return (
            self._cached(f"show:{show_key}", _SHOW_COST, lambda p: p.get_show(show_key).seasons)
            or []
        )

    def season_episodes(self, season_key: str) -> list[PlexEpisode] | None:
        """Empty when the season is gone from Plex; None when they cannot be read now."""

        def fetch(plex: PlexWatch) -> list[PlexEpisode]:
            try:
                return plex.get_season_episodes(season_key)
            except PlexItemNotFound:
                return []

        return self._cached(f"season:{season_key}", 1, fetch)

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
    marks: seen_titles.PersonMarks,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """This person's Continue watching cards, and the banner, which stands for the first."""
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
            href = play_href(key, resume=True, origin="/")
            cards.append(
                card(
                    "movie",
                    movie.title,
                    " · ".join(p for p in ("Movie", left_text) if p),
                    poster_url(movie.key),
                    href,
                    progress_pct=pct,
                    minutes_left=left,
                    mark=("movie", movie.key),
                    dismiss_ref=f"movie-{movie.key}",
                    title_fields=_title_fields(marks, movie),
                )
            )
            if banner is None:
                banner = _banner(movie.title, left_text, pct, href, movie.key, "movie")
                _mark_banner(banner, cards[-1])
            continue

        show = shows.get(progress.show_rating_key)
        if show is None:
            continue
        if entry.find_next or (progress.seen and not resumable(progress)):
            watched = show_progress(conn, username, show.key)
            episode = _next_unwatched(plex, show.key, key, watched)
            if episode is None:
                continue
            label = episode_label(episode.season_index, episode.index)
            href = play_href(episode.rating_key, origin="/")
            cards.append(
                card(
                    "show",
                    show.title,
                    label,
                    poster_url(show.key),
                    href,
                    mark=("episode", episode.rating_key),
                    dismiss_ref=f"show-{show.key}",
                    title_fields=_title_fields(marks, show),
                )
            )
            if banner is None:
                line = _episode_line(episode.season_index, episode.index, episode.title)
                banner = _banner(show.title, line, None, href, show.key, "show")
                _mark_banner(banner, cards[-1])
                banner["primary"]["label"] = "Play"
                banner["line_short"] = label
                banner["episodes_href"] = _episodes_href(show.key, episode.season_index)
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
                play_href(key, resume=True, origin="/"),
                progress_pct=pct,
                minutes_left=left,
                mark=("episode", key),
                dismiss_ref=f"show-{show.key}",
                title_fields=_title_fields(marks, show),
            )
        )
        if banner is None:
            if item is not None:
                line = _episode_line(item.parent_index, item.index, item.title)
                line = " · ".join(p for p in (line, left_text) if p)
                line_short = " · ".join(p for p in (label, left_text) if p)
                episodes_href = _episodes_href(show.key, item.parent_index)
            else:
                line = line_short = left_text
                episodes_href = _episodes_href(show.key, None)
            banner = _banner(
                show.title,
                line,
                pct,
                play_href(key, resume=True, origin="/"),
                show.key,
                "show",
            )
            _mark_banner(banner, cards[-1])
            banner["line_short"] = line_short
            banner["episodes_href"] = episodes_href
    return cards, banner


def _episode_line(season: int | None, episode: int | None, title: str) -> str:
    line = f"Episode {episode or 0}" if season is None else f"Season {season} · Episode {episode}"
    return f'{line} — "{title}"' if title else line


def _episodes_href(show_key: str, season: int | None) -> str:
    return f"/watch/show/{show_key}" + ("" if season is None else f"?season={season}")


def _banner(
    title: str, line: str, pct: int | None, href: str, key: str, kind: str
) -> dict[str, Any]:
    """The banner for a library title; its ref opens the title's detail overlay."""
    return {
        "kicker": "Continue watching",
        "title": title,
        "ref": f"{kind}-{key}",
        "line": line,
        "progress_pct": pct,
        "primary": {"label": "Resume", "href": href},
        "episodes_href": None,
        **_banner_art(key),
    }


def _mark_banner(banner: dict[str, Any], of: dict[str, Any]) -> None:
    """Give the banner the marks of the card it stands for."""
    copied = ("watched", "mark_kind", "mark_key", "dismiss_ref")
    banner.update({k: of[k] for k in copied})


def _leaving_card(
    pending: lib.PendingDeletion,
    now: datetime,
    films: Mapping[str, lib.LibraryMovie],
    shows: Mapping[str, lib.LibraryShow],
    seen_films: set[str],
    seen_shows: Mapping[str, lib.ShowSeen],
    marks: seen_titles.PersonMarks,
) -> dict[str, Any]:
    leaving = leaving_info(pending, now)
    if pending.season_number is None:
        key = pending.media_item_id
        return card(
            "movie",
            pending.title,
            movie_subtitle(films[key]) if key in films else "Movie",
            poster_url(key),
            f"/watch/movie/{key}",
            watched=key in seen_films,
            leaving=leaving,
            mark=("movie", key),
            title_fields=_title_fields(marks, films.get(key)),
        )
    show_key = pending.show_key or pending.media_item_id
    show = seen_shows.get(show_key)
    season = show.seasons.get(pending.season_number) if show else None
    return card(
        "season",
        pending.title,
        f"Season {pending.season_number}",
        poster_url(show_key),
        f"/watch/show/{show_key}?season={pending.season_number}",
        watched=season is not None and season.watched,
        leaving=leaving,
        mark=("season", pending.media_item_id),
        title_fields=_title_fields(marks, shows.get(show_key)),
    )


def _row(row_id: str, title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """A row; *row_id* names it in ?all=."""
    return {
        "id": row_id,
        "title": title,
        "items": items,
        "library": False,
        "kind": None,
        "page_size": None,
        "next": None,
    }


def _kind_of(title: lib.LibraryMovie | lib.LibraryShow) -> str:
    return "movie" if isinstance(title, lib.LibraryMovie) else "show"


def recently_added(
    titles: Sequence[lib.LibraryMovie | lib.LibraryShow], now: datetime
) -> list[lib.LibraryMovie | lib.LibraryShow]:
    """The Recently added row: those of *titles* added of late, newest first."""
    cutoff = now - timedelta(days=RECENT_DAYS)
    return lib.newest_first([t for t in titles if _added(t.added_at) >= cutoff])


def _paged_row(
    title: str,
    kind: str,
    entries: Sequence[lib.LibraryMovie | lib.LibraryShow],
    items: list[dict[str, Any]],
    *,
    library: bool = True,
) -> dict[str, Any]:
    """A row of titles of the library, newest first, and where its first page ends."""
    more = len(entries) > ROW_PAGE_SIZE
    return {
        **_row(kind, title, items),
        "library": library,
        "kind": kind,
        "page_size": ROW_PAGE_SIZE,
        "next": lib.cursor_text(lib.row_cursor(entries[ROW_PAGE_SIZE - 1])) if more else None,
    }


def _row_grid(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    row: dict[str, Any],
    entries: Sequence[lib.LibraryMovie | lib.LibraryShow] | None,
    sort: str | None,
) -> dict[str, Any]:
    """The row *row* opened as a grid, in the order *sort* asks for if it has that one: its first
    cards, and for one that pages (with *entries*) where they end."""
    items: list[dict[str, Any]] = row["items"]
    order: lib.RowOrder = "az" if sort == "az" and row["id"] != "leaving" else "added"
    end = None
    if entries is not None:
        by_key = {e.key: c for e, c in zip(entries, items, strict=True)}
        page, end = library_page(conn, username, libraries, entries, order, GRID_PAGE_SIZE)
        items = [by_key[e.key] for e in page]
    elif order == "az":
        items = sorted(items, key=lambda c: (lib.sort_title(c["title"]), c["title"].casefold()))
    return {
        "id": row["id"],
        "title": row["title"],
        "items": items,
        "sort": order,
        "total": len(row["items"]),
        "next": end,
        "loads_later": False,
    }


@router.api_route("/watch", methods=["GET", "HEAD"])
def old_watch_address(request: Request) -> Response:
    """Watch's address before it moved to the root; the browser keeps the hash itself."""
    query = request.url.query
    return RedirectResponse("/" + (f"?{query}" if query else ""), status_code=301)


@router.get("/", response_class=HTMLResponse)
def watch_page(
    request: Request,
    view: str = "rows",
    q: str = "",
    row_id: Annotated[str, Query(alias="all")] = "",
) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    is_admin = _is_admin(conn, username)
    if view == "list":
        to = list_page.address_of(request.query_params, admin=is_admin)
        return RedirectResponse(to, status_code=302)
    q = q.strip()[:_QUERY_MAX]
    tmdb = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    tmdb_configured = tmdb is not None
    libraries = lib.scanned_library_ids(conn)
    now = now_utc()
    set_up = plex_is_configured(conn)
    plex = _OptionalPlex(request, conn, None if set_up else "not_set_up")

    movies = lib.newest_first(lib.library_movies(conn, libraries))
    shows = lib.newest_first(lib.library_shows(conn, libraries))
    pending = lib.pending_deletions(conn, libraries)
    marks = seen_titles.person_marks(conn, username)
    movie_row = movie_cards(conn, username, libraries, movies, pending, now, marks)
    seen_shows = lib.show_seen(conn, username, libraries)
    going = seasons_leaving(pending, now)
    show_row = [
        show_card(s, seen_shows.get(s.key), going.get(s.key), _title_fields(marks, s))
        for s in shows
    ]

    films = {m.key: m for m in movies}
    by_key = {s.key: s for s in shows}
    continue_row, banner = _continue_watching(conn, username, plex, films, by_key, marks)
    plex.check_reachable()
    seen_films = lib.seen_films(conn, username, libraries)
    leaving_row = [
        _leaving_card(p, now, films, by_key, seen_films, seen_shows, marks) for p in pending
    ]

    card_of = {
        **{("movie", m.key): c for m, c in zip(movies, movie_row, strict=True)},
        **{("show", s.key): c for s, c in zip(shows, show_row, strict=True)},
    }
    titles: list[lib.LibraryMovie | lib.LibraryShow] = [*movies, *shows]
    newest = lib.newest_first(titles)
    recent = recently_added(newest, now)
    recent_row = [card_of[_kind_of(t), t.key] for t in recent]
    if banner is None and newest:
        first = newest[0]
        kind, key = _kind_of(first), first.key
        latest = card_of[kind, key]
        banner = {
            # Only as true as the Recently added row: an older newest title gets a neutral label.
            "kicker": "Recently added" if recent else "From your library",
            "title": latest["title"],
            "ref": f"{kind}-{key}",
            "line": latest["subtitle"],
            "progress_pct": None,
            # A movie plays, a show opens its page.
            "primary": {
                "label": "Play",
                "href": play_href(key, origin="/") if kind == "movie" else latest["href"],
            },
            "episodes_href": None,
            **_banner_art(key),
        }
        _mark_banner(banner, latest)

    tv = [(s, c) for s, c in zip(shows, show_row, strict=True) if not s.anime]
    anime = [(s, c) for s, c in zip(shows, show_row, strict=True) if s.anime]
    # Discover goes between the rows above the "Your library" divider and those under it.
    entries: dict[str, Sequence[lib.LibraryMovie | lib.LibraryShow]] = {
        "recent": recent,
        "movies": movies,
        "tv": [s for s, _ in tv],
        "anime": [s for s, _ in anime],
    }
    rows = [
        _row("continue", "Continue watching", continue_row),
        _row("leaving", "Leaving soon", leaving_row),
        _paged_row("Recently added", "recent", recent, recent_row, library=False),
        _paged_row("Movies", "movies", movies, movie_row),
        _paged_row("TV", "tv", entries["tv"], [c for _, c in tv]),
        _paged_row("Anime", "anime", entries["anime"], [c for _, c in anime]),
    ]
    index = catalog.library_index(conn, tmdb, libraries, lib.library_movies, lib.library_shows)
    continuing = {c["dismiss_ref"] for c in continue_row}
    asked_waiting = has_any(conn, username, libraries, index, continuing)
    picks_on = get_bool_setting(conn, "suggestions_enabled", default=True)
    later = [_ASKED_ROW, *([_RECOMMENDED_ROW] if picks_on else [])] + (
        [(shelf[0], shelf[1]) for shelf in catalog.SHELVES] if tmdb_configured else []
    )
    # What ?all= may name. Not set up, the page has no row of its own to open, but an admin still
    # has what the scans found.
    grid_titles = {r["id"]: r["title"] for r in rows if set_up} | dict(later)
    if set_up or is_admin:
        grid_titles["library"] = _LIBRARY_TITLE
    grid: dict[str, Any] | None = None
    asked = request.query_params
    if not (q or asked.get("genre") or asked.get("person")):
        if row_id in dict(later):
            grid = {"id": row_id, "title": grid_titles[row_id], "items": [], "total": None}
            grid |= {"sort": None, "next": None, "loads_later": True}
        elif "library" in grid_titles and row_id in _LIBRARY_GRIDS:
            # The grid of a row of the library is All titles with that filter.
            type_ = all_titles.known_type(
                asked.get("type") if row_id == "library" else row_id, is_admin
            )
            sort = all_titles.known_sort(asked.get("sort"), is_admin)
            by_ref = {f"{kind}-{key}": c for (kind, key), c in card_of.items()}
            grid = {"id": "library", "title": _LIBRARY_TITLE, "sort": sort, "loads_later": False}
            grid |= all_titles.in_one_read(
                conn,
                lambda: all_titles.page_of(
                    conn,
                    username,
                    libraries,
                    all_titles.load(conn, username, libraries, movies, shows, admin=is_admin),
                    lambda refs: {ref: by_ref[ref] for ref in refs},
                    type_=type_,
                    sort=sort,
                    limit=GRID_PAGE_SIZE,
                    now=now,
                ),
            )
        elif row_id in grid_titles:
            opened = next(r for r in rows if r["id"] == row_id)
            grid = _row_grid(
                conn, username, libraries, opened, entries.get(row_id), asked.get("sort")
            )
    downloading = strip.shared.last()
    library_total = sum(count_titles(conn).values()) if is_admin else len(titles)
    return _render(
        request,
        "watch.html",
        {
            "username": username,
            "banner": banner,
            "rows": [row for row in rows if row["items"]],
            # Not set up blanks the browse rows (nothing can play); a failure part way only
            # drops what came from Plex and shows a notice above the rows.
            "watch_error": None if set_up else "not_set_up",
            "plex_unreachable": set_up and plex.error is not None,
            "is_admin": is_admin,
            "q": q,
            "tmdb_configured": tmdb_configured,
            "picks_on": picks_on,
            "asked_waiting": set_up and asked_waiting,
            "grid": grid,
            "library_types": [t for t in all_titles.TYPES if is_admin or not t[2]],
            "library_total": library_total,
            "page_data": {
                "is_admin": is_admin,
                "tmdb_configured": tmdb_configured,
                "can_download": can_download(conn, request.app.state.config.secret_key),
                "q": q,
                "grids": grid_titles,
                "library_total": library_total,
                "grid": {k: grid.get(k) for k in ("id", "total", "next", "counts", "fact", "empty")}
                if grid
                else None,
                "settings_tmdb_href": "/settings#setg-integrations" if is_admin else None,
                "downloading": payload_for(
                    conn, request.app.state.config.secret_key, username, downloading
                )
                if downloading
                else None,
            },
        },
    )


def _primary_episode(
    plex: PlexWatch,
    show: PlexShow,
    progress: dict[str, WatchProgress],
    episodes_of: Callable[[str], list[PlexEpisode]],
    origin: str,
    counted: lib.ShowSeen | None = None,
) -> tuple[dict[str, str], int | None]:
    """The show page's main button, and the season of the episode it plays. *counted* saves
    reading the seasons it says are seen when looking back from the start."""
    last = max((p for p in progress.values() if p.played), key=lambda p: p.updated_at, default=None)
    found: tuple[PlexSeason, PlexEpisode] | None = None
    if last is not None:
        try:
            item = plex.get_item(last.rating_key)
            if resumable(last) or not last.seen:
                label = episode_label(item.parent_index, item.index)
                if resumable(last):
                    href = play_href(last.rating_key, resume=True, origin=origin)
                    return {"label": f"Resume {label}", "href": href}, item.parent_index
                return {
                    "label": f"Play {label}",
                    "href": play_href(last.rating_key, origin=origin),
                }, (item.parent_index)
            if item.parent_index:
                found = _first_unseen(
                    show.seasons, episodes_of, progress, (item.parent_index, item.index or 0)
                )
        except PlexItemNotFound:
            pass  # the last-played episode is gone; start from the beginning
    if found is None:
        # With every episode seen it starts over at the first.
        unread = [
            s
            for s in show.seasons
            if counted is None
            or s.index not in counted.seasons
            or not counted.seasons[s.index].watched
        ]
        found = _first_unseen(unread, episodes_of, progress) or _first_unseen(
            show.seasons, episodes_of, {}
        )
    if found is not None:
        season, episode = found
        return {
            "label": f"Play {episode_label(season.index, episode.index)}",
            "href": play_href(episode.rating_key, origin=origin),
        }, season.index
    return {"label": "Play", "href": f"/watch/show/{show.rating_key}"}, None


def _episode_row(
    episode: PlexEpisode, progress: WatchProgress | None, origin: str
) -> dict[str, Any]:
    seen = progress is not None and progress.seen
    state = "in_progress" if resumable(progress) else "watched" if seen else "none"
    pct, left = progress_fields(progress) if state == "in_progress" and progress else (None, None)
    return {
        "number": episode.index,
        "title": episode.title,
        "minutes": _minutes(episode.duration_s),
        "thumb_url": f"/api/watch/thumb/{episode.rating_key}",
        "key": episode.rating_key,
        "state": state,
        "watched": seen,
        "progress_pct": pct,
        "minutes_left": left,
        "href": play_href(episode.rating_key, resume=state == "in_progress", origin=origin),
    }


def _season_watched(
    episodes: list[PlexEpisode] | None,
    progress: Mapping[str, WatchProgress],
    counted: lib.ShowSeen | None,
    number: int,
) -> bool:
    """Whether every episode of a season is seen, by its own list when the page read it, else
    by the last scan's count."""
    if episodes is not None:
        return bool(episodes) and all(is_seen(progress, e.rating_key) for e in episodes)
    season = counted.seasons.get(number) if counted else None
    return season is not None and season.watched


def _title_address(ref: str, season: int | None = None) -> str:
    """Where Watch opens the title *ref*, on *season* when it names one."""
    named = season is not None and 0 <= season <= 9999
    return f"/#title={ref}" + (f"&season={season}" if named else "")


@router.get("/watch/show/{show_key}")
def show_page(request: Request, show_key: str, season: int | None = None) -> Response:
    """A show's address of before: its page on Watch, on the season it names."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    if not is_valid_key(show_key):
        raise HTTPException(status_code=404)
    return RedirectResponse(_title_address(f"show-{show_key}", season))


@router.get("/watch/movie/{key}")
def movie_page(request: Request, key: str) -> Response:
    """A film's address of before: its page on Watch."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    if not is_valid_key(key):
        raise HTTPException(status_code=404)
    return RedirectResponse(_title_address(f"movie-{key}"))


def _is_on_a_tv(conn: sqlite3.Connection, username: str, rating_key: str) -> bool:
    """Whether a TV plays the title for the person: a start anywhere else would end it there."""
    players = get_players()
    theirs = tvs.owned(conn, username) + older_play.everyones(conn, username)
    return any(held.token_hash and players.plays(held.token_hash, rating_key) for held in theirs)


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
            "back_href": safe_origin(origin) or "/",
            "watch_error": None if isinstance(exc, PlexItemNotFound) else plex_error_code(exc),
        }
        return _render(request, "watch_unavailable.html", ctx)
    if item.type not in ("movie", "episode") or not is_in_libraries(item, libraries):
        raise HTTPException(status_code=404)

    origin = safe_origin(origin)
    now = now_utc()
    next_episode: dict[str, str] | None = None
    next_state = "end"
    show_href: str | None = None
    if item.type == "movie":
        if lib.find_movie(conn, libraries, rating_key) is None:
            raise HTTPException(status_code=404)
        pending = lib.pending_deletion(conn, libraries, rating_key)
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
        pending = lib.pending_deletion(conn, libraries, season_id) if season_id else None
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
            next_state = "unknown"
        if following is not None:
            label = episode_label(following.season_index, following.index)
            next_state = "next"
            next_episode = {
                "href": play_href(following.rating_key, origin=_origin_for(origin, following)),
                "label": f"{label} — {following.title}" if following.title else label,
            }

    back_href = origin or back_href
    progress = get_progress(conn, username, [rating_key]).get(rating_key)
    # A reload or Back reopens the bare address and must not restart the video and overwrite
    # the place already saved.
    start_s = (
        progress.position_s
        if progress is not None and not progress.watched and (resume == "1" or resumable(progress))
        else 0
    )
    # Where the title plays somewhere else it is gone on with from there, however far that is.
    place_s = progress.position_s if progress is not None and progress.played else None
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
            "qualities": quality_rows(item),
            "start_s": start_s,
            "place_s": place_s,
            "on_tv": _is_on_a_tv(conn, username, rating_key),
            "back_href": back_href,
            "next": next_episode,
            "next_state": next_state,
            "show_href": show_href,
            "leaving": leaving_info(pending, now),
        },
    )


__all__ = ["router"]
