"""Watch search and episode thumbnails: only items in the scanned libraries, and no Plex
address in any answer."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.services.media_meta.plex_watch import (
    PlexItemNotFound,
    PlexUnavailable,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository.watch_progress import save_progress
from mediaman.web.routes.watch import browse_api, pages
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings


@pytest.fixture
def client(app_factory, conn):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1"]')
    insert_admin_user(conn, username="aoife", role="viewer")
    app = app_factory(browse_api.router, conn=conn)
    test_client = TestClient(app)
    test_client.cookies.set("session_token", create_session(conn, "aoife"))
    return test_client


def _movie(conn, key: str, title: str, library: int = 1) -> None:
    insert_media_item(conn, id=key, plex_rating_key=key, title=title, plex_library_id=library)


def _season(conn, key: str, show_key: str, title: str, library: int = 1) -> None:
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type="tv_season",
        season_number=1,
        show_rating_key=show_key,
        plex_library_id=library,
    )


def _search(client, q: str) -> list[dict]:
    resp = client.get("/api/watch/search", params={"q": q})
    assert resp.status_code == 200
    return resp.json()["items"]


class TestSearch:
    def test_finds_movies_and_shows_by_title_case_insensitively(self, client, conn):
        _movie(conn, "100", "The Glasshouse")
        _season(conn, "501", "500", "Glass Coast")
        _movie(conn, "101", "Brightwater")

        items = _search(client, "GLASS")

        assert [(i["title"], i["href"]) for i in items] == [
            ("Glass Coast", "/watch/show/500"),
            ("The Glasshouse", "/watch/movie/100"),
        ]

    def test_items_outside_scanned_libraries_are_not_found(self, client, conn):
        _movie(conn, "900", "Hidden Glass", library=9)
        _season(conn, "701", "700", "Hidden Glass Show", library=9)

        assert _search(client, "glass") == []

    @pytest.mark.parametrize(("q", "expected"), [("%", ["100% Wolf"]), ("_", ["Under_score"])])
    def test_like_wildcards_match_only_themselves(self, client, conn, q, expected):
        _movie(conn, "100", "100% Wolf")
        _movie(conn, "101", "Under_score")
        _movie(conn, "102", "Plain")

        assert [i["title"] for i in _search(client, q)] == expected

    def test_results_are_capped(self, client, conn):
        for n in range(browse_api.SEARCH_LIMIT + 5):
            _movie(conn, str(1000 + n), f"Film {n:03d}")

        assert len(_search(client, "film")) == browse_api.SEARCH_LIMIT

    def test_watched_mark_is_the_searchers_own(self, client, conn):

        _movie(conn, "100", "Glasshouse")
        insert_admin_user(conn, username="cian", role="viewer")
        save_progress(
            conn,
            username="cian",
            rating_key="100",
            media_item_id="100",
            show_rating_key=None,
            position_s=5900,
            duration_s=6000,
            played_s=5900,
            now=datetime(2026, 11, 23, 12, tzinfo=UTC),
        )

        assert _search(client, "glass")[0]["watched"] is False


def _fake_plex(monkeypatch, *, section="1", thumb="/library/metadata/511/thumb/1", error=None):
    def get_item(key):
        if error is not None:
            raise error
        return SimpleNamespace(rating_key=key, library_section_id=section, thumb=thumb)

    fake = SimpleNamespace(
        get_item=get_item,
        fetch_image=lambda path: (b"\xff\xd8jpeg-bytes", "image/jpeg"),
    )
    monkeypatch.setattr(pages, "PlexWatch", lambda *_a, **_k: fake)


class TestThumb:
    def test_serves_the_image_privately(self, client, monkeypatch):
        _fake_plex(monkeypatch)

        resp = client.get("/api/watch/thumb/511")

        assert resp.status_code == 200
        assert resp.content == b"\xff\xd8jpeg-bytes"
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"].startswith("private")

    def test_item_outside_scanned_libraries_is_refused(self, client, monkeypatch):
        _fake_plex(monkeypatch, section="9")

        assert client.get("/api/watch/thumb/711").status_code == 404

    def test_missing_item_is_404(self, client, monkeypatch):
        _fake_plex(monkeypatch, error=PlexItemNotFound("Item not found in Plex"))

        assert client.get("/api/watch/thumb/511").status_code == 404

    def test_unreachable_plex_gives_no_address(self, client, monkeypatch):
        _fake_plex(monkeypatch, error=PlexUnavailable("Plex could not be reached"))

        resp = client.get("/api/watch/thumb/511")

        assert resp.status_code == 503
        assert "plex.invalid" not in resp.text

    def test_a_person_past_the_cap_gets_429_and_no_plex_call(self, client, monkeypatch):
        _fake_plex(monkeypatch)
        monkeypatch.setattr(browse_api, "_THUMB_LIMITER", ActionRateLimiter(2, 60))

        codes = [client.get("/api/watch/thumb/511").status_code for _ in range(3)]

        assert codes == [200, 200, 429]

    def test_needs_a_session(self, client):
        client.cookies.clear()

        assert client.get("/api/watch/thumb/511").status_code == 401
