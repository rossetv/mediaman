"""Tests for mediaman.bootstrap.scheduling.

The module is mostly wiring/glue, so we test:
- _validate_scan_time: pure function, easy to exhaustively check.
- bootstrap_scheduling: pass fake collaborators and verify start_scheduler
  is called with the correct arguments parsed from DB settings.
"""

import pytest

from mediaman.bootstrap.scheduling import (
    _validate_scan_day,
    _validate_scan_time,
    _validate_scan_timezone,
    _validate_sync_interval,
)

# ---------------------------------------------------------------------------
# _validate_scan_time
# ---------------------------------------------------------------------------


class TestValidateScanTime:
    def test_valid_midnight(self):
        assert _validate_scan_time("00:00") == (0, 0)

    def test_valid_noon(self):
        assert _validate_scan_time("12:00") == (12, 0)

    def test_valid_last_minute_of_day(self):
        assert _validate_scan_time("23:59") == (23, 59)

    def test_valid_leading_zero(self):
        assert _validate_scan_time("09:05") == (9, 5)

    def test_invalid_missing_colon_raises(self):
        with pytest.raises(ValueError, match="HH:MM"):
            _validate_scan_time("0900")

    def test_invalid_hour_too_high_raises(self):
        with pytest.raises(ValueError):
            _validate_scan_time("25:00")

    def test_invalid_minute_too_high_raises(self):
        with pytest.raises(ValueError):
            _validate_scan_time("12:60")

    def test_invalid_empty_string_raises(self):
        with pytest.raises(ValueError):
            _validate_scan_time("")

    def test_invalid_letters_raise(self):
        with pytest.raises(ValueError):
            _validate_scan_time("ab:cd")

    def test_invalid_single_digit_hour_raises(self):
        # "9:00" has no leading zero — must fail the regex.
        with pytest.raises(ValueError):
            _validate_scan_time("9:00")


# ---------------------------------------------------------------------------
# bootstrap_scheduling — wiring tests
# ---------------------------------------------------------------------------


class TestBootstrapScheduling:
    """Verify the scheduling bootstrap calls start_scheduler with the
    settings values it reads from the DB."""

    def _make_app(self, db_path):
        """Return a minimal FastAPI-shaped stub with app.state.db."""
        from mediaman.db import init_db

        class _State:
            pass

        class _App:
            state = _State()

        app = _App()
        app.state.db = init_db(str(db_path))
        app.state.canary_ok = True
        return app

    def _make_config(self):
        class _Config:
            secret_key = "0123456789abcdef" * 4  # 64 hex chars

        return _Config()

    def test_returns_true_on_success(self, db_path, monkeypatch):
        app = self._make_app(db_path)
        calls = {}

        def fake_start_scheduler(**kwargs):
            calls.update(kwargs)

        monkeypatch.setattr(
            "mediaman.bootstrap.scheduling.start_scheduler",
            fake_start_scheduler,
            raising=False,
        )
        # Patch the import inside the module namespace.
        import mediaman.scanner.scheduler as _sched

        monkeypatch.setattr(_sched, "start_scheduler", fake_start_scheduler)

        from mediaman.bootstrap.scheduling import bootstrap_scheduling

        result = bootstrap_scheduling(app, self._make_config())
        assert result is True

    def test_passes_correct_hour_and_minute_to_scheduler(self, db_path, monkeypatch):
        app = self._make_app(db_path)
        # Override the scan_time setting in DB.
        app.state.db.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('scan_time', '14:30', 0, '2026-01-01')"
        )
        app.state.db.commit()

        captured = {}

        def fake_start(**kwargs):
            captured.update(kwargs)

        import mediaman.scanner.scheduler as _sched

        monkeypatch.setattr(_sched, "start_scheduler", fake_start)

        from mediaman.bootstrap.scheduling import bootstrap_scheduling

        bootstrap_scheduling(app, self._make_config())
        assert captured.get("hour") == 14
        assert captured.get("minute") == 30

    def test_returns_false_when_canary_fails(self, db_path, monkeypatch):
        app = self._make_app(db_path)
        app.state.canary_ok = False

        from mediaman.bootstrap.scheduling import bootstrap_scheduling

        result = bootstrap_scheduling(app, self._make_config())
        assert result is False

    def test_returns_false_on_invalid_scan_time_in_db(self, db_path, monkeypatch):
        app = self._make_app(db_path)
        app.state.db.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('scan_time', 'not-a-time', 0, '2026-01-01')"
        )
        app.state.db.commit()

        from mediaman.bootstrap.scheduling import bootstrap_scheduling

        # Invalid scan_time must not blow up the app — bootstrap catches it and returns False.
        result = bootstrap_scheduling(app, self._make_config())
        assert result is False

    def test_failure_records_scheduler_error_for_readyz(self, db_path):
        """The /readyz body needs the *why* — finding 7."""
        app = self._make_app(db_path)
        app.state.canary_ok = False

        from mediaman.bootstrap.scheduling import bootstrap_scheduling

        result = bootstrap_scheduling(app, self._make_config())
        assert result is False
        assert app.state.scheduler_error is not None
        assert "AES canary" in app.state.scheduler_error

    def test_logger_exception_used_on_failure(self, db_path, monkeypatch, caplog):
        """Failures must include the traceback — finding 16.

        The logger has to be ``exception``-level so the traceback is
        attached to the record; without it operators get the message
        only and have to reproduce the boot to debug.
        """
        import logging

        app = self._make_app(db_path)
        app.state.canary_ok = False

        from mediaman.bootstrap.scheduling import bootstrap_scheduling

        with caplog.at_level(logging.ERROR, logger="mediaman"):
            bootstrap_scheduling(app, self._make_config())

        # ``logger.exception`` logs at ERROR level and attaches exc_info;
        # the latter is the property the audit cares about.
        record = next(
            (r for r in caplog.records if "scheduler" in r.getMessage().lower()),
            None,
        )
        assert record is not None
        assert record.exc_info is not None


