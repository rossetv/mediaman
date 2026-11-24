"""The Watch pages: rows, show, movie and player context, built from the library sync,
each person's own progress and a fake Plex; anything outside the scanned libraries is
refused."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexItem,
    PlexItemNotFound,
    PlexSeason,
    PlexShow,
    PlexUnavailable,
)
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.viewer_guard import ViewerGuardMiddleware
from mediaman.web.repository.watch_progress import save_progress
from mediaman.web.routes.watch import list_page, pages
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
_WEB = Path(mediaman.__file__).parent / "web"


class FakePlex:
    """Answers like PlexWatch from in-memory items; *error* makes every call raise it."""

    def __init__(self) -> None:
        self.error: PlexError | None = None
        self.items: dict[str, PlexItem] = {}
        self.shows: dict[str, PlexShow] = {}
        self.episodes: dict[str, list[PlexEpisode]] = {}
        self._order: list[tuple[str, PlexEpisode]] = []

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    def add_movie(self, key: str, title: str, section: str = "1") -> None:
        self.items[key] = PlexItem(
            type="movie",
            rating_key=key,
            title=title,
            summary="A film.",
            year=2019,
            duration_s=6000,
            library_section_id=section,
            thumb=f"/library/metadata/{key}/thumb/1",
            subtitle_selected=False,
        )

    def add_show(
        self, key: str, title: str, seasons: dict[int, tuple[str, list[str]]], section: str = "1"
    ) -> None:
        """*seasons* maps a season number to (season key, episode keys in order)."""
        self.shows[key] = PlexShow(
            rating_key=key,
            title=title,
            summary="A town.",
            year_first=2021,
            year_last=2024,
            genre="Drama",
            library_section_id=section,
            thumb=None,
            seasons=[PlexSeason(sk, n, f"Season {n}") for n, (sk, _) in sorted(seasons.items())],
        )
        for number, (season_key, episode_keys) in sorted(seasons.items()):
            self.episodes[season_key] = []
            for index, ep_key in enumerate(episode_keys, start=1):
                episode = PlexEpisode(
                    rating_key=ep_key,
                    index=index,
                    title=f"Episode {ep_key}",
                    duration_s=3000,
                    thumb=f"/library/metadata/{ep_key}/thumb/1",
                    season_index=number,
                    season_rating_key=season_key,
                )
                self.episodes[season_key].append(episode)
                self._order.append((key, episode))
                self.items[ep_key] = PlexItem(
                    type="episode",
                    rating_key=ep_key,
                    title=episode.title,
                    summary="",
                    year=None,
                    duration_s=3000,
                    library_section_id=section,
                    thumb=episode.thumb,
                    subtitle_selected=False,
                    index=index,
                    parent_index=number,
                    parent_rating_key=season_key,
                    grandparent_rating_key=key,
                    grandparent_title=title,
                )

    def ping(self) -> None:
        self._check()

    def get_item(self, key: str) -> PlexItem:
        self._check()
        if key not in self.items:
            raise PlexItemNotFound("Item not found in Plex")
        return self.items[key]

    def get_show(self, key: str) -> PlexShow:
        self._check()
        if key not in self.shows:
            raise PlexItemNotFound("Item not found in Plex")
        return self.shows[key]

    def get_season_episodes(self, season_key: str) -> list[PlexEpisode]:
        self._check()
        return list(self.episodes.get(season_key, []))

    def next_episode(self, key: str) -> PlexEpisode | None:
        self._check()
        for i, (show, episode) in enumerate(self._order):
            if episode.rating_key == key:
                following = self._order[i + 1] if i + 1 < len(self._order) else None
                return following[1] if following and following[0] == show else None
        raise PlexItemNotFound("Item not found in Plex")


@pytest.fixture
def plex(monkeypatch):
    fake = FakePlex()
    fake.add_movie("100", "Brightwater")
    fake.add_movie("101", "Glasshouse")
    fake.add_movie("900", "Hidden Film", section="9")
    fake.add_show("500", "The Long Coast", {1: ("501", ["511", "512"]), 2: ("502", ["521", "522"])})
    fake.add_show("600", "Spirit Lanes", {1: ("601", ["611"])})
    fake.add_show("700", "Hidden Show", {1: ("701", ["711"])}, section="9")
    monkeypatch.setattr(pages, "PlexWatch", lambda *_a, **_k: fake)
    monkeypatch.setattr(pages, "_next_episode_cache", {})
    monkeypatch.setattr(pages, "_reachable_until", [0.0])
    return fake


@pytest.fixture
def library(conn):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1", "2"]')
    old = NOW - timedelta(days=200)
    insert_media_item(conn, id="100", plex_rating_key="100", title="Brightwater", added_at=old)
    insert_media_item(
        conn, id="101", plex_rating_key="101", title="Glasshouse", added_at=NOW - timedelta(days=2)
    )
    insert_media_item(
        conn, id="900", plex_rating_key="900", title="Hidden Film", plex_library_id=9, added_at=old
    )
    for season_key, number in (("501", 1), ("502", 2)):
        insert_media_item(
            conn,
            id=season_key,
            plex_rating_key=season_key,
            title="The Long Coast",
            show_title="The Long Coast",
            media_type="tv_season",
            season_number=number,
            show_rating_key="500",
            plex_library_id=2,
            added_at=old,
        )
    insert_media_item(
        conn,
        id="601",
        plex_rating_key="601",
        title="Spirit Lanes",
        show_title="Spirit Lanes",
        media_type="anime_season",
        season_number=1,
        show_rating_key="600",
        plex_library_id=2,
        added_at=old,
    )
    insert_media_item(
        conn,
        id="701",
        plex_rating_key="701",
        title="Hidden Show",
        show_title="Hidden Show",
        media_type="tv_season",
        season_number=1,
        show_rating_key="700",
        plex_library_id=9,
        added_at=old,
    )
    insert_admin_user(conn, username="aoife", role="viewer")
    insert_admin_user(conn, username="cian", role="viewer")
    return conn


def _schedule(conn, media_item_id: str, days: int) -> None:
    insert_scheduled_action(
        conn,
        media_item_id=media_item_id,
        execute_at=(NOW + timedelta(days=days)).isoformat(),
        token=f"tok-{media_item_id}",
    )


def _play(conn, user, key, *, position, duration, item, show=None, minutes_ago=10) -> None:
    save_progress(
        conn,
        username=user,
        rating_key=key,
        media_item_id=item,
        show_rating_key=show,
        position_s=position,
        duration_s=duration,
        played_s=position,
        now=NOW - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def get(app_factory, conn, templates_stub, library, plex, monkeypatch):
    monkeypatch.setattr(pages, "now_utc", lambda: NOW)
    app = app_factory(pages.router, conn=conn, state_extras={"templates": templates_stub})

    def _get(path: str, user: str = "aoife"):
        client = TestClient(app)
        client.cookies.set("session_token", create_session(conn, user))
        return client.get(path)

    return _get


def _rows(ctx: dict) -> dict[str, list[dict]]:
    return {row["title"]: row["items"] for row in ctx["rows"]}


def _titles(items: list[dict]) -> list[str]:
    return [i["title"] for i in items]


class TestWatchRows:
    def test_rows_come_in_order_with_all_their_items(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")
        _schedule(conn, "502", 2)

        ctx = get("/watch").json()

        assert [r["title"] for r in ctx["rows"]] == [
            "Continue watching",
            "Leaving soon",
            "Recently added",
            "Movies",
            "TV",
            "Anime",
        ]
        rows = _rows(ctx)
        assert _titles(rows["Movies"]) == ["Brightwater", "Glasshouse"]
        assert _titles(rows["Recently added"]) == ["Glasshouse"]
        assert rows["Continue watching"][0]["href"] == "/watch/play/100?resume=1&from=%2Fwatch"
        assert rows["Continue watching"][0]["minutes_left"] == 80
        assert ctx["watch_error"] is None

    def test_anime_show_is_in_anime_row_only(self, get):
        rows = _rows(get("/watch").json())

        assert _titles(rows["Anime"]) == ["Spirit Lanes"]
        assert _titles(rows["TV"]) == ["The Long Coast"]
        assert rows["TV"][0]["href"] == "/watch/show/500"
        assert rows["TV"][0]["poster_url"] == "/api/poster/500"

    def test_scheduled_season_leaves_with_its_show_poster_and_season_keep_id(self, get, conn):
        _schedule(conn, "502", 2)
        _schedule(conn, "100", 5)

        leaving = _rows(get("/watch").json())["Leaving soon"]

        season, movie = leaving
        assert season["title"] == "The Long Coast"
        assert season["subtitle"] == "Season 2"
        assert season["keep_id"] == "502"
        assert season["poster_url"] == "/api/poster/500"
        assert season["leaving_days"] == 2
        assert movie["keep_id"] == "100"

    def test_items_outside_scanned_libraries_appear_nowhere(self, get, conn):
        _schedule(conn, "900", 1)
        _play(conn, "aoife", "900", position=1200, duration=6000, item="900")

        ctx = get("/watch").json()

        titles = {i["title"] for row in ctx["rows"] for i in row["items"]}
        assert "Hidden Film" not in titles
        assert "Hidden Show" not in titles

    def test_one_persons_progress_is_invisible_to_another(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        ctx = get("/watch", user="cian").json()

        assert "Continue watching" not in _rows(ctx)
        assert ctx["banner"]["primary"]["label"] == "Play"
        assert ctx["banner"]["title"] == "Glasshouse"
        assert ctx["banner"]["ref"] == "movie-101"

    def test_finished_episode_is_followed_by_the_next_unwatched_one(self, get, conn):
        _play(conn, "aoife", "512", position=3000, duration=3000, item="501", show="500")

        cw = _rows(get("/watch").json())["Continue watching"]

        assert cw == [
            {
                "kind": "show",
                "title": "The Long Coast",
                "subtitle": "S2 · E1",
                "poster_url": "/api/poster/500",
                "href": "/watch/play/521?from=%2Fwatch",
                "progress_pct": None,
                "minutes_left": None,
                "watched": False,
                "leaving_days": None,
                "keep_id": None,
            }
        ]

    def test_a_long_history_of_finished_shows_makes_a_bounded_number_of_plex_calls(
        self, get, conn, plex, monkeypatch
    ):
        calls: list[str] = []
        real = plex.next_episode
        monkeypatch.setattr(plex, "next_episode", lambda key: calls.append(key) or real(key))
        for n in range(60):
            show = str(3000 + n)
            insert_media_item(
                conn,
                id=f"s{n}",
                plex_rating_key=f"s{n}",
                title=f"Show {n}",
                show_title=f"Show {n}",
                media_type="tv_season",
                season_number=1,
                show_rating_key=show,
                plex_library_id=1,
            )
            _play(conn, "aoife", f"e{n}", position=3000, duration=3000, item=f"s{n}", show=show)

        assert get("/watch").status_code == 200
        assert 0 < len(calls) <= pages._PLEX_LOOKUP_BUDGET

    def test_continue_watching_stops_filling_at_its_cap(self, get, conn):
        for n in range(30):
            _play(conn, "aoife", f"{4000 + n}", position=600, duration=6000, item=f"{4000 + n}")
            insert_media_item(
                conn, id=f"{4000 + n}", plex_rating_key=f"{4000 + n}", title=f"Film {n}"
            )

        cw = _rows(get("/watch").json())["Continue watching"]

        assert len(cw) == pages._CONTINUE_LIMIT

    def test_show_with_nothing_left_drops_out(self, get, conn):
        _play(conn, "aoife", "611", position=3000, duration=3000, item="601", show="600")

        assert "Continue watching" not in _rows(get("/watch").json())

    def test_banner_resumes_the_latest_episode_in_progress(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100", minutes_ago=60)
        _play(conn, "aoife", "522", position=1200, duration=3000, item="502", show="500")

        banner = get("/watch").json()["banner"]

        assert banner["title"] == "The Long Coast"
        assert banner["line"] == 'Season 2 · Episode 2 — "Episode 522" · 30 min left'
        assert banner["primary"] == {
            "label": "Resume",
            "href": "/watch/play/522?resume=1&from=%2Fwatch",
        }
        assert banner["episodes_href"] == "/watch/show/500?season=2"
        assert banner["progress_pct"] == 40
        assert banner["ref"] == "show-500"

    def test_the_banner_says_continue_watching_for_a_title_in_progress(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        assert get("/watch").json()["banner"]["kicker"] == "Continue watching"

    def test_the_newest_title_is_recently_added_only_within_the_rows_window(self, get, conn):
        assert get("/watch").json()["banner"]["kicker"] == "Recently added"

        added = (NOW - timedelta(days=pages.RECENT_DAYS + 10)).isoformat()
        conn.execute("UPDATE media_items SET added_at = ? WHERE id = '101'", (added,))
        conn.commit()
        ctx = get("/watch").json()

        assert "Recently added" not in _rows(ctx)
        assert ctx["banner"]["title"] == "Glasshouse"
        assert ctx["banner"]["kicker"] == "From your library"

    def test_plex_not_set_up_still_lists_the_library(self, get, conn):
        conn.execute("DELETE FROM settings WHERE key IN ('plex_url', 'plex_token')")
        conn.commit()

        ctx = get("/watch").json()

        assert ctx["watch_error"] == "not_set_up"
        assert "Movies" in _rows(ctx)

    def test_plex_unreachable_is_reported_and_rows_still_built(self, get, conn, plex):
        plex.error = PlexUnavailable("Plex could not be reached")
        _play(conn, "aoife", "522", position=1200, duration=3000, item="502", show="500")

        ctx = get("/watch").json()

        assert ctx["watch_error"] is None
        assert ctx["plex_unreachable"] is True
        assert _rows(ctx)["Continue watching"][0]["href"].startswith("/watch/play/522?resume=1")
        assert "Movies" in _rows(ctx)

    def test_plex_down_is_reported_even_when_no_episode_needs_it(self, get, conn, plex):
        plex.error = PlexUnavailable("Plex could not be reached")
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        ctx = get("/watch").json()

        assert ctx["plex_unreachable"] is True
        assert "Continue watching" in _rows(ctx)

    def test_a_good_reachability_check_is_reused_for_a_while(self, get, plex, monkeypatch):
        pings = []
        monkeypatch.setattr(plex, "ping", lambda: pings.append(1), raising=False)

        first, second = get("/watch").json(), get("/watch").json()

        assert first["plex_unreachable"] is False and second["plex_unreachable"] is False
        assert len(pings) == 1

    def test_a_finished_show_is_not_looked_up_again_on_the_next_load(self, get, conn, plex):
        _play(conn, "aoife", "611", position=2900, duration=3000, item="601", show="600")
        lookups = []
        real = plex.next_episode
        plex.next_episode = lambda key: lookups.append(key) or real(key)

        get("/watch")
        ctx = get("/watch").json()

        assert len(lookups) == 1
        assert "Continue watching" not in _rows(ctx)

    def test_in_progress_movie_shows_its_bar_in_the_movies_row(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        movies = _rows(get("/watch").json())["Movies"]

        assert movies[0]["progress_pct"] == 20
        assert movies[1]["progress_pct"] is None

    def test_movie_captions_say_movie_and_use_hours(self, get, conn):
        _play(conn, "aoife", "100", position=6000 - 3120, duration=6000, item="100")
        _play(conn, "aoife", "101", position=1200, duration=6000, item="101", minutes_ago=20)

        cards = _rows(get("/watch").json())["Continue watching"]

        assert [c["subtitle"] for c in cards] == ["Movie · 52 min left", "Movie · 1 h 20 min left"]
        assert pages.duration_text(100) == "1 h 40 min"
        assert pages.duration_text(120) == "2 h"

    @pytest.mark.parametrize(
        "origin",
        [
            "//evil.example/x",
            "https://evil.example/",
            "/watch/../admin",
            "/watch?x=1",
            "/watch/show/5/x",
            "/watch#title=",
            "/watch#title=movie-1/x",
            "/watch#title=tmdb-show-1",
            "/watch#title=rec-1&x=1",
            "/watch#x=movie-1",
        ],
    )
    def test_back_origin_outside_the_allow_list_is_ignored(self, origin):
        assert pages.safe_origin(origin) is None

    @pytest.mark.parametrize(
        "origin",
        [
            "/watch#title=movie-100",
            "/watch#title=show-500",
            "/watch#title=tmdb-tv-7",
            "/watch#title=rec-3",
        ],
    )
    def test_back_may_return_to_a_title_open_on_the_watch_page(self, get, origin):
        ctx = get(f"/watch/play/511?from={quote(origin, safe='')}").json()

        assert ctx["back_href"] == origin

    def test_back_returns_to_where_the_person_came_from(self, get):
        origin = "/watch/show/500?season=2"
        ctx = get(f"/watch/play/511?from={quote(origin, safe='')}").json()

        assert ctx["back_href"] == origin
        assert get("/watch/play/511?from=https%3A%2F%2Fevil.example").json()["back_href"] == (
            "/watch/show/500?season=1"
        )


class TestShowPage:
    def test_episode_states_and_resume_of_the_episode_in_progress(self, get, conn):
        _play(
            conn,
            "aoife",
            "511",
            position=3000,
            duration=3000,
            item="501",
            show="500",
            minutes_ago=60,
        )
        _play(conn, "aoife", "512", position=1200, duration=3000, item="501", show="500")

        ctx = get("/watch/show/500").json()

        assert ctx["primary"] == {
            "label": "Resume S1 · E2",
            "href": "/watch/play/512?resume=1&from=%2Fwatch%2Fshow%2F500",
        }
        assert [s["selected"] for s in ctx["seasons"]] == [True, False]
        assert [e["state"] for e in ctx["episodes"]] == ["watched", "in_progress"]
        assert ctx["episodes"][1]["minutes_left"] == 30
        assert ctx["episodes"][0]["thumb_url"] == "/api/watch/thumb/511"
        assert ctx["show"]["meta_line"] == "TV · 2021–2024 · 2 seasons · Drama"

    def test_after_a_finished_episode_plays_the_next_one_in_its_season(self, get, conn):
        _play(conn, "aoife", "512", position=3000, duration=3000, item="501", show="500")

        ctx = get("/watch/show/500").json()

        assert ctx["primary"] == {
            "label": "Play S2 · E1",
            "href": "/watch/play/521?from=%2Fwatch%2Fshow%2F500",
        }
        assert [s["selected"] for s in ctx["seasons"]] == [False, True]
        assert [e["state"] for e in ctx["episodes"]] == ["none", "none"]

    def test_nothing_played_starts_at_the_first_episode(self, get):
        ctx = get("/watch/show/500?season=2").json()

        assert ctx["primary"] == {
            "label": "Play S1 · E1",
            "href": "/watch/play/511?from=%2Fwatch%2Fshow%2F500",
        }
        assert [e["number"] for e in ctx["episodes"]] == [1, 2]
        assert (
            ctx["episodes"][0]["href"] == "/watch/play/521?from=%2Fwatch%2Fshow%2F500%3Fseason%3D2"
        )

    def test_scheduled_season_shows_leaving(self, get, conn):
        _schedule(conn, "502", 3)

        assert get("/watch/show/500?season=1").json()["leaving"] is None
        assert get("/watch/show/500?season=2").json()["leaving"] == {"days": 3, "keep_id": "502"}

    @pytest.mark.parametrize("key", ["700", "999", "abc"])
    def test_show_outside_scanned_libraries_is_refused(self, get, key):
        assert get(f"/watch/show/{key}").status_code == 404

    def test_plex_unreachable_sets_the_error(self, get, plex):
        plex.error = PlexUnavailable("Plex could not be reached")

        ctx = get("/watch/show/500").json()

        assert ctx["watch_error"] == "unreachable"


class TestMoviePage:
    def test_in_progress_movie_offers_resume_with_time_left(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        ctx = get("/watch/movie/100").json()

        assert ctx["primary"] == {
            "label": "Resume (80 min left)",
            "href": "/watch/play/100?resume=1",
        }
        assert ctx["movie"]["minutes"] == 100

    def test_watched_movie_offers_play(self, get, conn):
        _play(conn, "aoife", "100", position=5900, duration=6000, item="100")

        assert get("/watch/movie/100").json()["primary"] == {
            "label": "Play",
            "href": "/watch/play/100",
        }

    def test_movie_gone_from_plex_is_no_longer_available(self, get, plex):
        del plex.items["100"]

        ctx = get("/watch/movie/100").json()

        assert ctx["not_available"] is True
        assert ctx["movie"] is None

    @pytest.mark.parametrize("key", ["900", "501", "999"])
    def test_movie_outside_scanned_libraries_is_refused(self, get, key):
        assert get(f"/watch/movie/{key}").status_code == 404


class TestPlayerPage:
    def test_resume_starts_at_the_saved_position(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        assert get("/watch/play/100?resume=1").json()["start_s"] == 1200

    def test_reloading_the_plain_address_keeps_the_saved_place(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        assert get("/watch/play/100").json()["start_s"] == 1200

    def test_a_deliberate_start_over_begins_at_zero(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        assert get("/watch/play/100?resume=0").json()["start_s"] == 0

    def test_a_title_just_begun_starts_at_zero(self, get, conn):
        _play(conn, "aoife", "100", position=5, duration=6000, item="100")

        assert get("/watch/play/100").json()["start_s"] == 0

    def test_a_finished_title_starts_at_zero_without_resume(self, get, conn):
        _play(conn, "aoife", "100", position=5900, duration=6000, item="100")

        assert get("/watch/play/100").json()["start_s"] == 0

    def test_resume_of_a_watched_item_starts_from_the_beginning(self, get, conn):
        _play(conn, "aoife", "100", position=5900, duration=6000, item="100")

        assert get("/watch/play/100?resume=1").json()["start_s"] == 0

    def test_resume_uses_only_the_persons_own_position(self, get, conn):
        _play(conn, "aoife", "100", position=1200, duration=6000, item="100")

        assert get("/watch/play/100?resume=1", user="cian").json()["start_s"] == 0

    def test_episode_has_next_show_link_and_its_season_leaving(self, get, conn):
        _schedule(conn, "501", 4)

        ctx = get("/watch/play/512").json()

        assert ctx["item"]["title"] == "The Long Coast"
        assert ctx["item"]["subtitle"] == "S1 · E2 — Episode 512"
        assert ctx["next"] == {"href": "/watch/play/521", "label": "S2 · E1 — Episode 521"}
        assert ctx["show_href"] == ctx["back_href"] == "/watch/show/500?season=1"
        assert ctx["leaving"] == {"days": 4, "keep_id": "501"}

    def test_next_episode_in_another_season_carries_that_season_as_the_origin(self, get):
        origin = quote("/watch/show/500?season=1", safe="")

        ctx = get(f"/watch/play/512?from={origin}").json()

        assert (
            ctx["next"]["href"]
            == f"/watch/play/521?from={quote('/watch/show/500?season=2', safe='')}"
        )
        assert ctx["back_href"] == "/watch/show/500?season=1"

    def test_next_episode_keeps_an_origin_that_has_no_season(self, get):
        ctx = get(f"/watch/play/512?from={quote('/watch', safe='')}").json()

        assert ctx["next"]["href"] == f"/watch/play/521?from={quote('/watch', safe='')}"

    def test_phone_banner_line_is_the_short_form(self, get, conn):
        _play(conn, "aoife", "522", position=1200, duration=3000, item="502", show="500")

        assert get("/watch").json()["banner"]["line_short"] == "S2 · E2 · 30 min left"

    def test_movie_goes_back_to_its_page(self, get):
        ctx = get("/watch/play/100").json()

        assert ctx["back_href"] == "/watch/movie/100"
        assert ctx["next"] is None
        assert ctx["show_href"] is None

    @pytest.mark.parametrize("key", ["900", "711", "abc"])
    def test_item_outside_scanned_libraries_is_refused(self, get, key):
        assert get(f"/watch/play/{key}").status_code == 404

    def test_item_gone_from_plex_has_no_error_code_and_no_player(self, get):
        ctx = get("/watch/play/123456").json()

        assert ctx["watch_error"] is None
        assert "item" not in ctx


def test_gone_and_unreachable_pages_say_so_in_plain_words(
    app_factory, conn, library, plex, monkeypatch
):
    monkeypatch.setattr(pages, "now_utc", lambda: NOW)
    del plex.shows["500"]
    app = app_factory(
        pages.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_WEB / "templates"))},
    )
    app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "aoife"))

    show = client.get("/watch/show/500")
    assert show.status_code == 200
    assert "This show is not in the library any more." in show.text

    # An episode is gone from Plex: the text must not call it a film.
    play = client.get("/watch/play/123456")
    assert play.status_code == 200
    assert "This is not in the library any more." in play.text
    assert "film" not in play.text.lower()

    plex.error = PlexUnavailable("Plex could not be reached")
    play = client.get("/watch/play/512")
    assert play.status_code == 200
    assert "Can't reach Plex right now." in play.text


def _show_html(app_factory, conn, monkeypatch, url: str, *scheduled: tuple[str, int]) -> str:
    monkeypatch.setattr(pages, "now_utc", lambda: NOW)
    for media_item_id, days in scheduled:
        _schedule(conn, media_item_id, days)
    app = app_factory(
        pages.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_WEB / "templates"))},
    )
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "aoife"))
    return client.get(url).text


def test_show_page_has_no_leaving_line_when_no_season_is_leaving(
    app_factory, conn, library, plex, monkeypatch
):
    html = _show_html(app_factory, conn, monkeypatch, "/watch/show/500?season=1")

    assert "data-watch-leaving" not in html


def test_a_season_that_is_not_leaving_says_quietly_which_one_is(
    app_factory, conn, library, plex, monkeypatch
):
    html = _show_html(app_factory, conn, monkeypatch, "/watch/show/500?season=1", ("502", 3))

    line = html[html.index("data-watch-leaving") : html.index('class="watch-actions"')]
    assert "Season 2 leaves in 3 days" in line
    assert "watch-leaving--other" in html
    assert "data-watch-keep" not in line
    assert "Leaving in" not in html


def test_pages_render_with_the_real_templates(app_factory, conn, library, plex, monkeypatch):
    monkeypatch.setattr(pages, "now_utc", lambda: NOW)
    _schedule(conn, "100", 2)
    _play(conn, "aoife", "512", position=1200, duration=3000, item="501", show="500")
    app = app_factory(
        pages.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_WEB / "templates"))},
    )
    app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "aoife"))

    for path, text in [
        ("/watch", "Leaving in 2 days"),
        ("/watch/show/500", "Resume S1 · E2"),
        ("/watch/movie/100", "Leaving in 2 days"),
        ("/watch/play/512?resume=1", 'data-start-s="1200'),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert text in resp.text, path
        assert "plex.invalid" not in resp.text


def _page(app_factory, conn, monkeypatch, url: str, user: str, *, guard: bool = True):
    """*url* rendered with the real templates behind the viewer guard, signed in as *user*."""
    monkeypatch.setattr(pages, "now_utc", lambda: NOW)
    app = app_factory(
        pages.router,
        list_page.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_WEB / "templates"))},
    )
    if guard:
        app.add_middleware(ViewerGuardMiddleware)
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, user))
    return client.get(url, follow_redirects=False)


@pytest.fixture
def admin(conn):
    insert_admin_user(conn, username="orla")
    return "orla"


class TestWatchShell:
    def test_only_an_admin_can_ask_for_the_list_view(self, get, admin):
        assert get("/watch?view=list", user=admin).json()["view"] == "list"
        assert get("/watch?view=list").json()["view"] == "rows"
        assert get("/watch?view=other", user=admin).json()["view"] == "rows"

    def test_the_search_is_trimmed_and_cut_to_a_sane_length(self, get):
        assert get("/watch?q=%20%20glass%20").json()["q"] == "glass"
        assert len(get("/watch?q=" + "a" * 5000).json()["q"]) == 200

    def test_library_rows_are_marked_to_sit_under_their_divider(self, get):
        rows = get("/watch").json()["rows"]

        assert {r["title"]: r["library"] for r in rows} == {
            "Recently added": False,
            "Movies": True,
            "TV": True,
            "Anime": True,
        }

    def test_page_data_says_whether_tmdb_is_set_up_and_links_settings_for_admins(
        self, get, conn, admin
    ):
        assert get("/watch").json()["page_data"]["tmdb_configured"] is False
        insert_settings(conn, tmdb_read_token="tok")

        viewer, owner = (
            get("/watch").json()["page_data"],
            get("/watch", user=admin).json()["page_data"],
        )

        assert viewer["tmdb_configured"] is True
        assert viewer["settings_tmdb_href"] is None
        assert owner["settings_tmdb_href"] == "/settings#setg-integrations"


def test_discover_sits_between_recently_added_and_the_library_rows(
    app_factory, conn, library, plex, monkeypatch
):
    insert_settings(conn, tmdb_read_token="tok")
    html = _page(app_factory, conn, monkeypatch, "/watch", "aoife").text

    recent = html.index('aria-label="Recently added"')
    slot = html.index('id="watch-discover-slot"')
    divider = html.index(">Your library<")
    movies = html.index('aria-label="Movies"')
    assert recent < slot < divider < movies
    assert "watch-row--skel" in html[slot:divider]


@pytest.mark.parametrize(
    ("tmdb", "picks", "as_admin", "rows", "recommended", "note"),
    [
        (True, True, False, 4, 1, 0),
        (False, True, False, 1, 1, 0),
        (True, False, False, 3, 0, 0),
        (False, False, False, 0, 0, 0),
        (False, False, True, 1, 0, 1),
        (True, False, True, 4, 0, 1),
    ],
)
def test_the_discover_skeleton_holds_a_place_for_each_row_that_will_arrive(
    app_factory,
    conn,
    library,
    plex,
    monkeypatch,
    admin,
    tmdb,
    picks,
    as_admin,
    rows,
    recommended,
    note,
):
    if tmdb:
        insert_settings(conn, tmdb_read_token="tok")
    if not picks:
        insert_settings(conn, suggestions_enabled="false")

    html = _page(app_factory, conn, monkeypatch, "/watch", admin if as_admin else "aoife").text

    slot = html[html.index('id="watch-discover-slot"') : html.index(">Your library<")]
    assert slot.count('class="watch-row watch-row--skel') == rows
    assert slot.count("watch-row--skel-tools") == recommended
    assert slot.count("watch-row--skel-note") == note
    assert ('aria-busy="true"' in slot) is (rows > 0)


def test_plex_not_set_up_keeps_the_search_and_discover_but_not_the_rows(
    app_factory, conn, library, plex, monkeypatch
):
    conn.execute("DELETE FROM settings WHERE key IN ('plex_url', 'plex_token')")
    insert_settings(conn, tmdb_read_token="tok")

    html = _page(app_factory, conn, monkeypatch, "/watch", "aoife").text

    assert "Plex isn't set up yet." in html
    assert 'id="watch-search"' in html
    assert "watch-row--skel" in html
    assert 'aria-label="Movies"' not in html
    assert "Your library" not in html


def test_an_admin_gets_the_view_switch_and_the_list_in_place_of_the_rows(
    app_factory, conn, library, plex, monkeypatch, admin
):
    rows = _page(app_factory, conn, monkeypatch, "/watch", admin).text
    listed = _page(app_factory, conn, monkeypatch, "/watch?view=list", admin).text

    assert 'href="/watch" data-view="rows" aria-current="page"' in rows
    assert re.search(r'id="watch-list"[^>]*hidden', rows)
    assert not re.search(r'id="watch-browse"[^>]*hidden', rows)
    assert 'href="/watch?view=list" data-view="list" aria-current="page"' in listed
    assert re.search(r'id="watch-browse"[^>]*hidden', listed)
    assert not re.search(r'id="watch-list"[^>]*hidden', listed)
    assert 'placeholder="Search your library"' in listed


def test_a_viewer_gets_no_view_switch_list_or_admin_wording_even_when_asking_for_the_list(
    app_factory, conn, library, plex, monkeypatch
):
    _schedule(conn, "100", 2)

    html = _page(app_factory, conn, monkeypatch, "/watch?view=list", "aoife").text

    assert 'aria-label="Movies"' in html
    for leak in ("watch-viewtoggle", 'id="watch-list"', "watch/list.js", "watch/admin.js"):
        assert leak not in html
    for word in ("Delete", "Size", "Deletion", "/settings"):
        assert word not in html


def test_the_list_fragment_is_for_admins_only(app_factory, conn, library, plex, monkeypatch, admin):
    # Without the guard in front, so the route's own check is what refuses.
    viewer = _page(app_factory, conn, monkeypatch, "/watch/list", "aoife", guard=False)
    owner = _page(app_factory, conn, monkeypatch, "/watch/list", admin)

    assert (viewer.status_code, viewer.headers["location"]) == (303, "/watch")
    assert owner.status_code == 200
