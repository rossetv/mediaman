"""Films and whole shows marked watched outside the library: each person's own, and read
together with the library's marks once a title joins it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mediaman
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import disk_cache
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.csrf import CSRFOriginMiddleware
from mediaman.web.repository import seen_titles
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.ratings import RatedTitle, find_user_id
from mediaman.web.repository.watch_progress import (
    MarkTarget,
    get_progress,
    mark_watched,
    save_progress,
    show_progress,
    unmark_watched,
)
from mediaman.web.routes import ratings as rating_routes
from mediaman.web.routes import seen as routes
from mediaman.web.routes.watch import card_marks, mark_undo, marks_api
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_settings,
    insert_user_pick,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
BASE = "/api/watch/seen"
LIBS = ["1"]


def _seasons(*dates):
    return {
        "tmdb_seasons": [
            {"number": n, "episode_count": 8, "air_date": date} for n, date in enumerate(dates, 1)
        ]
    }


class FakeTmdb:
    def __init__(self):
        self.titles = {("movie", 603): {}, ("tv", 44): _seasons("2019-01-01", "2021-01-01", None)}
        self.down = False
        self.held = True
        self.asked = []

    def details(
        self, _client, kind, tmdb_id, *, allow_fetch=None, anew=False, background=False, **_k
    ):
        if background:
            self.asked.append(tmdb_id)
            self.held = True
        if self.down:
            raise catalog.TmdbUnavailable("down")
        if allow_fetch is not None and not allow_fetch() and not self.held:
            raise disk_cache.Refused("not held")
        if (kind, tmdb_id) not in self.titles:
            raise catalog.TitleNotFound("gone")
        if anew:
            self.asked.append("anew")
        return {
            "title": "Long Coast",
            "year": 2019,
            "tmdb_seasons": [],
            **self.titles[kind, tmdb_id],
        }


@pytest.fixture
def tmdb(monkeypatch):
    fake = FakeTmdb()
    monkeypatch.setattr(rating_routes.TmdbClient, "from_db", lambda *_a, **_k: object())
    monkeypatch.setattr(rating_routes.catalog, "details", fake.details)
    monkeypatch.setattr(routes, "now_utc", lambda: NOW)
    monkeypatch.setattr(card_marks, "now_utc", lambda: NOW)
    monkeypatch.setattr(card_marks, "sync", True)
    monkeypatch.setattr(mark_undo, "_held", {})
    monkeypatch.setattr(marks_api, "now_utc", lambda: NOW)
    return fake


@pytest.fixture
def as_user(app_factory, conn, tmdb):
    insert_settings(conn, plex_libraries='["1"]')
    for username in ("aoife", "cian"):
        insert_admin_user(conn, username=username, role="viewer")
    app = app_factory(routes.router, conn=conn)

    def _client(username):
        client = TestClient(app)
        client.cookies.set("session_token", create_session(conn, username))
        return client

    return _client


@pytest.fixture
def client(as_user):
    return as_user("aoife")


def _film(conn, key="100", tmdb_id=603, **fields):
    insert_media_item(conn, id=key, plex_rating_key=key, title="Brightwater", **fields)
    conn.execute("UPDATE media_items SET tmdb_id = ? WHERE id = ?", (tmdb_id, key))
    conn.commit()


def _season(conn, number, episodes, *, tmdb_id=44, listed=True, **fields):
    key = f"50{number}"
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title="Long Coast",
        show_title="Long Coast",
        media_type="tv_season",
        season_number=number,
        show_rating_key="500",
        **fields,
    )
    conn.execute(
        "UPDATE media_items SET tmdb_id = ?, episode_count = ?, episode_keys = ? WHERE id = ?",
        (
            tmdb_id,
            len(episodes) if listed else None,
            json.dumps([{"key": k, "number": n, "path": k} for n, k in enumerate(episodes, 1)])
            if listed
            else None,
            key,
        ),
    )
    conn.commit()


def _play(conn, key, *, position=600.0, item="501", show="500", now=NOW):
    save_progress(
        conn,
        username="aoife",
        rating_key=key,
        media_item_id=item,
        show_rating_key=show,
        position_s=position,
        duration_s=3000.0,
        played_s=20.0,
        now=now,
    )


def _stored(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT user_id, media_type, tmdb_id, through_season FROM user_seen_titles"
        )
    ]


def _mark_of(conn, username="aoife"):
    return seen_titles.get_mark(conn, find_user_id(conn, username), ("tv", 44))


def _show(conn, username="aoife"):
    return lib.show_seen(conn, username, LIBS)["500"]


class TestMarking:
    def test_a_film_is_marked_and_taken_back_by_its_own_person_alone(self, as_user, conn):
        aoife, cian = as_user("aoife"), as_user("cian")

        answer = aoife.put(f"{BASE}/tmdb-movie-603").json()
        cian.delete(f"{BASE}/tmdb-movie-603")

        assert answer == {
            "ok": True,
            "watched": True,
            "media_type": "movie",
            "tmdb_id": 603,
            "tmdb_ref": "tmdb-movie-603",
            "through_season": None,
        }
        assert _stored(conn) == [(find_user_id(conn, "aoife"), "movie", 603, None)]
        assert aoife.delete(f"{BASE}/tmdb-movie-603").json()["watched"] is False
        assert _stored(conn) == []

    def test_a_show_is_marked_up_to_the_latest_season_out_that_day_as_tmdb_says_now(
        self, client, conn, tmdb
    ):
        tmdb.titles["tv", 44] = _seasons("2019-01-01", NOW.date().isoformat(), "2026-11-26", None)

        assert client.put(f"{BASE}/tmdb-tv-44").json()["through_season"] == 2
        assert tmdb.asked == ["anew"]

    def test_marking_again_never_takes_a_season_back(self, client, conn, tmdb):
        client.put(f"{BASE}/tmdb-tv-44")
        tmdb.titles["tv", 44] = _seasons("2019-01-01")

        assert client.put(f"{BASE}/tmdb-tv-44").json()["through_season"] == 2

    @pytest.mark.parametrize("seasons", [[], ["2027-01-01"]])
    def test_a_show_whose_seasons_are_not_known_is_refused(self, client, conn, tmdb, seasons):
        tmdb.titles["tv", 44] = _seasons(*seasons)

        answer = client.put(f"{BASE}/tmdb-tv-44")

        assert (answer.status_code, answer.json()["error"]) == (503, "seasons_unknown")
        assert _stored(conn) == []

    def test_with_tmdb_away_a_film_is_marked_and_a_show_is_not(self, client, conn, tmdb):
        tmdb.down = True

        assert client.put(f"{BASE}/tmdb-movie-603").status_code == 200
        assert client.put(f"{BASE}/tmdb-tv-44").status_code == 503
        assert [row[1] for row in _stored(conn)] == ["movie"]

    def test_an_own_pick_is_marked_as_the_title_it_is_and_anothers_is_not_found(
        self, as_user, conn
    ):
        insert_user_pick(conn, "cian", title="Brightwater", tmdb_id=603, media_type="movie")
        pick = conn.execute("SELECT id FROM user_picks").fetchone()[0]

        assert as_user("aoife").put(f"{BASE}/pick-{pick}").status_code == 404
        assert as_user("cian").put(f"{BASE}/pick-{pick}").json()["tmdb_id"] == 603

    def test_undoing_a_later_mark_leaves_the_earlier_one(self, client, conn, tmdb):
        client.put(f"{BASE}/tmdb-tv-44")
        before = _mark_of(conn)
        tmdb.titles["tv", 44] = _seasons("2019-01-01", "2021-01-01", "2022-01-01")
        client.put(f"{BASE}/tmdb-tv-44")

        undone = client.delete(f"{BASE}/tmdb-tv-44?undo=1")

        assert (undone.json()["watched"], _mark_of(conn)) == (False, before)

    def test_a_mark_repeated_leaves_its_undo(self, client, conn, monkeypatch):
        client.put(f"{BASE}/tmdb-movie-603")
        monkeypatch.setattr(routes, "now_utc", lambda: NOW + timedelta(seconds=1))
        client.put(f"{BASE}/tmdb-movie-603")

        client.delete(f"{BASE}/tmdb-movie-603?undo=1")

        assert _stored(conn) == []

    def test_undoing_what_was_taken_back_gives_it_back_as_it_was(self, client, conn, tmdb):
        user = find_user_id(conn, "aoife")
        seen_titles.mark(conn, user, RatedTitle("tv", 44), 1, NOW, in_library=True)
        before = _mark_of(conn)
        client.delete(f"{BASE}/tmdb-tv-44")

        client.put(f"{BASE}/tmdb-tv-44?undo=1")

        assert _mark_of(conn) == before
        assert tmdb.asked == []

    def test_an_undo_is_the_persons_own_and_of_the_last_change_alone(self, as_user, conn):
        aoife, cian = as_user("aoife"), as_user("cian")
        aoife.put(f"{BASE}/tmdb-movie-603")
        assert cian.delete(f"{BASE}/tmdb-movie-603?undo=1").json()["error"] == "too_late"
        for call in (aoife.delete, aoife.put, aoife.delete):
            call(f"{BASE}/tmdb-movie-603")

        assert aoife.put(f"{BASE}/tmdb-movie-603?undo=1").status_code == 200
        assert aoife.delete(f"{BASE}/tmdb-movie-603?undo=1").status_code == 409
        assert [row[1:3] for row in _stored(conn)] == [("movie", 603)]

    def test_an_undo_works_though_the_title_joined_the_library_meanwhile(self, client, conn):
        client.put(f"{BASE}/tmdb-movie-603")
        _film(conn)

        assert client.delete(f"{BASE}/tmdb-movie-603?undo=1").status_code == 200
        assert _stored(conn) == []

    @pytest.mark.parametrize("ref", ["tmdb-movie-999", "tmdb-movie-0", "tmdb-music-1", "603"])
    def test_what_is_no_title_is_not_found(self, client, conn, ref):
        assert client.put(f"{BASE}/{ref}").status_code == 404
        assert _stored(conn) == []

    def test_a_title_the_library_holds_names_the_mark_of_its_own(self, client, conn):
        _film(conn)
        _season(conn, 1, ["511"])
        held = {"tmdb-movie-603": ("movie", "100"), "tmdb-tv-44": ("show", "500")}

        for ref, target in held.items():
            for call in (client.put, client.delete):
                answer = call(f"{BASE}/{ref}")
                assert answer.status_code == 409
                body = answer.json()
                assert (body["error"], body["mark_kind"], body["mark_key"]) == (
                    "in_library",
                    *target,
                )
        assert _stored(conn) == []

    def test_a_title_of_a_library_not_scanned_is_marked_as_one_not_held(self, client, conn):
        _film(conn, plex_library_id=9)

        assert client.put(f"{BASE}/tmdb-movie-603").status_code == 200


class TestJoiningTheLibrary:
    def test_a_film_reads_as_watched_and_nothing_is_written_for_it(self, client, conn):
        client.put(f"{BASE}/tmdb-movie-603")
        _film(conn)
        # Plex counts its items as TMDB does its films: this one is another film.
        _film(conn, key="603", tmdb_id=5)

        assert lib.seen_refs(conn, "aoife", LIBS) == {"movie-100"}
        assert lib.seen_refs(conn, "cian", LIBS) == set()
        assert conn.execute("SELECT count(*) FROM watch_progress").fetchone()[0] == 0

    def test_a_film_begun_again_stays_watched(self, client, conn):
        client.put(f"{BASE}/tmdb-movie-603")
        _film(conn)
        _play(conn, "100", item="100", show=None)

        assert lib.seen_refs(conn, "aoife", LIBS) == {"movie-100"}

    def test_a_show_reads_as_watched_up_to_its_season_and_a_newer_one_does_not(self, client, conn):
        client.put(f"{BASE}/tmdb-tv-44")
        _season(conn, 1, ["511", "512"])
        _season(conn, 2, ["521"])
        assert _show(conn).watched
        assert lib.seen_refs(conn, "aoife", LIBS) == {"show-500"}
        assert lib.seen_refs(conn, "cian", LIBS) == set()
        assert seen_titles.with_covered(conn, "cian", LIBS, "500", {}) == {}

        _season(conn, 3, ["531", "532"])

        show = _show(conn)
        assert [show.seasons[n].watched for n in (1, 2, 3)] == [True, True, False]
        assert (show.watched, show.left) == (False, 2)
        assert conn.execute("SELECT count(*) FROM watch_progress").fetchone()[0] == 0

    def test_a_show_known_by_its_tvdb_id_is_matched_by_the_tmdb_id_found_for_it(self, client, conn):
        client.put(f"{BASE}/tmdb-tv-44")
        _season(conn, 1, ["511"], tmdb_id=None)
        conn.execute("UPDATE media_items SET tvdb_id = 7")
        conn.execute(
            "INSERT INTO title_meta (id_type, id, tmdb_id, fetched_at) VALUES ('tvdb', 7, 44, '')"
        )

        assert _show(conn).watched

    def test_a_season_not_counted_yet_is_watched_too(self, client, conn):
        client.put(f"{BASE}/tmdb-tv-44")
        _season(conn, 1, [], listed=False)

        assert _show(conn).watched

    @pytest.mark.parametrize("reach", [None, {1: {1}}])
    def test_of_a_season_not_listed_less_than_the_whole_is_not_watched(self, conn, as_user, reach):
        _season(conn, 1, [], listed=False)
        user, title = find_user_id(conn, "aoife"), RatedTitle("tv", 44)
        seen_titles.mark(conn, user, title, 2, NOW, in_library=bool(reach), episodes=reach)
        if reach is None:
            part = seen_titles.ShowPart("500", 1, 1, frozenset({1, 2}))
            seen_titles.change_part(conn, user, ("tv", 44), part, seen=False)

        assert not _show(conn).watched

    def test_an_episode_begun_again_is_not_seen_until_it_ends(self, client, conn):
        client.put(f"{BASE}/tmdb-tv-44")
        _season(conn, 1, ["511", "512"])
        _play(conn, "511")
        assert (_show(conn).watched, _show(conn).left) == (False, 1)

        _play(conn, "511", position=3000.0)

        assert _show(conn).watched

    def test_each_covered_episode_reads_as_seen_beside_what_was_played(self, client, conn):
        client.put(f"{BASE}/tmdb-tv-44")
        _season(conn, 1, ["511", "512"])
        _season(conn, 3, ["531"])
        _play(conn, "511")
        held = show_progress(conn, "aoife", "500")

        read = seen_titles.with_covered(conn, "aoife", LIBS, "500", held)

        assert {key: row.seen for key, row in read.items()} == {"511": False, "512": True}
        assert read["511"] == held["511"]
        assert not read["512"].played


class TestMarkedInTheLibrary:
    def _mark(self, conn, *numbers):
        """Season 1's episodes of those *numbers*, as marking the show does."""
        targets = [MarkTarget(f"51{n}", "501", "500", 0) for n in numbers]
        mark_watched(conn, "aoife", targets, NOW)
        user = find_user_id(conn, "aoife")
        title = RatedTitle("tv", 44)
        seen_titles.mark(conn, user, title, 1, NOW, in_library=True, episodes={1: numbers})

    def test_a_new_episode_of_a_marked_season_is_not_seen(self, conn, as_user):
        _season(conn, 1, ["511", "512"])
        self._mark(conn, 1)

        assert (_show(conn).watched, _show(conn).left) == (False, 1)
        assert seen_titles.with_covered(conn, "aoife", LIBS, "500", {}) == {}

    def test_marked_again_without_a_season_it_keeps_what_it_reached_of_it(self, conn, as_user):
        self._mark(conn, 1)
        user, title = find_user_id(conn, "aoife"), RatedTitle("tv", 44)
        seen_titles.mark(conn, user, title, 2, NOW, in_library=True, episodes={2: {1}})
        assert _mark_of(conn).episodes == {1: {1}, 2: {1}}

        seen_titles.mark(conn, user, title, 2, NOW, in_library=False)

        assert _mark_of(conn).episodes == {}

    def test_taking_back_an_episode_it_never_reached_leaves_it(self, conn, as_user):
        self._mark(conn, 1)
        part = seen_titles.ShowPart("500", 1, 2, frozenset({1, 2}))
        user = find_user_id(conn, "aoife")

        assert seen_titles.change_part(conn, user, ("tv", 44), part, seen=False) is None

    def test_all_it_reached_of_its_seasons_taken_back_ends_it(self, conn, as_user):
        self._mark(conn, 1)
        part = seen_titles.ShowPart("500", 1, 1, frozenset({1, 2}))
        user = find_user_id(conn, "aoife")
        seen_titles.change_part(conn, user, ("tv", 44), part, seen=False)

        assert _stored(conn) == []

    def test_once_deleted_and_back_under_new_keys_it_is_watched(self, conn, as_user):
        self._mark(conn, 1, 2)
        conn.execute("DELETE FROM watch_progress")
        _season(conn, 1, ["611", "612"])
        assert _show(conn).watched

        _play(conn, "611", position=3000.0, now=NOW + timedelta(days=1))

        assert _show(conn).watched


