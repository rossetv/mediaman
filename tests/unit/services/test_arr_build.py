"""Tests for the Arr client builder helpers."""

import sqlite3

import pytest

from mediaman.crypto import encrypt_value
from mediaman.db import init_db
from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db


SECRET = "test-secret-32-chars-XXXXXXXXXX"


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


def _put(conn, key: str, value: str, encrypted: int = 0) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, '2026-01-01')",
        (key, value, encrypted),
    )
    conn.commit()


class TestBuildRadarr:
    def test_returns_none_when_url_missing(self, conn):
        _put(conn, "radarr_api_key", "key")
        assert build_radarr_from_db(conn, SECRET) is None

    def test_returns_none_when_key_missing(self, conn):
        _put(conn, "radarr_url", "http://radarr.local")
        assert build_radarr_from_db(conn, SECRET) is None

    def test_returns_client_for_plain_key(self, conn):
        _put(conn, "radarr_url", "http://radarr.local")
        _put(conn, "radarr_api_key", "plain-key")
        client = build_radarr_from_db(conn, SECRET)
        assert client is not None

    def test_decrypts_encrypted_key(self, conn):
        _put(conn, "radarr_url", "http://radarr.local")
        _put(conn, "radarr_api_key", encrypt_value("sekret", SECRET, conn=conn), encrypted=1)
        client = build_radarr_from_db(conn, SECRET)
        assert client is not None


class TestBuildSonarr:
    def test_returns_none_without_config(self, conn):
        assert build_sonarr_from_db(conn, SECRET) is None

    def test_returns_client_when_configured(self, conn):
        _put(conn, "sonarr_url", "http://sonarr.local")
        _put(conn, "sonarr_api_key", "plain-key")
        client = build_sonarr_from_db(conn, SECRET)
        assert client is not None
