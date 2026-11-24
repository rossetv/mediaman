"""The storage panel's and the Watch list's empty states when Plex is not set up."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.core.delete_root_state import write_delete_root_state
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.web.routes.dashboard import router as dashboard_router
from mediaman.web.routes.watch import list_page
from mediaman.web.routes.watch import pages as watch_pages
from tests.helpers.factories import insert_media_item, insert_scheduled_action, insert_settings

_TEMPLATE_DIR = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates"
_CONNECT = "Connect Plex to get started"


@pytest.fixture
def get(app_factory, authed_client, conn):
    app = app_factory(
        dashboard_router,
        watch_pages.router,
        list_page.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )
    client = authed_client(app, conn)

    def _get(path: str):
        conn.commit()
        resp = client.get(path)
        assert resp.status_code == 200
        return resp

    return _get


@pytest.fixture
def page(get):
    return lambda path: get(path).text


@pytest.fixture
def stats(get):
    return lambda: get("/api/dashboard/stats").json()


def _disk(monkeypatch, total):
    """Pin the disk reading so the page does not depend on the host's media path."""

    def fake(*_args, **_kwargs):
        if total is None:
            raise OSError("no disk")
        return {"total_bytes": total, "used_bytes": total // 2, "free_bytes": total - total // 2}

    monkeypatch.setattr("mediaman.web.routes.dashboard._data._cached_disk_usage", fake)


def _plex(conn, url="http://plex:32400", token="tok"):
    insert_settings(conn, plex_url=url)
    insert_settings(conn, plex_token=token, encrypted=1)


def _schedule(conn):
    insert_media_item(
        conn,
        id="m1",
        title="Still Scheduled",
        media_type="movie",
        plex_rating_key="rk1",
        file_path="/f",
        file_size_bytes=1,
    )
    insert_scheduled_action(conn, media_item_id="m1")


class TestPlexIsConfigured:
    def test_needs_both_url_and_token_without_decrypting(self, conn):
        assert not plex_is_configured(conn)
        insert_settings(conn, plex_url="http://plex:32400")
        assert not plex_is_configured(conn)
        # Ciphertext that no key could decrypt still counts as set.
        insert_settings(conn, plex_token="not-decryptable", encrypted=1)
        assert plex_is_configured(conn)

    def test_empty_url_is_not_configured(self, conn):
        _plex(conn, url="")
        assert not plex_is_configured(conn)

    def test_whitespace_only_url_counts_as_set_like_the_scanner(self, conn):
        _plex(conn, url="   ")
        assert plex_is_configured(conn)


class TestStoragePanel:
    def test_unset_plex_hides_empty_storage(self, stats, monkeypatch):
        _disk(monkeypatch, None)
        got = stats()
        assert (got["plex_configured"], got["show_storage"]) == (False, False)

    def test_unset_plex_keeps_storage_that_has_a_disk_reading(self, stats, monkeypatch):
        _disk(monkeypatch, 10_000_000_000)
        assert stats()["show_storage"] is True

    def test_unset_plex_keeps_storage_with_type_rows_but_no_disk_reading(
        self, stats, conn, monkeypatch
    ):
        _disk(monkeypatch, None)
        insert_media_item(conn, id="m2", media_type="movie", file_size_bytes=5_000_000)
        assert stats()["show_storage"] is True

    def test_set_up_plex_shows_storage(self, stats, conn, monkeypatch):
        _disk(monkeypatch, None)
        _plex(conn)
        got = stats()
        assert (got["plex_configured"], got["show_storage"]) == (True, True)

    def test_scheduled_rows_and_the_missing_folder_show_without_plex(self, get, stats, conn):
        _schedule(conn)
        write_delete_root_state(conn, {"/media/movies": 1})
        assert stats()["unavailable_roots"] == [{"path": "/media/movies", "waiting": 1}]
        items = get("/api/dashboard/scheduled").json()["items"]
        assert [i["title"] for i in items] == ["Still Scheduled"]


class TestWatchList:
    def test_unset_plex_empty_library_says_connect(self, page):
        html = page("/watch?view=list")
        assert _CONNECT in html
        assert "Run a scan" not in html

    def test_set_up_plex_empty_library_unchanged(self, page, conn):
        _plex(conn)
        html = page("/watch?view=list")
        assert "Run a scan from Storage to pull in media from Plex." in html
        assert _CONNECT not in html

    def test_filtered_empty_result_keeps_its_own_message(self, page):
        assert "Try another search, or show everything again." in page("/watch?view=list&q=zzz")
