"""Tests for mediaman.auth.password_hash.

Covers: create_user, authenticate, change_password, list_users, delete_user,
user_must_change_password, set_must_change_password.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mediaman.db import init_db
from mediaman.web.auth.login_lockout import login_ip_key
from mediaman.web.auth.password_hash import (
    BCRYPT_ROUNDS,
    authenticate,
    change_password,
    create_user,
    delete_user,
    list_users,
    set_must_change_password,
    user_must_change_password,
    verify_password,
)
from mediaman.web.auth.reauth import REAUTH_LOCKOUT_PREFIX


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


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
        # ``$2b$`` is the modern bcrypt prefix; the README claims cost
        # factor 12 is in force, so the third ``$``-delimited segment
        # MUST encode 12 rounds. A drift here (e.g. someone shipping
        # ``rounds=10`` for "speed") would silently weaken password
        # storage without changing any other observable behaviour.
        # Hash format: ``$2b$<rounds>$<22-byte salt><31-byte hash>``
        # so we can pull the cost out of the third field directly.
        password_hash = row["password_hash"]
        assert password_hash.startswith("$2b$")
        parts = password_hash.split("$")
        # parts[0] is empty (leading $); parts[1] = "2b"; parts[2] = cost.
        assert parts[1] == "2b"
        assert parts[2] == f"{BCRYPT_ROUNDS:02d}"
        assert int(parts[2]) == 12, (
            f"Expected bcrypt cost factor 12, got {parts[2]!r}. "
            "README documents cost 12 — keep them in sync."
        )

    def test_duplicate_username_raises_user_exists_error(self, conn):
        from mediaman.web.auth.password_hash import UserExistsError

        create_user(conn, "alice", "pass1", enforce_policy=False)
        with pytest.raises(UserExistsError, match="already exists"):
            create_user(conn, "alice", "pass2", enforce_policy=False)

    def test_must_change_password_defaults_to_false(self, conn):
        create_user(conn, "alice", "pass", enforce_policy=False)
        row = conn.execute(
            "SELECT must_change_password FROM admin_users WHERE username='alice'"
        ).fetchone()
        assert row["must_change_password"] == 0


class TestAuthenticate:
    def test_correct_credentials_return_true(self, conn):
        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        assert authenticate(conn, "alice", "correct-pass", client_ip="203.0.113.7") is True

    def test_wrong_password_returns_false(self, conn):
        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        assert authenticate(conn, "alice", "wrong-pass", client_ip="203.0.113.7") is False

    def test_nonexistent_user_returns_false(self, conn):
        # Must also not raise — constant-time dummy bcrypt check fires.
        assert authenticate(conn, "ghost", "any-pass", client_ip="203.0.113.7") is False

    def test_empty_username_returns_false(self, conn):
        assert authenticate(conn, "", "pass", client_ip="203.0.113.7") is False


class TestChangePassword:
    def test_change_with_correct_old_password(self, conn):
        create_user(conn, "alice", "old-pass-1", enforce_policy=False)
        ok = change_password(conn, "alice", "old-pass-1", "new-pass-2", enforce_policy=False)
        assert ok is True
        # New password must work; old must not.
        assert authenticate(conn, "alice", "new-pass-2", client_ip="203.0.113.7") is True
        assert authenticate(conn, "alice", "old-pass-1", client_ip="203.0.113.7") is False

    def test_change_with_wrong_old_password_returns_false(self, conn):
        create_user(conn, "alice", "correct-old", enforce_policy=False)
        ok = change_password(conn, "alice", "incorrect-old", "new-pass", enforce_policy=False)
        assert ok is False
        # Original password still works.
        assert authenticate(conn, "alice", "correct-old", client_ip="203.0.113.7") is True

    def test_change_revokes_existing_sessions(self, conn):
        from mediaman.web.auth.session_store import create_session, validate_session

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

    def test_change_succeeds_while_login_is_locked(self, conn):
        create_user(conn, "alice", "old-pass", enforce_policy=False)
        for _ in range(5):
            authenticate(conn, "alice", "wrong", client_ip="203.0.113.7")

        ok = change_password(conn, "alice", "old-pass", "new-pass", enforce_policy=False)

        assert ok is True

    def test_wrong_old_password_does_not_add_to_login_lock(self, conn):
        create_user(conn, "alice", "old-pass", enforce_policy=False)
        for _ in range(5):
            authenticate(conn, "alice", "wrong", client_ip="203.0.113.7")

        change_password(conn, "alice", "wrong-old", "new-pass", enforce_policy=False)

        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            (login_ip_key("alice", "203.0.113.7"),),
        ).fetchone()
        assert row["failure_count"] == 5


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

    def test_a_user_created_later_under_the_name_does_not_inherit_a_lock(self, conn):
        from mediaman.web.auth.login_lockout import is_locked_out, reauth_key, record_failure

        create_user(conn, "alice", "p1", enforce_policy=False)
        create_user(conn, "bob", "p2", enforce_policy=False)
        for _ in range(5):
            record_failure(conn, login_ip_key("bob", "203.0.113.7"))
            record_failure(conn, login_ip_key("bob", "198.51.100.4"))
        record_failure(conn, "bob")
        record_failure(conn, reauth_key("bob"))
        record_failure(conn, "carol")
        assert is_locked_out(conn, login_ip_key("bob", "203.0.113.7")) is True

        assert delete_user(conn, self._uid(conn, "bob"), current_username="alice") is True

        left = [r["username"] for r in conn.execute("SELECT username FROM login_failures")]
        assert left == ["carol"]
        create_user(conn, "bob", "p3", enforce_policy=False)
        assert authenticate(conn, "bob", "p3", client_ip="203.0.113.7") is True

    def test_a_refused_delete_keeps_the_failure_record(self, conn):
        from mediaman.web.auth.login_lockout import record_failure

        create_user(conn, "alice", "p1", enforce_policy=False)
        record_failure(conn, "alice")
        assert delete_user(conn, self._uid(conn, "alice"), current_username="alice") is False
        assert conn.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 1


class TestLongPasswordEntropyPreserved:
    """Bcrypt silently truncates inputs to 72 bytes; the SHA-256 pre-hash
    in _prepare_bcrypt_input must defeat that so two passwords
    differing only in their bytes 73+ remain distinguishable."""

    def test_two_passwords_differing_after_byte_72_are_distinguishable(self, conn):
        # Build two passwords whose first 72 bytes are identical and
        # which then diverge. Without the pre-hash, bcrypt would treat
        # them as the same input and authenticate() would return True
        # for the wrong one.
        common_prefix = "A" * 72
        password_a = common_prefix + "tail-a-12345"
        password_b = common_prefix + "tail-b-67890"
        assert password_a[:72] == password_b[:72]
        assert password_a != password_b

        create_user(conn, "alice", password_a, enforce_policy=False)
        # The correct long password must verify.
        assert authenticate(conn, "alice", password_a, client_ip="203.0.113.7") is True
        # The other long password — same first 72 bytes — must NOT
        # verify. Without the pre-hash, bcrypt would have treated it as
        # equal and this would return True.
        assert authenticate(conn, "alice", password_b, client_ip="203.0.113.7") is False

    def test_short_password_round_trips(self, conn):
        """Inputs ≤ 72 bytes must continue to verify against bcrypt
        directly, so existing rows minted before the pre-hash landed
        still validate."""
        create_user(conn, "alice", "short-pass-789!", enforce_policy=False)
        assert authenticate(conn, "alice", "short-pass-789!", client_ip="203.0.113.7") is True

    def test_existing_row_without_prehash_still_verifies(self, conn):
        """Belt-and-braces: a row inserted with the legacy
        ``bcrypt.hashpw(password.encode(), ...)`` pattern must still
        verify under the new authenticate(). This guards against an
        accidental scheme switch that would lock everyone out."""
        import bcrypt

        legacy = "legacy-pass-abc"
        legacy_hash = bcrypt.hashpw(legacy.encode(), bcrypt.gensalt(rounds=4)).decode()
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) "
            "VALUES (?, ?, '2026-01-01')",
            ("legacy", legacy_hash),
        )
        conn.commit()
        assert authenticate(conn, "legacy", legacy, client_ip="203.0.113.7") is True

    def test_bcrypt_rounds_constant_used(self, conn):
        """The bcrypt cost factor MUST come from the BCRYPT_ROUNDS
        module constant rather than scattered ``rounds=12`` literals,
        so the dummy hash and the real-user hash agree."""
        assert BCRYPT_ROUNDS == 12

    def test_unicode_normalisation_round_trip(self, conn):
        """``é`` (precomposed, U+00E9) and ``e`` + combining acute
        (U+0065 U+0301) must hash to the same bcrypt value so a user
        who sets their password on one OS can still log in from
        another."""
        precomposed = "Café-passphrase-789"
        decomposed = "Café-passphrase-789"
        # Sanity: these are different byte sequences.
        assert precomposed.encode("utf-8") != decomposed.encode("utf-8")
        # But identical to a human eye after NFKC.
        create_user(conn, "alice", precomposed, enforce_policy=False)
        assert authenticate(conn, "alice", decomposed, client_ip="203.0.113.7") is True
        # Sanity: a different password still fails.
        assert authenticate(conn, "alice", "Cafe-different-789", client_ip="203.0.113.7") is False


class TestEmptyUsernameShortCircuit:
    """An empty username must return False without burning a bcrypt
    round, otherwise an unauthenticated attacker can stream
    empty-username requests at the endpoint and DoS the server's CPU."""

    def test_empty_username_does_not_call_bcrypt(self, conn):
        with patch("mediaman.web.auth.password_hash._authenticate.bcrypt") as mock_bcrypt:
            assert authenticate(conn, "", "any-password", client_ip="203.0.113.7") is False
            assert not mock_bcrypt.checkpw.called
            assert not mock_bcrypt.hashpw.called

    def test_empty_username_returns_false_quickly(self, conn):
        # Smoke test: doesn't hit bcrypt, so wall time is in the
        # micro-second range. We don't measure here (CI noise) — the
        # mock-based test above is the load-bearing assertion.
        assert authenticate(conn, "", "any", client_ip="203.0.113.7") is False


