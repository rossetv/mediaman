"""Tests for Pydantic models."""

from mediaman.models import KeepRequest, SettingsUpdate, LoginRequest
import pytest


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
