"""Tests for mediaman.auth.session_store.

Covers: create_session, validate_session, destroy_session,
destroy_all_sessions_for, list_sessions_for, and the client-fingerprint
helpers.
"""

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import (
    _client_fingerprint,
    _hash_token,
    create_session,
    destroy_all_sessions_for,
    destroy_session,
    list_sessions_for,
    validate_session,
)


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

    def test_idle_expiry_still_deletes_session(self, conn, freezer):
        """The read-only fast-path must not skip the idle-expiry write."""
        token = create_session(conn, "alice", user_agent="ua", client_ip="1.2.3.4")
        # Force the last_used_at far enough in the past to trigger idle expiry.
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
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

    def test_idle_timeout_expires_session(self, conn, freezer):
        token = create_session(conn, "alice")
        # Wind ``last_used_at`` back by 25 hours to trigger idle timeout.
        past = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
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


# ---------------------------------------------------------------------------
# H-4: session destruction must also revoke matching reauth tickets
# ---------------------------------------------------------------------------


class TestSessionDestructionRevokesReauth:
    """H-4: every path that deletes a session row must also drop the matching
    reauth ticket — the previous code left the ticket alive until expiry, so
    a stolen cookie + ticket pair stayed replayable after logout / idle
    expiry / fingerprint mismatch.
    """

    def test_destroy_session_revokes_reauth(self, conn):
        from mediaman.web.auth.reauth import grant_recent_reauth, has_recent_reauth

        token = create_session(conn, "alice")
        grant_recent_reauth(conn, token, "alice")
        assert has_recent_reauth(conn, token, "alice") is True

        destroy_session(conn, token)

        assert has_recent_reauth(conn, token, "alice") is False

    def test_idle_expiry_revokes_reauth(self, conn, freezer):
        from mediaman.web.auth.reauth import grant_recent_reauth, has_recent_reauth

        token = create_session(conn, "alice", user_agent="ua", client_ip="1.2.3.4")
        grant_recent_reauth(conn, token, "alice")

        # Stale last_used_at: 25 h past the idle threshold (24 h).  Must be
        # strictly greater-than, so use 25 h to guarantee expiry under a frozen clock.
        stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        conn.execute(
            "UPDATE admin_sessions SET last_used_at = ? WHERE token_hash = ?",
            (stale, _hash_token(token)),
        )
        conn.commit()

        # validate_session sees the staleness and runs the idle-expiry destroy.
        assert validate_session(conn, token, user_agent="ua", client_ip="1.2.3.4") is None
        assert has_recent_reauth(conn, token, "alice") is False

    def test_fingerprint_mismatch_revokes_reauth(self, conn, monkeypatch):
        """Set strict fingerprint mode and validate from a different IP."""
        from mediaman.web.auth.reauth import grant_recent_reauth, has_recent_reauth

        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "strict")

        token = create_session(conn, "alice", user_agent="ua", client_ip="1.2.3.4")
        grant_recent_reauth(conn, token, "alice")

        # Different IP → fingerprint mismatch → destroy + reauth revoke.
        result = validate_session(conn, token, user_agent="ua", client_ip="9.9.9.9")
        assert result is None
        assert has_recent_reauth(conn, token, "alice") is False

    def test_destroy_all_sessions_for_revokes_all_reauth(self, conn):
        """Bulk session purge must drop every owned reauth ticket too."""
        from mediaman.web.auth.reauth import grant_recent_reauth, has_recent_reauth

        t1 = create_session(conn, "alice")
        t2 = create_session(conn, "alice")
        grant_recent_reauth(conn, t1, "alice")
        grant_recent_reauth(conn, t2, "alice")

        destroy_all_sessions_for(conn, "alice")

        # ``destroy_all_sessions_for`` calls ``revoke_all_reauth_for`` already
        # (added when reauth shipped); this test guards that wiring against
        # regressions that drop it.
        assert has_recent_reauth(conn, t1, "alice") is False
        assert has_recent_reauth(conn, t2, "alice") is False


