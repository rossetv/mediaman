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

Setting-key binding
-------------------

From 2026-04-18 the **setting key name is passed as GCM AAD** when
encrypting/decrypting rows of the ``settings`` table. This binds the
ciphertext to the row it lives in: an attacker who can write to the DB
cannot swap the ``plex_token`` ciphertext into the ``openai_api_key``
row and have mediaman silently exfiltrate the Plex token to OpenAI.

Callers opt in by passing ``aad=<setting_key>.encode()``. Ciphertexts
written with AAD can only be decrypted when the same AAD is supplied;
ciphertexts written without AAD (legacy v2 rows, pre-2026-04-18)
continue to decrypt with ``aad=None``.

HMAC token domain separation
----------------------------

Each HMAC token type (``keep``, ``download``, ``unsubscribe``,
``poster``) derives a **per-purpose sub-key** via
``HMAC-SHA256(secret, info)``. This prevents cross-token confusion: a
keep token cannot be replayed as a download token or vice versa, and
introducing a new token type in future can't accidentally accept
tokens of another type.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
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

# Maximum length of any HMAC-signed token accepted by the validators.
# A legitimate token stays well under 1 KiB; rejecting oversize inputs
# up-front stops an attacker burning CPU/RAM on base64+HMAC of a
# multi-megabyte body.
_MAX_TOKEN_LEN = 4096

# Maximum length of a setting ciphertext the decrypt path will consider.
# Real setting values are API keys and URLs — kilobytes at most.
_MAX_CIPHERTEXT_LEN = 65_536


# ---------------------------------------------------------------------------
# Secret key entropy validation
# ---------------------------------------------------------------------------


def _secret_key_looks_strong(secret: str) -> bool:
    """Heuristic entropy check for ``MEDIAMAN_SECRET_KEY``.

    Accepts:

    - hex string of at least 64 chars (256 bits of entropy from a
      proper random source — the exact shape ``secrets.token_hex(32)``
      produces and the value the README recommends).
    - URL-safe base64 string of at least 43 chars (≥256 bits, matches
      ``secrets.token_urlsafe(32)``).
    - mixed strings of at least 40 chars that use at least three
      character classes (lower, upper, digit, punctuation) — tolerates
      pass-phrases but rejects ``"mediaman" * 4`` style inputs.

    This is not a strict entropy estimator; it is a "refuse obviously
    weak keys at boot" check. A bad key invalidates every downstream
    guarantee — refusing to start is the right behaviour on a public
    deployment.
    """
    if not secret or len(secret) < 32:
        return False
    # Regardless of the input format, trivial repetition is always weak.
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


