"""Encryption and HMAC-token signing — facade over :mod:`aes` and :mod:`tokens`.

Split from the original monolithic ``crypto.py`` (R3). Callers continue
to import every symbol from :mod:`mediaman.crypto`.
"""
# ruff: noqa: F401, I001 — this module is a deliberate re-export facade;
# the "unused" imports ARE the public API, and ruff's import-ordering
# rewrite would split the submodule-imports from the ``import hmac``
# line that callers patch via ``mediaman.crypto.hmac.compare_digest``.

# Re-export so ``mediaman.crypto.hmac.compare_digest`` patches still bind —
# the test suite monkeypatches this attribute on the package.
import hmac

from .aes import (
    # Public API
    canary_check,
    decrypt_value,
    encrypt_value,
    # Used by config.py
    _secret_key_looks_strong,
    # Used by tests (test-only concession — these are internal implementation details)
    _MAX_CIPHERTEXT_LEN,
    _db_path,
    _derive_aes_key_hkdf,
    _load_or_create_salt,
    _salt_cache,
)
from .tokens import (
    # Public API
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
    # Used by tests (test-only concession — these are internal implementation details)
    _TOKEN_PURPOSE_KEEP,
    _encode_signed,
    _validate_signed,
)

__all__ = [
    "_derive_aes_key_hkdf",
    "_derive_aes_key_legacy",
    "_load_or_create_salt",
    "_secret_key_looks_strong",
    "canary_check",
    "decrypt_value",
    "encrypt_value",
    "generate_download_token",
    "generate_keep_token",
    "generate_poll_token",
    "generate_poster_token",
    "generate_session_token",
    "generate_unsubscribe_token",
    "sign_poster_url",
    "validate_download_token",
    "validate_keep_token",
    "validate_poll_token",
    "validate_poster_token",
    "validate_unsubscribe_token",
]
