"""Shared HTTP base for Arr-family API clients (Radarr, Sonarr, etc.).

``ArrClient`` holds the constructor and the four methods that are identical
across every *arr service so that subclasses only contain service-specific
logic. All outbound calls route through :class:`SafeHTTPClient` for SSRF
re-validation, size capping, and redirect refusal.
"""

from __future__ import annotations

import requests

from mediaman.services.http_client import SafeHTTPClient


class ArrClient:
    """Base class for *arr API clients.

    Provides authenticated HTTP helpers and a connection test.  Subclasses
    must not override ``__init__`` — they receive ``url`` and ``api_key``
    here and should add no extra constructor arguments.
    """

    def __init__(self, url: str, api_key: str):
        self._url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._session = requests.Session()
        self._http = SafeHTTPClient(self._url, session=self._session)

    def _get(self, path: str) -> dict | list:
        resp = self._http.get(path, headers=self._headers)
        return resp.json()

    def _put(self, path: str, data: dict) -> None:
        self._http.put(path, headers=self._headers, json=data)

    def _post(self, path: str, data: dict) -> dict | list:
        resp = self._http.post(path, headers=self._headers, json=data)
        return resp.json()

    def _delete(self, path: str) -> None:
        self._http.delete(path, headers=self._headers)

    def test_connection(self) -> bool:
        """Return True if the service's /api/v3/system/status endpoint responds."""
        try:
            self._get("/api/v3/system/status")
            return True
        except Exception:
            return False
