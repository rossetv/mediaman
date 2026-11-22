"""Tests for show-level keep API endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action

# NOTE: this file deliberately keeps its own ``app`` / ``authed_client``
# fixtures rather than adopting the shared ``app_factory`` / ``authed_client``
# in tests/unit/web/conftest.py. The show-keep flow leans on Origin-header
# checks and other middleware from ``create_app()``; the router-level
# shared fixture omits those.


@pytest.fixture
def app(db_path, secret_key):
    conn = init_db(str(db_path))
    set_connection(conn)
    application = create_app()
    application.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    application.state.db = conn
    yield application
    conn.close()


@pytest.fixture
def authed_client(app):
    from mediaman.web.auth.password_hash import create_user
    from mediaman.web.auth.session_store import create_session

    conn = app.state.db
    create_user(conn, "testadmin", "testpass123", enforce_policy=False)
    token = create_session(conn, "testadmin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    client.headers.update({"Origin": "http://testserver"})
    return client


def _insert_season(conn, item_id, show_title, season_num, show_rating_key):
    insert_media_item(
        conn,
        id=item_id,
        title=show_title,
        media_type="tv_season",
        show_title=show_title,
        season_number=season_num,
        plex_library_id=2,
        plex_rating_key=item_id,
        show_rating_key=show_rating_key,
        file_path="/media/tv/test",
        file_size_bytes=5000000000,
    )


class TestShowSeasonsAPI:
    def test_returns_seasons_for_show(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        _insert_season(conn, "102", "Breaking Bad", 2, "100")
        resp = authed_client.get("/api/show/100/seasons")
        assert resp.status_code == 200
        data = resp.json()
        assert data["show_title"] == "Breaking Bad"
        assert len(data["seasons"]) == 2

    def test_returns_empty_for_unknown_show(self, authed_client):
        resp = authed_client.get("/api/show/999/seasons")
        assert resp.status_code == 200
        assert resp.json()["seasons"] == []

    def test_requires_auth(self, app):
        client = TestClient(app)
        resp = client.get("/api/show/100/seasons")
        assert resp.status_code == 401


class TestShowKeepAPI:
    def test_keeps_show_forever(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        _insert_season(conn, "102", "Breaking Bad", 2, "100")
        resp = authed_client.post(
            "/api/show/100/keep",
            json={
                "duration": "forever",
                "season_ids": ["101", "102"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='100'").fetchone()
        assert row is not None
        assert row["action"] == "protected_forever"
        actions = conn.execute(
            "SELECT * FROM scheduled_actions WHERE action='protected_forever'"
        ).fetchall()
        assert len(actions) == 2

    def test_keeps_show_timed(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        resp = authed_client.post(
            "/api/show/100/keep",
            json={
                "duration": "30 days",
                "season_ids": ["101"],
            },
        )
        assert resp.status_code == 200
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='100'").fetchone()
        assert row["action"] == "snoozed"
        assert row["execute_at"] is not None

    def test_a_timed_show_keep_replaces_an_emailed_keep_forever(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        insert_scheduled_action(
            conn, media_item_id="101", action="protected_forever", token_used=True, execute_at=None
        )
        resp = authed_client.post(
            "/api/show/100/keep", json={"duration": "30 days", "season_ids": ["101"]}
        )
        assert resp.status_code == 200
        rows = conn.execute(
            "SELECT action, snooze_duration, token_used FROM scheduled_actions "
            "WHERE media_item_id='101'"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("snoozed", "30 days", 0)]

    def test_a_show_keep_converts_the_row_being_deleted_in_place(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        keep_id = insert_scheduled_action(
            conn,
            media_item_id="101",
            action="protected_forever",
            token_used=True,
            token="emailed-keep-token",
        )
        deleting_id = insert_scheduled_action(
            conn,
            media_item_id="101",
            action="scheduled_deletion",
            token="deleting-row-token",
            delete_status="deleting",
            execute_at="2020-01-01T00:00:00+00:00",
        )
        assert keep_id < deleting_id
        resp = authed_client.post(
            "/api/show/100/keep", json={"duration": "30 days", "season_ids": ["101"]}
        )
        assert resp.json()["deletion_running"] is True
        rows = conn.execute(
            "SELECT id, action, delete_status FROM scheduled_actions WHERE media_item_id='101'"
        ).fetchall()
        assert [tuple(r) for r in rows] == [(deleting_id, "snoozed", "deleting")]

    def test_rejects_empty_season_ids(self, authed_client):
        resp = authed_client.post(
            "/api/show/100/keep",
            json={
                "duration": "forever",
                "season_ids": [],
            },
        )
        assert resp.status_code == 400


class TestShowRemoveAPI:
    def test_removes_show_keep(self, authed_client, app):
        conn = app.state.db
        insert_kept_show(
            conn, show_rating_key="100", show_title="Breaking Bad", action="protected_forever"
        )
        resp = authed_client.post("/api/show/100/remove")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='100'").fetchone()
        assert row is None

    def test_returns_404_for_unknown(self, authed_client):
        resp = authed_client.post("/api/show/999/remove")
        assert resp.status_code == 404

    def test_removes_show_keep_whose_key_contains_a_slash(self, authed_client, app):
        conn = app.state.db
        insert_kept_show(
            conn, show_rating_key="Love/Hate", show_title="Love/Hate", action="protected_forever"
        )
        resp = authed_client.post("/api/show/Love%2FHate/remove")
        assert resp.status_code == 200
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='Love/Hate'").fetchone()
        assert row is None


class TestUntickingASeasonOnAShowKeep:
    def test_the_dialog_sequence_leaves_only_the_ticked_season_kept(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        _insert_season(conn, "102", "Breaking Bad", 2, "100")
        authed_client.post(
            "/api/show/100/keep", json={"duration": "forever", "season_ids": ["101", "102"]}
        )

        assert (
            authed_client.post("/api/media/101/keep", data={"duration": "7 days"}).status_code
            == 200
        )
        assert authed_client.post("/api/show/100/remove").status_code == 200
        assert authed_client.post("/api/media/102/unprotect").status_code == 200

        assert conn.execute("SELECT 1 FROM kept_shows").fetchone() is None
        rows = conn.execute(
            "SELECT media_item_id, action, snooze_duration FROM scheduled_actions"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("101", "snoozed", "7 days")]
        seasons = authed_client.get("/api/show/100/seasons").json()
        assert {s["id"]: s["kept"] for s in seasons["seasons"]} == {"101": True, "102": False}
