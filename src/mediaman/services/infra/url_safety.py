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
   the configured integration URLs out of the ``settings`` table.
   Mailgun's regional hostnames are pinned statically — they are not
   loaded from the settings table.

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

The deny-list constants and stateless predicates live in
:mod:`mediaman.services.infra._url_safety_blocks` — this file owns the
public API and the allowlist composition.
"""

from __future__ import annotations

import ipaddress
import logging
import sqlite3
from urllib.parse import urlparse

from mediaman.services.infra._url_safety_blocks import (
    _ALLOWED_SCHEMES,
    _host_is_metadata,
    _ip_is_blocked,
    _normalise_host,
    _resolve_all,
    _strict_egress_enabled,
)

logger = logging.getLogger(__name__)


class SSRFRefused(Exception):
    """Raised when a URL fails the SSRF guard and the caller cannot proceed.

    Distinct from the bool return of :func:`is_safe_outbound_url` — use this
    when the refusal must propagate up the call stack as a typed exception
    rather than a falsy return value.  Callers catch ``SSRFRefused`` to log
    and skip rather than letting a generic ``ValueError`` bubble through
    framework error handlers.
    """


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

    Fail-closed on DB error: if ``sqlite3.Error`` is raised while
    reading any integration row, the function returns the
    pinned-only set rather than a partially-populated allowlist.
    Routing the operator's traffic through an empty integration set
    is preferable to silently widening the allowlist with whichever
    rows happened to read successfully before the error.
    """
    hosts: set[str] = set(PINNED_EXTERNAL_HOSTS)
    for key in _INTEGRATION_URL_SETTING_KEYS:
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            # Schema drift or transient DB error — abandon the in-progress
            # allowlist and fail closed to the pinned-only set so an
            # outbound call doesn't go through a half-built allowlist.
            logger.warning(
                "settings read failed for %s — falling back to pinned-only allowlist", key
            )
            return frozenset(PINNED_EXTERNAL_HOSTS)
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


def _parse_and_normalise(url: str) -> tuple[bool, str | None, str | None]:
    """Parse and IDN-normalise *url*, applying the DNS-free reject rules.

    Returns ``(ok, host, None)`` mirroring
    :func:`resolve_safe_outbound_url`'s ``(safe, hostname, pinned_ip)``
    shape so the caller can forward a reject verbatim:

    * On rejection, ``ok`` is ``False`` and the second element is the
      hostname to attribute — ``None`` when the URL was malformed before
      host extraction, the **raw** hostname for a pre-IDN metadata-name
      hit or an IDN-normalisation failure, the **normalised** hostname for
      a post-IDN metadata-name hit. ``pinned_ip`` is always ``None``.
    * On success, ``ok`` is ``True`` and the second element is the
      IDN-normalised hostname the caller continues with.

    Covers, in order: empty/type check, parse, scheme check, userinfo
    rejection, hostname extraction, the pre-IDN metadata-name check, IDN
    normalisation, and the post-IDN metadata-name check. These are all the
    checks that can be made without touching the resolver — keeping them
    ahead of any ``getaddrinfo`` call is the documented fail-fast property.
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

    # Block hostnames whose name alone is a red flag, before any DNS.
    if _host_is_metadata(hostname):
        return False, hostname, None

    # IDN-normalise so a Unicode variant cannot bypass the ASCII checks.
    normalised = _normalise_host(hostname)
    if normalised is None:
        return False, hostname, None
    if _host_is_metadata(normalised):
        return False, normalised, None

    return True, normalised, None


def _check_literal_or_resolved(
    normalised: str,
    *,
    strict: bool,
) -> tuple[bool, str | None, str | None]:
    """Resolve *normalised* to a validated pin, or reject it.

    Returns the final ``(safe, hostname, pinned_ip)`` for the IP-level
    portion of :func:`resolve_safe_outbound_url`. *normalised* has already
    passed every DNS-free check; this is the part that may touch the
    resolver.

    Two branches, in the original order:

    * **Literal IP** — when *normalised* is itself an IP literal, it is
      checked directly against the deny-list (no DNS). A safe literal is
      pinned to itself: modern urllib3 still calls
      ``getaddrinfo("192.0.2.1", port)`` to build the connection tuple, so
      pinning the literal short-circuits a process-wide monkeypatched
      resolver that could otherwise redirect the connect.
    * **Hostname** — resolved via ``socket.getaddrinfo``; rejected if it
      fails to resolve at all, or if *any* returned address is blocked.
      The first safe address is pinned (every returned address has already
      been confirmed safe — the first is chosen for stability across
      calls).
    """
    # Literal IP in the URL → check directly, skip DNS.
    try:
        ip = ipaddress.ip_address(normalised)
    except ValueError:
        pass  # not an IP literal — fall through to DNS resolution below
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
    everything but the bool. The ordering — DNS-free parse-level rejects
    (:func:`_parse_and_normalise`), then the allowlist check, then the
    IP-level checks that may touch the resolver
    (:func:`_check_literal_or_resolved`) — is itself a security property:
    a refused host fails fast without ever reaching ``getaddrinfo``.
    """
    ok, normalised, _ = _parse_and_normalise(url)
    if not ok:
        # ``_parse_and_normalise`` already shaped the reject tuple
        # (malformed → ``None`` host; metadata-name hit → the attributed
        # hostname), and ``pinned_ip`` is always ``None`` on its rejects.
        return ok, normalised, None
    # ``ok`` is True ⇒ ``normalised`` is the IDN-normalised hostname.
    assert normalised is not None

    strict = _strict_egress_enabled(strict_egress)

    # Allowlist check sits between the cheap parse-level rules and the
    # expensive DNS lookups, so a refused host fails fast without
    # touching ``socket.getaddrinfo``. We test against the IDN-
    # normalised form so a Unicode lookalike (cyrillic 'а' in
    # ``radarr.local``) cannot smuggle past an ASCII allowlist entry.
    if allowed_hosts is not None and not _host_in_allowlist(normalised, allowed_hosts):
        return False, normalised, None

    return _check_literal_or_resolved(normalised, strict=strict)


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
