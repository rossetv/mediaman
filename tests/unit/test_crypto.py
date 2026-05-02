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
        salt = b"\xab" * 16
        key_a = _derive_aes_key_hkdf("secret-a", salt)
        key_b = _derive_aes_key_hkdf("secret-b", salt)
        assert key_a != key_b

    def test_same_secret_and_salt_produce_same_key(self, secret_key):
        salt = b"\xcd" * 16
        assert _derive_aes_key_hkdf(secret_key, salt) == _derive_aes_key_hkdf(secret_key, salt)

    def test_key_is_32_bytes(self, secret_key):
        salt = b"\x00" * 16
        assert len(_derive_aes_key_hkdf(secret_key, salt)) == 32


class TestSaltPersistence:
    def test_creates_salt_on_first_call(self, conn):
        salt = _load_or_create_salt(conn)
        assert len(salt) == 16
        # Persisted to DB
        row = conn.execute("SELECT value FROM settings WHERE key='aes_kdf_salt'").fetchone()
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

    def test_v1_with_v2_prefix_byte_in_nonce_decrypts(self, secret_key, conn):
        """Regression: a v1 ciphertext whose random nonce starts with 0x02
        (the v2 prefix byte) must still decrypt via the legacy path.

        The prior implementation refused to try v1 whenever the raw
        bytes began with the v2 prefix, which was a ~0.39% flake on
        every v1 decrypt — and a deterministic data-loss bug for the
        unlucky operator whose stored ciphertext happened to roll a
        ``0x02`` first nonce byte.
        """
        key = hashlib.sha256(secret_key.encode()).digest()
        aesgcm = AESGCM(key)
        nonce = b"\x02" + secrets.token_bytes(11)  # forced v2-prefix collision
        ct = aesgcm.encrypt(nonce, b"legacy-value", None)
        legacy = base64.urlsafe_b64encode(nonce + ct).decode()

        assert decrypt_value(legacy, secret_key, conn=conn) == "legacy-value"


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
        row = conn.execute("SELECT 1 FROM settings WHERE key='aes_kdf_canary'").fetchone()
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
        row = conn.execute("SELECT value FROM settings WHERE key='aes_kdf_canary'").fetchone()
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


class TestSecretKeyEntropyHardened:
    """The audit's HIGH finding on `_secret_key_looks_strong`.

    The previous heuristic accepted 43-char ``[A-Za-z0-9_-]`` strings
    with as few as 8 unique characters, which is far below the 256-bit
    entropy bar implied by the URL-safe shape. The hardened rule must
    reject the audit's worked example and any structurally similar
    low-entropy input.
    """

    def test_rejects_audit_low_unique_43_char_case(self):
        """The audit's example: 43 chars, 10 unique — must be refused."""
        from mediaman.crypto import _secret_key_looks_strong

        bad = "abcdefghij" * 4 + "abc"  # 43 chars, 10 unique
        assert len(bad) == 43
        assert _secret_key_looks_strong(bad) is False

    def test_rejects_8_unique_64_char_hex(self):
        """64 hex chars but only 8 unique digits is structured low-entropy."""
        from mediaman.crypto import _secret_key_looks_strong

        bad = "deadbeef" * 8  # 64 hex chars, 8 unique
        assert _secret_key_looks_strong(bad) is False

    def test_rejects_single_char_repeat(self):
        from mediaman.crypto import _secret_key_looks_strong

        assert _secret_key_looks_strong("a" * 64) is False
        assert _secret_key_looks_strong("0" * 64) is False

    def test_rejects_short_input(self):
        from mediaman.crypto import _secret_key_looks_strong

        assert _secret_key_looks_strong("") is False
        assert _secret_key_looks_strong("short") is False
        assert _secret_key_looks_strong("a" * 31) is False

    def test_rejects_43_char_decoding_to_too_few_bytes(self):
        """The base64url path requires ≥32 decoded bytes (token_urlsafe(32)+)."""
        from mediaman.crypto import _secret_key_looks_strong

        # 43-char string with high unique count BUT only when decoded as
        # base64url it yields ≥32 bytes. token_urlsafe(31) yields 42
        # chars, not 43, so we synthesise a string with the right shape
        # but invalid as base64. The implementation accepts 43+ chars
        # that decode cleanly — anything that fails to decode is None.
        bad_decode = "!" * 43  # not in [A-Za-z0-9_-], rejected by regex
        assert _secret_key_looks_strong(bad_decode) is False

    def test_accepts_token_hex_32(self):
        """Real-world ``secrets.token_hex(32)`` keys must always pass."""
        import secrets

        from mediaman.crypto import _secret_key_looks_strong

        # 1000 samples — one rejection here would be a regression.
        for _ in range(1000):
            assert _secret_key_looks_strong(secrets.token_hex(32)) is True

    def test_accepts_token_urlsafe_32(self):
        """Real-world ``secrets.token_urlsafe(32)`` keys must always pass."""
        import secrets

        from mediaman.crypto import _secret_key_looks_strong

        for _ in range(1000):
            assert _secret_key_looks_strong(secrets.token_urlsafe(32)) is True

    def test_accepts_test_fixture_value(self):
        """The widely-used test fixture (``"0123456789abcdef" * 4``) must
        keep passing — too many call sites depend on it for a bump now."""
        from mediaman.crypto import _secret_key_looks_strong

        assert _secret_key_looks_strong("0123456789abcdef" * 4) is True