# ---------------------------------------------------------------------------
# Audit: strict fingerprint mode must differ from loose
# ---------------------------------------------------------------------------


class TestStrictFingerprintMode:
    """Audit: ``strict`` was silently identical to ``loose`` — both bucketed
    IPs and truncated UA hashes.  Strict mode now uses the FULL IP and the
    FULL SHA-256 UA hash so a stolen cookie replayed from a sibling /24
    or with a single User-Agent character changed is caught.

    Trade-offs documented in :data:`session_store._VALID_FINGERPRINT_MODES`:
    strict is intolerant of CGNAT IP rotation and any UA churn.
    """

    def test_strict_ipv4_no_bucketing(self):
        # Same /24 — loose mode treats as identical, strict must NOT.
        loose1 = _client_fingerprint("UA", "192.168.1.50", mode="loose")
        loose2 = _client_fingerprint("UA", "192.168.1.99", mode="loose")
        strict1 = _client_fingerprint("UA", "192.168.1.50", mode="strict")
        strict2 = _client_fingerprint("UA", "192.168.1.99", mode="strict")

        assert loose1 == loose2  # loose bucketed at /24 — same bucket.
        assert strict1 != strict2  # strict — full IP, must differ.

    def test_strict_ipv6_no_bucketing(self):
        # Same /64 — loose treats as identical, strict must NOT.
        loose1 = _client_fingerprint("UA", "2001:db8::1", mode="loose")
        loose2 = _client_fingerprint("UA", "2001:db8::abcd", mode="loose")
        strict1 = _client_fingerprint("UA", "2001:db8::1", mode="strict")
        strict2 = _client_fingerprint("UA", "2001:db8::abcd", mode="strict")

        assert loose1 == loose2
        assert strict1 != strict2

    def test_strict_uses_full_ua_hash(self):
        # 16-char loose vs 64-char strict.
        loose_fp = _client_fingerprint("Mozilla/5.0", "1.2.3.4", mode="loose")
        strict_fp = _client_fingerprint("Mozilla/5.0", "1.2.3.4", mode="strict")
        loose_ua = loose_fp.split(":", 1)[0]
        strict_ua = strict_fp.split(":", 1)[0]

        assert len(loose_ua) == 16
        assert len(strict_ua) == 64
        # Loose is a prefix of strict — same SHA-256, different truncation.
        assert strict_ua.startswith(loose_ua)

    def test_strict_and_loose_differ_for_same_client(self):
        loose = _client_fingerprint("Mozilla/5.0", "192.168.1.50", mode="loose")
        strict = _client_fingerprint("Mozilla/5.0", "192.168.1.50", mode="strict")
        assert loose != strict

    def test_loose_unchanged_default_mode(self):
        # Sanity: bare call (no mode arg) still defaults to loose.
        default = _client_fingerprint("Mozilla/5.0", "192.168.1.50")
        loose = _client_fingerprint("Mozilla/5.0", "192.168.1.50", mode="loose")
        assert default == loose

    def test_unknown_mode_defaults_to_loose(self):
        # An unrecognised mode falls through to loose buckets so a
        # typo in the env var cannot silently disable the check.
        unknown = _client_fingerprint("Mozilla/5.0", "192.168.1.50", mode="garbage")
        loose = _client_fingerprint("Mozilla/5.0", "192.168.1.50", mode="loose")
        assert unknown == loose

    def test_strict_mode_catches_sibling_ip(self, db_path, monkeypatch):
        """End-to-end: in strict mode, a request from 1.2.3.4 + UA-A
        cannot validate a session created at 1.2.3.5 + UA-A even though
        the two IPs share a /24 bucket.  Loose mode would let this
        through; strict mode invalidates."""
        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "strict")
        conn = init_db(str(db_path))
        create_user(conn, "alice", "pass", enforce_policy=False)
        token = create_session(conn, "alice", user_agent="UA-A", client_ip="1.2.3.4")

        # Sibling IP in the same /24.
        result = validate_session(conn, token, user_agent="UA-A", client_ip="1.2.3.5")
        assert result is None

    def test_loose_mode_tolerates_sibling_ip(self, db_path, monkeypatch):
        """The contrast: loose mode DOES treat 1.2.3.4 and 1.2.3.5 as
        the same client — that is the documented trade-off."""
        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "loose")
        conn = init_db(str(db_path))
        create_user(conn, "alice", "pass", enforce_policy=False)
        token = create_session(conn, "alice", user_agent="UA-A", client_ip="1.2.3.4")

        result = validate_session(conn, token, user_agent="UA-A", client_ip="1.2.3.5")
        assert result == "alice"


