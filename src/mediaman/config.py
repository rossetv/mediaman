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
    """
    secret_key = os.environ.get("MEDIAMAN_SECRET_KEY")
    if not secret_key or len(secret_key) < 32:
        raise ConfigError(
            "MEDIAMAN_SECRET_KEY must be at least 32 characters. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )

    port = int(os.environ.get("MEDIAMAN_PORT", "8282"))
    data_dir = os.environ.get("MEDIAMAN_DATA_DIR", "/data")

    return Config(secret_key=secret_key, port=port, data_dir=data_dir)
