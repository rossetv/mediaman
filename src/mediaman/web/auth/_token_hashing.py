"""Shared SHA-256 token hashing for session and reauth bookkeeping.

Both :mod:`mediaman.web.auth.session_store` and :mod:`mediaman.web.auth.reauth`
need to derive the at-rest hash of a session token in order to look up
or revoke the matching row.  Historically each module carried its own
private ``_hash_token`` helper with an identical SHA-256 implementation.
Two definitions that MUST stay byte-for-byte identical is exactly the
sort of thing that drifts silently — if one side moves to SHA-512 and
the other doesn't, every reauth ticket suddenly stops matching its
session and the symptom shows up only in the logs.

Centralise the hash here so a future change touches one place.
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
