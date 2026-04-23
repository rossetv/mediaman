"""Mailgun email client with EU/US region fallback."""

from __future__ import annotations

import logging

import requests

from mediaman.services.http_client import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger("mediaman")


class MailgunClient:
    """Sends emails via the Mailgun HTTP API.

    Attempts the configured region's endpoint first (``eu`` by default).
    On a 401 or 404 — which Mailgun returns for a domain registered in
    the other region — the client transparently retries against the
    alternative endpoint and remembers the working one for future calls.
    """

    _EU_BASE = "https://api.eu.mailgun.net"
    _US_BASE = "https://api.mailgun.net"

    def __init__(self, domain: str, api_key: str, from_address: str, region: str = "eu") -> None:
        self._domain = domain
        self._api_key = api_key
        self._from = from_address
        self._base = self._EU_BASE if region == "eu" else self._US_BASE
        self._session = requests.Session()
        # Mailgun switches region per call, so the SafeHTTPClient is
        # instantiated without a base_url and the absolute URL is
        # recomputed per request.
        self._http = SafeHTTPClient(session=self._session)

    def _other_base(self) -> str:
        """Return the alternate (EU/US) base URL to the currently active one."""
        return self._US_BASE if self._base == self._EU_BASE else self._EU_BASE

    def send(self, *, to: str, subject: str, html: str) -> None:
        data = {"from": self._from, "to": to, "subject": subject, "html": html}
        bases = [self._base, self._other_base()]
        last_error: Exception | None = None
        for base in bases:
            try:
                self._http.post(
                    f"{base}/v3/{self._domain}/messages",
                    auth=("api", self._api_key),
                    data=data,
                    timeout=(5.0, 30.0),
                )
                self._base = base  # remember what worked
                return
            except SafeHTTPError as exc:
                if exc.status_code in (401, 404) and base != bases[-1]:
                    logger.info(
                        "Mailgun %s returned %s — retrying against alternate region",
                        base, exc.status_code,
                    )
                    last_error = exc
                    continue
                last_error = exc
                break
            except requests.RequestException as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error

    def send_to_many(self, *, recipients: list[str], subject: str, html: str) -> None:
        """Send the same message to each recipient.

        Failures on individual recipients propagate immediately — callers
        that need per-recipient fault isolation should loop over ``send``
        and handle exceptions themselves.
        """
        for recipient in recipients:
            self.send(to=recipient, subject=subject, html=html)

    def test_connection(self) -> bool:
        """Return True if either region reports the domain exists."""
        for base in (self._base, self._other_base()):
            try:
                self._http.get(
                    f"{base}/v3/domains/{self._domain}",
                    auth=("api", self._api_key),
                )
                self._base = base
                return True
            except (SafeHTTPError, requests.RequestException):
                continue
        return False
