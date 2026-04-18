"""Tests for session management."""

from datetime import datetime, timedelta, timezone

import pytest

from mediaman.db import init_db
from mediaman.auth.session import (
    authenticate,
    create_session,
    create_user,
    delete_user,
    destroy_session,
    list_users,
    validate_session,
)


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class TestCreateUser:
    def test_creates_user(self, conn):
        create_user(conn, "admin", "password123", enforce_policy=False)
        row = conn.execute(
            "SELECT username FROM admin_users WHERE username=?", ("admin",)
        ).fetchone()
        assert row["username"] == "admin"

    def test_password_is_hashed(self, conn):
        create_user(conn, "admin", "password123", enforce_policy=False)
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE username=?", ("admin",)
        ).fetchone()
        assert row["password_hash"] != "password123"
        assert row["password_hash"].startswith("$2b$")

    def test_duplicate_username_raises(self, conn):
        create_user(conn, "admin", "pass1", enforce_policy=False)
        with pytest.raises(ValueError, match="already exists"):
            create_user(conn, "admin", "pass2", enforce_policy=False)


class TestAuthenticate:
    def test_valid_credentials(self, conn):
        create_user(conn, "admin", "correct-password", enforce_policy=False)
        assert authenticate(conn, "admin", "correct-password") is True

    def test_wrong_password(self, conn):
        create_user(conn, "admin", "correct-password", enforce_policy=False)
        assert authenticate(conn, "admin", "wrong-password") is False

    def test_nonexistent_user(self, conn):
        assert authenticate(conn, "nobody", "password") is False


class TestSessions:
    def test_create_and_validate(self, conn):
        create_user(conn, "admin", "pass", enforce_policy=False)
        token = create_session(conn, "admin")
        assert len(token) == 64
        username = validate_session(conn, token)
        assert username == "admin"

    def test_expired_session_rejected(self, conn):
        create_user(conn, "admin", "pass", enforce_policy=False)
        token = create_session(conn, "admin", ttl_seconds=-1)
        assert validate_session(conn, token) is None

    def test_destroy_session(self, conn):
        create_user(conn, "admin", "pass", enforce_policy=False)
        token = create_session(conn, "admin")
        destroy_session(conn, token)
        assert validate_session(conn, token) is None


class TestDeleteUser:
    def _user_id(self, conn, username: str) -> int:
        row = conn.execute(
            "SELECT id FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        return int(row["id"])

    def test_deletes_existing_user(self, conn):
        create_user(conn, "alice", "pass1", enforce_policy=False)
        create_user(conn, "bob", "pass2", enforce_policy=False)
        ok = delete_user(conn, self._user_id(conn, "bob"), current_username="alice")
        assert ok is True
        assert [u["username"] for u in list_users(conn)] == ["alice"]

    def test_refuses_self_delete(self, conn):
        create_user(conn, "alice", "pass1", enforce_policy=False)
        create_user(conn, "bob", "pass2", enforce_policy=False)
        ok = delete_user(conn, self._user_id(conn, "alice"), current_username="alice")
        assert ok is False
        # Both users still present.
        assert {u["username"] for u in list_users(conn)} == {"alice", "bob"}

    def test_refuses_last_admin(self, conn):
        create_user(conn, "solo", "pass", enforce_policy=False)
        ok = delete_user(conn, self._user_id(conn, "solo"), current_username="other")
        assert ok is False
        # Solo still exists — we must never zero out the admin table.
        assert [u["username"] for u in list_users(conn)] == ["solo"]

    def test_unknown_user_returns_false(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        ok = delete_user(conn, 9999, current_username="alice")
        assert ok is False

    def test_atomic_last_admin_guard(self, conn):
        """The "last admin" check happens inside the DELETE statement so a
        racing call cannot bypass it by observing a stale count."""
        create_user(conn, "alice", "pass1", enforce_policy=False)
        create_user(conn, "bob", "pass2", enforce_policy=False)

        alice_id = self._user_id(conn, "alice")
        bob_id = self._user_id(conn, "bob")

        # Simulate the race: before caller A's DELETE runs, a concurrent
        # process (caller B) deletes the other admin.  Here we open a
        # second DB connection and delete bob out of band, then fire
        # caller A's delete_user on alice. The guard's subquery must see
        # the table has only one admin left and refuse.
        import sqlite3
        conn2 = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()["file"])
        conn2.row_factory = sqlite3.Row
        conn2.execute("DELETE FROM admin_users WHERE id=?", (bob_id,))
        conn2.commit()
        conn2.close()

        ok = delete_user(conn, alice_id, current_username="other")
        assert ok is False
        # Alice still there — the guard held.
        assert [u["username"] for u in list_users(conn)] == ["alice"]

    def test_deletes_user_sessions(self, conn):
        """Deleting a user also destroys any of their active sessions."""
        create_user(conn, "alice", "pass", enforce_policy=False)
        create_user(conn, "bob", "pass", enforce_policy=False)
        token = create_session(conn, "bob")
        assert validate_session(conn, token) == "bob"

        ok = delete_user(conn, self._user_id(conn, "bob"), current_username="alice")
        assert ok is True
        assert validate_session(conn, token) is None
