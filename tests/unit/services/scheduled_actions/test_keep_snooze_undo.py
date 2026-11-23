"""Guards on the timed keep and on putting the deletion back after it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.services.scheduled_actions import apply_keep_snooze, revert_keep_snooze
from tests.helpers.factories import insert_media_item, insert_scheduled_action

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
OLD_DATE = (NOW + timedelta(days=2)).isoformat()
KEPT_UNTIL = (NOW + timedelta(days=30)).isoformat()
ROW_SQL = "SELECT * FROM scheduled_actions WHERE id = ?"


@pytest.fixture
def action_id(conn) -> int:
    insert_media_item(conn, id="100")
    return insert_scheduled_action(conn, media_item_id="100", execute_at=OLD_DATE, token="tok")


def _row(conn, action_id: int) -> dict:
    return dict(conn.execute(ROW_SQL, (action_id,)).fetchone())


def _keep(conn, action_id: int) -> int:
    return apply_keep_snooze(conn, action_id, "30 days", 30, NOW, expected_execute_at=OLD_DATE)


def _revert(conn, action_id: int, *, now: datetime = NOW) -> int:
    return revert_keep_snooze(
        conn,
        action_id,
        kept_until=KEPT_UNTIL,
        execute_at=OLD_DATE,
        snoozed_at=None,
        snooze_duration=None,
        now=now,
    )


def test_keep_is_refused_when_the_date_changed_since_it_was_read(conn, action_id):
    conn.execute(
        "UPDATE scheduled_actions SET execute_at = ? WHERE id = ?",
        ((NOW + timedelta(days=3)).isoformat(), action_id),
    )
    before = _row(conn, action_id)

    assert _keep(conn, action_id) == 0
    assert _row(conn, action_id) == before


def test_revert_puts_the_row_back_as_it_was_before_the_keep(conn, action_id):
    before = _row(conn, action_id)
    assert _keep(conn, action_id) == 1

    assert _revert(conn, action_id) == 1
    assert _row(conn, action_id) == before


@pytest.mark.parametrize(
    "change",
    [
        "action = 'protected_forever'",
        "delete_status = 'deleting'",
        "execute_at = '2027-02-21T12:00:00+00:00'",
    ],
)
def test_revert_leaves_a_row_changed_since_the_keep_alone(conn, action_id, change):
    _keep(conn, action_id)
    conn.execute(f"UPDATE scheduled_actions SET {change} WHERE id = ?", (action_id,))
    changed = _row(conn, action_id)

    assert _revert(conn, action_id) == 0
    assert _row(conn, action_id) == changed


def test_revert_does_not_bring_back_a_date_that_has_passed(conn, action_id):
    _keep(conn, action_id)
    kept = _row(conn, action_id)

    assert _revert(conn, action_id, now=NOW + timedelta(days=3)) == 0
    assert _row(conn, action_id) == kept
