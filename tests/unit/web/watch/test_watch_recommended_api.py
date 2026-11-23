"""The Watch "Recommended for you" JSON: every batch reachable, picks marked with their
library or download state, the refresh cooldown for admins only, and a 200 whatever state
Radarr and Sonarr are in."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.core.time import now_utc
from mediaman.services import watch_catalog as catalog
from mediaman.services.openai.recommendations.throttle import record_manual_refresh
from mediaman.services.watch_catalog import _arr
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import recommended_api
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_settings,
    insert_suggestion,
)
from tests.unit.web.watch.test_watch_catalog_api import FakeArr


@pytest.fixture(autouse=True)
def _fresh_memo():
    catalog.invalidate()
    yield
    catalog.invalidate()


@pytest.fixture
def arr(monkeypatch):
    fake = FakeArr()
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_a: fake)
    monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_a: fake)
    return fake


def _client(app_factory, conn, role: str) -> TestClient:
    insert_settings(conn, plex_libraries='["1"]')
    insert_admin_user(conn, username=role, role=role)
    client = TestClient(app_factory(recommended_api.router, conn=conn))
    client.cookies.set("session_token", create_session(conn, role))
    return client


@pytest.fixture
def viewer(app_factory, conn, arr):
    return _client(app_factory, conn, "viewer")


@pytest.fixture
def admin(app_factory, conn, arr):
    return _client(app_factory, conn, "admin")


def _get(client, **params) -> dict:
    resp = client.get("/api/watch/recommended", params=params)
    assert resp.status_code == 200
    return resp.json()


class TestBatches:
    def test_the_latest_batch_is_shown_with_personal_picks_before_trending(self, viewer, conn):
        insert_suggestion(conn, title="Trend", category="trending", batch_id="2099-01-02")
        insert_suggestion(conn, title="Mine", category="personal", batch_id="2099-01-02")
        insert_suggestion(conn, title="Old", batch_id="2098-12-01")
        conn.commit()

        body = _get(viewer)

        assert body["enabled"] is True
        assert body["batch"] == "2099-01-02"
        assert [i["title"] for i in body["items"]] == ["Mine", "Trend"]
        assert (body["batches"][0]["id"], body["batches"][0]["label"]) == (
            "2099-01-02",
            "Latest picks",
        )

    def test_every_batch_is_listed_and_reachable_not_just_the_newest_four(self, viewer, conn):
        for day in range(1, 8):
            insert_suggestion(conn, title=f"Pick {day}", batch_id=f"2099-01-0{day}")
        conn.commit()

        body = _get(viewer, batch="2099-01-01")

        assert len(body["batches"]) == 7
        assert body["batch"] == "2099-01-01"
        assert [i["title"] for i in body["items"]] == ["Pick 1"]
        assert body["batches"][-1]["date_label"] == "1 Jan"

    def test_an_unknown_batch_is_a_404(self, viewer, conn):
        insert_suggestion(conn, batch_id="2099-01-01")
        conn.commit()

        assert viewer.get("/api/watch/recommended", params={"batch": "nope"}).status_code == 404

    def test_no_picks_yet_is_an_empty_row(self, viewer):
        body = _get(viewer)

        assert (body["enabled"], body["batch"], body["batches"], body["items"]) == (
            True,
            None,
            [],
            [],
        )

    def test_when_recommendations_are_off_nothing_is_returned(self, viewer, conn):
        insert_suggestion(conn, title="Hidden", batch_id="2099-01-01")
        insert_settings(conn, suggestions_enabled="false")
        conn.commit()

        body = _get(viewer)

        assert body["enabled"] is False
        assert body["items"] == []
        assert body["batches"] == []


class TestCards:
    def test_a_pick_carries_its_ref_reason_and_proxied_poster(self, viewer, conn):
        rec_id = insert_suggestion(
            conn,
            title="Arrival",
            year=2016,
            tmdb_id=100,
            reason="Because you like quiet sci-fi",
            poster_url="https://image.tmdb.org/t/p/w500/abc.jpg",
            batch_id="2099-01-01",
        )
        conn.commit()

        (item,) = _get(viewer)["items"]

        assert item["rec_id"] == rec_id
        assert item["ref"] == f"rec-{rec_id}"
        assert item["reason"] == "Because you like quiet sci-fi"
        assert item["kind"] == "movie"
        assert item["year"] == 2016
        assert item["poster_url"] == "/api/watch/img/tmdb/w342/abc.jpg"
        assert item["in_library"] is False
        assert item["download_state"] is None

    def test_a_pick_the_library_holds_uses_the_library_ref(self, viewer, conn):
        insert_media_item(conn, id="100", plex_rating_key="100", title="Arrival")
        conn.execute("UPDATE media_items SET tmdb_id = 100 WHERE id = '100'")
        insert_suggestion(conn, title="Arrival", tmdb_id=100, batch_id="2099-01-01")
        conn.commit()

        (item,) = _get(viewer)["items"]

        assert (item["ref"], item["in_library"], item["library_ref"]) == (
            "movie-100",
            True,
            "movie-100",
        )
        assert item["href"] == "/watch/movie/100"

    def test_a_pick_without_a_tmdb_id_still_gets_a_card(self, viewer, conn):
        insert_suggestion(conn, title="No Id", tmdb_id=None, batch_id="2099-01-01")
        conn.commit()

        (item,) = _get(viewer)["items"]

        assert item["title"] == "No Id"
        assert item["ref"].startswith("rec-")

    def test_a_pick_radarr_is_fetching_shows_its_state(self, viewer, conn, arr):
        arr.add_movie(100, "Arrival")
        insert_suggestion(conn, title="Arrival", tmdb_id=100, batch_id="2099-01-01")
        conn.commit()

        (item,) = _get(viewer)["items"]

        assert item["download_state"] == "queued"

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_a_down_arr_stack_still_answers_200_without_badges(self, viewer, conn, arr, media_type):
        arr.down = True
        insert_suggestion(
            conn, title="Pick", media_type=media_type, tmdb_id=200, batch_id="2099-01-01"
        )
        conn.commit()

        (item,) = _get(viewer)["items"]

        assert item["title"] == "Pick"
        assert item["download_state"] is None

    def test_radarr_and_sonarr_are_read_once_for_every_pick_of_a_request(
        self, viewer, conn, arr, monkeypatch
    ):
        reads = []
        monkeypatch.setattr(
            _arr.arr_build, "build_radarr_from_db", lambda *_a: reads.append("radarr") or arr
        )
        monkeypatch.setattr(
            _arr.arr_build, "build_sonarr_from_db", lambda *_a: reads.append("sonarr") or arr
        )
        arr.add_movie(100, "Arrival")
        insert_suggestion(conn, title="Arrival", tmdb_id=100, batch_id="2099-01-01")
        insert_suggestion(conn, title="Show", media_type="tv", tmdb_id=200, batch_id="2099-01-01")
        conn.commit()

        items = _get(viewer)["items"]

        assert sorted(reads) == ["radarr", "sonarr"]
        assert [i["download_state"] for i in items] == ["queued", None]

    def test_a_pick_sent_for_download_that_radarr_no_longer_tracks_loses_the_mark(
        self, viewer, conn, arr
    ):
        rec_id = insert_suggestion(
            conn, title="Gone", tmdb_id=300, downloaded_at="2099-01-01", batch_id="2099-01-01"
        )
        conn.commit()

        (item,) = _get(viewer)["items"]

        assert item["download_state"] is None
        row = conn.execute("SELECT downloaded_at FROM suggestions WHERE id = ?", (rec_id,))
        assert row.fetchone()[0] is None

    def test_a_view_outdated_by_an_invalidate_does_not_clear_the_mark(
        self, viewer, conn, arr, monkeypatch
    ):
        rec_id = insert_suggestion(
            conn, title="Sent", tmdb_id=300, downloaded_at="2099-01-01", batch_id="2099-01-01"
        )
        conn.commit()
        real = catalog.arr_view

        def built_then_invalidated(*args, **kwargs):
            view = real(*args, **kwargs)
            catalog.invalidate()
            return view

        monkeypatch.setattr(catalog, "arr_view", built_then_invalidated)

        (item,) = _get(viewer)["items"]

        assert item["download_state"] == "queued"
        row = conn.execute("SELECT downloaded_at FROM suggestions WHERE id = ?", (rec_id,))
        assert row.fetchone()[0] == "2099-01-01"

    def test_with_radarr_down_a_pick_sent_for_download_still_shows_queued(self, viewer, conn, arr):
        arr.down = True
        rec_id = insert_suggestion(
            conn, title="Sent", tmdb_id=300, downloaded_at="2099-01-01", batch_id="2099-01-01"
        )
        conn.commit()

        (item,) = _get(viewer)["items"]

        assert item["download_state"] == "queued"
        row = conn.execute("SELECT downloaded_at FROM suggestions WHERE id = ?", (rec_id,))
        assert row.fetchone()[0] == "2099-01-01"


class TestRefresh:
    def test_a_viewer_gets_no_refresh_block(self, viewer, conn):
        insert_suggestion(conn, batch_id="2099-01-01")
        conn.commit()

        assert "refresh" not in _get(viewer)

    def test_an_admin_sees_the_refresh_available_when_not_on_cooldown(self, admin):
        refresh = _get(admin)["refresh"]

        assert refresh == {"available": True, "next_available_at": None, "running": False}

    def test_an_admin_sees_the_cooldown_end_after_a_recent_refresh(self, admin, conn):
        record_manual_refresh(conn, now_utc() - timedelta(hours=1))

        refresh = _get(admin)["refresh"]

        assert refresh["available"] is False
        assert refresh["next_available_at"] > now_utc().isoformat()

    def test_an_admin_still_gets_the_block_when_recommendations_are_off(self, admin, conn):
        insert_settings(conn, suggestions_enabled="false")
        conn.commit()

        body = _get(admin)

        assert body["enabled"] is False
        assert "refresh" in body
