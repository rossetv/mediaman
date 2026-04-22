"""Shared HTTP base for Arr-family API clients (Radarr, Sonarr, etc.).

``ArrClient`` holds the constructor and the four methods that are identical
across every *arr service so that subclasses only contain service-specific
logic.
"""

from __future__ import annotations

import requests


class ArrClient:
    """Base class for *arr API clients.

    Provides authenticated HTTP helpers and a connection test.  Subclasses
    must not override ``__init__`` — they receive ``url`` and ``api_key``
    here and should add no extra constructor arguments.
    """

    def __init__(self, url: str, api_key: str):
        self._url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}

    def _get(self, path: str) -> dict | list:
        resp = requests.get(f"{self._url}{path}", headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict) -> None:
        resp = requests.put(f"{self._url}{path}", headers=self._headers, json=data, timeout=15)
        resp.raise_for_status()

    def _delete(self, path: str) -> None:
        resp = requests.delete(f"{self._url}{path}", headers=self._headers, timeout=15)
        resp.raise_for_status()

    def test_connection(self) -> bool:
        """Return True if the service's /api/v3/system/status endpoint responds."""
        try:
            self._get("/api/v3/system/status")
            return True
        except Exception:
            return False
