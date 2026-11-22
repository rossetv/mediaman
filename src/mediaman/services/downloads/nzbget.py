"""NZBGet JSON-RPC client."""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

import requests

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger(__name__)


class NzbgetError(Exception):
    """Raised when the JSON-RPC endpoint returns an error object (method not found, auth
    failure).

    Without it those cases returned {} and callers saw an empty queue with no
    explanation.
    """


#: NZBGet status/queue JSON responses are tiny (typically < 10 KiB).
#: Cap at 1 MiB so a misconfigured or compromised NZBGet cannot pin memory.
_NZBGET_MAX_BYTES = 1 * 1024 * 1024


def _is_lan_host(url: str) -> bool:
    """Return True when *url*'s host is a LAN or loopback address.

    Uses ipaddress to cover RFC1918 (including 172.16.0.0/12), loopback, link-local and
    unique-local ranges; a string-prefix check misclassified several of them and warned
    needlessly about plain HTTP on a LAN.
    """
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
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
        # Log only the hostname, not ``self._url``, which may embed a
        # ``user:pass@`` component and would leak credentials.
        if self._url.startswith("http://") and not _is_lan_host(self._url):
            host = urlparse(self._url).hostname or self._url
            logger.warning(
                "nzbget.plain_http_non_lan host=%s — basic-auth credentials "
                "are transmitted in the clear; consider enabling HTTPS",
                host,
            )

    def _call[T](self, method: str, expected: type[T]) -> T:
        """Invoke *method* and return its result. Raises NzbgetError when the response
        carries a JSON-RPC error field, or is not an object whose result is an
        *expected*, so an idle queue can be told from a broken connection or a reply
        that did not come from NZBGet.
        """
        resp = self._http.post(
            "/jsonrpc",
            json={"method": method},
            auth=self._auth,
            max_bytes=_NZBGET_MAX_BYTES,
        )
        data = resp.json()
        if not isinstance(data, dict):
            raise NzbgetError(f"NZBGet reply for method '{method}' is not a JSON object")
        if data.get("error"):
            raise NzbgetError(f"NZBGet JSON-RPC error for method '{method}': {data['error']}")
        result = data.get("result")
        if not isinstance(result, expected):
            raise NzbgetError(
                f"NZBGet reply for method '{method}' has no {expected.__name__} result"
            )
        return result

    def get_status(self) -> dict[str, object]:
        return self._call("status", dict)

    def get_queue(self) -> list[dict[str, object]]:
        return self._call("listgroups", list)

    def check_reachable(self) -> None:
        """Probe NZBGet; raise ``SafeHTTPError``, ``requests.RequestException`` or
        ``NzbgetError`` when it does not answer as NZBGet, so a caller can tell why.
        """
        self.get_status()

    def is_reachable(self) -> bool:
        """Return True if NZBGet responds. Catches only HTTP and transport errors, not the
        broad Exception.
        """
        try:
            self.check_reachable()
            return True
        except (SafeHTTPError, requests.RequestException, NzbgetError):
            return False
