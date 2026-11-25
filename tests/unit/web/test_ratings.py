"""Each person's own rating of a title, and the question at a film's or season's end."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mediaman
from mediaman.services import watch_catalog as catalog
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.csrf import CSRFOriginMiddleware
from mediaman.web.repository import ratings as repo
from mediaman.web.repository.watch_progress import save_progress
from mediaman.web.routes import ratings as routes
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_settings,
    insert_user_pick,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
BASE = "/api/watch/ratings"


class FakeTmdb:
    """TMDB's answers by (kind, id); an id it lacks is not found."""

    def __init__(self):
        self.titles = {}
        self.down = False

    def details(self, _client, kind, tmdb_id, **_kwargs):
        if self.down:
            raise catalog.TmdbUnavailable("down")
        if (kind, tmdb_id) not in self.titles:
            raise catalog.TitleNotFound("gone")
        return {"tmdb_seasons": [], **self.titles[kind, tmdb_id]}


@pytest.fixture
def tmdb(monkeypatch):
    fake = FakeTmdb()
    monkeypatch.setattr(routes.TmdbClient, "from_db", lambda *_a, **_k: object())
    monkeypatch.setattr(routes.catalog, "details", fake.details)
    monkeypatch.setattr(routes, "now_utc", lambda: NOW)
    return fake


def _item(conn, key, *, tmdb_id=None, year=None, episode_keys=None, **fields):
    insert_media_item(conn, id=key, plex_rating_key=key, **fields)
    conn.execute(
        "UPDATE media_items SET tmdb_id = ?, year = ?, episode_keys = ? WHERE id = ?",
        (tmdb_id, year, episode_keys, key),
    )
    conn.commit()


def _season(conn, key, number, episodes, **fields):
    _item(
        conn,
        key,
        title="The Long Coast",
        show_title="The Long Coast",
        media_type="tv_season",
        season_number=number,
        show_rating_key="500",
        episode_keys=json.dumps([{"key": k, "number": n, "path": k} for k, n in episodes]),
        **fields,
    )


@pytest.fixture
def as_user(app_factory, conn, tmdb):
    insert_settings(conn, plex_libraries='["1"]')
    _item(conn, "100", title="Brightwater", tmdb_id=603, year=2019)
    _item(conn, "101", title="Unmatched")
    _item(conn, "900", title="Hidden", tmdb_id=9, plex_library_id=9)
    _season(conn, "501", 1, [("511", 1), ("512", 2)], tmdb_id=44)
    app = app_factory(routes.router, conn=conn)

    def _client(username):
        client = TestClient(app)
        client.cookies.set("session_token", create_session(conn, username))
        return client

    for username in ("aoife", "cian"):
        insert_admin_user(conn, username=username, role="viewer")
    return _client


@pytest.fixture
def client(as_user):
    return as_user("aoife")


def _rate(client, ref, level):
    return client.put(f"{BASE}/{ref}", json={"level": level})


def _read(client, *refs):
    return client.get(BASE, params={"refs": ",".join(refs)}).json()["ratings"]


def _stored(conn):
    return [
        tuple(r)
        for r in conn.execute("SELECT media_type, tmdb_id, level, title, year FROM user_ratings")
    ]


def _finish(conn, key, *, show=None, item=None, position=3000.0, username="aoife"):
    save_progress(
        conn,
        username=username,
        rating_key=key,
        media_item_id=item or key,
        show_rating_key=show,
        position_s=position,
        duration_s=3000.0,
        played_s=position,
        now=NOW,
    )


def _ask(client, key):
    return client.post(f"{BASE}/ask/{key}").json()


