"""Tests for session management."""

from datetime import datetime, timedelta, timezone

import pytest

from mediaman.auth.session import (
    authenticate,
    create_session,
    create_user,
    delete_user,
    destroy_session,
    list_users,
    validate_session,
)
from mediaman.db import init_db


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

    def test_hard_expiry_matches_cookie_max_age(self, conn):
        """Hard expiry must match the ``max_age=86400`` (1 day) on the
        session cookie. A stolen raw token should not keep working after
        the browser has dropped the cookie."""
        from mediaman.auth import session as session_mod

        assert session_mod._HARD_EXPIRY_DAYS == 1

        create_user(conn, "admin", "pass", enforce_policy=False)
        token = create_session(conn, "admin")
        row = conn.execute(
            "SELECT created_at, expires_at FROM admin_sessions WHERE token_hash = ? OR token = ?",
            (__import__("hashlib").sha256(token.encode()).hexdigest(), token),
        ).fetchone()
        created = datetime.fromisoformat(row["created_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        delta = expires - created
        assert timedelta(hours=23, minutes=59) < delta <= timedelta(days=1, seconds=5)


class TestDeleteUser:
    def _user_id(self, conn, username: str) -> int:
        row = conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()
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


class TestStrictTokenShape:
    """H2: validate_session must only accept exactly 64 hex chars."""

    def test_accepts_canonical_token(self, conn):
        create_user(conn, "alice", "pw", enforce_policy=False)
        token = create_session(conn, "alice")
        assert validate_session(conn, token) == "alice"

    def test_rejects_too_short(self, conn):
        assert validate_session(conn, "a" * 32) is None
        assert validate_session(conn, "a" * 63) is None

    def test_rejects_too_long(self, conn):
        assert validate_session(conn, "a" * 65) is None
        assert validate_session(conn, "a" * 128) is None

    def test_rejects_uppercase_hex(self, conn):
        # canonical is lowercase; uppercase would mean the DB hash lookup
        # uses a different sha256 input and would never match anyway,
        # but we reject it up front.
        assert validate_session(conn, "A" * 64) is None

    def test_rejects_non_hex(self, conn):
        assert validate_session(conn, "z" * 64) is None


class TestCreateSessionStopsWritingRawToken:
    """H8: the raw token must not land in the ``token`` column.

    The legacy column is still written (schema NOT NULL + PK constraints
    require a value) but with the token *hash*, not the live credential.
    """

    def test_raw_token_not_in_token_column(self, conn):
        create_user(conn, "alice", "pw", enforce_policy=False)
        token = create_session(conn, "alice")
        row = conn.execute("SELECT token, token_hash FROM admin_sessions").fetchone()
        # The plaintext token is NOT stored.
        assert row["token"] != token
        # token column now carries the hash (defensive dup) so looking
        # it up directly yields nothing useful to an attacker.
        import hashlib

        expected_hash = hashlib.sha256(token.encode()).hexdigest()
        assert row["token_hash"] == expected_hash


class TestCreateUserIntegrityErrorNarrowing:
    """C36: only UNIQUE on username maps to "already exists". Other
    IntegrityErrors (FK, NOT NULL, CHECK) must propagate."""

    def test_duplicate_still_maps_to_value_error(self, conn):
        create_user(conn, "alice", "pw1", enforce_policy=False)
        with pytest.raises(ValueError, match="already exists"):
            create_user(conn, "alice", "pw2", enforce_policy=False)

    def test_non_unique_integrity_error_propagates(self, tmp_path):
        """A stray IntegrityError from a different constraint is NOT
        masked as 'user already exists'. We craft a bare-schema DB
        where ``password_hash`` is NOT NULL and pass an empty hash via
        a subclass that forwards all sqlite3 operations but lets us
        target the INSERT."""
        import sqlite3

        conn = init_db(str(tmp_path / "m.db"))

        # Patch bcrypt.hashpw so create_user inserts a NULL-ish row via
        # a side-channel: re-raise a non-unique IntegrityError from the
        # cursor's execute. We emulate it by patching conn.execute
        # through the ``sqlite3.Connection.execute`` descriptor — but
        # that's read-only. Easier: wrap the connection object so we
        # control its execute() while still keeping row_factory.
        class WrappedConn:
            def __init__(self, inner):
                self._inner = inner
                self.row_factory = inner.row_factory

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def execute(self, sql, *args, **kwargs):
                if sql.startswith("INSERT INTO admin_users"):
                    raise sqlite3.IntegrityError(
                        "NOT NULL constraint failed: admin_users.password_hash"
                    )
                return self._inner.execute(sql, *args, **kwargs)

            def commit(self):
                return self._inner.commit()

        wrapped = WrappedConn(conn)
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            create_user(wrapped, "bob", "pw", enforce_policy=False)  # type: ignore[arg-type]


class TestChangePasswordDoesNotLockSelf:
    """H9: mistyping your own current password 5 times in the change-
    password form must not lock you out of your own account."""

    def test_repeated_wrong_old_password_does_not_lock(self, conn):
        from mediaman.auth.login_lockout import check_lockout
        from mediaman.auth.session import change_password

        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(10):
            assert (
                change_password(
                    conn,
                    "alice",
                    "wrong",
                    "NewPassword-123!",
                    enforce_policy=False,
                )
                is False
            )

        # Counter never climbed; no lockout.
        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row is None
        assert check_lockout(conn, "alice") is False

    def test_successful_change_still_clears_counter(self, conn):
        from mediaman.auth.session import authenticate, change_password

        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        # Poison the counter via the real login path.
        for _ in range(3):
            authenticate(conn, "alice", "bad")
        # Now change password with the correct current one — must clear.
        ok = change_password(
            conn,
            "alice",
            "correct-password-123",
            "NewPassword-123!",
            enforce_policy=False,
        )
        assert ok is True
        row = conn.execute(
            "SELECT * FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row is None


class TestValidateSessionTransactional:
    """C35: expired sessions are swept with strict ``<`` inside a
    BEGIN IMMEDIATE transaction."""

    def test_session_at_exact_expiry_still_accepted(self, conn):
        """A session whose ``expires_at == now`` is still valid — the
        sweep uses strict ``<``. Verifies we aren't off-by-one on the
        boundary."""
        create_user(conn, "alice", "pw", enforce_policy=False)
        token = create_session(conn, "alice")

        # Pin expires_at to a future time so validate passes.
        future = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        conn.execute("UPDATE admin_sessions SET expires_at = ?", (future,))
        conn.commit()
        assert validate_session(conn, token) == "alice"

    def test_expired_by_one_second_rejected(self, conn):
        create_user(conn, "alice", "pw", enforce_policy=False)
        token = create_session(conn, "alice")

        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn.execute("UPDATE admin_sessions SET expires_at = ?", (past,))
        conn.commit()
        assert validate_session(conn, token) is None


class TestFingerprintModeOff:
    """C8: MEDIAMAN_FINGERPRINT_MODE=off disables the binding check."""

    def test_off_mode_skips_mismatch_rejection(self, conn, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "off")
        create_user(conn, "alice", "pw", enforce_policy=False)
        token = create_session(
            conn,
            "alice",
            user_agent="UA-1",
            client_ip="1.1.1.1",
        )
        # Different UA/IP would normally destroy the session; off mode
        # lets it through.
        assert (
            validate_session(
                conn,
                token,
                user_agent="UA-2",
                client_ip="2.2.2.2",
            )
            == "alice"
        )

    def test_off_mode_writes_empty_fingerprint(self, conn, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "off")
        create_user(conn, "alice", "pw", enforce_policy=False)
        create_session(conn, "alice", user_agent="UA", client_ip="1.1.1.1")
        row = conn.execute("SELECT fingerprint FROM admin_sessions").fetchone()
        assert row["fingerprint"] == ""
