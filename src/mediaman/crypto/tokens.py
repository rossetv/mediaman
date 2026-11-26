"""HMAC-SHA256 signed, time-limited tokens.

Tokens are HMAC-signed, per-purpose domain-separated, and time-limited.
Each token type derives a per-purpose sub-key via
``HMAC-SHA256(secret, info)``, preventing cross-token confusion.

Each public ``generate_*`` / ``validate_*`` function wraps the private
``_encode_signed`` / ``_validate_signed`` primitives with a fixed
purpose label.  Adding a new token type means adding one purpose
constant plus one ``generate_*`` / ``validate_*`` pair.
"""

# rationale: one cohesive signing module — the TypedDict payload definitions,
# the per-purpose purpose constants, the sub-key cache, and the
# _encode_signed/_validate_signed primitives are all tightly coupled.
# Splitting TypedDicts to _token_types.py and HMAC primitives to
# _token_hmac.py would scatter the type contract from the signing
# implementation without reducing complexity; every caller would still
# import from both, so a forced split is not justified.

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import NotRequired, TypedDict, cast

logger = logging.getLogger(__name__)


class KeepTokenPayload(TypedDict):
    """Payload carried by a keep token (email "Keep" link)."""

    media_item_id: str
    """Plex rating key of the item to keep."""
    action_id: int
    """Scheduled-action row ID that triggered the email."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class DownloadTokenPayload(TypedDict):
    email: str | None
    """Recipient email address, or ``None`` when the issuing admin has no
    notification email set — the redeemer then skips the notification row
    rather than storing an undeliverable address."""
    act: str
    """Action identifier: ``"download"`` or ``"redownload"``."""
    title: str
    """Media title displayed in the download confirmation."""
    mt: str
    """Media type: ``"movie"`` or ``"tv"``."""
    tmdb: int | None
    """TMDB ID of the item, or ``None`` when unavailable."""
    sid: int | None
    """Recommendation row ID, or ``None`` when not originating from a recommendation. A
    season's Re-Download link carries the ``audit_log`` ID of its deletion here instead."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class UnsubscribeTokenPayload(TypedDict):
    email: str
    """Lower-cased email address to unsubscribe."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class PosterTokenPayload(TypedDict):
    rk: str
    """Plex rating key whose poster image is authorised."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class TmdbImageTokenPayload(TypedDict):
    img: str
    """TMDB picture authorised, as ``"<size>/<file name>"``."""
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
    used-tokens table and check it in ``validate_poll_token()``."""
    first: NotRequired[int | float]
    """UNIX timestamp at which the chain of renewals began. Absent on tokens
    issued before the field existed."""
    exp: int | float
    """UNIX timestamp at which the token expires."""


class TokenPayload(TypedDict, total=False):
    """Generic union of all fields that may appear in any token payload.

    Prefer the per-purpose TypedDicts (``KeepTokenPayload``,
    ``DownloadTokenPayload``, etc.) when the token type is known.  This
    fallback type exists for internal plumbing that must handle any token
    shape (e.g. _validate_signed).

    All fields are optional (``total=False``) because different token types
    carry different subsets.
    """

    exp: int | float
    email: str | None
    act: str
    title: str
    mt: str
    tmdb: int | None
    sid: int | None
    media_item_id: str
    action_id: int
    rk: str
    img: str
    mid: str
    svc: str
    nonce: str


# Outer cap on the dotted ``payload.signature`` token string. Tokens
# never legitimately approach this size — every issued token is
# bounded by a small JSON payload (a few hundred bytes, base64-encoded)
# plus a 44-char base64 signature. The 4 KiB cap exists to fast-fail
# obviously-malformed inputs at the very top of _validate_signed.
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
_TOKEN_PURPOSE_TMDB_IMAGE = b"mediaman-token-tmdb-image-v1"

# A TMDB picture's path ("/abc.jpg") or its image.tmdb.org address at any size.
_TMDB_IMAGE_RE = re.compile(
    r"(?:https://image\.tmdb\.org/t/p/[a-z0-9]{1,16})?/([A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png|webp))"
)

# Already-sent and pending keep links are rebuilt from these, so they must not change.
_KEEP_TOKEN_MIN_TTL = timedelta(days=30)
_KEEP_TOKEN_DELETION_MARGIN = timedelta(days=1)

_SUBKEY_CACHE_MAX_ENTRIES = 64

# Per-process HMAC sub-key cache; avoids re-deriving for every token validation.
# Single-worker invariant. The entry count is bounded by
# N_PURPOSES (6) × N_KEY_ROTATIONS — well below 64 in practice, so the
# clear-all eviction path is never triggered in production; a comment rather
# than lru_cache suffices here.
# The cache key is a SHA-256 digest of the master secret, NOT the raw
# plaintext key. Pinning the plaintext ``MEDIAMAN_SECRET_KEY`` as a
# live dict key for the whole process lifetime would needlessly widen
# the blast radius of a heap dump / core file / debugger inspection.
# The digest is non-reversible and still uniquely identifies the key for
# cache-lookup purposes.
_subkey_cache: dict[tuple[bytes, bytes], bytes] = {}
_subkey_cache_lock = threading.Lock()


def _derive_token_subkey(secret_key: str, purpose: bytes) -> bytes:
    """Derive a per-purpose HMAC sub-key from the master secret.

    The cache is keyed on ``sha256(secret_key)`` rather than the raw
    plaintext secret so the master key is never held as a long-lived
    dict key. The cache assumes a process-lifetime-constant
    secret key — an in-process key rotation would leave stale (but
    bounded, ≤ _SUBKEY_CACHE_MAX_ENTRIES) entries; rotation is
    a process restart in this single-worker design.
    """
    cache_key = (hashlib.sha256(secret_key.encode()).digest(), purpose)
    with _subkey_cache_lock:
        if cache_key in _subkey_cache:
            return _subkey_cache[cache_key]
    subkey = hmac.new(secret_key.encode(), purpose, hashlib.sha256).digest()
    with _subkey_cache_lock:
        # rationale: worst case ≈ N_PURPOSES × N_KEY_ROTATIONS, with comfortable
        # headroom; small clear-on-overflow is acceptable.
        if len(_subkey_cache) >= _SUBKEY_CACHE_MAX_ENTRIES:
            _subkey_cache.clear()
        _subkey_cache[cache_key] = subkey
    return subkey


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(secret_key: str, purpose: bytes, payload: bytes) -> bytes:
    subkey = _derive_token_subkey(secret_key, purpose)
    return hmac.new(subkey, payload, hashlib.sha256).digest()


def _validate_signed(token: str, secret_key: str, purpose: bytes) -> TokenPayload | None:
    """Shared validator for ``payload.signature`` tokens.

    Order of checks:

    1. Outer string-length cap (_MAX_TOKEN_LEN) and dotted shape.
    2. Decoded-payload length cap (_MAX_PAYLOAD_BYTES) — applied
       BEFORE _sign is called. An attacker who supplies an
       oversize payload should not force the server to compute
       HMAC-SHA256 over megabytes of attacker-controlled bytes before
       the constant-time signature comparison rejects the token.
    3. Both parts are canonical unpadded URL-safe base64.
    4. Constant-time signature comparison.
    5. JSON parse + dict-shape check.
    6. ``exp`` field — must be a non-bool int/float in the future.
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
        # One token, one spelling: the text must be exactly what _encode_signed emits.
        if _b64encode(payload_bytes) != parts[0] or _b64encode(sig) != parts[1]:
            return None
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
        #
        # ``math.isfinite`` rejects ``Infinity`` / ``NaN`` — ``json.loads``
        # parses those by default (a Python extension to strict JSON), and
        # ``float("inf") < time.time()`` is ``False``, which would make a
        # token with ``"exp": Infinity`` (or a huge float like ``1e308``)
        # effectively immortal and defeat the short-lived guarantee.
        if (
            isinstance(exp, bool)
            or not isinstance(exp, (int, float))
            or not math.isfinite(exp)
            or exp < time.time()
        ):
            return None
        return cast(TokenPayload, payload)
    except (
        ValueError,
        TypeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
    ):
        # Only the signature half is safe to log. For a dotless token the
        # leading bytes are the start of the base64 *payload* (potentially
        # the start of an email address), so log a fixed marker instead.
        logger.debug(
            "crypto.token_invalid purpose=%s sig_prefix=%s",
            purpose.decode(errors="replace"),
            (token.split(".", 1)[1][:8] if "." in token else "<no-dot>"),
        )
        return None


