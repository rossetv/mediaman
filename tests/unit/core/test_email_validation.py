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
