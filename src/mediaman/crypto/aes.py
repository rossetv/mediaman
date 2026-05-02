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

from mediaman.services.infra.time import now_iso as _now_iso

logger = logging.getLogger("mediaman")

_HKDF_INFO = b"mediaman-aes-v2"
_V2_PREFIX = b"\x02"
_SALT_SETTING_KEY = "aes_kdf_salt"
_CANARY_SETTING_KEY = "aes_kdf_canary"
_CANARY_PLAINTEXT = "MEDIAMAN_KEY_CANARY"

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


def _derive_aes_key_legacy(secret_key: str) -> bytes:
    """Derive the legacy v1 AES key (plain SHA-256 of the secret)."""
    return hashlib.sha256(secret_key.encode()).digest()


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


def _reencrypt_legacy_no_aad(
    *,
    conn: sqlite3.Connection,
    settings_key: str,
    plaintext: str,
    secret_key: str,
    aad: bytes,
) -> None:
    """Re-encrypt a legacy no-AAD value with AAD and persist it back to ``settings``.

    Best-effort: any DB or crypto failure is logged and swallowed so a
    read path is never broken by an opportunistic upgrade.
    """
    try:
        new_ciphertext = encrypt_value(plaintext, secret_key, conn=conn, aad=aad)
        conn.execute(
            "UPDATE settings SET value=?, encrypted=1, updated_at=? WHERE key=?",
            (new_ciphertext, _now_iso(), settings_key),
        )
        conn.commit()
        logger.warning(
            "crypto.setting_aes_reencrypted key=%s — legacy no-AAD value upgraded to AAD-bound "
            "ciphertext on first read.",
            settings_key,
        )
        # Best-effort audit trail. Gated by import so the crypto module
        # remains usable in contexts where the audit table doesn't yet
        # exist (e.g. very early bootstrap, fresh test DB).
        try:
            from mediaman.audit import security_event

            security_event(
                conn,
                event="aes.setting_reencrypted",
                actor="",
                ip="",
                detail={"key": settings_key},
            )
        except Exception:  # pragma: no cover — never break the read on audit failure
            logger.exception("setting_aes_reencrypted audit write failed key=%s", settings_key)
    except Exception:  # pragma: no cover — re-encrypt is best-effort
        logger.exception(
            "Failed to opportunistically re-encrypt legacy no-AAD setting key=%s; "
            "the row remains decryptable but unbound to its key name.",
            settings_key,
        )


