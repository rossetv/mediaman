"""HMAC-SHA256 signed, time-limited tokens.

Each token type (``keep``, ``download``, ``unsubscribe``, ``poster``,
``poll``) derives a per-purpose sub-key via
``HMAC-SHA256(secret, info)``. This prevents cross-token confusion.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time

logger = logging.getLogger("mediaman")

_MAX_TOKEN_LEN = 4096

_TOKEN_PURPOSE_KEEP = b"mediaman-token-keep-v1"
_TOKEN_PURPOSE_DOWNLOAD = b"mediaman-token-download-v1"
_TOKEN_PURPOSE_UNSUBSCRIBE = b"mediaman-token-unsubscribe-v1"
_TOKEN_PURPOSE_POSTER = b"mediaman-token-poster-v1"
_TOKEN_PURPOSE_POLL = b"mediaman-token-poll-v1"


_subkey_cache: dict[tuple[str, bytes], bytes] = {}
_subkey_cache_lock = threading.Lock()


def _derive_token_subkey(secret_key: str, purpose: bytes) -> bytes:
    """Derive a per-purpose HMAC sub-key from the master secret."""
    cache_key = (secret_key, purpose)
    with _subkey_cache_lock:
        if cache_key in _subkey_cache:
            return _subkey_cache[cache_key]
    subkey = hmac.new(secret_key.encode(), purpose, hashlib.sha256).digest()
    with _subkey_cache_lock:
        _subkey_cache[cache_key] = subkey
    return subkey


def _sign(secret_key: str, purpose: bytes, payload: bytes) -> bytes:
    """Return HMAC-SHA256(subkey(secret,purpose), payload)."""
    subkey = _derive_token_subkey(secret_key, purpose)
    return hmac.new(subkey, payload, hashlib.sha256).digest()


def _validate_signed(token: str, secret_key: str, purpose: bytes) -> dict | None:
    """Shared validator for payload.signature tokens."""
    if not token or len(token) > _MAX_TOKEN_LEN:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_bytes = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
        sig = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        expected_sig = _sign(secret_key, purpose, payload_bytes)
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp", 0)
        if not isinstance(exp, (int, float)) or exp < time.time():
            return None
        return payload
    except (
        ValueError,
        TypeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
    ):
        logger.debug(
            "crypto.token_invalid purpose=%s sig_prefix=%s",
            purpose.decode(errors="replace"),
            (token.split(".", 1)[-1][:8] if "." in token else token[:8]),
        )
        return None


def _encode_signed(payload: dict, secret_key: str, purpose: bytes) -> str:
    """Encode a ``payload.signature`` token with domain-separated key."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    sig = _sign(secret_key, purpose, payload_bytes)
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def generate_keep_token(
    *,
    media_item_id: str,
    action_id: int,
    expires_at: int,
    secret_key: str,
) -> str:
    """Generate an HMAC-SHA256-signed keep token for email "Keep" links."""
    payload = {"media_item_id": media_item_id, "action_id": action_id, "exp": expires_at}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_KEEP)


def validate_keep_token(token: str, secret_key: str) -> dict | None:
    """Validate and decode a keep token produced by :func:`generate_keep_token`."""
    return _validate_signed(token, secret_key, _TOKEN_PURPOSE_KEEP)


def generate_download_token(
    *,
    email: str,
    action: str,
    title: str,
    media_type: str,
    tmdb_id: int | None,
    recommendation_id: int | None,
    secret_key: str,
    ttl_days: int = 14,
) -> str:
    """Generate an HMAC-SHA256-signed download token for email download CTAs."""
    exp = int(time.time()) + ttl_days * 86400
    payload = {
        "email": email,
        "act": action,
        "title": title,
        "mt": media_type,
        "tmdb": tmdb_id,
        "sid": recommendation_id,
        "exp": exp,
    }
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_DOWNLOAD)


def validate_download_token(token: str, secret_key: str) -> dict | None:
    """Validate and decode a download token produced by :func:`generate_download_token`."""
    return _validate_signed(token, secret_key, _TOKEN_PURPOSE_DOWNLOAD)


def generate_unsubscribe_token(
    *,
    email: str,
    secret_key: str,
    ttl_days: int = 180,
) -> str:
    """Generate a domain-separated unsubscribe token."""
    exp = int(time.time()) + ttl_days * 86400
    payload = {"email": email.lower(), "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_UNSUBSCRIBE)


def validate_unsubscribe_token(token: str, secret_key: str, email: str) -> bool:
    """Return True when *token* is a valid unsubscribe token for *email*."""
    payload = _validate_signed(token, secret_key, _TOKEN_PURPOSE_UNSUBSCRIBE)
    if payload is None:
        return False
    return payload.get("email", "").lower() == email.lower()


def generate_poster_token(rating_key: str, secret_key: str, *, ttl_days: int = 180) -> str:
    """Generate an HMAC token authorising access to a specific rating-key poster."""
    exp = int(time.time()) + ttl_days * 86400
    payload = {"rk": rating_key, "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_POSTER)


def validate_poster_token(token: str, secret_key: str, rating_key: str) -> bool:
    """Return True when *token* authorises access to *rating_key*."""
    payload = _validate_signed(token, secret_key, _TOKEN_PURPOSE_POSTER)
    if payload is None:
        return False
    return payload.get("rk") == rating_key


def sign_poster_url(rating_key: str, secret_key: str) -> str:
    """Return a signed ``/api/poster/{rating_key}?sig=...`` URL."""
    token = generate_poster_token(rating_key, secret_key)
    return f"/api/poster/{rating_key}?sig={token}"


def generate_poll_token(
    *,
    media_item_id: str,
    service: str,
    tmdb_id: int,
    secret_key: str,
    ttl_seconds: int = 600,
) -> str:
    """Generate a short-lived HMAC-signed polling-capability token."""
    exp = int(time.time()) + ttl_seconds
    payload = {
        "mid": media_item_id,
        "svc": service,
        "tmdb": tmdb_id,
        "nonce": secrets.token_hex(8),
        "exp": exp,
    }
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_POLL)


def validate_poll_token(
    token: str,
    secret_key: str,
    *,
    service: str,
    tmdb_id: int,
) -> bool:
    """Return True when *token* is a valid, unexpired poll token."""
    payload = _validate_signed(token, secret_key, _TOKEN_PURPOSE_POLL)
    if payload is None:
        return False
    return payload.get("svc") == service and payload.get("tmdb") == tmdb_id


def generate_session_token() -> str:
    """Generate a cryptographically random session token (64 hex chars)."""
    return secrets.token_hex(32)
