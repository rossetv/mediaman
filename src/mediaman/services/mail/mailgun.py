"""Mailgun email client with EU/US region fallback."""

from __future__ import annotations

import email.utils
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

import requests

from mediaman.services.infra.http_client import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger("mediaman")

_T = TypeVar("_T")

# Transient HTTP status codes that warrant a retry on POST requests.
_RETRYABLE_POST_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_with_jitter[T](fn: Callable[[], _T], *, attempts: int = 3) -> _T:
    """Call *fn* up to *attempts* times, retrying on transient errors.

    Applies exponential backoff with full jitter between retries. Aborts
    immediately after two consecutive 5xx responses (which suggest the
    remote is genuinely unhealthy rather than temporarily overloaded).

    Only :class:`SafeHTTPError` with a status in ``_RETRYABLE_POST_STATUSES``
    and :class:`requests.RequestException` (network errors) trigger a retry.
    Any other exception, including a 401 or 404, propagates immediately.

    Args:
        fn: Zero-argument callable to execute; must raise on failure.
        attempts: Maximum number of total attempts (default 3).

    Returns:
        Whatever *fn* returns on success.
    """
    consecutive_5xx = 0
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            result = fn()
            return result
        except SafeHTTPError as exc:
            if exc.status_code not in _RETRYABLE_POST_STATUSES:
                raise
            last_exc = exc
            if exc.status_code >= 500:
                consecutive_5xx += 1
                if consecutive_5xx >= 2:
                    logger.warning("Mailgun: two consecutive 5xx responses — aborting retries")
                    raise
            else:
                consecutive_5xx = 0
        except requests.RequestException as exc:
            last_exc = exc
            consecutive_5xx = 0

        if attempt < attempts - 1:
            # Full-jitter exponential backoff: sleep in [0, 2^attempt) seconds.
            delay = random.uniform(0, 2**attempt)
            logger.debug(
                "Mailgun: transient error on attempt %d, retrying in %.2fs", attempt + 1, delay
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


# Characters that must never appear in RFC 2822 header values (subject,
# from, to) — a newline would allow header injection.
_HEADER_INJECT_CHARS = frozenset("\r\n\0")


def _validate_recipient(address: str) -> None:
    """Raise ValueError if *address* is not a valid, injection-safe email address.

    Checks performed:
    - ``email.utils.parseaddr`` must yield a non-empty address.
    - The raw string must not contain CR, LF, or NUL (header injection guard).
    """
    if any(c in address for c in _HEADER_INJECT_CHARS):
        raise ValueError(f"Recipient address contains illegal characters: {address!r}")
    _, parsed = email.utils.parseaddr(address)
    if not parsed or "@" not in parsed:
        raise ValueError(f"Invalid recipient email address: {address!r}")


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

                def _do_post(b: str = base) -> None:
                    self._http.post(
                        f"{b}/v3/{self._domain}/messages",
                        auth=("api", self._api_key),
                        data=data,
                        timeout=(5.0, 30.0),
                    )

                _retry_with_jitter(_do_post)
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
