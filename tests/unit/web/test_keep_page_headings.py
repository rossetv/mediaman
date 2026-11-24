"""The keep page's error and expired states head their message with an h1."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman.web
from mediaman.config import Config
from mediaman.db import set_connection
from mediaman.web.routes.keep import router as keep_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action
from tests.unit.web.test_keep_route import SECRET, _make_keep_token

_TEMPLATE_DIR = Path(mediaman.web.__file__).parent / "templates"


class TestKeepPageHeadings:
    def _client(self, conn: sqlite3.Connection) -> TestClient:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        set_connection(conn)
        return TestClient(app, raise_server_exceptions=True)

    def test_invalid_state_has_a_neutral_h1_and_pill(self, conn):
        text = self._client(conn).get("/keep/badtoken").text

        assert re.search(r"<h1[^>]*>\s*This link isn't valid\s*</h1>", text)
        assert re.search(r"</span>\s*Invalid\s*</div>", text)
        assert "This link has expired" not in text

    def test_expired_link_without_a_poster_says_expired_throughout(self, conn):
        insert_media_item(conn, id="mi1", title="Test", plex_rating_key="", file_path="/f")
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", action="snoozed", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)

        text = self._client(conn).get(f"/keep/{token}").text

        assert re.search(r"<h1[^>]*>\s*This link has expired\s*</h1>", text)
        assert re.search(r"</span>\s*Expired\s*</div>", text)
        assert re.search(r"<title>\s*Expired", text)

    def test_error_state_has_an_h1(self, conn):
        resp = self._client(conn).post(
            "/keep/badtoken",
            data={"duration": "30 days"},
            headers={"accept": "text/html"},
        )

        assert re.search(r"<h1[^>]*>\s*We could not keep this item\s*</h1>", resp.text)

    def test_expired_state_with_an_item_keeps_a_single_h1(self, conn):
        insert_media_item(conn, id="mi1", title="Test", plex_rating_key="rk1", file_path="/f")
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", action="snoozed", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)

        resp = self._client(conn).get(f"/keep/{token}")

        assert resp.text.count("<h1") == 1
        assert "This link has expired" in resp.text


class TestKeepPageMessages:
    def _client(self, conn: sqlite3.Connection) -> TestClient:
        return TestKeepPageHeadings()._client(conn)

    def test_damaged_link_does_not_blame_the_protection_window(self, conn):
        text = self._client(conn).get("/keep/badtoken").text

        assert "This link is no longer valid or has expired." in text
        assert "protection window" not in text

    def test_really_expired_row_keeps_the_protection_window_sentence(self, conn):
        insert_media_item(conn, id="mi1", title="Test", plex_rating_key="rk1", file_path="/f")
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", action="snoozed", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)

        text = self._client(conn).get(f"/keep/{token}").text

        assert "The protection window for this item has passed." in text

    def _active_page(self, conn, **item):
        insert_media_item(conn, id="mi1", title="Test", file_path="/f", **item)
        execute_at = (datetime.now(UTC) + timedelta(days=5)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)
        return self._client(conn).get(f"/keep/{token}").text

    def test_watched_item_is_not_labelled_never_watched(self, conn):
        text = self._active_page(conn, last_watched_at="2025-05-14T10:00:00+00:00")

        assert "Never watched" not in text
        assert "Last watched 14 May 2025" in text

    def test_unwatched_item_is_labelled_never_watched(self, conn):
        assert "Never watched" in self._active_page(conn, last_watched_at=None)

    @pytest.mark.parametrize(
        ("media_type", "season", "kind", "label"),
        [
            ("tv_season", 2, "TV", "Season 2"),
            ("anime_season", 1, "Anime", "Season 1"),
            ("tv_season", 0, "TV", "Specials"),
        ],
    )
    def test_a_season_is_named_by_its_show_its_number_and_its_kind(
        self, conn, media_type, season, kind, label
    ):
        text = self._active_page(
            conn, media_type=media_type, show_title="Harrow Lane", season_number=season
        )

        assert re.search(r"<h1[^>]*>\s*Harrow Lane\s*</h1>", text)
        assert f'<div class="item-season">{label}</div>' in text
        assert re.search(rf'<span class="pill pill--{kind.lower()}">{kind}</span>', text)
        assert re.search(r"<title>\s*Keep · Harrow Lane", text)

    def test_the_year_it_was_added_does_not_stand_as_its_release_year(self, conn):
        text = self._active_page(conn, added_at="2019-03-02T10:00:00+00:00")

        assert "Added 2 March 2019" in text
        assert not re.search(r">\s*2019\s*<", text)

    def test_a_long_word_wraps_inside_the_card(self):
        css = (_TEMPLATE_DIR.parent / "static/css/_keep.css").read_text(encoding="utf-8")

        rule = re.search(r"\.keep-container \{([^}]*)\}", css)
        assert rule and "overflow-wrap: anywhere" in rule[1]

    def test_rate_limited_get_renders_the_styled_page(self, conn):
        from mediaman.web.routes.keep import _KEEP_GET_LIMITER

        while _KEEP_GET_LIMITER.check("testclient"):
            pass
        resp = self._client(conn).get("/keep/badtoken")

        assert resp.status_code == 429
        assert "keep-page" in resp.text
        assert "Too many requests" in resp.text
        assert "We could not keep this item" not in resp.text
        assert re.search(r"<title>\s*Too many requests", resp.text)
        assert "Not changed" not in resp.text
        assert "open the link again" in resp.text

    def test_rate_limited_form_post_renders_the_styled_page(self, conn):
        from mediaman.web.routes.keep import _KEEP_POST_LIMITER

        while _KEEP_POST_LIMITER.check("testclient"):
            pass
        resp = self._client(conn).post(
            "/keep/badtoken", data={"duration": "7 days"}, headers={"accept": "text/html"}
        )

        assert resp.status_code == 429
        assert "keep-page" in resp.text
