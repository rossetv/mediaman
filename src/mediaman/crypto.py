"""Encryption (AES-256-GCM) and HMAC token signing.

Provides two capabilities:
- AES-256-GCM symmetric encryption for storing secrets (API keys, tokens) in the DB.
- HMAC-SHA256 signed, time-limited tokens for "Keep" links in email newsletters.

Key derivation
--------------

From 2026-04-16 the AES key is derived via **HKDF-SHA256** with a
per-install random 16-byte salt stored in the ``settings`` table under
key ``aes_kdf_salt`` (base64). Pre-HKDF ciphertexts — encrypted with
plain SHA-256(secret) and no version byte — are still read by
:func:`decrypt_value` for backwards compatibility (legacy v1 format).

The encrypted payload layout is:

- **v2 (current)**: ``base64url(b"\\x02" || nonce(12) || ciphertext+tag)``
- **v1 (legacy)**: ``base64url(nonce(12) || ciphertext+tag)``

``encrypt_value`` always writes v2; ``decrypt_value`` tries v2 first and
falls back to v1 on a structural/tag mismatch.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("mediaman")

# Domain-separation string for HKDF. Do NOT change — any change
# invalidates every ciphertext already written to the DB.
_HKDF_INFO = b"mediaman-aes-v2"

# Version byte that prefixes every v2 ciphertext after base64-decoding.
_V2_PREFIX = b"\x02"

# Setting key where the per-install HKDF salt is persisted.
_SALT_SETTING_KEY = "aes_kdf_salt"

# Setting key for the startup canary (see canary_check).
_CANARY_SETTING_KEY = "aes_kdf_canary"
_CANARY_PLAINTEXT = "MEDIAMAN_KEY_CANARY"


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def _derive_aes_key_hkdf(secret_key: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key via HKDF-SHA256(secret, salt, info).

    The *salt* must be a stable per-install value — rotating it
    invalidates every existing ciphertext. The caller is responsible for
    persisting and reusing the same salt across runs.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO,
    )
    return hkdf.derive(secret_key.encode())


def _derive_aes_key_legacy(secret_key: str) -> bytes:
    """Derive the legacy v1 AES key (plain SHA-256 of the secret).

    Only used by :func:`decrypt_value` to read pre-HKDF ciphertexts
    (legacy v1 — pre-HKDF ciphertexts before 2026-04-16). Never used to
    encrypt new values.
    """
    return hashlib.sha256(secret_key.encode()).digest()


# ---------------------------------------------------------------------------
# Per-install salt
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_or_create_salt(conn: sqlite3.Connection) -> bytes:
    """Read the per-install HKDF salt from the DB, creating it if absent.

    The salt is 16 random bytes, base64-encoded for storage. ``INSERT OR
    IGNORE`` semantics are used so concurrent creators converge on the
    same value. Returns the raw decoded salt bytes.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)
    ).fetchone()
    if row is not None and row["value"]:
        try:
            return base64.b64decode(row["value"])
        except (ValueError, TypeError) as exc:
            # Corrupt salt — refuse to silently regenerate because that
            # would invalidate every ciphertext. Surface the problem.
            raise RuntimeError(
                f"Stored HKDF salt is corrupt and cannot be decoded: {exc}"
            ) from exc

    new_salt = secrets.token_bytes(16)
    encoded = base64.b64encode(new_salt).decode()
    # INSERT OR IGNORE so a concurrent writer doesn't cause us to overwrite.
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, encrypted, updated_at) "
        "VALUES (?, ?, 0, ?)",
        (_SALT_SETTING_KEY, encoded, _now_iso()),
    )
    conn.commit()

    # Re-read in case a concurrent writer beat us to it.
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)
    ).fetchone()
    return base64.b64decode(row["value"])


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------


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
) -> str:
    """Encrypt a string with AES-256-GCM using an HKDF-derived key.

    A fresh 12-byte nonce is generated per call, so encrypting the same
    plaintext twice produces different ciphertexts.

    The salt is resolved in order: explicit ``salt`` kwarg, then the
    per-install salt from ``conn`` (loaded/created via the ``settings``
    table). If neither is supplied, the call raises ``ValueError`` —
    ciphertexts must always be written in v2 format, which requires a
    salt.

    Returns a URL-safe base64-encoded string of
    ``b"\\x02" || nonce || ciphertext+tag``.
    """
    resolved_salt = _resolve_salt(conn, salt)
    if resolved_salt is None:
        raise ValueError(
            "encrypt_value requires a salt source — pass conn=... or salt=..."
        )
    key = _derive_aes_key_hkdf(secret_key, resolved_salt)
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(_V2_PREFIX + nonce + ciphertext).decode()


