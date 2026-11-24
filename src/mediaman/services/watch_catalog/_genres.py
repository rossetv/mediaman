"""The genres Watch lists titles by. TMDB numbers the genres of films and of TV apart and
draws some of them wider for TV ("Action & Adventure"); each genre here names both."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Genre:
    """*movie* and *tv* are TMDB's genre ids, None where it has no such genre; *aliases*
    are other words people type for it."""

    id: str
    name: str
    movie: int | None
    tv: int | None
    aliases: tuple[str, ...] = ()

    def tmdb_id(self, media_type: str) -> int | None:
        return self.movie if media_type == "movie" else self.tv

    @property
    def types(self) -> list[str]:
        return [t for t in ("movie", "tv") if self.tmdb_id(t) is not None]


_LIST = (
    Genre("action", "Action", 28, 10759, ("fighting", "martial arts")),
    Genre("comedy", "Comedy", 35, 35, ("comedies", "funny", "sitcom", "romcom")),
    Genre("drama", "Drama", 18, 18, ("dramas",)),
    Genre("science-fiction", "Science Fiction", 878, 10765, ("sci-fi", "scifi", "sf", "space")),
    Genre("thriller", "Thriller", 53, None, ("thrillers", "suspense")),
    Genre("horror", "Horror", 27, None, ("scary", "frightening")),
    Genre("crime", "Crime", 80, 80, ("gangster", "detective", "police")),
    Genre("adventure", "Adventure", 12, 10759, ("adventures",)),
    Genre("fantasy", "Fantasy", 14, 10765, ("magic",)),
    Genre("animation", "Animation", 16, 16, ("animated", "cartoon", "cartoons", "anime")),
    Genre("family", "Family", 10751, 10751, ("families",)),
    Genre("kids", "Kids", None, 10762, ("children",)),
    Genre("romance", "Romance", 10749, None, ("romantic", "love", "romcom")),
    Genre("mystery", "Mystery", 9648, 9648, ("mysteries", "whodunit", "detective")),
    Genre("documentary", "Documentary", 99, 99, ("documentaries", "docs", "true story")),
    Genre("history", "History", 36, None, ("historical", "period")),
    Genre("war", "War", 10752, 10768, ("military", "politics")),
    Genre("western", "Western", 37, 37, ("westerns", "cowboy")),
    Genre("music", "Music", 10402, None, ("musical", "musicals", "concert")),
    Genre("reality", "Reality", None, 10764, ("reality tv",)),
    Genre("talk", "Talk", None, 10767, ("talk show", "late night")),
    Genre("news", "News", None, 10763, ()),
    Genre("soap", "Soap", None, 10766, ("soap opera", "telenovela")),
    Genre("tv-movie", "TV Movie", 10770, None, ("television film",)),
)

GENRES: dict[str, Genre] = {genre.id: genre for genre in _LIST}


def genre_refs(genres: object, media_type: str) -> list[dict[str, str]]:
    """The genre of each entry of a TMDB details payload's *genres*, for those listed here.
    A wide TV genre is the first listed of the two it covers."""
    by_tmdb: dict[int, str] = {}
    for genre in _LIST:
        tmdb_id = genre.tmdb_id(media_type)
        if tmdb_id is not None:
            by_tmdb.setdefault(tmdb_id, genre.id)
    return [
        {"name": g["name"], "genre": by_tmdb[g["id"]]}
        for g in (genres if isinstance(genres, list) else [])
        if isinstance(g, Mapping)
        and isinstance(g.get("name"), str)
        and isinstance(g.get("id"), int)
        and g["id"] in by_tmdb
    ]
