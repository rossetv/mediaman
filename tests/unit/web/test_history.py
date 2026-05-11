"""Tests for the history API — paginated audit log with action-type filter."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.history import _PER_PAGE_DEFAULT, _PER_PAGE_MAX
from mediaman.web.routes.history import router as history_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(history_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


def _insert_audit_row(conn, action: str = "scanned", media_item_id: str = "m1") -> None:
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, created_at) VALUES (?, ?, ?)",
        (media_item_id, action, datetime.now(UTC).isoformat()),
    )
    conn.commit()


class TestApiHistory:
    def test_history_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/history")
        assert resp.status_code == 401

    def test_history_empty_returns_valid_shape(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["page"] == 1
        assert "per_page" in body
        assert "total_pages" in body

    def test_history_returns_rows(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        for action in ("scanned", "deleted", "kept"):
            _insert_audit_row(conn, action=action)
        resp = client.get("/api/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        item = body["items"][0]
        assert "action" in item
        assert "created_at" in item

    def test_history_action_filter(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_audit_row(conn, action="deleted", media_item_id="m1")
        _insert_audit_row(conn, action="deleted", media_item_id="m2")
        _insert_audit_row(conn, action="scanned", media_item_id="m3")
        resp = client.get("/api/history?action=deleted")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(i["action"] == "deleted" for i in body["items"])

    def test_history_invalid_action_filter_ignored(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_audit_row(conn, action="scanned", media_item_id="m1")
        _insert_audit_row(conn, action="deleted", media_item_id="m2")
        resp = client.get("/api/history?action=bogus_action_xyz")
        assert resp.status_code == 200
        # Invalid filter is silently dropped — all rows returned
        assert resp.json()["total"] == 2

    def test_history_pagination(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        for i in range(5):
            _insert_audit_row(conn, media_item_id=f"m{i}")

        resp = client.get("/api/history?per_page=2&page=1")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        assert resp.json()["total_pages"] == 3

        resp = client.get("/api/history?per_page=2&page=3")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    def test_per_page_max_enforced(self, db_path, secret_key):
        """per_page above the maximum must be clamped/rejected by the Query constraint."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get(f"/api/history?per_page={_PER_PAGE_MAX + 1}")
        # FastAPI Query(le=...) returns 422 Unprocessable Entity for out-of-range values.
        assert resp.status_code == 422

    def test_per_page_zero_rejected(self, db_path, secret_key):
        """per_page=0 must be rejected."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/history?per_page=0")
        assert resp.status_code == 422

    def test_shared_per_page_constants(self):
        """_PER_PAGE_DEFAULT and _PER_PAGE_MAX are within sensible ranges."""
        assert 1 <= _PER_PAGE_DEFAULT <= _PER_PAGE_MAX
        assert _PER_PAGE_MAX <= 100


def _insert_audit_full(
    conn,
    *,
    action: str,
    media_item_id: str,
    detail: str | None = None,
) -> None:
    """Insert an audit row with optional detail body — used by the
    title-resolution / detail-scrubbing tests."""
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
        (media_item_id, action, detail, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _insert_media_item(conn, *, id_: str, title: str, media_type: str = "movie") -> None:
    """Insert a minimal media_items row so the JOIN can resolve a title."""
    conn.execute(
        """
        INSERT INTO media_items
            (id, title, media_type, plex_library_id, plex_rating_key,
             added_at, file_path, file_size_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (id_, title, media_type, 1, id_, "2025-01-01T00:00:00Z", "/tmp/x", 0),
    )
    conn.commit()


