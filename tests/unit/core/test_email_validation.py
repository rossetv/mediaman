"""Shared email-address validation helper."""

from __future__ import annotations

import pytest

from mediaman.core.email_validation import validate_email_address


@pytest.mark.parametrize(
    "address",
    [
        "admin@example.com",
        "a.b+tag@sub.example.co.uk",
        "x@y.z",
    ],
)
def test_accepts_valid_addresses(address: str) -> None:
    validate_email_address(address)  # no raise


@pytest.mark.parametrize(
    "address",
    [
        "",
        "rossetv",
        "no-at-sign",
        "@no-local",
        "no-domain@",
        "spaces in@example.com",
    ],
)
def test_rejects_malformed_addresses(address: str) -> None:
    with pytest.raises(ValueError, match="Invalid email address"):
        validate_email_address(address)


@pytest.mark.parametrize("c", ["\r", "\n", "\0"])
def test_rejects_header_injection_chars(c: str) -> None:
    with pytest.raises(ValueError, match="illegal characters"):
        validate_email_address(f"victim{c}@example.com")


@pytest.mark.parametrize(
    "address",
    [
        " admin@example.com",  # non-breaking space
        "admin@ example.com",  # line separator
        "ad　min@example.com",  # ideographic space
    ],
)
def test_rejects_unicode_whitespace(address: str) -> None:
    with pytest.raises(ValueError, match="Invalid email address"):
        validate_email_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "Admin <admin@example.com>",
        "<admin@example.com>",
        "admin@example.com>",
    ],
)
def test_rejects_display_name_syntax(address: str) -> None:
    """The column is a bare address — display-name syntax is not stored."""
    with pytest.raises(ValueError, match="Invalid email address"):
        validate_email_address(address)


def test_rejects_double_at_sign() -> None:
    """``parseaddr`` accepts ``a@@b.com``; this layer must not."""
    with pytest.raises(ValueError, match="Invalid email address"):
        validate_email_address("a@@b.com")


def test_rejects_overlong_address() -> None:
    """Anything beyond the RFC 5321 320-octet cap is undeliverable."""
    overlong = ("a" * 310) + "@example.com"  # 322 chars
    with pytest.raises(ValueError, match="exceeds 320"):
        validate_email_address(overlong)


def test_accepts_address_at_exact_limit() -> None:
    """A 320-octet address must pass — the cap is inclusive of the limit."""
    local = "a" * (320 - len("@example.com"))
    validate_email_address(f"{local}@example.com")  # no raise
