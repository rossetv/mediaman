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