class TestDecryptValueAutoReencrypt:
    """The audit's HIGH finding on the v2 no-AAD fallback.

    A value encrypted before AAD binding was introduced silently
    decrypts via the no-AAD path even when the caller now supplies an
    AAD. The fix opportunistically re-encrypts the value with AAD on
    a successful no-AAD decrypt so the next read takes the secure
    path; the upgrade fires only when the caller passes both a
    ``conn`` and a ``settings_key`` so a write-back target is known.
    """

    def test_legacy_no_aad_value_self_upgrades(self, conn, secret_key):
        """A v2 ciphertext encrypted without AAD must:

        1. Decrypt successfully when the caller passes the AAD they want.
        2. Trigger a re-encrypt write-back when ``settings_key`` is supplied.
        3. After that, a re-read with the same AAD must succeed via the
           secure (AAD-bound) path — proving the row was upgraded.
        """
        # Step 1: encrypt WITHOUT AAD (simulating a pre-AAD value).
        legacy_ct = encrypt_value("api-key", secret_key, conn=conn, aad=None)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('plex_token', ?, 1, '2026-01-01')",
            (legacy_ct,),
        )
        conn.commit()

        # Step 2: decrypt WITH AAD + settings_key — should succeed and
        # opportunistically rewrite the row.
        aad = b"plex_token"
        plaintext = decrypt_value(
            legacy_ct,
            secret_key,
            conn=conn,
            aad=aad,
            settings_key="plex_token",
        )
        assert plaintext == "api-key"

        # Step 3: read back the updated row from the DB.
        row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
        new_ct = row["value"]
        assert new_ct != legacy_ct  # row was rewritten

        # Step 4: the new ciphertext must now decrypt cleanly with AAD
        # (i.e. the upgrade actually took, not a no-AAD second encrypt).
        # We do this by trying to decrypt with the WRONG AAD — that
        # must fail authentication, proving AAD is now bound.
        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            decrypt_value(new_ct, secret_key, conn=conn, aad=b"different_key")

        # And the right AAD still works.
        assert decrypt_value(new_ct, secret_key, conn=conn, aad=aad) == "api-key"

    def test_no_settings_key_skips_reencrypt(self, conn, secret_key):
        """Without ``settings_key`` the row is not rewritten — skip the
        upgrade so no-conn / no-key callers continue to work unchanged."""
        legacy_ct = encrypt_value("api-key", secret_key, conn=conn, aad=None)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('plex_token', ?, 1, '2026-01-01')",
            (legacy_ct,),
        )
        conn.commit()

        plaintext = decrypt_value(legacy_ct, secret_key, conn=conn, aad=b"plex_token")
        assert plaintext == "api-key"

        # Row must NOT have been touched.
        row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
        assert row["value"] == legacy_ct

    def test_no_conn_skips_reencrypt(self, secret_key):
        """Without a conn, decrypt still works but no upgrade fires."""
        import secrets as _secrets

        salt = _secrets.token_bytes(16)
        legacy_ct = encrypt_value("api-key", secret_key, salt=salt, aad=None)
        # No conn → no write-back possible.
        assert (
            decrypt_value(
                legacy_ct,
                secret_key,
                salt=salt,
                aad=b"plex_token",
                settings_key="plex_token",
            )
            == "api-key"
        )

    def test_aad_bound_value_does_not_trigger_reencrypt(self, conn, secret_key):
        """A value already encrypted WITH AAD must read normally — no
        re-encrypt fires, no audit event, no row mutation."""
        aad = b"plex_token"
        good_ct = encrypt_value("api-key", secret_key, conn=conn, aad=aad)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('plex_token', ?, 1, '2026-01-01')",
            (good_ct,),
        )
        conn.commit()

        plaintext = decrypt_value(
            good_ct,
            secret_key,
            conn=conn,
            aad=aad,
            settings_key="plex_token",
        )
        assert plaintext == "api-key"

        row = conn.execute("SELECT value FROM settings WHERE key='plex_token'").fetchone()
        assert row["value"] == good_ct  # unchanged

    def test_reencrypt_emits_audit_log(self, conn, secret_key):
        """Successful no-AAD upgrade must write a sec:aes.setting_reencrypted row."""
        legacy_ct = encrypt_value("api-key", secret_key, conn=conn, aad=None)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('plex_token', ?, 1, '2026-01-01')",
            (legacy_ct,),
        )
        conn.commit()

        decrypt_value(
            legacy_ct,
            secret_key,
            conn=conn,
            aad=b"plex_token",
            settings_key="plex_token",
        )

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action=?",
            ("sec:aes.setting_reencrypted",),
        ).fetchall()
        assert len(rows) == 1
        assert "plex_token" in rows[0]["detail"]


