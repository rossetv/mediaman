"""Settings URL-field validation helpers.

Owns:
- The set of settings keys that are URL-shaped (``_URL_FIELDS``).
- ``_scrub_url_for_log`` — strips credentials and query strings from a
  URL before it is written to the log.
- ``_validate_url_fields`` — validates all URL fields in a settings
  payload, returning a JSONResponse error or ``None``.

This module is intentionally narrow: it contains only the URL-handling
logic that would otherwise inflate the main ``__init__`` module.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse as _urlparse

from fastapi.responses import JSONResponse

from mediaman.services.infra.url_safety import is_safe_outbound_url
from mediaman.web.responses import respond_err

logger = logging.getLogger("mediaman")

_URL_FIELDS: frozenset[str] = frozenset(
    {
        "base_url",
        "plex_url",
        "plex_public_url",
        "sonarr_url",
        "sonarr_public_url",
        "radarr_url",
        "radarr_public_url",
        "nzbget_url",
        "nzbget_public_url",
    }
)


def _scrub_url_for_log(candidate: str) -> str:
    """Return a log-safe representation of *candidate* — host + path-prefix only.

    The SSRF-blocked path used to log the candidate URL verbatim.  That is
    user-supplied content that may carry an embedded password
    (``http://admin:pa55w0rd@host``), an API key in the query string
    (``?api_key=sk-...``), or an attacker-tagged URL designed for the log
    viewer.  We strip:

    * userinfo (anything before the ``@`` in the netloc),
    * the query string,
    * the fragment,
    * the path beyond the first 32 characters,

    then return ``scheme://host[:port]/<truncated path>``.  If the URL
    fails to parse, fall back to a length-only marker so the log still
    has something useful for triage.
    """
    try:
        parsed = _urlparse(candidate)
    except (ValueError, TypeError):
        return f"<unparseable len={len(candidate)}>"
    scheme = (parsed.scheme or "").lower() or "?"
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path or ""
    if len(path) > 32:
        path = path[:32] + "…"
    return f"{scheme}://{host}{port}{path}"


def validate_url_fields(body: dict) -> JSONResponse | None:
    """Validate all URL fields in *body*.

    Returns a :class:`JSONResponse` error if any URL field is invalid
    (too long, wrong scheme, or blocked by the SSRF guard), or ``None``
    if all URL fields pass validation.
    """
    for url_key in _URL_FIELDS:
        if body.get(url_key):
            candidate = str(body[url_key]).strip()
            if len(candidate) > 2048:
                return respond_err("url_too_long", status=400, message=f"{url_key} too long")
            try:
                parsed = _urlparse(candidate)
            except ValueError:
                parsed = None
            if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc:
                return respond_err(
                    "invalid_url",
                    status=400,
                    message=f"{url_key} must be an http(s) URL",
                )
            if not is_safe_outbound_url(candidate):
                logger.warning(
                    "settings.ssrf_blocked key=%s value=%s",
                    url_key,
                    _scrub_url_for_log(candidate),
                )
                return respond_err(
                    "ssrf_blocked",
                    status=400,
                    message=f"{url_key} points at a blocked address",
                )
    return None
