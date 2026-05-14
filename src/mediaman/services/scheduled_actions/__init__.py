"""Shared service helpers for the ``scheduled_actions`` table.

Domain 02 noted that ``web/routes/keep.py`` and ``web/routes/kept.py`` had
extensive overlap: the same execute-at parsing, the same token-hash insert
into ``keep_tokens_used``, the same guarded UPDATE for snooze/forever, and
the same human-readable expiry formatter were copy-pasted across both
files (and twice within ``keep.py`` alone).  This package is the single
source of truth for that logic so the route layer stays thin.

All DB-bound helpers take ``conn: sqlite3.Connection`` as the first
positional argument and never call ``conn.commit()`` themselves —
commits and rollbacks remain the route's responsibility so a single
HTTP request still maps to a single transaction boundary.

Package layout:
- ``_types``: ``KeepDecision``, ``VerifiedKeepAction``, ``resolve_keep_decision``
- ``_lookup``: token hash, DB lookups, ``mark_token_consumed``
- ``_mutations``: date-parsing predicates, guarded UPDATE helpers
- ``_display``: human-readable expiry and date formatters
"""

from mediaman.services.scheduled_actions._display import (
    format_added_display,
    format_expiry,
)
from mediaman.services.scheduled_actions._lookup import (
    find_active_keep_action_by_id_and_token,
    lookup_verified_action,
    mark_token_consumed,
    token_hash,
)
from mediaman.services.scheduled_actions._mutations import (
    apply_keep_forever,
    apply_keep_snooze,
    is_pending_unexpired,
    parse_execute_at,
)
from mediaman.services.scheduled_actions._types import (
    KeepDecision,
    VerifiedKeepAction,
    resolve_keep_decision,
)

__all__ = [
    "KeepDecision",
    "VerifiedKeepAction",
    "apply_keep_forever",
    "apply_keep_snooze",
    "find_active_keep_action_by_id_and_token",
    "format_added_display",
    "format_expiry",
    "is_pending_unexpired",
    "lookup_verified_action",
    "mark_token_consumed",
    "parse_execute_at",
    "resolve_keep_decision",
    "token_hash",
]
