"""Private deny-list constants and predicates for :mod:`url_safety`.

Lives in ``services/infra/`` alongside its consumer and is not part of
the public surface — every name in here begins with an underscore on
purpose so callers don't get a second front door past the SSRF guard.
Imported by :mod:`mediaman.services.infra.url_safety` only.
"""

from __future__ import annotations

import ipaddress
import os
import socket

import idna

_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hostnames that always resolve to cloud-provider metadata services and
#: have no legitimate use from an application. Matched case-insensitively
#: after lower-casing the parsed host.
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata",  # GCE short-name
    }
)

#: Literal IP addresses that expose cloud-provider metadata. Always
#: blocked regardless of any feature flag.
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS / Azure / DO IMDS
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS over IPv6
    }
)

#: Host suffixes that belong to private/internal zones and should be
#: refused. ``.internal`` leaks intent and covers GCP-style metadata.
_BLOCKED_HOST_SUFFIXES = (".internal",)

#: IPv4 networks that have no legitimate outbound use. CGNAT (100.64/10)
#: is the key addition over the old list — it is routable on a few ISPs
#: but an attacker could still use it to reach a colocated admin panel.
_BLOCKED_V4_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),  # "this" network
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("255.255.255.255/32"),  # limited broadcast
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved / class E
)

#: IPv6 networks that have no legitimate outbound use.
_BLOCKED_V6_NETS = (
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("fc00::/7"),  # ULA
    ipaddress.ip_network("2001::/32"),  # Teredo tunnel
    ipaddress.ip_network("2002::/16"),  # 6to4 tunnel
    ipaddress.ip_network("ff00::/8"),  # multicast
)

#: Additional networks blocked only under strict egress. In the default
#: permissive mode LAN services are explicitly allowed; strict mode
#: turns that off.
_STRICT_BLOCKED_V4_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918
)

_STRICT_BLOCKED_V6_NETS = (
    ipaddress.ip_network("::1/128"),  # loopback
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
    return any(h.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, strict: bool) -> bool:
    """Return True if *ip* should be refused outright.

    Blocks cloud metadata IPs, the IPv6 wildcard, link-local, ULA,
    Teredo/6to4, multicast and broadcast ranges, CGNAT and the "this
    network" range. IPv4-mapped-IPv6 addresses (``::ffff:x.x.x.x``)
    are unwrapped and the embedded v4 rechecked, so no attacker can
    smuggle 127.0.0.1 through ``[::ffff:127.0.0.1]``.

    When *strict* is True the full RFC1918 set and loopback are blocked
    too.

    All checks (metadata IPs, link-local, unspecified) are applied
    *after* the IPv4-mapped unwrap so the same address presented as
    ``169.254.169.254`` and ``::ffff:169.254.169.254`` is rejected by
    the same rule path. An earlier version checked some flags before
    the unwrap and others after, which left the metadata-IP membership
    test relying on incidental coverage by the broader range blocks.
    """
    # Unwrap IPv4-mapped-IPv6 first so every check below sees the
    # canonical embedded form. ``ipaddress`` returns the unwrapped
    # IPv4Address which has its own ``is_unspecified`` / ``is_link_local``
    # flags — the IPv4-mapped wrapper does not propagate those.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    # Re-check the metadata-IP allow-list AFTER the unwrap so a v6-mapped
    # 169.254.169.254 hits the explicit metadata block rather than relying
    # on the link-local range to catch it incidentally.
    if ip in _METADATA_IPS:
        return True
    if ip.is_unspecified:
        return True
    if ip.is_link_local:
        return True

    if isinstance(ip, ipaddress.IPv4Address):
        for net in _BLOCKED_V4_NETS:
            if ip in net:
                return True
        if strict:
            for net in _STRICT_BLOCKED_V4_NETS:
                if ip in net:
                    return True
        return False

    # IPv6 remaining branch.
    for net in _BLOCKED_V6_NETS:
        if ip in net:
            return True
    if strict:
        for net in _STRICT_BLOCKED_V6_NETS:
            if ip in net:
                return True
    return False


def _resolve_all(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve *hostname* to every address ``getaddrinfo`` returns.

    Returns an empty list on resolution failure — the caller should
    treat that as "cannot verify, refuse" rather than "looks fine".
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw = sockaddr[0]
        if not isinstance(raw, str) or raw in seen:
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

    A trailing dot (``"metadata.google.internal."``) is stripped before
    encoding so the suffix check downstream sees the bare label form.
    Without that strip, ``endswith(".internal")`` would miss
    ``"metadata.google.internal."`` (it ends with ``".internal."``),
    even though the resolver treats the two as identical.
    """
    if not hostname:
        return None
    # Strip a trailing dot — both DNS and idna treat the absolute form
    # as identical to the relative one, but the suffix-blocklist check
    # at ``_host_is_metadata`` is a literal ``endswith(".internal")``
    # and would otherwise miss the FQDN form.
    hostname = hostname.rstrip(".")
    if not hostname:
        return None
    # IP literals go through untouched — idna would reject them.
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        pass  # not an IP literal — fall through to IDN encoding
    try:
        return idna.encode(hostname, uts46=True, transitional=False).decode("ascii")
    except idna.IDNAError:
        return None
