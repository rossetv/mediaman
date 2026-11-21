"""Shared SHA-256 token hashing for session and reauth bookkeeping.

Both `mediaman.web.auth.session_store` and `mediaman.web.auth.reauth`
need to derive the at-rest hash of a session token in order to look up
or revoke the matching row.  Both sides MUST hash identically: if one
moved to SHA-512 and the other did not, every reauth ticket would stop
matching its session and the symptom would show up only in the logs.

The hash lives here so a change touches one place.
"""

from __future__ import annotations

import hashlib


def hash_token(token: str) -> str:
    """Return the canonical at-rest hash for a session/reauth token.

    SHA-256 hex digest of the UTF-8 bytes of *token*.  Used as the
    primary key for ``admin_sessions.token_hash`` and
    ``reauth_tickets.session_token_hash`` so the plaintext token never
    lands in storage.
    """
    return hashlib.sha256(token.encode()).hexdigest()