def _derive_token_subkey(secret_key: str, purpose: bytes) -> bytes:
    """Derive a per-purpose HMAC sub-key from the master secret.

    Uses HMAC-SHA256 with the purpose tag as the "message" under the
    master secret as the key — a standard domain-separation construction
    that yields 32 independent-looking bytes for each purpose without
    any DB access. Callers pass a static byte string like ``b"keep"``,
    ``b"download"``, ``b"unsubscribe"``, ``b"poster"``.
    """
    return hmac.new(secret_key.encode(), purpose, hashlib.sha256).digest()


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

    Hardening: if the stored salt decodes to an unexpected length, we
    refuse to proceed — a tampered-down zero-byte salt would still work
    with HKDF but would strip the per-install uniqueness.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)
    ).fetchone()
    if row is not None and row["value"]:
        try:
            decoded = base64.b64decode(row["value"])
        except (ValueError, TypeError) as exc:
            # Corrupt salt — refuse to silently regenerate because that
            # would invalidate every ciphertext. Surface the problem.
            raise RuntimeError(
                f"Stored HKDF salt is corrupt and cannot be decoded: {exc}"
            ) from exc
        if len(decoded) != 16:
            raise RuntimeError(
                "Stored HKDF salt has unexpected length — refusing to "
                "proceed. This indicates database tampering."
            )
        return decoded

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
    decoded = base64.b64decode(row["value"])
    if len(decoded) != 16:
        raise RuntimeError(
            "Stored HKDF salt has unexpected length after first-run "
            "insert — refusing to proceed."
        )
    return decoded


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
    aad: bytes | None = None,
) -> str:
    """Encrypt a string with AES-256-GCM using an HKDF-derived key.

    A fresh 12-byte nonce is generated per call, so encrypting the same
    plaintext twice produces different ciphertexts.

    The salt is resolved in order: explicit ``salt`` kwarg, then the
    per-install salt from ``conn`` (loaded/created via the ``settings``
    table). If neither is supplied, the call raises ``ValueError`` —
    ciphertexts must always be written in v2 format, which requires a
    salt.

    ``aad`` (additional authenticated data) is optional; when supplied,
    it is bound into the GCM authentication tag and must be presented
    unchanged to :func:`decrypt_value`. Callers storing per-row
    ciphertexts should pass the row key as AAD so rows cannot be
    swapped.

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
    """Decrypt an AES-256-GCM value produced by :func:`encrypt_value`.

    Tries v2 (HKDF-derived key, leading ``0x02`` byte) first. On
    structural or tag mismatch — which is what a legacy ciphertext
    looks like to a v2 parser — falls back to v1 (legacy v1 —
    pre-HKDF ciphertexts before 2026-04-16), deriving the key via
    plain SHA-256 of the secret.

    If ``aad`` is supplied, the v2 path attempts AAD-authenticated
    decrypt FIRST. If that fails with ``InvalidTag``, it retries
    without AAD so ciphertexts written before the AAD-binding change
    still decrypt. The v1 fallback does not use AAD (the legacy format
    never supported it).

    **Salt precondition**: if neither ``conn`` nor ``salt`` is supplied,
    the v2 (HKDF) path is skipped entirely and only the legacy v1
    (SHA-256 key derivation) path is attempted. Callers that only hold a
    ``secret_key`` and no ``conn`` should be aware that v2 ciphertexts
    will raise ``InvalidTag`` unless they provide the salt explicitly.

    Raises ``cryptography.exceptions.InvalidTag`` if neither path can
    authenticate the ciphertext. Raises ``ValueError`` for malformed
    input that isn't even valid base64, for inputs that exceed the
    hard size cap, or for empty ciphertexts.
    """
    if not encrypted:
        raise ValueError("decrypt_value: empty ciphertext")
    if len(encrypted) > _MAX_CIPHERTEXT_LEN:
        raise ValueError("decrypt_value: ciphertext exceeds max length")

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
                # AAD-bound attempt first when the caller supplied one.
                if aad is not None:
                    try:
                        return aesgcm.decrypt(nonce, ciphertext, aad).decode()
                    except InvalidTag:
                        # Might be a pre-AAD ciphertext — retry without
                        # AAD so legacy rows still read.
                        pass
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
    plaintext = aesgcm.decrypt(nonce, ciphertext, None).decode()
    logger.warning(
        "Decrypted a legacy v1 ciphertext — consider rotating encrypted "
        "settings by re-saving them on the Settings page."
    )
    return plaintext


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

    Tamper detection: if the canary row is missing BUT other encrypted
    rows exist, that's not first-run — it's possible tampering. Log
    and return False.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_CANARY_SETTING_KEY,)
    ).fetchone()

    if row is None or not row["value"]:
        other = conn.execute(
            "SELECT 1 FROM settings WHERE encrypted=1 AND key != ? LIMIT 1",
            (_CANARY_SETTING_KEY,),
        ).fetchone()
        if other is not None:
            logger.warning(
                "AES canary is missing but encrypted settings exist — "
                "possible database tampering. The app will reseed the "
                "canary, but you should verify encrypted settings are "
                "still decryptable."
            )
        # Seed the canary using AAD binding so the row is tamper-evident.
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
        return other is None  # False if tamper suspected; True on genuine first-run

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
# HMAC-signed tokens
# ---------------------------------------------------------------------------

# Per-purpose tags used to derive domain-separated HMAC sub-keys.
_TOKEN_PURPOSE_KEEP = b"mediaman-token-keep-v1"
_TOKEN_PURPOSE_DOWNLOAD = b"mediaman-token-download-v1"
_TOKEN_PURPOSE_UNSUBSCRIBE = b"mediaman-token-unsubscribe-v1"
_TOKEN_PURPOSE_POSTER = b"mediaman-token-poster-v1"


def _sign(secret_key: str, purpose: bytes, payload: bytes) -> bytes:
    """Return HMAC-SHA256(subkey(secret,purpose), payload).

    Derives a per-purpose sub-key so tokens of one type cannot be
    validated as tokens of another type.
    """
    subkey = _derive_token_subkey(secret_key, purpose)
    return hmac.new(subkey, payload, hashlib.sha256).digest()


def _validate_signed(
    token: str, secret_key: str, purpose: bytes
) -> dict | None:
    """Shared validator for payload.signature tokens.

    Returns the decoded payload dict on success, or None on any failure
    (malformed, wrong signature, wrong purpose, expired).
    """
    if not token or len(token) > _MAX_TOKEN_LEN:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_bytes = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
        sig = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        expected_sig = _sign(secret_key, purpose, payload_bytes)
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp", 0)
        if not isinstance(exp, (int, float)) or exp < time.time():
            return None
        return payload
    except Exception:
        return None


def _encode_signed(payload: dict, secret_key: str, purpose: bytes) -> str:
    """Encode a ``payload.signature`` token with domain-separated key."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    sig = _sign(secret_key, purpose, payload_bytes)
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def generate_keep_token(
    *,
    media_item_id: str,
    action_id: int,
    expires_at: int,
    secret_key: str,
) -> str:
    """Generate an HMAC-SHA256-signed keep token for email "Keep" links.

    Uses the ``keep``-purpose HMAC sub-key so the token cannot be
    replayed as any other token type.
    """
    payload = {"media_item_id": media_item_id, "action_id": action_id, "exp": expires_at}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_KEEP)


