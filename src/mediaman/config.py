"""Bootstrap configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Bootstrap configuration — loaded once at startup from env vars.

    All values are validated at construction time so the app fails loudly
    on startup rather than silently misbehaving at runtime.

    ``bind_host`` is intentionally an empty string by default — meaning
    "no operator override." :func:`mediaman.main.cli_main` then calls
    :func:`mediaman.main._resolve_bind_host` so the bind address can be
    Docker-aware (``0.0.0.0`` inside a container, ``127.0.0.1`` on bare
    metal). Operators who want a specific address must set
    ``MEDIAMAN_BIND_HOST`` explicitly.

    ``data_dir`` defaults to ``/data`` because the canonical deployment
    target is the container image (the Dockerfile owns ``/data`` and
    ``VOLUME``-declares it). On bare-metal installs the default will fail
    on most distros — operators must set ``MEDIAMAN_DATA_DIR`` to a
    directory the runtime user can write.

    ``MEDIAMAN_DELETE_ROOTS`` is read directly by the deletion-time call
    site in :mod:`mediaman.scanner.repository` (it is not threaded
    through this dataclass). Adding a stale ``delete_roots`` field here
    would imply a single source of truth that does not exist in practice
    and risks divergence between this snapshot and the live env var on
    each deletion.
    """

    secret_key: str
    port: int = 8282
    data_dir: str = "/data"
    bind_host: str = ""
    trusted_proxies: str = ""


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

    from mediaman.crypto._aes_key import _is_secret_key_strong

    if not _is_secret_key_strong(secret_key):
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
    except ValueError as exc:
        raise ConfigError(f"MEDIAMAN_PORT must be an integer, got {raw_port!r}") from exc
    if not (1 <= port <= 65535):
        raise ConfigError(f"MEDIAMAN_PORT must be between 1 and 65535, got {port}")

    # ── Data directory ────────────────────────────────────────────────────────
    data_dir = os.environ.get("MEDIAMAN_DATA_DIR", "/data").strip()
    if not data_dir:
        raise ConfigError("MEDIAMAN_DATA_DIR must not be empty")

    # ── Bind host ─────────────────────────────────────────────────────────────
    # Empty string means "unset" — let cli_main fall through to
    # _resolve_bind_host() which is Docker-aware. That way a fresh
    # container deployment binds to 0.0.0.0 (the only address reachable
    # via the published port) rather than wedging on 127.0.0.1 and
    # presenting a "healthy but unreachable" service.
    bind_host = os.environ.get("MEDIAMAN_BIND_HOST", "").strip()

    # ── Trusted proxies ───────────────────────────────────────────────────────
    trusted_proxies = os.environ.get("MEDIAMAN_TRUSTED_PROXIES", "").strip()

    # ``MEDIAMAN_DELETE_ROOTS`` is intentionally not snapshotted here —
    # it is read on demand at deletion time in
    # :mod:`mediaman.scanner.repository`. See the Config docstring.

    return Config(
        secret_key=secret_key,
        port=port,
        data_dir=data_dir,
        bind_host=bind_host,
        trusted_proxies=trusted_proxies,
    )
