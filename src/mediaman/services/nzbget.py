"""NZBGet JSON-RPC client."""

from __future__ import annotations

import logging

import requests

from mediaman.services.http_client import SafeHTTPClient

logger = logging.getLogger("mediaman")

#: NZBGet status/queue JSON responses are tiny (typically < 10 KiB).
#: Cap at 1 MiB so a misconfigured or compromised NZBGet cannot pin memory.
_NZBGET_MAX_BYTES = 1 * 1024 * 1024


def _is_lan_host(url: str) -> bool:
    """Return True when *url*'s host looks like a LAN/loopback address."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return host == "localhost" or any(
        host.startswith(p) for p in ("127.", "10.", "192.168.")
    )


class NzbgetClient:
    """Thin JSON-RPC wrapper around the NZBGet HTTP API.

    Caps response bodies at 1 MiB — NZBGet status/queue responses are tiny
    and an oversized body is a sign of misconfiguration or a compromised
    endpoint. Also warns (once per construction) when the URL is plain HTTP
    and not obviously on the LAN — credentials travel in the clear otherwise.
    """

    def __init__(self, url: str, username: str, password: str) -> None:
        self._url = url.rstrip("/")
        self._auth: tuple[str, str] = (username, password)
        self._session = requests.Session()
        self._http = SafeHTTPClient(self._url, session=self._session)

        # Warn if credentials will travel over plain HTTP to a non-LAN host.
        if self._url.startswith("http://") and not _is_lan_host(self._url):
            logger.warning(
                "nzbget.plain_http_non_lan url=%s — basic-auth credentials "
                "are transmitted in the clear; consider enabling HTTPS",
                self._url,
            )

    def _call(self, method: str) -> dict | list:
        """Invoke *method* on the NZBGet JSON-RPC endpoint and return the result."""
        resp = self._http.post(
            "/jsonrpc",
            json={"method": method},
            auth=self._auth,
            max_bytes=_NZBGET_MAX_BYTES,
        )
        return resp.json().get("result", {})

    def get_status(self) -> dict:
        """Return the NZBGet global status dict."""
        result = self._call("status")
        return result if isinstance(result, dict) else {}

    def get_queue(self) -> list[dict]:
        """Return the current NZBGet download queue."""
        result = self._call("listgroups")
        return result if isinstance(result, list) else []

    def test_connection(self) -> bool:
        """Return True if NZBGet is reachable and responding."""
        try:
            self.get_status()
            return True
        except Exception:
            return False
