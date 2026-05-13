"""Hardened `requests.Session` for Plex API calls with SSRF re-validation, body-size cap, and redirect refusal."""

from __future__ import annotations

import re as _re
import sys as _sys
from typing import Any

import requests as http_requests

from mediaman.services.infra import (
    SafeHTTPError,
)
from mediaman.services.infra import (
    resolve_safe_outbound_url as _resolve_safe_outbound_url_default,
)

# pin is not re-exported from mediaman.services.infra; import directly
# from the http sub-package which owns it.
from mediaman.services.infra.http import pin

# Name of the parent module — used to look up the potentially-monkeypatched
# ``resolve_safe_outbound_url`` at call time so test fixtures that patch
# ``mediaman.services.media_meta.plex.resolve_safe_outbound_url`` continue
# to work even though this class lives in a sub-module.
_PLEX_MODULE_NAME = "mediaman.services.media_meta.plex"


def _resolve_safe_outbound_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Call ``resolve_safe_outbound_url``, delegating through the parent
    ``plex`` module's namespace when it is already imported.

    This indirection allows test code that monkeypatches
    ``plex.resolve_safe_outbound_url`` to affect behaviour here without
    requiring a circular import.

    ``allowed_hosts`` is threaded through so per-session allowlist
    enforcement (W1.32) reaches the underlying helper. When ``None``,
    the helper falls back to deny-list-only validation.
    """
    plex_mod = _sys.modules.get(_PLEX_MODULE_NAME)
    fn = (
        getattr(plex_mod, "resolve_safe_outbound_url", _resolve_safe_outbound_url_default)
        if plex_mod is not None
        else _resolve_safe_outbound_url_default
    )
    # Preserve compatibility with monkeypatches that take ``url`` only:
    # only thread ``allowed_hosts`` through when the caller actually
    # supplied a set. A ``None`` allowlist is the deny-list-only default
    # and equivalent to omitting the kwarg.
    if allowed_hosts is None:
        return fn(url)
    return fn(url, allowed_hosts=allowed_hosts)


# Matches X-Plex-Token query parameter values so they can be redacted from
# exception messages and log lines before they propagate.
_PLEX_TOKEN_RE = _re.compile(r"(X-Plex-Token=)[^&\s\"'>]+", _re.IGNORECASE)

#: Hard cap on a single plexapi response body. Library/season XML is
#: small even on large libraries — 16 MiB is well above any sane limit
#: while still preventing a runaway upstream from filling memory.
_PLEX_MAX_BYTES = 16 * 1024 * 1024

#: ``(connect, read)`` timeout used when plexapi passes us a single int.
#: 5 s connect matches mediaman's other clients; 30 s read is generous
#: enough for a slow library scan but stops a slow-lorris stalling a
#: worker indefinitely.
_PLEX_TIMEOUT_SECONDS: tuple[float, float] = (5.0, 30.0)


def _scrub_plex_token(msg: str) -> str:
    """Replace any ``X-Plex-Token=<value>`` substring in *msg* with ``<redacted>``.

    Applied to exception messages and log lines before they propagate so the
    token never appears in tracebacks, log files, or error responses.
    """
    return _PLEX_TOKEN_RE.sub(r"\1<redacted>", msg)


class _SafePlexSession(http_requests.Session):
    """``requests.Session`` subclass enforcing mediaman's outbound rules.

    Injected into :class:`plexapi.server.PlexServer` via the ``session=``
    constructor kwarg so every plexapi call — library enumeration,
    section scanning, raw queries — inherits:

    * Per-call SSRF re-validation, including IDN normalisation and
      DNS-rebind defence (the validated address is pinned for the
      duration of the request).
    * ``allow_redirects=False`` — a 302 to ``169.254.169.254`` would
      otherwise leak the X-Plex-Token into cloud metadata.
    * Streamed body capped at :data:`_PLEX_MAX_BYTES`, so a malicious
      or buggy upstream cannot pin a worker's memory.
    * ``(connect, read)`` timeout split, so a slow-lorris read cannot
      hold a connection indefinitely.

    The class deliberately does NOT inherit from ``SafeHTTPClient``;
    plexapi calls ``self._session.get(...)``-style methods, and
    ``requests.Session`` is the base contract those expect. We hook
    ``request()`` because every verb method routes through it.
    """

    def __init__(self, *, allowed_hosts: frozenset[str] | None = None) -> None:
        super().__init__()
        # Per-session allowlist (W1.32). The caller derives the
        # composed set once at the boundary via
        # :func:`~mediaman.services.infra.url_safety.allowed_outbound_hosts`
        # and threads it through ``PlexClient`` to here. ``None`` means
        # deny-list-only — the default for unit-test fixtures that do
        # not exercise the allowlist path.
        self._allowed_hosts = allowed_hosts

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        # rationale: this method overrides ``requests.Session.request`` whose
        # base contract accepts ``**kwargs: Any`` to allow caller-supplied
        # transport options (timeout, allow_redirects, stream, verify, cert,
        # proxies, hooks, ...).  Narrowing here would break the inheritance
        # contract; plexapi passes a handful of these keys at call time.
        **kwargs: Any,
    ) -> http_requests.Response:
        # 1. SSRF re-validation. Re-runs at every request so DNS-rebind
        #    cannot slip past a one-off check at PlexServer construction
        #    time, and so a configured Plex URL pointing at an internal
        #    service is refused even if the operator persisted it before
        #    the check existed.
        safe, hostname, pinned_ip = _resolve_safe_outbound_url(
            url, allowed_hosts=self._allowed_hosts
        )
        if not safe:
            # Match the SafeHTTPError shape for consistency with
            # SafeHTTPClient — the scrubbed URL keeps any token out of
            # the exception message.
            raise SafeHTTPError(
                status_code=0,
                body_snippet="refused by SSRF guard",
                url=_scrub_plex_token(url),
            )

        # 2. Force redirect refusal. A 302 to a metadata endpoint would
        #    take the X-Plex-Token header along for the ride, so we
        #    refuse to follow ANY redirect from a Plex URL.
        kwargs["allow_redirects"] = False

        # 3. Always stream so we control the body cap.
        kwargs["stream"] = True

        # 4. Normalise the timeout. plexapi defaults to a single int —
        #    convert to (connect, read) for stable behaviour. A caller
        #    passing a tuple already is honoured untouched.
        timeout = kwargs.get("timeout")
        if timeout is None or isinstance(timeout, (int, float)):
            kwargs["timeout"] = _PLEX_TIMEOUT_SECONDS

        # 5. DNS pin + dispatch. The pin closes the rebind window
        #    between the SSRF check above and the actual connect.
        if hostname and pinned_ip:
            with pin(hostname, pinned_ip):
                response = super().request(method, url, **kwargs)
        else:
            response = super().request(method, url, **kwargs)

        # 6. Body cap — read up to the limit and re-attach so plexapi's
        #    .text / .content access works as it expects.
        try:
            body = self._read_capped(response, _PLEX_MAX_BYTES)
        except _PlexBodyTooLarge as exc:
            response.close()
            raise SafeHTTPError(
                status_code=response.status_code,
                body_snippet=str(exc),
                url=_scrub_plex_token(url),
            ) from None
        response._content = body
        # ``_content_consumed`` is a private requests internal; setattr keeps
        # mypy quiet about poking at non-public attributes.
        response.__setattr__("_content_consumed", True)
        return response

    @staticmethod
    def _read_capped(response: http_requests.Response, max_bytes: int) -> bytes:
        """Read the response body up to *max_bytes*, raising if the cap is hit.

        Mirrors ``http_client._read_capped`` but kept private to this
        module so the Plex session has no compile-time dependency on
        SafeHTTPClient internals.
        """
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise _PlexBodyTooLarge(
                        f"Plex response body too large: declared {declared} > cap {max_bytes}"
                    )
            except ValueError:
                pass
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise _PlexBodyTooLarge(f"Plex response body exceeded cap of {max_bytes} bytes")
        return bytes(buf)


class _PlexBodyTooLarge(Exception):
    """Internal signal that a Plex response breached the body cap."""
