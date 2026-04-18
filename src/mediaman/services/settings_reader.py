"""Unified reader for DB-backed settings.

Every route and scanner helper used to re-implement the same
decrypt-then-JSON-unwrap pattern around the ``settings`` table. This
module is the single home for that logic so fixes (e.g. consistent
handling of encrypt/decrypt failures) only need to be made once.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

logger = logging.getLogger("mediaman")


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
    row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key=?", (key,)
    ).fetchone()
    if row is None or row["value"] in (None, ""):
        return default

    val = row["value"]
    if row["encrypted"]:
        if not secret_key:
            return default
        try:
            from mediaman.crypto import decrypt_value
            # Pass ``conn`` so v2 (HKDF) ciphertexts can look up the
            # per-install salt; pass the setting key as AAD so a DB
            # row swap (moving a ciphertext from one key to another)
            # fails authentication instead of silently succeeding.
            # ``decrypt_value`` falls back to no-AAD on InvalidTag so
            # pre-AAD ciphertexts still read.
            val = decrypt_value(
                val, secret_key, conn=conn, aad=key.encode()
            )
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
    default: int,
) -> int:
    """Return an integer setting, falling back to *default* on any error."""
    raw = get_setting(conn, key, default=default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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
