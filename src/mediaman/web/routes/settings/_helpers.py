"""Settings helper functions."""

from __future__ import annotations

import json

from mediaman.crypto import decrypt_value

SECRET_FIELDS = {
    "plex_token", "sonarr_api_key", "radarr_api_key", "nzbget_password",
    "mailgun_api_key", "tmdb_api_key", "tmdb_read_token", "openai_api_key", "omdb_api_key",
}

_ALL_KEYS = SECRET_FIELDS | {
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
}

#: Internal crypto plumbing rows (HKDF salt, canary) — never shown in the UI.
_INTERNAL_KEYS = {"aes_kdf_salt", "aes_kdf_canary"}


def _load_settings(conn, secret_key: str) -> dict:
    """Return all settings from the DB with secrets decrypted."""
    rows = conn.execute("SELECT key, value, encrypted FROM settings").fetchall()
    settings: dict[str, object] = {}
    for row in rows:
        if row["key"] in _INTERNAL_KEYS:
            continue
        raw = row["value"]
        if row["encrypted"]:
            try:
                settings[row["key"]] = decrypt_value(
                    raw, secret_key, conn=conn, aad=row["key"].encode()
                )
            except Exception:
                settings[row["key"]] = ""
        else:
            try:
                settings[row["key"]] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                settings[row["key"]] = raw
    return settings


def _mask_secrets(settings: dict) -> dict:
    """Return a copy of *settings* with secret fields replaced by '****'."""
    out = dict(settings)
    for key in SECRET_FIELDS:
        if key in out and out[key]:
            out[key] = "****"
    return out
