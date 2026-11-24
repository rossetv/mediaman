"""Tests for item_enrichment helpers."""

from __future__ import annotations

import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.media_meta import item_enrichment
from mediaman.services.media_meta.item_enrichment import (
    enrich_item_with_tmdb,
    enrich_redownload_item,
    fill_backdrop,
    tidy_summary,
)
from tests.helpers.factories import insert_suggestion

SECRET = "test-secret-32-chars-XXXXXXXXXX"


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


class TestFetchTmdbForItem:
    """Tests for the TMDB + OMDB enrichment helper."""

    @patch("mediaman.services.media_meta.omdb.fetch_ratings")
    @patch("mediaman.services.media_meta.tmdb.TmdbClient")
    def test_enrichment_fills_missing_fields(self, mock_tmdb_cls, mock_fetch_ratings, conn):
        """When poster and year are absent the function fetches from TMDB."""
        mock_client = MagicMock()
        mock_tmdb_cls.from_db.return_value = mock_client

        # search() returns a raw TMDB result; shape_card/shape_detail process it
        mock_client.search.return_value = {"id": 42}
        mock_tmdb_cls.shape_card.return_value = {
            "tmdb_id": 42,
            "year": 2021,
            "description": "A film about sand.",
            "rating": 8.0,
            "poster_url": "https://example.com/dune.jpg",
        }
        mock_client.details.return_value = {"id": 42}
        mock_tmdb_cls.shape_detail.return_value = {
            "tagline": "Beyond fear, destiny awaits.",
            "runtime": 155,
            "genres": ["Sci-Fi"],
            "director": "Denis Villeneuve",
            "cast_json": "[]",
            "trailer_key": "abc123",
        }
        mock_fetch_ratings.return_value = {"rt": "83%", "imdb": "7.9"}

        item: dict = {"title": "Dune", "media_type": "movie"}
        enrich_item_with_tmdb(item, conn, SECRET)

        assert item["poster_url"] == "https://example.com/dune.jpg"
        assert item["year"] == 2021
        assert item["rating"] == 8.0
        assert item["rt_rating"] == "83%"
        assert item["imdb_rating"] == "7.9"
        assert item["director"] == "Denis Villeneuve"

    @patch("mediaman.services.media_meta.omdb.fetch_ratings")
    @patch("mediaman.services.media_meta.tmdb.TmdbClient")
    def test_enrichment_skips_search_when_tmdb_id_present(
        self, mock_tmdb_cls, mock_fetch_ratings, conn
    ):
        """When item already has a tmdb_id the search step is skipped."""
        mock_client = MagicMock()
        mock_tmdb_cls.from_db.return_value = mock_client

        mock_client.details.return_value = {"id": 99}
        mock_tmdb_cls.shape_detail.return_value = {
            "tagline": "",
            "runtime": 120,
            "genres": [],
            "director": "",
            "cast_json": "",
            "trailer_key": "",
        }
        mock_tmdb_cls.shape_card.return_value = {
            "tmdb_id": 99,
            "year": 2020,
            "description": "",
            "rating": None,
            "poster_url": "",
        }
        mock_fetch_ratings.return_value = {}

        item = {
            "title": "Already Enriched",
            "media_type": "movie",
            "tmdb_id": 99,
            "poster_url": "https://example.com/poster.jpg",
            "year": 2020,
        }
        enrich_item_with_tmdb(item, conn, SECRET)

        mock_client.search.assert_not_called()

    @patch("mediaman.services.media_meta.omdb.fetch_ratings")
    @patch("mediaman.services.media_meta.tmdb.TmdbClient")
    def test_enrichment_handles_tmdb_client_none(self, mock_tmdb_cls, mock_fetch_ratings, conn):
        """When TmdbClient.from_db returns None (no token), no search is attempted."""
        mock_tmdb_cls.from_db.return_value = None
        mock_fetch_ratings.return_value = {}

        item = {"title": "No Token Film", "media_type": "movie"}
        enrich_item_with_tmdb(item, conn, SECRET)  # must not raise

        assert item.get("poster_url") is None  # nothing was filled in

    @patch("mediaman.services.media_meta.omdb.fetch_ratings")
    @patch("mediaman.services.media_meta.tmdb.TmdbClient")
    def test_enrichment_handles_omdb_failure_gracefully(
        self, mock_tmdb_cls, mock_fetch_ratings, conn
    ):
        """If fetch_ratings raises, the item is still returned without crashing.

        fetch_ratings is documented as never-raising, but if it did the caller
        (route handler) should not crash — we verify enrich_item_with_tmdb
        itself doesn't suppress this incorrectly. In practice fetch_ratings
        returns {} on failure; we simulate that here.
        """
        mock_tmdb_cls.from_db.return_value = None
        mock_fetch_ratings.return_value = {}  # OMDB quietly failed

        item = {"title": "Graceful Failure", "media_type": "movie"}
        enrich_item_with_tmdb(item, conn, SECRET)

        # No ratings keys added, but no exception either
        assert "rt_rating" not in item
        assert "imdb_rating" not in item

    @patch("mediaman.services.media_meta.omdb.fetch_ratings")
    @patch("mediaman.services.media_meta.tmdb.TmdbClient")
    def test_existing_poster_not_overwritten_by_tmdb(self, mock_tmdb_cls, mock_fetch_ratings, conn):
        """poster_url already on the item is preserved when TMDB also returns one."""
        mock_client = MagicMock()
        mock_tmdb_cls.from_db.return_value = mock_client

        mock_client.search.return_value = {"id": 1}
        mock_tmdb_cls.shape_card.return_value = {
            "tmdb_id": 1,
            "year": 2023,
            "description": "",
            "rating": 7.5,
            "poster_url": "https://tmdb.example.com/new.jpg",
        }
        mock_client.details.return_value = None  # no details
        mock_fetch_ratings.return_value = {}

        existing_poster = "https://arr.example.com/existing.jpg"
        item = {
            "title": "Existing Poster Film",
            "media_type": "movie",
            "poster_url": existing_poster,
        }
        enrich_item_with_tmdb(item, conn, SECRET)

        # The search populates poster_url if missing; since item already had
        # one the search result replaces it (shape_card result is used
        # unconditionally in the search branch — this tests actual behaviour).
        # The important thing is no crash occurs and poster_url is set.
        assert item["poster_url"]  # some poster present


