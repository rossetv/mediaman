"""Mailgun email client with EU/US region fallback."""

from __future__ import annotations

import logging

import requests
from requests.exceptions import RequestException
from urllib3.exceptions import NewConnectionError

from mediaman.core.email_validation import validate_email_address as _validate_recipient
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError
from mediaman.services.mail.plaintext import html_to_text

logger = logging.getLogger(__name__)

# Only a 429 is retried: Mailgun refused the message outright.  A 5xx can follow a
# message Mailgun already accepted, and a send has no idempotency key, so repeating it
# could deliver twice.
_RETRYABLE_POST_STATUSES = frozenset({429})


def _request_never_left(exc: Exception) -> bool:
    """True when the failure happened before the request was sent, so a retry cannot duplicate it.

    That is a connect timeout, or a connection that could not be opened (refused, no route,
    DNS failure). A read timeout or a reset after the request went out may mean Mailgun
    accepted the message, so it is not retried.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError) and exc.args:
        reason = getattr(exc.args[0], "reason", None)
        return isinstance(reason, NewConnectionError)
    return False


# Characters that must never appear in RFC 2822 header values (subject,
# from, to) — a newline would allow header injection.
_HEADER_INJECT_CHARS = frozenset("\r\n\0")


def _validate_header_value(value: str, field: str) -> None:
    """Raise ValueError if *value* contains CR, LF, or NUL (header injection guard)."""
    if any(c in value for c in _HEADER_INJECT_CHARS):
        raise ValueError(f"Header field '{field}' contains illegal characters")


class MailgunClient:
    """Sends emails via the Mailgun HTTP API.

    On 401 or 404 (Mailgun's answer for a domain registered in the other region) it
    retries against the alternate region and remembers the working one.
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
        return self._US_BASE if self._base == self._EU_BASE else self._EU_BASE

    def send(self, *, to: str, subject: str, html: str, unsubscribe_url: str | None = None) -> None:
        """Send *html* with a plain-text alternative made from it. *unsubscribe_url*, when
        given, is advertised in the List-Unsubscribe header so clients can offer their own
        unsubscribe control.
        """
        # Defensive validation: reject bad addresses and header-injectable values
        # before making the network call. Routes may validate at ingress, but the
        # client must not depend on that.
        _validate_recipient(to)
        _validate_header_value(subject, "subject")

        data = {
            "from": self._from,
            "to": to,
            "subject": subject,
            "html": html,
            "text": html_to_text(html),
        }
        if unsubscribe_url:
            _validate_header_value(unsubscribe_url, "List-Unsubscribe")
            data["h:List-Unsubscribe"] = f"<{unsubscribe_url}>"
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
                    retryable_statuses=_RETRYABLE_POST_STATUSES,
                    retry_transport_error=_request_never_left,
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
                        "mailgun.region_fallback",
                        extra={"base": base, "reason": "404_domain_not_found"},
                    )
                    last_error = exc
                    continue
                last_error = exc
                break
            except RequestException as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error

    def check_reachable(self) -> None:
        """Probe both regions; raise only when every one failed. A 401 or 403 from any
        region wins, since it means the key is wrong; otherwise the last failure is raised.
        """
        last_error: SafeHTTPError | RequestException | None = None
        rejected: SafeHTTPError | None = None
        for base in (self._base, self._other_base()):
            try:
                self._http.get(
                    f"{base}/v3/domains/{self._domain}",
                    auth=("api", self._api_key),
                )
                self._base = base
                return
            except (SafeHTTPError, RequestException) as exc:
                # Log at debug level so an operator can see what failed.
                logger.debug(
                    "mailgun.reachability_check_failed",
                    extra={"base": base},
                    exc_info=True,
                )
                last_error = exc
                if isinstance(exc, SafeHTTPError) and exc.status_code in (401, 403):
                    rejected = exc
        if rejected is not None:
            raise rejected
        if last_error is not None:
            raise last_error

    def is_reachable(self) -> bool:
        try:
            self.check_reachable()
            return True
        except (SafeHTTPError, RequestException):
            return False
