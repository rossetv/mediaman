"""Client-IP resolution respecting trusted-proxy forwarded headers.

Split from the original monolithic ``rate_limit.py`` (R4). Contains
``get_client_ip``, the XFF / X-Real-IP / CF-Connecting-IP parsing, and
the trusted-proxy allowlist logic.

Cloudflare-specific note
------------------------
``cf-connecting-ip`` is honoured ONLY when the direct peer is in the
*Cloudflare* allowlist (``MEDIAMAN_CLOUDFLARE_PROXIES``), which is
deliberately separate from the general trusted-proxy allowlist
(``MEDIAMAN_TRUSTED_PROXIES``). A non-Cloudflare proxy can trivially
forge ``cf-connecting-ip``; trusting it without verifying the peer is
actually Cloudflare would let any trusted proxy spoof arbitrary client
IPs and bypass per-IP rate limits. Operators who terminate behind
Cloudflare must populate ``MEDIAMAN_CLOUDFLARE_PROXIES`` with the
published Cloudflare IP ranges.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import os
from collections.abc import Mapping
from ipaddress import IPv4Network, IPv6Network
from typing import Protocol


class _AddressLike(Protocol):
    """Minimal interface for the ``client`` attribute of an HTTP request."""

    @property
    def host(self) -> str: ...


class _HasClientAndHeaders(Protocol):
    """Minimal structural interface for an HTTP request object.

    ``get_client_ip`` only ever reads ``request.client.host`` and calls
    ``request.headers.get(...)``; this Protocol captures exactly that
    surface so the function stays decoupled from any specific web
    framework at static-analysis time.  FastAPI's ``Request``, Starlette's
    ``Request``, and any test double with the same shape all satisfy it
    structurally.
    """

    @property
    def client(self) -> _AddressLike | None: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


logger = logging.getLogger(__name__)

# Sentinel returned when no peer address could be determined. Centralised
# so callers comparing against it don't sprinkle a magic string.
_UNKNOWN_PEER = "unknown"


def _parse_proxy_env(env_var: str) -> list[IPv4Network | IPv6Network]:
    """Parse a comma-separated CIDR/IP list from *env_var*.

    Returns an empty list and logs CRITICAL when the literal ``*`` is
    present — a wildcard would let any peer spoof forwarded headers and
    completely bypass per-IP rate limiting. Invalid CIDR entries are
    skipped with a WARNING so misconfiguration is loud.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []

    networks: list[IPv4Network | IPv6Network] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token == "*":
            logger.critical(
                "%s contains the literal wildcard '*'; refusing to trust "
                "any proxy. Set explicit CIDR ranges instead — a wildcard "
                "would allow spoofed forwarded headers from any peer and "
                "bypass per-IP rate limiting.",
                env_var,
            )
            return []
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning(
                "%s contains invalid CIDR/IP entry %r; skipping. Fix the "
                "configuration to silence this warning.",
                env_var,
                token,
            )
            continue
    return networks


@functools.lru_cache(maxsize=1)
def _trusted_proxies_cached() -> tuple[IPv4Network | IPv6Network, ...]:
    """Memoised parse of ``MEDIAMAN_TRUSTED_PROXIES``."""
    return tuple(_parse_proxy_env("MEDIAMAN_TRUSTED_PROXIES"))


@functools.lru_cache(maxsize=1)
def _cloudflare_proxies_cached() -> tuple[IPv4Network | IPv6Network, ...]:
    """Memoised parse of ``MEDIAMAN_CLOUDFLARE_PROXIES``."""
    return tuple(_parse_proxy_env("MEDIAMAN_CLOUDFLARE_PROXIES"))


def trusted_proxies() -> list[IPv4Network | IPv6Network]:
    """Return the list of trusted proxy networks from MEDIAMAN_TRUSTED_PROXIES.

    Result is cached; call :func:`clear_cache` after changing the env var
    (tests do this via ``monkeypatch``).
    """
    return list(_trusted_proxies_cached())


