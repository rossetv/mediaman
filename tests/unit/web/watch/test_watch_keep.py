"""Keep from Watch: 7, 30 or 90 days on a pending deletion, as the newsletter keep does,
with History naming the person."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.scanner.repository.scheduled_actions import (
    claim_due_deletion,
    record_removed_files,
)
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository import watch_library as lib
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
    browse_api._UNDO.clear()
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
    assert resp.json() == {"ok": True, "undo": True}
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


def test_an_admin_is_told_the_keep_it_made_and_a_viewer_is_not(client, conn):
    _schedule(conn)
    assert (
        "keep"
        not in client.post("/api/watch/keep", json={"media_item_id": "100", "days": 7}).json()
    )
    client.post("/api/watch/unkeep", json={"media_item_id": "100"})
    insert_admin_user(conn, username="root", role="admin")
    client.cookies.set("session_token", create_session(conn, "root"))

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})

    assert resp.json()["keep"] == {
        "action": "snoozed",
        "execute_at": (NOW + timedelta(days=30)).isoformat(),
        "kept_at": NOW.isoformat(),
    }
    row = conn.execute("SELECT action, execute_at, snoozed_at FROM scheduled_actions").fetchone()
    assert list(row) == list(resp.json()["keep"].values())


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


def _schedule_past(conn) -> int:
    return insert_scheduled_action(
        conn, media_item_id="100", execute_at=(NOW - timedelta(days=1)).isoformat(), token="past"
    )


def test_a_deletion_past_its_date_can_be_kept_and_is_then_no_longer_deleted(client, conn):
    action_id = _schedule_past(conn)

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})

    assert resp.status_code == 200
    assert not claim_due_deletion(conn, action_id, NOW.isoformat())
    assert _action(conn, action_id)["action"] == "snoozed"


def test_a_keep_past_the_date_it_replaced_is_not_offered_to_be_undone(client, conn):
    _schedule_past(conn)

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})

    assert resp.json() == {"ok": True, "undo": False}
    assert browse_api._UNDO == {}
    assert client.post("/api/watch/unkeep", json={"media_item_id": "100"}).status_code == 409


def test_a_deletion_the_pass_has_claimed_cannot_be_kept(client, conn):
    action_id = _schedule_past(conn)
    assert claim_due_deletion(conn, action_id, NOW.isoformat())
    conn.commit()

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})

    assert resp.status_code == 404
    assert _action(conn, action_id)["action"] == "scheduled_deletion"


def test_a_claim_made_while_a_keep_is_on_its_way_refuses_it(client, conn, monkeypatch):
    action_id = _schedule_past(conn)
    seen = lib.pending_deletion

    def claimed_meanwhile(route_conn, *args):
        found = seen(route_conn, *args)
        assert claim_due_deletion(route_conn, action_id, NOW.isoformat())
        route_conn.commit()
        return found

    monkeypatch.setattr(lib, "pending_deletion", claimed_meanwhile)

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})

    assert resp.status_code == 409
    assert resp.json()["error"] == "already_processed"
    assert _action(conn, action_id)["action"] == "scheduled_deletion"


@pytest.mark.parametrize(("left", "status"), [([], 404), (["/m/b.mkv"], 200)])
def test_only_a_deletion_that_left_files_can_be_kept(client, conn, left, status):
    action_id = _schedule_past(conn)
    files = ["/m/a.mkv", *left]
    conn.execute(
        "UPDATE media_items SET tracked_files = ? WHERE id = '100'",
        (json.dumps([{"path": f, "size": 1} for f in files]),),
    )
    record_removed_files(conn, action_id, {"/m/a.mkv"}, 1, {"/m/a.mkv": 1}, left)
    conn.commit()

    resp = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})

    assert resp.status_code == status


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


def _keep_and_undo(client, days: int = 30):
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": days})
    return client.post("/api/watch/unkeep", json={"media_item_id": "100"})


def test_undo_puts_the_deletion_back_as_it_was(client, conn):
    action_id = _schedule(conn)
    row_sql = "SELECT * FROM scheduled_actions WHERE id = ?"
    before = dict(conn.execute(row_sql, (action_id,)).fetchone())

    resp = _keep_and_undo(client)

    assert resp.status_code == 200
    assert dict(conn.execute(row_sql, (action_id,)).fetchone()) == before
    audit = conn.execute(
        "SELECT action, actor FROM audit_log WHERE media_item_id = '100' ORDER BY id"
    ).fetchall()
    assert [tuple(a) for a in audit] == [("snoozed", "aoife"), ("unprotected", "aoife")]


def test_undo_lists_the_item_as_leaving_again(client, conn):
    _schedule(conn)
    _keep_and_undo(client)

    again = client.post("/api/watch/keep", json={"media_item_id": "100", "days": 7})

    assert again.status_code == 200


def test_undo_works_once(client, conn):
    action_id = _schedule(conn)
    _keep_and_undo(client)
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 90})

    client.post("/api/watch/unkeep", json={"media_item_id": "100"})
    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 409
    assert _action(conn, action_id)["action"] == "scheduled_deletion"


def test_only_the_person_who_kept_can_undo(client, conn):
    action_id = _schedule(conn)
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})
    insert_admin_user(conn, username="niamh", role="viewer")
    client.cookies.set("session_token", create_session(conn, "niamh"))

    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 409
    assert _action(conn, action_id)["action"] == "snoozed"


def test_a_keep_made_elsewhere_cannot_be_undone_here(client, conn):
    action_id = insert_scheduled_action(
        conn,
        media_item_id="100",
        action="snoozed",
        execute_at=(NOW + timedelta(days=30)).isoformat(),
        token="tok-100",
        token_used=1,
    )

    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 409
    assert _action(conn, action_id)["action"] == "snoozed"


def test_undo_is_refused_once_its_window_has_passed(client, conn, monkeypatch):
    action_id = _schedule(conn)
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})
    monkeypatch.setattr(
        browse_api, "now_utc", lambda: NOW + browse_api.UNDO_WINDOW + timedelta(seconds=1)
    )

    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 409
    assert _action(conn, action_id)["action"] == "snoozed"


def test_undo_is_refused_when_the_old_date_has_already_passed(client, conn, monkeypatch):
    action_id = insert_scheduled_action(
        conn,
        media_item_id="100",
        execute_at=(NOW + timedelta(seconds=30)).isoformat(),
        token="tok-100",
    )
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 7})
    monkeypatch.setattr(browse_api, "now_utc", lambda: NOW + timedelta(minutes=1))

    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 409
    assert _action(conn, action_id)["action"] == "snoozed"


def test_undo_leaves_a_keep_changed_since_alone(client, conn):
    action_id = _schedule(conn)
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})
    conn.execute(
        "UPDATE scheduled_actions SET execute_at = ?, snooze_duration = '90 days' WHERE id = ?",
        ((NOW + timedelta(days=90)).isoformat(), action_id),
    )
    conn.commit()

    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 409
    assert _action(conn, action_id)["snooze_duration"] == "90 days"


def test_undo_leaves_a_row_turned_into_a_forever_keep_alone(client, conn):
    action_id = _schedule(conn)
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})
    conn.execute(
        "UPDATE scheduled_actions SET action = 'protected_forever' WHERE id = ?", (action_id,)
    )
    conn.commit()
    row_sql = "SELECT * FROM scheduled_actions WHERE id = ?"
    before = dict(conn.execute(row_sql, (action_id,)).fetchone())

    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 409
    assert dict(conn.execute(row_sql, (action_id,)).fetchone()) == before


@pytest.mark.parametrize("media_item_id", [None, 100, "", "../etc"])
def test_undo_refuses_a_bad_item_id(client, media_item_id):
    resp = client.post("/api/watch/unkeep", json={"media_item_id": media_item_id})

    assert resp.status_code == 400


def test_undo_is_rate_limited_per_person(client, conn, monkeypatch):
    action_id = _schedule(conn)
    client.post("/api/watch/keep", json={"media_item_id": "100", "days": 30})
    monkeypatch.setattr(browse_api._KEEP_LIMITER, "check", lambda _key: False)

    resp = client.post("/api/watch/unkeep", json={"media_item_id": "100"})

    assert resp.status_code == 429
    assert _action(conn, action_id)["action"] == "snoozed"