class TestCards:
    def _watched(self, conn, key, library_ref=None, *, watched=False, client=True):
        marks = card_marks.card_marks(
            seen_titles.person_marks(conn, "aoife"), object() if client else None
        )
        return marks(key, library_ref, watched)

    def test_a_card_carries_its_persons_own_rating_and_mark(self, client, conn):
        client.put(f"{BASE}/tmdb-movie-603")
        for name, level in (("aoife", 2), ("cian", -2)):
            conn.execute(
                "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, updated_at)"
                " VALUES (?, 'movie', 603, ?, '')",
                (find_user_id(conn, name), level),
            )

        assert self._watched(conn, ("movie", 603)) == {
            "tmdb_ref": "tmdb-movie-603",
            "level": 2,
            "watched": True,
        }
        assert self._watched(conn, ("tv", 603))["watched"] is False
        assert self._watched(conn, None) == {"tmdb_ref": None, "level": None, "watched": False}

    def test_a_show_outside_the_library_is_unwatched_once_a_newer_season_is_out(
        self, client, conn, tmdb
    ):
        client.put(f"{BASE}/tmdb-tv-44")
        tmdb.titles["tv", 44] = _seasons("2019-01-01", "2021-01-01", "2026-11-26")
        assert self._watched(conn, ("tv", 44))["watched"] is True

        tmdb.titles["tv", 44] = _seasons("2019-01-01", "2021-01-01", "2026-11-25")

        assert self._watched(conn, ("tv", 44))["watched"] is False

    def test_seasons_not_held_are_asked_for_after_the_reply_and_once(self, client, conn, tmdb):
        client.put(f"{BASE}/tmdb-tv-44")
        tmdb.titles["tv", 44] = _seasons("2019-01-01", "2021-01-01", "2022-01-01")
        tmdb.asked, tmdb.held = [], False

        assert self._watched(conn, ("tv", 44))["watched"] is True
        assert self._watched(conn, ("tv", 44))["watched"] is False
        assert tmdb.asked == [44]

    @pytest.mark.parametrize("down", [True, False])
    def test_a_show_stays_watched_while_its_seasons_cannot_be_read(self, client, conn, tmdb, down):
        client.put(f"{BASE}/tmdb-tv-44")
        tmdb.down = down

        assert self._watched(conn, ("tv", 44), client=down)["watched"] is True

    def test_of_a_show_in_the_library_its_own_rows_say(self, client, conn):
        client.put(f"{BASE}/tmdb-tv-44")

        assert self._watched(conn, ("tv", 44), "show-500")["watched"] is False
        assert self._watched(conn, ("tv", 44), "show-500", watched=True)["watched"] is True

    def test_of_a_film_held_without_an_id_its_own_row_says_too(self, client, conn):
        client.put(f"{BASE}/tmdb-movie-603")

        assert self._watched(conn, ("movie", 603), "movie-100")["watched"] is False

    def test_a_show_of_which_a_part_was_taken_back_is_not_watched(self, client, conn):
        client.put(f"{BASE}/tmdb-tv-44")
        part = seen_titles.ShowPart("500", 1, 3, frozenset({1, 2, 3}))
        user = find_user_id(conn, "aoife")
        seen_titles.change_part(conn, user, ("tv", 44), part, seen=False)
        assert self._watched(conn, ("tv", 44))["watched"] is False

        client.put(f"{BASE}/tmdb-tv-44")

        assert self._watched(conn, ("tv", 44))["watched"] is True

    def test_an_answer_of_tmdb_held_from_before_dates_says_nothing(self):
        old = {"tmdb_seasons": [{"number": 3, "episode_count": 8}]}
        assert card_marks.latest_season_out(old, NOW.date()) is None

    def test_a_show_with_no_dates_at_all_is_out_by_its_episodes(self):
        fields = _seasons(None, None)
        fields["tmdb_seasons"][1]["episode_count"] = 0
        assert card_marks.latest_season_out(fields, NOW.date()) == 1

    def test_a_persons_marks_are_read_at_once_however_many_cards(self, client, conn):
        client.put(f"{BASE}/tmdb-movie-603")
        asked = []
        conn.set_trace_callback(asked.append)
        marks = card_marks.card_marks(seen_titles.person_marks(conn, "aoife"), None)
        for n in range(50):
            marks(("movie", 600 + n), None, False)
        conn.set_trace_callback(None)
        assert len(asked) == 1


