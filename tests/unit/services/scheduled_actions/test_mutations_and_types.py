"""Unit tests for scheduled_actions service layer.

Covers:
- resolve_keep_decision raises ValueError (not AssertionError) when days is None
  for a non-forever duration (L-05).
- is_pending_unexpired uses simplified membership check — both None and 'pending'
  are actionable; 'deleting' is not (M-04).
- find_active_keep_action_by_id_and_token is absent from the public API (H-02).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# L-05: resolve_keep_decision must raise ValueError, not AssertionError
# ---------------------------------------------------------------------------


class TestResolveKeepDecisionValidation:
    """L-05: domain validation must use ValueError, not assert."""

    def test_raises_value_error_when_days_none_for_non_forever(self):
        from datetime import UTC, datetime

        from mediaman.services.scheduled_actions._types import resolve_keep_decision

        with pytest.raises(ValueError, match="days is None"):
            resolve_keep_decision(
                "30 days",
                days=None,
                now=datetime.now(UTC),
            )

    def test_does_not_raise_for_forever_with_days_none(self):
        """forever + days=None is the normal path — must not raise."""
        from datetime import UTC, datetime

        from mediaman.services.scheduled_actions._types import resolve_keep_decision

        decision = resolve_keep_decision("forever", days=None, now=datetime.now(UTC))
        assert decision.execute_at is None
        assert decision.snooze_duration_days is None

    def test_raises_not_assertion_error(self):
        """The exception must be ValueError, not AssertionError, so -O builds are safe."""
        from datetime import UTC, datetime

        from mediaman.services.scheduled_actions._types import resolve_keep_decision

        with pytest.raises(ValueError):
            resolve_keep_decision("7 days", days=None, now=datetime.now(UTC))

        # Explicitly confirm it is NOT an AssertionError
        try:
            resolve_keep_decision("7 days", days=None, now=datetime.now(UTC))
        except ValueError:
            pass
        except AssertionError:
            pytest.fail("resolve_keep_decision raised AssertionError; must raise ValueError")


# ---------------------------------------------------------------------------
# M-04: is_pending_unexpired — membership check for delete_status
# ---------------------------------------------------------------------------


class TestIsPendingUnexpired:
    """M-04: both None and 'pending' are actionable; 'deleting' is not."""

    def _make_action(self, *, delete_status, execute_at_offset_days=1):
        from datetime import UTC, datetime, timedelta

        from mediaman.services.scheduled_actions._types import VerifiedKeepAction

        now = datetime.now(UTC)
        execute_at = (now + timedelta(days=execute_at_offset_days)).isoformat()
        return (
            VerifiedKeepAction(
                id=1,
                media_item_id="m1",
                action="scheduled_deletion",
                scheduled_at=now.isoformat(),
                execute_at=execute_at,
                token=None,
                token_used=0,
                snoozed_at=None,
                snooze_duration=None,
                notified=0,
                is_reentry=0,
                delete_status=delete_status,
                token_hash=None,
                title="T",
                media_type="movie",
                poster_path=None,
                file_size_bytes=0,
                plex_rating_key="rk",
                added_at=now.isoformat(),
                show_title=None,
                season_number=None,
            ),
            now,
        )

    def test_none_delete_status_is_actionable(self):
        from mediaman.services.scheduled_actions._mutations import is_pending_unexpired

        action, now = self._make_action(delete_status=None)
        assert is_pending_unexpired(action, now) is True

    def test_pending_delete_status_is_actionable(self):
        from mediaman.services.scheduled_actions._mutations import is_pending_unexpired

        action, now = self._make_action(delete_status="pending")
        assert is_pending_unexpired(action, now) is True

    def test_deleting_status_is_not_actionable(self):
        from mediaman.services.scheduled_actions._mutations import is_pending_unexpired

        action, now = self._make_action(delete_status="deleting")
        assert is_pending_unexpired(action, now) is False

    def test_deleted_status_is_not_actionable(self):
        from mediaman.services.scheduled_actions._mutations import is_pending_unexpired

        action, now = self._make_action(delete_status="deleted")
        assert is_pending_unexpired(action, now) is False


# ---------------------------------------------------------------------------
# H-02: find_active_keep_action_by_id_and_token must be absent from public API
# ---------------------------------------------------------------------------


class TestDeadFunctionRemoved:
    """H-02: the dead, HMAC-unverified lookup function must not be importable."""

    def test_function_absent_from_package_all(self):
        from mediaman.services import scheduled_actions

        assert "find_active_keep_action_by_id_and_token" not in scheduled_actions.__all__, (
            "find_active_keep_action_by_id_and_token is a dead, HMAC-unverified export "
            "that must not appear in the public __all__"
        )

    def test_function_absent_from_keep_py_all(self):
        from mediaman.web.routes import keep

        assert "find_active_keep_action_by_id_and_token" not in keep.__all__, (
            "keep.py must not re-export the removed function"
        )

    def test_function_not_importable_from_package(self):
        with pytest.raises(ImportError):
            from mediaman.services.scheduled_actions import (  # noqa: F401
                find_active_keep_action_by_id_and_token,
            )


# ---------------------------------------------------------------------------
# is_keep_token_consumed: shared replay-check helper used by both keep routes
# ---------------------------------------------------------------------------


class TestIsKeepTokenConsumed:
    """The replay-check helper backs the 409 path in both keep handlers."""

    def test_returns_false_for_unseen_token_hash(self, conn):
        from mediaman.services.scheduled_actions import is_keep_token_consumed, token_hash

        assert is_keep_token_consumed(conn, token_hash("never-used")) is False

    def test_returns_true_once_token_is_consumed(self, conn):
        from datetime import UTC, datetime

        from mediaman.services.scheduled_actions import (
            is_keep_token_consumed,
            mark_token_consumed,
            token_hash,
        )

        token = "the-keep-token"
        with conn:
            assert mark_token_consumed(conn, token, datetime.now(UTC)) is True

        assert is_keep_token_consumed(conn, token_hash(token)) is True

    def test_takes_the_hash_not_the_raw_token(self, conn):
        """The helper compares against stored hashes, so passing the raw
        token must not match a consumed entry."""
        from datetime import UTC, datetime

        from mediaman.services.scheduled_actions import (
            is_keep_token_consumed,
            mark_token_consumed,
        )

        token = "another-keep-token"
        with conn:
            mark_token_consumed(conn, token, datetime.now(UTC))

        assert is_keep_token_consumed(conn, token) is False
