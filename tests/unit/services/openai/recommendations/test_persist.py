"""Tests for mediaman.services.openai.recommendations.persist."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.openai.recommendations import persist
from mediaman.services.openai.recommendations.persist import refresh_recommendations
from tests.helpers.factories import insert_suggestion


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


class TestRefreshRecommendations:
    def test_returns_zero_when_no_recommendations_generated(self, conn):
        """When OpenAI returns nothing, refresh returns 0 and commits nothing."""
        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
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
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)
        rows = conn.execute("SELECT title FROM suggestions").fetchall()
        assert any(r["title"] == "Dune" for r in rows)

    def test_the_backdrop_found_for_a_pick_is_stored_with_it(self, conn):
        recs = _fake_trending()
        recs[0]["backdrop_url"] = "https://image.tmdb.org/t/p/w1280/wide.jpg"
        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=recs,
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)
        row = conn.execute("SELECT backdrop_url FROM suggestions").fetchone()
        assert row["backdrop_url"] == "https://image.tmdb.org/t/p/w1280/wide.jpg"

    def test_manual_refresh_replaces_todays_batch(self, conn, freezer):
        """manual=True should delete today's existing batch before inserting."""
        from datetime import datetime

        today = datetime(2026, 5, 1, tzinfo=UTC).strftime("%Y-%m-%d")
        insert_suggestion(
            conn,
            category="trending",
            title="Old Rec",
            media_type="movie",
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
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            refresh_recommendations(
                conn, plex_client=_plex_client(), secret_key="x" * 64, manual=True
            )
        rows = conn.execute("SELECT title FROM suggestions WHERE batch_id = ?", (today,)).fetchall()
        titles = {r["title"] for r in rows}
        assert "Old Rec" not in titles
        assert "Dune" in titles

    @pytest.mark.parametrize(
        ("old", "new_title", "new_tmdb"),
        [
            ({"tmdb_id": 438631}, "Dune!", 438631),
            ({"tmdb_id": None, "title": "The Dune!"}, "the dune", None),
        ],
    )
    def test_same_day_refresh_keeps_download_mark(self, conn, freezer, old, new_title, new_tmdb):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            **{
                "title": "Dune",
                "batch_id": "2026-05-01",
                "downloaded_at": "2026-05-01T08:00:00",
                **old,
            },
        )
        recs = _fake_trending()
        recs[0].update({"title": new_title, "tmdb_id": new_tmdb})
        self._refresh(conn, recs)
        rows = conn.execute("SELECT title, downloaded_at FROM suggestions").fetchall()
        assert [(r["title"], r["downloaded_at"]) for r in rows] == [
            (new_title, "2026-05-01T08:00:00")
        ]

    def test_same_day_refresh_keeps_downloaded_suggestion_missing_from_new_set(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Old Pick",
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        insert_suggestion(conn, category="trending", title="Not Downloaded", batch_id="2026-05-01")
        self._refresh(conn, _fake_trending())
        rows = {
            r["title"]: r["downloaded_at"]
            for r in conn.execute("SELECT title, downloaded_at FROM suggestions").fetchall()
        }
        assert rows == {"Dune": None, "Old Pick": "2026-05-01T08:00:00"}

    @staticmethod
    def _marks(conn):
        return {
            (r["title"], r["year"]): r["downloaded_at"]
            for r in conn.execute("SELECT title, year, downloaded_at FROM suggestions")
        }

    def test_downloaded_season_does_not_pass_its_mark_to_another_season_of_the_show(
        self, conn, freezer
    ):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Show \u2014 Season 2",
            media_type="tv",
            tmdb_id=77,
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        rec = {
            **_fake_trending()[0],
            "title": "Show \u2014 Season 3",
            "media_type": "tv",
            "tmdb_id": 77,
        }
        self._refresh(conn, [rec])
        assert self._marks(conn) == {
            ("Show \u2014 Season 3", None): None,
            ("Show \u2014 Season 2", None): "2026-05-01T08:00:00",
        }

    def test_downloaded_remake_does_not_pass_its_mark_to_a_different_year(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Dune",
            year=1984,
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        self._refresh(conn, [{**_fake_trending()[0], "year": 2021}])
        assert self._marks(conn) == {("Dune", 2021): None, ("Dune", 1984): "2026-05-01T08:00:00"}

    def test_download_mark_survives_when_only_one_side_has_a_year(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Dune",
            year=None,
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        self._refresh(conn, [{**_fake_trending()[0], "year": 2021}])
        assert self._marks(conn) == {("Dune", 2021): "2026-05-01T08:00:00"}

    def test_different_tmdb_id_with_same_title_does_not_match(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Dune",
            tmdb_id=1,
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        self._refresh(conn, [{**_fake_trending()[0], "tmdb_id": 2}])
        rows = conn.execute("SELECT tmdb_id, downloaded_at FROM suggestions").fetchall()
        assert {(r["tmdb_id"], r["downloaded_at"]) for r in rows} == {
            (1, "2026-05-01T08:00:00"),
            (2, None),
        }

    def test_one_downloaded_row_marks_only_one_of_two_identical_new_rows(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Dune",
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        rec = _fake_trending()[0]
        self._refresh(conn, [dict(rec), dict(rec)])
        marks = [r[0] for r in conn.execute("SELECT downloaded_at FROM suggestions")]
        assert len(marks) == 2
        assert marks.count("2026-05-01T08:00:00") == 1

    def test_marks_are_read_under_the_write_lock(self, conn, db_path, freezer):
        """A download click committed after the marks are read would be lost, so the
        write lock must already be held when they are matched."""
        import sqlite3

        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Dune",
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        conn.commit()
        other = sqlite3.connect(str(db_path), timeout=0)
        outcome: list[str] = []
        real_match = persist._match_downloaded

        def match_then_probe(downloaded, item, taken):
            try:
                other.execute("UPDATE suggestions SET downloaded_at = 'click'")
                other.commit()
                outcome.append("written")
            except sqlite3.OperationalError:
                outcome.append("locked")
            return real_match(downloaded, item, taken)

        with patch.object(persist, "_match_downloaded", match_then_probe):
            self._refresh(conn, _fake_trending())
        other.close()
        assert outcome == ["locked"]

    def test_download_mark_survives_a_failed_refresh(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Dune",
            batch_id="2026-05-01",
            downloaded_at="2026-05-01T08:00:00",
        )
        conn.commit()
        bad = {k: v for k, v in _fake_trending()[0].items() if k != "category"}
        with pytest.raises(KeyError):
            self._refresh(conn, [bad])
        rows = conn.execute("SELECT title, downloaded_at FROM suggestions").fetchall()
        assert [(r["title"], r["downloaded_at"]) for r in rows] == [("Dune", "2026-05-01T08:00:00")]

    def test_refresh_leaves_yesterdays_batch_and_its_download_mark_alone(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        insert_suggestion(
            conn,
            category="trending",
            title="Dune",
            batch_id="2026-04-30",
            downloaded_at="2026-04-30T08:00:00",
        )
        insert_suggestion(conn, category="trending", title="Other", batch_id="2026-04-30")
        conn.commit()
        self._refresh(conn, _fake_trending())
        rows = conn.execute(
            "SELECT title, batch_id, downloaded_at FROM suggestions ORDER BY batch_id, title"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("Dune", "2026-04-30", "2026-04-30T08:00:00"),
            ("Other", "2026-04-30", None),
            ("Dune", "2026-05-01", None),
        ]

    @staticmethod
    def _refresh(conn, recs):
        with (
            patch(
                "mediaman.services.openai.recommendations.persist.generate_trending",
                return_value=recs,
            ),
            patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
        ):
            refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)

    def test_second_scheduled_refresh_same_day_does_not_double_batch(self, conn, freezer):
        freezer.move_to("2026-05-01 12:00:00")
        for _ in range(2):
            with (
                patch(
                    "mediaman.services.openai.recommendations.persist.generate_trending",
                    return_value=_fake_trending(),
                ),
                patch("mediaman.services.openai.recommendations.persist.enrich_recommendations"),
            ):
                refresh_recommendations(conn, plex_client=_plex_client(), secret_key="x" * 64)
        count = conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE batch_id = '2026-05-01'"
        ).fetchone()[0]
        assert count == 1

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
            category="trending",
            title="Pre-existing Rec",
            media_type="movie",
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
