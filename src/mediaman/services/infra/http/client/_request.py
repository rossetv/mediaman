"""Low-level transport and per-request helpers for the outbound HTTP client.

This module owns the request-engine seam that :class:`._core.SafeHTTPClient`
composes: the single-attempt transport function (:func:`_dispatch`), the
per-call SSRF / transport indirection helpers (:func:`_resolve_outbound`,
:func:`_invoke_dispatch`), and the module-level timeout / size-cap defaults.

Patchability note
-----------------
``_dispatch`` and ``resolve_safe_outbound_url`` are resolved at call time
through the ``mediaman.services.infra.http.client`` package namespace (via
``sys.modules``) rather than via a static import binding.  This is required
so that ``monkeypatch.setattr(http_client, "_dispatch", ...)`` in tests
intercepts the actual transport call — otherwise pytest's monkeypatch would
change the name in the wrong module dict and the original function would still
be invoked.  The same applies to ``resolve_safe_outbound_url``.  Both names
are re-exported from the package barrel (``client/__init__.py``) so the
``sys.modules`` lookup of ``_HTTP_CLIENT_MODULE`` finds the patch target.
"""

from __future__ import annotations

import sys
from typing import Any

import requests

from mediaman.services.infra.url_safety import (
    resolve_safe_outbound_url as _resolve_safe_outbound_url,
)

# Public alias so tests can monkeypatch ``http.client.resolve_safe_outbound_url``
# and have ``_request`` pick up the patched version via sys.modules lookup.
resolve_safe_outbound_url = _resolve_safe_outbound_url

_HTTP_CLIENT_MODULE = "mediaman.services.infra.http.client"

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Default per-call timeouts.  Connect is short — a TCP handshake that hasn't
#: completed in 5 s means the target is unreachable.  Read is generous because
#: OpenAI and TMDB occasionally take 20-30 s.
_DEFAULT_TIMEOUT_SECONDS: tuple[float, float] = (5.0, 30.0)

#: Default response-size cap.  8 MiB is well above any sane JSON API payload
#: but low enough that a pathological upstream cannot pin memory on a worker.
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def _build_user_agent() -> str:
    """Return ``mediaman/<version>`` for outbound HTTP attribution.

    Lazy import keeps this module free of an early package-level import.
    Falls back to a fixed string when the version cannot be resolved (e.g.
    running uninstalled from source).
    """
    try:
        from mediaman import __version__ as version

        return f"mediaman/{version}"
    except ImportError:
        return "mediaman/dev"


_USER_AGENT = _build_user_agent()


# ---------------------------------------------------------------------------
# Low-level transport function
# ---------------------------------------------------------------------------

# rationale: caller / json / data / auth mirror ``requests.Session.request``
# (and ``requests.adapters.HTTPAdapter``), which themselves accept ``Any``
# for these parameters — there is no upstream stub that pins the shape.
# Tightening here would force every caller in the codebase to either cast
# or duplicate the requests library's permissive contract. The same
# applies to ``params: dict[str, Any]``: ``requests`` accepts values of
# any JSON-serialisable type for query parameters. The rationale below
# applies to every ``Any`` site in :func:`_dispatch` and the verb methods
# on :class:`SafeHTTPClient`.


def _dispatch(
    caller: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None,
    params: dict[str, Any] | None,
    json: Any,
    data: Any,
    auth: Any,
    timeout: tuple[float, float],
) -> requests.Response:
    """Issue a single HTTP request via *caller* with safe defaults.

    Split out so tests can patch the transport at one well-known point
    without caring about the retry / SSRF machinery above it.
    """
    resp: requests.Response = caller.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json,
        data=data,
        auth=auth,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    return resp


# ---------------------------------------------------------------------------
# Per-request helpers
# ---------------------------------------------------------------------------


def _resolve_outbound(
    url: str,
    allowed_hosts: frozenset[str] | None,
) -> tuple[bool, str | None, str | None]:
    """Run the SSRF guard on *url*, returning ``(safe, hostname, pinned_ip)``.

    ``resolve_safe_outbound_url`` is looked up at call time from the
    package's namespace (via ``sys.modules``) rather than the static import
    binding, so that ``monkeypatch.setattr(http_client,
    "resolve_safe_outbound_url", ...)`` in tests intercepts the real guard.
    See the module docstring for why the patch seam has to be dynamic.

    ``allowed_hosts`` is only forwarded when the caller actually supplied an
    allowlist: a ``None`` allowlist is equivalent to omitting the kwarg from
    the upstream call, which preserves compatibility with monkeypatches that
    accept ``url`` alone (and the older ``url, strict_egress=...`` shape).
    """
    _http_client_mod = sys.modules.get(_HTTP_CLIENT_MODULE)
    _resolve = (
        getattr(_http_client_mod, "resolve_safe_outbound_url", None)
        if _http_client_mod is not None
        else None
    ) or _resolve_safe_outbound_url
    if allowed_hosts is None:
        return _resolve(url)
    return _resolve(url, allowed_hosts=allowed_hosts)


def _invoke_dispatch(
    caller: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None,
    params: dict[str, Any] | None,
    json: Any,
    data: Any,
    auth: Any,
    timeout: tuple[float, float],
) -> requests.Response:
    """Issue one transport call, resolving ``_dispatch`` at call time.

    ``_dispatch`` is looked up from the package's namespace (via
    ``sys.modules``) on every call so that
    ``monkeypatch.setattr(http_client, "_dispatch", ...)`` in tests
    intercepts the actual transport. Lifted out of ``_request`` so the
    orchestrator stays a flat table of contents; the behaviour is identical
    to the former nested ``_dispatch_fn`` closure.
    """
    _hc = sys.modules.get(_HTTP_CLIENT_MODULE)
    _d = getattr(_hc, "_dispatch", None) if _hc is not None else None
    if _d is None:
        _d = _dispatch
    return _d(
        caller,
        method,
        url,
        headers=headers,
        params=params,
        json=json,
        data=data,
        auth=auth,
        timeout=timeout,
    )
