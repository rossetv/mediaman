"""Domain string constants for the `scheduled_actions` table's `action` column."""

from __future__ import annotations

#: The media item is kept indefinitely — never scheduled for deletion.
ACTION_PROTECTED_FOREVER = "protected_forever"

#: The media item is temporarily snoozed; deletion resumes after ``execute_at``.
ACTION_SNOOZED = "snoozed"

#: The media item is scheduled for deletion at ``execute_at``.
ACTION_SCHEDULED_DELETION = "scheduled_deletion"

__all__ = [
    "ACTION_PROTECTED_FOREVER",
    "ACTION_SCHEDULED_DELETION",
    "ACTION_SNOOZED",
]
