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
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
import sqlite3
import threading
from collections import OrderedDict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mediaman.core.time import now_iso as _now_iso

logger = logging.getLogger("mediaman")

_HKDF_INFO = b"mediaman-aes-v2"
_V2_PREFIX = b"\x02"
_SALT_SETTING_KEY = "aes_kdf_salt"
_CANARY_SETTING_KEY = "aes_kdf_canary"
_CANARY_PLAINTEXT = "MEDIAMAN_KEY_CANARY"

# Date on which the v1 ciphertext path (SHA-256-derived key, no prefix byte,
# no AAD) was removed from this module. Databases must have run migration v35
# (see :func:`migrate_legacy_ciphertexts`) before this version is deployed.
LEGACY_V1_REMOVED_AT = "2026-05-04"

# Per-call ciphertext cap. Settings rows are KB-scale (encrypted API
# keys, tokens, URLs); 64 KiB is comfortably above the largest legitimate
# value while keeping decrypt_value's failure mode bounded against a
# pathological INSERT that fills the row with megabytes of bogus base64.
_MAX_CIPHERTEXT_LEN = 65_536  # 64 KiB

# AES-GCM constants — RFC 5116 fixes both at these widths for the
# 128-bit-tag profile we use.
_GCM_NONCE_LEN = 12
_GCM_TAG_LEN = 16

# Minimum unique-character thresholds for the two accepted secret-key
# shapes (see :func:`_secret_key_looks_strong`).  Calibrated against
# 100k samples of secrets.token_hex(32) / token_urlsafe(32) — both
# bars sit well below the natural minimum a true CSPRNG produces, so
# legitimate keys are accepted while structured low-entropy strings
# are rejected.
_MIN_HEX_UNIQUE = 10
_MIN_URLSAFE_UNIQUE = 18


def _secret_key_looks_strong(secret: str) -> bool:
    """Return True if *secret* meets the minimum-entropy bar for ``MEDIAMAN_SECRET_KEY``.

    The check accepts exactly two shapes:

    * **Hex** — 64+ characters from ``[0-9a-fA-F]``, with at least
      :data:`_MIN_HEX_UNIQUE` distinct characters. 64 hex chars carries
      256 bits when truly random; the unique-char floor blocks structured
      low-entropy strings such as ``"deadbeef" * 8`` (8 unique).
    * **URL-safe base64** — 43+ characters from ``[A-Za-z0-9_-]`` that
      decode cleanly to 32+ bytes (the shape of
      :func:`secrets.token_urlsafe(32)`), with at least
      :data:`_MIN_URLSAFE_UNIQUE` distinct characters. The unique-char
      floor blocks the audit's ``"abcdefghij" * 4 + "abc"`` example
      (10 unique) which would otherwise satisfy the structural rule.

    Anything else — too short, mixed alphabets, structured repeats —
    is rejected so an operator running with ``MEDIAMAN_SECRET_KEY=abcd...``
    sees a clean startup error rather than silent acceptance of a
    near-zero-entropy key.
    """
    if not secret or len(secret) < 32:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{64,}", secret):
        return len(set(secret)) >= _MIN_HEX_UNIQUE
    if re.fullmatch(r"[A-Za-z0-9_\-]{43,}", secret):
        try:
            decoded = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        except (binascii.Error, ValueError):
            return False
        if len(decoded) < 32:
            return False
        return len(set(secret)) >= _MIN_URLSAFE_UNIQUE
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


# Salt cache — keyed by absolute DB path. Bounded to 4 entries to
# stop an unusual deployment that opens many distinct DB files
# (long-running test process, multi-tenant) from leaking memory.
_SALT_CACHE_MAX = 4
_salt_cache: OrderedDict[str, bytes] = OrderedDict()
_salt_cache_lock = threading.Lock()


def _salt_cache_get(cache_key: str) -> bytes | None:
    """Return the cached salt for *cache_key*, refreshing LRU order on hit."""
    with _salt_cache_lock:
        if cache_key in _salt_cache:
            _salt_cache.move_to_end(cache_key)
            return _salt_cache[cache_key]
    return None


def _salt_cache_put(cache_key: str, salt: bytes) -> None:
    """Insert *salt* under *cache_key*, evicting the LRU entry if at capacity."""
    with _salt_cache_lock:
        _salt_cache[cache_key] = salt
        _salt_cache.move_to_end(cache_key)
        while len(_salt_cache) > _SALT_CACHE_MAX:
            _salt_cache.popitem(last=False)


