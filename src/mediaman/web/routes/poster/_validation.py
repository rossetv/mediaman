"""Pure validation helpers for poster requests.

These helpers form the first line of defence for the poster proxy:

* :func:`is_valid_rating_key` rejects non-numeric and oversized keys
  before they can reach any URL construction or DB lookup.
* :func:`safe_mime` prevents a hostile CDN from injecting
  ``Content-Type: text/html`` through the proxy — a stored-XSS vector.
* :func:`is_allowed_poster_host` performs exact hostname matching plus
  a DNS-resolved public-IP check via
  :func:`mediaman.services.infra.url_safety.is_safe_outbound_url`.
* :func:`sanitise_plex_url` re-validates the DB-stored ``plex_url`` on
  every request because a settings-write compromise could otherwise
  swap it for a hostile target.

All helpers are stateless and side-effect-free except for the DNS
resolution performed inside :func:`is_safe_outbound_url`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from mediaman.services.infra import is_safe_outbound_url
from mediaman.web.routes.poster.cache import ALLOWED_IMAGE_MIMES

# SSRF allow-list for Radarr/Sonarr remote poster fetches.
#
# Exact hostname → permitted ports. Subdomain wildcards are intentionally
# absent: a DNS-rebind attack on e.g. ``evil.image.tmdb.org`` would pass a
# suffix check but fails an exact-match check. Only HTTPS (443) is
# permitted; port 80 or any non-standard port is refused.
_POSTER_ALLOWED_HOSTS: dict[str, tuple[int, ...]] = {
    "image.tmdb.org": (443,),
    "m.media-amazon.com": (443,),
    "images.amazon.com": (443,),
}


def is_valid_rating_key(rating_key: str) -> bool:
    """Return ``True`` only if *rating_key* is a valid Plex rating key.

    A valid rating key is a non-empty string of ASCII digits whose total
    length does not exceed 12 characters.  This rejects path-traversal
    sequences (``../``, ``%2F``), alphabetic strings, and arbitrarily
    long keys before they touch any URL template or filesystem path.
    """
    return bool(rating_key) and rating_key.isdigit() and len(rating_key) <= 12


def safe_mime(remote_type: str | None) -> str:
    """Coerce a remote ``Content-Type`` value into a safe served mime type.

    If the upstream response claims a type from :data:`~.cache.ALLOWED_IMAGE_MIMES`,
    it is passed through unchanged.  Everything else — including missing,
    malformed, or hostile values such as ``text/html`` — is normalised to
    ``image/jpeg``.  This is the primary defence against a malicious CDN
    using the poster proxy as a stored-XSS vector.
    """
    if not remote_type:
        return "image/jpeg"
    base = remote_type.split(";", 1)[0].strip().lower()
    if base in ALLOWED_IMAGE_MIMES:
        return base
    return "image/jpeg"


def is_allowed_poster_host(url: str) -> bool:
    """Return ``True`` only for HTTPS URLs pointing at a trusted image CDN.

    Performs exact hostname matching against ``_POSTER_ALLOWED_HOSTS`` —
    no subdomain wildcards — so a DNS-rebind via ``evil.image.tmdb.org``
    cannot bypass the check.  Additionally enforces that the port is in
    the permitted set (443 only) and delegates a full DNS-resolution +
    public-IP check to :func:`is_safe_outbound_url` with strict egress
    enabled, catching rebind attacks that return a private IP at
    request time.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # Exact hostname match — no wildcards.
    if host not in _POSTER_ALLOWED_HOSTS:
        return False
    # Port must be in the permitted set. ``parsed.port`` is None when the
    # URL omits the port, which for HTTPS means 443 implicitly.
    port = parsed.port if parsed.port is not None else 443
    if port not in _POSTER_ALLOWED_HOSTS[host]:
        return False
    # Resolve DNS and confirm every returned IP is public. This catches
    # rebind attacks where the initial check passes but the resolver
    # subsequently returns a private address.
    return bool(is_safe_outbound_url(url, strict_egress=True))


def sanitise_plex_url(raw: str | None) -> str | None:
    """Return ``scheme://host[:port]`` if *raw* passes SSRF + scheme checks.

    This runs on every poster request. The DB-stored ``plex_url`` could
    have been rotated by an attacker who lands settings-write since the
    app last started; a one-shot startup validation is not enough.
    Userinfo (``user:pass@``), non-http(s) schemes, and anything the
    SSRF guard refuses all result in ``None``.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if "@" in (parsed.netloc or ""):
        return None
    if not parsed.hostname:
        return None
    # Run the central SSRF check before we use the URL. This re-resolves
    # DNS, so a rebind answer would be caught here.
    if not is_safe_outbound_url(raw):
        return None
    authority = parsed.hostname
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme.lower()}://{authority}"
