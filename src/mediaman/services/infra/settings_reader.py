"""Unified reader for DB-backed settings.

Every route and scanner helper used to re-implement the same
decrypt-then-JSON-unwrap pattern around the ``settings`` table. This
module is the single home for that logic so fixes (e.g. consistent
handling of encrypt/decrypt failures) only need to be made once.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from mediaman.crypto import decrypt_value

logger = logging.getLogger("mediaman")


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
    default: Any = "",
) -> Any:
    """Return the value of *key* from the ``settings`` table.

    - If the row is marked ``encrypted=1`` and ``secret_key`` is provided,
      the value is decrypted first.
    - The resulting string is run through ``json.loads`` so lists/dicts/
      bools round-trip correctly. Plain strings that aren't valid JSON
      are returned as-is.
    - Decryption errors return ``default`` (and log a warning) — the
      likely cause is a rotated secret key, which should not crash the
      whole app.
    """
    row = conn.execute("SELECT value, encrypted FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return default

    val = row["value"]
    if row["encrypted"]:
        if not secret_key:
            return default
        try:
            # Pass ``conn`` so v2 (HKDF) ciphertexts can look up the
            # per-install salt; pass the setting key as AAD so a DB
            # row swap (moving a ciphertext from one key to another)
            # fails authentication instead of silently succeeding.
            # ``decrypt_value`` falls back to no-AAD on InvalidTag so
            # pre-AAD ciphertexts still read.
            val = decrypt_value(val, secret_key, conn=conn, aad=key.encode())
        except Exception:
            logger.warning("Failed to decrypt setting '%s' — returning default", key)
            return default

    try:
        parsed = json.loads(val)
    except (TypeError, ValueError):
        return val
    return parsed


def get_int_setting(
    conn: sqlite3.Connection,
    key: str,
    *,
    default: int,
    min: int | None = None,
    max: int | None = None,
) -> int:
    """Return an integer setting, falling back to *default* on any error.

    Args:
        conn: Open SQLite connection.
        key: Settings-table key to look up.
        default: Value returned when the key is absent or the stored value
            cannot be coerced to an integer.
        min: When supplied, the returned value is clamped to this lower bound.
            A stored value below ``min`` is silently raised to ``min``.
        max: When supplied, the returned value is clamped to this upper bound.
            A stored value above ``max`` is silently lowered to ``max``.
    """
    raw = get_setting(conn, key, default=default)
    try:
        result = int(raw)
    except (TypeError, ValueError):
        return default
    if min is not None and result < min:
        result = min
    if max is not None and result > max:
        result = max
    return result


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


class ConfigDecryptError(Exception):
    """Raised by :func:`get_string_setting_strict` when a setting exists but
    cannot be decrypted with the supplied *secret_key*.

    Callers that need to distinguish "setting not configured" from "setting
    present but key is wrong" should catch this exception separately from a
    ``None`` return.

    :param key: the settings-table key that failed to decrypt.
    :param cause: the underlying exception from the crypto layer.
    """

    def __init__(self, key: str, cause: Exception) -> None:
        self.key = key
        super().__init__(f"Failed to decrypt setting '{key}': {cause}")


def get_string_setting_strict(
    conn: sqlite3.Connection,
    key: str,
    *,
    secret_key: str | None = None,
) -> str | None:
    """Return a string setting, distinguishing *missing* from *undecryptable*.

    Unlike :func:`get_string_setting`, this function raises
    :exc:`ConfigDecryptError` instead of returning the ``default`` when the
    setting row is present but cannot be decrypted.  This lets callers show
    the user a meaningful error banner rather than silently acting as if the
    setting was never saved.

    Returns ``None`` when:
    - the key is absent from the ``settings`` table, or
    - the row value is empty/``None``, or
    - the row is marked encrypted but no ``secret_key`` was provided.

    Raises :exc:`ConfigDecryptError` when the row is encrypted, a
    ``secret_key`` is provided, but decryption fails (e.g. rotated key).
    """
    row = conn.execute("SELECT value, encrypted FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return None

    val = row["value"]
    if row["encrypted"]:
        if not secret_key:
            return None
        try:
            val = decrypt_value(val, secret_key, conn=conn, aad=key.encode())
        except Exception as exc:
            raise ConfigDecryptError(key, exc) from exc

    try:
        parsed = json.loads(val)
        return str(parsed) if not isinstance(parsed, str) else parsed
    except (TypeError, ValueError):
        return val