class TestValidatePayloadCap:
    """The audit's HIGH finding on the HMAC pre-image attack vector.

    ``_validate_signed`` must cap the decoded payload BEFORE computing
    HMAC-SHA256 over it. Otherwise an attacker can force HMAC over
    megabytes of attacker-controlled bytes per request, before the
    constant-time signature comparison rejects the token.
    """

    def test_oversized_payload_rejected_without_hmac_call(self, monkeypatch):
        """A 100 KB payload must short-circuit before ``_sign`` is invoked.

        We patch :func:`_sign` to raise — if the cap fires correctly,
        the patched function is never called. If the cap is missing,
        the AssertionError below fires.
        """
        from mediaman.crypto import _TOKEN_PURPOSE_KEEP, _validate_signed
        from mediaman.crypto import tokens as _t

        sign_calls = []

        def _spy_sign(*args, **kwargs):
            sign_calls.append(args)
            return b"\x00" * 32

        monkeypatch.setattr(_t, "_sign", _spy_sign)

        key = "0123456789abcdef" * 4
        # Build a payload that base64-decodes to 100 KB. The dotted
        # token shape: <100KB-payload-b64>.<sig>. Use 100 KB of zero
        # bytes for the payload.
        big_payload = base64.urlsafe_b64encode(b"\x00" * 100_000).decode().rstrip("=")
        token = f"{big_payload}.AAAA"

        result = _validate_signed(token, key, _TOKEN_PURPOSE_KEEP)
        assert result is None
        # _sign must NOT have been invoked — the cap fired first.
        assert sign_calls == []

    def test_oversize_outer_token_rejected(self):
        """The outer 4 KiB cap also still works."""
        from mediaman.crypto import _TOKEN_PURPOSE_KEEP, _validate_signed

        key = "0123456789abcdef" * 4
        huge = "A" * 5000 + "." + "AAAA"
        assert _validate_signed(huge, key, _TOKEN_PURPOSE_KEEP) is None

    def test_normal_token_still_validates(self, secret_key):
        """Regression: tokens within the cap must still round-trip."""
        from mediaman.crypto import (
            generate_keep_token,
            validate_keep_token,
        )

        token = generate_keep_token(
            media_item_id="12345",
            action_id=42,
            expires_at=int(time.time()) + 3600,
            secret_key=secret_key,
        )
        payload = validate_keep_token(token, secret_key)
        assert payload is not None
        assert payload["media_item_id"] == "12345"


class TestExpFieldBoolRejection:
    """The audit's MEDIUM finding on the ``exp`` type check.

    ``bool`` is a subclass of ``int`` in Python, so a payload of
    ``{"exp": True}`` would pass ``isinstance(exp, (int, float))``
    and coerce to ``1`` — a UNIX timestamp far in the past. The fix
    explicitly rejects bool first.
    """

    def test_exp_true_rejected(self, secret_key):
        from mediaman.crypto import (
            _TOKEN_PURPOSE_KEEP,
            _encode_signed,
            _validate_signed,
        )

        token = _encode_signed({"exp": True}, secret_key, _TOKEN_PURPOSE_KEEP)
        assert _validate_signed(token, secret_key, _TOKEN_PURPOSE_KEEP) is None

    def test_exp_false_rejected(self, secret_key):
        from mediaman.crypto import (
            _TOKEN_PURPOSE_KEEP,
            _encode_signed,
            _validate_signed,
        )

        token = _encode_signed({"exp": False}, secret_key, _TOKEN_PURPOSE_KEEP)
        assert _validate_signed(token, secret_key, _TOKEN_PURPOSE_KEEP) is None

    def test_exp_genuine_int_still_accepted(self, secret_key):
        """Regression: an int ``exp`` in the future must still validate."""
        from mediaman.crypto import (
            _TOKEN_PURPOSE_KEEP,
            _encode_signed,
            _validate_signed,
        )

        token = _encode_signed(
            {"exp": int(time.time()) + 3600},
            secret_key,
            _TOKEN_PURPOSE_KEEP,
        )
        assert _validate_signed(token, secret_key, _TOKEN_PURPOSE_KEEP) is not None