def decrypt_value(
    encrypted: str,
    secret_key: str,
    *,
    conn: sqlite3.Connection | None = None,
    salt: bytes | None = None,
    aad: bytes | None = None,
    settings_key: str | None = None,
) -> str:
    """Decrypt an AES-256-GCM value produced by :func:`encrypt_value`.

    For v2 ciphertexts (prefixed ``0x02``), decryption is attempted first
    with the supplied *aad*. If that raises :class:`InvalidTag` and *aad*
    was supplied, a second attempt without AAD is made — this exists to
    transparently read values that were encrypted before AAD binding was
    introduced. **Such legacy rows are not row-bound**: an attacker who
    can write to the ``settings`` table could move a no-AAD ciphertext to
    a different key name without the read path noticing. To close this
    drift over time, callers reading from the ``settings`` table can pass
    both ``conn=`` and ``settings_key=`` and the function will
    opportunistically re-encrypt the value with AAD on a successful
    no-AAD decrypt; the next read then takes the secure path. Skip the
    upgrade by omitting ``settings_key`` (or by reading without a conn,
    e.g. on cold boot).

    If both v2 attempts fail, the legacy v1 path is tried silently and a
    debug log entry is emitted.

    For v1 ciphertexts (no prefix byte), the legacy SHA-256-derived key
    is used directly and a ``WARNING`` is logged prompting the operator
    to re-encrypt the value.

    Args:
        encrypted: Base64url-encoded ciphertext.
        secret_key: Master ``MEDIAMAN_SECRET_KEY``.
        conn: Optional SQLite connection — required if you want the
            no-AAD-success self-upgrade to fire.
        salt: Optional explicit salt. Wins over ``conn``.
        aad: Optional Additional Authenticated Data — typically the
            settings row's key name.
        settings_key: When set together with ``conn`` and ``aad``, a
            successful no-AAD decrypt triggers a re-encrypt with AAD
            written back to ``settings(key=settings_key)``. Without
            this, the legacy ciphertext is read and returned unchanged.

    Raises:
        ValueError: If *encrypted* is empty or exceeds the maximum
            ciphertext length.
        InvalidTag: When a v2 ciphertext fails authentication and no v1
            fallback is possible.
    """
    if not encrypted:
        raise ValueError("decrypt_value: empty ciphertext")
    if len(encrypted) > _MAX_CIPHERTEXT_LEN:
        raise ValueError("decrypt_value: ciphertext exceeds max length")

    raw = base64.urlsafe_b64decode(encrypted)

    if len(raw) >= 1 + _GCM_NONCE_LEN + _GCM_TAG_LEN and raw[:1] == _V2_PREFIX:
        resolved_salt = _resolve_salt(conn, salt)
        if resolved_salt is not None:
            try:
                key = _derive_aes_key_hkdf(secret_key, resolved_salt)
                aesgcm = AESGCM(key)
                nonce = raw[1 : 1 + _GCM_NONCE_LEN]
                ciphertext = raw[1 + _GCM_NONCE_LEN :]
                if aad is not None:
                    try:
                        return aesgcm.decrypt(nonce, ciphertext, aad).decode()
                    except InvalidTag:
                        # Fall through to no-AAD attempt for legacy
                        # pre-AAD ciphertexts. On success below, we
                        # opportunistically re-encrypt with AAD if the
                        # caller supplied conn + settings_key.
                        pass
                plaintext = aesgcm.decrypt(nonce, ciphertext, None).decode()
                if aad is not None and conn is not None and settings_key is not None:
                    _reencrypt_legacy_no_aad(
                        conn=conn,
                        settings_key=settings_key,
                        plaintext=plaintext,
                        secret_key=secret_key,
                        aad=aad,
                    )
                return plaintext
            except InvalidTag:
                logger.debug("decrypt: v2 failed, trying v1 legacy path")

    # ---------------------------------------------------------------------------
    # Legacy v1 decrypt path — SUNSET TARGET: v2.0
    #
    # This path handles ciphertexts produced before HKDF key derivation was
    # introduced (plain SHA-256 of the secret key, no AAD, no prefix byte).
    # It will be removed in v2.0 once all installations have had the
    # opportunity to rotate their encrypted settings via the Settings page.
    # Track removal at https://github.com/rossetv/mediaman/issues/120.
    # ---------------------------------------------------------------------------
    if len(raw) < _GCM_NONCE_LEN + _GCM_TAG_LEN:
        raise InvalidTag()
    # Note: we do NOT short-circuit when raw[:1] == _V2_PREFIX. A legitimate
    # v1 ciphertext begins with a 12-byte random nonce whose first byte is
    # 0x02 about 1 time in 256 — refusing to decrypt those would be a
    # 0.39% flake on round-trip tests and (worse) on production reads.
    # If the bytes were truly v2 with wrong-key/corrupt content, the
    # legacy AESGCM.decrypt below will fail authentication and raise
    # InvalidTag exactly as before — no silent mis-decrypt is possible
    # because v1 and v2 use different key-derivation functions.
    key = _derive_aes_key_legacy(secret_key)
    aesgcm = AESGCM(key)
    nonce, ciphertext = raw[:_GCM_NONCE_LEN], raw[_GCM_NONCE_LEN:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None).decode()
    logger.warning(
        "Decrypted a legacy v1 ciphertext — consider rotating encrypted "
        "settings by re-saving them on the Settings page."
    )
    return plaintext


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
