"""Tests for mediaman.services.openai.recommendations.throttle."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mediaman.db import init_db
from mediaman.services.openai.recommendations.throttle import (
    last_manual_refresh,
    record_manual_refresh,
)


@pytest.fixture
def conn(db_path):
    c = init_db(str(db_path))
    yield c
    c.close()


class TestRecordManualRefresh:
    def test_stores_and_reads_back_timestamp(self, conn):
        """record_manual_refresh must persist the timestamp and last_manual_refresh returns it."""
        when = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        record_manual_refresh(conn, when)
        result = last_manual_refresh(conn)
        assert result is not None
        assert result == when

    def test_upsert_updates_existing_value(self, conn):
        """Calling record_manual_refresh twice must update, not duplicate the row."""
        first = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
        second = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        record_manual_refresh(conn, first)
        record_manual_refresh(conn, second)
        result = last_manual_refresh(conn)
        assert result == second
        # Must only be one row in settings for this key.
        rows = conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key = 'last_manual_recommendation_refresh'"
        ).fetchone()
        assert rows[0] == 1

    def test_uses_transaction_context_manager(self, conn):
        """F-11: record_manual_refresh must use with-conn: so failure rolls back cleanly.

        Verify the write is committed (readable after the call on the same
        connection) — the ``with conn:`` pattern auto-commits on success and
        auto-rolls-back on failure, unlike a bare ``conn.commit()``.
        """
        when = datetime(2026, 6, 5, 8, 30, 0, tzinfo=UTC)
        record_manual_refresh(conn, when)
        # Read it back via a raw query — if the commit was deferred or
        # missing this would return None.
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'last_manual_recommendation_refresh'"
        ).fetchone()
        assert row is not None
        assert "2026-06-05" in row["value"]