class TestLockedAccountSkipsBcrypt:
    """When the account is already locked, authenticate() must skip the
    bcrypt round. record_failure() is still called so the escalation
    thresholds (5/10/15 → 15min/1h/24h) remain reachable."""

    def test_locked_account_skips_bcrypt_call(self, conn):
        from mediaman.web.auth.login_lockout import record_failure

        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        # Force the account into a locked state.
        for _ in range(5):
            record_failure(conn, login_ip_key("alice", "203.0.113.7"))
        # Now patch bcrypt and confirm authenticate does not call it.
        with patch("mediaman.web.auth.password_hash._authenticate.bcrypt") as mock_bcrypt:
            assert authenticate(conn, "alice", "anything", client_ip="203.0.113.7") is False
            assert not mock_bcrypt.checkpw.called
            assert not mock_bcrypt.hashpw.called

    def test_locked_account_lookup_constant_for_existing_and_missing(self, conn):
        """Lockout state lookup must take roughly the same time whether
        the username exists or not, otherwise a timing channel
        enumerates valid usernames. We assert by execution-path: both
        cases skip bcrypt entirely once locked."""
        from mediaman.web.auth.login_lockout import record_failure

        # Real user, locked.
        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        for _ in range(5):
            record_failure(conn, login_ip_key("alice", "203.0.113.7"))

        # Phantom user, also locked. We push the counter directly so a
        # ghost username is "locked" without ever existing in
        # admin_users.
        for _ in range(5):
            record_failure(conn, login_ip_key("ghost", "203.0.113.7"))

        with patch("mediaman.web.auth.password_hash._authenticate.bcrypt") as mock_bcrypt:
            assert authenticate(conn, "alice", "anything", client_ip="203.0.113.7") is False
            assert authenticate(conn, "ghost", "anything", client_ip="203.0.113.7") is False
            assert not mock_bcrypt.checkpw.called
            assert not mock_bcrypt.hashpw.called

    def test_locked_account_keeps_counting(self, conn):
        """Regression: the short-circuit MUST still call record_failure
        so the 10-failure / 15-failure escalation remains reachable."""
        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        # Trip the 5-failure lock.
        for _ in range(5):
            authenticate(conn, "alice", "wrong", client_ip="203.0.113.7")
        # Then 5 more under lockout — counter must climb to 10.
        for _ in range(5):
            authenticate(conn, "alice", "wrong", client_ip="203.0.113.7")
        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            (login_ip_key("alice", "203.0.113.7"),),
        ).fetchone()
        assert row["failure_count"] == 10