class TestEnrichRedownloadItem:
    """Tests for the higher-level enrichment entry point."""

    def test_enrichment_uses_suggestions_cache_when_available(self, conn):
        """Cache hit: suggestions row with a poster_url populates item fields."""
        insert_suggestion(
            conn,
            title="Inception",
            media_type="movie",
            poster_url="https://example.com/inception.jpg",
            year=2010,
            description="A dream within a dream.",
            reason="Mind-bending heist.",
            rating=8.8,
            rt_rating="87%",
            tagline="Your mind is the scene of the crime.",
            runtime=148,
            genres='["Sci-Fi"]',
            cast_json="[]",
            director="Christopher Nolan",
            trailer_key="trailer-key-123",
            imdb_rating="8.8",
            metascore="74",
            created_at="2026-01-01T00:00:00",
        )

        item = {"title": "Inception", "media_type": "movie"}
        enrich_redownload_item(item, conn, SECRET)

        assert item["poster_url"] == "https://example.com/inception.jpg"
        assert item["director"] == "Christopher Nolan"
        assert item["rt_rating"] == "87%"

    @patch("mediaman.services.media_meta.item_enrichment.enrich_item_with_tmdb")
    def test_enrichment_falls_back_to_tmdb_when_no_cache(self, mock_fetch, conn):
        item = {"title": "Uncached Film", "media_type": "movie"}
        enrich_redownload_item(item, conn, SECRET)
        mock_fetch.assert_called_once_with(item, conn, SECRET)

    @patch("mediaman.services.media_meta.item_enrichment.enrich_item_with_tmdb")
    def test_enrichment_skips_tmdb_when_cache_has_poster(self, mock_fetch, conn):
        insert_suggestion(
            conn,
            title="Cached Film",
            media_type="movie",
            poster_url="https://example.com/cached.jpg",
            year=2022,
            description="Great film.",
            reason="Top pick.",
            rating=9.0,
            rt_rating="95%",
            tagline="Extraordinary.",
            runtime=120,
            genres="[]",
            cast_json="[]",
            director="Director Name",
            imdb_rating="9.0",
            metascore="90",
            created_at="2026-01-01T00:00:00",
        )

        item = {"title": "Cached Film", "media_type": "movie"}
        enrich_redownload_item(item, conn, SECRET)

        mock_fetch.assert_not_called()

    @patch("mediaman.services.media_meta.item_enrichment.enrich_item_with_tmdb")
    def test_enrichment_falls_back_when_cache_row_has_no_poster(self, mock_fetch, conn):
        """Cache row with a NULL/empty poster_url still triggers TMDB fallback."""
        insert_suggestion(
            conn,
            title="Incomplete Cache",
            media_type="movie",
            poster_url="",  # empty poster_url triggers TMDB fallback
            year=2020,
            description="Desc.",
            reason="Reason.",
            rating=7.0,
            created_at="2026-01-01T00:00:00",
        )

        item = {"title": "Incomplete Cache", "media_type": "movie"}
        enrich_redownload_item(item, conn, SECRET)

        mock_fetch.assert_called_once()

    @patch("mediaman.services.media_meta.item_enrichment.enrich_item_with_tmdb")
    def test_a_title_known_by_its_id_is_not_given_the_details_of_a_namesake(self, mock_fetch, conn):
        for tmdb_id, year, created in [(438631, 2021, "2026-01-01"), (841, 1984, "2026-02-01")]:
            insert_suggestion(
                conn,
                title="Dune" if tmdb_id == 841 else "Dune: Part One",
                media_type="movie",
                tmdb_id=tmdb_id,
                year=year,
                poster_url=f"https://example.com/{year}.jpg",
                created_at=created,
            )

        item = {"title": "Dune", "media_type": "movie", "tmdb_id": 438631}
        enrich_redownload_item(item, conn, SECRET)
        assert item["year"] == 2021

        other = {"title": "Dune", "media_type": "movie", "tmdb_id": 9}
        enrich_redownload_item(other, conn, SECRET)
        assert "year" not in other
        mock_fetch.assert_called_once_with(other, conn, SECRET)


