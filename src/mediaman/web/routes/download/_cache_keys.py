"""Shared cache-key helpers for the download package.

Centralises ``_key_fingerprint`` so ``confirm.py`` and ``status/__init__.py``
share one implementation rather than copy-pasting it (§1.9).
"""

from __future__ import annotations

import hashlib


def _key_fingerprint(secret_key: str) -> str:
    """Return a short fingerprint of *secret_key* for use as a cache key.

    The full key never appears in the cache dict; only the first 16 hex
    characters of its SHA-256 hash.  Different deployments with different
    secrets do not collide, and the fingerprint is fast to compute on
    every cache lookup.
    """
    return hashlib.sha256(secret_key.encode()).hexdigest()[:16]
