"""Tests for persistent per-username login lockout."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mediaman.auth import login_lockout
from mediaman.auth.login_lockout import (
    check_lockout,
    record_failure,
    record_success,
)
from mediaman.auth.session import authenticate, create_user
from mediaman.db import init_db


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class TestCounterMechanics:
    def test_fresh_user_not_locked(self, conn):
        assert check_lockout(conn, "alice") is False

    def test_single_failure_no_lock(self, conn):
        record_failure(conn, "alice")
        assert check_lockout(conn, "alice") is False

    def test_five_failures_lock_for_fifteen_minutes(self, conn):
        for _ in range(5):
            record_failure(conn, "alice")
        assert check_lockout(conn, "alice") is True

        row = conn.execute(
            "SELECT locked_until, failure_count FROM login_failures "
            "WHERE username = ?",
            ("alice",),
        ).fetchone()
        locked_until = datetime.fromisoformat(row["locked_until"])
        delta = locked_until - datetime.now(timezone.utc)
        # Allow a second of slack for clock jitter.
        assert timedelta(minutes=14) < delta <= timedelta(minutes=15, seconds=5)
        assert row["failure_count"] == 5

    def test_ten_failures_lock_for_one_hour(self, conn):
        for _ in range(10):
            record_failure(conn, "alice")
        assert check_lockout(conn, "alice") is True

        row = conn.execute(
            "SELECT locked_until, failure_count FROM login_failures "
            "WHERE username = ?",
            ("alice",),
        ).fetchone()
        locked_until = datetime.fromisoformat(row["locked_until"])
        delta = locked_until - datetime.now(timezone.utc)
        assert timedelta(minutes=59) < delta <= timedelta(minutes=61)
        assert row["failure_count"] == 10

    def test_success_clears_counter(self, conn):
        for _ in range(3):
            record_failure(conn, "alice")
        record_success(conn, "alice")

        assert check_lockout(conn, "alice") is False
        row = conn.execute(
            "SELECT * FROM login_failures WHERE username = ?", ("alice",)
        ).fetchone()
        assert row is None


class TestDecay:
    def test_old_streak_resets_on_next_failure(self, conn):
        """A failure streak that started > 24 h ago and is not locked
        should reset to 1 on the next recorded failure."""
        for _ in range(4):
            record_failure(conn, "alice")

        # Back-date the counter as if it started 2 days ago, and clear
        # the lock so decay can apply.
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.execute(
            "UPDATE login_failures SET first_failure_at = ?, locked_until = NULL "
            "WHERE username = ?",
            (two_days_ago, "alice"),
        )
        conn.commit()

        record_failure(conn, "alice")
        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        # 4 → decayed to 0 → +1 = 1.
        assert row["failure_count"] == 1
        assert check_lockout(conn, "alice") is False

    def test_locked_account_does_not_decay(self, conn):
        """Still-locked accounts should not have their counter wiped by
        the decay check — otherwise a patient attacker waits 24 h, then
        tries once to get the counter back to 1."""
        for _ in range(5):
            record_failure(conn, "alice")  # locked for 15 minutes

        # Back-date first_failure but leave locked_until alone.
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.execute(
            "UPDATE login_failures SET first_failure_at = ? WHERE username = ?",
            (two_days_ago, "alice"),
        )
        conn.commit()

        record_failure(conn, "alice")
        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        # Counter must keep climbing — no decay while locked.
        assert row["failure_count"] == 6


class TestLockExpiry:
    def test_lock_released_after_window(self, conn):
        for _ in range(5):
            record_failure(conn, "alice")
        # Artificially expire the lock.
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn.execute(
            "UPDATE login_failures SET locked_until = ? WHERE username = ?",
            (past, "alice"),
        )
        conn.commit()

        assert check_lockout(conn, "alice") is False


class TestAuthenticateIntegration:
    def test_lockout_blocks_even_correct_password(self, conn):
        """Once locked, even the right password must not authenticate —
        otherwise the lockout is pointless."""
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)

        # Trip the threshold with wrong passwords.
        for _ in range(5):
            assert authenticate(conn, "alice", "wrong-password") is False

        # Now the correct password must also be rejected while locked.
        assert authenticate(conn, "alice", "correct-password-123") is False

    def test_successful_login_resets_counter(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)

        for _ in range(3):
            authenticate(conn, "alice", "wrong-password")

        assert authenticate(conn, "alice", "correct-password-123") is True

        # Counter wiped — one more wrong guess should not lock anyone.
        authenticate(conn, "alice", "wrong-password")
        assert check_lockout(conn, "alice") is False

    def test_lockout_does_not_leak_to_caller(self, conn):
        """`authenticate` must return False for locked accounts — same
        value it returns for wrong-password. Revealing lock state would
        let an attacker enumerate valid usernames."""
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)

        for _ in range(5):
            authenticate(conn, "alice", "wrong-password")

        # Locked → False
        locked_result = authenticate(conn, "alice", "correct-password-123")
        # Nonexistent → False (nothing to lock)
        unknown_result = authenticate(conn, "does-not-exist", "anything")

        assert locked_result is False
        assert unknown_result is False
        # Both are the same bool shape — nothing leaks.
        assert type(locked_result) is type(unknown_result)

    def test_failures_against_unknown_user_still_tracked(self, conn):
        """Prevents username-enumeration via counter presence. We record
        against the claimed name, even if it doesn't exist."""
        for _ in range(3):
            authenticate(conn, "ghost", "whatever")

        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("ghost",),
        ).fetchone()
        assert row is not None
        assert row["failure_count"] == 3
