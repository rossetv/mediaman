"""A person's own picks on the Watch page: theirs alone to list, open, download and refresh,
with no exception for an admin, and the old household picks shown to no one."""

from __future__ import annotations

import json
from datetime import date

import pytest
import requests
from fastapi.testclient import TestClient

from mediaman.core.time import now_iso
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import ConfigDecryptError, SSRFRefused
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations import prompts, user_picks
from mediaman.services.watch_catalog import _arr, _discover, _library
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository.recommended import group_into_batches
from mediaman.web.routes import recommended
from mediaman.web.routes.recommended import api as recommended_api_module
from mediaman.web.routes.recommended import own
from mediaman.web.routes.watch import catalog_api, recommended_api
from tests.helpers.factories import (
    insert_media_item,
    insert_settings,
    insert_suggestion,
    insert_user_pick,
)
from tests.unit.web.watch.test_watch_catalog_api import DictCache, FakeArr, FakeTmdb

DAY = "2099-01-02"


@pytest.fixture
def arr(monkeypatch):
    fake = FakeArr()
    for module in (_arr.arr_build, recommended_api_module):
        monkeypatch.setattr(module, "build_radarr_from_db", lambda *_a: fake)
        monkeypatch.setattr(module, "build_sonarr_from_db", lambda *_a: fake)
    return fake


@pytest.fixture(autouse=True)
def _world(monkeypatch, arr):
    catalog.invalidate()
    recommended_api_module.reset_download_action_limiter()
    recommended_api_module.reset_share_token_limiter()
    monkeypatch.setattr(_library, "_tvdb", {})
    monkeypatch.setattr(_library, "_held", {})
    monkeypatch.setattr(_library, "sync", True)
    tmdb = FakeTmdb()
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: tmdb)
    monkeypatch.setattr(_discover, "_cache", lambda: DictCache())
    monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: {})
    yield
    catalog.invalidate()


@pytest.fixture
def people(app_factory, conn, db_path):
    """A signed-in client of each: viewers amy and bob, and the admin."""
    insert_settings(conn, plex_libraries='["1"]', openai_api_key="sk-test")
    app = app_factory(
        recommended_api.router,
        catalog_api.router,
        recommended.router,
        conn=conn,
        state_extras={"db_path": str(db_path)},
    )
    clients = {}
    for name, role in (("amy", "viewer"), ("bob", "viewer"), ("admin", "admin")):
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at, role)"
            " VALUES (?, 'x', '2020', ?)",
            (name, role),
        )
        conn.commit()
        clients[name] = TestClient(app)
        clients[name].cookies.set("session_token", create_session(conn, name))
    return clients


def _row(client, **params) -> dict:
    resp = client.get("/api/watch/recommended", params=params)
    assert resp.status_code == 200
    return resp.json()


def _titles(client, **params) -> list[str]:
    return [i["title"] for i in _row(client, **params)["items"]]


