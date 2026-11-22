"""The keep page's error and expired states head their message with an h1."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

    def test_expired_state_has_an_h1(self, conn):
        resp = self._client(conn).get("/keep/badtoken")

        assert re.search(r"<h1[^>]*>\s*This link has expired\s*</h1>", resp.text)

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
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)

        resp = self._client(conn).get(f"/keep/{token}")

        assert resp.text.count("<h1") == 1
        assert "This link has expired" in resp.text