class TestChangePasswordFailureLogging:
    def test_wrong_old_password_logs_warning(self, conn, caplog):
        import logging

        create_user(conn, "alice", "correct-old", enforce_policy=False)
        with caplog.at_level(logging.WARNING, logger="mediaman"):
            ok = change_password(conn, "alice", "wrong-old", "new-pass", enforce_policy=False)
        assert ok is False
        # Look for the password.change_failed event.
        records = [r for r in caplog.records if "password.change_failed" in r.getMessage()]
        assert records, "expected password.change_failed warning"
        # Must NOT contain the password itself.
        for r in records:
            assert "wrong-old" not in r.getMessage()
            assert "new-pass" not in r.getMessage()


class TestChangePasswordTOCTOU:
    def test_user_deleted_between_password_check_and_update(self, conn):
        """If the user is deleted between the old-password check returning
        True and the UPDATE running, change_password() must roll back and
        return False — not silently claim success."""
        create_user(conn, "alice", "correct-old", enforce_policy=False)

        # Race: delete the user *during* the password check but before
        # the UPDATE. We do that by patching the check to side-effect
        # the deletion just before returning True.
        original_verify = verify_password

        def verify_then_delete(c, u, p):
            result = original_verify(c, u, p)
            if result and u == "alice":
                # Race in: deletion happens before our caller's UPDATE.
                c.execute("DELETE FROM admin_users WHERE username = ?", (u,))
                c.commit()
            return result

        with patch(
            "mediaman.web.auth.password_hash._change_password.verify_password",
            side_effect=verify_then_delete,
        ):
            ok = change_password(conn, "alice", "correct-old", "new-pass-2", enforce_policy=False)
        assert ok is False
        # No row should have been updated (the user was deleted, so
        # rowcount is 0 and the rollback should have left things
        # consistent).
        row = conn.execute("SELECT 1 FROM admin_users WHERE username = ?", ("alice",)).fetchone()
        assert row is None


