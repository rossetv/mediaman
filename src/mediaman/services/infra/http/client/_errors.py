"""Error type for ``SafeHTTPClient``.

Kept apart from the client class (``_core``) so it can be imported
without a circular dependency. Re-exported from the package barrel so
callers use ``from mediaman.services.infra.http.client import
SafeHTTPError``.
"""

from __future__ import annotations

import json as _json
from urllib.parse import urlparse


def _safe_url(url: str) -> str:
    """Return ``host/path`` of *url* with the query string stripped.

    Query strings on outbound URLs routinely carry API keys
    (``?apikey=...``). This exception is logged with ``logger.exception``
    at several sites, so its ``str()`` must never embed the raw query
    or the key lands in the logs. Mirrors
    ``_safe_path``.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "?"
    host = parsed.hostname or "?"
    return f"{host}{parsed.path or '/'}"


class SafeHTTPError(Exception):
    """Raised when a ``SafeHTTPClient`` call returns a non-2xx.

    Carries the final status code, a truncated body snippet, and the
    query-stripped URL that failed.  The snippet is UTF-8 best-effort —
    binary bodies are replaced rather than raising a secondary decode error.

    ``url`` is stored as ``host/path`` only — the query string is dropped so
    a logged exception cannot leak an ``?apikey=...`` credential.
    """

    def __init__(self, status_code: int, body_snippet: str, url: str) -> None:
        self.status_code = status_code
        self.body_snippet = body_snippet
        self.url = _safe_url(url)
        super().__init__(f"HTTP {status_code} from {self.url}: {body_snippet[:120]}")

    def json_error(self) -> dict[str, object] | None:
        try:
            parsed = _json.loads(self.body_snippet)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