# ---------------------------------------------------------------------------
# Audit: expires_at must be parsed to datetime, not string-compared
# ---------------------------------------------------------------------------


class TestExpiresAtParsing:
    """Audit: ISO-8601 string ordering is fragile across format drift —
    e.g. trailing ``Z`` vs ``+00:00``.  ``expires_at`` comparisons must
    parse to ``datetime`` first.
    """

    def test_expires_with_trailing_z_is_parsed_correctly(self, conn, freezer):
        """A row stored with ``Z`` suffix (e.g. from a future migration)
        must still order against an ``+00:00`` row.

        ``datetime.fromisoformat`` only accepts ``Z`` from Python 3.11+;
        this test guards against future format drift breaking the sweep
        logic.
        """
        token = create_session(conn, "alice")
        # Pin expires_at to a known-future timestamp using the ``Z``
        # suffix flavour.  Python 3.11+ ``fromisoformat`` accepts this.
        future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute(
            "UPDATE admin_sessions SET expires_at = ? WHERE token_hash = ?",
            (future, _hash_token(token)),
        )
        conn.commit()
        # The session is in the future — must validate as alive even
        # though the format differs from what create_session wrote.
        assert validate_session(conn, token) == "alice"

    def test_corrupt_expires_at_does_not_misorder(self, conn):
        """A corrupt ``expires_at`` string must not silently expire a
        session via lexicographic ordering."""
        token = create_session(conn, "alice")
        conn.execute(
            "UPDATE admin_sessions SET expires_at = ? WHERE token_hash = ?",
            ("not-a-timestamp", _hash_token(token)),
        )
        conn.commit()
        # _parse_iso_aware returns None on a corrupt cell, which the
        # validate path treats as "no expiry stored" — the session is
        # still valid (idle-expiry will catch a long-stale row anyway).
        assert validate_session(conn, token) == "alice"


# ---------------------------------------------------------------------------
# Audit: atomic session-and-reauth delete
# ---------------------------------------------------------------------------


class TestAtomicSessionAndReauthDelete:
    """Audit: ``_delete_session_with_commit`` used to commit the session
    delete and then call reauth-revoke in a swallowed try/except.  If the
    revoke failed, the session was gone but the ticket survived.  Now both
    deletes happen inside one ``BEGIN IMMEDIATE`` so a failure on either
    side rolls both back.
    """

    def test_destroy_session_failure_rolls_back_both(self, conn, monkeypatch):
        """Force the reauth-revoke to raise and verify the session row
        is preserved (atomic rollback)."""
        from mediaman.web.auth import session_store
        from mediaman.web.auth.reauth import grant_recent_reauth

        token = create_session(conn, "alice")
        grant_recent_reauth(conn, token, "alice")

        # Patch ``revoke_reauth_by_hash_in_tx`` to raise so the
        # transaction must roll back. We patch the symbol on the
        # reauth module since session_store imports it lazily.
        from mediaman.web.auth import reauth

        def boom(_conn, _hash):
            raise RuntimeError("simulated reauth-side failure")

        monkeypatch.setattr(reauth, "revoke_reauth_by_hash_in_tx", boom)

        with pytest.raises(RuntimeError, match="simulated reauth-side failure"):
            destroy_session(conn, token)

        # The session row is still alive — atomic rollback worked.
        assert validate_session(conn, token) == "alice"

        # And the reauth ticket also still exists — both rolled back.
        from mediaman.web.auth.reauth import has_recent_reauth

        # Restore the original function so has_recent_reauth doesn't
        # also blow up if it touches the patched symbol.
        monkeypatch.undo()
        assert has_recent_reauth(conn, token, "alice") is True

        # Sanity: confirm session_store imports the function lazily so
        # the patch test is meaningful (not testing module-level import
        # state).
        assert hasattr(session_store, "_delete_session_with_commit")

    def test_idle_expiry_failure_does_not_500(self, conn, monkeypatch, freezer):
        """A *transient DB* failure during idle-expiry must NOT propagate
        to the validate_session caller — the user just sees "not
        authenticated" for that request and the next request retries
        cleanly.

        WP-C3: ``_try_delete_session`` now catches ``sqlite3.Error`` only,
        so the failure simulated here must be a DB error (lock
        contention is the only thing the swallow legitimately
        contemplates). A non-DB exception is a bug and is asserted to
        propagate by ``TestEvictionPathFailsClosedOnNonDbError``.
        """
        from mediaman.web.auth import reauth

        token = create_session(conn, "alice")
        # Wind ``last_used_at`` back to trigger idle expiry on the next
        # validate.
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        conn.execute("UPDATE admin_sessions SET last_used_at = ?", (old,))
        conn.commit()

        def boom(_conn, _hash):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(reauth, "revoke_reauth_by_hash_in_tx", boom)

        # Must not raise; must return None (session invalid).
        assert validate_session(conn, token) is None