def _encode_signed(payload: Mapping[str, object], secret_key: str, purpose: bytes) -> str:
    """Encode a ``payload.signature`` token with domain-separated key."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    sig = _sign(secret_key, purpose, payload_bytes)
    return f"{_b64encode(payload_bytes)}.{_b64encode(sig)}"


def generate_keep_token(
    *,
    media_item_id: str,
    action_id: int,
    expires_at: int,
    secret_key: str,
) -> str:
    payload = {"media_item_id": media_item_id, "action_id": action_id, "exp": expires_at}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_KEEP)


def keep_token_expiry(scheduled_at: datetime, execute_at: datetime) -> int:
    """Return the ``exp`` of the keep token for a deletion scheduled at *scheduled_at*.

    At least 30 days, and always a day past the deletion so the link works until then.
    For a grace under 30 days this equals the 30-day rule.
    """
    return int(
        max(
            scheduled_at + _KEEP_TOKEN_MIN_TTL, execute_at + _KEEP_TOKEN_DELETION_MARGIN
        ).timestamp()
    )


def rebuild_keep_token(
    *,
    media_item_id: str,
    action_id: int,
    scheduled_at: datetime,
    execute_at: datetime,
    token_hash: str | None,
    secret_key: str,
) -> str | None:
    """Re-mint the keep token of a scheduled deletion from its stored columns.

    Only the token's SHA-256 is stored, so the token is regenerated (keep tokens carry
    no nonce) under the current expiry rule and then the 30-day rule; the one
    whose hash equals *token_hash* is returned, or ``None`` when neither matches.
    """
    if not token_hash:
        return None
    candidates = dict.fromkeys(
        (
            keep_token_expiry(scheduled_at, execute_at),
            int((scheduled_at + _KEEP_TOKEN_MIN_TTL).timestamp()),
        )
    )
    for expires_at in candidates:
        token = generate_keep_token(
            media_item_id=media_item_id,
            action_id=action_id,
            expires_at=expires_at,
            secret_key=secret_key,
        )
        if hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), token_hash):
            return token
    return None


def validate_keep_token(token: str, secret_key: str) -> KeepTokenPayload | None:
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_KEEP)
    return cast(KeepTokenPayload, result) if result is not None else None


def generate_download_token(
    *,
    email: str | None,
    action: str,
    title: str,
    media_type: str,
    tmdb_id: int | None,
    recommendation_id: int | None,
    secret_key: str,
    ttl_days: int = 14,
) -> str:
    """Issue a download-token for *email* with the standard 14-day TTL.

    *email* may be ``None`` when the issuing admin has no notification
    email set; the token still authorises the download, and the redeemer
    skips the download-notification row instead of storing an
    undeliverable address.

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
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_DOWNLOAD)
    return cast(DownloadTokenPayload, result) if result is not None else None


