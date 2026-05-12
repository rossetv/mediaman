"""Tests for mediaman.services.openai.recommendations.enrich.

Mocking strategy: enrich_recommendations does all imports inside the function
body (``from mediaman.services.media_meta.tmdb import TmdbClient``, etc.), so we patch
at the *canonical* module path where each name lives rather than at the
enrich module's namespace.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.openai.recommendations.enrich import enrich_recommendations

_SECRET_KEY = "x" * 64


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _rec(title="Dune", media_type="movie") -> dict:
    return {
        "title": title,
        "year": None,
        "media_type": media_type,
        "category": "trending",
        "tmdb_id": None,
        "description": None,
        "rating": None,
        "poster_url": None,
        "trailer_url": "https://www.youtube.com",
    }


def _shape_card(title="Dune", year=2021, tmdb_id=438631) -> dict:
    return {
        "tmdb_id": tmdb_id,
        "year": year,
        "description": "An epic sci-fi film.",
        "rating": 7.9,
        "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
    }


def _shape_detail_empty() -> dict:
    return {
        "tagline": "",
        "runtime": None,
        "genres": "",
        "director": "",
        "cast_json": "",
        "trailer_key": "",
    }


def _mock_tmdb(search_return=None, card_return=None, detail_return=None):
    """Build a TmdbClient mock. Returned via ``from_db``."""
    cls = MagicMock()
    inst = MagicMock()
    inst.search.return_value = search_return  # None means "not found"
    inst.details.return_value = detail_return or {}
    cls.from_db.return_value = inst
    cls.shape_card.return_value = card_return or _shape_card()
    cls.shape_detail.return_value = _shape_detail_empty()
    return cls, inst


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnrichRecommendations:
    def test_tmdb_data_applied_to_rec(self, conn):
        cls, _inst = _mock_tmdb(
            search_return={"id": 438631, "title": "Dune"}, card_return=_shape_card()
        )
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            recs = [_rec()]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert recs[0]["tmdb_id"] == 438631
        assert recs[0]["year"] == 2021
        assert recs[0]["rating"] == 7.9
        assert recs[0]["poster_url"]

    def test_description_truncated_to_250_chars(self, conn):
        card = _shape_card()
        card["description"] = "A" * 400
        cls, _inst = _mock_tmdb(search_return={"id": 1, "title": "Dune"}, card_return=card)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            recs = [_rec()]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert len(recs[0]["description"]) <= 250

    def test_omdb_ratings_applied(self, conn):
        cls, _inst = _mock_tmdb(search_return=None)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch(
                "mediaman.services.media_meta.omdb.fetch_ratings",
                return_value={"rt": "92%", "imdb": "8.0", "metascore": "75"},
            ),
        ):
            recs = [_rec()]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert recs[0].get("rt_rating") == "92%"
        assert recs[0].get("imdb_rating") == "8.0"
        assert recs[0].get("metascore") == "75"

    def test_imdb_score_used_as_fallback_rating(self, conn):
        """When TMDB has no rating, fall back to the IMDb score from OMDb."""
        cls, _inst = _mock_tmdb(search_return=None)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={"imdb": "7.5"}),
        ):
            rec = _rec()
            rec["rating"] = None
            enrich_recommendations([rec], conn, _SECRET_KEY)

        assert rec["rating"] == 7.5

    def test_rec_without_title_skipped(self, conn):
        """A recommendation dict with no title must be skipped without raising."""
        cls, _inst = _mock_tmdb(search_return=None)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            rec = _rec()
            rec["title"] = ""
            enrich_recommendations([rec], conn, _SECRET_KEY)  # must not raise

    def test_trailer_url_updated_with_year(self, conn):
        """When TMDB yields a year, trailer_url is rebuilt with title + year."""
        card = _shape_card(year=2021)
        cls, _inst = _mock_tmdb(search_return={"id": 1, "title": "Dune"}, card_return=card)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            rec = _rec()
            enrich_recommendations([rec], conn, _SECRET_KEY)

        assert "2021" in rec["trailer_url"]
