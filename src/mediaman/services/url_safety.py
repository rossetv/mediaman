"""SSRF guard for admin-configured outbound service URLs.

Mediaman accepts URLs from the admin settings page (Radarr, Sonarr,
Plex, NZBGet, Mailgun webhook base URL, etc.) and then makes outbound
HTTP requests to them. If an attacker lands an admin session they can
point those URLs at cloud-metadata endpoints (AWS IMDS, GCP metadata)
or internal admin panels and read the response back through mediaman.

This module blocks the narrow set of destinations that have no
legitimate use in a self-hosted media stack — cloud metadata, the
IPv6 wildcard/loopback addresses, CGNAT, broadcast/multicast, and
the exotic IPv6 tunnel ranges — and optionally refuses loopback and
RFC1918 addresses too when ``MEDIAMAN_STRICT_EGRESS`` is truthy.

Hostnames are resolved via ``socket.getaddrinfo`` and *every* returned
address is checked against the block list, so an attacker cannot smuggle
169.254.169.254 behind a public DNS name. A host that fails to resolve
at all is **rejected** — we cannot prove it is safe, so we refuse it
rather than let the request issue with a last-moment DNS answer that
nobody checked.

The default deployment still allows RFC1918 (192.168.x.x, 10.x.x.x,
172.16-31.x.x) and loopback, because the vast majority of mediaman
users run Radarr/Sonarr/Plex on the LAN. Operators who want stricter
egress should set ``MEDIAMAN_STRICT_EGRESS=1`` in the environment or
pass ``strict_egress=True`` per-call.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

import idna

logger = logging.getLogger("mediaman")

#: Schemes allowed for outbound service URLs. Anything else (file, gopher,
#: ldap, dict, ftp, etc.) is refused outright.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hostnames that always resolve to cloud-provider metadata services and
#: have no legitimate use from an application. Matched case-insensitively
#: after lower-casing the parsed host.
_METADATA_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata",  # GCE short-name
})

#: Literal IP addresses that expose cloud-provider metadata. Always
#: blocked regardless of any feature flag.
_METADATA_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS / Azure / DO IMDS
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
    ipaddress.ip_address("fd00:ec2::254"),     # AWS IMDS over IPv6
})

#: Host suffixes that belong to private/internal zones and should be
#: refused. ``.internal`` leaks intent and covers GCP-style metadata.
_BLOCKED_HOST_SUFFIXES = (".internal",)

#: IPv4 networks that have no legitimate outbound use. CGNAT (100.64/10)
#: is the key addition over the old list — it is routable on a few ISPs
#: but an attacker could still use it to reach a colocated admin panel.
_BLOCKED_V4_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),          # "this" network
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("255.255.255.255/32"), # limited broadcast
    ipaddress.ip_network("224.0.0.0/4"),        # multicast
    ipaddress.ip_network("240.0.0.0/4"),        # reserved / class E
)

#: IPv6 networks that have no legitimate outbound use.
_BLOCKED_V6_NETS = (
    ipaddress.ip_network("fe80::/10"),          # link-local
    ipaddress.ip_network("fc00::/7"),           # ULA
    ipaddress.ip_network("2001::/32"),          # Teredo tunnel
    ipaddress.ip_network("2002::/16"),          # 6to4 tunnel
    ipaddress.ip_network("ff00::/8"),           # multicast
)

#: Additional networks blocked only under strict egress. In the default
#: permissive mode LAN services are explicitly allowed; strict mode
#: turns that off.
_STRICT_BLOCKED_V4_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),        # loopback
    ipaddress.ip_network("10.0.0.0/8"),         # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),      # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),     # RFC1918
)

_STRICT_BLOCKED_V6_NETS = (
    ipaddress.ip_network("::1/128"),            # loopback
)


def _strict_egress_enabled(override: bool | None) -> bool:
    """Resolve the effective strict-egress setting.

    Explicit ``override`` wins; otherwise fall back to the
    ``MEDIAMAN_STRICT_EGRESS`` environment variable.
    """
    if override is not None:
        return bool(override)
    raw = os.environ.get("MEDIAMAN_STRICT_EGRESS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _host_is_metadata(hostname: str) -> bool:
    """Return True if *hostname* is a known metadata endpoint name."""
    h = hostname.lower().rstrip(".")
    if h in _METADATA_HOSTNAMES:
        return True
    for suffix in _BLOCKED_HOST_SUFFIXES:
        if h.endswith(suffix):
            return True
    return False


def _ip_is_blocked(ip: ipaddress._BaseAddress, *, strict: bool) -> bool:
    """Return True if *ip* should be refused outright.

    Blocks cloud metadata IPs, the IPv6 wildcard, link-local, ULA,
    Teredo/6to4, multicast and broadcast ranges, CGNAT and the "this
    network" range. IPv4-mapped-IPv6 addresses (``::ffff:x.x.x.x``)
    are unwrapped and the embedded v4 rechecked, so no attacker can
    smuggle 127.0.0.1 through ``[::ffff:127.0.0.1]``.

    When *strict* is True the full RFC1918 set and loopback are blocked
    too.
    """
    if ip in _METADATA_IPS:
        return True
    if ip.is_unspecified:
        return True

    # Unwrap IPv4-mapped-IPv6 so ``::ffff:169.254.169.254`` is caught.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if isinstance(ip, ipaddress.IPv4Address):
        if ip.is_link_local:
            return True
        for net in _BLOCKED_V4_NETS:
            if ip in net:
                return True
        if strict:
            for net in _STRICT_BLOCKED_V4_NETS:
                if ip in net:
                    return True
        return False

    # IPv6 remaining branch.
    if ip.is_link_local:
        return True
    for net in _BLOCKED_V6_NETS:
        if ip in net:
            return True
    if strict:
        for net in _STRICT_BLOCKED_V6_NETS:
            if ip in net:
                return True
    return False


def _resolve_all(hostname: str) -> list[ipaddress._BaseAddress]:
    """Resolve *hostname* to every address ``getaddrinfo`` returns.

    Returns an empty list on resolution failure — the caller should
    treat that as "cannot verify, refuse" rather than "looks fine".
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    addrs: list[ipaddress._BaseAddress] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw = sockaddr[0]
        if raw in seen:
            continue
        seen.add(raw)
        try:
            addrs.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return addrs


