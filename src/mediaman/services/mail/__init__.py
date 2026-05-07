"""Mailgun email client — newsletter rendering, recipient management, and dispatch.

Sub-packages: ``mailgun`` (low-level Mailgun API wrapper), ``newsletter``
(HTML rendering, per-recipient token minting, and bulk dispatch). The
unsubscribe-token flow is threaded through from ``mediaman.crypto`` so PII
never appears in server logs or query strings.

Allowed dependencies: ``mediaman.services.infra.http``, ``mediaman.crypto``,
``mediaman.db``; may use Jinja2 templates located outside this package.

Forbidden patterns: do not import from ``mediaman.web`` — mail dispatch runs as
a background step inside the scanner pipeline.
"""
