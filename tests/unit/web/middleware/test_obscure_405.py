"""Tests for :class:`mediaman.web.middleware.obscure_405.Obscure405Middleware`.

Covers the 405→401 normalisation on ``/api/*`` (method-enumeration
defence) plus the L1 wiring guarantee: because ``obscure_405`` rebuilds a
fresh ``Response`` and drops the inner app's headers, the outer
``SecurityHeadersMiddleware`` must re-apply security headers via
``setdefault`` — which only holds when ``obscure_405`` is wrapped INSIDE
the security-headers middleware (the production ordering).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.web.middleware.obscure_405 import Obscure405Middleware
from mediaman.web.middleware.security_headers import SecurityHeadersMiddleware


def _app_with_405(*, with_security_headers: bool) -> FastAPI:
    """Minimal app with a single GET-only ``/api/thing`` route.

    A POST to that path yields a 405 from the router, which the
    ``Obscure405Middleware`` should rewrite to a 401.
    """
    app = FastAPI()
    # Production order: SecurityHeaders is added AFTER (so it wraps
    # OUTSIDE) Obscure405 — outermost is added last.
    app.add_middleware(Obscure405Middleware)
    if with_security_headers:
        app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/thing")
    def _thing():
        return {"ok": True}

    @app.get("/page")
    def _page():
        return {"ok": True}

    return app


class TestObscure405:
    def test_api_405_becomes_401(self):
        """A 405 on an ``/api/*`` path is rewritten to a generic 401."""
        client = TestClient(_app_with_405(with_security_headers=False))
        resp = client.post("/api/thing")
        assert resp.status_code == 401

    def test_api_401_has_no_allow_header(self):
        """The ``Allow`` header that advertises accepted methods is dropped
        so the method surface is no longer readable pre-auth."""
        client = TestClient(_app_with_405(with_security_headers=False))
        resp = client.post("/api/thing")
        assert "allow" not in {k.lower() for k in resp.headers}

    def test_non_api_405_is_untouched(self):
        """HTML pages may legitimately return 405 — only ``/api/*`` is
        normalised."""
        client = TestClient(_app_with_405(with_security_headers=False))
        resp = client.post("/page")
        assert resp.status_code == 405

    def test_security_headers_reapplied_to_obscured_401_when_wrapped(self):
        """L1 wiring: obscure_405 builds a fresh Response and drops all
        inner headers. With SecurityHeadersMiddleware wrapping it (the
        production order) the security headers are re-applied to the 401
        replacement via ``setdefault``.
        """
        client = TestClient(_app_with_405(with_security_headers=True))
        resp = client.post("/api/thing")
        assert resp.status_code == 401
        # The outer SecurityHeaders middleware re-applied its static
        # headers to the rebuilt 401 response.
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy" in resp.headers
