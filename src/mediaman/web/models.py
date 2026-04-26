"""Pydantic models for web API request/response validation."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------

#: Compiled once; used by the CRLF validator on every string field.
_CRLF_RE = re.compile(r"[\r\n]")

#: API keys / tokens sent as HTTP headers must only contain ASCII printable
#: characters and must be short enough that no single header overflows a
#: reasonable server limit. NUL is excluded because it terminates C strings.
#: The upper bound is generous enough to accommodate JWT-style tokens —
#: TMDB v4 "API Read Access Tokens" are ~220+ character JWTs.
_API_KEY_RE = re.compile(r"^[\x20-\x7E]{1,1024}$")

#: Allowed URL schemes for service base URLs.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _reject_crlf(v: str | None) -> str | None:
    """Raise ``ValueError`` if *v* contains a CR or LF character.

    These characters can be injected into HTTP headers when the value is
    used as an ``Authorization`` or ``X-Api-Key`` header. Rejecting them
    at the model layer means every code path — not just the settings route
    — is covered.
    """
    if v is not None and _CRLF_RE.search(v):
        raise ValueError("value must not contain CR or LF characters")
    return v


def _validate_api_key(v: str | None) -> str | None:
    """Enforce API-key character set: ASCII printable, length 1–1024.

    Rejects CR, LF, NUL, and non-ASCII so an injected key can never
    corrupt an HTTP header line.  An empty string (used by the UI to
    signal "leave unchanged") is passed through so the route can apply
    its "****" / empty sentinel logic.
    """
    if v is None or v == "" or v == "****":
        return v
    if not _API_KEY_RE.match(v):
        raise ValueError("API key must be 1–1024 ASCII printable characters (no CR, LF, or NUL)")
    return v


def _validate_url(v: str | None) -> str | None:
    """Validate that *v* is an http(s) URL with no CR/LF injection.

    Returns ``None`` if *v* is ``None`` or an empty string, to allow
    callers to clear a URL field. CR/LF rejection is applied before the
    scheme check so a header-injection attempt is never silently
    normalised away.
    """
    if v is None or v == "":
        return v
    _reject_crlf(v)
    if len(v) > 2048:
        raise ValueError("URL must not exceed 2048 characters")
    from urllib.parse import urlparse

    try:
        parsed = urlparse(v)
    except ValueError as exc:
        raise ValueError(f"invalid URL: {exc}") from exc
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError("URL must include a host")
    return v


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


class DiskThresholds(BaseModel):
    """Per-path disk-space warning thresholds, expressed as integer percentages.

    Keys are filesystem paths (e.g. ``"/media"``); values are the usage
    percentage (0–100) at which a warning should be surfaced.
    """

    model_config = ConfigDict(extra="forbid")

    thresholds: dict[str, int] = Field(default_factory=dict)

    @field_validator("thresholds")
    @classmethod
    def validate_thresholds(cls, v: dict[str, int]) -> dict[str, int]:
        for path, pct in v.items():
            _reject_crlf(path)
            if not isinstance(pct, int) or not (0 <= pct <= 100):
                raise ValueError(f"threshold for {path!r} must be an integer in 0–100, got {pct!r}")
        return v


class SettingsUpdate(BaseModel):
    """Full settings update — every key the UI can persist must be declared here.

    ``extra="forbid"`` means an unknown key from the client raises HTTP 422
    rather than being silently dropped. This makes schema drift visible
    immediately instead of causing silent data loss on save.

    All string fields are validated for CR/LF injection (header-injection
    defence). Secret fields (API keys, tokens, passwords) are additionally
    restricted to ASCII printable characters with a length cap of 200.
    URL fields must use http(s) and must have a host component.
    """

    model_config = ConfigDict(extra="forbid")

    # ------------------------------------------------------------------
    # Plex
    # ------------------------------------------------------------------
    plex_url: str | None = None
    plex_public_url: str | None = None
    plex_token: str | None = None
    plex_libraries: list[str] | None = None

    # ------------------------------------------------------------------
    # Sonarr
    # ------------------------------------------------------------------
    sonarr_url: str | None = None
    sonarr_public_url: str | None = None
    sonarr_api_key: str | None = None

    # ------------------------------------------------------------------
    # Radarr
    # ------------------------------------------------------------------
    radarr_url: str | None = None
    radarr_public_url: str | None = None
    radarr_api_key: str | None = None

    # ------------------------------------------------------------------
    # NZBGet
    # ------------------------------------------------------------------
    nzbget_url: str | None = None
    nzbget_public_url: str | None = None
    nzbget_username: str | None = None
    nzbget_password: str | None = None

    # ------------------------------------------------------------------
    # Mailgun
    # ------------------------------------------------------------------
    mailgun_domain: str | None = None
    mailgun_api_key: str | None = None
    mailgun_from_address: str | None = None

    # ------------------------------------------------------------------
    # TMDB
    # ------------------------------------------------------------------
    tmdb_api_key: str | None = None
    tmdb_read_token: str | None = None

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------
    openai_api_key: str | None = None
    openai_web_search_enabled: bool | None = None

    # ------------------------------------------------------------------
    # OMDb
    # ------------------------------------------------------------------
    omdb_api_key: str | None = None

    # ------------------------------------------------------------------
    # General / scheduling
    # ------------------------------------------------------------------
    base_url: str | None = None
    scan_day: str | None = None
    scan_time: str | None = None
    scan_timezone: str | None = None
    library_sync_interval: int | None = None
    min_age_days: int | None = None
    inactivity_days: int | None = None
    grace_days: int | None = None
    dry_run: bool | None = None
    suggestions_enabled: bool | None = None

    # ``disk_thresholds`` is stored as a JSON dict keyed by Plex library
    # id, whose value is a ``{"path": str, "threshold": int}`` config.
    # See ``scanner/runner.py`` for the canonical consumer.  We type the
    # field as ``dict[str, Any]`` and validate the shape in
    # ``validate_disk_thresholds`` below — Pydantic's structural typing
    # would otherwise reject the nested dict at parse time.
    disk_thresholds: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Stuck searches (abandon search feature)
    # ------------------------------------------------------------------
    abandon_search_visible_at: int | None = None
    abandon_search_escalate_at: int | None = None
    abandon_search_auto_multiplier: int | None = None

    # ------------------------------------------------------------------
    # Field-level validators
    # ------------------------------------------------------------------

    @field_validator(
        "plex_url",
        "plex_public_url",
        "sonarr_url",
        "sonarr_public_url",
        "radarr_url",
        "radarr_public_url",
        "nzbget_url",
        "nzbget_public_url",
        "base_url",
        mode="before",
    )
    @classmethod
    def validate_url_fields(cls, v: object) -> object:
        """Validate that URL fields are http(s) and contain no CR/LF."""
        if isinstance(v, str):
            return _validate_url(v)
        return v

    @field_validator(
        "plex_token",
        "sonarr_api_key",
        "radarr_api_key",
        "nzbget_password",
        "mailgun_api_key",
        "tmdb_api_key",
        "tmdb_read_token",
        "openai_api_key",
        "omdb_api_key",
        mode="before",
    )
    @classmethod
    def validate_api_key_fields(cls, v: object) -> object:
        """Restrict secret fields to ASCII printable, max 200 chars, no CR/LF."""
        if isinstance(v, str):
            return _validate_api_key(v)
        return v

    @field_validator(
        "scan_day",
        "scan_time",
        "nzbget_username",
        "mailgun_domain",
        "mailgun_from_address",
        "plex_libraries",
        mode="before",
    )
    @classmethod
    def validate_plain_string_fields(cls, v: object) -> object:
        """Reject CR/LF in plain string fields (header-injection defence)."""
        if isinstance(v, str):
            _reject_crlf(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    _reject_crlf(item)
        return v

    @field_validator("scan_timezone", mode="before")
    @classmethod
    def validate_timezone(cls, v: object) -> object:
        """Validate that *v* is a non-empty IANA timezone name."""
        if v is None:
            return v
        if not isinstance(v, str) or not v.strip():
            raise ValueError("scan_timezone must be a non-empty string")
        _reject_crlf(v)
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(v)
        except (KeyError, Exception) as exc:
            raise ValueError(f"scan_timezone {v!r} is not a valid IANA timezone") from exc
        return v

    @field_validator("library_sync_interval", mode="before")
    @classmethod
    def validate_library_sync_interval(cls, v: object) -> object:
        """Bound library_sync_interval to 0–1440 minutes (0 = disabled, max = 1 day).

        The dropdown in ``_sec_general.html`` writes this value in minutes
        (matching how ``bootstrap/scheduling.py`` consumes it as
        ``sync_interval_minutes``). Earlier revisions of this validator
        treated the value as seconds and rejected every legitimate
        dropdown option — fix is to bound in the unit the rest of the
        codebase actually uses.
        """
        if v is None:
            return v
        try:
            iv = int(v)
        except (TypeError, ValueError) as exc:
            raise ValueError("library_sync_interval must be an integer") from exc
        if not (0 <= iv <= 1440):
            raise ValueError("library_sync_interval must be between 0 and 1440 minutes")
        return iv

    @field_validator("disk_thresholds", mode="before")
    @classmethod
    def validate_disk_thresholds(cls, v: object) -> object:
        """Validate the nested ``{lib_id: {"path": str, "threshold": int}}`` shape.

        Keys are Plex library ids (string-coerced from the integer ids
        Plex returns).  Each entry is a dict with a string ``path`` and
        an integer ``threshold`` in 0–100 (where 0 = scan unconditionally,
        matching the scanner's "fail open" semantics).  Empty values are
        permitted to model "unset" — the user might have selected the
        library but not yet typed a path.
        """
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("disk_thresholds must be a JSON object")
        for lib_id, cfg in v.items():
            if not isinstance(lib_id, str):
                raise ValueError("disk_thresholds keys must be strings")
            _reject_crlf(lib_id)
            if cfg is None:
                continue
            if not isinstance(cfg, dict):
                raise ValueError(
                    f"disk_thresholds entry for library {lib_id!r} must be an object "
                    "with 'path' and 'threshold' keys"
                )
            path = cfg.get("path")
            if path is not None:
                if not isinstance(path, str):
                    raise ValueError(
                        f"disk_thresholds path for library {lib_id!r} must be a string"
                    )
                _reject_crlf(path)
            threshold = cfg.get("threshold")
            if threshold is not None and threshold != "":
                try:
                    pct_int = int(threshold)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"disk_thresholds threshold for library {lib_id!r} must be an integer"
                    ) from exc
                if not (0 <= pct_int <= 100):
                    raise ValueError(
                        f"disk_thresholds threshold for library {lib_id!r} "
                        "must be between 0 and 100"
                    )
        return v


class SubscriberCreate(BaseModel):
    email: str
