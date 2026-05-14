"""Tests for mediaman.services.openai.recommendations.persist."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.openai.recommendations.persist import refresh_recommendations
from tests.helpers.factories import insert_media_item, insert_suggestion

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _plex_client(ratings=None):
    c = MagicMock()
    c.get_user_ratings.return_value = ratings or []
    return c


def _fake_trending():
    return [
        {
            "title": "Dune",
            "year": None,
            "media_type": "movie",
            "category": "trending",
            "tmdb_id": None,
            "imdb_id": None,
            "description": None,
            "reason": "Trending this week.",
            "trailer_url": "https://www.youtube.com",
            "poster_url": None,
        }
    ]


def _fake_personal():
    return [
        {
            "title": "Severance",
            "year": None,
            "media_type": "tv",
            "category": "personal",
            "tmdb_id": None,
            "imdb_id": None,
            "description": None,
            "reason": "Matches your history.",
            "trailer_url": "https://www.youtube.com",
            "poster_url": None,
        }
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRefreshRecommendations:
    def test_returns_zero_when_no_recommendations_generated(self, conn):
        """When OpenAI returns nothing, refresh returns 0 and commits nothing."""
        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=[],
            ),
            patch(
                "mediaman.services.openai.recommendations.persist.generate_personal",
                return_value=[],
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            result = refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)
        assert result == 0

    def test_returns_count_of_inserted_rows(self, conn):
        recs = _fake_trending()
        recs[0].update(
            {
                "rt_rating": None,
                "imdb_rating": None,
                "metascore": None,
                "tagline": None,
                "runtime": None,
                "genres": None,
                "cast_json": None,
                "director": None,
                "trailer_key": None,
                "rating": None,
            }
        )
        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=recs,
            ),
            patch(
                "mediaman.services.openai.recommendations.persist.generate_personal",
                return_value=[],
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            result = refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)
        assert result == 1

    def test_rows_inserted_into_suggestions_table(self, conn):
        recs = _fake_trending()
        recs[0].update(
            {
                "rt_rating": None,
                "imdb_rating": None,
                "metascore": None,
                "tagline": None,
                "runtime": None,
                "genres": None,
                "cast_json": None,
                "director": None,
                "trailer_key": None,
                "rating": None,
            }
        )
        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=recs,
            ),
            patch(
                "mediaman.services.openai.recommendations.persist.generate_personal",
                return_value=[],
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)
        rows = conn.execute("SELECT title FROM suggestions").fetchall()
        assert any(r["title"] == "Dune" for r in rows)

    def test_manual_refresh_replaces_todays_batch(self, conn):
        """manual=True should delete today's existing batch before inserting."""
        from datetime import datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        insert_suggestion(
            conn,
            title="Old Rec",
            media_type="movie",
            category="trending",
            reason="old reason",
            batch_id=today,
            created_at="2026-01-01T00:00:00",
        )

        recs = _fake_trending()
        recs[0].update(
            {
                "rt_rating": None,
                "imdb_rating": None,
                "metascore": None,
                "tagline": None,
                "runtime": None,
                "genres": None,
                "cast_json": None,
                "director": None,
                "trailer_key": None,
                "rating": None,
            }
        )
        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=recs,
            ),
            patch(
                "mediaman.services.openai.recommendations.persist.generate_personal",
                return_value=[],
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            refresh_recommendations(
                conn, plex_client=_plex_client(), secret_key="x" * 64, manual=True
            )
        rows = conn.execute("SELECT title FROM suggestions WHERE batch_id = ?", (today,)).fetchall()
        titles = {r["title"] for r in rows}
        assert "Old Rec" not in titles
        assert "Dune" in titles

    def test_plex_rating_failure_does_not_crash(self, conn):
        """If Plex raises during get_user_ratings, refresh must still proceed."""
        client = MagicMock()
        client.get_user_ratings.side_effect = requests.ConnectionError("Plex connection error")

        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=[],
            ),
            patch(
                "mediaman.services.openai.recommendations.persist.generate_personal",
                return_value=[],
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            result = refresh_recommendations(conn, plex_client=client, secret_key="x" * 64)
        assert result == 0  # no recommendations, but no crash

    def test_refresh_recommendations_rolls_back_on_partial_insert_failure(self, conn):
        """If an INSERT fails mid-loop, the DELETE must be rolled back atomically.

        Pre-condition: the suggestions table contains one row from a prior batch.
        A bad recommendation row is injected as the second item so that the real
        ``conn.execute(INSERT …)`` raises an IntegrityError (non-null column
        violation via a None ``media_type``).
        Post-condition: the original row must still be present — NOT deleted and
        NOT partially replaced.
        """
        from datetime import datetime

        yesterday = (
            datetime.now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .strftime("%Y-%m-%d")
        )
        insert_suggestion(
            conn,
            title="Pre-existing Rec",
            media_type="movie",
            category="trending",
            reason="was here before",
            batch_id=yesterday,
            created_at="2026-01-01T00:00:00",
        )

        # Two recommendations — the second has media_type=None which violates
        # the NOT NULL constraint on suggestions.media_type, causing a real
        # sqlite3.IntegrityError mid-loop with no monkeypatching required.
        rec_template = {
            "year": None,
            "tmdb_id": None,
            "imdb_id": None,
            "description": None,
            "reason": "reason",
            "trailer_url": None,
            "poster_url": None,
            "rt_rating": None,
            "imdb_rating": None,
            "metascore": None,
            "tagline": None,
            "runtime": None,
            "genres": None,
            "cast_json": None,
            "director": None,
            "trailer_key": None,
            "rating": None,
        }
        good_rec = {
            **rec_template,
            "title": "New Rec 1",
            "media_type": "movie",
            "category": "trending",
        }
        # media_type is accessed via s["media_type"] in the INSERT loop — omitting it
        # triggers a KeyError mid-loop with no monkeypatching required.
        bad_rec = {
            **rec_template,
            "title": "New Rec 2",
            "media_type": "movie",
            "category": "trending",
        }
        bad_rec.pop("media_type")  # now KeyError when the loop hits s["media_type"]
        two_recs = [good_rec, bad_rec]

        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=two_recs,
            ),
            patch(
                "mediaman.services.openai.recommendations.persist.generate_personal",
                return_value=[],
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
            pytest.raises(KeyError),
        ):
            refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)

        rows = conn.execute("SELECT title FROM suggestions").fetchall()
        titles = {r["title"] for r in rows}
        # The pre-existing row must have survived; partial-insert state must not exist.
        assert "Pre-existing Rec" in titles, "DELETE was not rolled back — pre-existing row is gone"
        assert "New Rec 1" not in titles, (
            "Partial INSERT state was committed — rollback did not occur"
        )

    def test_watch_history_from_db_used(self, conn):
        """Media items in the DB are picked up and passed as watch history."""
        insert_media_item(
            conn,
            id="id-001",
            title="Breaking Bad",
            media_type="tv",
            plex_rating_key="rk-001",
            added_at="2026-01-01T00:00:00",
            file_path="/path/ep.mkv",
            file_size_bytes=1_000_000_000,
            last_watched_at="2026-01-01T00:00:00",
        )

        captured_history = {}

        def fake_generate_personal(
            conn, watch_history, user_ratings, previous_titles, *, secret_key=""
        ):
            captured_history["value"] = watch_history
            return []

        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=[],
            ),
            patch(
                "mediaman.services.openai.recommendations.persist.generate_personal",
                side_effect=fake_generate_personal,
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)
        assert any(h["title"] == "Breaking Bad" for h in captured_history["value"])
