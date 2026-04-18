"""Public/internal URL resolution for Radarr + Sonarr deep links."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

_KEY = "0123456789abcdef" * 4


def _seed_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) "
        "VALUES (?, ?, 0, ?)",
        (key, value, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _call(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _KEY)
    monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))
    from mediaman.services.download_queue import _arr_base_urls
    return _arr_base_urls(conn)


class TestArrBaseUrls:
    def test_uses_internal_when_public_unset(self, db_path, monkeypatch, tmp_path):
        from mediaman.db import init_db
        conn = init_db(str(db_path))
        _seed_setting(conn, "radarr_url", "http://radarr:7878")
        _seed_setting(conn, "sonarr_url", "http://sonarr:8989")

        out = _call(conn, monkeypatch, tmp_path)
        assert out["radarr"] == "http://radarr:7878"
        assert out["sonarr"] == "http://sonarr:8989"

    def test_prefers_public_url(self, db_path, monkeypatch, tmp_path):
        from mediaman.db import init_db
        conn = init_db(str(db_path))
        _seed_setting(conn, "radarr_url", "http://radarr:7878")
        _seed_setting(conn, "radarr_public_url", "https://radarr.example.com")
        _seed_setting(conn, "sonarr_url", "http://sonarr:8989")
        _seed_setting(conn, "sonarr_public_url", "https://sonarr.example.com/")

        out = _call(conn, monkeypatch, tmp_path)
        assert out["radarr"] == "https://radarr.example.com"
        # Trailing slash is stripped.
        assert out["sonarr"] == "https://sonarr.example.com"

    def test_whitespace_only_public_falls_back(self, db_path, monkeypatch, tmp_path):
        """A blank-with-whitespace public URL is treated as unset."""
        from mediaman.db import init_db
        conn = init_db(str(db_path))
        _seed_setting(conn, "radarr_url", "http://radarr:7878")
        _seed_setting(conn, "radarr_public_url", "   ")

        out = _call(conn, monkeypatch, tmp_path)
        assert out["radarr"] == "http://radarr:7878"
