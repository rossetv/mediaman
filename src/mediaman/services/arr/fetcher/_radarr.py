from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from mediaman.services.arr.fetcher._base import (
    ArrCard,
    _iter_still_searching,
    clamp_progress,
    film_id,
    make_arr_card,
)

if TYPE_CHECKING:
    from mediaman.services.arr._types import RadarrMovie
    from mediaman.services.arr.base import ArrClient
from mediaman.core.time import parse_iso_utc
from mediaman.services.downloads.download_format import (
    classify_movie_upcoming,
    compute_movie_released_at,
    extract_poster_url,
)


def _identify(card: ArrCard, tmdb_id: int | None) -> None:
    """Give a card the id of its film, where Radarr told which that is."""
    if tmdb_id:
        card["tmdb_id"] = tmdb_id
        card["dl_id"] = film_id(card["title"], tmdb_id)


def _build_queue_cards(client: ArrClient) -> list[ArrCard]:
    items: list[ArrCard] = []
    for q in client.get_queue():
        movie = q.get("movie") or {}
        size = q.get("size") or 0
        sizeleft = max(0, q.get("sizeleft") or 0)
        # Clamp to [0, 100]: Radarr can briefly report ``sizeleft > size`` while
        # a torrent re-downloads or pads, which would otherwise produce a
        # negative percentage and break progress bars.
        progress = clamp_progress(size, sizeleft)
        status = q.get("status") or q.get("trackedDownloadStatus") or "queued"
        poster_url = extract_poster_url(movie.get("images"))
        m_title = movie.get("title") or q.get("title") or "Unknown"
        release_name = q.get("title") or ""
        card = make_arr_card(
            "movie",
            m_title,
            source="Radarr",
            year=movie.get("year"),
            poster_url=poster_url,
            progress=progress,
            size=size,
            sizeleft=sizeleft,
            timeleft=q.get("timeleft") or "",
            status=status,
            release_names=[release_name] if release_name else [],
        )
        _identify(card, movie.get("tmdbId"))
        items.append(card)
    return items


def _build_still_searching_cards(
    movies: Sequence[RadarrMovie],
    queue_cards: Sequence[ArrCard],
) -> list[ArrCard]:
    """Phase 2: build cards for monitored movies still searching, those of *queue_cards* left
    out: by TMDB id, or by title and year where the queue did not give one.
    """
    queued = {card["tmdb_id"] for card in queue_cards if "tmdb_id" in card}
    queued_title_years = {
        (card["title"], card.get("year")) for card in queue_cards if "tmdb_id" not in card
    }
    items: list[ArrCard] = []
    for movie in movies:
        m_title = movie.get("title", "")
        m_year = movie.get("year")
        if not movie.get("monitored"):
            continue
        if movie.get("hasFile"):
            continue
        if movie.get("tmdbId") in queued or (m_title, m_year) in queued_title_years:
            continue

        is_upcoming, release_label = classify_movie_upcoming(movie)
        released_at = compute_movie_released_at(movie)

        added_at = 0.0
        added_dt = parse_iso_utc(movie.get("added", ""))
        if added_dt is not None:
            added_at = added_dt.timestamp()

        poster_url = extract_poster_url(movie.get("images"))

        card = make_arr_card(
            "movie",
            m_title,
            source="Radarr",
            year=m_year,
            poster_url=poster_url,
            arr_id=movie.get("id", 0),
            title_slug=movie.get("titleSlug", ""),
            added_at=added_at,
            released_at=released_at,
            is_upcoming=is_upcoming,
            release_label=release_label,
        )
        _identify(card, movie.get("tmdbId"))
        items.append(card)
    return items


def _label_shared_titles(cards: Sequence[ArrCard], movies: Sequence[RadarrMovie]) -> None:
    """Name each film whose title Radarr has more than once by its year too."""
    films = {(m.get("title", ""), m.get("tmdbId")) for m in movies}
    films |= {(card["title"], card.get("tmdb_id")) for card in cards}
    held = Counter(title for title, _ in films)
    for card in cards:
        if held[card["title"]] > 1 and card.get("year"):
            card["label"] = f"{card['title']} ({card['year']})"


def fetch_radarr_queue(
    client: ArrClient, *, on_failure: Callable[[], None] = lambda: None
) -> list[ArrCard]:
    """Build Radarr download cards from an already-constructed client.

    Returns cards for queue entries plus monitored movies still searching.
    ``_iter_still_searching`` keeps a failure of ``get_movies()`` from
    wiping out the queue entries we already have; *on_failure* is called
    instead.
    """
    items = _build_queue_cards(client)
    movies = list(
        _iter_still_searching(client.get_movies, service_label="Radarr", on_failure=on_failure)
    )
    items.extend(_build_still_searching_cards(movies, items))
    _label_shared_titles(items, movies)
    return items