# ---------------------------------------------------------------------------
# WP-C3: audit/cleanup swallows must be operator-visible, not DEBUG-hidden,
# and the eviction-path catches must be DB-only so a real bug fails closed.
# ---------------------------------------------------------------------------


class TestSwallowedFailuresAreVisible:
    """WP-C3: several best-effort writes in ``session_store`` previously
    logged a swallowed failure at ``logger.debug(..., exc_info=True)``.
    DEBUG is disabled in production, so those failures were invisible —
    including the audit-log write for a session destruction, a required
    audit event. The swallow stays (these writes are genuinely
    best-effort) but the log level is raised so the failure is seen.
    """

    def test_destroy_session_audit_failure_is_logged_at_warning_or_above(
        self, conn, monkeypatch, caplog
    ):
        """A failing audit write inside ``destroy_session`` must produce a
        log record at WARNING or above — not a DEBUG line that production
        never emits — and the logout must still complete.
        """
        from mediaman.core import audit

        token = create_session(conn, "alice")

        def boom(*_args, **_kwargs):
            # security_event swallows its own internals, so patch the
            # whole callable to force destroy_session's handler. A
            # sqlite3.Error is the narrowed catch the handler expects.
            raise sqlite3.OperationalError("simulated audit-log write failure")

        monkeypatch.setattr(audit, "security_event", boom)

        with caplog.at_level(logging.WARNING, logger="mediaman.web.auth.session_store"):
            # Must not raise — the session row is already gone; a failed
            # audit write must never 500 a logout that has happened.
            destroy_session(conn, token)

        # The failure is operator-visible: a WARNING-or-above record exists.
        visible = [
            r
            for r in caplog.records
            if r.name == "mediaman.web.auth.session_store"
            and r.levelno >= logging.WARNING
            and "audit" in r.getMessage().lower()
        ]
        assert visible, "destroy_session audit failure must log at WARNING or above"
        # And it carries the traceback for forensics.
        assert visible[0].exc_info is not None

        # The logout still took effect despite the audit failure.
        assert validate_session(conn, token) is None

    def test_cleanup_reauth_sweep_failure_is_logged_at_warning_or_above(
        self, conn, monkeypatch, caplog
    ):
        """A failing reauth sweep inside ``_cleanup_expired_with_commit``
        must log at WARNING or above and must not abort the session
        sweep that already ran.
        """
        from mediaman.web.auth import reauth, session_store

        # An expired session row so the session sweep has work to do.
        expired = create_session(conn, "alice", ttl_seconds=-1)

        def boom(_conn, _now_iso):
            raise sqlite3.OperationalError("simulated reauth sweep failure")

        monkeypatch.setattr(reauth, "cleanup_expired_reauth", boom)

        with caplog.at_level(logging.WARNING, logger="mediaman.web.auth.session_store"):
            session_store._cleanup_expired_with_commit(conn, datetime.now(UTC).isoformat())

        visible = [
            r
            for r in caplog.records
            if r.name == "mediaman.web.auth.session_store" and r.levelno >= logging.WARNING
        ]
        assert visible, "cleanup_expired_reauth failure must log at WARNING or above"
        assert visible[0].exc_info is not None

        # The session sweep still ran — the expired row is gone.
        row = conn.execute(
            "SELECT 1 FROM admin_sessions WHERE token_hash = ?",
            (_hash_token(expired),),
        ).fetchone()
        assert row is None

    def test_destroy_all_reauth_revoke_failure_is_logged_at_warning_or_above(
        self, conn, monkeypatch, caplog
    ):
        """A failing bulk reauth revoke inside ``destroy_all_sessions_for``
        must log at WARNING or above and must not roll back the session
        delete that already committed.
        """
        from mediaman.web.auth import reauth

        create_session(conn, "alice")
        create_session(conn, "alice")

        def boom(_conn, _username):
            raise sqlite3.OperationalError("simulated bulk reauth revoke failure")

        monkeypatch.setattr(reauth, "revoke_all_reauth_for", boom)

        with caplog.at_level(logging.WARNING, logger="mediaman.web.auth.session_store"):
            count = destroy_all_sessions_for(conn, "alice")

        # The session delete still committed despite the reauth failure.
        assert count == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM admin_sessions WHERE username = ?",
                ("alice",),
            ).fetchone()["n"]
            == 0
        )

        visible = [
            r
            for r in caplog.records
            if r.name == "mediaman.web.auth.session_store" and r.levelno >= logging.WARNING
        ]
        assert visible, "revoke_all_reauth_for failure must log at WARNING or above"
        assert visible[0].exc_info is not None