def generate_unsubscribe_token(
    *,
    email: str,
    secret_key: str,
    ttl_days: int = 180,
) -> str:
    exp = int(time.time()) + ttl_days * 86400
    payload = {"email": email.lower(), "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_UNSUBSCRIBE)


def validate_unsubscribe_token(token: str, secret_key: str) -> UnsubscribeTokenPayload | None:
    """Validate and decode an unsubscribe token produced by generate_unsubscribe_token.

    Returns the payload dict on success, or ``None`` when the token is
    invalid, expired, or tampered.  Callers are responsible for checking
    that ``payload["email"]`` matches the expected address.
    """
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_UNSUBSCRIBE)
    return cast(UnsubscribeTokenPayload, result) if result is not None else None


def generate_poster_token(
    *, rating_key: str, secret_key: str, ttl_days: int = 180, whole_days: bool = False
) -> str:
    """Issue a poster token. With *whole_days* it expires as its last day begins (UTC), so each
    one issued on a day is the same and a browser finds its picture in its cache.
    """
    exp = int(time.time()) + ttl_days * 86400
    if whole_days:
        exp -= exp % 86400
    payload = {"rk": rating_key, "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_POSTER)


def validate_poster_token(token: str, secret_key: str) -> PosterTokenPayload | None:
    """Validate and decode a poster token produced by generate_poster_token.

    Returns the payload dict on success, or ``None`` when the token is
    invalid, expired, or tampered.  Callers should verify ``payload["rk"]``
    matches the expected ``rating_key``.
    """
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_POSTER)
    return cast(PosterTokenPayload, result) if result is not None else None


