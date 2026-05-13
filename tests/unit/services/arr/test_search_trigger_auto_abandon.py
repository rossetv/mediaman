"""Tests for auto-abandon behaviour driven through the search-trigger flow.

Covers:
- ``maybe_auto_abandon`` gating (enabled/disabled, thresholds)
- Movie and series abandon paths
- Specials (season 0) filtering
- ``TestAutoAbandonAuditLog``: security audit trail for every firing
"""

from __future__ import annotations

import time as _time

import pytest

from mediaman.db import init_db
from mediaman.services.arr.auto_abandon import maybe_auto_abandon
from mediaman.services.arr.search_trigger import reset_search_triggers

_OVER_THRESHOLD = _time.time() - 20 * 86_400  # 20 days ago — over 14 d threshold
_OLD_RELEASE = _time.time() - 365 * 86_400  # past 30 d release-grace window


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure a clean slate before every test in this module."""
    reset_search_triggers()
    yield
    reset_search_triggers()


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# TestAutoAbandon
# ---------------------------------------------------------------------------


class TestAutoAbandon:
    def test_off_when_setting_disabled(self, db_conn, monkeypatch):
        """auto_abandon_enabled=False never auto-abandons regardless of age."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: False,
        )
        called = {"abandon_movie": 0}
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_movie",
            lambda *a, **kw: called.__setitem__("abandon_movie", called["abandon_movie"] + 1),
        )

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "movie",
                "dl_id": "radarr:X",
                "arr_id": 1,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
            },
            now=now,
        )
        assert called["abandon_movie"] == 0

    def test_fires_when_enabled_and_over_threshold(self, db_conn, monkeypatch):
        """Fires when auto_abandon_enabled=True and item is older than 14 days."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        called = {}

        def fake_abandon_movie(conn, secret, *, arr_id, dl_id):
            called["arr_id"] = arr_id
            called["dl_id"] = dl_id

        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", fake_abandon_movie)

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "movie",
                "dl_id": "radarr:X",
                "arr_id": 42,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
            },
            now=now,
        )
        assert called == {"arr_id": 42, "dl_id": "radarr:X"}

    def test_does_not_fire_below_threshold(self, db_conn, monkeypatch):
        """Does not fire when item is under 14 days old even if enabled."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        called = {"n": 0}
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_movie",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
        )

        now = _time.time()
        under_threshold = now - (14 * 86_400 - 3600)  # 13 d 23 h ago
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "movie",
                "dl_id": "radarr:X",
                "arr_id": 1,
                "added_at": under_threshold,
                "released_at": _OLD_RELEASE,
            },
            now=now,
        )
        assert called["n"] == 0

    def test_series_passes_derived_seasons(self, db_conn, monkeypatch):
        """For a series item, derives season list from episodes."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        called = {}

        def fake_abandon_seasons(conn, secret, *, series_id, season_numbers, dl_id):
            called["series_id"] = series_id
            called["seasons"] = sorted(season_numbers)
            called["dl_id"] = dl_id

        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", fake_abandon_seasons)

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:X",
                "arr_id": 7,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
                "episodes": [
                    {"season_number": 21},
                    {"season_number": 21},
                    {"season_number": 22},
                ],
            },
            now=now,
        )
        assert called == {"series_id": 7, "seasons": [21, 22], "dl_id": "sonarr:X"}

    def test_series_with_only_season_zero_episodes_skipped(self, db_conn, monkeypatch):
        """Domain-06 #12: all-specials queue must NOT be auto-abandoned.

        Regression: a specials-only queue produced ``seasons=[0]`` which
        would unmonitor every special — catastrophic for opt-in specials.
        """
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        called = {"n": 0}
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_seasons",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
        )

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:Specials",
                "arr_id": 7,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
                "episodes": [
                    {"season_number": 0},
                    {"season_number": 0},
                ],
            },
            now=now,
        )

        # The function should be a no-op — no abandon call AT ALL.
        assert called["n"] == 0

    def test_series_with_mixed_specials_filters_season_zero(self, db_conn, monkeypatch):
        """Mixed specials + real seasons → only the real seasons are abandoned."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        called = {}

        def fake_abandon_seasons(conn, secret, *, series_id, season_numbers, dl_id):
            called["seasons"] = sorted(season_numbers)

        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", fake_abandon_seasons)

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:Mixed",
                "arr_id": 7,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
                "episodes": [
                    {"season_number": 0},  # special — must be excluded
                    {"season_number": 1},
                    {"season_number": 2},
                ],
            },
            now=now,
        )

        assert called["seasons"] == [1, 2]

    def test_series_with_no_episodes_skipped(self, db_conn, monkeypatch):
        """Series with empty episodes list is silently skipped (no error)."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        called = {"n": 0}
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_seasons",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
        )

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:X",
                "arr_id": 7,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
                "episodes": [],
            },
            now=now,
        )
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# TestAutoAbandonAuditLog
# ---------------------------------------------------------------------------


def _read_auto_abandon_rows(conn) -> list[tuple[str, str | None, str]]:
    """Return ``(action, actor, detail)`` for every auto-abandon audit row."""
    return list(
        conn.execute(
            "SELECT action, actor, detail FROM audit_log "
            "WHERE action = 'sec:auto_abandon.fired' ORDER BY id"
        ).fetchall()
    )


class TestAutoAbandonAuditLog:
    """Finding 06 — every auto-abandon firing must leave a ``sec:auto_abandon.fired`` audit row."""

    def test_movie_fire_emits_audit_row(self, db_conn, monkeypatch):
        """Firing on a movie writes one ``sec:auto_abandon.fired`` row."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", lambda *a, **kw: None)

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "movie",
                "dl_id": "radarr:Dune",
                "arr_id": 42,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
            },
            now=now,
        )

        rows = _read_auto_abandon_rows(db_conn)
        assert len(rows) == 1
        action, actor, detail = rows[0]
        assert action == "sec:auto_abandon.fired"
        # System-driven event — actor is the empty-string convention used
        # by login.failed et al. for unauthenticated/system events.
        assert actor == ""
        # Detail is JSON-encoded after the actor= ip= prefix.
        assert "actor=- ip=-" in detail
        assert '"dl_id":"radarr:Dune"' in detail
        assert '"arr_id":42' in detail
        assert '"service":"radarr"' in detail
        assert '"searching_for_seconds"' in detail
        # Deprecated fields must not appear.
        assert '"multiplier"' not in detail
        assert '"escalate_at"' not in detail
        assert '"search_count"' not in detail

    def test_series_fire_emits_audit_row_with_seasons(self, db_conn, monkeypatch):
        """Series firings record the derived season list in the detail."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", lambda *a, **kw: None)

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:Foundation",
                "arr_id": 7,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
                "episodes": [
                    {"season_number": 1},
                    {"season_number": 2},
                    {"season_number": 2},
                ],
            },
            now=now,
        )

        rows = _read_auto_abandon_rows(db_conn)
        assert len(rows) == 1
        action, actor, detail = rows[0]
        assert action == "sec:auto_abandon.fired"
        assert actor == ""
        assert '"dl_id":"sonarr:Foundation"' in detail
        assert '"service":"sonarr"' in detail
        assert '"seasons":[1,2]' in detail

    def test_no_audit_row_when_setting_disabled(self, db_conn, monkeypatch):
        """auto_abandon_enabled=False writes no audit row, regardless of age."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: False,
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", lambda *a, **kw: None)

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "movie",
                "dl_id": "radarr:X",
                "arr_id": 1,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
            },
            now=now,
        )

        assert _read_auto_abandon_rows(db_conn) == []

    def test_no_audit_row_below_threshold(self, db_conn, monkeypatch):
        """Under 14 days old — gated, no row written."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", lambda *a, **kw: None)

        now = _time.time()
        under_threshold = now - (14 * 86_400 - 3600)  # 1 hour under
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "movie",
                "dl_id": "radarr:X",
                "arr_id": 1,
                "added_at": under_threshold,
                "released_at": _OLD_RELEASE,
            },
            now=now,
        )

        assert _read_auto_abandon_rows(db_conn) == []

    def test_no_audit_row_for_series_with_no_episodes(self, db_conn, monkeypatch):
        """Series skipped pre-firing (no episodes) writes no audit row."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", lambda *a, **kw: None)

        now = _time.time()
        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:X",
                "arr_id": 7,
                "added_at": _OVER_THRESHOLD,
                "released_at": _OLD_RELEASE,
                "episodes": [],
            },
            now=now,
        )

        assert _read_auto_abandon_rows(db_conn) == []

    def test_audit_row_persists_when_abandon_call_fails(self, db_conn, monkeypatch):
        """Abandon raising must NOT prevent the audit row from landing.

        ``security_event`` writes (and commits) before the abandon call,
        so a Radarr/Sonarr outage still leaves a discoverable trail of
        what the policy decided to do.
        """
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_bool_setting",
            lambda c, k, default=False: True,
        )
        import mediaman.services.downloads.abandon as abandon_module

        def boom(*a, **kw):
            raise RuntimeError("radarr offline")

        monkeypatch.setattr(abandon_module, "abandon_movie", boom)

        now = _time.time()
        with pytest.raises(RuntimeError):
            maybe_auto_abandon(
                db_conn,
                "secret",
                item={
                    "kind": "movie",
                    "dl_id": "radarr:Y",
                    "arr_id": 42,
                    "added_at": _OVER_THRESHOLD,
                    "released_at": _OLD_RELEASE,
                },
                now=now,
            )

        rows = _read_auto_abandon_rows(db_conn)
        assert len(rows) == 1
        action, _actor, detail = rows[0]
        assert action == "sec:auto_abandon.fired"
        assert '"dl_id":"radarr:Y"' in detail