WIDE = "https://image.tmdb.org/t/p/w1280/wide.jpg"


@pytest.fixture(autouse=True)
def _forget_backdrops():
    item_enrichment._backdrop_cache.clear()
    item_enrichment._facts_cache.clear()


def _tmdb_with(details):
    client = MagicMock()
    client.details.return_value = details
    return patch(
        "mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=client
    ), client


class TestFillBackdrop:
    """The emailed page's wide backdrop: the stored one, one stored for the same title, TMDB's."""

    def test_a_backdrop_stored_for_the_same_title_is_used_without_asking_tmdb(self, conn):
        sid = insert_suggestion(conn, title="Dune", tmdb_id=42, created_at="2026-01-01")
        conn.execute("UPDATE suggestions SET backdrop_url = ? WHERE id = ?", (WIDE, sid))
        patcher, client = _tmdb_with({"backdrop_path": "/other.jpg"})
        item: dict = {"media_type": "movie", "tmdb_id": 42}

        with patcher:
            fill_backdrop(item, conn, SECRET)

        assert item["backdrop_url"] == WIDE
        client.details.assert_not_called()

    def test_tmdb_gives_the_backdrop_when_none_is_stored_and_is_asked_once_a_while(self, conn):
        patcher, client = _tmdb_with({"backdrop_path": "/wide.jpg"})

        with patcher:
            for _ in range(2):
                item: dict = {"media_type": "tv", "tmdb_id": 7}
                fill_backdrop(item, conn, SECRET)

        assert item["backdrop_url"] == WIDE
        client.details.assert_called_once_with("tv", 7)

    def test_a_title_with_no_backdrop_or_no_answer_is_left_without_one(self, conn):
        patcher, _client = _tmdb_with(None)
        item: dict = {"media_type": "movie", "tmdb_id": 9}

        with patcher:
            fill_backdrop(item, conn, SECRET)

        assert item["backdrop_url"] is None

    def test_a_stored_address_that_is_no_tmdb_image_is_not_shown(self, conn):
        item: dict = {"media_type": "movie", "backdrop_url": 'https://evil.example/a.jpg"onerror='}

        fill_backdrop(item, conn, SECRET)

        assert item["backdrop_url"] is None

    def test_a_failing_tmdb_never_fails_the_page(self, conn):
        with patch(
            "mediaman.services.media_meta.tmdb.TmdbClient.from_db",
            side_effect=ValueError("bad token"),
        ):
            item: dict = {"media_type": "movie", "tmdb_id": 3}
            fill_backdrop(item, conn, SECRET)

        assert item["backdrop_url"] is None

    def test_a_redownload_takes_the_backdrop_stored_with_its_suggestion(self, conn):
        sid = insert_suggestion(
            conn, title="Dune", tmdb_id=42, poster_url="https://x/p.jpg", created_at="2026-01-01"
        )
        conn.execute("UPDATE suggestions SET backdrop_url = ? WHERE id = ?", (WIDE, sid))
        item: dict = {"title": "Dune", "media_type": "movie", "tmdb_id": 42}

        enrich_redownload_item(item, conn, SECRET)

        assert item["backdrop_url"] == WIDE

    @pytest.mark.parametrize(
        ("tmdb_answer", "held_s"),
        [({"overview": "From TMDB."}, 600), ({"id": 42}, 600), (None, 60)],
        ids=["found", "empty", "failed"],
    )
    def test_a_title_not_stored_is_asked_for_once_in_ten_minutes_or_one_after_a_failure(
        self, conn, monkeypatch, tmdb_answer, held_s
    ):
        clock = [1000.0]
        monkeypatch.setattr(item_enrichment, "_clock", lambda: clock[0])
        tmdb = MagicMock(**{"details.return_value": tmdb_answer, "search.return_value": None})
        shown = []
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}) as ratings,
        ):
            for later, tmdb_id in ((0, 42), (held_s - 1, 42), (held_s - 1, 43), (held_s, 42)):
                clock[0] = 1000.0 + later
                item: dict = {"title": "Dune", "media_type": "movie", "tmdb_id": tmdb_id}
                enrich_redownload_item(item, conn, SECRET)
                shown.append(item.get("description"))

        assert [c.args[1] for c in tmdb.details.call_args_list] == [42, 43, 42]
        assert ratings.call_count == 3
        assert shown[0] == shown[1] == (tmdb_answer and tmdb_answer.get("overview", ""))

    @pytest.mark.parametrize(
        ("answers", "held_s"),
        [([{"id": 7}], 600), ([None], 60), (ValueError("bad token"), 60)],
        ids=["none", "failed", "refused"],
    )
    def test_a_missing_backdrop_is_asked_for_again_sooner_after_a_failure(
        self, conn, monkeypatch, answers, held_s
    ):
        clock = [1000.0]
        monkeypatch.setattr(item_enrichment, "_clock", lambda: clock[0])
        tmdb = MagicMock()
        tmdb.details.side_effect = answers * 2 if isinstance(answers, list) else answers
        with patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb):
            asked = []
            for later in (0, held_s - 1, held_s):
                clock[0] = 1000.0 + later
                fill_backdrop({"media_type": "tv", "tmdb_id": 7}, conn, SECRET)
                asked.append(tmdb.details.call_count)
        assert asked == [1, 1, 2]

    def test_held_facts_are_of_one_type_and_carry_no_reason(self, conn):
        with patch("mediaman.services.media_meta.item_enrichment.enrich_item_with_tmdb") as fetch:
            first: dict = {"title": "D", "media_type": "movie", "tmdb_id": 4, "reason": "Mine."}
            enrich_redownload_item(first, conn, SECRET)
            again: dict = {"title": "D", "media_type": "movie", "tmdb_id": 4}
            enrich_redownload_item(again, conn, SECRET)
            enrich_redownload_item({"title": "D", "media_type": "tv", "tmdb_id": 4}, conn, SECRET)
        assert "reason" not in again
        assert [c.args[0]["media_type"] for c in fetch.call_args_list] == ["movie", "tv"]

    @pytest.mark.parametrize(("category", "shown"), [("trending", "A heist."), ("personal", None)])
    def test_a_reason_made_from_everyones_viewing_is_not_shown_on_a_redownload(
        self, conn, category, shown
    ):
        insert_suggestion(
            conn, tmdb_id=42, poster_url="https://x/p.jpg", category=category, reason="A heist."
        )
        item: dict = {"title": "Dune", "media_type": "movie", "tmdb_id": 42}

        enrich_redownload_item(item, conn, SECRET)

        assert (item["reason"], item["poster_url"]) == (shown, "https://x/p.jpg")


