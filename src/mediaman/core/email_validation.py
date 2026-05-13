"""Validate email addresses for safe use as mail recipients.

The check is deliberately small but stricter than bare ``parseaddr``:
- rejects CR/LF/NUL (RFC 2822 header injection),
- rejects any whitespace anywhere in the address (incl. Unicode),
- rejects display-name syntax (``"Admin <admin@example.com>"``) — the
  column is a bare address, not a mailbox specification,
- rejects more than one ``@`` (``parseaddr`` is happy with ``a@@b.com``),
- caps the input at the RFC 5321 hard limit of 320 octets,
- requires both a non-empty local part and a non-empty domain part.

RFC 5321/5322 conformance is the mail server's job; this guard exists to
reject obvious mistakes (e.g. a username stored where an address was
expected) and header-injection attempts before they reach the SMTP /
Mailgun layer.
"""

from __future__ import annotations

import email.utils

#: Characters that must never appear in an RFC 2822 header value.
_HEADER_INJECT_CHARS = frozenset("\r\n\0")

#: RFC 5321 §4.5.3.1 caps the full local@domain at 320 octets. Anything
#: longer than that cannot be delivered, so rejecting early avoids
#: surfacing the failure inside the SMTP transaction.
_MAX_ADDRESS_LEN = 320


def validate_email_address(address: str) -> None:
    """Raise ``ValueError`` if *address* is not a deliverable email address.

    Stricter than bare ``parseaddr`` for the reasons listed in the
    module docstring. Some technically RFC-legal but operationally weird
    forms (display-name syntax, double-``@``) are rejected here; they
    should never appear in a single-address admin-profile field anyway.
    """
    if len(address) > _MAX_ADDRESS_LEN:
        raise ValueError(f"Invalid email address: exceeds {_MAX_ADDRESS_LEN} characters")
    if any(c in address for c in _HEADER_INJECT_CHARS):
        raise ValueError(f"Address contains illegal characters: {address!r}")
    if any(c.isspace() for c in address):
        # ``str.isspace`` matches Unicode whitespace too (e.g. NBSP,
        # line separator) — desirable, since those forms would be just
        # as undeliverable as an ASCII space.
        raise ValueError(f"Invalid email address: {address!r}")
    if "<" in address or ">" in address:
        # Reject display-name / mailbox-spec syntax. ``parseaddr`` would
        # quietly extract the bare address from "Admin <a@b.com>"; for a
        # profile field we want exactly one bare address.
        raise ValueError(f"Invalid email address: {address!r}")
    if address.count("@") != 1:
        # ``parseaddr`` accepts ``a@@b.com`` and returns it untouched —
        # the SMTP layer would reject it, but we reject earlier so the
        # error surfaces in the UI instead of in a delivery log.
        raise ValueError(f"Invalid email address: {address!r}")
    _, parsed = email.utils.parseaddr(address)
    if not parsed or "@" not in parsed:
        raise ValueError(f"Invalid email address: {address!r}")
    local, domain = parsed.split("@", 1)
    if not local or not domain:
        raise ValueError(f"Invalid email address: {address!r}")
