"""Tests for ``PATCH /api/users/me/email``.

The endpoint lets the current authenticated admin set or clear their
notification email. It is gated by the same has_recent_reauth ticket
already used by POST /api/users and POST /api/users/{id}/unlock — a
stolen session cookie alone cannot redirect download alerts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.password_hash import get_user_email, set_user_email
from mediaman.web.routes.users import (
    _PASSWORD_CHANGE_IP_LIMITER,
    _PASSWORD_CHANGE_LIMITER,
    _REAUTH_LIMITER,
    _SESSIONS_LIST_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router
from tests.helpers.factories import insert_admin_user


def _app(app_factory, conn):
    return app_factory(users_router, conn=conn)


def _ticketed_client(app_factory, authed_client, conn):
    """Return a TestClient whose session has a fresh reauth ticket."""
    app = _app(app_factory, conn)
    return authed_client(app, conn, with_reauth=True)


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    for lim in (
        _USER_MGMT_LIMITER,
        _USER_CREATE_LIMITER,
        _REAUTH_LIMITER,
        _PASSWORD_CHANGE_LIMITER,
        _PASSWORD_CHANGE_IP_LIMITER,
        _SESSIONS_LIST_LIMITER,
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
    ):
        lim.reset()


class TestPatchOwnEmail:
    def test_happy_path_sets_email(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        resp = client.patch("/api/users/me/email", json={"email": "ops@example.com"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert get_user_email(conn, "admin") == "ops@example.com"

    def test_empty_email_clears_email(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        set_user_email(conn, "admin", "ops@example.com")
        resp = client.patch("/api/users/me/email", json={"email": ""})
        assert resp.status_code == 200
        assert get_user_email(conn, "admin") is None

    def test_invalid_email_returns_400_and_keeps_existing(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        set_user_email(conn, "admin", "ops@example.com")
        resp = client.patch("/api/users/me/email", json={"email": "rossetv"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_email"
        assert get_user_email(conn, "admin") == "ops@example.com"

    def test_without_reauth_returns_403_reauth_required(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=False)
        resp = client.patch("/api/users/me/email", json={"email": "ops@example.com"})
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "reauth_required"
        assert body["reauth_required"] is True
        assert get_user_email(conn, "admin") is None

    def test_with_expired_ticket_returns_403_reauth_required(
        self, app_factory, authed_client, conn, freezer
    ):
        from datetime import timedelta

        from mediaman.web.auth.reauth import reauth_window_seconds

        app = _app(app_factory, conn)
        client = authed_client(app, conn, with_reauth=True)
        freezer.tick(timedelta(seconds=reauth_window_seconds() + 5))
        resp = client.patch("/api/users/me/email", json={"email": "ops@example.com"})
        assert resp.status_code == 403
        assert resp.json()["reauth_required"] is True
        assert get_user_email(conn, "admin") is None

    def test_unauthenticated_returns_401(self, app_factory, conn):
        from mediaman.web.auth.password_hash import create_user

        create_user(conn, "admin", "password1234", enforce_policy=False)
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch("/api/users/me/email", json={"email": "ops@example.com"})
        assert resp.status_code == 401
        assert get_user_email(conn, "admin") is None

    def test_patch_email_writes_audit_row_on_success(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        resp = client.patch("/api/users/me/email", json={"email": "ops@example.com"})
        assert resp.status_code == 200
        row = conn.execute(
            "SELECT action, actor, detail FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row is not None
        assert row["actor"] == "admin"
        assert '"cleared":false' in row["detail"]

    def test_patch_clear_email_writes_audit_with_cleared_true(
        self, app_factory, authed_client, conn
    ):
        client = _ticketed_client(app_factory, authed_client, conn)
        set_user_email(conn, "admin", "ops@example.com")
        resp = client.patch("/api/users/me/email", json={"email": ""})
        assert resp.status_code == 200
        row = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row is not None
        assert '"cleared":true' in row["detail"]

    def test_patch_email_writes_audit_row_on_rate_limit(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        for _ in range(_USER_MGMT_LIMITER._max_in_window):
            _USER_MGMT_LIMITER.check("admin")
        resp = client.patch("/api/users/me/email", json={"email": "ops@example.com"})
        assert resp.status_code == 429
        row = conn.execute(
            "SELECT action, actor FROM audit_log "
            "WHERE action = 'sec:user.email_update.rate_limited'"
        ).fetchone()
        assert row is not None
        assert row["actor"] == "admin"

    def test_update_email_body_rejects_extra_fields(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        resp = client.patch(
            "/api/users/me/email",
            json={"email": "ops@example.com", "stowaway": "x"},
        )
        assert resp.status_code == 422

    def test_update_email_body_rejects_overlong(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        long_email = "a" * 321 + "@example.com"
        resp = client.patch("/api/users/me/email", json={"email": long_email})
        assert resp.status_code == 422


class TestAnAddressOfSomeoneElse:
    @pytest.fixture
    def viewer(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        conn.execute("UPDATE admin_users SET role = 'viewer' WHERE username = 'admin'")
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, role, created_at, email)"
            " VALUES ('casey', 'unused', 'admin', '2026', 'casey@example.com')"
        )
        conn.execute(
            "INSERT INTO subscribers (email, active, created_at)"
            " VALUES ('reader@example.com', 1, '2026'), ('left@example.com', 0, '2026')"
        )
        conn.commit()
        return client

    def _set(self, client, email):
        return client.patch("/api/users/me/email", json={"email": email})

    def test_another_users_and_a_subscribers_are_refused_alike(self, viewer, conn):
        user = self._set(viewer, " Casey@Example.com")
        reader = self._set(viewer, "READER@example.com")

        assert user.status_code == reader.status_code == 400
        assert user.json() == reader.json()
        assert "casey" not in user.text.lower()
        assert get_user_email(conn, "admin") is None

    def test_an_address_that_left_the_newsletter_is_free(self, viewer, conn):
        assert self._set(viewer, "Left@example.com").status_code == 200
        assert get_user_email(conn, "admin") == "Left@example.com"

    def test_an_admin_may_take_their_own_newsletter_address_but_no_other_users(self, viewer, conn):
        conn.execute("UPDATE admin_users SET role = 'admin' WHERE username = 'admin'")
        conn.commit()

        assert self._set(viewer, "reader@example.com").status_code == 200
        assert self._set(viewer, "casey@example.com").status_code == 400
        assert get_user_email(conn, "admin") == "reader@example.com"

    def test_an_address_already_held_can_be_saved_again_and_given_up(self, viewer, conn):
        conn.execute("UPDATE admin_users SET email = 'casey@example.com' WHERE username = 'admin'")
        conn.commit()

        assert self._set(viewer, "Casey@example.com").status_code == 200
        assert self._set(viewer, "").status_code == 200
        assert self._set(viewer, "casey@example.com").status_code == 400

    def test_the_record_says_which_address_was_set_and_which_it_replaced(self, viewer, conn):
        self._set(viewer, "one@example.com")
        self._set(viewer, "two@example.com")

        details = [
            row["detail"]
            for row in conn.execute(
                "SELECT detail FROM audit_log WHERE action = 'sec:user.email_updated' ORDER BY id"
            )
        ]
        assert "one@example.com" in details[0]
        assert "one@example.com" in details[1] and "two@example.com" in details[1]


class TestAnAdminSetsSomeoneElses:
    @pytest.fixture
    def client(self, app_factory, authed_client, conn):
        client = _ticketed_client(app_factory, authed_client, conn)
        conn.execute(
            "INSERT INTO subscribers (email, active, created_at)"
            " VALUES ('reader@example.com', 1, '2026')"
        )
        conn.commit()
        return client

    @pytest.fixture
    def vic(self, client, conn):
        return insert_admin_user(conn, username="vic", role="viewer")

    def _set(self, client, user_id, email):
        return client.patch(f"/api/users/{user_id}/email", json={"email": email})

    def test_a_newsletter_readers_address_can_be_given_and_taken_back(self, client, vic, conn):
        assert self._set(client, vic, " reader@example.com ").status_code == 200
        assert get_user_email(conn, "vic") == "reader@example.com"
        assert get_user_email(conn, "admin") is None

        assert self._set(client, vic, "").status_code == 200
        assert get_user_email(conn, "vic") is None

    def test_an_address_another_user_holds_is_never_given(self, client, vic, conn):
        set_user_email(conn, "admin", "boss@example.com")

        resp = self._set(client, vic, "BOSS@example.com")

        assert (resp.status_code, resp.json()["error"]) == (409, "email_taken")
        assert get_user_email(conn, "vic") is None

    def test_an_address_that_is_none_is_refused_and_the_old_one_stays(self, client, vic, conn):
        set_user_email(conn, "vic", "vic@example.com")

        assert self._set(client, vic, "vic").json()["error"] == "invalid_email"
        assert get_user_email(conn, "vic") == "vic@example.com"

    def test_the_record_says_whose_it_is_who_changed_it_and_from_what(self, client, vic, conn):
        set_user_email(conn, "vic", "old@example.com")
        self._set(client, vic, "new@example.com")

        row = conn.execute(
            "SELECT actor, detail FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row["actor"] == "admin"
        for part in ('"vic"', "old@example.com", "new@example.com"):
            assert part in row["detail"]

    def test_it_needs_a_recent_password(self, app_factory, authed_client, conn):
        client = authed_client(_app(app_factory, conn), conn)
        vic = insert_admin_user(conn, username="vic", role="viewer")

        resp = self._set(client, vic, "vic@example.com")

        assert (resp.status_code, resp.json()["reauth_required"]) == (403, True)
        assert get_user_email(conn, "vic") is None

    def test_a_viewer_is_told_the_same_whatever_the_user_or_address(self, client, vic, conn):
        conn.execute("UPDATE admin_users SET role = 'viewer' WHERE username = 'admin'")
        conn.commit()

        asked = [(vic, "reader@example.com"), (vic, "free@example.com"), (999, "free@example.com")]
        answers = [self._set(client, *each) for each in asked]

        assert {a.status_code for a in answers} == {403}
        assert len({a.text for a in answers}) == 1
        assert get_user_email(conn, "vic") is None

    def test_signed_out_gets_nothing(self, client, vic):
        assert self._set(TestClient(client.app), vic, "a@example.com").status_code == 401

    def test_nobody_by_that_id_and_your_own_are_refused(self, client, conn):
        own = conn.execute("SELECT id FROM admin_users WHERE username = 'admin'").fetchone()["id"]

        assert self._set(client, 999, "a@example.com").status_code == 404
        assert self._set(client, own, "a@example.com").status_code == 400
        assert get_user_email(conn, "admin") is None

    def test_too_many_changes_are_refused_and_recorded(self, client, vic, conn):
        while _USER_MGMT_LIMITER.check("admin"):
            pass

        assert self._set(client, vic, "a@example.com").status_code == 429
        assert get_user_email(conn, "vic") is None
        assert conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'sec:user.email_update.rate_limited'"
        ).fetchone()
