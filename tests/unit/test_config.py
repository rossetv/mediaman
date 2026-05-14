"""Tests for bootstrap configuration."""

from __future__ import annotations

import pytest

from mediaman.config import ConfigError, load_config

_GOOD_KEY = "0123456789abcdef" * 4  # 64 hex chars, 16 unique — passes entropy check


class TestLoadConfig:
    def test_loads_secret_key_from_env(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _GOOD_KEY)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", "/tmp/test")
        cfg = load_config()
        assert cfg.secret_key == _GOOD_KEY

    def test_loads_port_with_default(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _GOOD_KEY)
        cfg = load_config()
        assert cfg.port == 8282

    def test_loads_port_from_env(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _GOOD_KEY)
        monkeypatch.setenv("MEDIAMAN_PORT", "9090")
        cfg = load_config()
        assert cfg.port == 9090

    def test_loads_data_dir_with_default(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _GOOD_KEY)
        cfg = load_config()
        assert cfg.data_dir == "/data"

    def test_raises_without_secret_key(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_SECRET_KEY", raising=False)
        with pytest.raises(ConfigError, match="MEDIAMAN_SECRET_KEY"):
            load_config()

    def test_rejects_short_secret_key(self, monkeypatch):
        """A too-short secret is rejected for insufficient entropy."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "too-short")
        with pytest.raises(ConfigError):
            load_config()

    def test_rejects_low_entropy_32_char_key(self, monkeypatch):
        """A 32-char single-character string is trivially weak and must be rejected."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 32)
        with pytest.raises(ConfigError, match="weak"):
            load_config()

    def test_accepts_strong_hex_key(self, monkeypatch):
        """A 64-char hex key (from secrets.token_hex(32)) is accepted."""
        import secrets

        key = secrets.token_hex(32)
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", key)
        cfg = load_config()
        assert cfg.secret_key == key

    def test_accepts_strong_urlsafe_key(self, monkeypatch):
        """A URL-safe base64 key (from secrets.token_urlsafe(32)) is accepted."""
        import secrets

        key = secrets.token_urlsafe(32)
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", key)
        cfg = load_config()
        assert cfg.secret_key == key

    def test_config_has_no_delete_roots_field(self, monkeypatch):
        """Config no longer carries delete_roots — finding 8.

        ``MEDIAMAN_DELETE_ROOTS`` is read at the deletion call site
        (scanner.repository) so the env var is the single source of
        truth. A stale Config field would imply two sources and risk
        them drifting apart.
        """
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _GOOD_KEY)
        cfg = load_config()
        assert not hasattr(cfg, "delete_roots")

    def test_port_value_error_chained_with_from_exc(self, monkeypatch):
        """ConfigError raised on bad port is chained — finding 10."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _GOOD_KEY)
        monkeypatch.setenv("MEDIAMAN_PORT", "not-a-port")
        with pytest.raises(ConfigError) as excinfo:
            load_config()
        assert excinfo.value.__cause__ is not None
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_rejects_trivial_repetition(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 64)
        with pytest.raises(ConfigError, match="weak"):
            load_config()

    def test_rejects_mediaman_string(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "mediamanmediamanmediamanmediaman")
        with pytest.raises(ConfigError, match="weak"):
            load_config()

    def test_accepts_hex_key(self, monkeypatch):
        import secrets

        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secrets.token_hex(32))
        cfg = load_config()
        assert cfg.secret_key
