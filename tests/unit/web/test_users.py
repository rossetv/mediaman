"""Tests for user management API routes (list, create, delete, change-password, sessions)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.services.avatars.drawn import default_choice
from mediaman.services.plex_link import repository as plex_links
from mediaman.services.plex_link.plex_tv import PlexIdentity
from mediaman.web.auth.password_hash import authenticate, create_user
from mediaman.web.auth.reauth import grant_recent_reauth, has_recent_reauth
from mediaman.web.auth.session_store import create_session, list_sessions_for, validate_session
from mediaman.web.repository.avatars import avatar_url, find_user_id, set_drawn
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
    _SESSIONS_LIST_LIMITER,
    _SESSIONS_REVOKE_LIMITER,
    _UNTICKETED_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router
from tests.helpers.factories import insert_download_notification


def _app(app_factory, conn):
    return app_factory(users_router, conn=conn)


def _client(app_factory, authed_client, conn, *, with_reauth: bool = True):
    """Build the user-routes app + an authed client (reauth on by default)."""
    return authed_client(_app(app_factory, conn), conn, with_reauth=with_reauth)


def _make_second_user(conn, username: str = "other") -> int:
    create_user(conn, username, "OtherPass!99", enforce_policy=False)
    row = conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()
    return row["id"]


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    for lim in (
        _USER_MGMT_LIMITER,
        _USER_CREATE_LIMITER,
        _REAUTH_LIMITER,
        _PASSWORD_CHANGE_LIMITER,
        _PASSWORD_CHANGE_IP_LIMITER,
        _SESSIONS_LIST_LIMITER,
        _SESSIONS_REVOKE_LIMITER,
        _UNTICKETED_LIMITER,
    ):
        lim.reset()
    yield
    for lim in (
        _USER_MGMT_LIMITER,
        _USER_CREATE_LIMITER,
        _REAUTH_LIMITER,
        _PASSWORD_CHANGE_LIMITER,
        _PASSWORD_CHANGE_IP_LIMITER,
        _SESSIONS_LIST_LIMITER,
        _SESSIONS_REVOKE_LIMITER,
        _UNTICKETED_LIMITER,
    ):
        lim.reset()


class TestListUsers:
    def test_list_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/users")
        assert resp.status_code == 401

    def test_list_returns_users(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.get("/api/users")
        assert resp.status_code == 200
        body = resp.json()
        assert "users" in body
        assert "current" in body
        assert body["current"] == "admin"
        assert len(body["users"]) == 1
        assert body["users"][0]["username"] == "admin"

    def test_list_gives_each_user_s_own_picture(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        _make_second_user(conn)
        second = find_user_id(conn, "other")
        set_drawn(conn, second, 10 if default_choice(second) != 10 else 9)

        users = client.get("/api/users").json()["users"]

        assert {u["username"]: u["avatar_url"] for u in users} == {
            name: avatar_url(conn, name) for name in ("admin", "other")
        }

    def test_list_says_since_when_each_user_has_plex_linked_and_nothing_of_the_account(
        self, app_factory, authed_client, conn
    ):
        client = _client(app_factory, authed_client, conn)
        _make_second_user(conn)
        plex_links.save_link(
            conn, "other", PlexIdentity(3141592653, 3141592653, "secretname"), "server", ip=""
        )

        resp = client.get("/api/users")

        since = plex_links.linked_users(conn)[0]["linked_at"]
        assert {u["username"]: u["plex_linked_at"] for u in resp.json()["users"]} == {
            "admin": None,
            "other": since,
        }
        assert "secretname" not in resp.text
        assert "3141592653" not in resp.text


class TestCreateUser:
    def test_create_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/users", json={"username": "newadmin", "password": "ValidPass!99"})
        assert resp.status_code == 401

    def test_create_user_happy_path(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "newadmin", "password": "ValidPass!99"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "username": "newadmin"}
        row = conn.execute("SELECT id FROM admin_users WHERE username='newadmin'").fetchone()
        assert row is not None

    def test_create_user_short_username_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "ab", "password": "ValidPass!99"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_username"

    def test_create_user_long_username_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "a" * 65, "password": "ValidPass!99"})
        assert resp.status_code == 400

    def test_create_user_weak_password_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "validname", "password": "short"})
        assert resp.status_code == 400
        assert "issues" in resp.json()

    def test_create_user_duplicate_returns_409(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post("/api/users", json={"username": "admin", "password": "ValidPass!99"})
        assert resp.status_code == 409


class TestDeleteUser:
    def test_delete_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        admin_id = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()[
            "id"
        ]
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(f"/api/users/{admin_id}")
        assert resp.status_code == 401

    def test_delete_without_reauth_returns_403_reauth_required(
        self, app_factory, authed_client, conn
    ):
        """A valid session with no recent reauth ticket cannot delete users."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=False)
        other_id = _make_second_user(conn)
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "reauth_required"
        assert body["reauth_required"] is True
        row = conn.execute("SELECT id FROM admin_users WHERE id=?", (other_id,)).fetchone()
        assert row is not None

    def test_delete_with_expired_ticket_returns_403_reauth_required(
        self, app_factory, authed_client, conn, freezer
    ):
        """A ticket older than the reauth window must not authorise delete."""
        from datetime import timedelta

        from mediaman.web.auth.reauth import reauth_window_seconds

        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        other_id = _make_second_user(conn)
        freezer.tick(timedelta(seconds=reauth_window_seconds() + 5))
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 403
        assert resp.json()["reauth_required"] is True

    def test_delete_self_returns_400(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        admin_id = conn.execute("SELECT id FROM admin_users WHERE username='admin'").fetchone()[
            "id"
        ]
        resp = client.delete(f"/api/users/{admin_id}")
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_delete_other_user_happy_path(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        other_id = _make_second_user(conn)
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute("SELECT id FROM admin_users WHERE id=?", (other_id,)).fetchone()
        assert row is None

    def test_a_later_user_of_the_same_name_does_not_get_the_old_ones_requests(
        self, app_factory, authed_client, conn
    ):
        client = authed_client(_app(app_factory, conn), conn, with_reauth=True)
        other_id = _make_second_user(conn)
        name = conn.execute("SELECT username FROM admin_users WHERE id=?", (other_id,)).fetchone()
        insert_download_notification(conn, username=name["username"], tmdb_id=12)

        client.delete(f"/api/users/{other_id}")

        row = conn.execute("SELECT username FROM download_notifications").fetchone()
        assert row["username"] is None

    def test_successful_delete_writes_exactly_one_audit_row(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        other_id = _make_second_user(conn)
        resp = client.delete(f"/api/users/{other_id}")
        assert resp.status_code == 200
        rows = conn.execute(
            "SELECT action, actor FROM audit_log WHERE action = 'sec:user.deleted'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "admin"


class TestChangePassword:
    def test_change_password_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/api/users/change-password", json={"old_password": "x", "new_password": "y"}
        )
        assert resp.status_code == 401

    def test_change_password_happy_path(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert authenticate(conn, "admin", "NewStrongPass!99", client_ip="203.0.113.7") is True

    def test_change_password_wrong_old_returns_403(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "wrongpassword", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "wrong_password"

    def test_change_password_same_as_old_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "password1234"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "same_password"

    def test_change_password_weak_new_returns_400(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "password1234", "new_password": "abc"},
        )
        assert resp.status_code == 400
        assert "issues" in resp.json()

    def test_change_password_per_actor_throttle_independent_of_ip(
        self, app_factory, authed_client, conn
    ):
        """Per-actor throttle fires on 30+ wrong attempts from one user.

        The IP cap is the same shape, so this also exercises the AND
        composition — but here we verify the actor bucket trips first."""
        client = _client(app_factory, authed_client, conn)

        cap = _PASSWORD_CHANGE_LIMITER._max_in_window
        for _ in range(cap):
            client.post(
                "/api/users/change-password",
                json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
            )

        # Past the cap → 429.
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 429

    def test_change_password_per_ip_throttle_independent_of_actor(
        self, app_factory, authed_client, conn
    ):
        """A single attacker IP cycling through users still hits the IP cap.

        We can't easily change ``admin`` mid-test (the session is bound to
        it), so we simulate the per-IP exhaustion by clearing the per-actor
        bucket between attempts — leaving only the per-IP bucket as the
        gating limiter."""
        client = _client(app_factory, authed_client, conn)

        cap = _PASSWORD_CHANGE_IP_LIMITER._max_in_window
        for _ in range(cap):
            # Reset only the per-actor bucket — keep the per-IP bucket
            # accumulating so the IP cap is the one that trips.
            _PASSWORD_CHANGE_LIMITER.reset()
            client.post(
                "/api/users/change-password",
                json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
            )

        # Per-IP bucket is now exhausted; even with a fresh per-actor
        # bucket, the request must be 429.
        _PASSWORD_CHANGE_LIMITER.reset()
        resp = client.post(
            "/api/users/change-password",
            json={"old_password": "wrong", "new_password": "NewStrongPass!99"},
        )
        assert resp.status_code == 429


class TestListSessions:
    def test_sessions_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 401

    def test_sessions_returns_list(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 200
        assert "sessions" in resp.json()
        assert len(resp.json()["sessions"]) >= 1

    def test_sessions_response_does_not_leak_ip_or_fingerprint(
        self, app_factory, authed_client, conn
    ):
        """``issued_ip`` and ``fingerprint`` must NOT appear.

        The fingerprint exposes the SHA-256 prefix of UA + the /24 of the
        original IP, which is exactly what an attacker holding a stolen
        cookie would need to forge a matching fingerprint header.  The
        issued_ip would otherwise let any cookie thief dox the legitimate
        owner of the session.
        """
        client = _client(app_factory, authed_client, conn)
        resp = client.get("/api/users/sessions")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) >= 1
        for row in sessions:
            assert "issued_ip" not in row, "issued_ip leaks the user's IP — must be stripped"
            assert "fingerprint" not in row, (
                "fingerprint leaks the SHA-256/24-prefix material — must be stripped"
            )
        # Nothing but these may be added, least of all a token or its hash.
        assert set(sessions[0].keys()) == {
            "created_at",
            "expires_at",
            "last_used_at",
            "current",
            "device_label",
            "public_id",
        }

    def test_sessions_rate_limited_after_cap(self, app_factory, authed_client, conn):
        """Per-actor cap stops a cookie thief from
        polling this endpoint to detect the legitimate user signing in."""
        client = _client(app_factory, authed_client, conn)

        # Burn the burst cap directly via the limiter so we don't have to
        # fire 30 HTTP requests per test.
        for _ in range(_SESSIONS_LIST_LIMITER._max_in_window):
            _SESSIONS_LIST_LIMITER.check("admin")

        resp = client.get("/api/users/sessions")
        assert resp.status_code == 429
        assert resp.json()["ok"] is False


class TestRevokeOtherSessions:
    def test_revoke_others_requires_auth(self, app_factory, conn):
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/users/sessions/revoke-others")
        assert resp.status_code == 401

    def test_revoke_others_happy_path(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        # Create a second session for the same user
        create_session(conn, "admin")

        resp = client.post("/api/users/sessions/revoke-others")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # the caller's own session is replaced, not ended
        assert body["revoked"] == 1
        event = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:session.revoke_all'"
        ).fetchone()
        assert '"revoked": 1' in event["detail"] or '"revoked":1' in event["detail"]
        # A fresh session was issued; exactly one session now remains
        remaining = conn.execute(
            "SELECT COUNT(*) FROM admin_sessions WHERE username='admin'"
        ).fetchone()[0]
        assert remaining == 1


class TestRevokeOneSession:
    @staticmethod
    def _id(conn, token: str, username: str = "admin") -> str:
        return next(
            s["public_id"]
            for s in list_sessions_for(conn, username, current_token=token)
            if s["current"]
        )

    @staticmethod
    def _audit(conn) -> list[tuple[str, str]]:
        return [
            (r["actor"], r["detail"])
            for r in conn.execute(
                "SELECT actor, detail FROM audit_log WHERE action = 'sec:session.revoke_one'"
            )
        ]

    def test_requires_auth(self, app_factory, conn):
        client = TestClient(_app(app_factory, conn))
        assert client.delete("/api/users/sessions/abc").status_code == 401

    def test_ends_the_one_named_with_its_ticket_and_no_other(
        self, app_factory, authed_client, conn
    ):
        client = _client(app_factory, authed_client, conn)
        gone = create_session(conn, "admin", user_agent="Mozilla/5.0 (X11; Linux) Firefox/130.0")
        grant_recent_reauth(conn, gone, "admin")
        kept = create_session(conn, "admin")
        device = TestClient(
            client.app,
            headers={"user-agent": "Mozilla/5.0 (X11; Linux) Firefox/130.0"},
            cookies={"session_token": gone},
        )
        assert device.get("/api/users/sessions").status_code == 200

        resp = client.delete(f"/api/users/sessions/{self._id(conn, gone)}")

        assert (resp.status_code, resp.json()) == (200, {"ok": True})
        assert device.get("/api/users/sessions").status_code == 401
        assert not has_recent_reauth(conn, gone, "admin")
        assert validate_session(conn, kept) == "admin"
        assert client.get("/api/users/sessions").status_code == 200
        ((actor, detail),) = self._audit(conn)
        assert actor == "admin"
        assert "Firefox on Linux" in detail

    def test_needs_a_recent_password_and_counts_no_try_until_then(
        self, app_factory, authed_client, conn
    ):
        client = _client(app_factory, authed_client, conn, with_reauth=False)
        other = create_session(conn, "admin")
        path = f"/api/users/sessions/{self._id(conn, other)}"

        for _ in range(_SESSIONS_REVOKE_LIMITER._max_in_window + 1):
            resp = client.delete(path)

        assert (resp.status_code, resp.json()["reauth_required"]) == (403, True)
        assert validate_session(conn, other) == "admin"

    def test_never_ends_the_session_it_is_asked_from(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        mine = client.cookies["session_token"]

        resp = client.delete(f"/api/users/sessions/{self._id(conn, mine)}")

        assert (resp.status_code, resp.json()["error"]) == (400, "current_session")
        assert validate_session(conn, mine) == "admin"

    def test_one_already_gone_and_another_persons_answer_alike_and_leave_no_trace(
        self, app_factory, authed_client, conn
    ):
        client = _client(app_factory, authed_client, conn)
        _make_second_user(conn)
        theirs = create_session(conn, "other")
        mine = create_session(conn, "admin")
        path = f"/api/users/sessions/{self._id(conn, mine)}"
        client.delete(path)

        again = client.delete(path)
        foreign = client.delete(f"/api/users/sessions/{self._id(conn, theirs, 'other')}")
        unknown = client.delete("/api/users/sessions/" + "0" * 32)

        answers = {(r.status_code, r.text) for r in (again, foreign, unknown)}
        assert answers == {(200, '{"ok":true}')}
        assert validate_session(conn, theirs) == "other"
        assert len(self._audit(conn)) == 1

    def test_shares_the_limit_on_signing_devices_out(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        other = create_session(conn, "admin")
        while _SESSIONS_REVOKE_LIMITER.check("admin"):
            pass

        resp = client.delete(f"/api/users/sessions/{self._id(conn, other)}")

        assert resp.status_code == 429
        assert validate_session(conn, other) == "admin"

    def test_nothing_ends_when_it_cannot_be_recorded(self, app_factory, authed_client, conn):
        client = _client(app_factory, authed_client, conn)
        other = create_session(conn, "admin")
        grant_recent_reauth(conn, other, "admin")
        path = f"/api/users/sessions/{self._id(conn, other)}"
        conn.execute(
            "CREATE TRIGGER full BEFORE INSERT ON audit_log"
            " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )

        resp = client.delete(path)

        assert resp.status_code == 500
        assert validate_session(conn, other) == "admin"
        assert has_recent_reauth(conn, other, "admin")