class TestGuards:
    def test_someone_signed_out_is_refused(self, client):
        client.cookies.clear()
        assert client.put(f"{BASE}/tmdb-movie-603").status_code == 401
        assert client.delete(f"{BASE}/tmdb-movie-603").status_code == 401

    def test_another_sites_page_cannot_mark(self, client, conn):
        client.app.add_middleware(CSRFOriginMiddleware)
        headers = {"Origin": "https://evil.example"}
        assert client.put(f"{BASE}/tmdb-movie-603", headers=headers).status_code == 403
        assert _stored(conn) == []

    def test_marks_set_without_end_are_slowed_down(self, client):
        codes = [client.put(f"{BASE}/tmdb-movie-603").status_code for _ in range(61)]
        assert codes[-2:] == [200, 429]

    def test_marks_go_with_the_account(self, client, conn):
        client.put(f"{BASE}/tmdb-movie-603")
        conn.execute("DELETE FROM admin_users WHERE username = 'aoife'")
        assert _stored(conn) == []

    @pytest.mark.parametrize("in_library", [True, False])
    def test_a_blank_row_speaks_only_of_a_mark_made_in_the_library(self, conn, as_user, in_library):
        """Such a row says that a mark was taken back."""
        _season(conn, 1, ["511", "512"])
        mark_watched(conn, "aoife", [MarkTarget("511", "501", "500", 0)], NOW)
        unmark_watched(conn, "aoife", "episode", "511")
        seen_titles.mark(
            conn, find_user_id(conn, "aoife"), RatedTitle("tv", 44), 1, NOW, in_library=in_library
        )

        assert _show(conn).watched is not in_library

    def test_a_mark_is_undone_only_where_it_was_not_changed_since(self, client, conn):
        user = find_user_id(conn, "aoife")
        first = seen_titles.mark(conn, user, RatedTitle("tv", 44), 1, NOW, in_library=True)
        seen_titles.mark(conn, user, RatedTitle("tv", 44), 2, NOW, in_library=True)

        seen_titles.restore(conn, first)

        assert [row[3] for row in _stored(conn)] == [2]


_READERS = {
    "db/schema_definition.py",
    "services/openai/recommendations/taste.py",
    "web/repository/seen_titles.py",
}


def test_nothing_that_scans_deletes_keeps_shares_or_mails_names_the_marks():
    root = Path(mediaman.__file__).parent
    naming = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "user_seen_titles" in path.read_text()
    }
    assert naming == _READERS


def test_a_tmdb_id_never_becomes_a_key_of_watch_progress(client, conn):
    client.put(f"{BASE}/tmdb-movie-603")
    client.put(f"{BASE}/tmdb-tv-44")
    client.delete(f"{BASE}/tmdb-tv-44")

    assert get_progress(conn, "aoife", ["603", "44"]) == {}
