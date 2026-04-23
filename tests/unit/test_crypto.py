"""Tests for encryption and token signing."""

import base64
import hashlib
import secrets
import sqlite3
import time

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mediaman.crypto import (
    _derive_aes_key_hkdf,
    _load_or_create_salt,
    canary_check,
    decrypt_value,
    encrypt_value,
    generate_keep_token,
    generate_session_token,
    validate_keep_token,
)
from mediaman.db import init_db


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


class TestAesEncryption:
    def test_encrypt_decrypt_roundtrip(self, secret_key, conn):
        plaintext = "my-api-key-12345"
        encrypted = encrypt_value(plaintext, secret_key, conn=conn)
        assert encrypted != plaintext
        decrypted = decrypt_value(encrypted, secret_key, conn=conn)
        assert decrypted == plaintext

    def test_different_ciphertexts_for_same_input(self, secret_key, conn):
        plaintext = "same-input"
        a = encrypt_value(plaintext, secret_key, conn=conn)
        b = encrypt_value(plaintext, secret_key, conn=conn)
        assert a != b  # random nonce each time

    def test_wrong_key_fails(self, secret_key, conn):
        encrypted = encrypt_value("secret", secret_key, conn=conn)
        with pytest.raises(Exception):
            decrypt_value(encrypted, "wrong-key", conn=conn)

    def test_encrypt_requires_salt_source(self, secret_key):
        """Without conn or salt, encryption must refuse — no silent fallback."""
        with pytest.raises(ValueError):
            encrypt_value("secret", secret_key)

    def test_roundtrip_with_explicit_salt(self, secret_key):
        """Callers can pass ``salt=`` directly without a DB connection."""
        salt = secrets.token_bytes(16)
        encrypted = encrypt_value("hello", secret_key, salt=salt)
        assert decrypt_value(encrypted, secret_key, salt=salt) == "hello"


class TestHkdfKeyDerivation:
    def test_different_salts_produce_different_keys(self, secret_key):
        salt_a = b"\x01" * 16
        salt_b = b"\x02" * 16
        key_a = _derive_aes_key_hkdf(secret_key, salt_a)
        key_b = _derive_aes_key_hkdf(secret_key, salt_b)
        assert key_a != key_b

    def test_different_secrets_produce_different_keys(self):
        salt = b"\xAB" * 16
        key_a = _derive_aes_key_hkdf("secret-a", salt)
        key_b = _derive_aes_key_hkdf("secret-b", salt)
        assert key_a != key_b

    def test_same_secret_and_salt_produce_same_key(self, secret_key):
        salt = b"\xCD" * 16
        assert _derive_aes_key_hkdf(secret_key, salt) == _derive_aes_key_hkdf(secret_key, salt)

    def test_key_is_32_bytes(self, secret_key):
        salt = b"\x00" * 16
        assert len(_derive_aes_key_hkdf(secret_key, salt)) == 32


class TestSaltPersistence:
    def test_creates_salt_on_first_call(self, conn):
        salt = _load_or_create_salt(conn)
        assert len(salt) == 16
        # Persisted to DB
        row = conn.execute(
            "SELECT value FROM settings WHERE key='aes_kdf_salt'"
        ).fetchone()
        assert row is not None
        assert base64.b64decode(row["value"]) == salt

    def test_returns_existing_salt(self, conn):
        first = _load_or_create_salt(conn)
        second = _load_or_create_salt(conn)
        assert first == second


class TestLegacyV1Fallback:
    """Verify pre-HKDF ciphertexts (legacy v1) still decrypt."""

    @staticmethod
    def _v1_encrypt(plaintext: str, secret_key: str) -> str:
        """Recreate the legacy v1 encryption used before HKDF migration."""
        key = hashlib.sha256(secret_key.encode()).digest()
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(12)
        ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
        return base64.urlsafe_b64encode(nonce + ct).decode()

    def test_v1_ciphertext_decrypts_via_legacy_path(self, secret_key, conn):
        """v1 rows written before the migration still decrypt."""
        legacy = self._v1_encrypt("legacy-value", secret_key)
        assert decrypt_value(legacy, secret_key, conn=conn) == "legacy-value"

    def test_v1_decrypts_without_conn(self, secret_key):
        """Backwards compat: decrypting v1 ciphertext without conn/salt works."""
        legacy = self._v1_encrypt("legacy-value", secret_key)
        assert decrypt_value(legacy, secret_key) == "legacy-value"

    def test_v1_wrong_key_fails(self, secret_key):
        legacy = self._v1_encrypt("legacy-value", secret_key)
        with pytest.raises(InvalidTag):
            decrypt_value(legacy, "wrong-key")


