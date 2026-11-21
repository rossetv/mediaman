"""Settings URL-field validation: the _URL_FIELDS set, _scrub_url_for_log (strips credentials
and query strings before logging) and validate_url_fields. Kept narrow so the URL handling
does not inflate the package __init__.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse as _urlparse

from fastapi.responses import JSONResponse

from mediaman.services.infra import is_safe_outbound_url
from mediaman.web.responses import respond_err

logger = logging.getLogger(__name__)

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
    """Return a log-safe form of candidate: scheme://host[:port]/<path truncated to 32 chars>.

    The candidate is user-supplied and may carry a password (http://admin:pw@host), an API
    key in the query string or attacker-tagged content for the log viewer, so userinfo,
    query and fragment are dropped. If the URL fails to parse, a length-only marker is
    returned.
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


def validate_url_fields(body: dict[str, object]) -> JSONResponse | None:
    """Validate all URL fields in body. Returns a JSONResponse error if any is invalid (too
    long, wrong scheme or blocked by the SSRF guard), else None.
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
            # URL is validated pre-save against admin-typed input; no conn-derived allowlist applies here.
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
