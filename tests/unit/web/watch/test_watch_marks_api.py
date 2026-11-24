"""Watched and Dismissed marks: one person's own, undone exactly, only for library titles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.services.media_meta.plex_watch import PlexUnavailable
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.csrf import CSRFOriginMiddleware
from mediaman.web.repository.watch_progress import continue_watching, get_progress, save_progress
from mediaman.web.routes.watch import marks_api, pages
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings
from tests.unit.web.watch.test_watch_pages import FakePlex

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
EPISODES = ["511", "512", "521", "522"]


@pytest.fixture
def plex(monkeypatch):
    fake = FakePlex()
    fake.add_movie("100", "Brightwater")
    fake.add_movie("900", "Hidden Film", section="9")
    fake.add_show("500", "The Long Coast", {1: ("501", ["511", "512"]), 2: ("502", ["521", "522"])})
    fake.add_show("700", "Hidden Show", {1: ("701", ["711"])}, section="9")
    monkeypatch.setattr(pages, "PlexWatch", lambda *_a, **_k: fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    times = [NOW]
    monkeypatch.setattr(marks_api, "now_utc", lambda: times[0])
    monkeypatch.setattr(marks_api, "_UNDO", {})
    return times


def _season(conn, key, show, title, number, library=1):
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type="tv_season",
        season_number=number,
        show_rating_key=show,
        plex_library_id=library,
    )


@pytest.fixture
def as_user(app_factory, conn, plex, clock):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1"]')
    insert_media_item(conn, id="100", plex_rating_key="100", title="Brightwater")
    insert_media_item(conn, id="900", plex_rating_key="900", title="Hidden Film", plex_library_id=9)
    _season(conn, "501", "500", "The Long Coast", 1)
    _season(conn, "502", "500", "The Long Coast", 2)
    _season(conn, "701", "700", "Hidden Show", 1, library=9)
    app = app_factory(marks_api.router, conn=conn)

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


def _seen(conn, keys, username="aoife"):
    found = get_progress(conn, username, keys)
    return [k for k in keys if k in found and found[k].seen]


def _rows(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM watch_progress ORDER BY rating_key")]


def _play(conn, key, *, position, duration=3000.0, item="501", show="500"):
    save_progress(
        conn,
        username="aoife",
        rating_key=key,
        media_item_id=item,
        show_rating_key=show,
        position_s=position,
        duration_s=duration,
        played_s=position,
        now=NOW - timedelta(hours=1),
    )


@pytest.mark.parametrize(
    ("kind", "key", "covered"),
    [
        ("movie", "100", ["100"]),
        ("episode", "512", ["512"]),
        ("season", "502", ["521", "522"]),
        ("show", "500", EPISODES),
    ],
)
def test_marking_and_unmarking_cover_a_title_whole_and_no_other(as_user, conn, kind, key, covered):
    client, every = as_user("aoife"), ["100", *EPISODES]

    marked = client.post(f"/api/watch/watched/{kind}/{key}")
    assert marked.json() == {"ok": True, "watched": True, "changed": len(covered)}
    assert _seen(conn, every) == covered
    assert client.post(f"/api/watch/watched/{kind}/{key}").json()["changed"] == 0
    assert as_user("cian").delete(f"/api/watch/watched/{kind}/{key}").json()["changed"] == 0
    assert (_seen(conn, every), _seen(conn, every, "cian")) == (covered, [])

    unmarked = client.delete(f"/api/watch/watched/{kind}/{key}")
    assert unmarked.json() == {"ok": True, "watched": False, "changed": len(covered)}
    assert _seen(conn, every) == []


def test_an_episode_mark_belongs_to_its_season_and_show(client, conn):
    client.post("/api/watch/watched/season/502")
    rows = conn.execute("SELECT DISTINCT media_item_id, show_rating_key FROM watch_progress")
    assert [tuple(r) for r in rows] == [("502", "500")]


class TestUndo:
    def test_undoing_a_show_mark_restores_only_what_it_changed(self, client, conn):
        _play(conn, "511", position=3000)
        _play(conn, "512", position=900)
        before = _rows(conn)
        client.post("/api/watch/watched/show/500")

        undone = client.delete("/api/watch/watched/show/500?undo=1")

        assert undone.json() == {"ok": True, "watched": False, "changed": 3}
        assert [r for r in _rows(conn) if r[1] in ("511", "512")] == before
        assert _seen(conn, EPISODES) == ["511"]

    def test_undoing_an_unmark_brings_the_mark_and_the_place_back(self, client, conn):
        _play(conn, "511", position=2900)
        before = _rows(conn)
        client.delete("/api/watch/watched/episode/511")

        assert client.post("/api/watch/watched/episode/511?undo=1").json()["watched"] is True
        assert _rows(conn) == before

    def test_nothing_to_undo_changes_nothing(self, as_user, conn, clock):
        client = as_user("aoife")
        client.post("/api/watch/watched/movie/100")

        others = as_user("cian").delete("/api/watch/watched/movie/100?undo=1")
        wrong_way = client.post("/api/watch/watched/movie/100?undo=1")
        clock[0] = NOW + marks_api.UNDO_WINDOW
        too_late = client.delete("/api/watch/watched/movie/100?undo=1")

        for resp in (others, wrong_way, too_late):
            assert (resp.status_code, resp.json()["error"]) == (409, "too_late")
        assert _seen(conn, ["100"]) == ["100"]

    def test_an_undo_works_once(self, client):
        client.post("/api/watch/watched/movie/100")
        assert client.delete("/api/watch/watched/movie/100?undo=1").status_code == 200
        assert client.delete("/api/watch/watched/movie/100?undo=1").status_code == 409

    def test_marking_twice_keeps_the_first_marks_undo(self, client, conn):
        client.post("/api/watch/watched/movie/100")
        client.post("/api/watch/watched/movie/100")
        assert client.delete("/api/watch/watched/movie/100?undo=1").status_code == 200
        assert _seen(conn, ["100"]) == []


class TestRefusals:
    @pytest.mark.parametrize(
        "path",
        [
            "watched/movie/900",
            "watched/movie/511",
            "watched/episode/711",
            "watched/episode/100",
            "watched/season/701",
            "watched/season/500",
            "watched/season/404",
            "watched/show/700",
            "watched/show/501",
            "watched/trailer/100",
            "watched/movie/abc",
            "dismissed/movie-900",
            "dismissed/show-700",
            "dismissed/show-100",
            "dismissed/tmdb-movie-1",
            "dismissed/movie-",
        ],
    )
    def test_what_is_not_in_the_scanned_libraries_is_not_found(self, client, conn, path):
        resp = client.post(f"/api/watch/{path}")
        assert (resp.status_code, resp.json()["error"]) == (404, "not_found")
        assert _rows(conn) == []

    def test_progress_of_a_library_no_longer_scanned_cannot_be_unmarked(self, client, conn):
        _play(conn, "711", position=3000, item="701", show="700")
        for path in ("episode/711", "season/701", "show/700"):
            assert client.delete(f"/api/watch/watched/{path}").status_code == 404
        assert _seen(conn, ["711"]) == ["711"]

    def test_plex_failing_part_way_marks_nothing(self, client, conn, plex, monkeypatch):
        real = plex.get_season_episodes

        def fail_second(key):
            if key == "502":
                raise PlexUnavailable("down")
            return real(key)

        monkeypatch.setattr(plex, "get_season_episodes", fail_second)
        resp = client.post("/api/watch/watched/show/500")
        assert (resp.status_code, resp.json()["error"]) == (503, "unreachable")
        assert _rows(conn) == []

    def test_a_film_is_marked_and_a_show_unmarked_with_plex_down(self, client, conn, plex):
        client.post("/api/watch/watched/show/500")
        plex.error = PlexUnavailable("down")
        assert client.delete("/api/watch/watched/show/500").json()["changed"] == 4
        assert client.post("/api/watch/watched/movie/100").json()["changed"] == 1

    @pytest.mark.parametrize("path", ["watched/movie/100", "dismissed/movie-100"])
    @pytest.mark.parametrize("method", ["POST", "DELETE"])
    def test_changes_are_rate_limited_per_person(self, client, monkeypatch, method, path):
        monkeypatch.setattr(marks_api._MARK_LIMITER, "check", lambda _key: False)
        resp = client.request(method, f"/api/watch/{path}")
        assert (resp.status_code, resp.json()["error"]) == (429, "too_many_requests")

    @pytest.mark.parametrize("path", ["watched/movie/100", "dismissed/movie-100"])
    def test_a_signed_out_caller_changes_nothing(self, as_user, conn, path):
        client = as_user("aoife")
        client.cookies.clear()
        assert client.post(f"/api/watch/{path}").status_code == 401
        assert _rows(conn) == []


@pytest.mark.parametrize("path", ["watched/movie/100", "dismissed/movie-100"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_another_sites_request_changes_nothing(client, conn, method, path):
    client.app.add_middleware(CSRFOriginMiddleware)
    _play(conn, "100", position=900, item="100", show=None)
    before = _rows(conn)

    resp = client.request(
        method, f"/api/watch/{path}", headers={"origin": "https://elsewhere.example"}
    )

    assert resp.status_code == 403
    assert _rows(conn) == before


class TestDismiss:
    @pytest.mark.parametrize(
        ("ref", "key", "item", "show"),
        [("movie-100", "100", "100", None), ("show-500", "511", "501", "500")],
    )
    def test_dismissing_hides_the_title_and_undo_brings_it_back(
        self, client, conn, ref, key, item, show
    ):
        _play(conn, key, position=900, item=item, show=show)
        before = _rows(conn)

        assert client.post(f"/api/watch/dismissed/{ref}").json() == {"ok": True}
        assert continue_watching(conn, "aoife") == []
        assert client.delete(f"/api/watch/dismissed/{ref}").json() == {"ok": True}

        assert [c.progress.rating_key for c in continue_watching(conn, "aoife")] == [key]
        assert _rows(conn) == before
