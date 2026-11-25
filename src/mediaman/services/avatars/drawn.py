"""The drawn avatars, and which of them an account has until its person sets one."""

from __future__ import annotations

import hashlib

#: Changing this number changes the default of accounts that never chose one.
COUNT = 10


def default_choice(user_id: int) -> int:
    """The drawing *user_id* has by default; ids are never reused, so it never changes."""
    digest = hashlib.sha256(f"avatar:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8]) % COUNT + 1


def url(choice: int) -> str:
    return f"/static/avatars/avatar-{choice:02d}.svg"
