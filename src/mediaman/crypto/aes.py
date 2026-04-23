"""AES-256-GCM encryption-at-rest for settings.

Provides symmetric encryption for storing secrets (API keys, tokens) in
the SQLite ``settings`` table. Key derivation uses HKDF-SHA256 with a
per-install random 16-byte salt; ciphertexts are v2-prefixed
(``0x02``). A legacy v1 path (plain SHA-256 of the secret, no prefix
byte) is retained for backwards compatibility.

From 2026-04-18 the setting key name is passed as GCM AAD when
encrypting/decrypting rows of the ``settings`` table, binding each
ciphertext to the row it lives in.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("mediaman")

_HKDF_INFO = b"mediaman-aes-v2"
_V2_PREFIX = b"\x02"
_SALT_SETTING_KEY = "aes_kdf_salt"
_CANARY_SETTING_KEY = "aes_kdf_canary"
_CANARY_PLAINTEXT = "MEDIAMAN_KEY_CANARY"
_MAX_CIPHERTEXT_LEN = 1_048_576  # 1 MiB


def _secret_key_looks_strong(secret: str) -> bool:
    """Heuristic entropy check for ``MEDIAMAN_SECRET_KEY``."""
    if not secret or len(secret) < 32:
        return False
    if len(set(secret)) < 8:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{64,}", secret):
        return True
    if re.fullmatch(r"[A-Za-z0-9_\-]{43,}", secret):
        return True
    classes = sum(
        [
            any(c.islower() for c in secret),
            any(c.isupper() for c in secret),
            any(c.isdigit() for c in secret),
            any(not c.isalnum() for c in secret),
        ]
    )
    if len(secret) >= 40 and classes >= 3:
        return True
    return False


def _derive_aes_key_hkdf(secret_key: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key via HKDF-SHA256(secret, salt, info)."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO,
    )
    return hkdf.derive(secret_key.encode())


def _derive_aes_key_legacy(secret_key: str) -> bytes:
    """Derive the legacy v1 AES key (plain SHA-256 of the secret)."""
    return hashlib.sha256(secret_key.encode()).digest()


_salt_cache: dict[str, bytes] = {}
_salt_cache_lock = threading.Lock()


def _db_path(conn: sqlite3.Connection) -> str:
    """Return the absolute path of the primary database attached to *conn*."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_or_create_salt(conn: sqlite3.Connection) -> bytes:
    """Read the per-install HKDF salt from the DB, creating it if absent."""
    cache_key = _db_path(conn)
    if cache_key:
        with _salt_cache_lock:
            if cache_key in _salt_cache:
                return _salt_cache[cache_key]

    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)
    ).fetchone()
    if row is not None and row["value"]:
        try:
            decoded = base64.b64decode(row["value"])
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Stored HKDF salt is corrupt and cannot be decoded: {exc}"
            ) from exc
        if len(decoded) != 16:
            raise RuntimeError(
                "Stored HKDF salt has unexpected length — refusing to "
                "proceed. This indicates database tampering."
            )
        if cache_key:
            with _salt_cache_lock:
                _salt_cache[cache_key] = decoded
        return decoded

    new_salt = secrets.token_bytes(16)
    encoded = base64.b64encode(new_salt).decode()
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, encrypted, updated_at) "
        "VALUES (?, ?, 0, ?)",
        (_SALT_SETTING_KEY, encoded, _now_iso()),
    )
    conn.commit()

    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)
    ).fetchone()
    decoded = base64.b64decode(row["value"])
    if len(decoded) != 16:
        raise RuntimeError(
            "Stored HKDF salt has unexpected length after first-run "
            "insert — refusing to proceed."
        )
    if cache_key:
        with _salt_cache_lock:
            _salt_cache[cache_key] = decoded
    return decoded


def _resolve_salt(conn: sqlite3.Connection | None, salt: bytes | None) -> bytes | None:
    """Resolve the effective salt: explicit *salt* wins, else load from *conn*."""
    if salt is not None:
        return salt
    if conn is not None:
        return _load_or_create_salt(conn)
    return None


def encrypt_value(
    plaintext: str,
    secret_key: str,
    *,
    conn: sqlite3.Connection | None = None,
    salt: bytes | None = None,
    aad: bytes | None = None,
) -> str:
    """Encrypt a string with AES-256-GCM using an HKDF-derived key."""
    resolved_salt = _resolve_salt(conn, salt)
    if resolved_salt is None:
        raise ValueError(
            "encrypt_value requires a salt source — pass conn=... or salt=..."
        )
    key = _derive_aes_key_hkdf(secret_key, resolved_salt)
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)
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
    """Decrypt an AES-256-GCM value produced by :func:`encrypt_value`."""
    if not encrypted:
        raise ValueError("decrypt_value: empty ciphertext")
    if len(encrypted) > _MAX_CIPHERTEXT_LEN:
        raise ValueError("decrypt_value: ciphertext exceeds max length")

    raw = base64.urlsafe_b64decode(encrypted)

    if len(raw) >= 1 + 12 + 16 and raw[:1] == _V2_PREFIX:
        resolved_salt = _resolve_salt(conn, salt)
        if resolved_salt is not None:
            try:
                key = _derive_aes_key_hkdf(secret_key, resolved_salt)
                aesgcm = AESGCM(key)
                nonce = raw[1:13]
                ciphertext = raw[13:]
                if aad is not None:
                    try:
                        return aesgcm.decrypt(nonce, ciphertext, aad).decode()
                    except InvalidTag:
                        pass
                return aesgcm.decrypt(nonce, ciphertext, None).decode()
            except InvalidTag:
                pass

    if len(raw) < 12 + 16:
        raise InvalidTag()
    if raw[:1] == _V2_PREFIX:
        raise InvalidTag()
    key = _derive_aes_key_legacy(secret_key)
    aesgcm = AESGCM(key)
    nonce, ciphertext = raw[:12], raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None).decode()
    logger.warning(
        "Decrypted a legacy v1 ciphertext — consider rotating encrypted "
        "settings by re-saving them on the Settings page."
    )
    return plaintext


def canary_check(conn: sqlite3.Connection, secret_key: str) -> bool:
    """Verify the AES key can decrypt a stored canary value."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_CANARY_SETTING_KEY,)
    ).fetchone()

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
            with _salt_cache_lock:
                _salt_cache.pop(cache_key, None)
        return False

    if decrypted != _CANARY_PLAINTEXT:
        logger.warning(
            "AES key mismatch — canary decrypted to unexpected value. "
            "Rotate all encrypted settings (API keys, tokens) by re-entering "
            "them on the Settings page."
        )
        cache_key = _db_path(conn)
        if cache_key:
            with _salt_cache_lock:
                _salt_cache.pop(cache_key, None)
        return False

    return True
