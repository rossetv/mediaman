"""AES-256-GCM encryption-at-rest for settings.

Provides symmetric encryption for storing secrets (API keys, tokens) in
the SQLite ``settings`` table. Key derivation uses HKDF-SHA256 with a
per-install random 16-byte salt; ciphertexts are v2-prefixed (``0x02``).

From 2026-04-18 the setting key name is passed as GCM AAD when
encrypting/decrypting rows of the ``settings`` table, binding each
ciphertext to the row it lives in.

**v1 ciphertext support was removed on 2026-05-04.** Legacy v1
ciphertexts (plain SHA-256 of the secret, no prefix byte, no AAD) are
no longer decryptable by :func:`decrypt_value`. Databases that contain
v1 rows must run migration v35 via :func:`migrate_legacy_ciphertexts`
before upgrading past this version. Migration v35 is called automatically
at startup by the crypto bootstrap step once the canary check passes.

v1 is the legacy SHA-256-derived format; v2 is the current HKDF-derived
format with version-prefix byte. Migration is one-way via
``migrate_legacy_ciphertexts``.
"""

from __future__ import annotations

import base64
import collections.abc
import logging
import secrets
import sqlite3

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mediaman.core.time import now_iso as _now_iso

# Re-export everything from the key/salt submodule so that existing
# imports of these names from ``mediaman.crypto.aes`` keep working.
from ._aes_key import (  # noqa: F401
    _CANARY_PLAINTEXT,
    _CANARY_SETTING_KEY,
    _GCM_NONCE_LEN,
    _GCM_TAG_LEN,
    _HKDF_INFO,
    _MAX_CIPHERTEXT_LEN,
    _MIN_HEX_UNIQUE,
    _MIN_URLSAFE_UNIQUE,
    _SALT_SETTING_KEY,
    _V2_PREFIX,
    _db_path,
    _derive_aes_key_hkdf,
    _load_or_create_salt,
    _resolve_salt,
    _salt_cache,
    _salt_cache_get,
    _salt_cache_lock,
    _salt_cache_pop,
    _salt_cache_put,
    _secret_key_looks_strong,
)

# Re-export legacy-migration names so that existing imports from
# ``mediaman.crypto.aes`` keep working.
from ._aes_migrate import (  # noqa: F401
    LEGACY_V1_REMOVED_AT,
    _decrypt_v1_raw,
    migrate_legacy_ciphertexts,
)

logger = logging.getLogger(__name__)


def encrypt_value(
    plaintext: str,
    secret_key: str,
    *,
    conn: sqlite3.Connection | None = None,
    salt: bytes | None = None,
    aad: bytes | None = None,
) -> str:
    """Encrypt a string with AES-256-GCM using an HKDF-derived key.

    Raises:
        ValueError: If neither *conn* nor *salt* is provided — a salt source
            is required to derive the encryption key.
    """
    resolved_salt = _resolve_salt(conn, salt)
    if resolved_salt is None:
        raise ValueError("encrypt_value requires a salt source — pass conn=... or salt=...")
    key = _derive_aes_key_hkdf(secret_key, resolved_salt)
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(_GCM_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), aad)
    return base64.urlsafe_b64encode(_V2_PREFIX + nonce + ciphertext).decode()


def decrypt_value(
    encrypted: str,
    secret_key: str,
    *,
    conn: sqlite3.Connection | None = None,
    salt: bytes | None = None,
    aad: bytes | None = None,
) -> str:
    """Decrypt an AES-256-GCM v2 value produced by :func:`encrypt_value`.

    For v2 ciphertexts (prefixed ``0x02``), decryption is attempted first
    with the supplied *aad*. If that raises :class:`InvalidTag` and *aad*
    was supplied, a second attempt without AAD is made — this handles v2
    rows encrypted before AAD binding was introduced; migration v35
    (see :func:`migrate_legacy_ciphertexts`) re-encrypts those at startup
    so this fallback narrows to zero over time.

    v1 ciphertexts (no prefix byte, SHA-256-derived key) are no longer
    accepted. They must be migrated first via :func:`migrate_legacy_ciphertexts`.
    Presenting a v1 ciphertext raises :class:`InvalidTag`.

    Args:
        encrypted: Base64url-encoded ciphertext.
        secret_key: Master ``MEDIAMAN_SECRET_KEY``.
        conn: Optional SQLite connection — used to look up the per-install
            HKDF salt.
        salt: Optional explicit salt. Wins over ``conn``.
        aad: Optional Additional Authenticated Data — typically the
            settings row's key name.

    Raises:
        ValueError: If *encrypted* is empty or exceeds the maximum
            ciphertext length.
        InvalidTag: When the ciphertext fails authentication, or when a
            v1 ciphertext is presented (run migration v35 first).
    """
    if not encrypted:
        raise ValueError("decrypt_value: empty ciphertext")
    if len(encrypted) > _MAX_CIPHERTEXT_LEN:
        raise ValueError("decrypt_value: ciphertext exceeds max length")

    raw = base64.urlsafe_b64decode(encrypted)

    if len(raw) >= 1 + _GCM_NONCE_LEN + _GCM_TAG_LEN and raw[:1] == _V2_PREFIX:
        resolved_salt = _resolve_salt(conn, salt)
        if resolved_salt is not None:
            key = _derive_aes_key_hkdf(secret_key, resolved_salt)
            aesgcm = AESGCM(key)
            nonce = raw[1 : 1 + _GCM_NONCE_LEN]
            ciphertext = raw[1 + _GCM_NONCE_LEN :]
            if aad is not None:
                try:
                    return aesgcm.decrypt(nonce, ciphertext, aad).decode()
                except InvalidTag:
                    # Fall through to no-AAD attempt for pre-AAD v2
                    # ciphertexts. Migration v35 re-encrypts these at
                    # startup so this path narrows to zero over time.
                    pass
            return aesgcm.decrypt(nonce, ciphertext, None).decode()

    # v1 ciphertexts and anything structurally invalid reach here.
    # Run migration v35 via migrate_legacy_ciphertexts() to convert v1 rows.
    raise InvalidTag()