def _insert_kept_show(conn, *, rating_key: str, title: str) -> None:
    """Insert a kept_shows row so the show-action JOIN can resolve."""
    conn.execute(
        """
        INSERT INTO kept_shows
            (show_rating_key, show_title, action, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (rating_key, title, "kept_show", datetime.now(UTC).isoformat()),
    )
    conn.commit()


class TestHistoryPageClamp:
    """``GET /history`` must clamp ``?page=`` to ``total_pages`` so a stale
    or hostile URL doesn't run a wasteful OFFSET sweep AND doesn't render
    a misleading ``Page 9999 of 1`` footer."""

    def _make_page_app(self, conn):
        from unittest.mock import MagicMock

        from fastapi.responses import HTMLResponse

        app = _make_app(conn, "0123456789abcdef" * 4)
        mock_templates = MagicMock()

        def fake_template_response(request, template_name, ctx):
            # Echo the rendered ``page`` and ``total_pages`` numbers so the
            # test can assert against the clamped values.
            return HTMLResponse(
                f"page={ctx['page']};total_pages={ctx['total_pages']};total={ctx['total']}",
                status_code=200,
            )

        mock_templates.TemplateResponse.side_effect = fake_template_response
        app.state.templates = mock_templates
        return app

    def test_page_clamped_to_total_pages(self, db_path):
        conn = init_db(str(db_path))
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        # Insert 3 rows — one page at per_page=25.
        for i in range(3):
            _insert_audit_row(conn, media_item_id=f"m{i}")

        app = self._make_page_app(conn)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.get("/history?page=10000")
        assert resp.status_code == 200
        # Clamped to 1 (total_pages=1 for 3 rows).
        assert "page=1;" in resp.text
        assert "total_pages=1" in resp.text

    def test_negative_page_clamped_to_one(self, db_path):
        conn = init_db(str(db_path))
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        _insert_audit_row(conn, media_item_id="m0")

        app = self._make_page_app(conn)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        # ``int("-5")`` is valid but max(1, -5) clamps the lower end.
        resp = client.get("/history?page=-5")
        assert "page=1;" in resp.text


class TestKeptShowJoinDoesNotLeakMovieTitle:
    """The kept_show audit JOIN must not pick up a movie title even when
    the rating-key namespaces collide (defensive: today's Plex doesn't
    surface that, but a future migration could)."""

    def test_kept_show_resolves_to_show_title(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        # Same rating-key value exists as both a movie and a show.
        _insert_media_item(conn, id_="42", title="Wrong Movie Title", media_type="movie")
        _insert_kept_show(conn, rating_key="42", title="Right Show Title")
        _insert_audit_full(conn, action="kept_show", media_item_id="42")

        resp = client.get("/api/history?action=kept_show")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        # The kept_show JOIN must produce the show title, not the movie title.
        assert items[0]["title"] == "Right Show Title"

    def test_movie_kept_does_not_pull_show_title(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        # Set up a movie audit row whose rating-key happens to collide
        # with a kept_show entry.
        _insert_media_item(conn, id_="99", title="Real Movie", media_type="movie")
        _insert_kept_show(conn, rating_key="99", title="Wrong Show")
        _insert_audit_full(conn, action="kept", media_item_id="99")

        resp = client.get("/api/history?action=kept")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        # ``kept`` is NOT a show action, so the kept_shows JOIN must miss
        # and the title comes from media_items.
        assert items[0]["title"] == "Real Movie"


class TestDetailControlByteScrubbing:
    """The ``detail`` blob is rendered into the history page UI and the
    JSON API.  A future audit row whose detail carried a CR/LF or a
    terminal escape must not corrupt either."""

    def test_control_bytes_removed_from_response(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        _insert_audit_full(
            conn,
            action="scanned",
            media_item_id="m1",
            detail="line1\x00\x1b[31mred\x07bell",
        )

        resp = client.get("/api/history")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        detail = items[0]["detail"]
        assert "\x00" not in detail
        assert "\x1b" not in detail
        assert "\x07" not in detail

    def test_visible_whitespace_preserved(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        _insert_audit_full(
            conn,
            action="scanned",
            media_item_id="m1",
            detail="line1\nline2\tcol2",
        )

        resp = client.get("/api/history")
        items = resp.json()["items"]
        # Newline and tab are visible whitespace — must NOT be stripped.
        assert "\n" in items[0]["detail"]
        assert "\t" in items[0]["detail"]


class TestSecurityTitleHoist:
    """For ``sec:*`` rows the title must be the event name, not a
    parsed snippet of the detail — even when the detail happens to
    contain a single-quoted phrase that the regex would otherwise grab."""

    def test_security_title_does_not_pull_quoted_string_from_detail(self, db_path, secret_key):
        from mediaman.core.audit import security_event

        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        # Detail body contains a single-quoted phrase that the legacy
        # regex would have lifted into the page title.
        security_event(
            conn,
            event="settings.write",
            actor="admin",
            ip="127.0.0.1",
            detail="rotated 'plex_token' value",
        )

        resp = client.get("/api/history?action=security")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        # Must be the event name, not "plex_token".
        assert items[0]["title"] == "settings.write"
        assert items[0]["title"] != "plex_token"
