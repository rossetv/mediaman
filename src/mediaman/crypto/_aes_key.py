"""AES key/salt management: constants, HKDF derivation, and per-install salt LRU cache."""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import sqlite3
import threading

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mediaman.core.time import now_iso as _now_iso


class CryptoError(Exception):
    """Base class for cryptography-subsystem failures.

    Raised by HKDF salt management (corrupt / tampered salt rows) and any
    other ``crypto/`` failure that callers may want to handle distinctly
    from ``cryptography.exceptions.InvalidTag``.
    """


class CryptoInputError(CryptoError):
    """Raised when a caller passes an invalid ciphertext to a crypto function.

    Covers structural problems that are detectable before attempting
    decryption — e.g. an empty ciphertext or one that exceeds the
    maximum length.  Distinct from :class:`CryptoError` so callers that
    want to distinguish a malformed-input condition from a key-management
    failure can do so without catching the broad base class.
    """


# ---------------------------------------------------------------------------
# AES / GCM / HKDF constants
# ---------------------------------------------------------------------------

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
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16

# Minimum unique-character thresholds for the two accepted secret-key
# shapes (see :func:`_secret_key_looks_strong`).  Calibrated against
# 100k samples of secrets.token_hex(32) / token_urlsafe(32) — both
# bars sit well below the natural minimum a true CSPRNG produces, so
# legitimate keys are accepted while structured low-entropy strings
# are rejected.
_MIN_HEX_UNIQUE = 10
_MIN_URLSAFE_UNIQUE = 18


# ---------------------------------------------------------------------------
# Key-strength heuristic
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HKDF key derivation
# ---------------------------------------------------------------------------


def _derive_aes_key_hkdf(secret_key: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key via HKDF-SHA256(secret, salt, info)."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO,
    )
    return hkdf.derive(secret_key.encode())


# ---------------------------------------------------------------------------
# Salt cache — single entry
# ---------------------------------------------------------------------------

# mediaman is a single-process design (CODE_GUIDELINES §1.12), so the salt
# cache only ever needs to remember one DB path. Storing a single
# ``(path, salt)`` tuple under a lock is enough to skip the second DB read
# on the hot path; if a different path is queried the previous entry is
# overwritten.
_salt_cache: dict[str, bytes] = {}
_salt_cache_lock = threading.Lock()


def _salt_cache_get(cache_key: str) -> bytes | None:
    """Return the cached salt for *cache_key* if it matches the single entry."""
    with _salt_cache_lock:
        return _salt_cache.get(cache_key)


def _salt_cache_put(cache_key: str, salt: bytes) -> None:
    """Store *salt* under *cache_key*, replacing any previous single entry."""
    with _salt_cache_lock:
        _salt_cache.clear()
        _salt_cache[cache_key] = salt


def _salt_cache_pop(cache_key: str) -> None:
    """Drop the cached salt for *cache_key* if present."""
    with _salt_cache_lock:
        _salt_cache.pop(cache_key, None)


# ---------------------------------------------------------------------------
# DB path helper
# ---------------------------------------------------------------------------


def _get_db_path(conn: sqlite3.Connection) -> str:
    """Return the absolute path of the primary database attached to *conn*."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


# ---------------------------------------------------------------------------
# Salt persistence
# ---------------------------------------------------------------------------


def _load_or_create_salt(conn: sqlite3.Connection) -> bytes:
    """Read the per-install HKDF salt from the DB, creating it if absent.

    The first-run INSERT uses ``INSERT OR IGNORE`` followed by a re-read.
    Two workers racing to seed the salt will both produce a candidate;
    only one INSERT wins and both then read back the same persisted value.

    Raises:
        CryptoError: If the stored salt is corrupt (cannot be base64-decoded),
            has an unexpected length (indicating possible database tampering),
            or if the salt persisted on first run has an unexpected length
            after the INSERT/re-read cycle.
    """
    cache_key = _get_db_path(conn)
    if cache_key:
        cached = _salt_cache_get(cache_key)
        if cached is not None:
            return cached

    row = conn.execute("SELECT value FROM settings WHERE key=?", (_SALT_SETTING_KEY,)).fetchone()
    if row is not None and row["value"]:
        try:
            decoded = base64.b64decode(row["value"])
        except (ValueError, TypeError) as exc:
            raise CryptoError(f"Stored HKDF salt is corrupt and cannot be decoded: {exc}") from exc
        if len(decoded) != 16:
            raise CryptoError(
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
        raise CryptoError(
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
