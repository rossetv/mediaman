"""Settings secrets — field sets, masking helpers, and sentinel constants.

Owns:
- The canonical sets of secret, sensitive, and all-known settings keys.
- Sentinel values for "unchanged" and "delete" secret writes.
- Pure helper functions for masking secret fields and reading the
  encrypted-key set from the database — no network, no crypto.

Nothing in this module performs decryption or writes to the DB, so
it is safe to import from anywhere without side effects.
"""

from __future__ import annotations

import sqlite3

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

ALL_KEYS: frozenset[str] = SECRET_FIELDS | frozenset(
    {
        "plex_url",
        "plex_public_url",
        "plex_libraries",
        "sonarr_url",
        "sonarr_public_url",
        "radarr_url",
        "radarr_public_url",
        "nzbget_url",
        "nzbget_public_url",
        "nzbget_username",
        "mailgun_domain",
        "mailgun_from_address",
        "base_url",
        "scan_day",
        "scan_time",
        "scan_timezone",
        "library_sync_interval",
        "min_age_days",
        "inactivity_days",
        "grace_days",
        "dry_run",
        "disk_thresholds",
        "suggestions_enabled",
        "openai_web_search_enabled",
        "auto_abandon_enabled",
    }
)

#: Internal crypto plumbing rows (HKDF salt, canary) — never shown in the UI.
INTERNAL_KEYS: frozenset[str] = frozenset({"aes_kdf_salt", "aes_kdf_canary"})

#: Settings keys that require a recent-reauth ticket before they can be
#: written. See the main module docstring for the membership rule.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "plex_url",
        "plex_public_url",
        "sonarr_url",
        "sonarr_public_url",
        "radarr_url",
        "radarr_public_url",
        "nzbget_url",
        "nzbget_public_url",
        "nzbget_username",
        "mailgun_domain",
        "mailgun_from_address",
        "base_url",
    }
    | SECRET_FIELDS
)


def touches_sensitive_keys(body: dict) -> bool:
    """Return True when *body* attempts to write any sensitive key.

    Secret fields whose value is the unchanged sentinel (``****``) or an
    empty string are skipped because the PUT handler ignores them too —
    a no-op write should not demand a fresh reauth. The explicit
    :data:`SECRET_CLEAR_SENTINEL` is NOT skipped: deleting a stored
    credential is a sensitive change.
    """
    for key, value in body.items():
        if key not in SENSITIVE_KEYS:
            continue
        if key in SECRET_FIELDS and (value == SECRET_PLACEHOLDER or value == ""):
            continue
        if value is None:
            continue
        return True
    return False


def encrypted_keys(conn: sqlite3.Connection) -> set[str]:
    """Return the set of keys in the ``settings`` table that are stored encrypted.

    Used by the masking layer of GET /api/settings so we never pay the
    cost of decrypting a secret just to immediately mask it. The
    distinction "is this key encrypted on disk?" is enough — we don't
    need the plaintext.
    """
    return {
        row["key"] for row in conn.execute("SELECT key FROM settings WHERE encrypted=1").fetchall()
    }


def mask_secrets(settings: dict[str, object]) -> dict[str, object]:
    """Return a copy of *settings* with secret fields replaced by '****'."""
    out = dict(settings)
    for key in SECRET_FIELDS:
        if out.get(key):
            out[key] = SECRET_PLACEHOLDER
    return out


def mask_encrypted_keys(settings: dict[str, object], enc_keys: set[str]) -> dict[str, object]:
    """Return a copy of *settings* with every encrypted-on-disk key showing '****'.

    Unlike :func:`mask_secrets`, this does not require the plaintext to
    have been read — the caller passes a pre-computed set of keys that
    are encrypted in the DB.  Used by GET /api/settings to avoid
    decrypting secrets just to immediately throw the plaintext away.
    """
    out = dict(settings)
    for key in enc_keys & SECRET_FIELDS:
        out[key] = SECRET_PLACEHOLDER
    return out