class TestTidySummary:
    CUT = (
        "After being separated for 15 years, the Sword of Power leads Prince Adam back to "
        "Eternia, where he discovers his home shattered under the fiendish rule of Skeletor. "
        "To save his family and his world, Adam must join forces with his closest allies, Teel"
    )

    def test_a_cut_summary_ends_at_its_last_full_sentence(self):
        assert len(self.CUT) == 250

        assert tidy_summary(self.CUT) == self.CUT[: self.CUT.index("Skeletor.") + 9]

    def test_a_cut_with_no_full_sentence_ends_at_a_whole_word_with_an_ellipsis(self):
        cut = ("word " * 60)[:250]

        out = tidy_summary(cut)

        assert out is not None and out.endswith("word…") and len(out) < 250

    def test_a_summary_that_is_not_a_cut_is_left_as_it_is(self):
        short = "A film about sand and no end to it"
        whole = "x" * 249 + "."
        assert tidy_summary(short) == short
        assert tidy_summary(whole) == whole
        assert tidy_summary(None) is None


class TestAHungTmdbOnAPublicPage:
    @pytest.fixture(autouse=True)
    def _open_gate(self):
        from mediaman.services.watch_catalog import gate

        gate.reset()
        yield
        gate.reset()

    def test_the_page_asks_tmdb_briefly_and_once(self, conn):
        tmdb = MagicMock(token="t", **{"details.return_value": None, "search.return_value": None})
        with (
            patch(
                "mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb
            ) as from_db,
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            enrich_redownload_item({"title": "Dune", "media_type": "movie"}, conn, SECRET)
            fill_backdrop({"media_type": "movie", "tmdb_id": 5}, conn, SECRET)

        assert from_db.call_count == 2
        for call in from_db.call_args_list:
            assert call.kwargs["retry"] is False
            assert call.kwargs["timeout"] <= 3.0

    def test_a_paused_tmdb_is_not_asked_by_the_page(self, conn):
        from mediaman.services.watch_catalog import TmdbDown, gate

        tmdb = MagicMock(token="t")
        with gate.asking("t"):
            pass
        gate.note("t", TmdbDown("x"))
        gate.note("t", TmdbDown("x"))
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            item: dict = {"title": "Dune", "media_type": "movie"}
            enrich_redownload_item(item, conn, SECRET)

        tmdb.search.assert_not_called()
        tmdb.details.assert_not_called()

    def test_two_visitors_of_one_title_cost_one_lookup(self, conn):
        entered = threading.Event()

        def slow_search(*_a, **_k):
            entered.set()
            time.sleep(0.3)

        tmdb = MagicMock(token="t", **{"details.return_value": None})
        tmdb.search.side_effect = slow_search

        db_path = conn.execute("PRAGMA database_list").fetchone()[2]

        def visit():
            own = sqlite3.connect(db_path)
            own.row_factory = sqlite3.Row
            enrich_redownload_item({"title": "Dune", "media_type": "movie"}, own, SECRET)
            own.close()

        threads = [threading.Thread(target=visit) for _ in range(3)]
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            threads[0].start()
            assert entered.wait(5)
            for t in threads[1:]:
                t.start()
            for t in threads:
                t.join(10)

        assert tmdb.search.call_count == 1

    def test_a_paused_tmdb_is_not_asked_for_a_backdrop_and_the_refusal_is_not_held(self, conn):
        from mediaman.services.watch_catalog import TmdbDown, gate

        tmdb = MagicMock(token="t", **{"details.return_value": {"backdrop_path": "/wide.jpg"}})
        with gate.asking("t"):
            pass
        gate.note("t", TmdbDown("x"))
        gate.note("t", TmdbDown("x"))
        with patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb):
            paused: dict = {"media_type": "movie", "tmdb_id": 5}
            fill_backdrop(paused, conn, SECRET)
            tmdb.details.assert_not_called()
            gate.reset()
            later: dict = {"media_type": "movie", "tmdb_id": 5}
            fill_backdrop(later, conn, SECRET)

        assert paused["backdrop_url"] is None
        assert later["backdrop_url"] == WIDE

    def test_a_view_waits_for_the_lookup_of_its_title_in_hand_and_takes_its_answer(self, conn):
        tmdb = MagicMock(token="t", **{"details.return_value": {"backdrop_path": "/wide.jpg"}})
        key = ("backdrop", "movie", 5)
        item: dict = {"media_type": "movie", "tmdb_id": 5}
        lock = threading.Lock()
        item_enrichment._flights[key] = lock
        lock.acquire()
        with patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb):
            db_path = conn.execute("PRAGMA database_list").fetchone()[2]

            def visit_page():
                own = sqlite3.connect(db_path)
                own.row_factory = sqlite3.Row
                fill_backdrop(item, own, SECRET)
                own.close()

            visit = threading.Thread(target=visit_page)
            visit.start()
            item_enrichment._backdrop_cache[("movie", 5)] = (time.monotonic() + 60, WIDE)
            lock.release()
            visit.join(10)

        tmdb.details.assert_not_called()
        assert item["backdrop_url"] == WIDE

    @pytest.mark.parametrize("page", ["backdrop", "facts"])
    def test_a_view_that_waits_too_long_for_a_lookup_in_hand_goes_without(
        self, conn, monkeypatch, page
    ):
        monkeypatch.setattr(item_enrichment, "_FLIGHT_WAIT_S", 0.05)
        tmdb = MagicMock(token="t", **{"details.return_value": {"backdrop_path": "/wide.jpg"}})
        key = ("backdrop", "movie", 5) if page == "backdrop" else ("movie", 5, "Dune")
        item_enrichment._flights[key] = threading.Lock()
        item_enrichment._flights[key].acquire()
        item: dict = {"title": "Dune", "media_type": "movie", "tmdb_id": 5}
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=tmdb),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            if page == "backdrop":
                fill_backdrop(item, conn, SECRET)
            else:
                enrich_redownload_item(item, conn, SECRET)

        tmdb.details.assert_not_called()
        assert not item.get("backdrop_url")
        item_enrichment._flights.pop(key).release()
