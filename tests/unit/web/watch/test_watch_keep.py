"""Keep from Watch: 7, 30 or 90 days on a pending deletion, as the newsletter keep does,
with History naming the person."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import browse_api
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(app_factory, conn, monkeypatch):
    monkeypatch.setattr(browse_api, "now_utc", lambda: NOW)
    browse_api._KEEP_LIMITER.reset()
    insert_settings(conn, plex_libraries='["1"]')
    insert_admin_user(conn, username="aoife", role="viewer")
    insert_media_item(conn, id="100", plex_rating_key="100", title="Glasshouse")
    insert_media_item(conn, id="900", plex_rating_key="900", title="Hidden", plex_library_id=9)
    app = app_factory(browse_api.router, conn=conn)
    test_client = TestClient(app)
    test_client.cookies.set("session_token", create_session(conn, "aoife"))
    return test_client


def _schedule(conn, media_item_id: str = "100") -> int:
    return insert_scheduled_action(
        conn,
        media_item_id=media_item_id,
        execute_at=(NOW + timedelta(days=2)).isoformat(),
        token=f"tok-{media_item_id}",
    )


def _action(conn, action_id: int):
    return conn.execute(
        "SELECT action, execute_at, snooze_duration, token_used FROM scheduled_actions WHERE id = ?",
        (action_id,),
    ).fetchone()


@pytest.mark.parametrize("days", [7, 30, 90])
def test_keep_snoozes_the_deletion_and_names_the_person(client, conn, days):
    action_id = _schedule(conn)

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": days})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    row = _action(conn, action_id)
    assert row["action"] == "snoozed"
    assert row["snooze_duration"] == f"{days} days"
    assert row["execute_at"] == (NOW + timedelta(days=days)).isoformat()
    audit = conn.execute(
        "SELECT action, detail, actor FROM audit_log WHERE media_item_id = '100'"
    ).fetchone()
    assert dict(audit) == {
        "action": "snoozed",
        "detail": f"Kept for {days} days by aoife",
        "actor": "aoife",
    }


@pytest.mark.parametrize("days", ["forever", 0, 14, "30", True, None, 30.0])
def test_other_durations_are_refused(client, conn, days):
    action_id = _schedule(conn)

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": days})

    assert resp.status_code == 400
    assert _action(conn, action_id)["action"] == "scheduled_deletion"


def test_item_without_a_pending_deletion_is_404(client):
    assert (
        client.post("/api/watch/keep", json={"media_item_id": "100", "days": 7}).status_code == 404
    )


def test_kept_item_cannot_be_kept_again(client, conn):
    _schedule(conn)
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 7})

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 90})

    assert resp.status_code == 404


def test_item_outside_scanned_libraries_is_404(client, conn):
    action_id = _schedule(conn, "900")

    resp = client.post("/api/watch/keep", json={"media_item_id": "900", "days": 7})

    assert resp.status_code == 404
    assert _action(conn, action_id)["action"] == "scheduled_deletion"


def test_keeps_are_rate_limited_per_person(client, conn, monkeypatch):
    monkeypatch.setattr(browse_api._KEEP_LIMITER, "check", lambda _key: False)
    action_id = _schedule(conn)

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 7})

    assert resp.status_code == 429
    assert _action(conn, action_id)["action"] == "scheduled_deletion"
