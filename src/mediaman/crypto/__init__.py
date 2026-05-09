"""Encryption and HMAC-token signing — facade over :mod:`aes` and :mod:`tokens`.

Split from the original monolithic ``crypto.py`` (R3). Only the legitimate
public surface is re-exported here; tests that need implementation-detail
names import them from the canonical sub-module (``_aes_key``,
``aes``, or ``tokens``) directly.
"""
# ruff: noqa: F401 — the imports below ARE the public API surface.

# Re-export so ``mediaman.crypto.hmac.compare_digest`` patches still bind —
# the test suite monkeypatches this attribute on the package.
import hmac

from .aes import (
    decrypt_value,
    encrypt_value,
    is_canary_valid,
    migrate_legacy_ciphertexts,
)
from .tokens import (
    generate_download_token,
    generate_keep_token,
    generate_poll_token,
    generate_poster_token,
    generate_session_token,
    generate_unsubscribe_token,
    sign_poster_url,
    validate_download_token,
    validate_keep_token,
    validate_poll_token,
    validate_poster_token,
    validate_unsubscribe_token,
)

__all__ = [
    "decrypt_value",
    "encrypt_value",
    "generate_download_token",
    "generate_keep_token",
    "generate_poll_token",
    "generate_poster_token",
    "generate_session_token",
    "generate_unsubscribe_token",
    "is_canary_valid",
    "migrate_legacy_ciphertexts",
    "sign_poster_url",
    "validate_download_token",
    "validate_keep_token",
    "validate_poll_token",
    "validate_poster_token",
    "validate_unsubscribe_token",
]
