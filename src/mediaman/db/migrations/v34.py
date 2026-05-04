"""Migration v34: drop deprecated abandon-search settings; reset throttle counters.

The three ``abandon_search_*`` keys have been replaced by a single
time-based ``auto_abandon_enabled`` toggle.  Existing rows are ignored by the
new code, but deleting them keeps the settings table clean.

Throttle counters are reset to 0 because the legacy flat-15-minute cadence
accumulated counts (149+ on long-stalled items) that have no useful meaning
under the new exponential backoff curve.  ``last_triggered_at`` is set to
epoch zero (1970-01-01) so the next poll's ``now - last >= interval`` check
passes immediately.

Both tables are guarded individually so the migration handles partial test
fixtures safely.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Delete obsolete settings rows and reset throttle counters."""
    if _table_exists(conn, "settings"):
        conn.execute(
            "DELETE FROM settings WHERE key IN ("
            "'abandon_search_visible_at',"
            "'abandon_search_escalate_at',"
            "'abandon_search_auto_multiplier')"
        )
    if _table_exists(conn, "arr_search_throttle"):
        conn.execute(
            "UPDATE arr_search_throttle "
            "SET search_count = 0, "
            "    last_triggered_at = '1970-01-01T00:00:00+00:00'"
        )
