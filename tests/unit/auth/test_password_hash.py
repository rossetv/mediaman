"""Tests for mediaman.auth.password_hash.

Covers: create_user, authenticate, change_password, list_users, delete_user,
user_must_change_password, set_must_change_password.
"""

import pytest

from mediaman.auth.password_hash import (
    authenticate,
    change_password,
    create_user,
    delete_user,
    list_users,
    set_must_change_password,
    user_must_change_password,
)
from mediaman.db import init_db


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


class TestCreateUser:
    def test_creates_user_record(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        row = conn.execute("SELECT username FROM admin_users WHERE username='alice'").fetchone()
        assert row["username"] == "alice"

    def test_password_is_bcrypt_hashed(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE username='alice'"
        ).fetchone()
        # Must not store the plaintext and must be a bcrypt hash.
        assert row["password_hash"] != "pass"
        assert row["password_hash"].startswith("$2b$")

    def test_duplicate_username_raises_value_error(self, conn):
        create_user(conn, "alice", "pass1", enforce_policy=False)
        with pytest.raises(ValueError, match="already exists"):
            create_user(conn, "alice", "pass2", enforce_policy=False)

    def test_must_change_password_defaults_to_false(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        row = conn.execute(
            "SELECT must_change_password FROM admin_users WHERE username='alice'"
        ).fetchone()
        assert row["must_change_password"] == 0


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


class TestAuthenticate:
    def test_correct_credentials_return_true(self, conn):
        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        assert authenticate(conn, "alice", "correct-pass") is True

    def test_wrong_password_returns_false(self, conn):
        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        assert authenticate(conn, "alice", "wrong-pass") is False

    def test_nonexistent_user_returns_false(self, conn):
        # Must also not raise — constant-time dummy bcrypt check fires.
        assert authenticate(conn, "ghost", "any-pass") is False

    def test_empty_username_returns_false(self, conn):
        assert authenticate(conn, "", "pass") is False


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


class TestChangePassword:
    def test_change_with_correct_old_password(self, conn):
        create_user(conn, "alice", "old-pass-1", enforce_policy=False)
        ok = change_password(conn, "alice", "old-pass-1", "new-pass-2", enforce_policy=False)
        assert ok is True
        # New password must work; old must not.
        assert authenticate(conn, "alice", "new-pass-2") is True
        assert authenticate(conn, "alice", "old-pass-1") is False

    def test_change_with_wrong_old_password_returns_false(self, conn):
        create_user(conn, "alice", "correct-old", enforce_policy=False)
        ok = change_password(conn, "alice", "incorrect-old", "new-pass", enforce_policy=False)
        assert ok is False
        # Original password still works.
        assert authenticate(conn, "alice", "correct-old") is True

    def test_change_revokes_existing_sessions(self, conn):
        from mediaman.auth.session_store import create_session, validate_session

        create_user(conn, "alice", "old-pass", enforce_policy=False)
        token = create_session(conn, "alice")
        assert validate_session(conn, token) == "alice"

        change_password(conn, "alice", "old-pass", "new-pass", enforce_policy=False)

        assert validate_session(conn, token) is None

    def test_change_clears_must_change_password_flag(self, conn):
        create_user(conn, "alice", "old-pass", enforce_policy=False)
        set_must_change_password(conn, "alice", True)
        assert user_must_change_password(conn, "alice") is True

        change_password(conn, "alice", "old-pass", "new-pass", enforce_policy=False)

        assert user_must_change_password(conn, "alice") is False


# ---------------------------------------------------------------------------
# user_must_change_password / set_must_change_password
# ---------------------------------------------------------------------------


class TestMustChangePassword:
    def test_flag_starts_false(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        assert user_must_change_password(conn, "alice") is False

    def test_set_true_and_read_back(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        set_must_change_password(conn, "alice", True)
        assert user_must_change_password(conn, "alice") is True

    def test_set_false_clears_flag(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        set_must_change_password(conn, "alice", True)
        set_must_change_password(conn, "alice", False)
        assert user_must_change_password(conn, "alice") is False

    def test_nonexistent_user_returns_false(self, conn):
        assert user_must_change_password(conn, "nobody") is False


# ---------------------------------------------------------------------------
# list_users
# ---------------------------------------------------------------------------


class TestListUsers:
    def test_returns_all_users_in_order(self, conn):
        create_user(conn, "alice", "p1", enforce_policy=False)
        create_user(conn, "bob", "p2", enforce_policy=False)
        users = list_users(conn)
        names = [u["username"] for u in users]
        assert names == ["alice", "bob"]

    def test_does_not_expose_password_hash(self, conn):
        create_user(conn, "alice", "secret", enforce_policy=False)
        users = list_users(conn)
        assert "password_hash" not in users[0]

    def test_empty_when_no_users(self, conn):
        # Fresh DB may have users from migrations; delete them.
        conn.execute("DELETE FROM admin_users")
        conn.commit()
        assert list_users(conn) == []


# ---------------------------------------------------------------------------
# delete_user
# ---------------------------------------------------------------------------


class TestDeleteUser:
    def _uid(self, conn, username: str) -> int:
        return conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()[
            "id"
        ]

    def test_deletes_other_user(self, conn):
        create_user(conn, "alice", "p1", enforce_policy=False)
        create_user(conn, "bob", "p2", enforce_policy=False)
        ok = delete_user(conn, self._uid(conn, "bob"), current_username="alice")
        assert ok is True
        assert [u["username"] for u in list_users(conn)] == ["alice"]

    def test_refuses_self_delete(self, conn):
        create_user(conn, "alice", "p1", enforce_policy=False)
        create_user(conn, "bob", "p2", enforce_policy=False)
        ok = delete_user(conn, self._uid(conn, "alice"), current_username="alice")
        assert ok is False

    def test_refuses_to_delete_last_admin(self, conn):
        # Ensure there is only one user.
        conn.execute("DELETE FROM admin_users")
        conn.commit()
        create_user(conn, "solo", "pass", enforce_policy=False)
        ok = delete_user(conn, self._uid(conn, "solo"), current_username="other")
        assert ok is False
        assert len(list_users(conn)) == 1

    def test_unknown_id_returns_false(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        assert delete_user(conn, 99999, current_username="alice") is False
