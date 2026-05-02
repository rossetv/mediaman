"""Domain-04 hardening tests for ``mediaman.web.models``.

These tests cover the changes added in the security pass: ``extra=
"forbid"`` on the legacy ``LoginRequest`` / ``KeepRequest`` /
``SubscriberCreate`` models, NUL rejection in the CRLF guard, the
``DiskThresholds.thresholds`` size cap, and the per-field
``max_length`` caps on ``SettingsUpdate``.
"""

from __future__ import annotations

import pytest

from mediaman.web.models import (
    DiskThresholds,
    KeepRequest,
    LoginRequest,
    SettingsUpdate,
    SubscriberCreate,
    _reject_crlf,
)


class TestLoginRequestForbidsExtra:
    """``LoginRequest`` rejects unknown keys (``extra="forbid"``)."""

    def test_basic_login_passes(self):
        req = LoginRequest(username="admin", password="pw")
        assert req.username == "admin"
        assert req.password == "pw"

    def test_unknown_key_rejected(self):
        with pytest.raises(Exception) as exc:
            LoginRequest(username="admin", password="pw", is_admin=True)
        assert "is_admin" in str(exc.value)

    def test_username_too_long_rejected(self):
        with pytest.raises(Exception):
            LoginRequest(username="a" * 257, password="pw")

    def test_password_too_long_rejected(self):
        with pytest.raises(Exception):
            LoginRequest(username="admin", password="a" * 1025)

    def test_empty_username_rejected(self):
        with pytest.raises(Exception):
            LoginRequest(username="", password="pw")

    def test_empty_password_rejected(self):
        with pytest.raises(Exception):
            LoginRequest(username="admin", password="")


class TestKeepRequestForbidsExtra:
    """``KeepRequest`` rejects unknown keys (``extra="forbid"``)."""

    def test_valid_duration_passes(self):
        req = KeepRequest(duration="7 days")
        assert req.duration == "7 days"

    def test_unknown_key_rejected(self):
        with pytest.raises(Exception) as exc:
            KeepRequest(duration="7 days", note="hidden")
        assert "note" in str(exc.value)

    def test_too_long_duration_rejected(self):
        """Duration max length is 32 — anything longer is bogus."""
        with pytest.raises(Exception):
            KeepRequest(duration="x" * 33)


class TestRejectCRLFAlsoRejectsNUL:
    """The CR/LF guard now also rejects NUL bytes."""

    def test_nul_byte_rejected(self):
        with pytest.raises(ValueError, match="CR, LF, or NUL"):
            _reject_crlf("hello\x00world")

    def test_cr_still_rejected(self):
        with pytest.raises(ValueError):
            _reject_crlf("hello\rworld")

    def test_lf_still_rejected(self):
        with pytest.raises(ValueError):
            _reject_crlf("hello\nworld")

    def test_clean_string_passes(self):
        assert _reject_crlf("hello world") == "hello world"

    def test_none_passes(self):
        assert _reject_crlf(None) is None

    def test_settings_update_rejects_nul_in_strings(self):
        """Make sure the model-level CRLF validator picks up NUL too."""
        with pytest.raises(Exception):
            SettingsUpdate(scan_day="mon\x00day")


class TestDiskThresholdsSizeCap:
    """``DiskThresholds.thresholds`` is bounded at 64 entries."""

    def test_one_entry_passes(self):
        cfg = DiskThresholds(thresholds={"/media": 80})
        assert cfg.thresholds == {"/media": 80}

    def test_at_cap_passes(self):
        v = {f"/p/{i}": 50 for i in range(64)}
        cfg = DiskThresholds(thresholds=v)
        assert len(cfg.thresholds) == 64

    def test_over_cap_rejected(self):
        v = {f"/p/{i}": 50 for i in range(65)}
        with pytest.raises(Exception):
            DiskThresholds(thresholds=v)


class TestSettingsUpdateMaxLengthCaps:
    """All string fields on ``SettingsUpdate`` are bounded.  The previous
    layer only enforced length on URL fields (2048) and API-key fields
    (1024).  New caps:

    - ``nzbget_username``       128
    - ``mailgun_domain``        256
    - ``mailgun_from_address``  320
    - ``scan_day``              16
    - ``scan_time``             16
    - ``scan_timezone``         64
    """

    def test_nzbget_username_cap(self):
        with pytest.raises(Exception):
            SettingsUpdate(nzbget_username="a" * 129)

    def test_nzbget_username_just_under_cap_passes(self):
        update = SettingsUpdate(nzbget_username="a" * 128)
        assert update.nzbget_username == "a" * 128

    def test_mailgun_domain_cap(self):
        with pytest.raises(Exception):
            SettingsUpdate(mailgun_domain="a" * 257)

    def test_mailgun_from_address_cap(self):
        with pytest.raises(Exception):
            SettingsUpdate(mailgun_from_address="a@" + "b" * 320)

    def test_scan_day_cap(self):
        with pytest.raises(Exception):
            SettingsUpdate(scan_day="a" * 17)

    def test_scan_time_cap(self):
        with pytest.raises(Exception):
            SettingsUpdate(scan_time="a" * 17)

    def test_scan_timezone_cap(self):
        with pytest.raises(Exception):
            SettingsUpdate(scan_timezone="a" * 65)

    def test_plex_libraries_count_cap(self):
        """``plex_libraries`` itself is capped at 128 entries."""
        with pytest.raises(Exception):
            SettingsUpdate(plex_libraries=[f"L{i}" for i in range(129)])

    def test_plex_libraries_per_entry_cap(self):
        """Each entry in ``plex_libraries`` is capped at 256 chars."""
        with pytest.raises(Exception):
            SettingsUpdate(plex_libraries=["a" * 257])


class TestSubscriberCreateHardened:
    """``SubscriberCreate`` rejects too-long emails, invalid emails, and
    extra keys."""

    def test_valid_email_passes(self):
        req = SubscriberCreate(email="alice@example.com")
        assert req.email == "alice@example.com"

    def test_email_lowercased(self):
        req = SubscriberCreate(email="Alice@Example.COM")
        assert req.email == "alice@example.com"

    def test_invalid_email_rejected(self):
        with pytest.raises(Exception):
            SubscriberCreate(email="not-an-email")

    def test_email_with_crlf_rejected(self):
        with pytest.raises(Exception):
            SubscriberCreate(email="ok@example.com\r\nBcc: evil@evil")

    def test_email_with_nul_rejected(self):
        with pytest.raises(Exception):
            SubscriberCreate(email="ok\x00@example.com")

    def test_too_long_email_rejected(self):
        # 320 is the RFC 5321 max; build something larger.
        too_long = "a" * 310 + "@example.com"
        assert len(too_long) > 320
        with pytest.raises(Exception):
            SubscriberCreate(email=too_long)

    def test_unknown_key_rejected(self):
        with pytest.raises(Exception) as exc:
            SubscriberCreate(email="a@b.com", active=False)
        assert "active" in str(exc.value)
