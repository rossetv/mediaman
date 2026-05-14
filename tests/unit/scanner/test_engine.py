"""Scan engine tests — split into per-phase modules.

All tests have been moved to:
  - test_engine_fetch.py    (Plex fetch / run_scan / show-level keep / per-library orphan guard)
  - test_engine_decision.py (execute_deletions / delete-roots config / two-phase delete)
  - test_engine_cleanup.py  (orphan guard / concurrent scan guard)
  - test_engine_write.py    (dry_run suppression / _resolve_added_at)
"""

from __future__ import annotations