class TestChangePasswordRaces:
    def test_a_change_is_refused_when_the_password_changed_after_its_check(self, conn):
        import bcrypt

        from mediaman.web.auth.login_lockout import reauth_key
        from mediaman.web.auth.session_store import create_session, validate_session

        create_user(conn, "alice", "old-pass", enforce_policy=False)
        token = create_session(conn, "alice")
        elsewhere = bcrypt.hashpw(b"set-elsewhere", bcrypt.gensalt(rounds=4)).decode()

        def verify_then_change(c, u, p):
            result = verify_password(c, u, p)
            c.execute("UPDATE admin_users SET password_hash = ? WHERE username = ?", (elsewhere, u))
            c.commit()
            return result

        with patch(
            "mediaman.web.auth.password_hash._change_password.verify_password",
            side_effect=verify_then_change,
        ):
            ok = change_password(conn, "alice", "old-pass", "new-pass", enforce_policy=False)

        assert ok is False
        assert verify_password(conn, "alice", "set-elsewhere") is True
        assert validate_session(conn, token) == "alice"
        # The old password was right when checked: no failure is counted for it.
        assert (
            conn.execute(
                "SELECT 1 FROM login_failures WHERE username = ?", (reauth_key("alice"),)
            ).fetchone()
            is None
        )

    def test_of_two_changes_at_once_only_one_succeeds(self, conn, db_path):
        import sqlite3
        import threading

        import bcrypt

        quick = bcrypt.hashpw(b"old-pass", bcrypt.gensalt(rounds=4)).decode()
        create_user(conn, "alice", "unused", enforce_policy=False)

        def attempt(new_password, barrier, results):
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=5000")
            try:
                barrier.wait(timeout=5)
                results[new_password] = change_password(
                    c, "alice", "old-pass", new_password, enforce_policy=False
                )
            finally:
                c.close()

        with patch("mediaman.web.auth.password_hash._change_password.BCRYPT_ROUNDS", 4):
            for _ in range(15):
                conn.execute(
                    "UPDATE admin_users SET password_hash = ? WHERE username = 'alice'", (quick,)
                )
                conn.commit()
                barrier = threading.Barrier(2)
                results: dict[str, bool] = {}
                threads = [
                    threading.Thread(target=attempt, args=(new, barrier, results))
                    for new in ("new-pass-a", "new-pass-b")
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)

                assert sorted(results.values()) == [False, True]
                winner = next(p for p, ok in results.items() if ok)
                loser = next(p for p, ok in results.items() if not ok)
                assert verify_password(conn, "alice", winner) is True
                assert verify_password(conn, "alice", loser) is False


