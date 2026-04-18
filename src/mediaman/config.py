"""Bootstrap configuration from environment variables."""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Config:
    """Bootstrap configuration — loaded once at startup from env vars."""

    secret_key: str
    port: int = 8282
    data_dir: str = "/data"


def load_config() -> Config:
    """Load configuration from environment variables.

    Only bootstrap config lives here. All other settings
    (Plex, Sonarr, Radarr, etc.) are stored in the database
    and managed via the Settings UI.

    ``MEDIAMAN_SECRET_KEY`` must pass a minimum-entropy check — the
    value is the root of every downstream security property (AES key
    derivation, HMAC token signing, session cookie integrity), so
    accepting a low-entropy input here would silently invalidate
    every other guarantee in the app.
    """
    secret_key = os.environ.get("MEDIAMAN_SECRET_KEY")
    if not secret_key:
        raise ConfigError(
            "MEDIAMAN_SECRET_KEY must be set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )

    from mediaman.crypto import _secret_key_looks_strong

    if not _secret_key_looks_strong(secret_key):
        raise ConfigError(
            "MEDIAMAN_SECRET_KEY looks weak. Use at least 64 hex chars "
            "(256 bits from secrets.token_hex(32)) or 43+ URL-safe "
            "base64 chars. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )

    port = int(os.environ.get("MEDIAMAN_PORT", "8282"))
    data_dir = os.environ.get("MEDIAMAN_DATA_DIR", "/data")

    return Config(secret_key=secret_key, port=port, data_dir=data_dir)