class TestRating:
    def test_each_level_is_kept_with_the_librarys_own_name(self, client, conn):
        for level in (-2, -1, 1, 2):
            answer = _rate(client, "movie-100", level).json()
            del answer["was"]
            assert answer == {"ok": True, "media_type": "movie", "tmdb_id": 603, "level": level}
            assert _stored(conn) == [("movie", 603, level, "Brightwater", 2019)]

    def test_a_rating_can_be_changed_and_taken_back(self, client, conn):
        _rate(client, "movie-100", 2)
        _rate(client, "movie-100", -1)
        assert [r[2] for r in _stored(conn)] == [-1]
        assert client.delete(f"{BASE}/movie-100").json()["level"] is None
        assert _stored(conn) == []

    def test_each_change_answers_the_level_it_replaced_of_that_person_alone(self, as_user):
        mine, theirs = as_user("aoife"), as_user("cian")
        _rate(theirs, "movie-100", 1)
        assert _rate(mine, "movie-100", 2).json()["was"] is None
        assert _rate(mine, "movie-100", -2).json()["was"] == 2
        assert mine.delete(f"{BASE}/movie-100").json()["was"] == -2
        assert mine.delete(f"{BASE}/movie-100").json()["was"] is None
        assert theirs.delete(f"{BASE}/movie-100").json()["was"] == 1

    def test_any_other_level_is_refused(self, client, conn):
        for level in (0, 3, 1.5, "2", None):
            assert _rate(client, "movie-100", level).status_code == 422
        assert _stored(conn) == []

    def test_a_name_sent_by_the_page_is_refused(self, client, conn):
        body = {"level": 2, "title": "Anything"}
        assert client.put(f"{BASE}/movie-100", json=body).status_code == 422
        assert _stored(conn) == []

    def test_a_title_outside_the_library_is_named_by_tmdb(self, client, conn, tmdb):
        tmdb.titles["tv", 77] = {"title": "Kitchen", "year": 2021}
        _rate(client, "tmdb-tv-77", 1)
        assert _stored(conn) == [("tv", 77, 1, "Kitchen", 2021)]

    def test_a_rating_is_kept_while_tmdb_is_down_and_a_known_name_stays(self, client, conn, tmdb):
        tmdb.titles["tv", 77] = {"title": "Kitchen", "year": 2021}
        _rate(client, "tmdb-tv-77", 1)
        tmdb.down = True
        assert _rate(client, "tmdb-tv-77", 2).status_code == 200
        assert _stored(conn) == [("tv", 77, 2, "Kitchen", 2021)]

    def test_what_is_no_title_for_this_person_is_not_found(self, client, conn):
        insert_user_pick(conn, "cian", id=1, tmdb_id=8, media_type="movie", title="Theirs")
        for ref in (
            "tmdb-movie-5",
            "tmdb-person-1",
            "movie-900",
            "movie-404",
            "pick-1",
            "x",
        ):
            assert _rate(client, ref, 1).status_code == 404, ref
        assert _stored(conn) == []

    def test_an_own_pick_is_rated_as_the_title_it_is(self, client, conn):
        insert_user_pick(
            conn, "aoife", id=1, tmdb_id=8, media_type="movie", title="Mine", year=2000
        )
        _rate(client, "pick-1", -2)
        assert _stored(conn) == [("movie", 8, -2, "Mine", 2000)]

    def test_a_title_with_no_tmdb_id_cannot_be_rated(self, client):
        assert _rate(client, "movie-101", 1).json()["error"] == "no_tmdb_id"
        assert _read(client, "movie-101") == {"movie-101": {"level": None, "rateable": False}}

    def test_a_show_known_by_its_tvdb_id_is_rated_by_the_tmdb_id_found_for_it(self, client, conn):
        conn.execute("UPDATE media_items SET tmdb_id = NULL, tvdb_id = 7000 WHERE id = '501'")
        conn.execute(
            "INSERT INTO title_meta (id_type, id, tmdb_id, fetched_at)"
            " VALUES ('tvdb', 7000, 55, '2026')"
        )
        conn.commit()
        assert _rate(client, "show-500", 1).json()["tmdb_id"] == 55


class TestReading:
    def test_a_page_of_cards_reads_one_rating_under_every_ref_of_the_title(self, client):
        _rate(client, "movie-100", 2)
        assert _read(client, "movie-100", "tmdb-movie-603", "tmdb-tv-603", "tmdb-movie-0", "x") == {
            "movie-100": {"level": 2, "rateable": True},
            "tmdb-movie-603": {"level": 2, "rateable": True},
            "tmdb-tv-603": {"level": None, "rateable": True},
        }

    def test_too_many_titles_at_once_are_refused(self, client):
        refs = [f"tmdb-movie-{n}" for n in range(1, 102)]
        assert client.get(BASE, params={"refs": ",".join(refs)}).status_code == 400
        assert len(_read(client, *refs[:100])) == 100

    def test_a_rating_is_seen_and_taken_back_by_its_own_person_alone(self, as_user, conn):
        mine, theirs = as_user("aoife"), as_user("cian")
        _rate(mine, "movie-100", 2)
        assert _read(theirs, "movie-100")["movie-100"]["level"] is None
        theirs.delete(f"{BASE}/movie-100")
        assert _read(mine, "movie-100")["movie-100"]["level"] == 2
        ids = {n: repo.find_user_id(conn, n) for n in ("aoife", "cian")}
        assert repo.levels_for(conn, ids["aoife"]) == {("movie", 603): 2}
        assert repo.levels_for(conn, ids["cian"]) == {}


