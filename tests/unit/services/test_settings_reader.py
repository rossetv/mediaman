"""Tests for the unified settings reader."""

import sqlite3

import pytest

from mediaman.crypto import encrypt_value
from mediaman.db import init_db
from mediaman.services.settings_reader import (
    ConfigDecryptError,
    get_int_setting,
    get_setting,
    get_string_setting,
    get_string_setting_strict,
)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


def _put(conn: sqlite3.Connection, key: str, value: str, encrypted: int = 0) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, '2026-01-01')",
        (key, value, encrypted),
    )
    conn.commit()


class TestGetSetting:
    def test_returns_default_when_missing(self, conn):
        assert get_setting(conn, "nope", default="fallback") == "fallback"

    def test_returns_plain_string(self, conn):
        _put(conn, "url", "http://example.com")
        assert get_setting(conn, "url") == "http://example.com"

    def test_parses_json_lists(self, conn):
        _put(conn, "libs", '["1","2","3"]')
        assert get_setting(conn, "libs") == ["1", "2", "3"]

    def test_parses_json_booleans(self, conn):
        _put(conn, "on", "true")
        assert get_setting(conn, "on") is True

    def test_decrypts_encrypted_values(self, conn):
        ct = encrypt_value("secret-key", "test-secret-32-chars-XXXXXXXXXX", conn=conn)
        _put(conn, "api_key", ct, encrypted=1)
        assert get_setting(conn, "api_key", secret_key="test-secret-32-chars-XXXXXXXXXX") == "secret-key"

    def test_returns_default_on_decrypt_failure(self, conn):
        ct = encrypt_value("secret", "right-secret-32-chars-XXXXXXXXXXXXXXX", conn=conn)
        _put(conn, "api_key", ct, encrypted=1)
        assert get_setting(conn, "api_key", secret_key="wrong-secret-32-chars-XXXXXXXXXXXXXXX", default="FB") == "FB"

    def test_missing_secret_for_encrypted_returns_default(self, conn):
        _put(conn, "api_key", "gibberish", encrypted=1)
        assert get_setting(conn, "api_key", default="FB") == "FB"

    def test_empty_value_returns_default(self, conn):
        _put(conn, "blank", "")
        assert get_setting(conn, "blank", default="FB") == "FB"


class TestGetIntSetting:
    def test_returns_int_when_set(self, conn):
        _put(conn, "count", "42")
        assert get_int_setting(conn, "count", default=0) == 42

    def test_falls_back_on_invalid(self, conn):
        _put(conn, "count", "abc")
        assert get_int_setting(conn, "count", default=7) == 7

    def test_falls_back_when_missing(self, conn):
        assert get_int_setting(conn, "nope", default=9) == 9


class TestGetStringSetting:
    def test_coerces_non_string(self, conn):
        _put(conn, "x", "123")
        assert get_string_setting(conn, "x") == "123"

    def test_returns_default_on_missing(self, conn):
        assert get_string_setting(conn, "nope", default="hello") == "hello"


# ---------------------------------------------------------------------------
# H45: ConfigDecryptError and get_string_setting_strict
# ---------------------------------------------------------------------------


class TestConfigDecryptError:
    def test_is_exception_subclass(self):
        exc = ConfigDecryptError("my_key", ValueError("boom"))
        assert isinstance(exc, Exception)

    def test_key_attribute_set(self):
        exc = ConfigDecryptError("radarr_api_key", RuntimeError("bad"))
        assert exc.key == "radarr_api_key"

    def test_message_includes_key(self):
        exc = ConfigDecryptError("sonarr_api_key", ValueError("oops"))
        assert "sonarr_api_key" in str(exc)


class TestGetStringSettingStrict:
    def test_returns_none_when_key_missing(self, conn):
        assert get_string_setting_strict(conn, "nonexistent") is None

    def test_returns_none_when_value_empty(self, conn):
        _put(conn, "blank", "")
        assert get_string_setting_strict(conn, "blank") is None

    def test_returns_plain_string(self, conn):
        _put(conn, "url", "http://radarr.local")
        assert get_string_setting_strict(conn, "url") == "http://radarr.local"

    def test_returns_decrypted_value(self, conn):
        ct = encrypt_value("my-secret", "test-secret-32-chars-XXXXXXXXXX", conn=conn)
        _put(conn, "api_key", ct, encrypted=1)
        result = get_string_setting_strict(
            conn, "api_key", secret_key="test-secret-32-chars-XXXXXXXXXX"
        )
        assert result == "my-secret"

    def test_raises_config_decrypt_error_on_wrong_key(self, conn):
        ct = encrypt_value("secret", "right-secret-32-chars-XXXXXXXXXXXXXXX", conn=conn)
        _put(conn, "api_key", ct, encrypted=1)
        with pytest.raises(ConfigDecryptError) as exc_info:
            get_string_setting_strict(
                conn, "api_key", secret_key="wrong-secret-32-chars-XXXXXXXXXXXXXXX"
            )
        assert exc_info.value.key == "api_key"

    def test_returns_none_for_encrypted_row_without_secret_key(self, conn):
        _put(conn, "api_key", "cipher", encrypted=1)
        assert get_string_setting_strict(conn, "api_key") is None

    def test_json_boolean_coerced_to_string(self, conn):
        _put(conn, "flag", "true")
        result = get_string_setting_strict(conn, "flag")
        assert result == "True"