def is_canary_valid(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    on_failure: collections.abc.Callable[[str], None] | None = None,
) -> bool:
    """Verify the AES key can decrypt a stored canary value.

    Args:
        conn:       Open SQLite connection.
        secret_key: Master ``MEDIAMAN_SECRET_KEY``.
        on_failure: Optional callback invoked with the failure-reason string
                    when the check fails.  The caller (typically
                    ``bootstrap_crypto`` in ``app_factory``) passes a closure
                    that writes a ``security_event`` audit row.  Keeping the
                    audit-write out of ``crypto/`` preserves the leaf-package
                    invariant (§2.2): ``crypto/`` must not import from
                    ``mediaman.audit``.

    Returns:
        ``True`` on success, ``False`` on any failure.
    """
    row = conn.execute("SELECT value FROM settings WHERE key=?", (_CANARY_SETTING_KEY,)).fetchone()

    if row is None or not row["value"]:
        other = conn.execute(
            "SELECT 1 FROM settings WHERE encrypted=1 AND key != ? LIMIT 1",
            (_CANARY_SETTING_KEY,),
        ).fetchone()
        if other is not None:
            logger.error(
                "AES canary row is missing but encrypted settings exist — "
                "possible database tampering. Refusing to re-seed the "
                "canary; investigate the DB before restarting."
            )
            _invoke_on_failure(on_failure, "canary_missing_with_encrypted_rows")
            return False

        ciphertext = encrypt_value(
            _CANARY_PLAINTEXT,
            secret_key,
            conn=conn,
            aad=_CANARY_SETTING_KEY.encode(),
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, encrypted, updated_at) "
            "VALUES (?, ?, 1, ?)",
            (_CANARY_SETTING_KEY, ciphertext, _now_iso()),
        )
        conn.commit()
        return True

    try:
        decrypted = decrypt_value(
            row["value"],
            secret_key,
            conn=conn,
            aad=_CANARY_SETTING_KEY.encode(),
        )
    except (InvalidTag, ValueError):
        logger.warning(
            "AES key mismatch — existing encrypted settings can no longer be "
            "decrypted. The secret key likely changed since the last run. "
            "Rotate all encrypted settings (API keys, tokens) by re-entering "
            "them on the Settings page."
        )
        cache_key = _db_path(conn)
        if cache_key:
            _salt_cache_pop(cache_key)
        _invoke_on_failure(on_failure, "canary_decrypt_invalid_tag")
        return False

    if decrypted != _CANARY_PLAINTEXT:
        logger.warning(
            "AES key mismatch — canary decrypted to unexpected value. "
            "Rotate all encrypted settings (API keys, tokens) by re-entering "
            "them on the Settings page."
        )
        cache_key = _db_path(conn)
        if cache_key:
            _salt_cache_pop(cache_key)
        _invoke_on_failure(on_failure, "canary_plaintext_mismatch")
        return False

    return True


def _invoke_on_failure(
    on_failure: collections.abc.Callable[[str], None] | None, reason: str
) -> None:
    """Safely invoke *on_failure* (the caller-supplied audit callback).

    Swallows all exceptions so a broken audit path never overrides the
    security verdict returned to the caller.
    """
    if on_failure is None:
        return
    try:
        on_failure(reason)
    except Exception:  # pragma: no cover — never break the canary check on audit failure
        logger.exception("canary on_failure callback raised reason=%s", reason)
