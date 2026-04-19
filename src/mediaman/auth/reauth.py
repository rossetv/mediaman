"""Password re-authentication helper.

Used to gate destructive operations behind a second password check so that a
compromised session cookie alone cannot trigger high-impact actions (delete
user, delete subscriber, send newsletter, etc.).
"""

from __future__ import annotations


def require_reauth(conn, admin: str, password: str) -> bool:
    """Return True if *password* matches *admin*'s current hash.

    A compromised session cookie WITHOUT the password cannot trigger flows
    guarded by this function.
    """
    if not password:
        return False
    from mediaman.auth.session import authenticate
    return authenticate(conn, admin, password)


# Underscore alias kept for callers that imported the private name.
_require_reauth = require_reauth
