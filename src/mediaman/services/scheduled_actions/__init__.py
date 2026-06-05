"""Shared service helpers for the ``scheduled_actions`` table.

The two keep-route handlers in ``web/routes/keep.py`` and
``web/routes/kept.py`` previously duplicated the same execute-at parsing,
token-hash insert into ``keep_tokens_used``, guarded UPDATE for
snooze/forever, and human-readable expiry formatter.  This package is the
single source of truth for that logic so the route layer stays thin.

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

from __future__ import annotations

from mediaman.services.scheduled_actions._display import (
    format_added_display,
    format_expiry,
)
from mediaman.services.scheduled_actions._lookup import (
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
    "format_added_display",
    "format_expiry",
    "is_pending_unexpired",
    "lookup_verified_action",
    "mark_token_consumed",
    "parse_execute_at",
    "resolve_keep_decision",
    "token_hash",
]
