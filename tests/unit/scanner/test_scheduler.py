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


@patch("mediaman.scanner.scheduler.BackgroundScheduler")
def test_start_scheduler_starts_and_returns(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    result = start_scheduler(scan_fn=lambda: None)

    mock_instance.start.assert_called_once()
    assert result is mock_instance


@patch("mediaman.scanner.scheduler.BackgroundScheduler")
def test_start_scheduler_registers_weekly_scan(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    scan_fn = MagicMock()

    start_scheduler(scan_fn=scan_fn)

    job_calls = mock_instance.add_job.call_args_list
    ids = [c.kwargs.get("id") or (c.args[2] if len(c.args) > 2 else None) for c in job_calls]
    assert "weekly_scan" in ids


@patch("mediaman.scanner.scheduler.BackgroundScheduler")
def test_start_scheduler_registers_fixed_background_jobs(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(scan_fn=lambda: None)

    job_ids = [
        c.kwargs.get("id") for c in mock_instance.add_job.call_args_list
    ]
    assert "cleanup_recent_downloads" in job_ids
    assert "trigger_pending_searches" in job_ids


@patch("mediaman.scanner.scheduler.BackgroundScheduler")
def test_start_scheduler_no_sync_fn_skips_library_sync(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(scan_fn=lambda: None, sync_fn=None)

    job_ids = [c.kwargs.get("id") for c in mock_instance.add_job.call_args_list]
    assert "library_sync" not in job_ids


@patch("mediaman.scanner.scheduler.BackgroundScheduler")
def test_start_scheduler_with_sync_fn_registers_library_sync(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(scan_fn=lambda: None, sync_fn=lambda: None, sync_interval_minutes=15)

    job_ids = [c.kwargs.get("id") for c in mock_instance.add_job.call_args_list]
    assert "library_sync" in job_ids


@patch("mediaman.scanner.scheduler.BackgroundScheduler")
def test_stop_scheduler_shuts_down(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    start_scheduler(scan_fn=lambda: None)

    stop_scheduler()

    mock_instance.shutdown.assert_called_once_with(wait=False)
    assert _sched_module._scheduler is None


def test_stop_scheduler_noop_when_not_started():
    """stop_scheduler must not raise when no scheduler is running."""
    assert _sched_module._scheduler is None
    stop_scheduler()  # should not raise


@patch("mediaman.scanner.scheduler.BackgroundScheduler")
def test_start_scheduler_sets_module_level_ref(mock_cls):
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    start_scheduler(scan_fn=lambda: None)

    assert _sched_module._scheduler is mock_instance
