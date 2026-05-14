"""Error type for :class:`~mediaman.services.infra.http.client.SafeHTTPClient`.

Split out from the orchestration so both the request engine
(:mod:`._request`) and the client class (:mod:`._core`) can import
:class:`SafeHTTPError` without a circular dependency. Re-exported from the
package barrel so ``from mediaman.services.infra.http.client import
SafeHTTPError`` keeps working.
"""

from __future__ import annotations

import json as _json


class SafeHTTPError(Exception):
    """Raised when a :class:`SafeHTTPClient` call returns a non-2xx.

    Carries the final status code, a truncated body snippet, and the URL that
    failed.  The snippet is UTF-8 best-effort — binary bodies are replaced
    rather than raising a secondary decode error.
    """

    def __init__(self, status_code: int, body_snippet: str, url: str) -> None:
        self.status_code = status_code
        self.body_snippet = body_snippet
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}: {body_snippet[:120]}")

    def json_error(self) -> dict[str, object] | None:
        """Return the parsed JSON error body, or ``None`` if not JSON."""
        try:
            parsed = _json.loads(self.body_snippet)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