class TestEvictionPathFailsClosedOnNonDbError:
    """WP-C3: ``_try_delete_session`` previously caught ``Exception``,
    so a programming error (bad import, ``TypeError``) on the
    security-critical session-eviction path was silently downgraded to a
    WARNING and the stolen/expired session was left alive — fail *open*.
    The catch is now ``sqlite3.Error`` only: a non-DB exception
    propagates so a real bug is loud, not a silent failure-to-evict.
    """

    def test_non_db_exception_in_delete_propagates(self, conn, monkeypatch):
        """A non-``sqlite3.Error`` raised by the delete call must NOT be
        swallowed by ``_try_delete_session`` — it must propagate."""
        from mediaman.web.auth import session_store

        token = create_session(conn, "alice")

        def boom(_conn, _token_hash):
            # A TypeError stands in for any non-DB bug (bad import, etc.)
            # on the eviction path. Pre-fix this was swallowed; post-fix
            # it must escape.
            raise TypeError("simulated programming error on the eviction path")

        monkeypatch.setattr(session_store, "_delete_session_with_commit", boom)

        with pytest.raises(TypeError, match="simulated programming error"):
            session_store._try_delete_session(conn, _hash_token(token), reason="idle_expired")

    def test_sqlite_error_in_delete_is_still_swallowed(self, conn, monkeypatch, caplog):
        """The narrowing must not regress the intended behaviour: a
        genuine ``sqlite3.Error`` (lock contention) is still caught and
        logged, never bubbled into ``validate_session``'s caller."""
        from mediaman.web.auth import session_store

        token = create_session(conn, "alice")

        def boom(_conn, _token_hash):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(session_store, "_delete_session_with_commit", boom)

        with caplog.at_level(logging.WARNING, logger="mediaman.web.auth.session_store"):
            # Must NOT raise — a transient DB failure on the best-effort
            # eviction write is logged and retried next request.
            session_store._try_delete_session(
                conn, _hash_token(token), reason="fingerprint_mismatch"
            )

        visible = [
            r
            for r in caplog.records
            if r.name == "mediaman.web.auth.session_store" and r.levelno >= logging.WARNING
        ]
        assert visible, "a swallowed sqlite3.Error on the eviction path must still log"


