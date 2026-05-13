"""Mailgun email client with EU/US region fallback."""

from __future__ import annotations

import logging

import requests

from mediaman.core.email_validation import validate_email_address as _validate_recipient
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger(__name__)

# Transient HTTP status codes that warrant a retry on POST requests.
# Mailgun's previous standalone retry primitive included ``500`` in this
# set; the consolidated :func:`dispatch_loop` retry path now accepts this
# override via ``retryable_statuses`` so the mailgun POST retains its
# 500-also-retryable policy without duplicating retry orchestration.
_RETRYABLE_POST_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Number of consecutive 5xx responses that trip the early-abort guard.
#: A genuinely unhealthy upstream is unlikely to recover within the retry
#: window — burning all three attempts only delays the inevitable failure.
_CONSECUTIVE_5XX_ABORT = 2


# Characters that must never appear in RFC 2822 header values (subject,
# from, to) — a newline would allow header injection.
_HEADER_INJECT_CHARS = frozenset("\r\n\0")


def _validate_header_value(value: str, field: str) -> None:
    """Raise ValueError if *value* contains CR, LF, or NUL (header injection guard)."""
    if any(c in value for c in _HEADER_INJECT_CHARS):
        raise ValueError(f"Header field '{field}' contains illegal characters")


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
        # Defensive validation: a CR/LF/NUL in the configured ``from``
        # address would let an attacker who controlled the settings table
        # (e.g. via a stale admin session) inject arbitrary headers into
        # every outbound message.  Reject at construction so a bad
        # configuration fails closed instead of slipping through into
        # every ``send`` call.
        _validate_header_value(from_address, "from")
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
        # Defensive validation: reject bad addresses and header-injectable values
        # before making the network call. Routes may validate at ingress, but the
        # client must not depend on that.
        _validate_recipient(to)
        _validate_header_value(subject, "subject")

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
                    retry=True,
                    jitter_strategy="full",
                    abort_after_consecutive_5xx=_CONSECUTIVE_5XX_ABORT,
                    retryable_statuses=_RETRYABLE_POST_STATUSES,
                )
                self._base = base  # remember what worked
                return
            except SafeHTTPError as exc:
                # 401 means the API key is wrong — retrying the other region
                # will not help and would confuse the log.  Only fall back on
                # 404, which Mailgun uses when the domain is registered in the
                # other region (a genuine region-routing error).
                if exc.status_code == 401:
                    last_error = exc
                    break
                if exc.status_code == 404 and base != bases[-1]:
                    logger.info(
                        "Mailgun %s returned 404 — domain may be in alternate region, retrying",
                        base,
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

    def is_reachable(self) -> bool:
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
