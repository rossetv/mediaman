"""Tests for mediaman.auth.password_hash.

Covers: create_user, authenticate, change_password, list_users, delete_user,
user_must_change_password, set_must_change_password.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mediaman.db import init_db
from mediaman.web.auth.password_hash import (
    BCRYPT_ROUNDS,
    authenticate,
    change_password,
    create_user,
    delete_user,
    list_users,
    set_must_change_password,
    user_must_change_password,
)


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


# ---------------------------------------------------------------------------
# Bcrypt 72-byte truncation defence (FINDINGS Domain 01: D01-1, D01-9)
# ---------------------------------------------------------------------------


class TestLongPasswordEntropyPreserved:
    """Bcrypt silently truncates inputs to 72 bytes; the SHA-256 pre-hash
    in :func:`_prepare_bcrypt_input` must defeat that so two passwords
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
        assert authenticate(conn, "alice", password_a) is True
        # The other long password — same first 72 bytes — must NOT
        # verify. Without the pre-hash, bcrypt would have treated it as
        # equal and this would return True.
        assert authenticate(conn, "alice", password_b) is False

    def test_short_password_round_trips(self, conn):
        """Inputs ≤ 72 bytes must continue to verify against bcrypt
        directly, so existing rows minted before the pre-hash landed
        still validate."""
        create_user(conn, "alice", "short-pass-789!", enforce_policy=False)
        assert authenticate(conn, "alice", "short-pass-789!") is True

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
        assert authenticate(conn, "legacy", legacy) is True

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
        assert authenticate(conn, "alice", decomposed) is True
        # Sanity: a different password still fails.
        assert authenticate(conn, "alice", "Cafe-different-789") is False


# ---------------------------------------------------------------------------
# Empty-username DoS short-circuit (FINDINGS Domain 01: D01-2)
# ---------------------------------------------------------------------------


class TestEmptyUsernameShortCircuit:
    """An empty username must return False without burning a bcrypt
    round, otherwise an unauthenticated attacker can stream
    empty-username requests at the endpoint and DoS the server's CPU."""

    def test_empty_username_does_not_call_bcrypt(self, conn):
        with patch("mediaman.web.auth.password_hash.bcrypt") as mock_bcrypt:
            assert authenticate(conn, "", "any-password") is False
            assert not mock_bcrypt.checkpw.called
            assert not mock_bcrypt.hashpw.called

    def test_empty_username_returns_false_quickly(self, conn):
        # Smoke test: doesn't hit bcrypt, so wall time is in the
        # micro-second range. We don't measure here (CI noise) — the
        # mock-based test above is the load-bearing assertion.
        assert authenticate(conn, "", "any") is False


# ---------------------------------------------------------------------------
# Locked-account short-circuit (FINDINGS Domain 01: D01-4)
# ---------------------------------------------------------------------------


class TestLockedAccountSkipsBcrypt:
    """When the account is already locked, authenticate() must skip the
    bcrypt round. record_failure() is still called so the escalation
    thresholds (5/10/15 → 15min/1h/24h) remain reachable — see C6 in
    test_login_lockout.py."""

    def test_locked_account_skips_bcrypt_call(self, conn):
        from mediaman.web.auth.login_lockout import record_failure

        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        # Force the account into a locked state.
        for _ in range(5):
            record_failure(conn, "alice")
        # Now patch bcrypt and confirm authenticate does not call it.
        with patch("mediaman.web.auth.password_hash.bcrypt") as mock_bcrypt:
            assert authenticate(conn, "alice", "anything") is False
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
            record_failure(conn, "alice")

        # Phantom user, also locked. We push the counter directly so a
        # ghost username is "locked" without ever existing in
        # admin_users.
        for _ in range(5):
            record_failure(conn, "ghost")

        with patch("mediaman.web.auth.password_hash.bcrypt") as mock_bcrypt:
            assert authenticate(conn, "alice", "anything") is False
            assert authenticate(conn, "ghost", "anything") is False
            assert not mock_bcrypt.checkpw.called
            assert not mock_bcrypt.hashpw.called

    def test_locked_account_keeps_counting(self, conn):
        """Regression: the short-circuit MUST still call record_failure
        so the 10-failure / 15-failure escalation remains reachable."""
        create_user(conn, "alice", "correct-pass", enforce_policy=False)
        # Trip the 5-failure lock.
        for _ in range(5):
            authenticate(conn, "alice", "wrong")
        # Then 5 more under lockout — counter must climb to 10.
        for _ in range(5):
            authenticate(conn, "alice", "wrong")
        row = conn.execute(
            "SELECT failure_count FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert row["failure_count"] == 10


# ---------------------------------------------------------------------------
# change_password failure logging (FINDINGS Domain 01: D01-5)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# change_password TOCTOU (FINDINGS Domain 01: D01-6)
# ---------------------------------------------------------------------------


class TestChangePasswordTOCTOU:
    def test_user_deleted_between_authenticate_and_update(self, conn):
        """If the user is deleted between authenticate() returning True
        and the UPDATE running, change_password() must roll back and
        return False — not silently claim success."""
        create_user(conn, "alice", "correct-old", enforce_policy=False)

        # Race: delete the user *during* authenticate() but before the
        # UPDATE. We do that by patching authenticate to side-effect
        # the deletion just before returning True.
        original_authenticate = authenticate

        def authenticate_then_delete(c, u, p, *, record_failures=True):
            result = original_authenticate(c, u, p, record_failures=record_failures)
            if result and u == "alice":
                # Race in: deletion happens before our caller's UPDATE.
                c.execute("DELETE FROM admin_users WHERE username = ?", (u,))
                c.commit()
            return result

        with patch(
            "mediaman.web.auth.password_hash.authenticate",
            side_effect=authenticate_then_delete,
        ):
            ok = change_password(conn, "alice", "correct-old", "new-pass-2", enforce_policy=False)
        assert ok is False
        # No row should have been updated (the user was deleted, so
        # rowcount is 0 and the rollback should have left things
        # consistent).
        row = conn.execute("SELECT 1 FROM admin_users WHERE username = ?", ("alice",)).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# change_password reauth ticket revocation inside the transaction
# (FINDINGS Domain 01: D01-7)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Best-effort counter-cleanup catch is narrowed to sqlite3.Error
# ---------------------------------------------------------------------------


class TestChangePasswordCounterCleanupCatchNarrowed:
    """The post-transaction ``record_success`` counter cleanup in
    :func:`change_password` is best-effort: a ``sqlite3.Error`` is
    swallowed (the password has already changed), but any non-DB
    exception is a bug in ``record_success`` and must propagate rather
    than being silently swallowed.

    ``record_success`` is also called by ``authenticate`` on the
    success path *before* the cleanup site, so the patches below raise
    only for the ``reauth:<username>`` namespace — that is the argument
    the post-transaction cleanup call passes, never the bare username
    the ``authenticate`` path uses.
    """

    @staticmethod
    def _raise_for_reauth_namespace(exc: Exception):
        def _side_effect(_conn, namespace, *_a, **_kw):
            if namespace.startswith("reauth:"):
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
        assert authenticate(conn, "alice", "new-pass") is True
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
        assert authenticate(conn, "alice", "new-pass") is True


# ---------------------------------------------------------------------------
# Best-effort reauth-cleanup catch in delete_user is narrowed to sqlite3.Error
# ---------------------------------------------------------------------------


class TestDeleteUserReauthCleanupCatchNarrowed:
    """The post-transaction ``revoke_all_reauth_for`` cleanup in
    :func:`delete_user` is best-effort: a ``sqlite3.Error`` is swallowed
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
