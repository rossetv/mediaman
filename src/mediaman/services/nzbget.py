"""NZBGet JSON-RPC client."""

from __future__ import annotations

import requests

from mediaman.services.http_client import SafeHTTPClient


class NzbgetClient:
    """Thin JSON-RPC wrapper around the NZBGet HTTP API."""

    def __init__(self, url: str, username: str, password: str) -> None:
        self._url = url.rstrip("/")
        self._auth: tuple[str, str] = (username, password)
        self._session = requests.Session()
        self._http = SafeHTTPClient(self._url, session=self._session)

    def _call(self, method: str) -> dict | list:
        """Invoke *method* on the NZBGet JSON-RPC endpoint and return the result."""
        resp = self._http.post(
            "/jsonrpc",
            json={"method": method},
            auth=self._auth,
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
