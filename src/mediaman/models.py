"""Pydantic models for API request/response validation."""

from pydantic import BaseModel, field_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class KeepRequest(BaseModel):
    duration: str

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, v: str) -> str:
        allowed = {"7 days", "30 days", "90 days", "forever"}
        if v not in allowed:
            raise ValueError(f"Duration must be one of: {allowed}")
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
