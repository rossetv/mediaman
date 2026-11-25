"""The keep page's error and expired states head their message with an h1."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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

    def test_invalid_state_has_a_neutral_h1_and_title(self, conn):
        text = self._client(conn).get("/keep/badtoken").text

        assert re.search(r"<h1[^>]*>\s*This link does not work\s*</h1>", text)
        assert re.search(r"<title>\s*Invalid link", text)
        assert "This link has run out" not in text

    def test_expired_link_without_a_poster_is_headed_by_its_title_and_says_expired(self, conn):
        insert_media_item(conn, id="mi1", title="Test", plex_rating_key="", file_path="/f")
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", action="snoozed", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)

        text = self._client(conn).get(f"/keep/{token}").text

        assert re.search(r"<h1[^>]*>\s*Test\s*</h1>", text)
        assert "This link has run out." in text and "<img" not in text
        assert re.search(r"<title>\s*Expired", text)

    def test_error_state_has_an_h1(self, conn):
        resp = self._client(conn).post(
            "/keep/badtoken",
            data={"duration": "30 days"},
            headers={"accept": "text/html"},
        )

        assert re.search(r"<h1[^>]*>\s*This link does not work\s*</h1>", resp.text)

    def test_expired_state_with_an_item_keeps_a_single_h1(self, conn):
        insert_media_item(conn, id="mi1", title="Test", plex_rating_key="rk1", file_path="/f")
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", action="snoozed", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)

        resp = self._client(conn).get(f"/keep/{token}")

        assert resp.text.count("<h1") == 1
        assert "This link has run out." in resp.text


class TestKeepPageMessages:
    def _client(self, conn: sqlite3.Connection) -> TestClient:
        return TestKeepPageHeadings()._client(conn)

    def test_damaged_link_does_not_blame_the_protection_window(self, conn):
        text = self._client(conn).get("/keep/badtoken").text

        assert "It may have run out, or part of it may be missing." in text
        assert "The time this was kept for" not in text

    def test_really_expired_row_keeps_the_protection_window_sentence(self, conn):
        insert_media_item(conn, id="mi1", title="Test", plex_rating_key="rk1", file_path="/f")
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", action="snoozed", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)

        text = self._client(conn).get(f"/keep/{token}").text

        assert "The time this was kept for is over." in text

    def _active_page(self, conn, **item):
        insert_media_item(conn, id="mi1", title="Test", file_path="/f", **item)
        execute_at = (datetime.now(UTC) + timedelta(days=5)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)
        return self._client(conn).get(f"/keep/{token}").text

    @pytest.mark.parametrize("last_watched_at", ["2025-05-14T10:00:00+00:00", None])
    def test_whether_it_was_watched_is_not_said(self, conn, last_watched_at):
        text = self._active_page(conn, last_watched_at=last_watched_at)

        assert "atched" not in text and "14 May" not in text

    @pytest.mark.parametrize(
        ("media_type", "season", "kind", "label"),
        [
            ("tv_season", 2, "TV series", "Season 2"),
            ("anime_season", 1, "Anime", "Season 1"),
            ("tv_season", 0, "TV series", "Specials"),
        ],
    )
    def test_a_season_is_named_by_its_show_its_number_and_its_kind(
        self, conn, media_type, season, kind, label
    ):
        text = self._active_page(
            conn, media_type=media_type, show_title="Harrow Lane", season_number=season
        )

        assert re.search(r"<h1[^>]*>\s*Harrow Lane\s*</h1>", text)
        assert f'<p class="share-season">{label}</p>' in text
        assert f'<span class="ttl__kind">{kind}</span>' in text
        assert re.search(r"<title>\s*Keep · Harrow Lane", text)

    def test_the_year_it_was_added_does_not_stand_as_its_release_year(self, conn):
        text = self._active_page(conn, added_at="2019-03-02T10:00:00+00:00")

        assert "2019" not in text

    def test_a_long_word_wraps_inside_the_card(self):
        css = (_TEMPLATE_DIR.parent / "static/css/_share.css").read_text(encoding="utf-8")

        for box in (r"\.share-page \.watch-banner__body", r"\.share-note \.login-card"):
            rule = re.search(box + r" \{([^}]*)\}", css)
            assert rule and "overflow-wrap: anywhere" in rule[1]

    def test_rate_limited_get_renders_the_styled_page(self, conn):
        from mediaman.web.routes.keep import _KEEP_GET_LIMITER

        while _KEEP_GET_LIMITER.check("testclient"):
            pass
        resp = self._client(conn).get("/keep/badtoken")

        assert resp.status_code == 429
        assert "One moment" in resp.text
        assert "does not work" not in resp.text
        assert re.search(r"<title>\s*Too many requests", resp.text)
        assert re.search(r'<a [^>]*href="" data-count="60">Try again</a>', resp.text)
        assert resp.headers["Retry-After"] == "60"

    @pytest.mark.parametrize("path", ["/keep/badtoken", "/api/keep/badtoken/forever"])
    def test_a_rate_limited_json_post_says_when_to_come_back(self, conn, authed_client, path):
        from mediaman.web.routes.keep import _KEEP_POST_LIMITER

        client = authed_client(self._client(conn).app, conn)
        with patch.object(_KEEP_POST_LIMITER, "check", return_value=False):
            resp = client.post(path, data={"duration": "7 days"})

        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "60"

    def test_rate_limited_form_post_renders_the_styled_page(self, conn):
        from mediaman.web.routes.keep import _KEEP_POST_LIMITER

        while _KEEP_POST_LIMITER.check("testclient"):
            pass
        resp = self._client(conn).post(
            "/keep/badtoken", data={"duration": "7 days"}, headers={"accept": "text/html"}
        )

        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "60"
        assert "One moment" in resp.text