# ---------------------------------------------------------------------------
# Audit: monotonic cleanup throttle uses post-cleanup timestamp
# ---------------------------------------------------------------------------


class TestCleanupThrottleStampedAfterCompletion:
    """Audit: the ``finally`` block stamped ``_last_cleanup_at`` with
    ``mono`` — the value captured at function entry — instead of the
    moment the cleanup actually finished.  A slow sweep would let the
    next request fire another sweep almost immediately.
    """

    def test_last_cleanup_at_is_post_cleanup(self, conn, monkeypatch):
        from mediaman.web.auth import session_store

        # Reset the module-global throttle so the test starts in a
        # known state.
        monkeypatch.setattr(session_store, "_last_cleanup_at", 0.0)

        # Use a fake monotonic clock that we advance explicitly inside
        # slow_cleanup, so we never sleep for real.
        # All callers — both test code and session_store production code —
        # go through session_store.time.monotonic (via the patched binding).
        fake_clock = [1000.0]

        def fake_monotonic() -> float:
            return fake_clock[0]

        monkeypatch.setattr(session_store.time, "monotonic", fake_monotonic)

        # Pretend the cleanup itself takes a measurable sliver of time so
        # we can distinguish "stamped at entry" from "stamped at exit".
        original_cleanup = session_store._cleanup_expired_with_commit

        def slow_cleanup(*args, **kwargs):
            original_cleanup(*args, **kwargs)
            fake_clock[0] += 0.1  # advance the fake clock by 100 ms — no real sleep

        monkeypatch.setattr(session_store, "_cleanup_expired_with_commit", slow_cleanup)

        token = create_session(conn, "alice")
        before = fake_monotonic()  # == 1000.0 (entry clock value)
        assert validate_session(conn, token) == "alice"
        after = fake_monotonic()  # == 1000.1 (clock advanced inside slow_cleanup)

        # The post-cleanup timestamp must lie at or after before + 0.05.
        # If the bug were still present, ``_last_cleanup_at`` would equal
        # the value of ``time.monotonic()`` BEFORE slow_cleanup ran, i.e.
        # 1000.0, not 1000.1.
        assert session_store._last_cleanup_at >= before + 0.04
        assert session_store._last_cleanup_at <= after + 0.001


# ---------------------------------------------------------------------------
# Audit: SESSION_TOKEN_RE — anchors are redundant under fullmatch
# ---------------------------------------------------------------------------


class TestSessionTokenRegex:
    def test_pattern_has_no_redundant_anchors(self):
        from mediaman.web.auth import session_store

        # The compiled regex MUST NOT carry ``^...$`` anchors —
        # ``fullmatch`` already anchors implicitly.  This is cosmetic
        # but the audit flagged it as wasted work on every request.
        assert session_store._SESSION_TOKEN_RE.pattern == r"[0-9a-f]{64}"

    def test_fullmatch_still_rejects_extra_chars(self):
        from mediaman.web.auth.session_store import _SESSION_TOKEN_RE

        # A 65th character must be rejected by ``fullmatch`` — the
        # same as if anchors were present.
        assert _SESSION_TOKEN_RE.fullmatch("a" * 65) is None
        assert _SESSION_TOKEN_RE.fullmatch(" " + "a" * 64) is None
        assert _SESSION_TOKEN_RE.fullmatch("a" * 64) is not None


# ---------------------------------------------------------------------------
# Audit: list_sessions_for builds SessionMetadata explicitly
# ---------------------------------------------------------------------------


class TestListSessionsExplicitConstruction:
    def test_returned_dicts_have_exact_keys(self, conn):
        """A future column-type drift must surface as a missing-key
        construction error rather than being papered over by ``cast()``.
        """
        create_session(conn, "alice", client_ip="10.0.0.1")
        sessions = list_sessions_for(conn, "alice")
        assert len(sessions) == 1
        # Every documented SessionMetadata key must be present.
        meta = sessions[0]
        assert set(meta.keys()) == {
            "created_at",
            "expires_at",
            "last_used_at",
            "issued_ip",
            "fingerprint",
        }


