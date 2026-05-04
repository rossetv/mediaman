"""Pure helper functions for poster URL validation and mime normalisation.

These helpers are stateless and have no side-effects.  They are kept here
rather than inlined into the route handler so they can be tested in
isolation without constructing a full FastAPI application.

Threat model
------------
The primary concern for poster fetching is SSRF (Server-Side Request
Forgery).  An attacker who can influence the Plex URL stored in the
database — or who can inject a Radarr/Sonarr poster URL — could redirect
the proxy to an internal metadata endpoint, loopback address, or cloud
credential service.  The helpers in this module form the first line of
defence:

* :func:`validate_rating_key` rejects non-numeric and oversized keys
  before they can reach any URL construction or DB lookup.
* :func:`safe_mime` prevents a hostile CDN from injecting
  ``Content-Type: text/html`` through the proxy — a stored-XSS vector.
"""

from __future__ import annotations

from mediaman.web.routes.poster.cache import ALLOWED_IMAGE_MIMES


def validate_rating_key(rating_key: str) -> bool:
    """Return ``True`` only if *rating_key* is a valid Plex rating key.

    A valid rating key is a non-empty string of ASCII digits whose total
    length does not exceed 12 characters.  This rejects path-traversal
    sequences (``../``, ``%2F``), alphabetic strings, and arbitrarily
    long keys before they touch any URL template or filesystem path.

    Args:
        rating_key: The raw rating key string from the URL path parameter.

    Returns:
        ``True`` when the key is safe to use in URL templates and cache
        path computation.
    """
    return bool(rating_key) and rating_key.isdigit() and len(rating_key) <= 12


def safe_mime(remote_type: str | None) -> str:
    """Coerce a remote ``Content-Type`` value into a safe served mime type.

    If the upstream response claims a type from :data:`~.cache.ALLOWED_IMAGE_MIMES`,
    it is passed through unchanged.  Everything else — including missing,
    malformed, or hostile values such as ``text/html`` — is normalised to
    ``image/jpeg``.

    This is the primary defence against a malicious CDN using the poster
    proxy as a stored-XSS vector: even if the CDN serves HTML with an
    image-like status code, the response Content-Type seen by the browser
    will always be a safe image mime.

    Args:
        remote_type: The raw ``Content-Type`` header value from the upstream
                     response, or ``None`` if the header was absent.

    Returns:
        A safe mime type string, always one of :data:`~.cache.ALLOWED_IMAGE_MIMES`
        or ``"image/jpeg"``.
    """
    if not remote_type:
        return "image/jpeg"
    base = remote_type.split(";", 1)[0].strip().lower()
    if base in ALLOWED_IMAGE_MIMES:
        return base
    return "image/jpeg"
