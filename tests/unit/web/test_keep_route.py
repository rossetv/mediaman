"""Unit tests for keep route — _lookup_verified_action and keep_submit snooze paths."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import generate_keep_token
from mediaman.db import set_connection
from mediaman.web.routes.keep import (
    _KEEP_GET_LIMITER,
    _KEEP_POST_LIMITER,
    _lookup_verified_action,
    _token_hash,
)
from mediaman.web.routes.keep import router as keep_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action

# Keep tokens in this file are minted with an isolated SECRET (not the
# conftest's ``secret_key``) so the test seam stays orthogonal to the rest
# of the suite — that means the local class ``_make_app`` methods cannot
# adopt the shared ``app_factory`` (which always uses the conftest secret).
SECRET = "a" * 64


def _insert_media_item(conn: sqlite3.Connection, media_id: str = "mi1") -> None:
    insert_media_item(
        conn,
        id=media_id,
        title="Test",
        plex_rating_key="rk1",
        file_path="/f",
        file_size_bytes=0,
    )


def _insert_action(
    conn: sqlite3.Connection, media_id: str = "mi1", placeholder: str = "placeholder"
) -> int:
    execute_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    return insert_scheduled_action(
        conn,
        media_item_id=media_id,
        token=placeholder,
        execute_at=execute_at,
    )


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


def _insert_scheduled_action_findings(
    conn: sqlite3.Connection,
    media_id: str = "mi1",
    action: str = "scheduled_deletion",
    delete_status: str = "pending",
    execute_at_offset_days: int = 7,
) -> int:
    """Insert a scheduled_actions row and return the rowid."""
    execute_at = (datetime.now(UTC) + timedelta(days=execute_at_offset_days)).isoformat()
    return insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action=action,
        token="placeholder",
        execute_at=execute_at,
        delete_status=delete_status,
    )


def _make_keep_app_findings(app_factory, conn: sqlite3.Connection):
    """Stand up a minimal keep-router app using the findings-suite SECRET."""
    mock_templates = MagicMock()
    mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
        json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200
    )
    app = FastAPI()
    app.include_router(keep_router)
    app.state.config = Config(secret_key=SECRET)
    app.state.db = conn
    app.state.templates = mock_templates
    from mediaman.db import set_connection

    set_connection(conn)
    return app, TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Finding 12: "forever" rejected on public route, accepted on admin endpoint
# ---------------------------------------------------------------------------


class TestFinding12ForeverEndpointSeparation:
    """Finding 12: forever must be refused on the public keep POST."""

    def test_forever_duration_rejected_on_public_post(self, app_factory, conn):
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_findings(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "forever"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_admin_forever_endpoint_exists(self, conn):
        """POST /api/keep/{token}/forever route must be registered."""
        from mediaman.web.routes.keep import router

        routes = [r.path for r in router.routes]
        assert any("forever" in r for r in routes), (
            f"No 'forever' route found in keep router routes: {routes}"
        )

    def test_unauthenticated_forever_returns_401(self, app_factory, conn):
        """Un-authed POST to /api/keep/{token}/forever must return 401."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_findings(app_factory, conn)
        resp = client.post(f"/api/keep/{token}/forever")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Finding 13: Keep POST refuses expired / non-pending actions
# ---------------------------------------------------------------------------


class TestFinding13KeepDeadlineCheck:
    """Finding 13: keep_submit must reject rows that have expired or are not pending."""

    def test_expired_action_returns_400(self, app_factory, conn):
        """A keep POST where execute_at is in the past must return 400."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn, execute_at_offset_days=-1)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_findings(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_non_pending_delete_status_returns_400(self, app_factory, conn):
        """A row with delete_status='deleting' must be refused."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn, delete_status="deleting")
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_findings(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_non_deletion_action_returns_400(self, app_factory, conn):
        """A keep POST against a 'snoozed' action (not 'scheduled_deletion') must fail."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn, action="snoozed")
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_findings(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_valid_pending_action_succeeds(self, app_factory, conn):
        """A valid keep POST against a pending scheduled_deletion must succeed."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_findings(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code in (200, 302, 307)


# ---------------------------------------------------------------------------
# Finding 16: Keep token hash storage
# ---------------------------------------------------------------------------


class TestFinding16KeepTokenHash:
    """Finding 16: token hash helpers and insert-only-hash logic."""

    def test_find_active_keep_action_by_id_and_token(self, conn):
        """Helper must return the row when token hash matches and conditions are met."""
        from mediaman.web.routes.keep import find_active_keep_action_by_id_and_token

        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = find_active_keep_action_by_id_and_token(conn, action_id, token)
        assert row is not None
        assert row["id"] == action_id

    def test_find_active_returns_none_for_expired(self, conn):
        """Helper must return None when execute_at is in the past."""

        from mediaman.web.routes.keep import find_active_keep_action_by_id_and_token

        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn, execute_at_offset_days=-1)
        token = _make_keep_token(conn, "mi1", action_id)

        row = find_active_keep_action_by_id_and_token(conn, action_id, token)
        assert row is None

    def test_find_active_returns_none_for_wrong_token(self, conn):
        """Helper must return None when token does not match the hash in the row."""
        import time as _time

        from mediaman.crypto import generate_keep_token
        from mediaman.web.routes.keep import find_active_keep_action_by_id_and_token

        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn)
        _make_keep_token(conn, "mi1", action_id)

        # Use a different token
        wrong_token = generate_keep_token(
            media_item_id="mi1",
            action_id=action_id,
            expires_at=int(_time.time()) + 86400,
            secret_key="b" * 64,
        )
        row = find_active_keep_action_by_id_and_token(conn, action_id, wrong_token)
        assert row is None

    def test_schedule_deletion_writes_token_hash(self, conn):
        """schedule_deletion must write token_hash to the row."""
        from mediaman.scanner.phases.upsert import schedule_deletion

        _insert_media_item(conn)
        schedule_deletion(
            conn,
            media_id="mi1",
            is_reentry=False,
            grace_days=7,
            secret_key=SECRET,
        )
        conn.commit()

        row = conn.execute(
            "SELECT token_hash, token FROM scheduled_actions WHERE media_item_id = 'mi1'"
        ).fetchone()
        assert row is not None
        assert row["token_hash"] is not None
        assert len(row["token_hash"]) == 64

    def test_raw_token_fallback_lookup(self, conn):
        """Lookup should fall back to raw token column for un-migrated rows."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_findings(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        # Simulate un-migrated row: clear the token_hash
        conn.execute("UPDATE scheduled_actions SET token_hash = NULL WHERE id = ?", (action_id,))
        conn.commit()

        # _lookup_verified_action should still find the row via raw token
        row = _lookup_verified_action(conn, token, SECRET)
        assert row is not None
