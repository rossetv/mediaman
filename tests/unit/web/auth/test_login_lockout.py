from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db
from mediaman.web.auth import login_lockout
from mediaman.web.auth.login_lockout import (
    admin_unlock,
    admin_unlock_with_audit,
    clear_unlocked_login_failures,
    is_locked_out,
    login_ip_key,
    login_key,
    reauth_key,
    record_failure,
    record_success,
)
from mediaman.web.auth.password_hash import authenticate, change_password, create_user
from mediaman.web.auth.reauth import verify_reauth_password


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

    def test_corrupt_locked_until_is_treated_as_still_locked(self, conn):
        """A NON-EMPTY but unparseable ``locked_until``
        fails CLOSED — the account is treated as STILL LOCKED.

        An attacker who can corrupt the stored value must not be able to
        escape the lock by garbling it.
        """
        for _ in range(5):
            record_failure(conn, "alice")
        assert is_locked_out(conn, "alice") is True

        conn.execute(
            "UPDATE login_failures SET locked_until = ? WHERE username = ?",
            ("not-a-timestamp", "alice"),
        )
        conn.commit()
        assert is_locked_out(conn, "alice") is True

    def test_null_locked_until_is_not_locked(self, conn):
        """A NULL ``locked_until`` (counted failures but no active lock) is
        the legitimate not-locked state — it must NOT be swept up by the
        fail-closed branch, which only fires on a non-empty corrupt
        value.
        """
        record_failure(conn, "alice")  # counts 1, locked_until stays NULL
        row = conn.execute(
            "SELECT locked_until FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row["locked_until"] is None
        assert is_locked_out(conn, "alice") is False


class TestAuthenticateIntegration:
    def test_lockout_blocks_even_correct_password(self, conn):
        """Once locked, even the right password must not authenticate —
        otherwise the lockout is pointless."""
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)

        # Trip the threshold with wrong passwords.
        for _ in range(5):
            assert authenticate(conn, "alice", "wrong-password", client_ip="203.0.113.7") is False

        # Now the correct password must also be rejected while locked.
        assert authenticate(conn, "alice", "correct-password-123", client_ip="203.0.113.7") is False

    def test_locked_account_attempt_still_bumps_counter(self, conn):
        """The locked short-circuit must bump the counter so the
        escalation bands (5 → 10 → 15) stay reachable."""
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)

        # Lock the account at the 15-minute band (count == 5).
        for _ in range(5):
            authenticate(conn, "alice", "wrong-password", client_ip="203.0.113.7")
        assert is_locked_out(conn, login_ip_key("alice", "203.0.113.7")) is True

        def _count() -> int:
            return conn.execute(
                "SELECT failure_count FROM login_failures WHERE username = ?",
                (login_ip_key("alice", "203.0.113.7"),),
            ).fetchone()["failure_count"]

        before = _count()
        # An attempt against the LOCKED account must still bump the counter.
        assert authenticate(conn, "alice", "wrong-password", client_ip="203.0.113.7") is False
        assert _count() == before + 1

    def test_successful_login_resets_counter(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)

        for _ in range(3):
            authenticate(conn, "alice", "wrong-password", client_ip="203.0.113.7")

        assert authenticate(conn, "alice", "correct-password-123", client_ip="203.0.113.7") is True

        # Counter wiped — one more wrong guess should not lock anyone.
        authenticate(conn, "alice", "wrong-password", client_ip="203.0.113.7")
        assert is_locked_out(conn, login_ip_key("alice", "203.0.113.7")) is False

    def test_lockout_does_not_leak_to_caller(self, conn):
        """`authenticate` must return False for locked accounts — same
        value it returns for wrong-password. Revealing lock state would
        let an attacker enumerate valid usernames."""
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)

        for _ in range(5):
            authenticate(conn, "alice", "wrong-password", client_ip="203.0.113.7")

        # Locked → False
        locked_result = authenticate(conn, "alice", "correct-password-123", client_ip="203.0.113.7")
        # Nonexistent → False (nothing to lock)
        unknown_result = authenticate(conn, "does-not-exist", "anything", client_ip="203.0.113.7")

        assert locked_result is False
        assert unknown_result is False
        # Both are the same bool shape — nothing leaks.
        assert type(locked_result) is type(unknown_result)

    def test_failures_against_unknown_user_still_tracked(self, conn):
        """Prevents username-enumeration via counter presence. We record
        against the claimed name, even if it doesn't exist."""
        for _ in range(3):
            authenticate(conn, "ghost", "whatever", client_ip="203.0.113.7")

        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            (login_ip_key("ghost", "203.0.113.7"),),
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
    """The 5-failure wall must not freeze the counter — otherwise
    the 10/15 escalation windows are unreachable."""

    def test_counter_keeps_climbing_while_locked(self, conn, freezer):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        # Trip the 5-failure lock.
        for _ in range(5):
            authenticate(conn, "alice", "bad-password", client_ip="203.0.113.7")

        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            (login_ip_key("alice", "203.0.113.7"),),
        ).fetchone()
        assert row["failure_count"] == 5

        # Attempt 5 more times while the account is locked. Previously
        # the authenticate() fast-path skipped record_failure() when
        # locked, so the counter sat at 5 forever and the 10-failure
        # escalation never fired.
        for _ in range(5):
            authenticate(conn, "alice", "bad-password", client_ip="203.0.113.7")

        row = conn.execute(
            "SELECT failure_count, locked_until FROM login_failures WHERE username = ?",
            (login_ip_key("alice", "203.0.113.7"),),
        ).fetchone()
        assert row["failure_count"] == 10
        # Lock now escalated to 1 h — clock is frozen so the delta is exact.
        locked_until = datetime.fromisoformat(row["locked_until"])
        delta = locked_until - datetime.now(UTC)
        assert delta == timedelta(hours=1)


