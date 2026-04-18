"""Tests for the SecurityHeadersMiddleware.

These tests exercise the middleware against a minimal FastAPI app rather
than the full mediaman app, because the mediaman app requires a DB and
config at startup. The middleware itself is framework-agnostic, so the
minimal app is a faithful substitute.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.web import (
    SecurityHeadersMiddleware,
    register_security_middleware,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    register_security_middleware(app)

    @app.get("/ping")
    def _ping() -> dict:
        return {"ok": True}

    return app


class TestSecurityHeadersMiddleware:
    def test_always_on_headers_present(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert resp.status_code == 200
        # The five mandatory always-on headers.
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert resp.headers["Permissions-Policy"] == "interest-cohort=()"
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp
        assert "frame-src https://www.youtube.com" in csp

    def test_hsts_absent_on_http(self):
        """HSTS must NOT be set on plain HTTP — otherwise browsers refuse
        to talk to the dev server at all."""
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_present_on_https(self):
        """When served over HTTPS (or behind a TLS-terminating proxy that
        forwards X-Forwarded-Proto), HSTS must be attached."""
        client = TestClient(_build_app(), base_url="https://testserver")
        resp = client.get("/ping")
        assert (
            resp.headers["Strict-Transport-Security"]
            == "max-age=63072000; includeSubDomains"
        )

    def test_headers_do_not_override_handler_values(self):
        """A route that intentionally sets one of the headers must win —
        the middleware uses ``setdefault`` so handlers can opt out."""
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/custom")
        def _custom():
            from starlette.responses import JSONResponse

            resp = JSONResponse({"ok": True})
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
            return resp

        client = TestClient(app)
        resp = client.get("/custom")
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
        # Unrelated headers still get applied.
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_middleware_can_be_added_directly(self):
        """``SecurityHeadersMiddleware`` can be used via ``add_middleware``
        without the helper — smoke test the public class path."""
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/")
        def _root():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/")
        assert resp.headers["X-Frame-Options"] == "DENY"
