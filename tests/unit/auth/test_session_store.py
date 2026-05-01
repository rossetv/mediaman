"""Tests for mediaman.auth.session_store.

Covers: create_session, validate_session, destroy_session,
destroy_all_sessions_for, list_sessions_for, and the client-fingerprint
helpers.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from mediaman.auth.password_hash import create_user
from mediaman.auth.session_store import (
    _client_fingerprint,
    _hash_token,
    create_session,
    destroy_all_sessions_for,
    destroy_session,
    list_sessions_for,
    validate_session,
)
from mediaman.db import init_db


@pytest.fixture
def conn(db_path):
    c = init_db(str(db_path))
    create_user(c, "alice", "pass", enforce_policy=False)
    return c


# ---------------------------------------------------------------------------
# Finding 26: validate_session must not take the writer lock for ordinary
# requests.
# ---------------------------------------------------------------------------


class TestValidateSessionReadOnlyByDefault:
    def test_repeated_validation_skips_writer_within_throttle(self, conn):
        """Two validations within the refresh interval must not write twice.

        Regression: previously every validate_session call opened
        ``BEGIN IMMEDIATE`` and wrote ``last_used_at`` whenever the
        throttle had elapsed. With the read-only-by-default refactor
        the throttle still gates the write, but a second validation in
        the same minute must not hit the writer at all.
        """
        token = create_session(conn, "alice", user_agent="ua", client_ip="1.2.3.4")
        # First call: refresh window has just passed creation, so a
        # write happens to stamp last_used_at.
        assert validate_session(conn, token) == "alice"
        first_last_used = conn.execute(
            "SELECT last_used_at FROM admin_sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()["last_used_at"]

        # Second call in immediate succession: the throttle blocks the
        # refresh, so last_used_at must not change.
        assert validate_session(conn, token) == "alice"
        second_last_used = conn.execute(
            "SELECT last_used_at FROM admin_sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()["last_used_at"]
        assert second_last_used == first_last_used

    def test_validate_does_not_open_write_transaction_during_lookup(self, conn):
        """A reader connection should be able to query while validate_session runs.

        We can't intercept SQLite transaction state directly, but we
        can confirm validate_session works with the ``in_transaction``
        flag never staying set after the call returns — i.e. it never
        leaves a write transaction open.
        """
        token = create_session(conn, "alice", user_agent="ua", client_ip="1.2.3.4")
        validate_session(conn, token)
        # After return, no transaction must be hanging.
        assert conn.in_transaction is False

    def test_invalid_token_short_circuits_without_writes(self, conn):
        """A junk token returns None without touching the DB at all.

        ``in_transaction`` must remain False. The test only covers the
        symptom — a regression to BEGIN IMMEDIATE on every entry would
        either flip the flag or hold the writer lock and timing-show
        up as test slowness.
        """
        assert validate_session(conn, "not-a-token") is None
        assert conn.in_transaction is False

    def test_idle_expiry_still_deletes_session(self, conn):
        """The read-only fast-path must not skip the idle-expiry write."""
        token = create_session(conn, "alice", user_agent="ua", client_ip="1.2.3.4")
        # Force the last_used_at far enough in the past to trigger idle expiry.
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn.execute(
            "UPDATE admin_sessions SET last_used_at = ? WHERE token_hash = ?",
            (old, _hash_token(token)),
        )
        conn.commit()
        assert validate_session(conn, token) is None
        # Row must have been deleted.
        row = conn.execute(
            "SELECT 1 FROM admin_sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# _hash_token
# ---------------------------------------------------------------------------


class TestHashToken:
    def test_produces_sha256_hex(self):
        result = _hash_token("mytoken")
        assert result == hashlib.sha256(b"mytoken").hexdigest()

    def test_different_tokens_different_hashes(self):
        assert _hash_token("tok1") != _hash_token("tok2")


# ---------------------------------------------------------------------------
# _client_fingerprint
# ---------------------------------------------------------------------------


class TestClientFingerprint:
    def test_same_ua_and_ip_gives_same_fingerprint(self):
        fp1 = _client_fingerprint("Mozilla/5.0", "192.168.1.50")
        fp2 = _client_fingerprint("Mozilla/5.0", "192.168.1.99")
        # Same /24 network — fingerprint must match.
        assert fp1 == fp2

    def test_different_ua_gives_different_fingerprint(self):
        fp1 = _client_fingerprint("Firefox/120", "1.2.3.4")
        fp2 = _client_fingerprint("Chrome/120", "1.2.3.4")
        assert fp1 != fp2

    def test_ipv6_prefix_64(self):
        # Two addresses in the same /64 must share a fingerprint.
        fp1 = _client_fingerprint("UA", "2001:db8::1")
        fp2 = _client_fingerprint("UA", "2001:db8::2")
        assert fp1 == fp2

    def test_none_ip_uses_unknown_prefix(self):
        fp = _client_fingerprint("UA", None)
        assert "unknown" in fp

    def test_invalid_ip_uses_unknown_prefix(self):
        fp = _client_fingerprint("UA", "not-an-ip")
        assert "unknown" in fp


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_returns_64_hex_chars(self, conn):
        token = create_session(conn, "alice")
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_raw_token_not_stored_in_db(self, conn):
        token = create_session(conn, "alice")
        row = conn.execute("SELECT token FROM admin_sessions").fetchone()
        # Only the hash should be in the DB, never the raw token.
        assert row["token"] != token

    def test_custom_ttl_reflected_in_expiry(self, conn):
        create_session(conn, "alice", ttl_seconds=3600)
        row = conn.execute("SELECT created_at, expires_at FROM admin_sessions").fetchone()
        created = datetime.fromisoformat(row["created_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        delta = expires - created
        assert timedelta(minutes=59) < delta < timedelta(hours=1, minutes=1)

    def test_fingerprint_mode_off_stores_empty(self, conn, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "off")
        create_session(conn, "alice", user_agent="UA", client_ip="1.1.1.1")
        row = conn.execute("SELECT fingerprint FROM admin_sessions").fetchone()
        assert row["fingerprint"] == ""


# ---------------------------------------------------------------------------
# validate_session
# ---------------------------------------------------------------------------


class TestValidateSession:
    def test_valid_token_returns_username(self, conn):
        token = create_session(conn, "alice")
        assert validate_session(conn, token) == "alice"

    def test_expired_session_returns_none(self, conn):
        token = create_session(conn, "alice", ttl_seconds=-1)
        assert validate_session(conn, token) is None

    def test_unknown_token_returns_none(self, conn):
        assert validate_session(conn, "a" * 64) is None

    def test_malformed_token_returns_none(self, conn):
        # Wrong length — must be rejected by the regex before DB lookup.
        assert validate_session(conn, "a" * 32) is None
        assert validate_session(conn, "z" * 64) is None  # non-hex
        assert validate_session(conn, "") is None

    def test_fingerprint_mismatch_destroys_session(self, conn, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "strict")
        token = create_session(conn, "alice", user_agent="UA-1", client_ip="1.1.1.1")
        result = validate_session(conn, token, user_agent="UA-2", client_ip="9.9.9.9")
        assert result is None
        # Row must be gone — permanently revoked.
        assert validate_session(conn, token) is None

    def test_idle_timeout_expires_session(self, conn):
        token = create_session(conn, "alice")
        # Wind ``last_used_at`` back by 25 hours to trigger idle timeout.
        past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        conn.execute("UPDATE admin_sessions SET last_used_at = ?", (past,))
        conn.commit()
        assert validate_session(conn, token) is None


# ---------------------------------------------------------------------------
# destroy_session
# ---------------------------------------------------------------------------


class TestDestroySession:
    def test_destroys_existing_session(self, conn):
        token = create_session(conn, "alice")
        destroy_session(conn, token)
        assert validate_session(conn, token) is None

    def test_destroy_nonexistent_token_is_silent(self, conn):
        # Must not raise — idempotent cleanup.
        destroy_session(conn, "a" * 64)


# ---------------------------------------------------------------------------
# destroy_all_sessions_for
# ---------------------------------------------------------------------------


class TestDestroyAllSessionsFor:
    def test_revokes_all_sessions(self, conn):
        t1 = create_session(conn, "alice")
        t2 = create_session(conn, "alice")
        count = destroy_all_sessions_for(conn, "alice")
        assert count == 2
        assert validate_session(conn, t1) is None
        assert validate_session(conn, t2) is None

    def test_returns_zero_when_no_sessions(self, conn):
        count = destroy_all_sessions_for(conn, "alice")
        assert count == 0

    def test_only_removes_sessions_for_target_user(self, conn):
        create_user(conn, "bob", "pass", enforce_policy=False)
        alice_token = create_session(conn, "alice")
        _bob_token = create_session(conn, "bob")
        destroy_all_sessions_for(conn, "bob")
        # Alice's session must still be valid.
        assert validate_session(conn, alice_token) == "alice"


# ---------------------------------------------------------------------------
# list_sessions_for
# ---------------------------------------------------------------------------


class TestListSessionsFor:
    def test_returns_metadata_for_active_sessions(self, conn):
        create_session(conn, "alice", client_ip="10.0.0.1")
        sessions = list_sessions_for(conn, "alice")
        assert len(sessions) == 1
        assert sessions[0]["issued_ip"] == "10.0.0.1"

    def test_empty_when_no_sessions(self, conn):
        assert list_sessions_for(conn, "alice") == []

    def test_multiple_sessions_returned_newest_first(self, conn):
        create_session(conn, "alice")
        create_session(conn, "alice")
        sessions = list_sessions_for(conn, "alice")
        assert len(sessions) == 2
        # Ordered DESC by created_at — newer first.
        assert sessions[0]["created_at"] >= sessions[1]["created_at"]
