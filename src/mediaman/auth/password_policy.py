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

import re


# Top-~300 common passwords — sourced from public breach corpora.
# Kept as a frozenset so membership is O(1). Entries are stored
# lowercase; callers compare against ``pw.lower()``.
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    p.lower()
    for p in (
        # Top 50 essentials
        "password", "password1", "password123", "password123!", "password!", "qwerty",
        "qwerty123", "qwertyuiop", "123456", "123456789", "12345678",
        "12345", "1234567", "111111", "1234567890", "letmein",
        "admin", "admin123", "administrator", "welcome", "welcome1",
        "changeme", "default", "login", "abc123", "abcdef",
        "iloveyou", "monkey", "dragon", "master", "superman",
        "batman", "sunshine", "princess", "football", "baseball",
        "trustno1", "shadow", "ashley", "michael", "jennifer",
        "jordan", "harley", "pepper", "summer", "winter",
        "spring", "autumn", "hello", "hello123", "secret",
        # Common keyboard walks and trivial patterns
        "qazwsx", "qazwsxedc", "asdf", "asdfgh", "asdfghjkl",
        "zxcvbn", "zxcvbnm", "1q2w3e", "1q2w3e4r", "1q2w3e4r5t",
        "qwer1234", "qwerty1", "passw0rd", "p@ssw0rd", "p@ssword",
        "passw0rd1", "passw0rd!", "correctbatteryhorse",
        # Variants of "mediaman"
        "mediaman", "mediaman1", "mediaman!", "mediaman123",
        "plex", "plex123", "plexserver",
        # Seasons + numbers
        "winter2023", "winter2024", "winter2025", "winter2026",
        "summer2023", "summer2024", "summer2025", "summer2026",
        "spring2023", "spring2024", "spring2025", "spring2026",
        "autumn2023", "autumn2024", "autumn2025", "autumn2026",
        # Miscellaneous popular
        "letmein123", "iloveu", "whatever", "starwars", "hunter2",
        "trustno1", "computer", "internet", "freedom", "liverpool",
        "chelsea", "arsenal", "manutd", "cricket", "charlie",
        "donald", "nicole", "daniel", "anthony", "ranger",
        "joshua", "andrew", "buster", "thomas", "robert",
        "jessica", "amanda", "michelle", "diamond", "killer",
        "jasmine", "golfer", "tigger", "mustang", "mercedes",
        "ferrari", "porsche", "corvette", "nascar", "chevrolet",
        "honda", "toyota", "nissan", "hyundai", "volkswagen",
        # Common "secure-looking" but known patterns
        "Passw0rd!", "Passw0rd1", "Welcome123!", "Admin@123",
        "Changeme1!", "Letmein1!", "Qwerty1!", "Password1!",
        "Password2!", "Password@1", "Spring2025!", "Summer2025!",
        "Autumn2025!", "Winter2025!", "Spring2026!", "Summer2026!",
        "Autumn2026!", "Winter2026!",
    )
)


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

    if password.lower() in _COMMON_PASSWORDS:
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
        pair = pw_low[i:i + 2]
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
