"""Tests for the shared formatting helpers."""

from datetime import datetime, timedelta, timezone

from mediaman.services.infra.format import days_ago, format_bytes, parse_iso_utc


class TestFormatBytes:
    def test_zero(self):
        assert format_bytes(0) == "0 B"

    def test_negative(self):
        assert format_bytes(-5) == "0 B"

    def test_none(self):
        assert format_bytes(None) == "0 B"

    def test_bytes(self):
        assert format_bytes(500) == "500 B"

    def test_kilobytes(self):
        assert format_bytes(2048) == "2.0 KB"

    def test_megabytes(self):
        assert format_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        assert format_bytes(int(2.5 * 1024 * 1024 * 1024)) == "2.5 GB"

    def test_terabytes(self):
        assert format_bytes(3 * 1024 * 1024 * 1024 * 1024) == "3.0 TB"

    def test_large_value_drops_decimal(self):
        assert format_bytes(150 * 1024 * 1024 * 1024) == "150 GB"


class TestParseIsoUtc:
    def test_empty_returns_none(self):
        assert parse_iso_utc(None) is None
        assert parse_iso_utc("") is None

    def test_z_suffix(self):
        dt = parse_iso_utc("2026-04-16T12:00:00Z")
        assert dt == datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)

    def test_naive_treated_as_utc(self):
        dt = parse_iso_utc("2026-04-16T12:00:00")
        assert dt == datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)

    def test_offset_preserved(self):
        dt = parse_iso_utc("2026-04-16T12:00:00+02:00")
        assert dt.utcoffset() == timedelta(hours=2)

    def test_invalid_returns_none(self):
        assert parse_iso_utc("not-a-date") is None

    def test_truncates_extra_fractional_digits(self):
        dt = parse_iso_utc("2026-04-16T12:00:00.12345678+00:00")
        assert dt is not None
        assert dt.microsecond == 123456


class TestDaysAgo:
    def test_returns_empty_on_invalid(self):
        assert days_ago("oof") == ""
        assert days_ago(None) == ""

    def test_today(self):
        now = datetime.now(timezone.utc).isoformat()
        assert days_ago(now) == "today"

    def test_yesterday(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert days_ago(yesterday) == "yesterday"

    def test_multiple_days(self):
        past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        assert days_ago(past) == "10 days ago"
