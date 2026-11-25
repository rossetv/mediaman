"""Private helpers for `mediaman.web.auth.password_hash`.

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
import threading
import unicodedata

import bcrypt

from mediaman.web.models._common import _MAX_USERNAME_LEN

#: Single source of truth for the bcrypt cost factor. Tied together so
#: the dummy hash and every real hash use the same work factor — a
#: drift here would create a measurable timing channel between
#: nonexistent-user and real-user code paths.
BCRYPT_ROUNDS = 12

#: Hard cap on bcrypt input. ``bcrypt.hashpw`` silently truncates above
#: this; we redirect oversized inputs through a SHA-256 pre-hash so the
#: full input contributes to the final digest.
_BCRYPT_MAX_INPUT_BYTES = 72


def _escape_log_char(char: str) -> str:
    """Return *char* as a ``\\x``, ``\\u`` or ``\\U`` escape."""
    code = ord(char)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _sanitise_log_field(value: str, limit: int = _MAX_USERNAME_LEN) -> str:
    """Return *value* made safe to write into a log line, cut to *limit* characters.

    Backslashes, whitespace and every non-printable character (control,
    line and paragraph separators, zero-width and other format characters)
    become escapes; the backslash is escaped too, so two different inputs
    never give the same text. Printable characters, accents and other
    scripts included, stay as they are. A cut value ends in ``...``.
    """
    out = "".join(
        char
        if char != "\\" and char.isprintable() and not char.isspace()
        else _escape_log_char(char)
        for char in value[:limit]
    )
    return out + "..." if len(value) > limit else out


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
    # Bytes for any string, so that every password can be checked against a hash.
    encoded = normalised.encode("utf-8", "surrogatepass")
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
    """Lazily compute the bcrypt dummy hash the first time it's needed.

    The ~150 ms bcrypt computation runs OUTSIDE the lock (locks
    stay narrow): a fast double-checked read returns the cached value
    when present, otherwise the hash is computed lock-free and the lock is
    held only to publish the result. Two cold-start callers may each
    compute a (different but equivalent) dummy hash; the first to take the
    lock wins and the loser's value is discarded — harmless, since any
    valid bcrypt hash of the dummy serves the constant-time purpose.
    """
    global _DUMMY_HASH
    cached = _DUMMY_HASH
    if cached is not None:
        return cached
    computed = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
    with _DUMMY_HASH_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = computed
        return _DUMMY_HASH
