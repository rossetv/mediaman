"""Password strength policy — shared between user-creation, password-
change, and login-time re-evaluation.

Rules (NIST SP 800-63B-ish):

- Minimum length: 12 characters.
- Not a substring of or equal to the username (case-insensitive).
- Not in the top common-passwords list.
- At least three of four character classes: lowercase, uppercase,
  digits, symbols. We prefer passphrases — so we waive the class
  requirement when the password is ≥ 20 characters AND contains at
  least one whitespace OR has high unique-char variance
  (``len(set(pw)) >= 12``). That lets "correct horse battery staple"
  pass without forcing a symbol.
- Must not be trivial repetition (``len(set(pw)) >= 6`` minimum even
  with the passphrase exemption).

The public entry point is :func:`password_issues`, which returns an
ordered list of human-readable issues — an empty list means the
password is acceptable. UI layers render the list; server-side
validation just checks whether the list is empty.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_common_passwords() -> frozenset[str]:
    """Load the common-password list from the bundled data file (lazy, cached).

    The file lives at ``auth/data/common_passwords.txt`` (one lowercase
    entry per line, blank lines and ``#``-comments ignored). Wrapped in
    ``lru_cache`` so the file is read at most once per process — on first
    use rather than at module import, which keeps test collection fast and
    avoids touching the filesystem for modules that never call
    :func:`password_issues`. The ``frozenset`` makes membership checks O(1).
    """
    data_file = Path(__file__).parent / "data" / "common_passwords.txt"
    passwords: set[str] = set()
    with data_file.open(encoding="utf-8") as fh:
        for line in fh:
            entry = line.strip().lower()
            if entry and not entry.startswith("#"):
                passwords.add(entry)
    return frozenset(passwords)


def __getattr__(name: str) -> object:
    """Lazy module attribute for ``_COMMON_PASSWORDS``.

    Exposes ``_COMMON_PASSWORDS`` as a module-level name for backward
    compatibility (tests import it directly) while keeping the actual file
    read deferred until first use.
    """
    if name == "_COMMON_PASSWORDS":
        return _load_common_passwords()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


MIN_LENGTH = 12
MIN_UNIQUE = 6
PASSPHRASE_MIN_LENGTH = 20
PASSPHRASE_MIN_UNIQUE = 12


def _char_classes(password: str) -> set[str]:
    """Return the set of character classes present in *password*."""
    classes: set[str] = set()
    for c in password:
        if c.islower():
            classes.add("lower")
        elif c.isupper():
            classes.add("upper")
        elif c.isdigit():
            classes.add("digit")
        elif not c.isspace():
            classes.add("symbol")
    return classes


def _looks_like_passphrase(password: str) -> bool:
    """Accept long high-variance strings without requiring 3+ character classes.

    A passphrase qualifies when it is long enough AND either contains whitespace
    (multi-word phrase) or has high character variance (≥60% unique characters).
    """
    if len(password) < PASSPHRASE_MIN_LENGTH:
        return False
    if len(set(password)) < PASSPHRASE_MIN_UNIQUE:
        return False
    # Multi-word passphrase OR high variance (≥60% of characters are unique).
    has_whitespace = any(c.isspace() for c in password)
    high_variance = len(set(password)) / len(password) >= 0.6
    return has_whitespace or high_variance


def password_issues(password: str, username: str = "") -> list[str]:
    """Return a list of user-facing issues. Empty list = acceptable.

    Deterministic order: issues are returned in the order checked
    so the UI can display them consistently.
    """
    issues: list[str] = []

    if not password:
        return ["Password is required."]

    if len(password) < MIN_LENGTH:
        issues.append(f"Must be at least {MIN_LENGTH} characters (yours is {len(password)}).")

    if len(set(password)) < MIN_UNIQUE:
        issues.append("Avoid repeating the same few characters.")

    if username:
        low_pw = password.lower()
        low_user = username.lower().strip()
        if low_user and (low_user == low_pw or low_user in low_pw or low_pw in low_user):
            issues.append("Must not contain the username.")

    if password.lower() in _load_common_passwords():
        issues.append("This password is on the common-password list.")

    # Class-diversity requirement, waived for long passphrases.
    if not _looks_like_passphrase(password):
        classes = _char_classes(password)
        if len(classes) < 3:
            issues.append(
                "Mix at least three of: lowercase, uppercase, digits, symbols "
                "(or use a passphrase of 20+ characters)."
            )

    # Reject trivially-sequential passwords like "abcdefghijkl" or
    # "123456789012" even if they pass length — low unique-chars
    # usually catches them but this is a belt-and-braces check.
    if _is_sequential(password):
        issues.append("Avoid sequential characters (abc…, 123…, qwerty…).")

    return issues


def is_strong(password: str, username: str = "") -> bool:
    """Return True when *password* has zero policy issues."""
    return not password_issues(password, username)


_SEQUENCE_ALPHABETS = (
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
)


def _is_sequential(password: str) -> bool:
    """Return True if *password* is overwhelmingly sequential.

    Heuristic: more than 60% of adjacent pairs follow one of our
    alphabets forward or backward. Catches ``"abcdefghijkl"`` and
    ``"qwertyuiop12"`` while letting ``"Mycar-qwerty"`` through.
    """
    pw_low = password.lower()
    if len(pw_low) < 6:
        return False
    hits = 0
    total = len(pw_low) - 1
    for i in range(total):
        pair = pw_low[i : i + 2]
        for alpha in _SEQUENCE_ALPHABETS:
            if pair in alpha or pair in alpha[::-1]:
                hits += 1
                break
    return total > 0 and hits / total >= 0.6


def policy_summary() -> list[str]:
    """Return the human-readable policy summary for UI display."""
    return [
        f"At least {MIN_LENGTH} characters long.",
        "Mix of lowercase, uppercase, digits, and symbols "
        f"(or a passphrase of {PASSPHRASE_MIN_LENGTH}+ characters).",
        "Not a commonly used password.",
        "Must not contain the username.",
    ]