class TestRow:
    def test_each_person_sees_their_own_picks_then_the_shared_ones(self, people, conn):
        insert_suggestion(conn, title="Shared", tmdb_id=501, batch_id=DAY)
        insert_user_pick(conn, "amy", title="Amys", tmdb_id=502, batch_id=DAY)
        insert_user_pick(conn, "bob", title="Bobs", tmdb_id=503, batch_id=DAY)

        assert _titles(people["amy"]) == ["Amys", "Shared"]
        assert _titles(people["bob"]) == ["Bobs", "Shared"]
        assert _titles(people["admin"]) == ["Shared"]

    def test_a_batch_only_another_person_has_does_not_exist(self, people, conn):
        insert_suggestion(conn, title="Shared", batch_id="2099-01-01")
        insert_user_pick(conn, "bob", title="Bobs", batch_id=DAY)

        assert [b["id"] for b in _row(people["admin"])["batches"]] == ["2099-01-01"]
        resp = people["admin"].get("/api/watch/recommended", params={"batch": DAY})
        assert resp.status_code == 404

    def test_the_row_begins_with_the_persons_latest_picks_before_newer_shared_ones(
        self, people, conn
    ):
        insert_suggestion(conn, title="Shared new", tmdb_id=1, batch_id="2099-01-03")
        insert_user_pick(conn, "amy", title="Amys", tmdb_id=2, batch_id="2099-01-02")
        insert_suggestion(conn, title="Shared mid", tmdb_id=3, batch_id="2099-01-02")
        insert_user_pick(conn, "amy", title="Amys old", tmdb_id=4, batch_id="2099-01-01")

        body = _row(people["amy"])
        assert [i["title"] for i in body["items"]] == ["Amys", "Shared new"]
        assert (body["batch"], body["next_batch"]) == ("2099-01-03", "2099-01-02")
        assert [b["id"] for b in body["batches"]] == ["2099-01-03", "2099-01-02", "2099-01-01"]
        assert _titles(people["amy"], batch="2099-01-02", start="2099-01-03") == ["Shared mid"]
        assert _titles(people["amy"], batch="2099-01-01", start="2099-01-03") == ["Amys old"]
        # Chosen by itself, a batch has all of its own.
        assert _titles(people["amy"], batch="2099-01-02") == ["Amys", "Shared mid"]
        assert _titles(people["bob"]) == ["Shared new"]

    def test_the_latest_shared_picks_follow_newer_ones_of_the_persons_own(self, people, conn):
        insert_user_pick(conn, "amy", title="Amys", tmdb_id=2, batch_id="2099-01-03")
        insert_suggestion(conn, title="Shared", tmdb_id=1, batch_id="2099-01-02")

        body = _row(people["amy"])
        assert [i["title"] for i in body["items"]] == ["Amys", "Shared"]
        assert body["next_batch"] is None

    @pytest.mark.parametrize(
        ("batch", "titles"), [("2026-04-22", ["Amys", "Shared"]), ("2026-04-21", ["Shared"])]
    )
    def test_own_picks_over_28_days_old_no_longer_come_first(self, batch, titles):
        rows = [
            {"title": "Shared", "category": "trending", "batch_id": "2026-05-20"},
            {"title": "Amys", "category": "personal", "batch_id": batch},
        ]
        first = group_into_batches(rows, date(2026, 5, 20))[0]
        assert [i["title"] for i in first["items"]] == titles

    def test_the_card_says_it_is_an_ai_pick_and_where_its_detail_is(self, people, conn):
        insert_media_item(conn, id="9", plex_rating_key="9", title="Held")
        conn.execute("UPDATE media_items SET tmdb_id = 77 WHERE id = '9'")
        pick = insert_user_pick(conn, "amy", title="Held", tmdb_id=77, batch_id=DAY, reason="")
        rec = insert_suggestion(conn, title="Shared", tmdb_id=78, batch_id=DAY)

        own_card, shared_card = _row(people["amy"])["items"]

        assert own_card["ref"] == "movie-9"
        assert own_card["pick_ref"] == f"pick-{pick}"
        assert (own_card["ai_pick"], own_card["own"], own_card["rec_id"]) == (True, True, None)
        assert (shared_card["ai_pick"], shared_card["own"]) == (True, False)
        assert (shared_card["pick_ref"], shared_card["rec_id"]) == (f"rec-{rec}", rec)

    def test_the_titles_that_led_to_a_pick_are_not_in_the_row(self, people, conn):
        insert_user_pick(conn, "amy", batch_id=DAY, because_of='["Secret Film"]')
        assert "Secret Film" not in people["amy"].get("/api/watch/recommended").text

    def test_a_pick_radarr_no_longer_tracks_loses_its_mark_and_no_shared_one_does(
        self, people, conn
    ):
        pick = insert_user_pick(conn, "amy", tmdb_id=70, batch_id=DAY, downloaded_at=now_iso())
        rec = insert_suggestion(conn, tmdb_id=71, batch_id="2098-01-01", downloaded_at=now_iso())
        assert pick == rec

        _row(people["amy"])

        assert user_picks.fetch_pick(conn, "amy", pick).downloaded_at is None
        row = conn.execute("SELECT downloaded_at FROM suggestions").fetchone()
        assert row["downloaded_at"] is not None


class TestDetail:
    def test_the_owner_sees_the_reason_and_the_titles_that_led_to_it(self, people, conn):
        pick = insert_user_pick(
            conn, "amy", tmdb_id=12, reason="A heist.", because_of='["Alpha", "Beta"]'
        )

        body = people["amy"].get(f"/api/watch/title/pick-{pick}").json()

        assert (body["title"], body["reason"], body["ai_pick"]) == (
            "Glass Onion",
            "A heist.",
            True,
        )
        assert body["because_of"] == ["Alpha", "Beta"]
        assert body["download"]["request"] == {"url": f"/api/watch/picks/{pick}/download"}

    @pytest.mark.parametrize("other", ["bob", "admin"])
    def test_anyone_else_gets_the_answer_of_a_pick_that_does_not_exist(
        self, people, conn, arr, other
    ):
        pick = insert_user_pick(conn, "amy", tmdb_id=12)
        calls = (("get", "/api/watch/title/pick-{}"), ("post", "/api/watch/picks/{}/download"))
        for method, url in calls:
            theirs = getattr(people[other], method)(url.format(pick))
            missing = getattr(people[other], method)(url.format(pick + 1))
            assert theirs.status_code == missing.status_code == 404
            assert theirs.json() == missing.json()
        assert arr.movies == []

    def test_a_shared_pick_names_no_titles_and_a_library_title_is_no_ai_pick(self, people, conn):
        rec = insert_suggestion(conn, tmdb_id=12, reason="")
        body = people["amy"].get(f"/api/watch/title/rec-{rec}").json()
        assert (body["ai_pick"], body["reason"], body["because_of"]) == (True, None, [])
        assert people["amy"].get("/api/watch/title/tmdb-movie-12").json()["ai_pick"] is False


