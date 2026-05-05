"""NZBGet JSON-RPC client."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import requests

from mediaman.services.infra.http import SafeHTTPClient

logger = logging.getLogger("mediaman")

#: NZBGet status/queue JSON responses are tiny (typically < 10 KiB).
#: Cap at 1 MiB so a misconfigured or compromised NZBGet cannot pin memory.
_NZBGET_MAX_BYTES = 1 * 1024 * 1024


def _is_lan_host(url: str) -> bool:
    """Return True when *url*'s host looks like a LAN/loopback address.

    Uses :mod:`ipaddress` to evaluate every RFC1918 / loopback / link-local /
    ULA range correctly, including the previously-missed:

    * ``172.16.0.0/12`` — the third RFC1918 v4 block.
    * ``::1/128`` — IPv6 loopback.
    * ``fc00::/7`` — unique local addresses (RFC4193).
    * ``fe80::/10`` — IPv6 link-local.

    The plain string-prefix check this replaced (``"127."``, ``"10."``,
    ``"192.168."``) silently misclassified any of the above as
    "internet-side" and therefore noisily warned about plain-HTTP
    credentials on a perfectly LAN-local NZBGet.
    """
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    # Strip IPv6 brackets if present — urlparse leaves them off, but a
    # literal IPv6 host could still arrive here from a misconfigured URL.
    candidate = host.strip("[]")
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        # Hostname rather than literal IP — we cannot resolve it here
        # without DNS (and doing so would defeat the purpose of the
        # warning).  Be conservative: assume non-LAN so the operator
        # still sees the plain-HTTP advisory.
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


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

    def _call(self, method: str) -> Any:
        """Invoke *method* on the NZBGet JSON-RPC endpoint and return the result."""
        resp = self._http.post(
            "/jsonrpc",
            json={"method": method},
            auth=self._auth,
            max_bytes=_NZBGET_MAX_BYTES,
        )
        return resp.json().get("result", {})

    def get_status(self) -> dict[str, object]:
        """Return the NZBGet global status dict."""
        result = self._call("status")
        return result if isinstance(result, dict) else {}

    def get_queue(self) -> list[dict[str, object]]:
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
