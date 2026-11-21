"""TypedDict shapes for the recommendations pipeline."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RecommendationItem(TypedDict):
    """A recommendation flowing through the pipeline: parsed by parse_recommendations,
    enriched in place by enrich_recommendations, persisted by refresh_recommendations.

    Nullable fields are NotRequired because raw LLM output lacks them.
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