def cloudflare_proxies() -> list[IPv4Network | IPv6Network]:
    """Return the list of Cloudflare proxy networks from MEDIAMAN_CLOUDFLARE_PROXIES.

    Defaults to empty — without an explicit Cloudflare allowlist, the
    ``cf-connecting-ip`` header is *never* honoured, even if the peer is
    in the generic trusted-proxy list. This prevents non-Cloudflare
    proxies from spoofing arbitrary client IPs via a Cloudflare-only
    header.
    """
    return list(_cloudflare_proxies_cached())


def clear_cache() -> None:
    """Drop cached env-var parses. Tests call this after monkeypatch."""
    _trusted_proxies_cached.cache_clear()
    _cloudflare_proxies_cached.cache_clear()


def _ip_in_networks(ip: str, networks: list[IPv4Network | IPv6Network]) -> bool:
    """Return True if *ip* parses and falls inside any of *networks*."""
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def peer_is_trusted(peer: str | None, trusted: list[IPv4Network | IPv6Network]) -> bool:
    """Return True if the direct peer IP is in the trusted-proxy allowlist."""
    if not peer:
        return False
    return _ip_in_networks(peer, trusted)


def get_client_ip(
    request: _HasClientAndHeaders,
    # rationale: body is 58 executable lines (§3.1 ceiling is 60). The function
    # is a single security decision tree: peer trust check → CF header → XFF
    # chain → X-Real-IP → peer fallback. Splitting individual header checks
    # into private helpers would distribute the trust-hierarchy logic across
    # multiple call sites, making it harder to audit that every path applies
    # the correct trust checks in the correct order.
) -> str:
    """Extract the real client IP, respecting forwarded headers only from trusted proxies."""
    peer = request.client.host if request.client else None
    trusted = trusted_proxies()
    if not peer_is_trusted(peer, trusted):
        return peer or _UNKNOWN_PEER

    # cf-connecting-ip is honoured ONLY when the peer is in the
    # *Cloudflare* allowlist — see module docstring for the security
    # rationale. An empty MEDIAMAN_CLOUDFLARE_PROXIES means the header is
    # never trusted, regardless of MEDIAMAN_TRUSTED_PROXIES.
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        cf_networks = cloudflare_proxies()
        if peer_is_trusted(peer, cf_networks):
            cf_ip = cf_ip.strip()
            try:
                ipaddress.ip_address(cf_ip)
                return cf_ip
            except ValueError:
                logger.debug(
                    "rate_limit.ip_resolver: invalid cf-connecting-ip %r from peer %s; ignoring",
                    cf_ip,
                    peer,
                )

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        raw_entries = [part.strip() for part in forwarded.split(",") if part.strip()]
        valid_entries: list[str] = []
        for entry in raw_entries:
            try:
                ipaddress.ip_address(entry)
            except ValueError:
                logger.warning(
                    "x-forwarded-for contains non-IP entry %r from peer %s; skipping.",
                    entry,
                    peer,
                )
                continue
            valid_entries.append(entry)

        for ip in reversed(valid_entries):
            if not _ip_in_networks(ip, trusted):
                return ip

        # Every entry parsed and was inside the trusted allowlist. This
        # is suspicious: it usually means the proxy chain is misconfigured
        # (e.g. operator listed every internal hop including the client's
        # own subnet) or a proxy is double-counting itself. Either way the
        # client identity is unknown and we'd be silently rate-limiting
        # the wrong actor — surface it loudly.
        if valid_entries:
            logger.warning(
                "x-forwarded-for chain %r from peer %s has no untrusted "
                "entries; falling back to peer. This usually indicates a "
                "misconfigured trusted-proxy list.",
                valid_entries,
                peer,
            )
        return peer or _UNKNOWN_PEER

    x_real = request.headers.get("x-real-ip")
    if x_real:
        x_real = x_real.strip()
        try:
            ipaddress.ip_address(x_real)
            return x_real
        except ValueError:
            logger.debug(
                "rate_limit.ip_resolver: invalid x-real-ip %r from peer %s; ignoring",
                x_real,
                peer,
            )

    return peer or _UNKNOWN_PEER
