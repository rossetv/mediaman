"""Tests for the reauth module — recent-reauth tickets and verify path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.reauth import (
    REAUTH_LOCKOUT_PREFIX,
    cleanup_expired_reauth,
    grant_recent_reauth,
    has_recent_reauth,
    reauth_window_seconds,
    revoke_all_reauth_for,
    revoke_reauth,
    revoke_reauth_by_hash,
    verify_reauth_password,
)
from mediaman.web.auth.session_store import _hash_token


@pytest.fixture
def conn(db_path):
    c = init_db(str(db_path))
    create_user(c, "alice", "correct-password-99", enforce_policy=False)
    return c


# ---------------------------------------------------------------------------
# reauth_window_seconds
# ---------------------------------------------------------------------------


class TestReauthWindow:
    def test_default_is_five_minutes(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_REAUTH_WINDOW_SECONDS", raising=False)
        assert reauth_window_seconds() == 300

    def test_env_override_within_bounds(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_REAUTH_WINDOW_SECONDS", "120")
        assert reauth_window_seconds() == 120

    def test_too_low_clamps_up(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_REAUTH_WINDOW_SECONDS", "5")
        assert reauth_window_seconds() == 30

    def test_too_high_clamps_down(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_REAUTH_WINDOW_SECONDS", "100000")
        assert reauth_window_seconds() == 3600

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_REAUTH_WINDOW_SECONDS", "not-a-number")
        assert reauth_window_seconds() == 300


# ---------------------------------------------------------------------------
# grant / has / revoke
# ---------------------------------------------------------------------------


class TestGrantHasRevoke:
    def test_no_token_returns_false(self, conn):
        assert has_recent_reauth(conn, None, "alice") is False
        assert has_recent_reauth(conn, "", "alice") is False

    def test_grant_then_has(self, conn):
        token = "a" * 64
        grant_recent_reauth(conn, token, "alice")
        assert has_recent_reauth(conn, token, "alice") is True

    def test_has_for_wrong_username_false(self, conn):
        """A reauth granted to alice does not satisfy a check for bob."""
        token = "a" * 64
        grant_recent_reauth(conn, token, "alice")
        assert has_recent_reauth(conn, token, "bob") is False

    def test_revoke_clears(self, conn):
        token = "a" * 64
        grant_recent_reauth(conn, token, "alice")
        revoke_reauth(conn, token)
        assert has_recent_reauth(conn, token, "alice") is False

    def test_grant_uses_token_hash_not_token(self, conn):
        """The DB stores the SHA-256 hash, never the plaintext token."""
        token = "a" * 64
        grant_recent_reauth(conn, token, "alice")
        rows = conn.execute("SELECT session_token_hash FROM reauth_tickets").fetchall()
        assert len(rows) == 1
        assert rows[0]["session_token_hash"] == _hash_token(token)
        # And critically — the plaintext is nowhere in the table.
        assert rows[0]["session_token_hash"] != token

    def test_re_grant_extends_window(self, conn, monkeypatch):
        """Re-granting before expiry slides the window forward."""
        from datetime import UTC, datetime, timedelta

        from mediaman.web.auth import reauth as _reauth

        tick = [datetime(2000, 1, 1, tzinfo=UTC)]

        def _fake_now():
            return tick[0]

        monkeypatch.setattr(_reauth, "_now", _fake_now)

        token = "a" * 64
        grant_recent_reauth(conn, token, "alice", window_seconds=60)
        first = conn.execute("SELECT expires_at FROM reauth_tickets").fetchone()["expires_at"]
        tick[0] = tick[0] + timedelta(seconds=1)  # advance the fake clock by 1 s
        grant_recent_reauth(conn, token, "alice", window_seconds=300)
        second = conn.execute("SELECT expires_at FROM reauth_tickets").fetchone()["expires_at"]
        assert second > first

    def test_expired_ticket_not_recent(self, conn):
        token = "a" * 64
        grant_recent_reauth(conn, token, "alice", window_seconds=60)
        # Back-date both timestamps beyond the window.
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        conn.execute(
            "UPDATE reauth_tickets SET granted_at = ?, expires_at = ? WHERE session_token_hash = ?",
            (old, old, _hash_token(token)),
        )
        conn.commit()
        assert has_recent_reauth(conn, token, "alice") is False

    def test_per_call_max_age_clamps_below_stored_window(self, conn):
        """Caller can demand a stricter window than the stored expiry."""
        token = "a" * 64
        grant_recent_reauth(conn, token, "alice", window_seconds=300)
        # Force the granted_at to be 200 s ago — within the stored 300 s
        # window but above any stricter caller-supplied limit.
        old = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        future = (datetime.now(UTC) + timedelta(seconds=100)).isoformat()
        conn.execute(
            "UPDATE reauth_tickets SET granted_at = ?, expires_at = ? WHERE session_token_hash = ?",
            (old, future, _hash_token(token)),
        )
        conn.commit()
        # Default (300 s) — passes.
        assert has_recent_reauth(conn, token, "alice") is True
        # Stricter window — fails.
        assert has_recent_reauth(conn, token, "alice", max_age_seconds=60) is False

    def test_revoke_all_for_user(self, conn):
        """revoke_all_reauth_for drops every ticket for the named user."""
        for i in range(3):
            grant_recent_reauth(conn, f"{i}" * 64, "alice")
        # And one for bob — should be untouched.
        create_user(conn, "bob", "bob-pass-1234", enforce_policy=False)
        grant_recent_reauth(conn, "f" * 64, "bob")

        deleted = revoke_all_reauth_for(conn, "alice")
        assert deleted == 3
        # Bob's ticket survives.
        bob_count = conn.execute(
            "SELECT COUNT(*) AS n FROM reauth_tickets WHERE username = 'bob'"
        ).fetchone()["n"]
        assert bob_count == 1


# ---------------------------------------------------------------------------
# H-4: revoke_reauth_by_hash and cleanup_expired_reauth
# ---------------------------------------------------------------------------


class TestRevokeReauthByHash:
    """H-4: session-destruction sites only have a token hash on hand."""

    def test_revoke_by_hash_deletes_matching_ticket(self, conn):
        token = "ses-tok-h4-1"
        grant_recent_reauth(conn, token, "alice")
        assert has_recent_reauth(conn, token, "alice") is True

        revoke_reauth_by_hash(conn, _hash_token(token))
        assert has_recent_reauth(conn, token, "alice") is False

    def test_revoke_by_hash_is_idempotent(self, conn):
        revoke_reauth_by_hash(conn, _hash_token("never-existed"))
        revoke_reauth_by_hash(conn, _hash_token("never-existed"))
        # Should not raise.

    def test_revoke_by_hash_does_not_touch_other_tickets(self, conn):
        grant_recent_reauth(conn, "tok-keep", "alice")
        grant_recent_reauth(conn, "tok-drop", "alice")

        revoke_reauth_by_hash(conn, _hash_token("tok-drop"))

        assert has_recent_reauth(conn, "tok-keep", "alice") is True
        assert has_recent_reauth(conn, "tok-drop", "alice") is False

    def test_revoke_by_hash_with_empty_hash_is_a_noop(self, conn):
        grant_recent_reauth(conn, "tok-keep", "alice")
        revoke_reauth_by_hash(conn, "")
        assert has_recent_reauth(conn, "tok-keep", "alice") is True


class TestCleanupExpiredReauth:
    """H-4: a periodic sweep stops dead tickets piling up."""

    def test_sweeps_only_past_expiry(self, conn):
        future = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
        past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        now_iso = datetime.now(UTC).isoformat()

        # Two tickets, one fresh, one expired.
        conn.execute(
            "INSERT INTO reauth_tickets (session_token_hash, username, granted_at, expires_at) "
            "VALUES (?, 'alice', ?, ?)",
            ("hash-fresh", now_iso, future),
        )
        conn.execute(
            "INSERT INTO reauth_tickets (session_token_hash, username, granted_at, expires_at) "
            "VALUES (?, 'alice', ?, ?)",
            ("hash-expired", now_iso, past),
        )
        conn.commit()

        deleted = cleanup_expired_reauth(conn)
        assert deleted == 1

        remaining = {
            row["session_token_hash"]
            for row in conn.execute("SELECT session_token_hash FROM reauth_tickets")
        }
        assert remaining == {"hash-fresh"}


# ---------------------------------------------------------------------------
# verify_reauth_password — feeds the reauth-namespace lockout
# ---------------------------------------------------------------------------


class TestVerifyReauthPassword:
    def test_correct_password_returns_true(self, conn):
        assert verify_reauth_password(conn, "alice", "correct-password-99") is True

    def test_wrong_password_returns_false(self, conn):
        assert verify_reauth_password(conn, "alice", "wrong") is False

    def test_failure_records_in_reauth_namespace_only(self, conn):
        """Failed reauth attempts must not bump the plain-login counter.

        Otherwise an attacker with a session cookie could lock the
        legitimate user out of the login flow without ever knowing
        the password.
        """
        for _ in range(3):
            verify_reauth_password(conn, "alice", "wrong")

        # Plain-login counter is untouched.
        plain = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert plain is None

        # Reauth-namespace counter is at 3.
        ns_row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            (f"{REAUTH_LOCKOUT_PREFIX}alice",),
        ).fetchone()
        assert ns_row["failure_count"] == 3

    def test_five_wrong_attempts_lock_reauth_namespace(self, conn):
        """5+ failed reauth attempts trip the standard lockout window."""
        for _ in range(5):
            assert verify_reauth_password(conn, "alice", "wrong") is False

        # The reauth namespace is now locked. Even the correct password
        # is refused while the lock is active.
        assert verify_reauth_password(conn, "alice", "correct-password-99") is False

    def test_success_clears_namespace_counter(self, conn):
        for _ in range(3):
            verify_reauth_password(conn, "alice", "wrong")
        assert verify_reauth_password(conn, "alice", "correct-password-99") is True
        ns_row = conn.execute(
            "SELECT * FROM login_failures WHERE username = ?",
            (f"{REAUTH_LOCKOUT_PREFIX}alice",),
        ).fetchone()
        assert ns_row is None
