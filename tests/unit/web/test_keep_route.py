"""Unit tests for keep route — _lookup_verified_action and keep_submit snooze paths."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import generate_keep_token
from mediaman.db import init_db, set_connection
from mediaman.web.routes.keep import (
    _KEEP_GET_LIMITER,
    _KEEP_POST_LIMITER,
    _lookup_verified_action,
    _token_hash,
)
from mediaman.web.routes.keep import router as keep_router

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
        (media_id, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _insert_action(
    conn: sqlite3.Connection, media_id: str = "mi1", placeholder: str = "placeholder"
) -> int:
    cur = conn.execute(
        "INSERT INTO scheduled_actions (media_item_id, action, execute_at, token, scheduled_at) "
        "VALUES (?, 'scheduled_deletion', datetime('now', '+7 days'), ?, datetime('now'))",
        (media_id, placeholder),
    )
    conn.commit()
    return cur.lastrowid


def _make_keep_token(conn: sqlite3.Connection, media_id: str, action_id: int) -> str:
    import time

    token = generate_keep_token(
        media_item_id=media_id,
        action_id=action_id,
        expires_at=int(time.time()) + 86400 * 180,
        secret_key=SECRET,
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
            media_item_id="mi1",
            action_id=action_id_1,
            expires_at=int(time.time()) + 86400,
            secret_key=SECRET,
        )
        # Store action_id_2's token in action_id_1's DB row — payload and row disagree
        token_for_2 = generate_keep_token(
            media_item_id="mi2",
            action_id=action_id_2,
            expires_at=int(time.time()) + 86400,
            secret_key=SECRET,
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


class TestKeepLimiters:
    """H28: GET and POST must have independent rate-limit counters."""

    def test_get_and_post_limiters_are_separate_objects(self):
        assert _KEEP_GET_LIMITER is not _KEEP_POST_LIMITER

    def test_get_limiter_does_not_share_state_with_post_limiter(self):
        # Both limiters use the same IP bucket style, but their internal
        # attempt stores must be distinct so exhausting one does not affect the other.
        assert _KEEP_GET_LIMITER._attempts is not _KEEP_POST_LIMITER._attempts


class TestKeepPageRoute:
    def _make_app(self, conn: sqlite3.Connection) -> FastAPI:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        set_connection(conn)
        mock_templates = MagicMock()
        mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
            json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200
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


class TestKeepSubmitTokenInvalidation:
    """H27: keep_submit must mark the token as used and reject replays."""

    def _make_app(self, conn: sqlite3.Connection) -> FastAPI:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        set_connection(conn)
        mock_templates = MagicMock()
        mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
            json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200
        )
        app.state.templates = mock_templates
        return app

    def test_invalid_token_returns_400_invalid_or_expired(self, conn):
        """A forged / unknown token must return 400 with error=invalid_or_expired."""
        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/keep/bogustoken", data={"duration": "30 days"})

        assert resp.status_code == 400
        import json as _json

        body = _json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_unknown_duration_returns_400(self, conn):
        """An unrecognised duration must return 400 before touching the DB."""
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post(f"/keep/{token}", data={"duration": "never"})

        assert resp.status_code == 400

    def test_token_used_flag_set_in_keep_tokens_used(self, conn):
        """A successful POST must insert into keep_tokens_used."""
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        # Successful POST redirects to the keep page.
        assert resp.status_code in (200, 302, 307)

        th = _token_hash(token)
        row = conn.execute(
            "SELECT token_hash FROM keep_tokens_used WHERE token_hash = ?", (th,)
        ).fetchone()
        assert row is not None, "Token hash must be persisted in keep_tokens_used"

    def test_replay_returns_409_already_processed(self, conn):
        """Submitting the same token twice must return 409 on the second attempt."""
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        # First POST — should succeed.
        client.post(f"/keep/{token}", data={"duration": "30 days"})

        # Second POST — same token, must be rejected as already processed.
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 409
        import json as _json

        body = _json.loads(resp.text)
        assert body["error"] == "already_processed"

    def test_token_hash_helper(self):
        """_token_hash must return a stable 64-char hex string."""
        h = _token_hash("test-token")
        assert len(h) == 64
        assert h == _token_hash("test-token")
        assert h != _token_hash("other-token")