class TestCanary:
    def test_seeds_canary_on_first_run(self, conn, secret_key):
        assert canary_check(conn, secret_key) is True
        row = conn.execute(
            "SELECT value, encrypted FROM settings WHERE key='aes_kdf_canary'"
        ).fetchone()
        assert row is not None
        assert row["encrypted"] == 1

    def test_passes_on_subsequent_runs_with_same_key(self, conn, secret_key):
        assert canary_check(conn, secret_key) is True
        # Second run reads the seeded canary.
        assert canary_check(conn, secret_key) is True

    def test_returns_false_on_key_mismatch(self, conn, secret_key, caplog):
        canary_check(conn, secret_key)  # seed
        with caplog.at_level("WARNING", logger="mediaman"):
            ok = canary_check(conn, "different-secret-32-chars-YYYYYY")
        assert ok is False
        assert any("AES key mismatch" in rec.message for rec in caplog.records)


class TestKeepTokens:
    def test_generate_and_validate(self, secret_key):
        token = generate_keep_token(
            media_item_id="12345",
            action_id=42,
            expires_at=int(time.time()) + 3600,
            secret_key=secret_key,
        )
        payload = validate_keep_token(token, secret_key)
        assert payload["media_item_id"] == "12345"
        assert payload["action_id"] == 42

    def test_expired_token_rejected(self, secret_key):
        token = generate_keep_token(
            media_item_id="12345",
            action_id=42,
            expires_at=int(time.time()) - 1,
            secret_key=secret_key,
        )
        assert validate_keep_token(token, secret_key) is None

    def test_tampered_token_rejected(self, secret_key):
        token = generate_keep_token(
            media_item_id="12345",
            action_id=42,
            expires_at=int(time.time()) + 3600,
            secret_key=secret_key,
        )
        tampered = token[:-4] + "XXXX"
        assert validate_keep_token(tampered, secret_key) is None

    def test_wrong_key_rejected(self, secret_key):
        token = generate_keep_token(
            media_item_id="12345",
            action_id=42,
            expires_at=int(time.time()) + 3600,
            secret_key=secret_key,
        )
        assert validate_keep_token(token, "wrong-key") is None


class TestSessionToken:
    def test_generates_hex_string(self):
        token = generate_session_token()
        assert len(token) == 64  # 32 bytes = 64 hex chars
        int(token, 16)  # must be valid hex

    def test_unique_each_call(self):
        a = generate_session_token()
        b = generate_session_token()
        assert a != b


class TestCanaryNoReseedOnTamper:
    """C26: canary_check must NOT re-seed when other encrypted rows
    exist but the canary row is missing. Previously it re-seeded,
    self-erasing the tamper signal after one run."""

    def test_missing_canary_with_encrypted_rows_returns_false(self, conn, secret_key):
        # Seed a non-canary encrypted setting.
        from mediaman.crypto import encrypt_value
        ct = encrypt_value("api-key-value", secret_key, conn=conn)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('plex_token', ?, 1, '2026-01-01')",
            (ct,),
        )
        conn.commit()

        # No canary row exists yet. canary_check must refuse to seed
        # and must return False.
        ok = canary_check(conn, secret_key)
        assert ok is False

        # Canary row must NOT have been created.
        row = conn.execute(
            "SELECT 1 FROM settings WHERE key='aes_kdf_canary'"
        ).fetchone()
        assert row is None

    def test_tamper_signal_persists_across_runs(self, conn, secret_key):
        """Second run must also report False — no silent self-heal."""
        from mediaman.crypto import encrypt_value
        ct = encrypt_value("api-key-value", secret_key, conn=conn)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('plex_token', ?, 1, '2026-01-01')",
            (ct,),
        )
        conn.commit()

        assert canary_check(conn, secret_key) is False
        assert canary_check(conn, secret_key) is False

    def test_genuine_first_run_still_seeds(self, conn, secret_key):
        """No encrypted rows at all → clean first-run → seed + True."""
        assert canary_check(conn, secret_key) is True
        row = conn.execute(
            "SELECT value FROM settings WHERE key='aes_kdf_canary'"
        ).fetchone()
        assert row is not None


