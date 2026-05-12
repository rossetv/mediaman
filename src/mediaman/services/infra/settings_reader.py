"""Unified reader for DB-backed settings.

Every route and scanner helper used to re-implement the same
decrypt-then-JSON-unwrap pattern around the ``settings`` table. This
module is the single home for that logic so fixes (e.g. consistent
handling of encrypt/decrypt failures) only need to be made once.
"""

from __future__ import annotations

import binascii
import json
import logging
import os
import sqlite3

from cryptography.exceptions import InvalidTag

from mediaman.crypto import CryptoInputError, decrypt_value

# Type alias for values that json.loads can return.
_JsonValue = str | int | float | bool | list[object] | dict[str, object] | None

logger = logging.getLogger(__name__)


class ConfigDecryptError(Exception):
    """Raised when a setting exists but cannot be decrypted with the supplied *secret_key*.

    Callers that need to distinguish "setting not configured" from "setting
    present but key is wrong" should catch this exception separately from a
    ``None``/default return.

    :param key: the settings-table key that failed to decrypt.
    :param cause: the underlying exception from the crypto layer.
    """

    def __init__(self, key: str, cause: Exception) -> None:
        self.key = key
        super().__init__(f"Failed to decrypt setting '{key}': {cause}")


def get_media_path() -> str:
    """Return the configured media root, defaulting to /media.

    Reads ``MEDIAMAN_MEDIA_PATH`` at call time rather than import time so
    operators can set the env var after process start (e.g. in tests) and
    have it picked up correctly.
    """
    return os.environ.get("MEDIAMAN_MEDIA_PATH", "/media").strip() or "/media"


def get_setting(
    conn: sqlite3.Connection,
    key: str,
    *,
    secret_key: str | None = None,
    default: _JsonValue = "",
) -> _JsonValue:
    """Return the value of *key* from the ``settings`` table.

    - If the row is marked ``encrypted=1`` and ``secret_key`` is provided,
      the value is decrypted first.
    - The resulting string is run through ``json.loads`` so lists/dicts/
      bools round-trip correctly. Plain strings that aren't valid JSON
      are returned as-is.
    - Decryption errors return ``default`` (and log a warning) — the
      likely cause is a rotated secret key, which should not crash the
      whole app.

    Raises :exc:`ConfigDecryptError` when the row is encrypted but no
    ``secret_key`` was supplied. Returning the *default* in that case
    silently hides a deployment misconfiguration: an operator that
    forgot to set their secret key would see all their saved
    credentials disappear with no log entry pointing at the cause.
    Surfacing the error gives the caller a clear failure rather than
    a mysterious "feature stopped working".
    """
    row = conn.execute("SELECT value, encrypted FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return default

    val = row["value"]
    if row["encrypted"]:
        if not secret_key:
            raise ConfigDecryptError(
                key,
                ValueError("encrypted setting requires secret_key — none was supplied"),
            )
        try:
            # Pass ``conn`` so v2 (HKDF) ciphertexts can look up the
            # per-install salt; pass the setting key as AAD so a DB
            # row swap (moving a ciphertext from one key to another)
            # fails authentication instead of silently succeeding.
            # ``decrypt_value`` falls back to no-AAD on InvalidTag so
            # any pre-AAD v2 rows that haven't been upgraded by
            # migrate_legacy_ciphertexts (migration v35) still read.
            val = decrypt_value(
                val,
                secret_key,
                conn=conn,
                aad=key.encode(),
            )
        except (
            sqlite3.OperationalError,
            sqlite3.DatabaseError,
            InvalidTag,
            CryptoInputError,
            binascii.Error,
        ):
            # Narrow exception list:
            # * sqlite3.* — salt lookup failed (corrupted bootstrap
            #   row, locked DB, schema drift)
            # * InvalidTag — wrong key, tampered ciphertext, or
            #   missing AAD (the no-AAD fallback inside decrypt_value
            #   already retried before this fires)
            # * CryptoInputError — malformed ciphertext (empty or
            #   exceeds max length)
            # * binascii.Error — ciphertext is not valid base64 (e.g.
            #   incorrect padding from a truncated or corrupted value)
            #
            # The previous ``except Exception`` swallowed everything
            # including programmer errors (e.g. a typo in the call
            # site that raised AttributeError), making the cause
            # invisible. Anything outside this list now propagates.
            logger.warning("Failed to decrypt setting '%s' — returning default", key)
            return default

    val_str: str = val if isinstance(val, str) else str(val)
    try:
        parsed: _JsonValue = json.loads(val_str)
    except (TypeError, ValueError):
        return val_str
    return parsed


def get_int_setting(
    conn: sqlite3.Connection,
    key: str,
    *,
    default: int,
) -> int:
    """Return an integer setting, falling back to *default* on any error.

    Args:
        conn: Open SQLite connection.
        key: Settings-table key to look up.
        default: Value returned when the key is absent or the stored value
            cannot be coerced to an integer.
    """
    raw = get_setting(conn, key, default=default)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def get_bool_setting(
    conn: sqlite3.Connection,
    key: str,
    *,
    default: bool = True,
) -> bool:
    """Return a boolean setting from the ``settings`` table.

    Treats the stored string 'false', '0', 'no', or 'off' (case-insensitive)
    as ``False``; any other value (including missing rows) returns *default*.
    This avoids the silent 'value != "false"' trap where 'False', '0', or
    'disabled' would incorrectly be treated as truthy.
    """
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return default
    return row["value"].strip().lower() not in ("false", "0", "no", "off")


def get_string_setting(
    conn: sqlite3.Connection,
    key: str,
    *,
    secret_key: str | None = None,
    default: str = "",
) -> str:
    """Return a string setting. Wraps :func:`get_setting` and coerces to str."""
    value = get_setting(conn, key, secret_key=secret_key, default=default)
    if value is None:
        return default
    return str(value) if not isinstance(value, str) else value
