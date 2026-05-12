"""Private helpers for :mod:`mediaman.web.auth.password_hash`.

Owns the bcrypt-input preparation pipeline (NFKC normalise → SHA-256
pre-hash for >72-byte inputs → bcrypt-ready bytes), the cached dummy
hash for constant-time-failure paths, the TOCTOU sentinels used inside
``with conn:`` blocks, and the log-field sanitiser.

Lives alongside ``password_hash.py`` rather than at package root because
nothing outside the auth package should consume these — every name in
here begins with an underscore on purpose.
"""

from __future__ import annotations

import base64
import hashlib
import re as _re
import threading
import unicodedata

import bcrypt

#: Single source of truth for the bcrypt cost factor. Tied together so
#: the dummy hash and every real hash use the same work factor — a
#: drift here would create a measurable timing channel between
#: nonexistent-user and real-user code paths.
BCRYPT_ROUNDS = 12

#: Hard cap on bcrypt input. ``bcrypt.hashpw`` silently truncates above
#: this; we redirect oversized inputs through a SHA-256 pre-hash so the
#: full input contributes to the final digest.
_BCRYPT_MAX_INPUT_BYTES = 72

# Characters allowed in sanitised log fields. Anything else is stripped
# before interpolation so usernames cannot inject CR/LF or control
# characters into log lines.
_LOG_FIELD_RE = _re.compile(r"[^A-Za-z0-9._@\-]")


def _sanitise_log_field(value: str, limit: int = 64) -> str:
    """Strip non-safe characters from *value* and truncate to *limit*."""
    truncated = len(value) > limit
    sanitised = _LOG_FIELD_RE.sub("", value)[:limit]
    return sanitised + "..." if truncated else sanitised


def _normalise_password(password: str) -> str:
    """NFKC-normalise *password* so visually-identical strings agree.

    Different OSes / IMEs emit different byte sequences for the same
    visible character — e.g. ``é`` may arrive as a single precomposed
    code point or as ``e`` + combining acute. Without normalisation,
    those two strings hash to different bcrypt outputs, so a user who
    set their password on one platform cannot log in from another.
    NFKC also folds compatibility forms (full-width digits, ligatures,
    etc.) so the normalised representation is stable across input
    methods.
    """
    return unicodedata.normalize("NFKC", password)


def _prepare_bcrypt_input(password: str) -> bytes:
    """Return the bytes that should be fed into ``bcrypt.hashpw``/``checkpw``.

    Inputs that fit within bcrypt's 72-byte limit (after NFKC
    normalisation) are passed straight through, so existing hashes that
    were generated before this defence landed continue to verify
    against the same bytes. Inputs that exceed the limit are pre-hashed
    with SHA-256 and base64-encoded — the result is 44 bytes, well
    under bcrypt's threshold, so the full entropy of the original
    password reaches the final digest.

    Both ``hashpw`` and ``checkpw`` MUST route through this helper so
    the encoding stays symmetric. A mismatch would lock every user out.
    """
    normalised = _normalise_password(password)
    encoded = normalised.encode("utf-8")
    if len(encoded) <= _BCRYPT_MAX_INPUT_BYTES:
        return encoded
    digest = hashlib.sha256(encoded).digest()
    # base64 of a 32-byte digest is 44 chars — comfortably under bcrypt's
    # 72-byte limit. We keep the b64 padding (rather than stripping it)
    # so the encoding is exactly what stdlib base64 produces, leaving
    # zero ambiguity when re-deriving on verify.
    return base64.b64encode(digest)


class _UserVanished(Exception):
    """TOCTOU sentinel: the target user was deleted between the auth check and the UPDATE.

    Used inside ``change_password`` to abort the ``with conn:`` block
    (forcing rollback of the half-written transaction) and signal back to
    the outer handler that the change should return ``False`` instead of
    claiming success. Private because the public contract is ``return
    False`` — callers must never see this exception.
    """


class _LastUser(Exception):
    """Last-admin guard sentinel: refused to delete the only remaining admin.

    Used inside ``delete_user`` to abort the ``with conn:`` block
    (forcing rollback of the half-written transaction) and signal back to
    the outer handler that the delete should return ``False``. Private —
    the public contract is ``return False``.
    """


# Module-global dummy hash: computed once at cold-start cost (bcrypt) and
# reused on every unauthenticated request to prevent timing-attack enumeration
# of valid usernames — a per-request hash would be both wasteful and less stable.
_DUMMY_HASH: bytes | None = None
_DUMMY_HASH_LOCK = threading.Lock()


def _get_dummy_hash() -> bytes:
    """Lazily compute the bcrypt dummy hash the first time it's needed."""
    global _DUMMY_HASH
    with _DUMMY_HASH_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
        return _DUMMY_HASH