class TestShutdownScheduling:
    """Finding 3 / shutdown_scheduling: bounded wait so SIGTERM unblocks."""

    def test_shutdown_returns_within_bounded_time(self, monkeypatch):
        """Even if ``stop_scheduler`` hangs, shutdown_scheduling returns."""
        import time

        # A fake stop_scheduler that ignores the wait. shutdown_scheduling
        # should still return — abandoning the worker — within the bounded
        # timeout, not block forever.
        def slow_stop():
            time.sleep(60)

        import mediaman.scanner.scheduler as _sched

        monkeypatch.setattr(_sched, "stop_scheduler", slow_stop)

        # Squash the timeout for the test so we don't actually wait 30s.
        import mediaman.bootstrap.scheduling as _boot

        monkeypatch.setattr(_boot, "_SHUTDOWN_TIMEOUT_SECONDS", 0.2)

        from mediaman.bootstrap.scheduling import shutdown_scheduling

        start = time.monotonic()
        shutdown_scheduling()
        elapsed = time.monotonic() - start
        # 0.2s timeout + thread overhead — must be << the 60s slow_stop.
        assert elapsed < 5.0


class TestBootstrapCryptoFailClosed:
    """Finding 14: canary state must default to False, not True."""

    def test_canary_ok_starts_false_on_import_failure(self, db_path, monkeypatch):
        """An import-time exception must leave canary_ok=False."""
        from mediaman.bootstrap import crypto as crypto_mod

        class _State:
            pass

        class _App:
            state = _State()

        from mediaman.db import init_db

        app = _App()
        app.state.db = init_db(str(db_path))

        # Force the canary call to raise so we exercise the except path.
        def boom(*_a, **_kw):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr("mediaman.crypto.canary_check", boom)

        class _Cfg:
            secret_key = "0123456789abcdef" * 4

        crypto_mod.bootstrap_crypto(app, _Cfg())
        assert app.state.canary_ok is False
        # And — finding 15 — bootstrap_crypto must NOT touch
        # scheduler_healthy; that's bootstrap_scheduling's job.
        assert not hasattr(app.state, "scheduler_healthy")


# ---------------------------------------------------------------------------
# Finding 10: scheduler-setting validators
# ---------------------------------------------------------------------------


class TestValidateScanDay:
    def test_accepts_single_day(self):
        assert _validate_scan_day("mon") == "mon"

    def test_accepts_comma_list(self):
        assert _validate_scan_day("mon,wed,fri") == "mon,wed,fri"

    def test_normalises_case_and_whitespace(self):
        assert _validate_scan_day(" Mon , WED ") == "mon,wed"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _validate_scan_day("")

    def test_rejects_unknown_token(self):
        with pytest.raises(ValueError, match="weekday token"):
            _validate_scan_day("moonday")

    def test_rejects_partial_match(self):
        with pytest.raises(ValueError):
            _validate_scan_day("mon,xyz,wed")


class TestValidateScanTimezone:
    def test_accepts_utc(self):
        assert _validate_scan_timezone("UTC") == "UTC"

    def test_accepts_iana(self):
        assert _validate_scan_timezone("Europe/London") == "Europe/London"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _validate_scan_timezone("")

    def test_rejects_made_up(self):
        with pytest.raises(ValueError):
            _validate_scan_timezone("Mars/Olympus")


class TestValidateSyncInterval:
    def test_accepts_positive_int(self):
        assert _validate_sync_interval("30") == 30

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            _validate_sync_interval("0")

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            _validate_sync_interval("-5")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            _validate_sync_interval("nope")

    def test_rejects_over_24_hours(self):
        with pytest.raises(ValueError):
            _validate_sync_interval("1500")
