"""Error type for :class:`~mediaman.services.infra.http.client.SafeHTTPClient`.

Split out from the orchestration so both the request engine
(:mod:`._request`) and the client class (:mod:`._core`) can import
:class:`SafeHTTPError` without a circular dependency. Re-exported from the
package barrel so ``from mediaman.services.infra.http.client import
SafeHTTPError`` keeps working.
"""

from __future__ import annotations

import json as _json
from urllib.parse import urlparse


def _safe_url(url: str) -> str:
    """Return ``host/path`` of *url* with the query string stripped.

    Query strings on outbound URLs routinely carry API keys
    (``?apikey=...``). This exception is logged with ``logger.exception``
    at several §6.4 sites, so its ``str()`` must never embed the raw query
    or the key lands in the logs. Mirrors
    :func:`~mediaman.services.infra.http.retry._safe_path`.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "?"
    host = parsed.hostname or "?"
    return f"{host}{parsed.path or '/'}"


class SafeHTTPError(Exception):
    """Raised when a :class:`SafeHTTPClient` call returns a non-2xx.

    Carries the final status code, a truncated body snippet, and the
    query-stripped URL that failed.  The snippet is UTF-8 best-effort —
    binary bodies are replaced rather than raising a secondary decode error.

    ``url`` is stored as ``host/path`` only — the query string is dropped so
    a logged exception cannot leak an ``?apikey=...`` credential (L1 / §7.4).
    """

    def __init__(self, status_code: int, body_snippet: str, url: str) -> None:
        self.status_code = status_code
        self.body_snippet = body_snippet
        self.url = _safe_url(url)
        super().__init__(f"HTTP {status_code} from {self.url}: {body_snippet[:120]}")

    def json_error(self) -> dict[str, object] | None:
        """Return the parsed JSON error body, or ``None`` if not JSON."""
        try:
            parsed = _json.loads(self.body_snippet)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
