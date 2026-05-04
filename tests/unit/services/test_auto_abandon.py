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


def test_auto_abandon_release_grace_is_thirty_days():
    from mediaman.services.arr.auto_abandon import _AUTO_ABANDON_RELEASE_GRACE_SECONDS

    assert _AUTO_ABANDON_RELEASE_GRACE_SECONDS == 30 * 86_400


class TestMaybeAutoAbandon:
    """Time-based auto-abandon, gated by the auto_abandon_enabled setting."""

    def _movie_item(
        self,
        added_at: float,
        dl_id: str = "radarr:Old",
        arr_id: int = 1,
        released_at: float | None = None,
    ) -> dict:
        # Default ``released_at`` to a year before ``added_at`` so the
        # release-grace gate doesn't unintentionally short-circuit tests
        # that aren't exercising it.
        if released_at is None:
            released_at = added_at - 365 * 86_400
        return {
            "kind": "movie",
            "dl_id": dl_id,
            "arr_id": arr_id,
            "added_at": added_at,
            "released_at": released_at,
            "is_upcoming": False,
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
        old_release = now - 365 * 86_400
        item = {
            "kind": "movie",
            "dl_id": "",
            "arr_id": 1,
            "added_at": now - 15 * 86_400,
            "released_at": old_release,
        }
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)
        item = {
            "kind": "movie",
            "dl_id": "radarr:X",
            "arr_id": 0,
            "added_at": now - 15 * 86_400,
            "released_at": old_release,
        }
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
            "released_at": now - 200 * 86_400,
            "is_upcoming": False,
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

    def test_skips_upcoming_movie_even_when_added_long_ago(self, monkeypatch):
        """Coming-soon movies must never auto-abandon, even if added > 14 d ago.

        Regression: enabling auto-abandon was wiping monitored movies from
        the "Coming soon" list. Indexers correctly have no copies for
        unreleased movies, so the 14-day search threshold trips even
        though the movie isn't out yet.
        """
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
        item = {
            "kind": "movie",
            "dl_id": "radarr:Avatar 3",
            "arr_id": 99,
            "added_at": now - 60 * 86_400,
            # Future release date.
            "released_at": now + 90 * 86_400,
            "is_upcoming": True,
        }
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == []

    def test_skips_movie_released_within_thirty_days(self, monkeypatch):
        """Recently-released movies must not auto-abandon — copies may still be propagating."""
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
        item = {
            "kind": "movie",
            "dl_id": "radarr:Fresh",
            "arr_id": 11,
            # Searching for ages, but the movie only came out 10 days ago.
            "added_at": now - 60 * 86_400,
            "released_at": now - 10 * 86_400,
            "is_upcoming": False,
        }
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == []

    def test_skips_when_released_at_unknown(self, monkeypatch):
        """No release-date metadata → conservative skip; never abandon blindly."""
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
        item = {
            "kind": "movie",
            "dl_id": "radarr:NoDate",
            "arr_id": 12,
            "added_at": now - 60 * 86_400,
            # released_at omitted (or 0.0) → unknown release date.
            "is_upcoming": False,
        }
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == []

    def test_fires_movie_released_long_ago(self, monkeypatch):
        """Movie released > 30 d ago AND searching > 14 d → eligible to abandon."""
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
        item = {
            "kind": "movie",
            "dl_id": "radarr:Stale",
            "arr_id": 13,
            "added_at": now - 20 * 86_400,
            "released_at": now - 90 * 86_400,
            "is_upcoming": False,
        }
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == [{"arr_id": 13, "dl_id": "radarr:Stale"}]

    def test_skips_upcoming_series(self, monkeypatch):
        """Coming-soon series must not auto-abandon either."""
        from mediaman.services.arr.auto_abandon import maybe_auto_abandon

        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda conn, key, default=False: True,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.downloads.abandon.abandon_seasons",
            lambda *a, **kw: called.append(kw),
        )

        now = _time_mod.time()
        item = {
            "kind": "series",
            "dl_id": "sonarr:Future Show",
            "arr_id": 88,
            "added_at": now - 60 * 86_400,
            "released_at": 0.0,  # nothing aired yet
            "is_upcoming": True,
            "episodes": [{"season_number": 1}],
        }
        maybe_auto_abandon(MagicMock(), "secret", item=item, now=now)

        assert called == []
