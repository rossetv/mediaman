"""Shared constants, regexes, and validation helpers for the models package.

These primitives are imported by every other module in
:mod:`mediaman.web.models`; nothing in this file is itself a Pydantic
model.  Keeping them isolated lets the per-domain submodules
(:mod:`auth`, :mod:`settings`, :mod:`subscribers`) stay focused on the
shapes they declare.
"""

from __future__ import annotations

import re

#: Field-level cap on password input.  bcrypt itself only consumes 72
#: bytes, but we accept up to this many characters at the API surface
#: so a passphrase user gets a clear "too long" rejection instead of a
#: silent truncation.  Anything bigger than this is almost certainly a
#: log-injection or DoS payload.
_MAX_PASSWORD_LEN = 1024

#: Field-level cap on username input.  RFC 5321 caps SMTP local-parts
#: at 64 characters; usernames here are even shorter in practice
#: (admin/operator handles).  256 is a generous bound that accommodates
#: any UTF-8 username we might reasonably see.
_MAX_USERNAME_LEN = 256

#: RFC 5321 maximum length for an email address (64 + 1 + 255).
_MAX_EMAIL_LEN = 320

#: Permissive subset of RFC 5322 — same regex used by the subscribers
#: route helper (kept in sync) so adding a subscriber via the model
#: layer mirrors the route's hand-rolled validator without pulling in
#: ``email-validator`` as a hard dependency.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

#: URL fields cap at 2048 — matches ``_validate_url``'s explicit
#: length check; kept as a Field-level bound so the rejection
#: surfaces before the validator runs.
_URL_MAX = 2048

#: API-key / token fields cap at 1024 — matches the upper bound in
#: ``_API_KEY_RE``.
_SECRET_MAX = 1024

#: Hostname (incl. fully-qualified) max per RFC 1035 is 253; round up
#: to 256 for a comfortable margin.
_HOST_MAX = 256

# ---------------------------------------------------------------------------
# Action type constants — canonical string values stored in scheduled_actions
# ---------------------------------------------------------------------------

ACTION_PROTECTED_FOREVER = "protected_forever"
ACTION_SNOOZED = "snoozed"
ACTION_SCHEDULED_DELETION = "scheduled_deletion"

# ---------------------------------------------------------------------------
# Keep duration vocabulary — maps canonical long-form label to days (None = forever)
# ---------------------------------------------------------------------------

VALID_KEEP_DURATIONS: dict[str, int | None] = {
    "7 days": 7,
    "30 days": 30,
    "90 days": 90,
    "forever": None,
}

# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------

#: Compiled once; used by the CRLF validator on every string field.
#: Includes NUL (\x00) because a stray null byte in a value that is
#: subsequently used as a C string (libcurl, sqlite3, syslog) terminates
#: the rest of the value silently — the same blast radius as a CR/LF
#: header injection but harder to spot.
_CRLF_RE = re.compile(r"[\r\n\x00]")

#: API keys / tokens sent as HTTP headers must only contain ASCII printable
#: characters and must be short enough that no single header overflows a
#: reasonable server limit. NUL is excluded because it terminates C strings.
#: The upper bound is generous enough to accommodate JWT-style tokens —
#: TMDB v4 "API Read Access Tokens" are ~220+ character JWTs.
_API_KEY_RE = re.compile(r"^[\x20-\x7E]{1,1024}$")

#: Allowed URL schemes for service base URLs.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _reject_crlf(v: str | None) -> str | None:
    """Raise ``ValueError`` if *v* contains a CR, LF, or NUL character.

    These characters can be injected into HTTP headers when the value is
    used as an ``Authorization`` or ``X-Api-Key`` header. Rejecting them
    at the model layer means every code path — not just the settings route
    — is covered.

    NUL (``\\x00``) is also rejected: many downstream consumers treat
    strings as C strings (libcurl, sqlite3 BLOB-text coercion, syslog),
    so a smuggled NUL silently truncates the rest of the value at the
    boundary.
    """
    if v is not None and _CRLF_RE.search(v):
        raise ValueError("value must not contain CR, LF, or NUL characters")
    return v


def _validate_api_key(v: str | None) -> str | None:
    """Enforce API-key character set: ASCII printable, length 1–1024.

    Rejects CR, LF, NUL, and non-ASCII so an injected key can never
    corrupt an HTTP header line.  An empty string (used by the UI to
    signal "leave unchanged") is passed through so the route can apply
    its "****" / empty sentinel logic.
    """
    if v is None or v == "" or v == "****":
        return v
    if not _API_KEY_RE.match(v):
        raise ValueError("API key must be 1–1024 ASCII printable characters (no CR, LF, or NUL)")
    return v
