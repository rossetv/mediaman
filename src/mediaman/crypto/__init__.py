"""Encryption and HMAC-token signing — facade over aes and tokens.

Callers import every symbol from mediaman.crypto.

Allowed dependencies: cryptography (AESGCM, HKDF), stdlib hmac,
hashlib, secrets, and mediaman.core (time helpers only).
Forbidden: no DB access beyond canary/salt rows (via mediaman.crypto.aes
only), no logging of key material, no imports from mediaman.services or
mediaman.web.
"""

from __future__ import annotations

# ruff: noqa: F401 — this module is a deliberate re-export facade;
# the "unused" imports ARE the public API, and the explicit ``import hmac``
# line is patched by callers via ``mediaman.crypto.hmac.compare_digest``.
# Re-export so ``mediaman.crypto.hmac.compare_digest`` patches still bind —
# the test suite monkeypatches this attribute on the package.
import hmac

from ._aes_key import CryptoError, CryptoInputError
from .aes import (
    decrypt_value,
    encrypt_value,
    is_canary_valid,
)
from .tokens import (
    generate_download_token,
    generate_keep_token,
    generate_poll_token,
    generate_poster_token,
    generate_session_token,
    generate_unsubscribe_token,
    sign_poster_url,
    sign_tmdb_image_url,
    validate_download_token,
    validate_keep_token,
    validate_poll_token,
    validate_poster_token,
    validate_tmdb_image_token,
    validate_unsubscribe_token,
)

__all__ = [
    "CryptoError",
    "CryptoInputError",
    "decrypt_value",
    "encrypt_value",
    "generate_download_token",
    "generate_keep_token",
    "generate_poll_token",
    "generate_poster_token",
    "generate_session_token",
    "generate_unsubscribe_token",
    "is_canary_valid",
    "sign_poster_url",
    "sign_tmdb_image_url",
    "validate_download_token",
    "validate_keep_token",
    "validate_poll_token",
    "validate_poster_token",
    "validate_tmdb_image_token",
    "validate_unsubscribe_token",
]
