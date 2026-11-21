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
import binascii
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
    _GCM_NONCE_BYTES,
    _GCM_TAG_BYTES,
    _MAX_CIPHERTEXT_LEN,
    _V2_PREFIX,
    CryptoInputError,
    _derive_aes_key_hkdf,
    _get_db_path,
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
    nonce = secrets.token_bytes(_GCM_NONCE_BYTES)
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
    """Decrypt an AES-256-GCM v2 value produced by encrypt_value.

    Args:
        encrypted: Base64url-encoded ciphertext.
        secret_key: Master ``MEDIAMAN_SECRET_KEY``.
        conn: Optional SQLite connection — used to look up the per-install
            HKDF salt.
        salt: Optional explicit salt. Wins over ``conn``.
        aad: Optional Additional Authenticated Data — typically the
            settings row's key name.

    Raises:
        CryptoInputError: If *encrypted* is empty, exceeds the maximum
            ciphertext length, or is not valid base64url.
        ValueError: On a v2-shaped ciphertext when no salt source is
            supplied (neither *conn* nor *salt*) — a key-material
            configuration error, distinct from an authentication failure.
        InvalidTag: When the ciphertext fails authentication.
    """
    if not encrypted:
        raise CryptoInputError("decrypt_value: empty ciphertext")
    if len(encrypted) > _MAX_CIPHERTEXT_LEN:
        raise CryptoInputError("decrypt_value: ciphertext exceeds max length")

    # ``encrypted`` is attacker/DB-controlled; a malformed base64 string
    # raises ``binascii.Error`` (a ``ValueError`` subclass). Surface it as
    # the documented ``CryptoInputError`` so callers catching the contract
    # don't see an unexpected error type escape.
    try:
        raw = base64.urlsafe_b64decode(encrypted)
    except (binascii.Error, ValueError) as exc:
        raise CryptoInputError("decrypt_value: ciphertext is not valid base64") from exc

    if len(raw) >= 1 + _GCM_NONCE_BYTES + _GCM_TAG_BYTES and raw[:1] == _V2_PREFIX:
        resolved_salt = _resolve_salt(conn, salt)
        if resolved_salt is None:
            # v2-shaped input but no key material — a programming/config
            # error, not a tampered ciphertext. Raise a distinct ValueError
            # (matching encrypt_value) rather than a misleading InvalidTag.
            raise ValueError("decrypt_value requires a salt source — pass conn=... or salt=...")
        key = _derive_aes_key_hkdf(secret_key, resolved_salt)
        aesgcm = AESGCM(key)
        nonce = raw[1 : 1 + _GCM_NONCE_BYTES]
        ciphertext = raw[1 + _GCM_NONCE_BYTES :]
        return aesgcm.decrypt(nonce, ciphertext, aad).decode()

    raise InvalidTag()


def is_canary_valid(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    on_failure: collections.abc.Callable[[str], None] | None = None,
) -> bool:
    """Verify the AES key can decrypt a stored canary value.

    When the canary no longer decrypts (the key changed), it is rewritten
    under *secret_key* only if every other encrypted setting decrypts
    with that key; otherwise the check fails and logs which settings
    still need re-entering or clearing.

    Args:
        conn:       Open SQLite connection.
        secret_key: Master ``MEDIAMAN_SECRET_KEY``.
        on_failure: Optional callback invoked with the failure-reason string
                    when the check fails.  The caller (typically
                    ``bootstrap_crypto`` in ``app_factory``) passes a closure
                    that writes a ``security_event`` audit row.  Keeping the
                    audit-write out of ``crypto/`` preserves the leaf-package
                    invariant: ``crypto/`` must not import from
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
    except (InvalidTag, ValueError, CryptoInputError):
        return _reseal_canary_or_fail(conn, secret_key, on_failure)

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


def _reseal_canary_or_fail(
    conn: sqlite3.Connection,
    secret_key: str,
    on_failure: collections.abc.Callable[[str], None] | None,
) -> bool:
    """Rewrite the canary under *secret_key* if every secret reads under it.

    The canary no longer decrypts, so the key changed. Once every
    encrypted setting has been re-entered or cleared under the new key,
    scans can read all their credentials again and the canary is
    rewritten. While any one still fails, the canary is left alone and
    the check fails, naming those settings (never their values).
    """
    # The cached salt may belong to a database swapped in under the same
    # path; re-read it so the probe and any rewrite use this DB's salt.
    cache_key = _get_db_path(conn)
    if cache_key:
        _salt_cache_pop(cache_key)

    rows = conn.execute(
        "SELECT key, value FROM settings WHERE encrypted=1 AND key != ? ORDER BY key",
        (_CANARY_SETTING_KEY,),
    ).fetchall()
    unreadable = []
    for row in rows:
        try:
            decrypt_value(row["value"], secret_key, conn=conn, aad=row["key"].encode())
        except (InvalidTag, ValueError, CryptoInputError):
            unreadable.append(row["key"])

    if unreadable:
        return _fail_canary(
            conn,
            on_failure,
            "canary_decrypt_invalid_tag",
            "AES key mismatch — MEDIAMAN_SECRET_KEY changed and these encrypted "
            f"settings cannot be decrypted with it: {', '.join(map(repr, unreadable))}. "
            "Put the previous MEDIAMAN_SECRET_KEY back, or re-enter or clear each "
            "of these settings on the Settings page, then restart mediaman — the "
            "key is checked only at startup.",
        )

    ciphertext = encrypt_value(
        _CANARY_PLAINTEXT,
        secret_key,
        conn=conn,
        aad=_CANARY_SETTING_KEY.encode(),
    )
    conn.execute(
        "UPDATE settings SET value=?, updated_at=? WHERE key=?",
        (ciphertext, _now_iso(), _CANARY_SETTING_KEY),
    )
    conn.commit()
    logger.warning(
        "MEDIAMAN_SECRET_KEY changed; every encrypted setting decrypts with the "
        "new key, so the AES canary was rewritten under it."
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
    cache_key = _get_db_path(conn)
    if cache_key:
        _salt_cache_pop(cache_key)
    _invoke_on_failure(on_failure, reason)
    return False


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
    except Exception:  # pragma: no cover; rationale: a buggy or slow audit closure (e.g. a signature-mismatch TypeError) must not flip the canary's security verdict — log it and return the verdict the crypto check produced
        logger.exception("canary on_failure callback raised reason=%s", reason)
