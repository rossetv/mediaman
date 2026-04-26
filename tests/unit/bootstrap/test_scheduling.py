"""Tests for mediaman.bootstrap.scheduling.

The module is mostly wiring/glue, so we test:
- _validate_scan_time: pure function, easy to exhaustively check.
- bootstrap_scheduling: pass fake collaborators and verify start_scheduler
  is called with the correct arguments parsed from DB settings.
"""

import pytest

from mediaman.bootstrap.scheduling import _validate_scan_time

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