class TestPerClientIp:
    """Failures count per username and client IP."""

    def test_failures_from_one_ip_do_not_lock_the_user_out_from_another(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(15):
            authenticate(conn, "alice", "wrong-password", client_ip="198.51.100.4")

        assert authenticate(conn, "alice", "correct-password-123", client_ip="203.0.113.7") is True

    def test_a_login_from_another_ip_does_not_lift_the_lock(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(5):
            authenticate(conn, "alice", "wrong-password", client_ip="198.51.100.4")
        assert authenticate(conn, "alice", "correct-password-123", client_ip="203.0.113.7") is True

        assert (
            authenticate(conn, "alice", "correct-password-123", client_ip="198.51.100.4") is False
        )

    def test_ipv6_addresses_in_one_64_share_a_counter(self):
        same = login_ip_key("alice", "2001:db8:1:2::1")
        assert login_ip_key("alice", "2001:db8:1:2:ffff:ffff:ffff:ffff") == same
        assert login_ip_key("alice", "2001:db8:1:3::1") != same

    def test_ipv4_addresses_do_not_share_a_counter(self):
        assert login_ip_key("alice", "192.0.2.1") != login_ip_key("alice", "192.0.2.2")

    def test_admin_unlock_clears_every_ip_and_only_that_user(self, conn):
        for _ in range(5):
            record_failure(conn, login_ip_key("alice", "198.51.100.4"))
            record_failure(conn, login_ip_key("alice", "2001:db8::1"))
        record_failure(conn, "alice")
        record_failure(conn, login_ip_key("al_ce", "198.51.100.4"))
        record_failure(conn, login_ip_key("al%", "198.51.100.4"))
        record_failure(conn, login_ip_key("x\x1falice", "198.51.100.4"))

        assert admin_unlock(conn, "alice") is True
        conn.commit()

        left = {r["username"] for r in conn.execute("SELECT username FROM login_failures")}
        assert left == {
            login_ip_key("al_ce", "198.51.100.4"),
            login_ip_key("al%", "198.51.100.4"),
            login_ip_key("x\x1falice", "198.51.100.4"),
        }

    def test_a_username_only_lock_refuses_login_from_any_ip(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(5):
            record_failure(conn, "alice")

        for ip in ("198.51.100.4", "203.0.113.7"):
            assert authenticate(conn, "alice", "correct-password-123", client_ip=ip) is False

    def test_a_failed_attempt_does_not_extend_a_username_only_lock(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(5):
            record_failure(conn, "alice")
        before = tuple(
            conn.execute(
                "SELECT failure_count, locked_until FROM login_failures WHERE username = ?",
                ("alice",),
            ).fetchone()
        )

        authenticate(conn, "alice", "wrong-password", client_ip="203.0.113.7")

        after = tuple(
            conn.execute(
                "SELECT failure_count, locked_until FROM login_failures WHERE username = ?",
                ("alice",),
            ).fetchone()
        )
        assert after == before

    def test_admin_unlock_lifts_a_username_only_lock(self, conn):
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(5):
            record_failure(conn, "alice")

        admin_unlock(conn, "alice")
        conn.commit()

        assert authenticate(conn, "alice", "correct-password-123", client_ip="203.0.113.7") is True

    def test_a_lock_from_any_ip_is_audited_as_locked(self, conn):
        for _ in range(5):
            record_failure(conn, login_ip_key("alice", "198.51.100.4"))

        result = admin_unlock_with_audit(conn, "alice", audit_actor="admin", target_id=1)

        assert result.was_locked is True
        assert not is_locked_out(conn, login_ip_key("alice", "198.51.100.4"))

    def test_a_proved_password_clears_unlocked_counters_from_every_ip_and_keeps_locks(self, conn):
        record_failure(conn, login_ip_key("alice", "203.0.113.7"))
        for _ in range(5):
            record_failure(conn, login_ip_key("alice", "198.51.100.4"))

        clear_unlocked_login_failures(conn, "alice")

        left = [r["username"] for r in conn.execute("SELECT username FROM login_failures")]
        assert left == [login_ip_key("alice", "198.51.100.4")]


class TestConcurrentRecordFailure:
    """Record_failure must be atomic under parallel writes.

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

                # Raw connect is intentional: each worker needs its own independent
                # connection to exercise real concurrent WAL writes.
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

        # Raw connect is intentional: a separate read connection to verify the
        # final count after all worker threads have committed.
        c = sqlite3.connect(str(db_file))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        row = c.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        c.close()
        assert row["failure_count"] == 20


class TestNoExtendWhileLocked:
    """While an account is locked, sub-threshold failures must not
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
        the correct password (only stops the WINDOW being
        extended, not the lock being effective)."""
        create_user(conn, "alice", "correct-password-99", enforce_policy=False)
        for _ in range(5):
            authenticate(conn, "alice", "wrong", client_ip="203.0.113.7")
        assert authenticate(conn, "alice", "correct-password-99", client_ip="203.0.113.7") is False

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

    def test_unlock_is_case_sensitive(self, conn):
        """The lockout key is the verbatim username — NOT lowercased —
        so it stays aligned with the case-sensitive ``admin_users.username``
        lookup. ``admin_unlock`` therefore only clears the EXACT-case key.

        A different-case unlock attempt is a no-op (there is no row under
        that key), and the original-case record is left untouched.
        """
        for _ in range(5):
            record_failure(conn, "Alice")  # stored verbatim as "Alice"

        # Wrong case clears nothing — the keys are distinct identities.
        cleared_wrong_case = admin_unlock(conn, "ALICE")
        conn.commit()
        assert cleared_wrong_case is False
        assert is_locked_out(conn, "Alice") is True

        # Exact case clears the real record.
        cleared = admin_unlock(conn, "Alice")
        conn.commit()
        assert cleared is True
        assert is_locked_out(conn, "Alice") is False

    def test_unlock_does_not_commit(self, conn):
        """Caller owns the transaction so unlock + audit land atomically."""
        for _ in range(5):
            record_failure(conn, "alice")
        admin_unlock(conn, "alice")
        # Roll back — the unlock must not have been auto-committed.
        conn.rollback()
        # The lock should still be in place because admin_unlock didn't commit.
        assert is_locked_out(conn, "alice") is True


def _seed_login_row(conn, username, *, count, locked_until):
    conn.execute(
        "INSERT INTO login_failures (username, failure_count, locked_until) VALUES (?, ?, ?)",
        (login_key(username), count, locked_until),
    )
    conn.commit()


def _login_row(conn, username):
    return conn.execute(
        "SELECT failure_count, locked_until FROM login_failures WHERE username = ?",
        (login_key(username),),
    ).fetchone()


class TestLoginCounterAfterProvedPassword:
    """A password proved by re-auth or password change clears typos, never a lock."""

    @pytest.fixture
    def user_conn(self, conn):
        create_user(conn, "alice", "correct-password-99", enforce_policy=False)
        return conn

    @staticmethod
    def _prove(kind, conn):
        if kind == "reauth":
            assert verify_reauth_password(conn, "alice", "correct-password-99") is True
        else:
            assert change_password(
                conn, "alice", "correct-password-99", "another-password-77", enforce_policy=False
            )

    @pytest.mark.parametrize("kind", ["reauth", "change_password"])
    def test_success_clears_unlocked_failures(self, user_conn, kind):
        _seed_login_row(user_conn, "alice", count=3, locked_until=None)
        self._prove(kind, user_conn)
        assert _login_row(user_conn, "alice") is None

    @pytest.mark.parametrize("kind", ["reauth", "change_password"])
    def test_success_keeps_an_active_lock_and_its_count(self, user_conn, kind):
        until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        _seed_login_row(user_conn, "alice", count=5, locked_until=until)
        self._prove(kind, user_conn)
        row = _login_row(user_conn, "alice")
        assert (row["failure_count"], row["locked_until"]) == (5, until)

    def test_expired_lock_is_cleared(self, user_conn):
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        _seed_login_row(user_conn, "alice", count=5, locked_until=past)
        clear_unlocked_login_failures(user_conn, "alice")
        assert _login_row(user_conn, "alice") is None

    def test_corrupt_lock_value_stays_locked(self, user_conn):
        _seed_login_row(user_conn, "alice", count=5, locked_until="garbage")
        clear_unlocked_login_failures(user_conn, "alice")
        assert _login_row(user_conn, "alice") is not None

    @pytest.mark.parametrize("kind", ["reauth", "change_password"])
    @pytest.mark.parametrize("value", ["1", "2460000", "now"])
    def test_success_keeps_a_lock_value_that_is_not_a_timestamp(self, user_conn, kind, value):
        _seed_login_row(user_conn, "alice", count=5, locked_until=value)
        self._prove(kind, user_conn)
        row = _login_row(user_conn, "alice")
        assert (row["failure_count"], row["locked_until"]) == (5, value)

    def test_no_other_writer_gets_in_between_the_check_and_the_clear(
        self, user_conn, db_path, monkeypatch
    ):
        _seed_login_row(user_conn, "alice", count=3, locked_until=None)
        other = sqlite3.connect(str(db_path), timeout=0)
        outcome = []
        real_check = login_lockout.is_locked_out

        def check_then_try_a_write(conn, username):
            result = real_check(conn, username)
            try:
                other.execute("UPDATE login_failures SET failure_count = 9")
                outcome.append("written")
            except sqlite3.OperationalError:
                outcome.append("blocked")
            return result

        monkeypatch.setattr(login_lockout, "is_locked_out", check_then_try_a_write)
        clear_unlocked_login_failures(user_conn, "alice")
        other.close()
        assert outcome == ["blocked"]
        assert _login_row(user_conn, "alice") is None

    def test_wrong_passwords_write_no_login_row(self, user_conn):
        assert verify_reauth_password(user_conn, "alice", "wrong") is False
        assert not change_password(
            user_conn, "alice", "wrong", "another-password-77", enforce_policy=False
        )
        assert _login_row(user_conn, "alice") is None


class TestPruneStaleFailures:
    @staticmethod
    def _row(conn, username, *, first_ago, locked_in=None, locked_raw=None):
        now = datetime.now(UTC)
        locked = locked_raw
        if locked_in is not None:
            locked = (now + locked_in).isoformat()
        conn.execute(
            "INSERT INTO login_failures (username, failure_count, first_failure_at, locked_until)"
            " VALUES (?, 3, ?, ?)",
            (username, (now - first_ago).isoformat(), locked),
        )
        conn.commit()

    @staticmethod
    def _names(conn):
        return {r["username"] for r in conn.execute("SELECT username FROM login_failures")}

    def test_prunes_old_unlocked_rows_and_keeps_the_rest(self, conn):
        old = timedelta(hours=25)
        self._row(conn, "unknown-user", first_ago=old)
        self._row(conn, "lock-ended", first_ago=old, locked_in=-timedelta(hours=1))
        self._row(conn, "recent", first_ago=timedelta(hours=2))
        self._row(conn, "locked", first_ago=old, locked_in=timedelta(hours=1))
        self._row(conn, "corrupt-lock", first_ago=old, locked_raw="garbage")

        deleted = login_lockout.prune_stale_failures(conn)

        assert deleted == 2
        assert self._names(conn) == {"recent", "locked", "corrupt-lock"}

    def test_a_pruned_username_starts_counting_from_one(self, conn):
        self._row(conn, "alice", first_ago=timedelta(hours=30))
        login_lockout.prune_stale_failures(conn)
        record_failure(conn, "alice")
        row = conn.execute("SELECT failure_count FROM login_failures").fetchone()
        assert row["failure_count"] == 1

    def test_the_session_sweep_prunes_them(self, conn):
        from mediaman.web.auth import session_store

        self._row(conn, "unknown-user", first_ago=timedelta(hours=25))
        self._row(conn, "locked", first_ago=timedelta(hours=25), locked_in=timedelta(hours=1))

        session_store._writes._cleanup_expired_with_commit(conn, datetime.now(UTC).isoformat())

        assert self._names(conn) == {"locked"}

    def test_a_streak_just_under_a_day_old_is_kept_and_one_just_over_is_pruned(self, conn):
        self._row(conn, "just-under", first_ago=timedelta(hours=23, minutes=59))
        self._row(conn, "just-over", first_ago=timedelta(hours=24, minutes=1))

        deleted = login_lockout.prune_stale_failures(conn)

        assert deleted == 1
        assert self._names(conn) == {"just-under"}


class TestPruneOnRecordFailure:
    @pytest.fixture(autouse=True)
    def _fresh_throttle(self, monkeypatch):
        monkeypatch.setattr(login_lockout, "_last_prune_at", float("-inf"))

    @staticmethod
    def _old_row(conn, username, *, locked_in=None):
        now = datetime.now(UTC)
        locked = (now + locked_in).isoformat() if locked_in is not None else None
        conn.execute(
            "INSERT INTO login_failures (username, failure_count, first_failure_at, locked_until)"
            " VALUES (?, 3, ?, ?)",
            (username, (now - timedelta(hours=25)).isoformat(), locked),
        )
        conn.commit()

    @staticmethod
    def _names(conn):
        return {r["username"] for r in conn.execute("SELECT username FROM login_failures")}

    def test_recording_a_failure_prunes_a_stale_row_but_not_a_lock(self, conn):
        self._old_row(conn, "stale")
        self._old_row(conn, "locked", locked_in=timedelta(hours=1))

        record_failure(conn, "someone")

        assert self._names(conn) == {"locked", "someone"}

    def test_the_prune_runs_at_most_once_a_minute(self, conn, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(login_lockout.time, "monotonic", lambda: clock[0])
        self._old_row(conn, "first-stale")
        record_failure(conn, "a")
        self._old_row(conn, "second-stale")

        record_failure(conn, "b")
        assert "second-stale" in self._names(conn)

        clock[0] += 61
        record_failure(conn, "c")
        assert "second-stale" not in self._names(conn)

    def test_a_failing_prune_does_not_break_recording_the_failure(self, conn, monkeypatch):
        def boom(_conn):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(login_lockout, "prune_stale_failures", boom)

        for _ in range(5):
            minutes = record_failure(conn, "alice")

        assert minutes == 15
        assert is_locked_out(conn, "alice") is True


class TestAdminUnlockWithAudit:
    @staticmethod
    def _audit_had_lock(conn):
        row = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:user.unlocked'"
        ).fetchone()
        return '"had_lock":true' in row["detail"]

    def test_failures_without_a_lock_are_audited_as_not_locked(self, conn):
        record_failure(conn, "alice")

        result = admin_unlock_with_audit(conn, "alice", audit_actor="admin", target_id=1)

        assert self._audit_had_lock(conn) is False
        assert result.cleared is True
        assert result.was_locked is False

    def test_a_login_lock_is_audited_as_locked(self, conn):
        for _ in range(5):
            record_failure(conn, "alice")

        result = admin_unlock_with_audit(conn, "alice", audit_actor="admin", target_id=1)

        assert result.was_locked is True
        assert self._audit_had_lock(conn) is True

    def test_a_reauth_lock_alone_is_audited_as_locked(self, conn):
        for _ in range(5):
            record_failure(conn, reauth_key("alice"))

        result = admin_unlock_with_audit(conn, "alice", audit_actor="admin", target_id=1)

        assert result.was_locked is True
        assert self._audit_had_lock(conn) is True


class TestLogInjection:
    HOSTILE = "bob\r\nWARNING forged auth.line"

    def test_locked_branch_cannot_start_a_new_log_line(self, conn, caplog):
        for _ in range(5):
            authenticate(conn, self.HOSTILE, "x", client_ip="203.0.113.5")
        caplog.clear()
        with caplog.at_level("WARNING"):
            authenticate(conn, self.HOSTILE, "x", client_ip="203.0.113.5")
        text = caplog.text
        assert "auth.account_locked" in text
        assert "\r" not in caplog.records[0].getMessage()
        assert "\n" not in caplog.records[0].getMessage()

    def test_per_ip_lock_logs_bucket_and_username_separately(self, conn, caplog):
        with caplog.at_level("WARNING"):
            for _ in range(5):
                record_failure(conn, login_ip_key("nobody", "203.0.113.5"))
        msg = next(
            r.getMessage() for r in caplog.records if "auth.account_locked" in r.getMessage()
        )
        assert "user=nobody" in msg
        assert "ip=203.0.113.5" in msg

    def test_ipv6_buckets_stay_distinct_in_the_log(self, conn, caplog):
        with caplog.at_level("WARNING"):
            for ip in ("2001:db8:1::1", "2001:db8:2::1"):
                for _ in range(5):
                    record_failure(conn, login_ip_key("nobody", ip))
        msgs = [r.getMessage() for r in caplog.records if "auth.account_locked" in r.getMessage()]
        assert any("ip=2001:db8:1::/64" in m for m in msgs)
        assert any("ip=2001:db8:2::/64" in m for m in msgs)

    def test_reauth_lock_line_shows_the_plain_username(self, conn, caplog):
        with caplog.at_level("WARNING"):
            for _ in range(5):
                record_failure(conn, reauth_key("alice"))
        msg = next(
            r.getMessage() for r in caplog.records if "auth.account_locked" in r.getMessage()
        )
        assert "user=alice" in msg
        assert "scope=reauth" in msg

    def test_look_alike_usernames_log_differently(self, conn, caplog):
        with caplog.at_level("WARNING"):
            for name in ("admin", "admin\u200b", "ad min"):
                for _ in range(5):
                    record_failure(conn, login_ip_key(name, "203.0.113.5"))
        users = {
            r.getMessage().split("user=")[1].split(" ")[0]
            for r in caplog.records
            if "auth.account_locked" in r.getMessage()
        }
        assert len(users) == 3

    def test_unparsable_ip_cannot_split_a_log_line(self, conn, caplog):
        with caplog.at_level("WARNING"):
            for _ in range(5):
                record_failure(conn, login_ip_key("nobody", "1.2.3.4\nWARNING forged"))
        msg = next(
            r.getMessage() for r in caplog.records if "auth.account_locked" in r.getMessage()
        )
        assert "\n" not in msg
