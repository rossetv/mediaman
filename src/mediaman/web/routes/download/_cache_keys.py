"""Shared cache-key helper for confirm.py and status/__init__.py."""

from __future__ import annotations

import hashlib


def _key_fingerprint(secret_key: str) -> str:
    """Return a short SHA-256 fingerprint of secret_key for use as a cache key, so the full key
    never sits in the cache dict.
    """
    return hashlib.sha256(secret_key.encode()).hexdigest()[:16]
