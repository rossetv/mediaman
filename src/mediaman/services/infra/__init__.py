"""Shared infrastructure — SSRF-safe HTTP client, rate-limit infrastructure, path safety, and storage.

Sub-packages: ``http`` (DNS-pinning, retry, streaming, SafeHTTPClient),
``storage`` (_safe_rmtree TOCTOU-hardened deletion), ``path_safety``
(allowlist parsing and containment checks), ``settings_reader``
(settings-row fetch + decryption helpers), ``url_safety`` (outbound URL
validation guard).

Allowed dependencies: Python standard library, ``mediaman.crypto``; must NOT
import from ``mediaman.web``, ``mediaman.scanner``, or ``mediaman.services.arr``.

Forbidden patterns: do not add business logic here — this package supplies
primitives that every other service package depends on.
"""
