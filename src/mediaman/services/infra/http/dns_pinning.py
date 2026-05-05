"""DNS pinning — the TOCTOU defence for SSRF-checked outbound requests.

Threat model
------------
The SSRF guard in :mod:`mediaman.services.infra.url_safety` resolves the
target hostname to an IP *before* the request is dispatched.  Between that
resolution and the moment urllib3 (via ``socket.getaddrinfo``) resolves the
same hostname to open a TCP connection, an attacker-controlled DNS server can
respond with a different IP — typically a private/link-local address that
passed a "is this public?" test on the first lookup but is now pointing at
``169.254.169.254`` or the local network.  This is the classic DNS rebind
attack window.

The fix is to *pin* the validated IP and force every ``getaddrinfo`` call for
that hostname to return the same address for the duration of the request.  We
do this by replacing ``socket.getaddrinfo`` once at import time with a thin
wrapper that consults a per-thread pin table.  Concurrent worker threads never
see each other's pins because the table lives on a :class:`threading.local`.

Idempotency invariant
---------------------
``_install_dns_pin_hook()`` may be called multiple times safely — it is
idempotent and protected by a lock.  ``_ensure_dns_pin_hook_installed()`` is
called at the start of every outbound request; if anything in the process
replaced ``socket.getaddrinfo`` after import (a test fixture, a plugin, a
monitor) it re-captures the replacement as the real delegate and re-installs
the patched resolver.  The pin therefore works regardless of what the rest of
the process does to ``socket.getaddrinfo`` — the only way to defeat it is to
replace ``_patched_getaddrinfo`` itself, which requires arbitrary code
execution inside our process.

Usage
-----
The :func:`pin` context manager is the **only** supported way to install a
pin.  Direct manipulation of ``_DNS_PIN_LOCAL`` bypasses the SSRF defence.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
import threading

logger = logging.getLogger("mediaman")

_HTTP_CLIENT_MODULE = "mediaman.services.infra.http.dns_pinning"

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_DNS_PIN_LOCAL = threading.local()
_ORIG_GETADDRINFO = socket.getaddrinfo
_PIN_INSTALL_LOCK = threading.Lock()
_PIN_INSTALLED = False


# ---------------------------------------------------------------------------
# The monkey-patched resolver
# ---------------------------------------------------------------------------


def _patched_getaddrinfo(host, port, *args, **kwargs):  # pragma: no cover - thin wrapper
    """``socket.getaddrinfo`` wrapper that honours per-thread DNS pins.

    When a pin is set for *host* we synthesise a single ``getaddrinfo``
    record for the pinned IP, preserving the address family (v4 vs v6).
    If no pin is set, behaviour is identical to the upstream resolver.

    If the caller asked for a specific address family and the pin holds an
    address from the other family, an empty list is returned — the same
    signal urllib3 uses to try the next strategy, avoiding a record the
    connection layer cannot use.
    """
    pins: dict[str, str] | None = getattr(_DNS_PIN_LOCAL, "pins", None)
    if pins:
        ip = pins.get(host)
        if ip is not None:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            requested_family = kwargs.get("family")
            if requested_family is None and len(args) > 0:
                requested_family = args[0]
            # ``AF_UNSPEC`` (0) means "either family is fine" — treat as wildcard.
            # Any explicit family that disagrees with the pin must yield [].
            if (
                requested_family
                and requested_family != socket.AF_UNSPEC
                and requested_family != family
            ):
                return []
            socktype = kwargs.get("type") or (args[1] if len(args) > 1 else 0)
            proto = kwargs.get("proto") or (args[2] if len(args) > 2 else 0)
            sockaddr = (ip, port or 0, 0, 0) if family == socket.AF_INET6 else (ip, port or 0)
            return [
                (
                    family,
                    socktype or socket.SOCK_STREAM,
                    proto,
                    "",
                    sockaddr,
                )
            ]
    # Resolve ``_ORIG_GETADDRINFO`` through ``http_client``'s module namespace
    # at call time so ``monkeypatch.setattr(http_client, "_ORIG_GETADDRINFO",
    # fake)`` in tests correctly replaces the delegate resolver.
    _hc = sys.modules.get(_HTTP_CLIENT_MODULE)
    _orig = getattr(_hc, "_ORIG_GETADDRINFO", None) if _hc is not None else None
    if _orig is None:
        _orig = _ORIG_GETADDRINFO
    return _orig(host, port, *args, **kwargs)


# ---------------------------------------------------------------------------
# Hook installation
# ---------------------------------------------------------------------------


def _install_dns_pin_hook() -> None:
    """Install :func:`_patched_getaddrinfo` in place of ``socket.getaddrinfo``.

    Idempotent and thread-safe.  Called once at module import so any request
    that goes through this client is automatically pinning-aware.
    """
    global _PIN_INSTALLED
    with _PIN_INSTALL_LOCK:
        if socket.getaddrinfo is _patched_getaddrinfo:
            _PIN_INSTALLED = True
            return
        socket.getaddrinfo = _patched_getaddrinfo
        _PIN_INSTALLED = True


def ensure_hook_installed() -> None:
    """Verify (and re-install) the patched ``socket.getaddrinfo`` resolver.

    Called at the start of every request.  If anything in the process replaced
    ``socket.getaddrinfo`` after import, this:

    1. Logs a CRITICAL message so an operator notices.
    2. Captures the replacement as the new ``_ORIG_GETADDRINFO`` delegate so
       non-pinned lookups still flow through it.
    3. Re-installs ``_patched_getaddrinfo`` so the pin takes effect again.
    """
    global _ORIG_GETADDRINFO
    # Fast path — cheap pointer compare without the lock.
    if socket.getaddrinfo is _patched_getaddrinfo:
        return

    with _PIN_INSTALL_LOCK:
        replacement = socket.getaddrinfo
        if replacement is _patched_getaddrinfo:
            # Another thread reinstalled while we waited on the lock.
            return
        logger.critical(
            "socket.getaddrinfo was replaced after http_client import — "
            "DNS pin would not have applied. Capturing the replacement as "
            "the new delegate and re-installing the patched resolver. The "
            "replacement was: %r",
            replacement,
        )
        # Capture under the lock so a concurrent re-install can't slip
        # ``_patched_getaddrinfo`` itself into ``_ORIG_GETADDRINFO``.
        _ORIG_GETADDRINFO = replacement
        socket.getaddrinfo = _patched_getaddrinfo
        # Propagate the updated delegate to the back-compat shim module so
        # that ``sys.modules["...http_client"]._ORIG_GETADDRINFO`` stays
        # current.  The shim imports ``_ORIG_GETADDRINFO`` as a static
        # binding; without this update, ``_patched_getaddrinfo``'s
        # sys.modules lookup would return the stale original resolver.
        _hc = sys.modules.get(_HTTP_CLIENT_MODULE)
        if _hc is not None:
            _hc._ORIG_GETADDRINFO = replacement  # type: ignore[attr-defined]


# Install immediately on import.
_install_dns_pin_hook()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def pin(hostname: str, ip: str):
    """Pin DNS for *hostname* to *ip* for the duration of the ``with`` block.

    The pin is stored on a :class:`threading.local`, so other threads are
    unaffected.  On exit the pin is cleared (or restored to the previous pin
    if contexts are nested for the same hostname).

    This is the **only** supported way to install a pin.  Bypassing this
    context manager bypasses the SSRF defence.
    """
    pins: dict[str, str] | None = getattr(_DNS_PIN_LOCAL, "pins", None)
    if pins is None:
        pins = {}
        _DNS_PIN_LOCAL.pins = pins
    previous = pins.get(hostname)
    pins[hostname] = ip
    try:
        yield
    finally:
        if previous is None:
            pins.pop(hostname, None)
        else:
            pins[hostname] = previous


# Back-compat alias used by the original http_client.py public API.
pin_dns_for_request = pin