class TestChangePasswordReauthInsideTransaction:
    def test_reauth_tickets_dropped_on_password_change(self, conn):
        """Reauth tickets keyed by the user's old session must be
        revoked atomically with the password-change transaction so a
        thief holding a ticket cannot redeem it under the freshly
        issued session."""
        from mediaman.web.auth.reauth import grant_recent_reauth, has_recent_reauth
        from mediaman.web.auth.session_store import create_session

        create_user(conn, "alice", "old-pass", enforce_policy=False)
        token = create_session(conn, "alice")
        grant_recent_reauth(conn, token, "alice")
        assert has_recent_reauth(conn, token, "alice") is True

        ok = change_password(conn, "alice", "old-pass", "new-pass", enforce_policy=False)
        assert ok is True

        # Both the session AND the reauth ticket must be gone.
        assert has_recent_reauth(conn, token, "alice") is False
        ticket_row = conn.execute(
            "SELECT 1 FROM reauth_tickets WHERE username = ?", ("alice",)
        ).fetchone()
        assert ticket_row is None


class TestChangePasswordCounterCleanupCatchNarrowed:
    """The post-transaction ``record_success`` counter cleanup in
    change_password is best-effort: a ``sqlite3.Error`` is
    swallowed (the password has already changed), but any non-DB
    exception is a bug in ``record_success`` and must propagate rather
    than being silently swallowed.

    ``record_success`` is also called by ``authenticate`` on a login
    success, so the patches below raise only for the re-auth namespace —
    that is the key the post-transaction cleanup call passes, never the
    login key ``authenticate`` uses.
    """

    @staticmethod
    def _raise_for_reauth_namespace(exc: Exception):
        def _side_effect(_conn, namespace, *_a, **_kw):
            if namespace.startswith(REAUTH_LOCKOUT_PREFIX):
                raise exc
            return None

        return _side_effect

    def test_sqlite_error_in_counter_cleanup_is_swallowed(self, conn, caplog):
        import logging
        import sqlite3

        create_user(conn, "alice", "old-pass", enforce_policy=False)
        # ``record_success`` is imported into change_password's body from
        # login_lockout; patch it there so only the cleanup call raises.
        with (
            patch(
                "mediaman.web.auth.login_lockout.record_success",
                side_effect=self._raise_for_reauth_namespace(
                    sqlite3.OperationalError("database is locked")
                ),
            ),
            caplog.at_level(logging.ERROR, logger="mediaman"),
        ):
            ok = change_password(conn, "alice", "old-pass", "new-pass", enforce_policy=False)
        # The rotation still succeeds — the counter cleanup is best-effort.
        assert ok is True
        assert authenticate(conn, "alice", "new-pass", client_ip="203.0.113.7") is True
        # And the swallowed failure is logged at ERROR via logger.exception.
        assert any("counter cleanup failed" in r.getMessage() for r in caplog.records), (
            "expected a logged counter-cleanup failure"
        )

    def test_non_sqlite_error_in_counter_cleanup_propagates(self, conn):
        # A bug in record_success (e.g. a TypeError) is NOT swallowed —
        # the narrowed ``except sqlite3.Error`` lets it surface.
        create_user(conn, "alice", "old-pass", enforce_policy=False)
        with (
            patch(
                "mediaman.web.auth.login_lockout.record_success",
                side_effect=self._raise_for_reauth_namespace(TypeError("record_success bug")),
            ),
            pytest.raises(TypeError, match="record_success bug"),
        ):
            change_password(conn, "alice", "old-pass", "new-pass", enforce_policy=False)
        # The password change itself already committed before the
        # best-effort cleanup ran — the propagating bug does not undo it.
        assert authenticate(conn, "alice", "new-pass", client_ip="203.0.113.7") is True


