"""TypedDict shapes for the recommendations pipeline."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RecommendationItem(TypedDict):
    """A single recommendation as it flows through the pipeline.

    Produced by :func:`.prompts.parse_recommendations`, mutated in-place
    by :func:`.enrich.enrich_recommendations`, and persisted by
    :func:`.persist.refresh_recommendations`.

    All nullable fields are ``NotRequired`` because they are absent on the
    raw LLM output and filled in by later pipeline stages.
    """

    title: str
    year: NotRequired[int | None]
    media_type: str
    category: str
    tmdb_id: NotRequired[int | None]
    imdb_id: NotRequired[str | None]
    description: NotRequired[str | None]
    reason: str
    trailer_url: NotRequired[str | None]
    poster_url: NotRequired[str | None]
    # Fields added by enrich stage
    rating: NotRequired[float | None]
    rt_rating: NotRequired[str | None]
    imdb_rating: NotRequired[str | None]
    tagline: NotRequired[str | None]
    runtime: NotRequired[int | None]
    genres: NotRequired[str | None]
    cast_json: NotRequired[str | None]
    director: NotRequired[str | None]
    trailer_key: NotRequired[str | None]
    metascore: NotRequired[str | None]