class TestDownload:
    def test_the_owner_downloads_it_and_the_mark_is_on_the_pick_alone(self, people, conn, arr):
        pick = insert_user_pick(conn, "amy", title="Glass Onion", tmdb_id=12)
        rec = insert_suggestion(conn, tmdb_id=99)
        assert pick == rec

        resp = people["amy"].post(f"/api/watch/picks/{pick}/download")

        assert resp.json()["ok"] is True
        assert [m["tmdbId"] for m in arr.movies] == [12]
        assert user_picks.fetch_pick(conn, "amy", pick).downloaded_at is not None
        assert conn.execute("SELECT downloaded_at FROM suggestions").fetchone()[0] is None

    def test_a_pick_is_marked_only_under_the_name_it_belongs_to(self, conn):
        for name in ("amy", "bob"):
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, created_at, role)"
                " VALUES (?, 'x', '2020', 'viewer')",
                (name,),
            )
        pick = insert_user_pick(conn, "amy", tmdb_id=12)

        user_picks.mark_downloaded(conn, "bob", pick, now_iso())

        assert user_picks.fetch_pick(conn, "amy", pick).downloaded_at is None

    @pytest.mark.parametrize(
        "url", ["/api/watch/picks/{}/download", "/api/recommended/{}/download"]
    )
    @pytest.mark.parametrize("number", [2**63, 10**30])
    def test_a_number_too_large_for_any_pick_is_refused(self, people, arr, url, number):
        assert people["amy"].post(url.format(number)).status_code == 422
        assert arr.movies == []


class TestOldHouseholdPicks:
    @pytest.mark.parametrize("who", ["amy", "admin"])
    def test_they_are_listed_for_no_one_and_cannot_be_opened_or_downloaded(self, people, conn, who):
        old = insert_suggestion(conn, title="Household", category="personal", batch_id=DAY)
        client = people[who]

        assert _row(client)["batches"] == []
        body = client.get("/api/recommended").json()
        assert (body["recommendations"], body["total"]) == ([], 0)
        assert client.get(f"/api/watch/title/rec-{old}").status_code == 404
        assert client.post(f"/api/recommended/{old}/download").status_code == 404
        if who == "admin":
            assert client.post(f"/api/recommended/{old}/share-token").status_code == 404


