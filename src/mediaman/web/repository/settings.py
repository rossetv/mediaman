"""Repository functions for the ``settings`` table (web-side).

Encapsulates every ``conn.execute`` against the ``settings`` table that
the web route layer needs. The route handlers should never reach into
the ``settings`` table directly — they call these functions instead,
which means the encrypt-on-write / decrypt-on-read dance happens at the
storage boundary as required by §9.9.

The scanner has its own reader at
:mod:`mediaman.scanner.repository.settings` for the narrow keys it
consumes — that module deliberately stays small. This module is the
counterpart for the settings UI, which writes the full key set and
reads it back with secrets decrypted.
"""

from __future__ import annotations

import binascii
import json
import logging
import sqlite3
from collections.abc import Iterable
from typing import TypedDict

from cryptography.exceptions import InvalidTag

from mediaman.crypto import CryptoInputError, decrypt_value, encrypt_value
from mediaman.services.infra import ConfigDecryptError

#: Sentinel value displayed in the UI and sent back when a secret field is
#: unchanged — never persisted to the database.
SECRET_PLACEHOLDER = "****"

#: Explicit "delete this row" sentinel for secret fields. The previous
#: design conflated "" (no-op) with "clear" — once a secret was stored,
#: the UI had no way to delete it without falling back to direct DB
#: surgery. Sending this sentinel deletes the row.
SECRET_CLEAR_SENTINEL = "__CLEAR__"

SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "plex_token",
        "sonarr_api_key",
        "radarr_api_key",
        "nzbget_password",
        "mailgun_api_key",
        "tmdb_api_key",
        "tmdb_read_token",
        "openai_api_key",
        "omdb_api_key",
    }
)

#: Internal crypto plumbing rows (HKDF salt, canary) — never shown in the UI.
INTERNAL_KEYS: frozenset[str] = frozenset({"aes_kdf_salt", "aes_kdf_canary"})

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def fetch_encrypted_key_set(conn: sqlite3.Connection) -> set[str]:
    """Return the set of keys in the ``settings`` table that are stored encrypted.

    Used by the masking layer of GET /api/settings so we never pay the
    cost of decrypting a secret just to immediately mask it. The
    distinction "is this key encrypted on disk?" is enough — we don't
    need the plaintext.
    """
    rows = conn.execute("SELECT key FROM settings WHERE encrypted=1").fetchall()
    return {row["key"] for row in rows}


def load_settings(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    keys: set[str] | None = None,
) -> dict[str, object]:
    """Return settings from the DB with secrets decrypted.

    When *keys* is supplied, only those rows are read and decrypted. The
    api_test_service flow uses this so a single-service test does NOT
    decrypt every other secret — minimising the blast radius if any one
    decryption is logged or panics. When *keys* is ``None`` (the default)
    every non-internal row is loaded as before.

    Decryption errors are distinguished from "no value set":

    * If the row exists and is marked encrypted, but decryption fails,
      we raise :class:`ConfigDecryptError` so callers can show a
      meaningful banner instead of silently substituting ``""`` (which
      was previously indistinguishable from a never-saved key — a
      regression hazard once an operator rotates ``MEDIAMAN_SECRET_KEY``).
    * If the row simply does not exist, the key is absent from the
      returned dict (callers already use ``.get(key, "")``).
    """
    if keys is not None:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT key, value, encrypted FROM settings WHERE key IN ({placeholders})",
            tuple(keys),
        ).fetchall()
    else:
        rows = conn.execute("SELECT key, value, encrypted FROM settings").fetchall()
    settings: dict[str, object] = {}
    for row in rows:
        if row["key"] in INTERNAL_KEYS:
            continue
        raw = row["value"]
        if row["encrypted"]:
            try:
                settings[row["key"]] = decrypt_value(
                    raw, secret_key, conn=conn, aad=row["key"].encode()
                )
            # rationale: tightened to the exact exception set raised by
            # the AES-GCM decrypt path — InvalidTag for AAD/ciphertext
            # corruption, CryptoInputError for malformed length / empty
            # ciphertext, binascii.Error for base64 decode failure.
            # A broader ``except Exception`` would swallow programmer
            # bugs (TypeError, AttributeError) in the same banner.
            except (InvalidTag, CryptoInputError, binascii.Error) as exc:
                logger.warning(
                    "Failed to decrypt setting %r — surfacing error to caller",
                    row["key"],
                )
                raise ConfigDecryptError(row["key"], exc) from exc
        else:
            try:
                settings[row["key"]] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                settings[row["key"]] = raw
    return settings


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class _AuditKwargs(TypedDict, total=False):
    """Keyword arguments forwarded to :func:`~mediaman.core.audit.security_event_or_raise`."""

    event: str
    actor: str
    ip: str
    detail: dict[str, object] | str | None


