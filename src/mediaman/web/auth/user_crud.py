"""Admin-user lookup helper that does not touch the bcrypt machinery.

Owns the narrow concern of resolving a numeric user ID to a username for
route handlers.  All bcrypt-bound operations (create, authenticate,
change-password, delete) live in :mod:`mediaman.web.auth.password_hash`.
"""

from __future__ import annotations

import sqlite3


def find_username_by_user_id(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Return the username for *user_id*, or None if no such user exists.

    The single sanctioned reader of ``admin_users.username`` outside the
    bcrypt-bound helpers in ``password_hash`` (§2.7.4: auth owns the users
    table; route handlers go through this module).
    """
    row = conn.execute(
        "SELECT username FROM admin_users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return row["username"] if row else None
