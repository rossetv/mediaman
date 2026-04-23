"""Tests for Pydantic models."""

import pytest

from mediaman.models import KeepRequest, LoginRequest, SettingsUpdate


class TestKeepRequest:
    def test_valid_duration(self):
        req = KeepRequest(duration="7 days")
        assert req.duration == "7 days"

    def test_forever(self):
        req = KeepRequest(duration="forever")
        assert req.duration == "forever"

    def test_invalid_duration_raises(self):
        with pytest.raises(Exception):
            KeepRequest(duration="invalid")


class TestLoginRequest:
    def test_valid(self):
        req = LoginRequest(username="admin", password="test1234")
        assert req.username == "admin"


class TestSettingsUpdate:
    def test_partial_update(self):
        update = SettingsUpdate(plex_url="http://plex:32400")
        assert update.plex_url == "http://plex:32400"
        assert update.sonarr_url is None

    def test_all_none_by_default(self):
        update = SettingsUpdate()
        dumped = update.model_dump(exclude_none=True)
        assert dumped == {}

    # ------------------------------------------------------------------
    # C11: extra="forbid" — unknown keys must raise HTTP 422
    # ------------------------------------------------------------------

    def test_settings_update_rejects_unknown_keys(self):
        """Unknown fields raise ValidationError, not silent drop."""
        with pytest.raises(Exception) as exc_info:
            SettingsUpdate(not_a_real_field="oops")
        # Pydantic surfaces this as ValidationError; the message must
        # mention the offending field.
        assert "not_a_real_field" in str(exc_info.value)

    def test_settings_update_rejects_multiple_unknown_keys(self):
        with pytest.raises(Exception) as exc_info:
            SettingsUpdate(foo="bar", baz=123)
        err = str(exc_info.value)
        # At least one unknown key must be named in the error.
        assert "foo" in err or "baz" in err

    # ------------------------------------------------------------------
    # CR/LF injection defence
    # ------------------------------------------------------------------

    def test_settings_update_rejects_crlf_in_strings(self):
        """CR or LF in any string field must be rejected."""
        with pytest.raises(Exception):
            SettingsUpdate(scan_day="Mon\r\nEvil: injected")

    def test_rejects_crlf_in_scan_time(self):
        with pytest.raises(Exception):
            SettingsUpdate(scan_time="08:00\nX-Header: evil")

    def test_rejects_crlf_in_nzbget_username(self):
        with pytest.raises(Exception):
            SettingsUpdate(nzbget_username="user\revil")

    def test_rejects_crlf_in_mailgun_domain(self):
        with pytest.raises(Exception):
            SettingsUpdate(mailgun_domain="example.com\nBcc: attacker@evil.com")

    def test_rejects_crlf_in_mailgun_from_address(self):
        with pytest.raises(Exception):
            SettingsUpdate(mailgun_from_address="no@example.com\r\nBcc: evil@evil.com")

    def test_rejects_crlf_in_plex_token(self):
        with pytest.raises(Exception):
            SettingsUpdate(plex_token="tok\ren\ninjected")

    def test_rejects_crlf_in_openai_api_key(self):
        with pytest.raises(Exception):
            SettingsUpdate(openai_api_key="sk-good\nX-Evil: yes")

    def test_rejects_crlf_in_tmdb_read_token(self):
        with pytest.raises(Exception):
            SettingsUpdate(tmdb_read_token="token\r\nevil")

    def test_rejects_crlf_in_plex_libraries_items(self):
        with pytest.raises(Exception):
            SettingsUpdate(plex_libraries=["Movies", "TV\nevil"])

    # ------------------------------------------------------------------
    # API-key character-set validation
    # ------------------------------------------------------------------

    def test_api_key_non_ascii_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(openai_api_key="sk-évil")

    def test_api_key_too_long_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(openai_api_key="A" * 201)

    def test_api_key_sentinel_values_pass(self):
        """'****' and '' are sentinel values meaning 'do not change'."""
        update = SettingsUpdate(openai_api_key="****")
        assert update.openai_api_key == "****"
        update2 = SettingsUpdate(openai_api_key="")
        assert update2.openai_api_key == ""

    def test_valid_api_key_passes(self):
        update = SettingsUpdate(sonarr_api_key="abc123XYZ-._~")
        assert update.sonarr_api_key == "abc123XYZ-._~"

    # ------------------------------------------------------------------
    # URL validation
    # ------------------------------------------------------------------

    def test_url_must_be_http_or_https(self):
        with pytest.raises(Exception):
            SettingsUpdate(plex_url="ftp://plex:32400")

    def test_url_file_scheme_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(sonarr_url="file:///etc/passwd")

    def test_url_javascript_scheme_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(radarr_url="javascript:alert(1)")

    def test_url_too_long_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(base_url="http://example.com/" + "a" * 2100)

    def test_valid_http_url_passes(self):
        update = SettingsUpdate(plex_url="http://192.168.1.10:32400")
        assert update.plex_url == "http://192.168.1.10:32400"

    def test_valid_https_url_passes(self):
        update = SettingsUpdate(base_url="https://media.example.com")
        assert update.base_url == "https://media.example.com"

    def test_public_url_fields_are_present(self):
        """All *_public_url fields must exist on the model."""
        update = SettingsUpdate(
            plex_public_url="http://plex.example.com",
            sonarr_public_url="http://sonarr.example.com",
            radarr_public_url="http://radarr.example.com",
            nzbget_public_url="http://nzbget.example.com",
        )
        assert update.plex_public_url == "http://plex.example.com"
        assert update.sonarr_public_url == "http://sonarr.example.com"
        assert update.radarr_public_url == "http://radarr.example.com"
        assert update.nzbget_public_url == "http://nzbget.example.com"

    # ------------------------------------------------------------------
    # scan_timezone
    # ------------------------------------------------------------------

    def test_valid_timezone_passes(self):
        update = SettingsUpdate(scan_timezone="Europe/London")
        assert update.scan_timezone == "Europe/London"

    def test_invalid_timezone_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(scan_timezone="Not/ATimezone")

    def test_empty_timezone_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(scan_timezone="")

    def test_crlf_in_timezone_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(scan_timezone="Europe/London\nevil")

    # ------------------------------------------------------------------
    # library_sync_interval
    # ------------------------------------------------------------------

    def test_library_sync_interval_valid(self):
        update = SettingsUpdate(library_sync_interval=300)
        assert update.library_sync_interval == 300

    def test_library_sync_interval_minimum(self):
        update = SettingsUpdate(library_sync_interval=60)
        assert update.library_sync_interval == 60

    def test_library_sync_interval_maximum(self):
        update = SettingsUpdate(library_sync_interval=86400)
        assert update.library_sync_interval == 86400

    def test_library_sync_interval_too_low_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(library_sync_interval=59)

    def test_library_sync_interval_too_high_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(library_sync_interval=86401)

    # ------------------------------------------------------------------
    # suggestions_enabled / disk_thresholds
    # ------------------------------------------------------------------

    def test_suggestions_enabled_bool(self):
        update = SettingsUpdate(suggestions_enabled=True)
        assert update.suggestions_enabled is True

    def test_disk_thresholds_valid(self):
        update = SettingsUpdate(disk_thresholds={"/media": 85, "/data": 90})
        assert update.disk_thresholds == {"/media": 85, "/data": 90}

    def test_disk_thresholds_out_of_range_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(disk_thresholds={"/media": 101})

    def test_disk_thresholds_negative_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(disk_thresholds={"/media": -1})

    def test_disk_thresholds_crlf_in_path_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(disk_thresholds={"/media\nevil": 80})

    def test_disk_thresholds_non_dict_rejected(self):
        with pytest.raises(Exception):
            SettingsUpdate(disk_thresholds="not-a-dict")
