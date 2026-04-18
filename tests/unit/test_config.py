"""Tests for bootstrap configuration."""

import os

import pytest

from mediaman.config import load_config, ConfigError


class TestLoadConfig:
    def test_loads_secret_key_from_env(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", "/tmp/test")
        cfg = load_config()
        assert cfg.secret_key == "a" * 64

    def test_loads_port_with_default(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 64)
        cfg = load_config()
        assert cfg.port == 8282

    def test_loads_port_from_env(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("MEDIAMAN_PORT", "9090")
        cfg = load_config()
        assert cfg.port == 9090

    def test_loads_data_dir_with_default(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 64)
        cfg = load_config()
        assert cfg.data_dir == "/data"

    def test_raises_without_secret_key(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_SECRET_KEY", raising=False)
        with pytest.raises(ConfigError, match="MEDIAMAN_SECRET_KEY"):
            load_config()

    def test_rejects_short_secret_key(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "too-short")
        with pytest.raises(ConfigError, match="at least 32"):
            load_config()

    def test_accepts_32_char_key(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 32)
        cfg = load_config()
        assert len(cfg.secret_key) == 32
