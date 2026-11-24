"""Dashboard and Watch list empty states when Plex is not set up."""

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
def page(app_factory, authed_client, conn):
    app = app_factory(
        dashboard_router,
        watch_pages.router,
        list_page.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )
    client = authed_client(app, conn)

    def get(path: str) -> str:
        conn.commit()
        resp = client.get(path)
        assert resp.status_code == 200
        return resp.text

    return get


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


class TestDashboard:
    def test_unset_plex_says_connect_and_hides_empty_storage(self, page, monkeypatch):
        _disk(monkeypatch, None)
        html = page("/")
        assert _CONNECT in html
        assert 'href="/settings"' in html
        assert "Library is healthy" not in html
        assert 'class="storage"' not in html

    def test_unset_plex_keeps_storage_that_has_a_disk_reading(self, page, monkeypatch):
        _disk(monkeypatch, 10_000_000_000)
        assert 'class="storage"' in page("/")

    def test_unset_plex_keeps_storage_with_type_rows_but_no_disk_reading(
        self, page, conn, monkeypatch
    ):
        _disk(monkeypatch, None)
        insert_media_item(conn, id="m2", media_type="movie", file_size_bytes=5_000_000)
        assert 'class="storage"' in page("/")

    def test_set_up_plex_is_unchanged(self, page, conn):
        _plex(conn)
        html = page("/")
        assert _CONNECT not in html
        assert "Library is healthy" in html
        assert 'class="storage"' in html

    def test_scheduled_rows_and_banner_show_without_plex(self, page, conn):
        _schedule(conn)
        write_delete_root_state(conn, {"/media/movies": 1})
        html = page("/")
        assert "Still Scheduled" in html
        assert "root-banner" in html
        assert _CONNECT not in html


class TestWatchList:
    def test_unset_plex_empty_library_says_connect(self, page):
        html = page("/watch?view=list")
        assert _CONNECT in html
        assert "Run a scan" not in html

    def test_set_up_plex_empty_library_unchanged(self, page, conn):
        _plex(conn)
        html = page("/watch?view=list")
        assert "Run a scan to pull in media from Plex." in html
        assert _CONNECT not in html

    def test_filtered_empty_result_keeps_its_own_message(self, page):
        assert "Try another search, or show everything again." in page("/watch?view=list&q=zzz")
