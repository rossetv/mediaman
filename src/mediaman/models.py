"""Pydantic models for API request/response validation."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Action type constants — canonical string values stored in scheduled_actions
# ---------------------------------------------------------------------------

ACTION_PROTECTED_FOREVER = "protected_forever"
ACTION_SNOOZED = "snoozed"
ACTION_SCHEDULED_DELETION = "scheduled_deletion"

# ---------------------------------------------------------------------------
# Keep duration vocabulary — maps canonical long-form label to days (None = forever)
# ---------------------------------------------------------------------------

VALID_KEEP_DURATIONS: dict[str, int | None] = {
    "7 days": 7,
    "30 days": 30,
    "90 days": 90,
    "forever": None,
}


class LoginRequest(BaseModel):
    username: str
    password: str


class KeepRequest(BaseModel):
    duration: str

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, v: str) -> str:
        if v not in VALID_KEEP_DURATIONS:
            raise ValueError(f"Duration must be one of: {set(VALID_KEEP_DURATIONS)}")
        return v


class SettingsUpdate(BaseModel):
    """Partial settings update — only provided keys are changed."""

    plex_url: str | None = None
    plex_token: str | None = None
    plex_libraries: list[str] | None = None
    sonarr_url: str | None = None
    sonarr_api_key: str | None = None
    radarr_url: str | None = None
    radarr_api_key: str | None = None
    nzbget_url: str | None = None
    nzbget_username: str | None = None
    nzbget_password: str | None = None
    mailgun_domain: str | None = None
    mailgun_api_key: str | None = None
    mailgun_from_address: str | None = None
    base_url: str | None = None
    scan_day: str | None = None
    scan_time: str | None = None
    scan_timezone: str | None = None
    min_age_days: int | None = None
    inactivity_days: int | None = None
    grace_days: int | None = None
    dry_run: bool | None = None


class SubscriberCreate(BaseModel):
    email: str
