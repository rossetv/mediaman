"""Client-IP resolution respecting trusted-proxy forwarded headers.

Split from the original monolithic ``rate_limit.py`` (R4). Contains
``get_client_ip``, the XFF / X-Real-IP / CF-Connecting-IP parsing, and
the trusted-proxy allowlist logic.
"""

from __future__ import annotations

import ipaddress
import os

from fastapi import Request


def trusted_proxies() -> list[ipaddress._BaseNetwork]:
    """Return the list of trusted proxy networks from MEDIAMAN_TRUSTED_PROXIES."""
    raw = os.environ.get("MEDIAMAN_TRUSTED_PROXIES", "").strip()
    if not raw:
        return []
    networks: list[ipaddress._BaseNetwork] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return networks


def _ip_in_networks(ip: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    """Return True if *ip* parses and falls inside any of *networks*."""
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def peer_is_trusted(peer: str | None, trusted: list[ipaddress._BaseNetwork]) -> bool:
    """Return True if the direct peer IP is in the trusted-proxy allowlist."""
    if not peer:
        return False
    return _ip_in_networks(peer, trusted)


def get_client_ip(request: Request) -> str:
    """Extract the real client IP, respecting forwarded headers only from trusted proxies."""
    peer = request.client.host if request.client else None
    trusted = trusted_proxies()
    if not peer_is_trusted(peer, trusted):
        return peer or "unknown"

    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        cf_ip = cf_ip.strip()
        try:
            ipaddress.ip_address(cf_ip)
            return cf_ip
        except ValueError:
            pass

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        entries = [part.strip() for part in forwarded.split(",") if part.strip()]
        for ip in reversed(entries):
            if not _ip_in_networks(ip, trusted):
                return ip
        return peer or "unknown"

    x_real = request.headers.get("x-real-ip")
    if x_real:
        x_real = x_real.strip()
        try:
            ipaddress.ip_address(x_real)
            return x_real
        except ValueError:
            pass

    return peer or "unknown"