def validate_keep_token(token: str, secret_key: str) -> dict | None:
    """Validate and decode a keep token produced by :func:`generate_keep_token`."""
    return _validate_signed(token, secret_key, _TOKEN_PURPOSE_KEEP)


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
    """Generate an HMAC-SHA256-signed download token for email download CTAs.

    Uses the ``download``-purpose HMAC sub-key; unreachable from the
    keep/unsubscribe validators.
    """
    exp = int(time.time()) + ttl_days * 86400
    payload = {
        "email": email,
        "act": action,
        "title": title,
        "mt": media_type,
        "tmdb": tmdb_id,
        "sid": recommendation_id,
        "exp": exp,
    }
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_DOWNLOAD)


def validate_download_token(token: str, secret_key: str) -> dict | None:
    """Validate and decode a download token produced by :func:`generate_download_token`."""
    return _validate_signed(token, secret_key, _TOKEN_PURPOSE_DOWNLOAD)


def generate_unsubscribe_token(
    *,
    email: str,
    secret_key: str,
    ttl_days: int = 180,
) -> str:
    """Generate a domain-separated unsubscribe token.

    Unlike the previous shape (``hmac_hex[:32]``), this token:

    - uses a ``unsubscribe``-purpose HMAC sub-key so it cannot be
      confused with keep/download tokens;
    - carries an explicit expiry (default 180 days) so archival copies
      stop working eventually;
    - retains full 256-bit HMAC output so truncation doesn't weaken
      forgery resistance.
    """
    exp = int(time.time()) + ttl_days * 86400
    payload = {"email": email.lower(), "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_UNSUBSCRIBE)


def validate_unsubscribe_token(
    token: str, secret_key: str, email: str
) -> bool:
    """Return True when *token* is a valid unsubscribe token for *email*."""
    payload = _validate_signed(token, secret_key, _TOKEN_PURPOSE_UNSUBSCRIBE)
    if payload is None:
        return False
    return payload.get("email", "").lower() == email.lower()


def generate_poster_token(rating_key: str, secret_key: str, *, ttl_days: int = 180) -> str:
    """Generate an HMAC token authorising access to a specific rating-key poster.

    Payload is a tiny ``{"rk": "...", "exp": N}`` blob signed with the
    poster-purpose sub-key. Replaces the previous "bare HMAC of rating
    key" shape so (a) the signature cannot be confused with other
    HMAC-over-string constructs, (b) every emitted URL eventually
    expires, (c) the sub-key is not the master secret.
    """
    exp = int(time.time()) + ttl_days * 86400
    payload = {"rk": rating_key, "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_POSTER)


def validate_poster_token(token: str, secret_key: str, rating_key: str) -> bool:
    """Return True when *token* authorises access to *rating_key*."""
    payload = _validate_signed(token, secret_key, _TOKEN_PURPOSE_POSTER)
    if payload is None:
        return False
    return payload.get("rk") == rating_key


def sign_poster_url(rating_key: str, secret_key: str) -> str:
    """Return a signed ``/api/poster/{rating_key}?sig=...`` URL.

    Lives in crypto alongside :func:`generate_poster_token` so service
    modules (e.g. newsletter) can import it without depending on the web
    layer.
    """
    token = generate_poster_token(rating_key, secret_key)
    return f"/api/poster/{rating_key}?sig={token}"


def generate_session_token() -> str:
    """Generate a cryptographically random session token.

    Returns 32 random bytes encoded as a 64-character lowercase hex string.
    """
    return secrets.token_hex(32)
