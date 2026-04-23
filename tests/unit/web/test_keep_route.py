"""Unit tests for keep route — _lookup_verified_action and keep_submit snooze paths."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import generate_keep_token
from mediaman.db import init_db, set_connection
from mediaman.web.routes.keep import _lookup_verified_action, router as keep_router


SECRET = "a" * 64


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


def _insert_media_item(conn: sqlite3.Connection, media_id: str = "mi1") -> None:
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, plex_library_id, plex_rating_key, "
        "added_at, file_path, file_size_bytes) VALUES (?, 'Test', 'movie', 1, 'rk1', ?, '/f', 0)",
        (media_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _insert_action(conn: sqlite3.Connection, media_id: str = "mi1", placeholder: str = "placeholder") -> int:
    cur = conn.execute(
        "INSERT INTO scheduled_actions (media_item_id, action, execute_at, token, scheduled_at) "
        "VALUES (?, 'delete', datetime('now', '+7 days'), ?, datetime('now'))",
        (media_id, placeholder),
    )
    conn.commit()
    return cur.lastrowid


def _make_keep_token(conn: sqlite3.Connection, media_id: str, action_id: int) -> str:
    import time
    token = generate_keep_token(
        media_item_id=media_id, action_id=action_id,
        expires_at=int(time.time()) + 86400 * 180, secret_key=SECRET
    )
    conn.execute("UPDATE scheduled_actions SET token=? WHERE id=?", (token, action_id))
    conn.commit()
    return token


class TestLookupVerifiedAction:
    def test_valid_token_returns_row(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = _lookup_verified_action(conn, token, SECRET)

        assert row is not None
        assert row["media_item_id"] == "mi1"

    def test_invalid_hmac_returns_none(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        _make_keep_token(conn, "mi1", action_id)

        row = _lookup_verified_action(conn, "tampered_token", SECRET)

        assert row is None

    def test_missing_db_row_returns_none(self, conn):
        """Token validates but no matching scheduled_actions row exists."""
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        # Delete the action so DB lookup fails
        conn.execute("DELETE FROM scheduled_actions WHERE id=?", (action_id,))
        conn.commit()

        row = _lookup_verified_action(conn, token, SECRET)

        assert row is None

    def test_payload_mismatch_rejected(self, conn):
        """Token with wrong action_id in payload is rejected even if HMAC is valid."""
        import time
        _insert_media_item(conn)
        _insert_media_item(conn, "mi2")
        action_id_1 = _insert_action(conn, "mi1", "placeholder1")
        action_id_2 = _insert_action(conn, "mi2", "placeholder2")
        # Build a token for action_id_1 but point it at action_id_2's row
        token_for_1 = generate_keep_token(
            media_item_id="mi1", action_id=action_id_1,
            expires_at=int(time.time()) + 86400, secret_key=SECRET
        )
        # Store action_id_2's token in action_id_1's DB row — payload and row disagree
        token_for_2 = generate_keep_token(
            media_item_id="mi2", action_id=action_id_2,
            expires_at=int(time.time()) + 86400, secret_key=SECRET
        )
        conn.execute("UPDATE scheduled_actions SET token=? WHERE id=?", (token_for_2, action_id_1))
        conn.commit()

        # Looking up token_for_1 finds the row (stored token differs) → None
        row = _lookup_verified_action(conn, token_for_1, SECRET)

        assert row is None

    def test_wrong_secret_returns_none(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = _lookup_verified_action(conn, token, "b" * 64)

        assert row is None


class TestKeepPageRoute:
    def _make_app(self, conn: sqlite3.Connection) -> FastAPI:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        set_connection(conn)
        mock_templates = MagicMock()
        mock_templates.TemplateResponse.side_effect = (
            lambda req, tmpl, ctx: HTMLResponse(json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200)
        )
        app.state.templates = mock_templates
        return app

    def test_overlong_token_returns_expired(self, conn):
        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/keep/{'x' * 5000}")

        assert resp.status_code == 200
        assert "expired" in resp.text

    def test_invalid_token_returns_expired(self, conn):
        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/keep/badtoken")

        assert resp.status_code == 200
        assert "expired" in resp.text

    def test_valid_token_returns_active_state(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/keep/{token}")

        assert resp.status_code == 200
        assert "active" in resp.text
