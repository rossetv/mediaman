"""Tests for the kept/protected media API routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from mediaman.web.routes.kept import router as kept_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action


def _insert_protection(conn, media_item_id: str, action: str = "protected_forever") -> None:
    # Snoozed/protected rows omit ``execute_at`` (the dashboard schedules-queue
    # ignores them); use the shared factory but null the execute_at default.
    insert_scheduled_action(
        conn,
        media_item_id=media_item_id,
        action=action,
        execute_at=None,
        token=f"tok-{media_item_id}",
    )


def _insert_season(
    conn, media_id: str, show_rating_key: str | None, show_title: str, season: int = 1
) -> None:
    """Insert a TV season with a specific show_rating_key / show_title."""
    insert_media_item(
        conn,
        id=media_id,
        title=f"{show_title} S{season}",
        media_type="tv_season",
        plex_rating_key=f"rk-{media_id}",
        file_path="/p",
        file_size_bytes=1,
        show_rating_key=show_rating_key,
        show_title=show_title,
        season_number=season,
    )


class TestApiKept:
    def test_requires_auth(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/kept")
        assert resp.status_code == 401

    def test_returns_empty(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/kept")
        assert resp.status_code == 200
        body = resp.json()
        assert "forever" in body
        assert "snoozed" in body
        assert body["forever"] == []
        assert body["snoozed"] == []

    def test_returns_protected_items(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        insert_media_item(
            conn, id="m1", title="Inception", plex_rating_key="rk1", file_size_bytes=0
        )
        _insert_protection(conn, "m1", "protected_forever")
        resp = client.get("/api/kept")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["forever"]) == 1
        assert body["forever"][0]["title"] == "Inception"


class TestApiUnprotect:
    def test_requires_auth(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 401

    def test_unprotect_not_found_returns_404(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_unprotect_happy_path(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        insert_media_item(conn, id="m1", title="Dune", plex_rating_key="rk1", file_size_bytes=0)
        _insert_protection(conn, "m1", "protected_forever")
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        remaining = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE media_item_id='m1' "
            "AND action='protected_forever'"
        ).fetchone()[0]
        assert remaining == 0
        audit = conn.execute("SELECT action FROM audit_log WHERE media_item_id='m1'").fetchone()
        assert audit is not None
        assert audit["action"] == "unprotected"


class TestApiShowSeasons:
    def test_requires_auth(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/show/rk_show/seasons")
        assert resp.status_code == 401

    def test_empty_returns_no_seasons(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/show/rk_nonexistent/seasons")
        assert resp.status_code == 200
        body = resp.json()
        assert body["seasons"] == []
        assert body["show_title"] == ""


class TestKeepShowIdorDefence:
    """C13 — /api/show/{key}/keep must not collide two shows sharing a title."""

    def test_seasons_owned_by_different_show_rejected(self, app_factory, authed_client, conn):
        """A season_id from a different show with the same title is refused."""
        # Two distinct shows, both titled "Kingdom", different rating keys.
        _insert_season(conn, "m-A", "rk-show-A", "Kingdom", season=1)
        _insert_season(conn, "m-B", "rk-show-B", "Kingdom", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        # Try to keep show A but pass show B's season id — must be rejected.
        resp = client.post(
            "/api/show/rk-show-A/keep",
            json={"duration": "forever", "season_ids": ["m-B"]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        # No protection row was created for m-B
        row = conn.execute("SELECT 1 FROM scheduled_actions WHERE media_item_id='m-B'").fetchone()
        assert row is None

    def test_unknown_rating_key_returns_409(self, app_factory, authed_client, conn):
        """A rating_key with no matching media_items row is refused with 409."""
        _insert_season(conn, "m-A", "rk-show-A", "Kingdom", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-does-not-exist/keep",
            json={"duration": "forever", "season_ids": ["m-A"]},
        )
        assert resp.status_code == 409
        assert resp.json()["ok"] is False

    def test_correct_ownership_still_allowed(self, app_factory, authed_client, conn):
        """Happy path — seasons with matching show_rating_key are accepted."""
        _insert_season(conn, "m-A1", "rk-show-A", "Kingdom", season=1)
        _insert_season(conn, "m-A2", "rk-show-A", "Kingdom", season=2)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-A/keep",
            json={"duration": "forever", "season_ids": ["m-A1", "m-A2"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions WHERE media_item_id IN ('m-A1','m-A2')"
        ).fetchone()
        assert rows["n"] == 2


class TestResolveShowRatingKey:
    """C13 helper unit tests."""

    def test_empty_supplied_key_is_refused(self, conn):
        from mediaman.web.routes.kept import _resolve_show_rating_key

        resolved, err = _resolve_show_rating_key(conn, "")
        assert resolved is None
        assert err is not None

    def test_known_key_resolves(self, conn):
        from mediaman.web.routes.kept import _resolve_show_rating_key

        _insert_season(conn, "m-A", "rk-known", "Show", season=1)
        resolved, err = _resolve_show_rating_key(conn, "rk-known")
        assert resolved == "rk-known"
        assert err is None

    def test_unknown_key_returns_error(self, conn):
        from mediaman.web.routes.kept import _resolve_show_rating_key

        resolved, err = _resolve_show_rating_key(conn, "rk-unknown")
        assert resolved is None
        assert err is not None


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------


class TestKeptPageRedirects:
    """Auth-gated /kept and /kept/page redirect behaviour."""

    def test_kept_unauthenticated_redirects_to_login(self, app_factory, conn):
        """GET /kept without a session redirects to /login."""
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/kept", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_kept_authenticated_redirects_to_library_kept(self, app_factory, authed_client, conn):
        """GET /kept with a valid session 301-redirects to /library?type=kept."""
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/kept", follow_redirects=False)
        assert resp.status_code == 301
        assert "library" in resp.headers["location"]
        assert "kept" in resp.headers["location"]

    def test_kept_page_unauthenticated_redirects_to_login(self, app_factory, conn):
        """GET /kept/page without a session redirects to /login."""
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/kept/page", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_protected_redirect_unauthenticated(self, app_factory, conn):
        """GET /protected without a session redirects to /login."""
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/protected", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_protected_redirect_authenticated(self, app_factory, authed_client, conn):
        """GET /protected with a valid session 301-redirects to /library?type=kept."""
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/protected", follow_redirects=False)
        assert resp.status_code == 301
        assert "library" in resp.headers["location"]


class TestApiRemoveShowKeep:
    """POST /api/show/{key}/remove — remove a show-level keep rule."""

    def test_remove_show_keep_requires_auth(self, app_factory, conn):
        """Removing a show keep without a session returns 401."""
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/show/rk-show/remove")
        assert resp.status_code == 401

    def test_remove_nonexistent_show_keep_returns_404(self, app_factory, authed_client, conn):
        """Removing a keep rule that does not exist returns 404."""
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.post("/api/show/rk-does-not-exist/remove")
        assert resp.status_code == 404

    def test_remove_show_keep_happy_path(self, app_factory, authed_client, conn):
        """Happy path: kept_shows row is deleted and ok=True returned."""
        _insert_season(conn, "m-X", "rk-show-X", "Galaxy Quest", season=1)

        # Create a keep rule first
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, created_at) "
            "VALUES ('rk-show-X', 'Galaxy Quest', 'protected_forever', ?)",
            (now,),
        )
        conn.commit()

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.post("/api/show/rk-show-X/remove")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        row = conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk-show-X'").fetchone()
        assert row is None


class TestKeepShowSnoozed:
    """POST /api/show/{key}/keep with a finite duration stores a snoozed action."""

    def test_keep_show_with_duration_stores_snoozed(self, app_factory, authed_client, conn):
        """Keep with a finite duration inserts snoozed rows, not protected_forever."""
        _insert_season(conn, "m-Z", "rk-show-Z", "Firefly", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-Z/keep",
            json={"duration": "30 days", "season_ids": ["m-Z"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        sa = conn.execute(
            "SELECT action, snooze_duration FROM scheduled_actions WHERE media_item_id='m-Z'"
        ).fetchone()
        assert sa is not None
        assert sa["action"] == "snoozed"
        assert sa["snooze_duration"] == "30 days"

    def test_keep_show_invalid_duration_returns_400(self, app_factory, authed_client, conn):
        """An unrecognised duration is rejected with 400."""
        _insert_season(conn, "m-Y", "rk-show-Y", "Futurama", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-Y/keep",
            json={"duration": "2 years", "season_ids": ["m-Y"]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_keep_show_no_seasons_returns_400(self, app_factory, authed_client, conn):
        """Submitting an empty season_ids list returns 400."""
        _insert_season(conn, "m-Q", "rk-show-Q", "The Wire", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-Q/keep",
            json={"duration": "forever", "season_ids": []},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