# ---------------------------------------------------------------------------
# Audit: shared _hash_token module
# ---------------------------------------------------------------------------


class TestSharedHashTokenModule:
    """The canonical hash helper now lives in
    :mod:`mediaman.auth._token_hashing` so session_store and reauth
    share one implementation.
    """

    def test_session_store_and_reauth_use_same_helper(self):
        # rationale: verifying that two modules share the same hash_token
        # object requires importing the canonical implementation directly;
        # no public surface exposes this identity check.
        from mediaman.web.auth import reauth as reauth_mod
        from mediaman.web.auth import session_store as session_mod
        from mediaman.web.auth._token_hashing import hash_token

        # All three names point at the same callable — no second
        # definition can drift independently.
        assert session_mod._hash_token is hash_token
        assert reauth_mod._hash_token is hash_token

    def test_shared_helper_matches_sha256_hex(self):
        # rationale: verifying the algorithm (SHA-256) requires direct access
        # to the private hashing helper; public session creation hides the hash.
        from mediaman.web.auth._token_hashing import hash_token

        assert hash_token("token-1") == hashlib.sha256(b"token-1").hexdigest()


# ---------------------------------------------------------------------------
# Session fingerprint binding
# ---------------------------------------------------------------------------


class TestSessionFingerprint:
    def _conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def test_fingerprint_rejects_different_client(self, tmp_path):
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session, validate_session

        conn = self._conn(tmp_path)
        create_user(conn, "alice", "test-password-long-enough", enforce_policy=False)
        token = create_session(
            conn,
            "alice",
            user_agent="Mozilla/5.0 X",
            client_ip="192.0.2.1",
        )

        # Same UA, same /24 — OK.
        assert (
            validate_session(
                conn,
                token,
                user_agent="Mozilla/5.0 X",
                client_ip="192.0.2.99",
            )
            == "alice"
        )

        # Different UA — cookie theft → reject.
        assert (
            validate_session(
                conn,
                token,
                user_agent="Chrome attacker",
                client_ip="192.0.2.1",
            )
            is None
        )

    def test_unbound_session_works_without_fingerprint(self, tmp_path):
        """Sessions created with no UA/IP (CLI, tests, legacy) are unbound."""
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session, validate_session

        conn = self._conn(tmp_path)
        create_user(conn, "bob", "test-password-long-enough", enforce_policy=False)
        token = create_session(conn, "bob")

        # No fingerprint stored → any client succeeds.
        assert (
            validate_session(
                conn,
                token,
                user_agent="anything",
                client_ip="1.2.3.4",
            )
            == "bob"
        )

    def test_token_stored_as_hash(self, tmp_path):
        """Raw token must not be the primary key — token_hash is stored."""
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        conn = self._conn(tmp_path)
        create_user(conn, "carol", "test-password-long-enough", enforce_policy=False)
        create_session(conn, "carol", user_agent="UA", client_ip="1.1.1.1")

        row = conn.execute(
            "SELECT token_hash, fingerprint, issued_ip FROM admin_sessions"
        ).fetchone()
        # token_hash is populated
        assert row["token_hash"] is not None
        assert len(row["token_hash"]) == 64
        # fingerprint captured
        assert row["fingerprint"]
        # issued_ip captured
        assert row["issued_ip"] == "1.1.1.1"


class TestSessionIdleTimeout:
    def test_idle_session_expires(self, tmp_path):
        from datetime import UTC, datetime, timedelta

        from mediaman.db import init_db
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session, validate_session

        conn = init_db(str(tmp_path / "mm.db"))
        create_user(conn, "dan", "test-password-long-enough", enforce_policy=False)
        token = create_session(conn, "dan")

        # Poke last_used_at into the past (25 h ago — beyond idle window).
        past = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        conn.execute("UPDATE admin_sessions SET last_used_at = ?", (past,))
        conn.commit()

        # Should now be rejected.
        assert validate_session(conn, token) is None