def decrypt_value(
    encrypted: str,
    secret_key: str,
    *,
    conn: sqlite3.Connection | None = None,
    salt: bytes | None = None,
) -> str:
    """Decrypt an AES-256-GCM value produced by :func:`encrypt_value`.

    Tries v2 (HKDF-derived key, leading ``0x02`` byte) first. On
    structural or tag mismatch — which is what a legacy ciphertext
    looks like to a v2 parser — falls back to v1 (legacy v1 —
    pre-HKDF ciphertexts before 2026-04-16), deriving the key via
    plain SHA-256 of the secret.

    Raises ``cryptography.exceptions.InvalidTag`` if neither path can
    authenticate the ciphertext. Raises ``ValueError`` for malformed
    input that isn't even valid base64.
    """
    raw = base64.urlsafe_b64decode(encrypted)

    # --- v2 attempt ---------------------------------------------------------
    # A valid v2 payload is: 1 prefix byte + 12 nonce bytes + at least 16
    # tag bytes (AES-GCM always emits a 16-byte tag). Anything shorter is
    # definitely not v2.
    if len(raw) >= 1 + 12 + 16 and raw[:1] == _V2_PREFIX:
        resolved_salt = _resolve_salt(conn, salt)
        if resolved_salt is not None:
            try:
                key = _derive_aes_key_hkdf(secret_key, resolved_salt)
                aesgcm = AESGCM(key)
                nonce = raw[1:13]
                ciphertext = raw[13:]
                return aesgcm.decrypt(nonce, ciphertext, None).decode()
            except InvalidTag:
                # Fall through to v1 — the leading 0x02 may just happen
                # to collide with the first byte of a legacy nonce.
                pass

    # --- v1 fallback --------------------------------------------------------
    # legacy v1 — pre-HKDF ciphertexts before 2026-04-16
    # No prefix byte; raw = nonce(12) || ciphertext+tag. Key is SHA-256(secret).
    key = _derive_aes_key_legacy(secret_key)
    aesgcm = AESGCM(key)
    nonce, ciphertext = raw[:12], raw[12:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


# ---------------------------------------------------------------------------
# Startup canary
# ---------------------------------------------------------------------------


def canary_check(conn: sqlite3.Connection, secret_key: str) -> bool:
    """Verify the AES key can decrypt a stored canary value.

    On first run the canary doesn't exist — encrypt a fixed plaintext
    (v2 format) and store it. On subsequent runs, decrypt the stored
    canary and confirm it matches the expected plaintext.

    Returns ``True`` when the canary is absent (just seeded) or
    decrypts correctly. Returns ``False`` on any decrypt failure or
    plaintext mismatch — in that case a LOUD warning is logged and the
    caller should surface this to the admin, but the app MUST NOT
    refuse to start (otherwise the admin can never log in to fix it).
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_CANARY_SETTING_KEY,)
    ).fetchone()

    if row is None or not row["value"]:
        # First run — seed the canary.
        ciphertext = encrypt_value(_CANARY_PLAINTEXT, secret_key, conn=conn)
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, encrypted, updated_at) "
            "VALUES (?, ?, 1, ?)",
            (_CANARY_SETTING_KEY, ciphertext, _now_iso()),
        )
        conn.commit()
        return True

    try:
        decrypted = decrypt_value(row["value"], secret_key, conn=conn)
    except (InvalidTag, ValueError):
        logger.warning(
            "AES key mismatch — existing encrypted settings can no longer be "
            "decrypted. The secret key likely changed since the last run. "
            "Rotate all encrypted settings (API keys, tokens) by re-entering "
            "them on the Settings page."
        )
        return False

    if decrypted != _CANARY_PLAINTEXT:
        logger.warning(
            "AES key mismatch — canary decrypted to unexpected value. "
            "Rotate all encrypted settings (API keys, tokens) by re-entering "
            "them on the Settings page."
        )
        return False

    return True


# ---------------------------------------------------------------------------
# HMAC-signed tokens (keep, download, session) — unchanged
# ---------------------------------------------------------------------------


def generate_keep_token(
    *,
    media_item_id: str,
    action_id: int,
    expires_at: int,
    secret_key: str,
) -> str:
    """Generate an HMAC-SHA256-signed keep token for email "Keep" links.

    The token is a URL-safe base64 encoding of ``payload_json | hmac_sig``.
    The payload carries ``media_item_id``, ``action_id``, and an expiry
    timestamp (``exp``, Unix seconds).
    """
    payload = json.dumps(
        {"media_item_id": media_item_id, "action_id": action_id, "exp": expires_at},
        separators=(",", ":"),
    )
    sig = hmac.new(
        secret_key.encode(), payload.encode(), hashlib.sha256
    ).digest()
    # Encode payload and signature separately, then join with a dot.
    # This avoids the delimiter appearing inside the binary signature.
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig_b64 = base64.urlsafe_b64encode(sig).decode()
    return f"{payload_b64}.{sig_b64}"


def validate_keep_token(token: str, secret_key: str) -> dict | None:
    """Validate and decode a keep token produced by :func:`generate_keep_token`.

    Returns the decoded payload dict if the signature is valid and the token
    has not expired; returns ``None`` for any failure (tampered, expired,
    wrong key, malformed).

    Uses a constant-time comparison to prevent timing attacks on the HMAC.
    """
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_bytes = base64.urlsafe_b64decode(parts[0])
        sig = base64.urlsafe_b64decode(parts[1])
        expected_sig = hmac.new(
            secret_key.encode(), payload_bytes, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(payload_bytes)
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def generate_download_token(
    *,
    email: str,
    action: str,
    title: str,
    media_type: str,
    tmdb_id: int | None,
    recommendation_id: int | None,
    secret_key: str,
    ttl_days: int = 14,
) -> str:
    """Generate an HMAC-SHA256-signed download token for email download/re-download CTAs.

    The token is a URL-safe base64 encoding of ``payload_json.hmac_sig``.
    The payload carries the recipient's ``email``, the requested ``act``ion
    (``"download"`` or ``"redownload"``), the media ``title``, ``mt``
    (media type), ``tmdb`` (TMDB ID), ``sid`` (recommendation ID), and an expiry
    timestamp (``exp``, Unix seconds).

    Tokens expire after *ttl_days* days (default 14).
    """
    exp = int(time.time()) + ttl_days * 86400
    payload = json.dumps(
        {
            "email": email,
            "act": action,
            "title": title,
            "mt": media_type,
            "tmdb": tmdb_id,
            "sid": recommendation_id,
            "exp": exp,
        },
        separators=(",", ":"),
    )
    sig = hmac.new(
        secret_key.encode(), payload.encode(), hashlib.sha256
    ).digest()
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig_b64 = base64.urlsafe_b64encode(sig).decode()
    return f"{payload_b64}.{sig_b64}"


def validate_download_token(token: str, secret_key: str) -> dict | None:
    """Validate and decode a download token produced by :func:`generate_download_token`.

    Returns the decoded payload dict if the signature is valid and the token
    has not expired; returns ``None`` for any failure (tampered, expired,
    wrong key, malformed).

    Uses a constant-time comparison to prevent timing attacks on the HMAC.
    """
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_bytes = base64.urlsafe_b64decode(parts[0])
        sig = base64.urlsafe_b64decode(parts[1])
        expected_sig = hmac.new(
            secret_key.encode(), payload_bytes, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(payload_bytes)
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def generate_session_token() -> str:
    """Generate a cryptographically random session token.

    Returns 32 random bytes encoded as a 64-character lowercase hex string.
    """
    return secrets.token_hex(32)
