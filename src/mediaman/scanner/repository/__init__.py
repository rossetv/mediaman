"""SQL repository for the scanner — re-export barrel for the ``repo/`` package.

Every ``conn.execute(...)`` that talks to ``media_items``,
``scheduled_actions``, ``audit_log``, ``kept_shows`` or ``snoozes`` on
behalf of the scanner lives here. Keeping SQL in one module means the
engine, fetcher, and deletion executor read as orchestration — not as
a pile of string literals — and makes the schema contract easy to spot
when it changes.

**Package layout:**

* mediaman.scanner.repository.media_items — table-group 1:
  ``upsert_media_item``, ``update_last_watched``,
  ``count_items_in_libraries``, ``fetch_ids_in_libraries``,
  ``delete_media_items``.
* mediaman.scanner.repository.scheduled_actions — table-groups 2 & 3:
  protection/schedule reads and mutations on ``scheduled_actions`` and
  ``kept_shows``.
* mediaman.scanner.repository.settings — table-group 4:
  ``read_delete_allowed_roots_setting``, ``read_setting``.
* mediaman.scanner.repository.rekeyed — the keeps and Watch progress of titles
  Plex lists under a new key.
* mediaman.scanner.repository.watch_credit — table-group 5:
  ``last_credited_by_media_item``, the plays made in mediaman, and the last
  watch of titles Plex lists under a new key.

**Repository purity contract:** this package is pure SQL — it must not
import crypto primitives at module level.  Token generation lives in
phases.upsert.schedule_deletion which owns the HMAC keep-token
logic; this package has no dependency on mediaman.crypto.

This package depends only on sqlite3; it MUST NOT import from
``fetch`` or ``deletions`` (see engine.py header for the import-cycle
rule).
"""

from __future__ import annotations

from mediaman.scanner.repository.media_items import (
    count_items_in_libraries,
    delete_media_items,
    fetch_ids_in_libraries,
    fetch_ids_touched_since,
    fetch_places,
    update_last_watched,
    upsert_media_item,
)
from mediaman.scanner.repository.rekeyed import (
    carry_pruned_keeps,
    carry_show_keeps,
    carry_stranded_keeps,
    forget_pruned_show_season,
    is_kept_under_another_key,
    refresh_progress_keys,
    restore_progress,
    save_pruned_progress,
    save_pruned_show_seasons,
)
from mediaman.scanner.repository.scheduled_actions import (
    DELETION_ACTION,
    DeletionRow,
    book_deletion,
    book_removed_files,
    book_removed_files_where,
    cleanup_expired_show_snoozes,
    cleanup_expired_snoozes,
    clear_pending_deletions,
    count_pending_deletions,
    delete_actions_for_media_items,
    delete_scheduled_action,
    delete_show_scheduled_deletions,
    fetch_already_scheduled_media_ids,
    fetch_kept_show_keys,
    fetch_pending_deletions,
    fetch_pending_unmonitors,
    fetch_protected_media_ids,
    fetch_stuck_deletions,
    fetch_unbooked_media_ids,
    forget_unremoved_files,
    has_expired_snooze,
    is_already_scheduled,
    is_protected,
    is_show_kept,
    is_show_kept_pure,
    mark_delete_status,
    restore_keeps,
    save_pruned_keeps,
    take_pruned_keeps,
)
from mediaman.scanner.repository.settings import (
    read_delete_allowed_roots_setting,
    read_setting,
)
from mediaman.scanner.repository.watch_credit import (
    carry_last_watched,
    last_credited_by_media_item,
    save_pruned_watches,
)

__all__ = [
    "DELETION_ACTION",
    "DeletionRow",
    "book_deletion",
    "book_removed_files",
    "book_removed_files_where",
    "carry_last_watched",
    "carry_pruned_keeps",
    "carry_show_keeps",
    "carry_stranded_keeps",
    "cleanup_expired_show_snoozes",
    "cleanup_expired_snoozes",
    "clear_pending_deletions",
    "count_items_in_libraries",
    "count_pending_deletions",
    "delete_actions_for_media_items",
    "delete_media_items",
    "delete_scheduled_action",
    "delete_show_scheduled_deletions",
    "fetch_already_scheduled_media_ids",
    "fetch_ids_in_libraries",
    "fetch_ids_touched_since",
    "fetch_kept_show_keys",
    "fetch_pending_deletions",
    "fetch_pending_unmonitors",
    "fetch_places",
    "fetch_protected_media_ids",
    "fetch_stuck_deletions",
    "fetch_unbooked_media_ids",
    "forget_pruned_show_season",
    "forget_unremoved_files",
    "has_expired_snooze",
    "is_already_scheduled",
    "is_kept_under_another_key",
    "is_protected",
    "is_show_kept",
    "is_show_kept_pure",
    "last_credited_by_media_item",
    "mark_delete_status",
    "read_delete_allowed_roots_setting",
    "read_setting",
    "refresh_progress_keys",
    "restore_keeps",
    "restore_progress",
    "save_pruned_keeps",
    "save_pruned_progress",
    "save_pruned_show_seasons",
    "save_pruned_watches",
    "take_pruned_keeps",
    "update_last_watched",
    "upsert_media_item",
]
