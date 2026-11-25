"""Settings secrets: the canonical secret, sensitive and all-known key sets, the "unchanged"
and "delete" write sentinels, and pure masking helpers. Nothing here decrypts, encrypts or
touches the DB, so it is safe to import anywhere. Reads of the settings table live in
mediaman.web.repository.settings.
"""

from __future__ import annotations

from mediaman.web.repository.settings import (
    INTERNAL_KEYS as INTERNAL_KEYS,
)
from mediaman.web.repository.settings import (
    SECRET_CLEAR_SENTINEL as SECRET_CLEAR_SENTINEL,
)
from mediaman.web.repository.settings import (
    SECRET_FIELDS as SECRET_FIELDS,
)
from mediaman.web.repository.settings import (
    SECRET_PLACEHOLDER as SECRET_PLACEHOLDER,
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
        "openai_model",
        "ai_questions_per_day",
        "ai_everyone_per_day",
        "auto_abandon_enabled",
        "older_tvs_enabled",
        "older_tvs_http",
        "google_cast_enabled",
        "tv_address",
    }
)

#: Settings keys that require a recent-reauth ticket before they can be
#: written: credentials, where mediaman connects or sends mail, what TVs may
#: fetch from it, whether Google's script may run in people's browsers, and
#: the settings that decide when media is deleted.
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
        "min_age_days",
        "inactivity_days",
        "grace_days",
        "dry_run",
        "plex_libraries",
        "disk_thresholds",
        "older_tvs_enabled",
        "older_tvs_http",
        "google_cast_enabled",
        "tv_address",
    }
    | SECRET_FIELDS
)


def has_sensitive_key_changes(body: dict[str, object]) -> bool:
    """Return True when body attempts to write any sensitive key. Secret fields holding the
    unchanged sentinel (****) or an empty string are skipped, as the PUT handler ignores
    them too, so a no-op write does not demand a reauth. SECRET_CLEAR_SENTINEL is not
    skipped: deleting a credential is a sensitive change.
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


def mask_secrets(settings: dict[str, object]) -> dict[str, object]:
    out = dict(settings)
    for key in SECRET_FIELDS:
        if out.get(key):
            out[key] = SECRET_PLACEHOLDER
    return out


def mask_encrypted_keys(settings: dict[str, object], enc_keys: set[str]) -> dict[str, object]:
    """Return a copy of settings with every encrypted-on-disk key showing '****'. Unlike
    mask_secrets it takes a precomputed key set, so GET /api/settings need not decrypt
    secrets only to discard the plaintext.
    """
    out = dict(settings)
    for key in enc_keys & SECRET_FIELDS:
        out[key] = SECRET_PLACEHOLDER
    return out