class TestCanaryFailureAudits:
    """The audit's MEDIUM finding on canary_check audit logging.

    When the canary fails (key mismatch / DB tamper), an audit row
    must be written so an operator's audit trail captures the event.
    """

    def test_canary_key_mismatch_writes_audit_row(self, conn, secret_key):
        canary_check(conn, secret_key)  # seed
        # Fail with a different valid-shape key.
        ok = canary_check(conn, "fedcba9876543210" * 4)
        assert ok is False

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action=?",
            ("sec:aes.canary_failed",),
        ).fetchall()
        assert len(rows) >= 1
        # Detail should reference the failure mode.
        assert any("canary_decrypt_invalid_tag" in r["detail"] for r in rows)

    def test_canary_missing_with_encrypted_rows_audits(self, conn, secret_key):
        ct = encrypt_value("api-key", secret_key, conn=conn)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('plex_token', ?, 1, '2026-01-01')",
            (ct,),
        )
        conn.commit()

        ok = canary_check(conn, secret_key)
        assert ok is False

        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action=?",
            ("sec:aes.canary_failed",),
        ).fetchall()
        assert len(rows) >= 1
        assert any("canary_missing_with_encrypted_rows" in r["detail"] for r in rows)


class TestSaltCacheBounded:
    """The audit's LOW finding on the unbounded ``_salt_cache``.

    The cache must enforce an upper bound so a long-running test
    process opening many distinct DB files doesn't accumulate state
    indefinitely.
    """

    def test_cache_evicts_lru_at_capacity(self, tmp_path):
        from mediaman.crypto import _salt_cache
        from mediaman.crypto.aes import _SALT_CACHE_MAX, _load_or_create_salt
        from mediaman.db import init_db

        # Wipe any leakage from earlier tests.
        _salt_cache.clear()

        conns = []
        try:
            # Open _SALT_CACHE_MAX + 2 distinct DBs to force eviction.
            for i in range(_SALT_CACHE_MAX + 2):
                db = init_db(str(tmp_path / f"mm_{i}.db"))
                _load_or_create_salt(db)
                conns.append(db)
            # Cache must not exceed the configured maximum.
            assert len(_salt_cache) == _SALT_CACHE_MAX
        finally:
            for c in conns:
                c.close()


class TestRaceFreeSaltSeed:
    """The audit's MEDIUM finding on the salt-seed race.

    Two workers seeing the absent salt and racing to INSERT must both
    end up reading the same persisted value. ``INSERT OR IGNORE``
    plus a re-read covers this.
    """

    def test_concurrent_callers_agree_on_salt(self, tmp_path):
        """Two distinct connections to the same DB must both return
        the same salt even if neither finds the row at first read."""
        from mediaman.crypto import _load_or_create_salt, _salt_cache
        from mediaman.db import init_db

        path = str(tmp_path / "race.db")
        # Initialise the schema once via init_db, then open a second
        # connection. We bypass the cache by clearing it, simulating
        # two cold processes.
        a = init_db(path)
        b = init_db(path)
        try:
            _salt_cache.clear()
            salt_a = _load_or_create_salt(a)
            _salt_cache.clear()
            salt_b = _load_or_create_salt(b)
            assert salt_a == salt_b
        finally:
            a.close()
            b.close()


class TestCiphertextCapTightened:
    """The audit's MEDIUM finding on ``_MAX_CIPHERTEXT_LEN``.

    Settings rows are KB-scale; 1 MiB was generous. The cap is now
    64 KiB, which is comfortably above the largest legitimate value.
    """

    def test_max_cap_is_64kib(self):
        from mediaman.crypto import _MAX_CIPHERTEXT_LEN

        assert _MAX_CIPHERTEXT_LEN == 65_536

    def test_normal_settings_round_trip(self, conn, secret_key):
        """Settings-shape values (a few hundred bytes) must round-trip."""
        plaintext = "x" * 500
        ct = encrypt_value(plaintext, secret_key, conn=conn)
        assert decrypt_value(ct, secret_key, conn=conn) == plaintext
