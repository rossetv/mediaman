"""Tests for security findings 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 34, 35.

Each test class maps to one finding.
"""

from __future__ import annotations

import json
import sqlite3
import time as _time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.auth.middleware import get_current_admin, get_optional_admin
from mediaman.config import Config
from mediaman.crypto import generate_download_token, generate_keep_token, generate_poll_token
from mediaman.db import init_db, set_connection
from mediaman.web import register_security_middleware
from mediaman.web.routes.download import status as _status_module
from mediaman.web.routes.keep import find_active_keep_action_by_id_and_token
from mediaman.web.routes.keep import router as keep_router
from mediaman.web.routes.recommended.api import router as rec_router

SECRET = "a" * 64


# ---------------------------------------------------------------------------
# Helpers shared by multiple tests
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


def _insert_media_item(conn: sqlite3.Connection, media_id: str = "mi1") -> None:
    conn.execute(
        "INSERT INTO media_items "
        "(id, title, media_type, plex_library_id, plex_rating_key, added_at, file_path, file_size_bytes) "
        "VALUES (?, 'Test Title', 'movie', 1, 'rk1', ?, '/f', 0)",
        (media_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _insert_scheduled_action(
    conn: sqlite3.Connection,
    media_id: str = "mi1",
    action: str = "scheduled_deletion",
    delete_status: str = "pending",
    execute_at_offset_days: int = 7,
) -> int:
    """Insert a scheduled_actions row and return the rowid."""
    execute_at = (datetime.now(timezone.utc) + timedelta(days=execute_at_offset_days)).isoformat()
    cur = conn.execute(
        "INSERT INTO scheduled_actions "
        "(media_item_id, action, scheduled_at, execute_at, token, delete_status) "
        "VALUES (?, ?, datetime('now'), ?, 'placeholder', ?)",
        (media_id, action, execute_at, delete_status),
    )
    conn.commit()
    return cur.lastrowid


def _make_keep_token(conn: sqlite3.Connection, media_id: str, action_id: int) -> str:
    token = generate_keep_token(
        media_item_id=media_id,
        action_id=action_id,
        expires_at=int(_time.time()) + 86400 * 180,
        secret_key=SECRET,
    )
    conn.execute("UPDATE scheduled_actions SET token=? WHERE id=?", (token, action_id))
    conn.commit()
    return token


def _make_keep_app(conn: sqlite3.Connection) -> tuple[FastAPI, TestClient]:
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
    return app, TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Finding 11: CSRF blocks no-Origin requests with session cookie
# ---------------------------------------------------------------------------


class TestFinding11CSRFNoOriginWithCookie:
    """Finding 11: unsafe requests with session cookie but no Origin must be rejected."""

    def _app(self):
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        return TestClient(app)

    def test_no_origin_no_cookie_allowed(self):
        """Non-browser client (no cookie, no origin) should be allowed through."""
        client = self._app()
        resp = client.post("/api/thing")
        assert resp.status_code == 200

    def test_no_origin_with_session_cookie_rejected(self):
        """Finding 11: POST with a session_token cookie but no Origin must return 403."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app)
        client.cookies.set("session_token", "some-session-token")
        resp = client.post("/api/thing")
        assert resp.status_code == 403
        assert b"CSRF" in resp.content

    def test_correct_origin_with_session_cookie_allowed(self):
        """Correct same-origin request with cookie should pass."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app)
        client.cookies.set("session_token", "some-session-token")
        resp = client.post("/api/thing", headers={"Origin": "http://testserver"})
        assert resp.status_code == 200

    def test_csrf_exempt_path_still_allowed_without_origin(self):
        """Keep/download paths are exempt — even with a cookie."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/keep/{token}")
        def _keep(token: str):
            return {"ok": True}

        client = TestClient(app)
        client.cookies.set("session_token", "some-session")
        resp = client.post("/keep/testtoken")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Finding 12: "forever" rejected on public route, accepted on admin endpoint
# ---------------------------------------------------------------------------


class TestFinding12ForeverEndpointSeparation:
    """Finding 12: forever must be refused on the public keep POST."""

    def test_forever_duration_rejected_on_public_post(self, conn):
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app(conn)
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

    def test_unauthenticated_forever_returns_401(self, conn):
        """Un-authed POST to /api/keep/{token}/forever must return 401."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app(conn)
        resp = client.post(f"/api/keep/{token}/forever")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Finding 13: Keep POST refuses expired / non-pending actions
# ---------------------------------------------------------------------------


class TestFinding13KeepDeadlineCheck:
    """Finding 13: keep_submit must reject rows that have expired or are not pending."""

    def test_expired_action_returns_400(self, conn):
        """A keep POST where execute_at is in the past must return 400."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn, execute_at_offset_days=-1)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app(conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_non_pending_delete_status_returns_400(self, conn):
        """A row with delete_status='deleting' must be refused."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn, delete_status="deleting")
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app(conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_non_deletion_action_returns_400(self, conn):
        """A keep POST against a 'snoozed' action (not 'scheduled_deletion') must fail."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn, action="snoozed")
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app(conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_valid_pending_action_succeeds(self, conn):
        """A valid keep POST against a pending scheduled_deletion must succeed."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app(conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code in (200, 302, 307)


# ---------------------------------------------------------------------------
# Finding 14: Status polling requires poll_token, not download token
# ---------------------------------------------------------------------------


class TestFinding14PollTokenRequired:
    """Finding 14: unauthenticated status polling must use poll_token, not download token."""

    def _make_status_app(self, conn: sqlite3.Connection) -> TestClient:
        app = FastAPI()
        from mediaman.web.routes.download import status as status_mod

        app.include_router(status_mod.router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        set_connection(conn)
        # Override get_optional_admin so no admin is injected
        app.dependency_overrides[get_optional_admin] = lambda: None
        return TestClient(app, raise_server_exceptions=True)

    def test_no_token_returns_401(self, conn):
        """Calling /api/download/status without any token must return 401."""
        client = self._make_status_app(conn)
        resp = client.get("/api/download/status", params={"service": "radarr", "tmdb_id": 42})
        assert resp.status_code == 401

    def test_download_token_no_longer_accepted(self, conn):
        """The long-lived download token must not be accepted for polling (finding 14)."""
        download_token = generate_download_token(
            email="test@example.com",
            action="download",
            title="Test",
            media_type="movie",
            tmdb_id=42,
            recommendation_id=None,
            secret_key=SECRET,
        )
        client = self._make_status_app(conn)
        # Passing the download token in the 'token' param must now be ignored
        # (the 'token' param was removed; the endpoint only knows poll_token).
        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "token": download_token},
        )
        # Must not authenticate — 401 expected
        assert resp.status_code == 401

    def test_valid_poll_token_accepted(self, conn):
        """A valid poll_token bound to the correct service/tmdb must authenticate."""
        from unittest.mock import patch

        poll_token = generate_poll_token(
            media_item_id="radarr:Test",
            service="radarr",
            tmdb_id=42,
            secret_key=SECRET,
        )
        client = self._make_status_app(conn)

        # Patch the service lookup so we don't need a real Radarr client
        with patch.object(
            _status_module,
            "_radarr_status",
            return_value={"state": "searching"},
        ):
            resp = client.get(
                "/api/download/status",
                params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
            )
        assert resp.status_code == 200

    def test_poll_token_wrong_service_rejected(self, conn):
        """A poll_token issued for 'sonarr' must not authenticate a 'radarr' request."""
        poll_token = generate_poll_token(
            media_item_id="sonarr:Test",
            service="sonarr",
            tmdb_id=42,
            secret_key=SECRET,
        )
        client = self._make_status_app(conn)
        resp = client.get(
            "/api/download/status",
            params={"service": "radarr", "tmdb_id": 42, "poll_token": poll_token},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Finding 15: Refuse to mint public download token without TMDB id
# ---------------------------------------------------------------------------


class TestFinding15MintRequiresTmdbId:
    """Finding 15: share-token mint must refuse recommendations without a TMDB id."""

    def _make_rec_app(self, conn: sqlite3.Connection) -> TestClient:
        app = FastAPI()
        app.include_router(rec_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        set_connection(conn)
        # Override auth so admin is always provided
        app.dependency_overrides[get_current_admin] = lambda: "admin"
        return TestClient(app, raise_server_exceptions=True)

    def _insert_suggestion(self, conn: sqlite3.Connection, tmdb_id: int | None = None) -> int:
        cur = conn.execute(
            "INSERT INTO suggestions "
            "(title, media_type, category, tmdb_id, created_at) "
            "VALUES ('Test Movie', 'movie', 'personal', ?, datetime('now'))",
            (tmdb_id,),
        )
        conn.commit()
        return cur.lastrowid

    def test_mint_without_tmdb_id_returns_422(self, conn):
        """Minting a share token for a suggestion without tmdb_id must return 422."""
        sid = self._insert_suggestion(conn, tmdb_id=None)
        client = self._make_rec_app(conn)
        resp = client.post(f"/api/recommended/{sid}/share-token")
        assert resp.status_code == 422
        body = resp.json()
        assert not body.get("ok")
        assert "TMDB" in body.get("error", "")

    def test_mint_with_tmdb_id_would_succeed_if_base_url_set(self, conn):
        """Minting with a tmdb_id present does not fail on the identifier check."""
        sid = self._insert_suggestion(conn, tmdb_id=12345)
        client = self._make_rec_app(conn)
        resp = client.post(f"/api/recommended/{sid}/share-token")
        # Should NOT return 422; may return 200 or other error (e.g. missing base_url)
        assert resp.status_code != 422


class TestFinding15NewsletterSkipsMintWithoutTmdb:
    """Finding 15 (H-1): newsletter must skip redownload mint when no tmdb_id.

    The previous code hardcoded ``tmdb_id=None`` for deleted items, producing
    a public token whose submit fell back to ``lookup_by_term(title)``.  The
    fix is to omit the redownload URL entirely when the deleted item carries
    no stable identifier.  The template hides the button via
    ``{% if item.redownload_url %}``.
    """

    def test_deleted_item_without_tmdb_has_empty_redownload_url(self):
        from unittest.mock import MagicMock

        from mediaman.services.mail.newsletter.recipients import _send_to_recipients

        captured: dict = {}

        class _FakeTemplate:
            def render(self, **kwargs):
                captured.update(kwargs)
                return "<html></html>"

        mailgun_stub = MagicMock()
        mailgun_stub.send_message.return_value = True

        deleted_no_tmdb = [{"title": "Ambiguous Title", "media_type": "movie"}]
        deleted_with_tmdb = [{"title": "Specific Film", "media_type": "movie", "tmdb_id": 12345}]

        _send_to_recipients(
            recipient_emails=["dest@example.com"],
            scheduled_items=[],
            deleted_items=deleted_no_tmdb + deleted_with_tmdb,
            this_week_items=[],
            storage={"total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "by_type": {}},
            reclaimed_week=0,
            reclaimed_month=0,
            reclaimed_total=0,
            subject="x",
            base_url="https://mm.example.com",
            secret_key=SECRET,
            dry_run=False,
            grace_days=7,
            template=_FakeTemplate(),
            mailgun=mailgun_stub,
            report_date="2026-05-01",
            conn=None,
        )

        rendered_deleted = captured["deleted_items"]
        assert len(rendered_deleted) == 2
        # No tmdb_id → no redownload link, regardless of base_url.
        assert rendered_deleted[0]["redownload_url"] == ""
        # With tmdb_id → token-bearing URL.
        assert rendered_deleted[1]["redownload_url"].startswith("https://mm.example.com/download/")


# ---------------------------------------------------------------------------
# Finding 16: Keep token hash storage
# ---------------------------------------------------------------------------


class TestFinding16KeepTokenHash:
    """Finding 16: token hash helpers and insert-only-hash logic."""

    def test_find_active_keep_action_by_id_and_token(self, conn):
        """Helper must return the row when token hash matches and conditions are met."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = find_active_keep_action_by_id_and_token(conn, action_id, token)
        assert row is not None
        assert row["id"] == action_id

    def test_find_active_returns_none_for_expired(self, conn):
        """Helper must return None when execute_at is in the past."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn, execute_at_offset_days=-1)
        token = _make_keep_token(conn, "mi1", action_id)

        row = find_active_keep_action_by_id_and_token(conn, action_id, token)
        assert row is None

    def test_find_active_returns_none_for_wrong_token(self, conn):
        """Helper must return None when token does not match the hash in the row."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn)
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
        from mediaman.scanner.repository import schedule_deletion

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
        assert row["token_hash"] is not None and len(row["token_hash"]) == 64

    def test_raw_token_fallback_lookup(self, conn):
        """Lookup should fall back to raw token column for un-migrated rows."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        # Simulate un-migrated row: clear the token_hash
        conn.execute("UPDATE scheduled_actions SET token_hash = NULL WHERE id = ?", (action_id,))
        conn.commit()

        # _lookup_verified_action should still find the row via raw token
        from mediaman.web.routes.keep import _lookup_verified_action

        row = _lookup_verified_action(conn, token, SECRET)
        assert row is not None


# ---------------------------------------------------------------------------
# Finding 19: Trailer key validation
# ---------------------------------------------------------------------------


class TestFinding19TrailerKeyValidation:
    """Finding 19: trailer key must be exactly 11 URL-safe base64 characters."""

    def _validate(self, key: str) -> bool:
        from mediaman.web.routes.download.confirm import validate_youtube_id

        return validate_youtube_id(key) is not None

    def test_valid_11_char_key_accepted(self):
        assert self._validate("dQw4w9WgXcQ")

    def test_10_char_key_rejected(self):
        assert not self._validate("dQw4w9WgXc")

    def test_12_char_key_rejected(self):
        assert not self._validate("dQw4w9WgXcQQ")

    def test_key_with_invalid_chars_rejected(self):
        assert not self._validate("dQw4w9WgX!Q")

    def test_none_returns_none(self):
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id(None) is None

    def test_empty_string_returns_none(self):
        from mediaman.web.routes.download.confirm import validate_youtube_id

        assert validate_youtube_id("") is None


# ---------------------------------------------------------------------------
# Finding 20: CSP img-src tightened
# ---------------------------------------------------------------------------


class TestFinding20CspImgSrc:
    """Finding 20: CSP img-src must not allow arbitrary https: images."""

    def test_csp_does_not_allow_arbitrary_https(self):
        # Parse out the img-src directive value and check it is not the
        # permissive bare-https: wildcard.  The new CSP has individual HTTPS
        # host entries; "https:" without a host suffix would match anything.
        import re

        from mediaman.web import _CSP

        m = re.search(r"img-src ([^;]+)", _CSP)
        assert m, "img-src directive not found in CSP"
        img_src_value = m.group(1).strip()
        # The bare scheme token "https:" allows any HTTPS host.
        # Ensure no bare https: appears (host-qualified https://host is fine).
        tokens = img_src_value.split()
        assert "https:" not in tokens, (
            f"img-src still contains bare 'https:' wildcard — tokens: {tokens}"
        )

    def test_csp_allows_tmdb(self):
        from mediaman.web import _CSP

        assert "image.tmdb.org" in _CSP

    def test_csp_allows_ytimg(self):
        from mediaman.web import _CSP

        assert "i.ytimg.com" in _CSP


# ---------------------------------------------------------------------------
# Finding 34: Dashboard re-download passes stable identifiers
# ---------------------------------------------------------------------------


class TestFinding34DashboardRedownload:
    """Finding 34: the redownload button must pass media_item_id and media_type."""

    def test_dashboard_item_includes_media_type(self, conn, tmp_path):
        """_fetch_recently_deleted must populate media_type in the returned dict."""
        from mediaman.web.routes.dashboard import _fetch_recently_deleted

        conn.execute(
            "INSERT INTO media_items "
            "(id, title, media_type, plex_library_id, plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('m1', 'Test', 'movie', 1, 'rk1', '2024-01-01', '/f', 0)"
        )
        conn.execute(
            "INSERT INTO audit_log "
            "(media_item_id, action, detail, space_reclaimed_bytes, created_at) "
            "VALUES ('m1', 'deleted', 'detail', 1024, '2024-06-01')"
        )
        conn.commit()
        set_connection(conn)

        items = _fetch_recently_deleted(conn)
        assert len(items) >= 1
        item = next(i for i in items if i["media_item_id"] == "m1")
        assert "media_type" in item
        assert item["media_type"] == "movie"
        assert "media_item_id" in item


# ---------------------------------------------------------------------------
# Finding 35: Bulk keep/remove honours response.ok
# ---------------------------------------------------------------------------


class TestFinding35BulkKeepResponseOk:
    """Finding 35: library bulk-keep JS must check response.ok; test verifiable via the API.

    The JS change is template-level; these tests verify the server side
    (keep API returns appropriate status codes for bad requests) and a
    smoke-test that the template JS no longer uses Promise.all with no
    error-handling (checked via file content, not runtime execution).
    """

    def test_library_template_does_not_use_unchecked_promise_all(self):
        """The library.html template must not have the old unchecked Promise.all pattern."""
        import pathlib

        tmpl = pathlib.Path("src/mediaman/web/templates/library.html").read_text()
        # Old pattern was: Promise.all(promises).then(function () { window.location.reload(); })
        # New pattern checks response.ok. The old verbatim one-liner should be gone.
        assert (
            "Promise.all(promises).then(function () { window.location.reload(); })" not in tmpl
        ), "Old unchecked Promise.all found — failed requests were silently ignored"

    def test_library_template_has_response_ok_check(self):
        """The new bulk-keep code must check response.ok."""
        import pathlib

        tmpl = pathlib.Path("src/mediaman/web/templates/library.html").read_text()
        assert "if (!r.ok)" in tmpl or "response.ok" in tmpl, (
            "No response.ok check found in library.html bulk-keep JS"
        )
