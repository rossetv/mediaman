"""Unit tests for mediaman.scanner.scheduler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mediaman.scanner import scheduler as _sched_module
from mediaman.scanner.scheduler import start_scheduler, stop_scheduler


@pytest.fixture(autouse=True)
def reset_scheduler():
    """Ensure module-level _scheduler is None before and after each test."""
    _sched_module._scheduler = None
    yield
    if _sched_module._scheduler is not None:
        try:
            _sched_module._scheduler.shutdown(wait=False)
        except Exception:
            pass
        _sched_module._scheduler = None


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_start_scheduler_starts_and_returns(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    result = start_scheduler(scan_fn=lambda: None, secret_key="test")

    mock_instance.start.assert_called_once()
    assert result is mock_instance


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_start_scheduler_registers_weekly_scan(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    scan_fn = MagicMock()

    start_scheduler(scan_fn=scan_fn, secret_key="test")

    job_calls = mock_instance.add_job.call_args_list
    ids = [c.kwargs.get("id") or (c.args[2] if len(c.args) > 2 else None) for c in job_calls]
    assert "weekly_scan" in ids


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_start_scheduler_registers_fixed_background_jobs(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(scan_fn=lambda: None, secret_key="test")

    job_ids = [c.kwargs.get("id") for c in mock_instance.add_job.call_args_list]
    assert "cleanup_recent_downloads" in job_ids
    assert "trigger_pending_searches" in job_ids


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_start_scheduler_no_sync_fn_skips_library_sync(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(scan_fn=lambda: None, sync_fn=None, secret_key="test")

    job_ids = [c.kwargs.get("id") for c in mock_instance.add_job.call_args_list]
    assert "library_sync" not in job_ids


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_start_scheduler_with_sync_fn_registers_library_sync(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(
        scan_fn=lambda: None, sync_fn=lambda: None, sync_interval_minutes=15, secret_key="test"
    )

    job_ids = [c.kwargs.get("id") for c in mock_instance.add_job.call_args_list]
    assert "library_sync" in job_ids


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_stop_scheduler_shuts_down(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    start_scheduler(scan_fn=lambda: None, secret_key="test")

    stop_scheduler()

    mock_instance.shutdown.assert_called_once_with(wait=False)
    assert _sched_module._scheduler is None


def test_stop_scheduler_noop_when_not_started():
    """stop_scheduler must not raise when no scheduler is running."""
    assert _sched_module._scheduler is None
    stop_scheduler()  # should not raise


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_start_scheduler_sets_module_level_ref(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(scan_fn=lambda: None, secret_key="test")

    assert _sched_module._scheduler is mock_instance


@patch("apscheduler.schedulers.background.BackgroundScheduler")
def test_start_scheduler_called_twice_does_not_create_two_schedulers(mock_cls):
    """Calling start_scheduler a second time must return the existing instance
    and must not instantiate a second BackgroundScheduler (C17: idempotent start)."""
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    first = start_scheduler(scan_fn=lambda: None, secret_key="test")
    second = start_scheduler(scan_fn=lambda: None, secret_key="test")

    assert first is second
    # BackgroundScheduler() must have been called exactly once.
    assert mock_cls.call_count == 1
    # start() must also have been called exactly once.
    mock_instance.start.assert_called_once()


# ── H61: bind host resolution ───────────────────────────────────────────────


class TestResolveBindHost:
    """H61: MEDIAMAN_BIND_HOST controls the uvicorn bind address."""

    def test_defaults_to_localhost(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_BIND_HOST", raising=False)
        from mediaman.main import _resolve_bind_host

        assert _resolve_bind_host() == "127.0.0.1"

    def test_respects_env_override(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_BIND_HOST", "0.0.0.0")
        from mediaman.main import _resolve_bind_host

        assert _resolve_bind_host() == "0.0.0.0"

    def test_empty_env_falls_back_to_localhost(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_BIND_HOST", "")
        from mediaman.main import _resolve_bind_host

        assert _resolve_bind_host() == "127.0.0.1"


# ── H64: scan_time validation ────────────────────────────────────────────────


class TestValidateScanTime:
    """H64: scan_time must be validated as a proper HH:MM 24-hour time."""

    def test_valid_times_parse_correctly(self):
        from mediaman.bootstrap.scheduling import _validate_scan_time

        assert _validate_scan_time("09:00") == (9, 0)
        assert _validate_scan_time("00:00") == (0, 0)
        assert _validate_scan_time("23:59") == (23, 59)
        assert _validate_scan_time("12:30") == (12, 30)

    def test_rejects_invalid_hour(self):
        from mediaman.bootstrap.scheduling import _validate_scan_time

        with pytest.raises(ValueError):
            _validate_scan_time("25:00")

    def test_rejects_invalid_minute(self):
        from mediaman.bootstrap.scheduling import _validate_scan_time

        with pytest.raises(ValueError):
            _validate_scan_time("09:60")

    def test_rejects_wrong_format(self):
        from mediaman.bootstrap.scheduling import _validate_scan_time

        with pytest.raises(ValueError):
            _validate_scan_time("9:00")

    def test_rejects_garbage_input(self):
        from mediaman.bootstrap.scheduling import _validate_scan_time

        with pytest.raises(ValueError):
            _validate_scan_time("not-a-time")

    def test_rejects_24_00(self):
        """24:00 is not a valid 24-hour clock time."""
        from mediaman.bootstrap.scheduling import _validate_scan_time

        with pytest.raises(ValueError):
            _validate_scan_time("24:00")