class TestDeleteUserReauthCleanupCatchNarrowed:
    """The post-transaction ``revoke_all_reauth_for`` cleanup in
    delete_user is best-effort: a ``sqlite3.Error`` is swallowed
    (the user row is already gone), but any non-DB exception is a bug
    and must propagate rather than being silently swallowed."""

    def _uid(self, conn, username: str) -> int:
        return conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()[
            "id"
        ]

    def test_sqlite_error_in_reauth_cleanup_is_swallowed(self, conn, caplog):
        import logging
        import sqlite3

        create_user(conn, "alice", "p1", enforce_policy=False)
        create_user(conn, "bob", "p2", enforce_policy=False)
        # ``revoke_all_reauth_for`` is imported into delete_user's body
        # from reauth; patch it there so the cleanup call raises.
        with (
            patch(
                "mediaman.web.auth.reauth.revoke_all_reauth_for",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            caplog.at_level(logging.ERROR, logger="mediaman"),
        ):
            ok = delete_user(conn, self._uid(conn, "bob"), current_username="alice")
        # The delete still succeeds — the reauth cleanup is best-effort.
        assert ok is True
        assert [u["username"] for u in list_users(conn)] == ["alice"]
        assert any("reauth cleanup failed" in r.getMessage() for r in caplog.records), (
            "expected a logged reauth-cleanup failure"
        )

    def test_non_sqlite_error_in_reauth_cleanup_propagates(self, conn):
        # A bug in the cleanup path (e.g. a TypeError) is NOT swallowed —
        # the narrowed ``except sqlite3.Error`` lets it surface.
        create_user(conn, "alice", "p1", enforce_policy=False)
        create_user(conn, "bob", "p2", enforce_policy=False)
        with (
            patch(
                "mediaman.web.auth.reauth.revoke_all_reauth_for",
                side_effect=TypeError("revoke_all_reauth_for bug"),
            ),
            pytest.raises(TypeError, match="revoke_all_reauth_for bug"),
        ):
            delete_user(conn, self._uid(conn, "bob"), current_username="alice")
        # The delete transaction already committed before the best-effort
        # cleanup ran — the propagating bug does not undo it.
        assert [u["username"] for u in list_users(conn)] == ["alice"]
