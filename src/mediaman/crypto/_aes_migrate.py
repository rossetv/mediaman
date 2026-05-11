"""Legacy AES ciphertext migration: v1 (SHA-256 key) and v2-no-AAD to v2+AAD (HKDF key)."""

from __future__ import annotations

import base64
import binascii
import collections.abc
import logging
import sqlite3

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mediaman.core.time import now_iso as _now_iso

from ._aes_key import (
    _CANARY_SETTING_KEY,
    _GCM_NONCE_LEN,
    _GCM_TAG_LEN,
    _SALT_SETTING_KEY,
    _V2_PREFIX,
    _derive_aes_key_hkdf,
    _load_or_create_salt,
)

logger = logging.getLogger(__name__)

# Date on which the v1 ciphertext path (SHA-256-derived key, no prefix byte,
# no AAD) was removed from this module. Databases must have run migration v35
# (see :func:`migrate_legacy_ciphertexts`) before this version is deployed.
LEGACY_V1_REMOVED_AT = "2026-05-04"


def _decrypt_v1_raw(raw: bytes, secret_key: str) -> str:
    """Decrypt raw v1 bytes (no prefix byte, SHA-256-derived key, no AAD).

    Private — used only by :func:`migrate_legacy_ciphertexts`. Not part of
    the public :func:`decrypt_value` API; v1 support was removed on
    2026-05-04.

    Raises:
        InvalidTag: if authentication fails (wrong key or corrupt bytes).
    """
    import hashlib

    if len(raw) < _GCM_NONCE_LEN + _GCM_TAG_LEN:
        raise InvalidTag()
    key = hashlib.sha256(secret_key.encode()).digest()
    aesgcm = AESGCM(key)
    nonce, ciphertext = raw[:_GCM_NONCE_LEN], raw[_GCM_NONCE_LEN:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


def migrate_legacy_ciphertexts(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    on_complete: collections.abc.Callable[[int], None] | None = None,
) -> int:
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

    Args:
        conn:        Open SQLite connection.
        secret_key:  Master ``MEDIAMAN_SECRET_KEY``.
        on_complete: Optional callback invoked with the number of re-encrypted
                     rows after a successful commit.  The caller (typically
                     ``bootstrap_crypto`` in ``app_factory``) passes a closure
                     that writes a ``security_event`` audit row.  Keeping the
                     audit-write out of ``crypto/`` preserves the leaf-package
                     invariant (§2.2): ``crypto/`` must not import from
                     ``mediaman.core.audit``.

    Returns:
        Number of rows that were re-encrypted (0 if nothing to do).

    Raises:
        RuntimeError: If the HKDF salt cannot be loaded (corrupt DB).
    """
    # Import here to avoid circular dependency (encrypt_value lives in aes.py
    # which imports from this module).
    from mediaman.crypto.aes import encrypt_value

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
        except (binascii.Error, ValueError):
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
        except sqlite3.Error:
            logger.error(
                "migrate_legacy_ciphertexts: commit failed after %d "
                "re-encryptions — rows were NOT persisted; the migration "
                "will be retried on next startup",
                migrated,
                exc_info=True,
            )
            raise
        if on_complete is not None:
            try:
                on_complete(migrated)
            except Exception:  # pragma: no cover; rationale: best-effort audit callback — never block migration success on audit failure
                logger.exception("aes.v35_migration_complete on_complete callback failed")

    return migrated
