"""Bootstrap configuration from environment variables."""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Bootstrap configuration — loaded once at startup from env vars.

    All values are validated at construction time so the app fails loudly
    on startup rather than silently misbehaving at runtime.
    """

    secret_key: str
    port: int = 8282
    data_dir: str = "/data"
    bind_host: str = "127.0.0.1"
    trusted_proxies: str = ""
    delete_roots: str = ""


def load_config() -> Config:
    """Load and validate configuration from environment variables.

    Only bootstrap config lives here. All other settings
    (Plex, Sonarr, Radarr, etc.) are stored in the database
    and managed via the Settings UI.

    ``MEDIAMAN_SECRET_KEY`` must pass a minimum-entropy check — the
    value is the root of every downstream security property (AES key
    derivation, HMAC token signing, session cookie integrity), so
    accepting a low-entropy input here would silently invalidate
    every other guarantee in the app.

    Raises :class:`ConfigError` with a clear message on any invalid value
    so operators see an actionable error rather than a cryptic crash.
    """
    secret_key = os.environ.get("MEDIAMAN_SECRET_KEY")
    if not secret_key:
        raise ConfigError(
            "MEDIAMAN_SECRET_KEY must be set. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )

    from mediaman.crypto import _secret_key_looks_strong

    if not _secret_key_looks_strong(secret_key):
        raise ConfigError(
            "MEDIAMAN_SECRET_KEY looks weak. Use at least 64 hex chars "
            "(256 bits from secrets.token_hex(32)) or 43+ URL-safe "
            "base64 chars. Generate one with: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )

    # ── Port ──────────────────────────────────────────────────────────────────
    raw_port = os.environ.get("MEDIAMAN_PORT", "8282").strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigError(f"MEDIAMAN_PORT must be an integer, got {raw_port!r}")
    if not (1 <= port <= 65535):
        raise ConfigError(f"MEDIAMAN_PORT must be between 1 and 65535, got {port}")

    # ── Data directory ────────────────────────────────────────────────────────
    data_dir = os.environ.get("MEDIAMAN_DATA_DIR", "/data").strip()
    if not data_dir:
        raise ConfigError("MEDIAMAN_DATA_DIR must not be empty")

    # ── Bind host ─────────────────────────────────────────────────────────────
    bind_host = os.environ.get("MEDIAMAN_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"

    # ── Trusted proxies ───────────────────────────────────────────────────────
    trusted_proxies = os.environ.get("MEDIAMAN_TRUSTED_PROXIES", "").strip()

    # ── Delete roots ──────────────────────────────────────────────────────────
    delete_roots = os.environ.get("MEDIAMAN_DELETE_ROOTS", "").strip()

    return Config(
        secret_key=secret_key,
        port=port,
        data_dir=data_dir,
        bind_host=bind_host,
        trusted_proxies=trusted_proxies,
        delete_roots=delete_roots,
    )