class TestV2V1PlausibilityGate:
    """C33: fallback from v2 → v1 must check v1 structural shape
    first — don't burn a second AES round on bytes that clearly
    aren't v1."""

    def test_junk_bytes_do_not_fall_back_to_v1(self, secret_key, conn):
        """Short garbage input must fail fast as InvalidTag, not as a
        second AES attempt. We can't directly observe CPU burn but we
        can at least confirm the error surfaces and the input is
        rejected cleanly."""
        # 10 random bytes, base64-encoded — far too short for v1 (needs
        # ≥ 12 nonce + 16 tag = 28) and doesn't start with the v2 prefix.
        junk = base64.urlsafe_b64encode(b"\x03" * 10).decode()
        with pytest.raises(InvalidTag):
            decrypt_value(junk, secret_key, conn=conn)

    def test_v2_prefixed_failure_does_not_attempt_v1(self, secret_key, conn):
        """A valid-length v2 payload with the right prefix byte but a
        wrong tag should NOT fall back to v1 — it's clearly a v2
        ciphertext that failed authentication, not a v1 coincidence."""
        # Fabricate a fake v2 payload whose tag will fail.
        fake = b"\x02" + b"\x00" * 12 + b"\x00" * 32
        encoded = base64.urlsafe_b64encode(fake).decode()
        with pytest.raises(InvalidTag):
            decrypt_value(encoded, secret_key, conn=conn)

    def test_legit_v1_ciphertext_still_decrypts(self, secret_key, conn):
        """Regression: valid v1 bytes must still round-trip — the
        plausibility gate only blocks bytes that can't structurally be
        v1 or that started life as v2."""
        # Synthesise a v1 ciphertext: no prefix, 12-byte nonce, then
        # AES-GCM ciphertext.
        key = hashlib.sha256(secret_key.encode()).digest()
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(12)
        ct = aesgcm.encrypt(nonce, b"legacy", None)
        legacy = base64.urlsafe_b64encode(nonce + ct).decode()
        assert decrypt_value(legacy, secret_key, conn=conn) == "legacy"


class TestCiphertextCap:
    """H3: per-call ciphertext cap is enforced by decrypt_value."""

    def test_oversized_ciphertext_rejected(self, secret_key, conn):
        """Ciphertexts that exceed _MAX_CIPHERTEXT_LEN must be rejected."""
        import base64

        from mediaman.crypto import _MAX_CIPHERTEXT_LEN
        # Build a base64 string that decodes to more than _MAX_CIPHERTEXT_LEN bytes.
        raw_len = _MAX_CIPHERTEXT_LEN + 1
        oversize = base64.urlsafe_b64encode(b"A" * raw_len).decode()
        with pytest.raises(ValueError, match="exceeds max length"):
            decrypt_value(oversize, secret_key, conn=conn)

    def test_ciphertext_at_exact_cap_is_rejected(self, secret_key, conn):
        """A base64 string that decodes to exactly _MAX_CIPHERTEXT_LEN + 1 bytes fails."""
        from mediaman.crypto import _MAX_CIPHERTEXT_LEN
        # The cap is checked on the base64 *string* length, not the raw bytes.
        # Build a string whose length is _MAX_CIPHERTEXT_LEN + 1.
        over = "A" * (_MAX_CIPHERTEXT_LEN + 1)
        with pytest.raises(ValueError, match="exceeds max length"):
            decrypt_value(over, secret_key, conn=conn)


class TestSaltCache:
    """H4: salt is cached per-DB-path, avoiding repeated DB reads."""

    def test_salt_cached_after_first_call(self, conn):
        """Subsequent calls to _load_or_create_salt return cached value without DB hit."""
        from mediaman.crypto import _db_path, _load_or_create_salt, _salt_cache
        first = _load_or_create_salt(conn)
        # Must be in cache now, keyed by DB file path.
        assert _db_path(conn) in _salt_cache
        second = _load_or_create_salt(conn)
        assert first == second

    def test_cache_invalidated_on_canary_key_mismatch(self, conn, secret_key):
        """canary_check returning False (key mismatch) must evict the cached salt."""
        from mediaman.crypto import _db_path, _load_or_create_salt, _salt_cache
        # Prime the cache.
        _load_or_create_salt(conn)
        assert _db_path(conn) in _salt_cache
        # Seed canary with correct key.
        canary_check(conn, secret_key)
        # Now fail with a wrong key — cache must be cleared.
        canary_check(conn, "wrong-key-32-chars-padding-xxxxx")
        assert _db_path(conn) not in _salt_cache


class TestValidateSignedNarrowedException:
    """C7 / C34: the bare-except in _validate_signed was replaced with
    a narrow tuple. Non-dict JSON must also be rejected up front."""

    def test_non_dict_payload_rejected(self):
        """A JSON-array or JSON-null payload must not slide through —
        even with the right signature, it's not a valid token shape."""
        from mediaman.crypto import _TOKEN_PURPOSE_KEEP, _encode_signed, _validate_signed
        key = "0123456789abcdef" * 4
        # Craft a payload that's a list, not a dict. _encode_signed
        # will still produce a valid signature over it.
        token = _encode_signed([1, 2, 3], key, _TOKEN_PURPOSE_KEEP)  # type: ignore[arg-type]
        assert _validate_signed(token, key, _TOKEN_PURPOSE_KEEP) is None

    def test_malformed_token_returns_none_not_exception(self):
        from mediaman.crypto import _TOKEN_PURPOSE_KEEP, _validate_signed
        key = "0123456789abcdef" * 4
        # Bad base64, bad JSON, no dot — all must degrade to None
        # via the narrowed except.
        assert _validate_signed("no-dot-here", key, _TOKEN_PURPOSE_KEEP) is None
        assert _validate_signed("not_base64!!.also_bad", key, _TOKEN_PURPOSE_KEEP) is None
