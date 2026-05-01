"""Tests for :mod:`mediaman.web.routes.download._tokens`.

The module maintains both an in-memory cache and a persistent SQLite
table of consumed download tokens so the same link cannot be replayed
even after a process restart or against a sibling worker. These tests
exercise the atomic consume semantics, the release path, the eviction
of expired entries, and the cross-restart / cross-worker durability.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from mediaman.db import init_db, set_connection
from mediaman.web.routes.download._tokens import (
    _USED_TOKENS,
    _USED_TOKENS_LOCK,
    _mark_token_used,
    _unmark_token_used,
)


def _clear_used_tokens():
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.clear()


class TestMarkTokenUsed:
    def setup_method(self):
        _clear_used_tokens()

    def test_first_use_returns_true(self):
        """A fresh token is accepted on first use."""
        exp = int(time.time()) + 3600
        assert _mark_token_used("brand-new-token", exp) is True

    def test_second_use_returns_false(self):
        """The same token is rejected on the second call."""
        exp = int(time.time()) + 3600
        _mark_token_used("replay-token", exp)
        assert _mark_token_used("replay-token", exp) is False

    def test_different_tokens_both_accepted(self):
        """Two distinct tokens do not interfere with each other."""
        exp = int(time.time()) + 3600
        assert _mark_token_used("token-alpha", exp) is True
        assert _mark_token_used("token-beta", exp) is True

    def test_uses_sha256_digest_not_raw_token(self):
        """The raw token value is never stored — only the SHA-256 digest."""
        import hashlib

        exp = int(time.time()) + 3600
        _mark_token_used("secret-token", exp)
        digest = hashlib.sha256("secret-token".encode()).hexdigest()
        with _USED_TOKENS_LOCK:
            assert digest in _USED_TOKENS
            assert "secret-token" not in _USED_TOKENS

    def test_stores_expiry_as_float(self):
        """The stored value for a token is the float representation of exp."""
        import hashlib

        exp = int(time.time()) + 3600
        _mark_token_used("expiry-token", exp)
        digest = hashlib.sha256("expiry-token".encode()).hexdigest()
        with _USED_TOKENS_LOCK:
            assert _USED_TOKENS[digest] == float(exp)

    def test_evicts_expired_entries_when_over_1000(self):
        """When the cache exceeds 1000 entries, expired ones are pruned."""
        # Fill the store with 1001 expired tokens
        past_exp = int(time.time()) - 1  # already expired
        with _USED_TOKENS_LOCK:
            for i in range(1001):
                _USED_TOKENS[f"fake-digest-{i:04d}"] = float(past_exp)

        # Adding one more token must trigger eviction
        future_exp = int(time.time()) + 3600
        _mark_token_used("trigger-eviction", future_exp)

        # The store must now only contain non-expired entries (our new token)
        import hashlib

        digest = hashlib.sha256("trigger-eviction".encode()).hexdigest()
        with _USED_TOKENS_LOCK:
            assert digest in _USED_TOKENS
            # All the expired fakes should be gone
            for i in range(1001):
                assert f"fake-digest-{i:04d}" not in _USED_TOKENS


class TestUnmarkTokenUsed:
    def setup_method(self):
        _clear_used_tokens()

    def test_unmark_allows_retry(self):
        """After unmarking, the same token can be consumed again."""
        exp = int(time.time()) + 3600
        _mark_token_used("retry-token", exp)
        _unmark_token_used("retry-token")
        assert _mark_token_used("retry-token", exp) is True

    def test_unmark_nonexistent_token_is_safe(self):
        """Unmarking a token that was never marked must not raise."""
        _unmark_token_used("never-seen-before")  # must not raise

    def test_unmark_removes_digest_from_store(self):
        """The SHA-256 digest is removed from _USED_TOKENS after unmarking."""
        import hashlib

        exp = int(time.time()) + 3600
        _mark_token_used("vanish-token", exp)
        _unmark_token_used("vanish-token")
        digest = hashlib.sha256("vanish-token".encode()).hexdigest()
        with _USED_TOKENS_LOCK:
            assert digest not in _USED_TOKENS


# ---------------------------------------------------------------------------
# Finding 2: persistence to SQLite — token reuse must survive restarts
# and cross-worker collisions.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(str(tmp_path / "mm.db"))
    set_connection(conn)
    yield conn
    conn.close()


class TestPersistentClaim:
    def setup_method(self):
        _clear_used_tokens()

    def test_claim_writes_row_to_used_download_tokens(self, db_conn):
        """A successful claim populates the SQLite authoritative table."""
        exp = int(time.time()) + 3600
        assert _mark_token_used("persist-me", exp) is True

        digest = hashlib.sha256("persist-me".encode()).hexdigest()
        row = db_conn.execute(
            "SELECT token_hash, expires_at, used_at FROM used_download_tokens WHERE token_hash = ?",
            (digest,),
        ).fetchone()
        assert row is not None
        assert row["token_hash"] == digest
        assert row["expires_at"]
        assert row["used_at"]

    def test_replay_after_in_memory_clear_is_rejected(self, db_conn):
        """Simulates a process restart: claim, wipe in-memory, replay must fail."""
        exp = int(time.time()) + 3600
        assert _mark_token_used("replay-me", exp) is True
        # Wipe the in-memory cache as if the process had just restarted.
        _clear_used_tokens()
        assert _mark_token_used("replay-me", exp) is False

    def test_replay_against_sibling_worker_is_rejected(self, db_conn):
        """A sibling worker's pre-populated DB row blocks a claim from this worker."""
        from datetime import datetime, timezone

        exp = int(time.time()) + 3600
        digest = hashlib.sha256("sibling-token".encode()).hexdigest()
        db_conn.execute(
            "INSERT INTO used_download_tokens (token_hash, expires_at, used_at) VALUES (?, ?, ?)",
            (
                digest,
                datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db_conn.commit()
        # Cache is empty (this "worker" never saw the claim) yet the token
        # must still be refused — the DB constraint is authoritative.
        _clear_used_tokens()
        assert _mark_token_used("sibling-token", exp) is False

    def test_unmark_clears_persistent_row(self, db_conn):
        """Releasing a token deletes the SQLite row so a retry can succeed."""
        exp = int(time.time()) + 3600
        _mark_token_used("retry-after-failure", exp)
        _unmark_token_used("retry-after-failure")

        digest = hashlib.sha256("retry-after-failure".encode()).hexdigest()
        row = db_conn.execute(
            "SELECT token_hash FROM used_download_tokens WHERE token_hash = ?",
            (digest,),
        ).fetchone()
        assert row is None
        # And it can be re-used now.
        assert _mark_token_used("retry-after-failure", exp) is True

    def test_first_use_via_db_only_blocks_replay(self, db_conn):
        """Simulates: cache empty, DB already has the row. Must be rejected."""
        from datetime import datetime, timezone

        exp = int(time.time()) + 3600
        digest = hashlib.sha256("from-other-worker".encode()).hexdigest()
        db_conn.execute(
            "INSERT INTO used_download_tokens (token_hash, expires_at, used_at) VALUES (?, ?, ?)",
            (
                digest,
                datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db_conn.commit()
        _clear_used_tokens()
        # First call to mark — but the DB already has it.
        assert _mark_token_used("from-other-worker", exp) is False
