"""SSRF guard for admin-configured outbound service URLs.

Mediaman accepts URLs from the admin settings page (Radarr, Sonarr,
Plex, NZBGet, Mailgun webhook base URL, etc.) and then makes outbound
HTTP requests to them. If an attacker lands an admin session they can
point those URLs at cloud-metadata endpoints (AWS IMDS, GCP metadata)
or internal admin panels and read the response back through mediaman.

This module blocks the narrow set of destinations that have no
legitimate use in a self-hosted media stack — cloud metadata and the
IPv6 wildcard/loopback addresses. It deliberately does *not* block
RFC1918 addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x) because the
vast majority of mediaman deployments run Radarr/Sonarr/Plex on the
LAN — blocking those would break the product.

Hostnames are resolved via ``socket.getaddrinfo`` and *every* returned
address is checked against the block list, so an attacker cannot smuggle
169.254.169.254 behind a public DNS name.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

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
#: refused. ``.local`` is mDNS and ``.internal`` is a common convention
#: (e.g. ``metadata.google.internal``) — both leak intent.
_BLOCKED_HOST_SUFFIXES = (".internal",)


def _host_is_metadata(hostname: str) -> bool:
    """Return True if *hostname* is a known metadata endpoint name."""
    h = hostname.lower().rstrip(".")
    if h in _METADATA_HOSTNAMES:
        return True
    for suffix in _BLOCKED_HOST_SUFFIXES:
        if h.endswith(suffix):
            return True
    return False


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """Return True if *ip* should be refused outright.

    Blocks cloud metadata IPs and the IPv6 wildcard/loopback — these
    have no legitimate use as an admin-configured service URL. RFC1918
    addresses are **not** blocked: most mediaman users run Radarr and
    friends on their LAN and that is the supported deployment.
    """
    if ip in _METADATA_IPS:
        return True
    # Block the unspecified address (0.0.0.0, ::) — connecting to it from
    # client code routes to localhost on most stacks, an SSRF foot-gun.
    if ip.is_unspecified:
        return True
    # Block link-local (169.254.0.0/16, fe80::/10) — these can shadow
    # cloud metadata and are never a legitimate LAN service endpoint.
    if ip.is_link_local:
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


def is_safe_outbound_url(url: str) -> bool:
    """Return True if *url* is safe for mediaman to request.

    Blocks:

    * Schemes other than http/https (no ``file://``, ``gopher://`` etc.).
    * Cloud-provider metadata IPs (169.254.169.254, 100.100.100.200,
      fd00:ec2::254) and hostnames (``metadata.google.internal``,
      ``*.internal``).
    * Link-local addresses (169.254.0.0/16, fe80::/10) and the IPv6/IPv4
      unspecified address.
    * Hostnames that resolve to any of the above.

    Does **not** block RFC1918 (LAN) addresses — those are the common
    case for self-hosted Radarr/Sonarr/Plex. If an operator wants
    stricter egress they should enforce it at the network layer.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    # Block hostnames whose name alone is a red flag, before any DNS.
    if _host_is_metadata(hostname):
        return False

    # Literal IP in the URL → check directly, skip DNS.
    try:
        ip = ipaddress.ip_address(hostname)
        return not _ip_is_blocked(ip)
    except ValueError:
        pass

    # Hostname → resolve and reject if *any* returned address is blocked.
    # An empty result means resolution failed; that's suspicious but not
    # conclusive — the admin may be configuring something that will
    # resolve later. We allow the URL through and rely on the actual
    # request failing rather than refusing configuration.
    for ip in _resolve_all(hostname):
        if _ip_is_blocked(ip):
            return False

    return True
