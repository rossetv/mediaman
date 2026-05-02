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
from typing import TypedDict, cast

logger = logging.getLogger("mediaman")


# ---------------------------------------------------------------------------
# Per-purpose TypedDicts — use these as return types for validate_* functions.
# ---------------------------------------------------------------------------


class KeepTokenPayload(TypedDict):
    """Payload carried by a keep token (email "Keep" link)."""

    media_item_id: str
    """Plex rating key of the item to keep."""
    action_id: int
    """Scheduled-action row ID that triggered the email."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class DownloadTokenPayload(TypedDict):
    """Payload carried by a download token (email download CTA)."""

    email: str
    """Recipient email address."""
    act: str
    """Action identifier: ``"download"`` or ``"redownload"``."""
    title: str
    """Media title displayed in the download confirmation."""
    mt: str
    """Media type: ``"movie"`` or ``"tv"``."""
    tmdb: int | None
    """TMDB ID of the item, or ``None`` when unavailable."""
    sid: int | None
    """Recommendation row ID, or ``None`` when not originating from a recommendation."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class UnsubscribeTokenPayload(TypedDict):
    """Payload carried by an unsubscribe token."""

    email: str
    """Lower-cased email address to unsubscribe."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class PosterTokenPayload(TypedDict):
    """Payload carried by a poster-access token."""

    rk: str
    """Plex rating key whose poster image is authorised."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class PollTokenPayload(TypedDict):
    """Payload carried by a short-lived polling-capability token."""

    mid: str
    """Media item identifier being polled."""
    svc: str
    """Service name: ``"radarr"`` or ``"sonarr"``."""
    tmdb: int
    """TMDB ID of the item being polled."""
    nonce: str
    """Random nonce — included to make every issued token unique even when
    every other field is identical. Note: this is **not** a replay defence
    on its own. Without a server-side ``poll_tokens_used`` table no consumed
    token is ever invalidated, so any holder can replay it until ``exp``.
    Replay protection during the short TTL is provided solely by the
    expiry check; if stronger semantics are needed later, add a
    used-tokens table and check it in :func:`validate_poll_token`."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


# ---------------------------------------------------------------------------
# Generic fallback — used by _validate_signed and cross-validator code that
# does not yet know which purpose was used.
# ---------------------------------------------------------------------------


class TokenPayload(TypedDict, total=False):
    """Generic union of all fields that may appear in any token payload.

    Prefer the per-purpose TypedDicts (``KeepTokenPayload``,
    ``DownloadTokenPayload``, etc.) when the token type is known.  This
    fallback type exists for internal plumbing that must handle any token
    shape (e.g. :func:`_validate_signed`).

    All fields are optional (``total=False``) because different token types
    carry different subsets.
    """

    exp: int | float
    email: str
    act: str
    title: str
    mt: str
    tmdb: int | None
    sid: int | None
    media_item_id: str
    action_id: int
    rk: str
    mid: str
    svc: str
    nonce: str


# Outer cap on the dotted ``payload.signature`` token string. Tokens
# never legitimately approach this size — every issued token is
# bounded by a small JSON payload (a few hundred bytes, base64-encoded)
# plus a 44-char base64 signature. The 4 KiB cap exists to fast-fail
# obviously-malformed inputs at the very top of :func:`_validate_signed`.
_MAX_TOKEN_LEN = 4096

# Cap on the *decoded* payload bytes inside the dotted token. Cuts the
# HMAC computation short on attacker-controlled oversize payloads — an
# attacker who supplies (say) a 100 KB payload would otherwise force
# HMAC-SHA256 over 100 KB before constant-time signature comparison
# rejects the request. Real payloads are JSON dicts of <500 bytes; 8 KiB
# leaves comfortable headroom while neutralising the amplification.
_MAX_PAYLOAD_BYTES = 8192

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


def _validate_signed(token: str, secret_key: str, purpose: bytes) -> TokenPayload | None:
    """Shared validator for ``payload.signature`` tokens.

    Order of checks:

    1. Outer string-length cap (:data:`_MAX_TOKEN_LEN`) and dotted shape.
    2. Decoded-payload length cap (:data:`_MAX_PAYLOAD_BYTES`) — applied
       BEFORE :func:`_sign` is called. An attacker who supplies an
       oversize payload should not force the server to compute
       HMAC-SHA256 over megabytes of attacker-controlled bytes before
       the constant-time signature comparison rejects the token.
    3. Constant-time signature comparison.
    4. JSON parse + dict-shape check.
    5. ``exp`` field — must be a non-bool int/float in the future.
       (``bool`` is a subclass of ``int`` in Python, so the explicit
       bool-rejection is required to stop ``"exp": True`` slipping
       through as ``int(True) == 1``.)
    """
    if not token or len(token) > _MAX_TOKEN_LEN:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        # Cap the b64 input so the eventual decoded payload is bounded
        # too; with a generous +4 fudge for padding this is the cheap
        # pre-HMAC short-circuit.
        if len(parts[0]) > _MAX_PAYLOAD_BYTES * 2:
            return None
        payload_bytes = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
        if len(payload_bytes) > _MAX_PAYLOAD_BYTES:
            return None
        sig = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        expected_sig = _sign(secret_key, purpose, payload_bytes)
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp", 0)
        # bool is a subclass of int — exclude it explicitly so
        # ``"exp": True`` (which would coerce to 1, far in the past)
        # cannot slide through. Same for ``"exp": False``.
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp < time.time():
            return None
        return cast(TokenPayload, payload)
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


def validate_keep_token(token: str, secret_key: str) -> KeepTokenPayload | None:
    """Validate and decode a keep token produced by :func:`generate_keep_token`."""
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_KEEP)
    return cast(KeepTokenPayload, result) if result is not None else None


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
    """Generate an HMAC-SHA256-signed download token for email download CTAs.

    .. warning::

       The generated token's payload includes the recipient's *email*
       in plain JSON, base64-encoded into the URL. Anyone with a copy
       of the token (e.g. a forwarded email, a server log, a referer
       header leak) can read the email address back out — base64 is
       a reversible encoding, not a confidentiality boundary.

       This is acceptable for the current threat model because the
       email is already known to the holder of the URL (they received
       it). It is **not** suitable for cases where leaking the email
       to a third party that observes the URL is unacceptable — for
       those, switch to a server-side opaque ID that maps to the email
       in a DB-side lookup. Changing the token shape now would
       invalidate every issued in-flight email link, so this stays as
       documented behaviour rather than a silent fix.
    """
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


def validate_download_token(token: str, secret_key: str) -> DownloadTokenPayload | None:
    """Validate and decode a download token produced by :func:`generate_download_token`."""
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_DOWNLOAD)
    return cast(DownloadTokenPayload, result) if result is not None else None


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


def validate_unsubscribe_token(token: str, secret_key: str) -> UnsubscribeTokenPayload | None:
    """Validate and decode an unsubscribe token produced by :func:`generate_unsubscribe_token`.

    Returns the payload dict on success, or ``None`` when the token is
    invalid, expired, or tampered.  Callers are responsible for checking
    that ``payload["email"]`` matches the expected address.
    """
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_UNSUBSCRIBE)
    return cast(UnsubscribeTokenPayload, result) if result is not None else None


def generate_poster_token(*, rating_key: str, secret_key: str, ttl_days: int = 180) -> str:
    """Generate an HMAC token authorising access to a specific rating-key poster."""
    exp = int(time.time()) + ttl_days * 86400
    payload = {"rk": rating_key, "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_POSTER)


def validate_poster_token(token: str, secret_key: str) -> PosterTokenPayload | None:
    """Validate and decode a poster token produced by :func:`generate_poster_token`.

    Returns the payload dict on success, or ``None`` when the token is
    invalid, expired, or tampered.  Callers should verify ``payload["rk"]``
    matches the expected ``rating_key``.
    """
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_POSTER)
    return cast(PosterTokenPayload, result) if result is not None else None


def sign_poster_url(rating_key: str, secret_key: str) -> str:
    """Return a signed ``/api/poster/{rating_key}?sig=...`` URL."""
    token = generate_poster_token(rating_key=rating_key, secret_key=secret_key)
    return f"/api/poster/{rating_key}?sig={token}"


def generate_poll_token(
    *,
    media_item_id: str,
    service: str,
    tmdb_id: int,
    secret_key: str,
    ttl_seconds: int = 600,
) -> str:
    """Generate a short-lived HMAC-signed polling-capability token.

    The payload includes a random ``nonce`` purely to keep distinct
    issued tokens distinct on the wire — it is **not** a replay
    defence. There is no server-side used-tokens table, so any holder
    of a valid (unexpired) token can replay it freely. Replay
    resistance during the short TTL window comes entirely from the
    expiry; tighten this by introducing a ``poll_tokens_used`` table
    if/when the threat model demands it.
    """
    exp = int(time.time()) + ttl_seconds
    payload = {
        "mid": media_item_id,
        "svc": service,
        "tmdb": tmdb_id,
        "nonce": secrets.token_hex(8),
        "exp": exp,
    }
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_POLL)


def validate_poll_token(token: str, secret_key: str) -> PollTokenPayload | None:
    """Validate and decode a poll token produced by :func:`generate_poll_token`.

    Returns the payload dict on success, or ``None`` when the token is
    invalid, expired, or tampered.  Callers should verify ``payload["svc"]``
    and ``payload["tmdb"]`` match the expected service and TMDB ID.
    """
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_POLL)
    return cast(PollTokenPayload, result) if result is not None else None


def generate_session_token() -> str:
    """Generate a cryptographically random session token (64 hex chars)."""
    return secrets.token_hex(32)