def _salt_cache_pop(cache_key: str) -> None:
    """Drop the cached salt for *cache_key* if present."""
    with _salt_cache_lock:
        _salt_cache.pop(cache_key, None)


def _db_path(conn: sqlite3.Connection) -> str:
    """Return the absolute path of the primary database attached to *conn*."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


def _load_or_create_salt(conn: sqlite3.Connection) -> bytes:
    """Read the per-install HKDF salt from the DB, creating it if absent.

    The first-run INSERT uses ``INSERT OR IGNORE`` followed by a re-read.
    Two workers racing to seed the salt will both produce a candidate;
    only one INSERT wins and both then read back the same persisted value.

    Raises:
        RuntimeError: If the stored salt is corrupt (cannot be base64-decoded),
            has an unexpected length (indicating possible database tampering),
            or if the salt persisted on first run has an unexpected length
            after the INSERT/re-read cycle.
    """
    cache_key = _db_path(conn)
    if cache_key:
        cached = _salt_cache_get(cache_key)
        if cached is not None:
            return cached

    row = conn.execute("SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)).fetchone()
    if row is not None and row["value"]:
        try:
            decoded = base64.b64decode(row["value"])
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Stored HKDF salt is corrupt and cannot be decoded: {exc}") from exc
        if len(decoded) != 16:
            raise RuntimeError(
                "Stored HKDF salt has unexpected length — refusing to "
                "proceed. This indicates database tampering."
            )
        if cache_key:
            _salt_cache_put(cache_key, decoded)
        return decoded

    # First-run path. INSERT OR IGNORE makes the seed race-safe: two
    # workers both see the absent row, both generate a candidate, both
    # try to INSERT — only the first commits, the second's IGNORE
    # silently no-ops, and the re-read below returns whichever value
    # actually landed.
    new_salt = secrets.token_bytes(16)
    encoded = base64.b64encode(new_salt).decode()
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
        (_SALT_SETTING_KEY, encoded, _now_iso()),
    )
    conn.commit()

    row = conn.execute("SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)).fetchone()
    decoded = base64.b64decode(row["value"])
    if len(decoded) != 16:
        raise RuntimeError(
            "Stored HKDF salt has unexpected length after first-run insert — refusing to proceed."
        )
    if cache_key:
        _salt_cache_put(cache_key, decoded)
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


def _decrypt_v1_raw(raw: bytes, secret_key: str) -> str:
    """Decrypt raw v1 bytes (no prefix byte, SHA-256-derived key, no AAD).

    Private — used only by :func:`migrate_legacy_ciphertexts`. Not part of
    the public :func:`decrypt_value` API; v1 support was removed on
    2026-05-04.

    Raises:
        InvalidTag: if authentication fails (wrong key or corrupt bytes).
    """
    if len(raw) < _GCM_NONCE_LEN + _GCM_TAG_LEN:
        raise InvalidTag()
    key = hashlib.sha256(secret_key.encode()).digest()
    aesgcm = AESGCM(key)
    nonce, ciphertext = raw[:_GCM_NONCE_LEN], raw[_GCM_NONCE_LEN:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


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


def migrate_legacy_ciphertexts(conn: sqlite3.Connection, secret_key: str) -> int:
    """Re-encrypt all legacy settings ciphertexts to v2 (AAD-bound).

    This is the migration v35 worker. It is called once at startup by the
    crypto bootstrap step after :func:`canary_check` passes. It is safe
    to call multiple times — already-migrated rows pass v2+AAD decryption
    and are left untouched.

    Two categories of rows are upgraded:

    * **v1 rows** — SHA-256-derived key, no prefix byte, no AAD. These
      would raise :class:`InvalidTag` from :func:`decrypt_value` without
      this migration.
    * **v2 no-AAD rows** — HKDF key, ``0x02`` prefix, but encrypted
      before AAD binding was introduced. They decrypt but are not row-bound;
      this migration re-encrypts them with the setting key as AAD.

    Returns:
        Number of rows that were re-encrypted (0 if nothing to do).

    Raises:
        RuntimeError: If the HKDF salt cannot be loaded (corrupt DB).
    """
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE encrypted=1 AND key != ? AND key != ?",
        (_SALT_SETTING_KEY, _CANARY_SETTING_KEY),
    ).fetchall()

    if not rows:
        return 0

    salt = _load_or_create_salt(conn)
    hkdf_key = _derive_aes_key_hkdf(secret_key, salt)
    migrated = 0

    for row in rows:
        key_name: str = row["key"]
        raw_ct: str = row["value"]
        aad = key_name.encode()

        try:
            raw = base64.urlsafe_b64decode(raw_ct)
        except Exception:
            # Include exc_info — "bad base64" is a category, but the
            # specific Exception subclass and message tell an operator
            # whether they're looking at a corrupted row, an unexpected
            # byte type from sqlite3, or something stranger.
            logger.warning(
                "migrate_legacy_ciphertexts: skipping key=%s (decode failed)",
                key_name,
                exc_info=True,
            )
            continue

        # Check whether the row is already v2+AAD (happy path → skip).
        if len(raw) >= 1 + _GCM_NONCE_LEN + _GCM_TAG_LEN and raw[:1] == _V2_PREFIX:
            aesgcm = AESGCM(hkdf_key)
            nonce = raw[1 : 1 + _GCM_NONCE_LEN]
            ciphertext = raw[1 + _GCM_NONCE_LEN :]
            try:
                aesgcm.decrypt(nonce, ciphertext, aad)
                continue  # already AAD-bound
            except InvalidTag:
                # v2 no-AAD row — decrypt without AAD then re-encrypt.
                try:
                    plaintext = aesgcm.decrypt(nonce, ciphertext, None).decode()
                except InvalidTag:
                    logger.warning(
                        "migrate_legacy_ciphertexts: v2 row failed both AAD and "
                        "no-AAD decrypt for key=%s — skipping (wrong key?)",
                        key_name,
                    )
                    continue
        else:
            # Attempt v1 decrypt (SHA-256 key, no prefix byte).
            try:
                plaintext = _decrypt_v1_raw(raw, secret_key)
            except InvalidTag:
                logger.warning(
                    "migrate_legacy_ciphertexts: unrecognised ciphertext for key=%s — skipping",
                    key_name,
                )
                continue

        new_ct = encrypt_value(plaintext, secret_key, salt=salt, aad=aad)
        conn.execute(
            "UPDATE settings SET value=?, encrypted=1, updated_at=? WHERE key=?",
            (new_ct, _now_iso(), key_name),
        )
        migrated += 1
        logger.info("migrate_legacy_ciphertexts: re-encrypted key=%s to v2+AAD", key_name)

    if migrated:
        # Guard the commit explicitly. A silently-failed commit produces a
        # livelock: every restart re-runs the migration, attempts the same
        # commit, fails the same way, and reports success without ever
        # persisting the AAD-bound rows. Re-raise so the caller (typically
        # bootstrap_crypto) can decide whether to abort startup or
        # continue with rollback semantics.
        try:
            conn.commit()
        except Exception:
            logger.error(
                "migrate_legacy_ciphertexts: commit failed after %d "
                "re-encryptions — rows were NOT persisted; the migration "
                "will be retried on next startup",
                migrated,
                exc_info=True,
            )
            raise
        try:
            from mediaman.audit import security_event

            security_event(
                conn,
                event="aes.v35_migration_complete",
                actor="",
                ip="",
                detail={"migrated_count": migrated},
            )
        except Exception:  # pragma: no cover — never block on audit failure
            logger.exception("aes.v35_migration_complete audit write failed")

    return migrated


def canary_check(conn: sqlite3.Connection, secret_key: str) -> bool:
    """Verify the AES key can decrypt a stored canary value."""
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
            _audit_canary_failure(conn, reason="canary_missing_with_encrypted_rows")
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
        _audit_canary_failure(conn, reason="canary_decrypt_invalid_tag")
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
        _audit_canary_failure(conn, reason="canary_plaintext_mismatch")
        return False

    return True


def _audit_canary_failure(conn: sqlite3.Connection, *, reason: str) -> None:
    """Best-effort audit-log a canary failure when a *conn* is available.

    The canary fires before the audit table is guaranteed to exist on
    fresh-DB bootstrap, so any failure in the audit path is logged and
    swallowed — the security verdict (False) is what matters; the audit
    row is the cherry on top.
    """
    if conn is None:
        return
    try:
        from mediaman.audit import security_event

        security_event(
            conn,
            event="aes.canary_failed",
            actor="",
            ip="",
            detail={"reason": reason},
        )
    except Exception:  # pragma: no cover — never break the read on audit failure
        logger.exception("aes.canary_failed audit write failed reason=%s", reason)