def sign_poster_url(rating_key: str, secret_key: str, *, whole_days: bool = False) -> str:
    token = generate_poster_token(
        rating_key=rating_key, secret_key=secret_key, whole_days=whole_days
    )
    return f"/api/poster/{rating_key}?sig={token}"


def generate_tmdb_image_token(*, size: str, name: str, expires_at: int, secret_key: str) -> str:
    """Issue a token for one TMDB picture at one size. Its expiry is *expires_at* rounded up to
    a whole day (UTC), so each one issued for a picture on a day is the same and a browser finds
    it in its cache.
    """
    exp = expires_at + (-expires_at % 86400)
    payload = {"img": f"{size}/{name}", "exp": exp}
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_TMDB_IMAGE)


def validate_tmdb_image_token(token: str, secret_key: str) -> TmdbImageTokenPayload | None:
    """Validate and decode a token produced by generate_tmdb_image_token; ``None`` when it is
    invalid, expired or tampered. Callers check ``payload["img"]`` names the picture asked for.
    """
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_TMDB_IMAGE)
    return cast(TmdbImageTokenPayload, result) if result is not None else None


def sign_tmdb_image_url(
    image: object, size: str, secret_key: str, *, expires_at: int
) -> str | None:
    """The app's own signed address of a TMDB picture at *size*, given its path ("/abc.jpg") or
    its image.tmdb.org address; None for anything else.
    """
    match = _TMDB_IMAGE_RE.fullmatch(image) if isinstance(image, str) else None
    if match is None:
        return None
    name = match.group(1)
    token = generate_tmdb_image_token(
        size=size, name=name, expires_at=expires_at, secret_key=secret_key
    )
    return f"/api/img/tmdb/{size}/{name}?sig={token}"


def generate_poll_token(
    *,
    media_item_id: str,
    service: str,
    tmdb_id: int,
    secret_key: str,
    ttl_seconds: int = 600,
    first_issued: int | None = None,
) -> str:
    """Issue a short-lived polling-capability token with the standard 600-second TTL.

    ``first_issued`` (UNIX seconds) is when the chain of tokens this one
    renews began; it defaults to now. It rides along in the ``first`` field
    so the status endpoint can stop renewing a chain that has run for 24
    hours, which bounds how long a held token can be kept alive.

    The payload includes a random ``nonce`` purely to keep distinct
    issued tokens distinct on the wire — it is **not** a replay
    defence. There is no server-side used-tokens table, so any holder
    of a valid (unexpired) token can replay it freely. Replay
    resistance during the short TTL window comes entirely from the
    expiry, and renewal stops 24 hours after ``first`` (enforced by the
    status endpoint); tighten this by introducing a ``poll_tokens_used``
    table if/when the threat model demands it.
    """
    now = int(time.time())
    payload = {
        "mid": media_item_id,
        "svc": service,
        "tmdb": tmdb_id,
        "nonce": secrets.token_hex(8),
        "first": now if first_issued is None else first_issued,
        "exp": now + ttl_seconds,
    }
    return _encode_signed(payload, secret_key, _TOKEN_PURPOSE_POLL)


def validate_poll_token(token: str, secret_key: str) -> PollTokenPayload | None:
    """Validate and decode a poll token produced by generate_poll_token.

    Returns the payload dict on success, or ``None`` when the token is
    invalid, expired, or tampered.  Callers should verify ``payload["svc"]``
    and ``payload["tmdb"]`` match the expected service and TMDB ID.
    """
    result = _validate_signed(token, secret_key, _TOKEN_PURPOSE_POLL)
    return cast(PollTokenPayload, result) if result is not None else None


def generate_session_token() -> str:
    return secrets.token_hex(32)
