"""In-memory single-use token store for download confirmations."""

from __future__ import annotations

import hashlib
import threading
import time

_USED_TOKENS_LOCK = threading.Lock()
_USED_TOKENS: dict[str, float] = {}


def _mark_token_used(token: str, exp: int) -> bool:
    """Atomically mark *token* as consumed. Return False if already used."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _USED_TOKENS_LOCK:
        if len(_USED_TOKENS) > 1000:
            for k, v in list(_USED_TOKENS.items()):
                if v < now:
                    _USED_TOKENS.pop(k, None)
        if digest in _USED_TOKENS:
            return False
        _USED_TOKENS[digest] = float(exp)
        return True


def _unmark_token_used(token: str) -> None:
    """Release a previously claimed token so the user can retry."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.pop(digest, None)
