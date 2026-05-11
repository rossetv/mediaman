"""SSRF guard for admin-configured outbound service URLs.

Mediaman accepts URLs from the admin settings page (Radarr, Sonarr,
Plex, NZBGet, Mailgun webhook base URL, etc.) and then makes outbound
HTTP requests to them. If an attacker lands an admin session they can
point those URLs at cloud-metadata endpoints (AWS IMDS, GCP metadata)
or internal admin panels and read the response back through mediaman.

Two layers of defence
---------------------

1. **Deny-list (always on).** The narrow set of destinations that have
   no legitimate use in a self-hosted media stack — cloud metadata,
   IPv6 wildcard/loopback, CGNAT, broadcast/multicast, exotic IPv6
   tunnel ranges — is always refused. ``MEDIAMAN_STRICT_EGRESS`` adds
   RFC1918 and loopback to the list.
2. **Allowlist (opt-in).** When a caller passes
   ``allowed_hosts={...}`` to :func:`is_safe_outbound_url` or
   :func:`resolve_safe_outbound_url`, only hostnames whose IDN-
   normalised form appears in that set OR in
   :data:`PINNED_EXTERNAL_HOSTS` are accepted, even if they pass the
   deny-list. Callers compose the configured-integration set via
   :func:`allowed_outbound_hosts`, which reads the current
   ``plex_url`` / ``radarr_url`` / ``sonarr_url`` / ``nzbget_url`` and
   the Mailgun region hostname out of the ``settings`` table.

Hostnames are resolved via ``socket.getaddrinfo`` and *every* returned
address is checked against the deny list, so an attacker cannot smuggle
169.254.169.254 behind a public DNS name. A host that fails to resolve
at all is **rejected** — we cannot prove it is safe, so we refuse it
rather than let the request issue with a last-moment DNS answer that
nobody checked.

The default deployment still allows RFC1918 (192.168.x.x, 10.x.x.x,
172.16-31.x.x) and loopback, because the vast majority of mediaman
users run Radarr/Sonarr/Plex on the LAN. Operators who want stricter
egress should set ``MEDIAMAN_STRICT_EGRESS=1`` in the environment or
pass ``strict_egress=True`` per-call.

Lives under ``services/infra/`` because the implementation depends on
``idna`` (a pure-Python IDNA codec) and performs DNS resolution via
``socket.getaddrinfo``. ``core/`` is stdlib-only and I/O-free, so this
module belongs alongside its sibling ``services/infra/path_safety.py``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import sqlite3
from urllib.parse import urlparse

import idna

logger = logging.getLogger(__name__)

#: External hosts mediaman speaks to that are NOT configured by the
#: operator. These are static for the lifetime of the codebase and are
#: always trusted when the allowlist is enforced. Adding to this list
#: is a security change — review CODE_GUIDELINES §10.6 first.
PINNED_EXTERNAL_HOSTS: frozenset[str] = frozenset(
    {
        # TMDB metadata and posters.
        "api.themoviedb.org",
        "image.tmdb.org",
        # OMDb fallback metadata.
        "www.omdbapi.com",
        # Mailgun — US default and EU region (operator picks one via
        # ``mailgun_region``; we trust both because either may be in use
        # without a fresh restart after toggling regions).
        "api.mailgun.net",
        "api.eu.mailgun.net",
        # OpenAI recommendations.
        "api.openai.com",
    }
)

#: Settings-table keys whose values are URLs that should be treated as
#: trusted outbound destinations when the allowlist is enforced.
_INTEGRATION_URL_SETTING_KEYS = (
    "plex_url",
    "radarr_url",
    "sonarr_url",
    "nzbget_url",
)

#: Schemes allowed for outbound service URLs. Anything else (file, gopher,
#: ldap, dict, ftp, etc.) is refused outright.
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


def _extract_host(raw_url: str) -> str | None:
    """Return the IDN-normalised hostname for *raw_url*, or ``None``.

    Mirrors the parse + IDN step performed by
    :func:`resolve_safe_outbound_url` so callers can build the allowlist
    without re-implementing the URL parsing rules.
    """
    if not raw_url or not isinstance(raw_url, str):
        return None
    try:
        parsed = urlparse(raw_url.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    return _normalise_host(hostname)


def allowed_outbound_hosts(conn: sqlite3.Connection) -> frozenset[str]:
    """Return the current outbound host allowlist.

    The allowlist is the union of:

    * :data:`PINNED_EXTERNAL_HOSTS` — TMDB, OMDb, Mailgun (both
      regions), OpenAI; static for the lifetime of the codebase.
    * Configured integration URLs in the ``settings`` table —
      ``plex_url``, ``radarr_url``, ``sonarr_url``, ``nzbget_url``.
      Each is parsed, IDN-normalised, and added by hostname only
      (port and path are ignored).

    Empty / missing / unparseable settings rows are silently dropped:
    the operator may not have configured Radarr yet, and we don't want
    to refuse Plex calls because Radarr is empty. Encrypted settings
    are read in their stored form — for URL fields mediaman stores
    plaintext so this is fine. If a future schema encrypts URLs, the
    caller would need to pass ``secret_key`` through; flagged for
    review.
    """
    hosts: set[str] = set(PINNED_EXTERNAL_HOSTS)
    for key in _INTEGRATION_URL_SETTING_KEYS:
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            # Schema drift or transient DB error — surface as
            # "allowlist contains only the static pins" so an outbound
            # call refuses rather than silently going through with a
            # half-built allowlist.
            logger.warning("settings read failed for %s — allowlist may be incomplete", key)
            continue
        if row is None:
            continue
        raw = row["value"] if hasattr(row, "keys") else row[0]
        if not raw:
            continue
        host = _extract_host(str(raw))
        if host:
            hosts.add(host)
    return frozenset(hosts)


def _host_in_allowlist(hostname: str, allowed_hosts: frozenset[str] | set[str]) -> bool:
    """Return True if *hostname* is in *allowed_hosts*.

    Comparison is case-insensitive and ignores a trailing dot. The
    pinned external hosts are merged in here so callers that pass an
    integration-only set still allow TMDB/OMDb/Mailgun/OpenAI.
    """
    h = hostname.lower().rstrip(".")
    if h in PINNED_EXTERNAL_HOSTS:
        return True
    return h in {a.lower().rstrip(".") for a in allowed_hosts}


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
        pass
    try:
        return idna.encode(hostname, uts46=True, transitional=False).decode("ascii")
    except idna.IDNAError:
        return None


def resolve_safe_outbound_url(
    url: str,
    *,
    strict_egress: bool | None = None,
    allowed_hosts: frozenset[str] | set[str] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Validate *url* and return a pinned IP for the eventual connection.

    Returns ``(safe, hostname, pinned_ip)``:

    * ``safe`` — ``True`` only if every check in
      :func:`is_safe_outbound_url` passes.
    * ``hostname`` — the IDN-normalised hostname from the URL, or
      ``None`` if the URL was malformed before host extraction. Useful
      to the caller for installing a host-specific DNS pin.
    * ``pinned_ip`` — the **validated** address that the eventual
      connection must use, or ``None`` if no pin is required (URL
      already contains a literal IP, or the URL was rejected). When
      present, callers must connect to this exact address rather than
      re-resolving the hostname — that's the DNS-rebinding defence.

    When *allowed_hosts* is provided, the URL's hostname must appear in
    that set (after IDN normalisation) **or** in
    :data:`PINNED_EXTERNAL_HOSTS`, otherwise the URL is refused even if
    the deny-list checks would pass. Pass ``None`` (the default) to
    skip the allowlist check; pass an empty set to refuse every host
    except the pinned externals.

    The function is the single place in the codebase that performs
    SSRF safety analysis; :func:`is_safe_outbound_url` simply discards
    everything but the bool.
    """
    if not url or not isinstance(url, str):
        return False, None, None

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False, None, None

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, None, None

    # Reject ``user:pass@host`` style authorities outright. The parsed
    # .username / .password attributes are populated when '@' sits in
    # the netloc, even if empty — treat any userinfo as hostile.
    if "@" in (parsed.netloc or ""):
        return False, None, None

    hostname = parsed.hostname
    if not hostname:
        return False, None, None

    strict = _strict_egress_enabled(strict_egress)

    # Block hostnames whose name alone is a red flag, before any DNS.
    if _host_is_metadata(hostname):
        return False, hostname, None

    # IDN-normalise so a Unicode variant cannot bypass the ASCII checks.
    normalised = _normalise_host(hostname)
    if normalised is None:
        return False, hostname, None
    if _host_is_metadata(normalised):
        return False, normalised, None

    # Allowlist check sits between the cheap parse-level rules and the
    # expensive DNS lookups, so a refused host fails fast without
    # touching ``socket.getaddrinfo``. We test against the IDN-
    # normalised form so a Unicode lookalike (cyrillic 'а' in
    # ``radarr.local``) cannot smuggle past an ASCII allowlist entry.
    if allowed_hosts is not None and not _host_in_allowlist(normalised, allowed_hosts):
        return False, normalised, None

    # Literal IP in the URL → check directly, skip DNS.
    try:
        ip = ipaddress.ip_address(normalised)
    except ValueError:
        pass
    else:
        if _ip_is_blocked(ip, strict=strict):
            return False, normalised, None
        # Even for literal IPs, modern urllib3 still calls
        # ``getaddrinfo("192.0.2.1", port)`` to build the connection
        # tuple — and any future test/library that monkeypatches
        # ``socket.getaddrinfo`` could redirect that lookup elsewhere.
        # Pinning the literal address to itself short-circuits the
        # resolver and makes the connect deterministic with the
        # validated answer, regardless of what's installed on the
        # process-wide ``socket.getaddrinfo``.
        return True, normalised, normalised

    # Hostname → resolve and reject if *any* returned address is blocked,
    # OR if the name fails to resolve at all. A non-resolving name used
    # to be allowed through on the theory that the admin might be saving
    # a URL that will resolve later; we can no longer afford that — a
    # second DNS call at request time could return a metadata IP.
    addrs = _resolve_all(normalised)
    if not addrs:
        return False, normalised, None
    for ip in addrs:
        if _ip_is_blocked(ip, strict=strict):
            return False, normalised, None

    # Pin the first safe address we got back. Every address in ``addrs``
    # has already been confirmed safe, so the choice is irrelevant for
    # security; we use the first one for stability across calls. The
    # caller installs this in a thread-local DNS pin so the eventual
    # ``socket.getaddrinfo`` returns the same address we just verified.
    return True, normalised, str(addrs[0])


def is_safe_outbound_url(
    url: str,
    *,
    strict_egress: bool | None = None,
    allowed_hosts: frozenset[str] | set[str] | None = None,
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

    When *allowed_hosts* is provided, the URL's IDN-normalised hostname
    must appear in that set or in :data:`PINNED_EXTERNAL_HOSTS`. The
    deny-list checks still apply on top — an allowlisted host that
    resolves to a metadata IP is still refused.
    """
    safe, _hostname, _pinned_ip = resolve_safe_outbound_url(
        url,
        strict_egress=strict_egress,
        allowed_hosts=allowed_hosts,
    )
    return safe