class TestRefresh:
    @pytest.fixture
    def openai(self, monkeypatch):
        """Runs the background thread at once and answers the model with one title."""

        class AtOnce:
            def __init__(self, *, target, args, **_kw):
                self.run = lambda: target(*args)

            def start(self):
                self.run()

        sent: list[str] = []

        def call(prompt, conn, use_web_search=False, *, secret_key=None):
            sent.append(prompt)
            return [{"title": "Glass Onion", "media_type": "movie", "reason": "A heist."}]

        monkeypatch.setattr(own.threading, "Thread", AtOnce)
        monkeypatch.setattr(own, "build_plex_from_db", lambda *_a: None)
        monkeypatch.setattr(prompts, "call_openai", call)
        monkeypatch.setattr("mediaman.services.media_meta.omdb.fetch_ratings", lambda *_a, **_k: {})
        monkeypatch.setattr(
            FakeTmdb,
            "search_results_or_raise",
            lambda *_a, **_k: [{"id": 12, "title": "Glass Onion"}],
            raising=False,
        )
        monkeypatch.setattr(
            FakeTmdb, "shape_card", staticmethod(TmdbClient.shape_card), raising=False
        )
        monkeypatch.setattr(
            FakeTmdb, "shape_detail", staticmethod(TmdbClient.shape_detail), raising=False
        )
        return sent

    def _play(self, conn, username, count):
        for n in range(count):
            if not conn.execute("SELECT 1 FROM media_items WHERE id = ?", (f"f{n}",)).fetchone():
                insert_media_item(conn, id=f"f{n}", plex_rating_key=f"f{n}", title=f"Film {n}")
            conn.execute(
                "INSERT INTO watch_progress (username, rating_key, media_item_id, watched,"
                " updated_at) VALUES (?, ?, ?, 1, ?)",
                (username, f"f{n}", f"f{n}", now_iso()),
            )
        conn.commit()

    def test_a_viewer_has_their_own_made_and_then_waits_a_week(self, people, conn, openai):
        self._play(conn, "amy", 5)
        amy = people["amy"]
        assert _row(amy)["my_refresh"]["available"] is True

        resp = amy.post("/api/watch/picks/refresh")
        assert (resp.status_code, resp.json()["status"]) == (202, "started")

        status = amy.get("/api/watch/picks/refresh").json()
        assert status["status"] == "done"
        assert (status["my_refresh"]["available"], status["my_refresh"]["reason"]) == (
            False,
            "wait",
        )
        assert status["my_refresh"]["next_available_at"]
        assert [p["title"] for p in user_picks.fetch_picks(conn, "amy")] == ["Glass Onion"]
        assert user_picks.fetch_picks(conn, "bob") == []

        again = amy.post("/api/watch/picks/refresh")
        assert (again.status_code, again.json()["error"]) == (429, "wait")
        assert len(openai) == 1

    @pytest.mark.parametrize("error", [None, RuntimeError("plex")])
    def test_the_persons_new_plex_plays_are_read_first_and_their_trouble_costs_nothing(
        self, people, conn, openai, monkeypatch, error
    ):
        self._play(conn, "amy", 5)
        asked = []

        def read(_conn, _key, *, username=None):
            asked.append((username, len(openai)))
            if error:
                raise error

        monkeypatch.setattr(own, "read_plays", read)

        people["amy"].post("/api/watch/picks/refresh")

        assert asked == [("amy", 0)]
        assert len(user_picks.fetch_picks(conn, "amy")) == 1

    @pytest.mark.parametrize(
        "error",
        [
            SSRFRefused("refused"),
            ConfigDecryptError("plex_token", ValueError("bad key")),
            requests.ConnectionError("down"),
            ValueError("bad url"),
        ],
    )
    def test_a_plex_client_that_cannot_be_built_costs_only_its_stars(
        self, people, conn, openai, monkeypatch, error
    ):
        self._play(conn, "amy", 5)

        def build(*_a):
            raise error

        monkeypatch.setattr(own, "build_plex_from_db", build)

        people["amy"].post("/api/watch/picks/refresh")

        status = people["amy"].get("/api/watch/picks/refresh").json()
        assert (status["status"], status["my_refresh"]["running"]) == ("done", False)
        assert len(user_picks.fetch_picks(conn, "amy")) == 1

    def test_one_that_gives_nothing_says_failed_and_may_be_tried_again(
        self, people, conn, openai, monkeypatch
    ):
        self._play(conn, "amy", 5)
        monkeypatch.setattr(prompts, "call_openai", lambda *_a, **_k: [])
        people["amy"].post("/api/watch/picks/refresh")

        status = people["amy"].get("/api/watch/picks/refresh").json()
        assert (status["status"], status["my_refresh"]["available"]) == ("failed", True)

    @pytest.mark.parametrize(
        ("plays", "setting", "reason"),
        [
            (4, {}, "not_enough_plays"),
            (5, {"openai_api_key": ""}, "not_configured"),
            (5, {"suggestions_enabled": "false"}, "disabled"),
        ],
    )
    def test_when_none_can_be_made_it_says_why_and_sends_nothing(
        self, people, conn, openai, plays, setting, reason
    ):
        self._play(conn, "amy", plays)
        conn.execute(
            "DELETE FROM settings WHERE key IN (SELECT value FROM json_each(?))",
            (json.dumps(list(setting)),),
        )
        insert_settings(conn, **setting)
        amy = people["amy"]

        resp = amy.post("/api/watch/picks/refresh")

        assert (resp.status_code, resp.json()["error"]) == (409, reason)
        assert _row(amy)["my_refresh"]["reason"] == reason
        assert openai == []
        assert conn.execute("SELECT COUNT(*) FROM user_pick_runs").fetchone()[0] == 0

    def test_it_takes_a_session(self, people, app_factory, conn):
        client = TestClient(people["amy"].app)
        for resp in (
            client.post("/api/watch/picks/refresh", follow_redirects=False),
            client.get("/api/watch/picks/refresh", follow_redirects=False),
            client.post("/api/watch/picks/1/download", follow_redirects=False),
        ):
            assert resp.status_code == 401