class TestAsking:
    def test_a_finished_film_is_asked_of_once(self, client, conn):
        _finish(conn, "100")
        assert _ask(client, "100") == {
            "ok": True,
            "ask": True,
            "ref": "movie-100",
            "media_type": "movie",
            "tmdb_id": 603,
            "title": "Brightwater",
            "season": None,
        }
        assert _ask(client, "100") == {"ok": True, "ask": False}

    def test_one_persons_question_leaves_anothers(self, as_user, conn):
        for name in ("aoife", "cian"):
            _finish(conn, "100", username=name)
        assert _ask(as_user("aoife"), "100")["ask"]
        assert _ask(as_user("cian"), "100")["ask"]

    def test_a_film_not_finished_is_not_asked_of_and_may_be_later(self, client, conn):
        _finish(conn, "100", position=600.0)
        assert not _ask(client, "100")["ask"]
        _finish(conn, "100")
        assert _ask(client, "100")["ask"]

    def test_a_rated_title_is_not_asked_of(self, client, conn):
        _finish(conn, "100")
        _rate(client, "movie-100", 1)
        assert not _ask(client, "100")["ask"]

    def test_a_season_is_asked_of_at_its_last_episode_and_each_season_once(
        self, client, conn, tmdb
    ):
        tmdb.titles["tv", 44] = {
            "tmdb_seasons": [{"number": 1, "episode_count": 2}, {"number": 2, "episode_count": 1}]
        }
        _season(conn, "502", 2, [("521", 1)], tmdb_id=44)
        _finish(conn, "511", show="500", item="501")
        assert not _ask(client, "511")["ask"]
        for key, item, number in (("512", "501", 1), ("521", "502", 2)):
            _finish(conn, key, show="500", item=item)
            answer = _ask(client, key)
            assert (answer["ask"], answer["ref"], answer["season"]) == (True, "show-500", number)
            assert not _ask(client, key)["ask"]

    def test_the_newest_episode_of_a_season_still_on_air_is_not_its_end(self, client, conn, tmdb):
        tmdb.titles["tv", 44] = {"tmdb_seasons": [{"number": 1, "episode_count": 8}]}
        _finish(conn, "512", show="500", item="501")
        assert not _ask(client, "512")["ask"]

    def test_a_season_of_unknown_length_is_asked_of_only_once_it_is_known(self, client, conn, tmdb):
        _finish(conn, "512", show="500", item="501")
        tmdb.down = True
        assert not _ask(client, "512")["ask"]
        tmdb.down = False
        for count in (None, 2):
            tmdb.titles["tv", 44] = {"tmdb_seasons": [{"number": 1, "episode_count": count}]}
            assert _ask(client, "512")["ask"] is (count == 2)

    def test_a_special_is_not_asked_of(self, client, conn, tmdb):
        tmdb.titles["tv", 44] = {"tmdb_seasons": [{"number": 0, "episode_count": 1}]}
        _season(conn, "503", 0, [("531", 1)], tmdb_id=44)
        _finish(conn, "531", show="500", item="503")
        assert not _ask(client, "531")["ask"]

    def test_a_title_of_a_library_not_scanned_is_not_asked_of(self, client, conn):
        _finish(conn, "900")
        assert not _ask(client, "900")["ask"]


def test_suggestions_lose_a_strong_no_and_put_a_title_rated_down_last():
    rows = [{"media_type": "movie", "tmdb_id": n} for n in (1, 2, 3)]
    rows += [{"media_type": "tv", "tmdb_id": 2}, {"media_type": "movie", "tmdb_id": None}]
    shown = repo.suggestible(rows, {("movie", 1): -1, ("movie", 2): -2, ("movie", 3): 2})
    assert [(r["media_type"], r["tmdb_id"]) for r in shown] == [
        ("movie", 3),
        ("tv", 2),
        ("movie", None),
        ("movie", 1),
    ]


class TestGuards:
    def test_someone_signed_out_is_refused(self, client):
        client.cookies.clear()
        assert client.get(BASE).status_code == 401
        assert _rate(client, "movie-100", 1).status_code == 401
        assert client.delete(f"{BASE}/movie-100").status_code == 401
        assert client.post(f"{BASE}/ask/100").status_code == 401

    def test_another_sites_page_cannot_rate(self, client, conn):
        client.app.add_middleware(CSRFOriginMiddleware)
        answer = client.put(
            f"{BASE}/movie-100", json={"level": 1}, headers={"Origin": "https://evil.example"}
        )
        assert answer.status_code == 403
        assert _stored(conn) == []

    def test_ratings_set_without_end_are_slowed_down(self, client):
        assert [_rate(client, "movie-100", 1).status_code for _ in range(61)][-2:] == [200, 429]

    def test_ratings_go_with_the_account(self, client, conn):
        _rate(client, "movie-100", 1)
        conn.execute("DELETE FROM admin_users WHERE username = 'aoife'")
        assert _stored(conn) == []


def test_nothing_that_deletes_or_keeps_reads_ratings():
    root = Path(mediaman.__file__).parent
    guarded = [
        *(root / "scanner").rglob("*.py"),
        *(root / "services" / "scheduled_actions").rglob("*.py"),
        *(root / "web" / "routes" / "library_api").rglob("*.py"),
        *(
            root / "web" / "repository" / f"{n}.py"
            for n in ("kept", "library_api", "delete_intents")
        ),
        *(root / "web" / "routes" / f"{n}.py" for n in ("keep", "kept", "kept_show")),
        root / "web" / "routes" / "watch" / "browse_api.py",
    ]
    assert len(guarded) > 20
    readers = []
    for path in guarded:
        text = path.read_text()
        named = {
            f"{getattr(node, 'module', '') or ''}.{alias.name}"
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        if "user_rating" in text or any("ratings" in name.split(".") for name in named):
            readers.append(path.name)
    assert readers == []
