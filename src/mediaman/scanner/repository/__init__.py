"""SQL repository for the scanner — re-export barrel for the ``repo/`` package.

Every ``conn.execute(...)`` that talks to ``media_items``,
``scheduled_actions``, ``audit_log``, ``kept_shows`` or ``snoozes`` on
behalf of the scanner lives here. Keeping SQL in one module means the
engine, fetcher, and deletion executor read as orchestration — not as
a pile of string literals — and makes the schema contract easy to spot
when it changes.

**Package layout:**

* :mod:`mediaman.scanner.repository.media_items` — table-group 1:
  ``upsert_media_item``, ``update_last_watched``,
  ``count_items_in_libraries``, ``fetch_ids_in_libraries``,
  ``delete_media_items``.
* :mod:`mediaman.scanner.repository.scheduled_actions` — table-groups 2 & 3:
  protection/schedule reads and mutations on ``scheduled_actions`` and
  ``kept_shows``.
* :mod:`mediaman.scanner.repository.settings` — table-group 4:
  ``read_delete_allowed_roots_setting``.

**Repository purity contract:** this package is pure SQL — it must not
import crypto primitives at module level.  The one historical exception
is :func:`schedule_deletion`, which generates an HMAC keep-token after
the INSERT so it can bind the token to the assigned ``action_id``.  The
production scan path now delegates to :func:`phases.upsert.schedule_deletion`
which owns the token generation; this function is kept for back-compat
with tests and any out-of-engine callers.  The ``generate_keep_token``
import is lazy (inside the function body) so this package has no
module-level dependency on :mod:`mediaman.crypto`.

This package depends only on :mod:`sqlite3`; it MUST NOT import from
``fetch`` or ``deletions`` (see engine.py header for the import-cycle
rule).
"""

from mediaman.scanner.repository.media_items import (
    count_items_in_libraries,
    delete_media_items,
    fetch_ids_in_libraries,
    update_last_watched,
    upsert_media_item,
)
from mediaman.scanner.repository.scheduled_actions import (
    DELETION_ACTION,
    _TOKEN_TTL_DAYS,
    _is_show_kept_pure,
    cleanup_expired_show_snoozes,
    cleanup_expired_snoozes,
    delete_scheduled_action,
    fetch_pending_deletions,
    fetch_stuck_deletions,
    has_expired_snooze,
    is_already_scheduled,
    is_protected,
    is_show_kept,
    mark_delete_status,
    schedule_deletion,
)
from mediaman.scanner.repository.settings import read_delete_allowed_roots_setting

__all__ = [
    # media_items
    "upsert_media_item",
    "update_last_watched",
    "count_items_in_libraries",
    "fetch_ids_in_libraries",
    "delete_media_items",
    # scheduled_actions — reads
    "is_protected",
    "is_already_scheduled",
    "has_expired_snooze",
    "_is_show_kept_pure",
    "cleanup_expired_show_snoozes",
    "is_show_kept",
    # scheduled_actions — mutations
    "schedule_deletion",
    "fetch_stuck_deletions",
    "fetch_pending_deletions",
    "mark_delete_status",
    "delete_scheduled_action",
    "cleanup_expired_snoozes",
    "DELETION_ACTION",
    "_TOKEN_TTL_DAYS",
    # settings
    "read_delete_allowed_roots_setting",
]
