"""Validate email addresses for safe use as mail recipients.

The check is intentionally minimal — ``email.utils.parseaddr`` followed by
a CR/LF/NUL guard. RFC 5321/5322 conformance is the mail server's job;
this guard exists to reject obvious mistakes (e.g. a username stored
where an address was expected) and header-injection attempts before they
reach the SMTP / Mailgun layer.
"""

from __future__ import annotations

import email.utils

#: Characters that must never appear in an RFC 2822 header value.
_HEADER_INJECT_CHARS = frozenset("\r\n\0")


def validate_email_address(address: str) -> None:
    """Raise ``ValueError`` if *address* is not a deliverable email address.

    Checks:
    - No CR, LF or NUL anywhere in the raw input.
    - No ASCII whitespace anywhere in the raw input.
    - ``email.utils.parseaddr`` yields a non-empty address containing ``@``
      with a non-empty local part (the portion before ``@``).
    """
    if any(c in address for c in _HEADER_INJECT_CHARS):
        raise ValueError(f"Address contains illegal characters: {address!r}")
    if any(c.isspace() for c in address):
        raise ValueError(f"Invalid email address: {address!r}")
    _, parsed = email.utils.parseaddr(address)
    if not parsed or "@" not in parsed:
        raise ValueError(f"Invalid email address: {address!r}")
    local, _ = parsed.split("@", 1)
    if not local:
        raise ValueError(f"Invalid email address: {address!r}")