def _normalise_host(hostname: str) -> str | None:
    """Return the ASCII / punycode form of *hostname*, or None on failure.

    Uses IDNA UTS-46 so that a Unicode homoglyph cannot slip past an
    ASCII-only blocklist match. An empty string is returned for IP
    literals (they're caught by the caller before this is used).
    """
    if not hostname:
        return None
    # IP literals go through untouched — idna would reject them.
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        pass
    try:
        return idna.encode(hostname, uts46=True, transitional=False).decode("ascii")
    except idna.IDNAError:
        return None


def is_safe_outbound_url(
    url: str,
    *,
    strict_egress: bool | None = None,
) -> bool:
    """Return True if *url* is safe for mediaman to request.

    Blocks:

    * Schemes other than http/https (no ``file://``, ``gopher://`` etc.).
    * URLs with userinfo (``http://user:pass@host``) — credentials in
      the authority are a well-known bypass for naive validators.
    * Cloud-provider metadata IPs and hostnames.
    * Link-local, CGNAT, broadcast, multicast, reserved, ULA, Teredo,
      6to4 ranges, and the IPv6/IPv4 unspecified address.
    * Hostnames that fail DNS resolution entirely — we cannot prove a
      non-resolving name is safe, so we refuse it.
    * Hostnames that resolve to any of the above.

    By default RFC1918 (LAN) addresses and loopback are **allowed** —
    those are the common case for self-hosted Radarr/Sonarr/Plex. Set
    ``MEDIAMAN_STRICT_EGRESS=1`` in the environment (or pass
    ``strict_egress=True``) to additionally refuse them.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False

    # Reject ``user:pass@host`` style authorities outright. The parsed
    # .username / .password attributes are populated when '@' sits in
    # the netloc, even if empty — treat any userinfo as hostile.
    if "@" in (parsed.netloc or ""):
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    strict = _strict_egress_enabled(strict_egress)

    # Block hostnames whose name alone is a red flag, before any DNS.
    if _host_is_metadata(hostname):
        return False

    # IDN-normalise so a Unicode variant cannot bypass the ASCII checks.
    normalised = _normalise_host(hostname)
    if normalised is None:
        return False
    if _host_is_metadata(normalised):
        return False

    # Literal IP in the URL → check directly, skip DNS.
    try:
        ip = ipaddress.ip_address(normalised)
        return not _ip_is_blocked(ip, strict=strict)
    except ValueError:
        pass

    # Hostname → resolve and reject if *any* returned address is blocked,
    # OR if the name fails to resolve at all. A non-resolving name used
    # to be allowed through on the theory that the admin might be saving
    # a URL that will resolve later; we can no longer afford that — a
    # second DNS call at request time could return a metadata IP.
    addrs = _resolve_all(normalised)
    if not addrs:
        return False
    for ip in addrs:
        if _ip_is_blocked(ip, strict=strict):
            return False

    return True
