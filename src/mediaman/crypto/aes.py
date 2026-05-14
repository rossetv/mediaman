"""AES-256-GCM encryption-at-rest for settings.

Provides symmetric encryption for storing secrets (API keys, tokens) in
the SQLite ``settings`` table. Key derivation uses HKDF-SHA256 with a
per-install random 16-byte salt; ciphertexts are v2-prefixed (``0x02``).
The setting key name is passed as GCM AAD on every encrypt/decrypt so
each ciphertext is bound to the row it lives in — moving a ciphertext
between rows fails authentication.
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

from ._aes_key import (
    _CANARY_PLAINTEXT,
    _CANARY_SETTING_KEY,
    _GCM_NONCE_LEN,
    _GCM_TAG_LEN,
    _MAX_CIPHERTEXT_LEN,
    _V2_PREFIX,
    CryptoInputError,
    _db_path,
    _derive_aes_key_hkdf,
    _resolve_salt,
    _salt_cache_pop,
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

    Args:
        encrypted: Base64url-encoded ciphertext.
        secret_key: Master ``MEDIAMAN_SECRET_KEY``.
        conn: Optional SQLite connection — used to look up the per-install
            HKDF salt.
        salt: Optional explicit salt. Wins over ``conn``.
        aad: Optional Additional Authenticated Data — typically the
            settings row's key name.

    Raises:
        CryptoInputError: If *encrypted* is empty or exceeds the maximum
            ciphertext length.
        InvalidTag: When the ciphertext fails authentication.
    """
    if not encrypted:
        raise CryptoInputError("decrypt_value: empty ciphertext")
    if len(encrypted) > _MAX_CIPHERTEXT_LEN:
        raise CryptoInputError("decrypt_value: ciphertext exceeds max length")

    raw = base64.urlsafe_b64decode(encrypted)

    if len(raw) >= 1 + _GCM_NONCE_LEN + _GCM_TAG_LEN and raw[:1] == _V2_PREFIX:
        resolved_salt = _resolve_salt(conn, salt)
        if resolved_salt is not None:
            key = _derive_aes_key_hkdf(secret_key, resolved_salt)
            aesgcm = AESGCM(key)
            nonce = raw[1 : 1 + _GCM_NONCE_LEN]
            ciphertext = raw[1 + _GCM_NONCE_LEN :]
            return aesgcm.decrypt(nonce, ciphertext, aad).decode()

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
                    ``mediaman.core.audit``.

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
        if _upgrade_legacy_canary(conn, row["value"], secret_key):
            return True
        return _fail_canary(
            conn,
            on_failure,
            "canary_decrypt_invalid_tag",
            "AES key mismatch — existing encrypted settings can no longer be "
            "decrypted. The secret key likely changed since the last run. "
            "Rotate all encrypted settings (API keys, tokens) by re-entering "
            "them on the Settings page.",
        )

    if decrypted != _CANARY_PLAINTEXT:
        return _fail_canary(
            conn,
            on_failure,
            "canary_plaintext_mismatch",
            "AES key mismatch — canary decrypted to unexpected value. "
            "Rotate all encrypted settings (API keys, tokens) by re-entering "
            "them on the Settings page.",
        )

    return True


def _fail_canary(
    conn: sqlite3.Connection,
    on_failure: collections.abc.Callable[[str], None] | None,
    reason: str,
    message: str,
) -> bool:
    """Record a canary decrypt/compare failure and return ``False``.

    Logs *message* at WARNING, evicts the cached HKDF salt (so the next
    encrypt/decrypt re-reads it from the DB rather than trusting a salt
    that may belong to a swapped database), and fires the audit callback
    with *reason*. Always returns ``False`` so callers can
    ``return _fail_canary(...)``.
    """
    logger.warning(message)
    cache_key = _db_path(conn)
    if cache_key:
        _salt_cache_pop(cache_key)
    _invoke_on_failure(on_failure, reason)
    return False


def _upgrade_legacy_canary(
    conn: sqlite3.Connection, ciphertext: str, secret_key: str
) -> bool:
    """Heal a pre-AAD canary row in place, returning whether it healed.

    Installs created before AAD binding (2026-04-18) seeded the
    ``aes_kdf_canary`` row as a v2 ciphertext with no AAD. The
    legacy-ciphertext migration excluded the canary key, so once the
    no-AAD fallback was removed from :func:`decrypt_value` those rows
    stopped decrypting under the AAD-bound path.

    A no-AAD decrypt that yields the known canary plaintext proves the
    AES key itself is correct — only the ciphertext's AAD shape is stale.
    When that holds, the row is re-encrypted with AAD so the upgrade
    happens exactly once. Any other outcome (wrong key, corrupt row)
    returns ``False`` and the caller reports a genuine canary failure.
    """
    try:
        legacy = decrypt_value(ciphertext, secret_key, conn=conn, aad=None)
    except (InvalidTag, ValueError):
        return False
    if legacy != _CANARY_PLAINTEXT:
        return False
    conn.execute(
        "UPDATE settings SET value=?, updated_at=? WHERE key=?",
        (
            encrypt_value(
                _CANARY_PLAINTEXT,
                secret_key,
                conn=conn,
                aad=_CANARY_SETTING_KEY.encode(),
            ),
            _now_iso(),
            _CANARY_SETTING_KEY,
        ),
    )
    conn.commit()
    logger.info(
        "AES canary upgraded in place — pre-AAD ciphertext re-encrypted with AAD binding."
    )
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
    except Exception:  # pragma: no cover; rationale: best-effort audit write — never override the canary's security verdict because the audit callback raised
        logger.exception("canary on_failure callback raised reason=%s", reason)