def write_settings(
    conn: sqlite3.Connection,
    *,
    body_dict: dict[str, object],
    allowed_keys: Iterable[str],
    secret_key: str,
    now: str,
    audit: _AuditKwargs | None = None,
) -> None:
    """Persist a settings payload atomically with an optional audit row.

    The full mutation runs inside a single ``with conn:`` block that
    acquires ``BEGIN IMMEDIATE`` first so concurrent writers don't race
    on the secret-encryption-then-insert read-modify-write. Encryption
    is performed inside the block so plaintext never leaks back to the
    caller; only ciphertext lands on disk (§9.9). When *audit* is
    supplied, the row is appended via :func:`security_event_or_raise`
    inside the same transaction so any audit failure rolls the entire
    settings write back (M27 fail-closed contract).

    The shape of *audit* mirrors the keyword arguments of
    :func:`mediaman.core.audit.security_event_or_raise` (``event``, ``actor``,
    ``ip``, ``detail``).

    Behaviour for each known key:

    * Secret fields whose value is :data:`SECRET_PLACEHOLDER` or empty
      are skipped (no-op write — preserves the existing on-disk value).
    * Secret fields whose value equals :data:`SECRET_CLEAR_SENTINEL`
      cause the row to be deleted.
    * All other values are JSON-encoded for the non-secret rows and
      stored as plaintext.

    Keys not in *allowed_keys* are silently ignored — the caller decides
    which keys are part of the public schema and which are not.
    """
    # rationale: this function performs encrypt-on-write for every secret
    # field inside the same transaction as the audit insert; splitting
    # the secret/non-secret branches into separate helpers would either
    # require two passes over body_dict (extra work + duplicated key
    # filtering) or a third helper that takes both halves. Keeping the
    # body short (one branch per kind) preserves the §9.9 contract that
    # plaintext never escapes the repository.
    allowed = set(allowed_keys)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for key, value in body_dict.items():
            if key not in allowed or value is None:
                continue
            if key in SECRET_FIELDS:
                if value == SECRET_PLACEHOLDER or value == "":
                    continue
                if value == SECRET_CLEAR_SENTINEL:
                    conn.execute("DELETE FROM settings WHERE key=?", (key,))
                    continue
                encrypted_value = encrypt_value(str(value), secret_key, conn=conn, aad=key.encode())
                conn.execute(
                    "INSERT INTO settings (key, value, encrypted, updated_at) "
                    "VALUES (?, ?, 1, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "encrypted=1, updated_at=excluded.updated_at",
                    (key, encrypted_value, now),
                )
            else:
                str_value = (
                    json.dumps(value) if isinstance(value, list | dict | bool) else str(value)
                )
                conn.execute(
                    "INSERT INTO settings (key, value, encrypted, updated_at) "
                    "VALUES (?, ?, 0, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "encrypted=0, updated_at=excluded.updated_at",
                    (key, str_value, now),
                )
        if audit is not None:
            from mediaman.core.audit import security_event_or_raise

            security_event_or_raise(conn, **audit)
