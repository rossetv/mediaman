"""Tests for persistent per-username login lockout."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db
from mediaman.web.auth.login_lockout import (
    admin_unlock,
    is_locked_out,
    record_failure,
    record_success,
)
from mediaman.web.auth.password_hash import authenticate, create_user


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class TestCounterMechanics:
    def test_fresh_user_not_locked(self, conn):
        assert is_locked_out(conn, "alice") is False

    def test_single_failure_no_lock(self, conn):
        record_failure(conn, "alice")
        assert is_locked_out(conn, "alice") is False

    def test_five_failures_lock_for_fifteen_minutes(self, conn, freezer):
        for _ in range(5):
            record_failure(conn, "alice")
        assert is_locked_out(conn, "alice") is True

        row = conn.execute(
            "SELECT locked_until, failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        locked_until = datetime.fromisoformat(row["locked_until"])
        delta = locked_until - datetime.now(UTC)
        # Clock is frozen so the delta is exact — no jitter slack needed.
        assert delta == timedelta(minutes=15)
        assert row["failure_count"] == 5

    def test_ten_failures_lock_for_one_hour(self, conn, freezer):
        for _ in range(10):
            record_failure(conn, "alice")
        assert is_locked_out(conn, "alice") is True

        row = conn.execute(
            "SELECT locked_until, failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        locked_until = datetime.fromisoformat(row["locked_until"])
        delta = locked_until - datetime.now(UTC)
        # Clock is frozen so the delta is exact.
        assert delta == timedelta(hours=1)
        assert row["failure_count"] == 10

    def test_success_clears_counter(self, conn):
        for _ in range(3):
            record_failure(conn, "alice")
        record_success(conn, "alice")

        assert is_locked_out(conn, "alice") is False
        row = conn.execute("SELECT * FROM login_failures WHERE username = ?", ("alice",)).fetchone()
        assert row is None


class TestDecay:
    def test_old_streak_resets_on_next_failure(self, conn, freezer):
        """A failure streak that started > 24 h ago and is not locked
        should reset to 1 on the next recorded failure."""
        for _ in range(4):
            record_failure(conn, "alice")

        # Back-date the counter as if it started 2 days ago, and clear
        # the lock so decay can apply.
        two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
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
        assert is_locked_out(conn, "alice") is False

    def test_locked_account_does_not_decay(self, conn, freezer):
        """Still-locked accounts should not have their counter wiped by
        the decay check — otherwise a patient attacker waits 24 h, then
        tries once to get the counter back to 1."""
        for _ in range(5):
            record_failure(conn, "alice")  # locked for 15 minutes

        # Back-date first_failure but leave locked_until alone.
        two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
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
    def test_lock_released_after_window(self, conn, freezer):
        for _ in range(5):
            record_failure(conn, "alice")
        # Artificially expire the lock by back-dating it.
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        conn.execute(
            "UPDATE login_failures SET locked_until = ? WHERE username = ?",
            (past, "alice"),
        )
        conn.commit()

        assert is_locked_out(conn, "alice") is False


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
        assert is_locked_out(conn, "alice") is False

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


class TestEscalatingLockoutWindows:
    """5-9 → 15 min, 10-14 → 1 h, 15+ → 24 h."""

    def test_fifteen_failures_lock_for_twenty_four_hours(self, conn, freezer):
        for _ in range(15):
            record_failure(conn, "alice")
        row = conn.execute(
            "SELECT locked_until, failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        locked_until = datetime.fromisoformat(row["locked_until"])
        delta = locked_until - datetime.now(UTC)
        # Clock is frozen so the delta is exact.
        assert delta == timedelta(hours=24)
        assert row["failure_count"] == 15

    def test_record_failure_returns_current_window_minutes(self, conn):
        # First four failures: sub-threshold → None.
        for _ in range(4):
            assert record_failure(conn, "alice") is None
        # Fifth failure crosses 5 → 15 min.
        assert record_failure(conn, "alice") == 15
        # Continue to 10 → 60 min.
        for _ in range(4):
            record_failure(conn, "alice")
        assert record_failure(conn, "alice") == 60
        # Continue to 15 → 1440 min.
        for _ in range(4):
            record_failure(conn, "alice")
        assert record_failure(conn, "alice") == 24 * 60


class TestAuthenticateKeepsCountingWhileLocked:
    """C6: the 5-failure wall must not freeze the counter — otherwise
    the 10/15 escalation windows are unreachable."""

    def test_counter_keeps_climbing_while_locked(self, conn, freezer):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        # Trip the 5-failure lock.
        for _ in range(5):
            authenticate(conn, "alice", "bad-password")

        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row["failure_count"] == 5

        # Attempt 5 more times while the account is locked. Previously
        # the authenticate() fast-path skipped record_failure() when
        # locked, so the counter sat at 5 forever and the 10-failure
        # escalation never fired.
        for _ in range(5):
            authenticate(conn, "alice", "bad-password")

        row = conn.execute(
            "SELECT failure_count, locked_until FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row["failure_count"] == 10
        # Lock now escalated to 1 h — clock is frozen so the delta is exact.
        locked_until = datetime.fromisoformat(row["locked_until"])
        delta = locked_until - datetime.now(UTC)
        assert delta == timedelta(hours=1)


class TestConcurrentRecordFailure:
    """C25: record_failure must be atomic under parallel writes.

    The previous read-modify-write lost increments around the 5/10
    boundary — two threads reading failure_count=4 both wrote back
    failure_count=5, so a 10-failure attack counted as 9 and the 1 h
    escalation never fired.
    """

    def test_parallel_failures_all_counted(self, tmp_path):
        import threading

        from mediaman.db import init_db

        db_file = tmp_path / "mm.db"
        init_db(str(db_file))  # create schema on the bootstrap conn

        # 20 threads, each recording one failure against the same user
        # through its own connection. All 20 must be counted.
        errors: list[BaseException] = []
        barrier = threading.Barrier(20)

        def worker() -> None:
            try:
                import sqlite3

                c = sqlite3.connect(str(db_file))
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA busy_timeout=30000")
                barrier.wait()
                record_failure(c, "alice")
                c.close()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"worker errors: {errors}"

        import sqlite3

        c = sqlite3.connect(str(db_file))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        c.close()
        assert row["failure_count"] == 20


class TestAuthenticateRecordFailuresFlag:
    """H9: change_password must not lock the user out of their own account."""

    def test_record_failures_false_does_not_lock(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        # 10 failed attempts with record_failures=False — should NOT lock.
        for _ in range(10):
            assert (
                authenticate(
                    conn,
                    "alice",
                    "bad",
                    record_failures=False,
                )
                is False
            )
        assert is_locked_out(conn, "alice") is False
        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row is None

    def test_record_failures_false_still_clears_counter_on_success(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        # Trip some failures with the normal path.
        for _ in range(3):
            authenticate(conn, "alice", "bad")
        # A correct password via the trusted path must still clear them.
        assert (
            authenticate(
                conn,
                "alice",
                "correct-password-123",
                record_failures=False,
            )
            is True
        )
        row = conn.execute(
            "SELECT * FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row is None


class TestNoExtendWhileLocked:
    """M21: while an account is locked, sub-threshold failures must not
    slide ``locked_until`` forwards.

    Otherwise an unauthenticated attacker keeps the legitimate user
    locked out indefinitely by pinging the login endpoint forever.
    """

    def test_locked_window_not_extended_by_same_threshold_failure(self, conn, freezer):
        """Attempts 6, 7, 8, 9 (still in the 5-9 band) must not refresh
        the 15-minute lock window."""
        # Trip the 5-failure / 15-minute lock.
        for _ in range(5):
            record_failure(conn, "alice")
        original_until = conn.execute(
            "SELECT locked_until FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()["locked_until"]

        # Pound the endpoint 4 more times — count goes 6→7→8→9 but the
        # window remains in the same severity band, so the locked_until
        # stamp must not move forwards.
        for _ in range(4):
            record_failure(conn, "alice")
        new_until = conn.execute(
            "SELECT locked_until, failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert new_until["failure_count"] == 9
        assert new_until["locked_until"] == original_until

    def test_locked_account_still_blocks_correct_password(self, conn):
        """Existing semantics preserved — a locked account refuses even
        the correct password (M21 fix only stops the WINDOW being
        extended, not the lock being effective)."""
        create_user(conn, "alice", "correct-password-99", enforce_policy=False)
        for _ in range(5):
            authenticate(conn, "alice", "wrong")
        assert authenticate(conn, "alice", "correct-password-99") is False

    def test_escalation_still_extends_window(self, conn, freezer):
        """When the 10th and 15th failures cross to a STRICTER window,
        ``locked_until`` MUST be promoted — otherwise the escalation is
        unreachable."""
        for _ in range(5):
            record_failure(conn, "alice")
        fifteen_min_until = datetime.fromisoformat(
            conn.execute(
                "SELECT locked_until FROM login_failures WHERE username = ?",
                ("alice",),
            ).fetchone()["locked_until"]
        )

        # Cross the 10-failure threshold → 1 h window must replace the
        # 15-minute one.
        for _ in range(5):
            record_failure(conn, "alice")
        sixty_min_until = datetime.fromisoformat(
            conn.execute(
                "SELECT locked_until FROM login_failures WHERE username = ?",
                ("alice",),
            ).fetchone()["locked_until"]
        )
        assert sixty_min_until > fifteen_min_until


class TestAdminUnlock:
    """The admin-unlock helper clears the failure counter and the lock."""

    def test_unlock_clears_an_existing_lock(self, conn):
        for _ in range(5):
            record_failure(conn, "alice")
        assert is_locked_out(conn, "alice") is True

        cleared = admin_unlock(conn, "alice")
        conn.commit()  # admin_unlock leaves commit to caller for tx control
        assert cleared is True
        assert is_locked_out(conn, "alice") is False
        row = conn.execute(
            "SELECT * FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row is None

    def test_unlock_returns_false_when_no_record(self, conn):
        cleared = admin_unlock(conn, "alice")
        conn.commit()
        assert cleared is False

    def test_unlock_normalises_case(self, conn):
        """admin_unlock matches lockout's lowercase normalisation."""
        for _ in range(5):
            record_failure(conn, "Alice")  # stored as "alice"

        cleared = admin_unlock(conn, "ALICE")
        conn.commit()
        assert cleared is True

    def test_unlock_does_not_commit(self, conn):
        """Caller owns the transaction so unlock + audit land atomically."""
        for _ in range(5):
            record_failure(conn, "alice")
        admin_unlock(conn, "alice")
        # Roll back — the unlock must not have been auto-committed.
        conn.rollback()
        # The lock should still be in place because admin_unlock didn't commit.
        assert is_locked_out(conn, "alice") is True
