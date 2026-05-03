"""Tests for the time-based abandon thresholds and gate."""

from __future__ import annotations

import time as _time_mod
from unittest.mock import MagicMock


def test_abandon_button_threshold_is_ten_hours():
    from mediaman.services.arr.auto_abandon import _ABANDON_BUTTON_VISIBLE_AFTER_SECONDS

    assert _ABANDON_BUTTON_VISIBLE_AFTER_SECONDS == 10 * 3600


def test_auto_abandon_threshold_is_fourteen_days():
    from mediaman.services.arr.auto_abandon import _AUTO_ABANDON_AFTER_SECONDS

    assert _AUTO_ABANDON_AFTER_SECONDS == 14 * 86_400


class TestMaybeAutoAbandon:
    """Time-based auto-abandon, gated by the auto_abandon_enabled setting."""

    def _movie_item(self, added_at: float, dl_id: str = "radarr:Old", arr_id: int = 1) -> dict:
        return {
            "kind": "movie",
            "dl_id": dl_id,
            "arr_id": arr_id,
            "added_at": added_at,
        }

    def test_skips_when_setting_disabled(self, monkeypatch):
        from mediaman.services.arr.auto_abandon import maybe_auto_abandon

        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda conn, key, default=False: False,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.abandon_movie",
            lambda *a, **kw: called.append(kw),
        )

        now = _time_mod.time()
        item = self._movie_item(added_at=now - 30 * 86_400)  # 30 days
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == []

    def test_skips_when_under_threshold(self, monkeypatch):
        from mediaman.services.arr.auto_abandon import maybe_auto_abandon

        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda conn, key, default=False: True,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.abandon_movie",
            lambda *a, **kw: called.append(kw),
        )

        now = _time_mod.time()
        # 13 days, 23 hours — under 14 d.
        item = self._movie_item(added_at=now - (14 * 86_400 - 3600))
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == []

    def test_fires_movie_when_enabled_and_over_threshold(self, monkeypatch):
        from mediaman.services.arr.auto_abandon import maybe_auto_abandon

        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda conn, key, default=False: True,
        )
        # Capture the security_event detail so we know the new shape is right.
        captured_events = []
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.security_event",
            lambda conn, **kw: captured_events.append(kw),
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.abandon_movie",
            lambda *a, **kw: called.append(kw),
        )

        now = _time_mod.time()
        item = self._movie_item(added_at=now - 15 * 86_400, arr_id=42)
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        # Movie was abandoned with the right arr_id.
        assert called == [{"arr_id": 42, "dl_id": "radarr:Old"}]
        # Audit event was emitted with the new time-based detail shape.
        assert len(captured_events) == 1
        ev = captured_events[0]
        assert ev["event"] == "auto_abandon.fired"
        assert ev["actor"] == ""
        detail = ev["detail"]
        assert detail["kind"] == "movie"
        assert detail["service"] == "radarr"
        assert detail["arr_id"] == 42
        assert "searching_for_seconds" in detail
        # And the deprecated fields are gone.
        assert "multiplier" not in detail
        assert "search_count" not in detail
        assert "escalate_at" not in detail

    def test_skips_when_no_dl_id_or_arr_id(self, monkeypatch):
        from mediaman.services.arr.auto_abandon import maybe_auto_abandon

        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda conn, key, default=False: True,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.abandon_movie",
            lambda *a, **kw: called.append(kw),
        )

        now = _time_mod.time()
        item = {"kind": "movie", "dl_id": "", "arr_id": 1, "added_at": now - 15 * 86_400}
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)
        item = {"kind": "movie", "dl_id": "radarr:X", "arr_id": 0, "added_at": now - 15 * 86_400}
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)
        assert called == []

    def test_skips_when_added_at_is_zero(self, monkeypatch):
        """added_at=0 must never trigger auto-abandon (missing timestamp, not ancient)."""
        from mediaman.services.arr.auto_abandon import maybe_auto_abandon

        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda conn, key, default=False: True,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.abandon_movie",
            lambda *a, **kw: called.append(kw),
        )

        now = _time_mod.time()
        item = self._movie_item(added_at=0.0)
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == []

    def test_fires_series_with_filtered_seasons(self, monkeypatch):
        """Series branch: specials (S00) filtered out; only positive seasons abandoned."""
        from mediaman.services.arr.auto_abandon import maybe_auto_abandon

        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda conn, key, default=False: True,
        )
        captured_events = []
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.security_event",
            lambda conn, **kw: captured_events.append(kw),
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.abandon_seasons",
            lambda *a, **kw: called.append(kw),
        )

        now = _time_mod.time()
        item = {
            "kind": "series",
            "dl_id": "sonarr:Show",
            "arr_id": 7,
            "added_at": now - 15 * 86_400,
            "episodes": [
                {"season_number": 0},  # specials, filtered
                {"season_number": 2},
                {"season_number": 2},
                {"season_number": 5},
            ],
        }
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == [{"series_id": 7, "season_numbers": [2, 5], "dl_id": "sonarr:Show"}]
        assert captured_events[0]["detail"]["seasons"] == [2, 5]
