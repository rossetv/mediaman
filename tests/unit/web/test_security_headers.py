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
        # The mandatory always-on headers.
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        pp = resp.headers["Permissions-Policy"]
        assert "interest-cohort=()" in pp
        assert "geolocation=()" in pp
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp
        assert "frame-src https://www.youtube.com" in csp
        # img-src allows any HTTPS source (Radarr/Sonarr/Plex return
        # posters from shifting CDNs we can't enumerate).
        assert "img-src 'self' data: blob: https:" in csp

    def test_hsts_emitted_by_default(self, monkeypatch):
        """Public-facing mediaman must emit HSTS unless the operator opts out."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert "Strict-Transport-Security" in resp.headers
        assert "max-age=63072000" in resp.headers["Strict-Transport-Security"]

    def test_hsts_can_be_disabled_for_dev(self, monkeypatch):
        """MEDIAMAN_FORCE_SECURE_COOKIES=false disables HSTS for local dev."""
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "false")
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_present_on_https(self, monkeypatch):
        """When served over HTTPS, HSTS is attached."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        client = TestClient(_build_app(), base_url="https://testserver")
        resp = client.get("/ping")
        assert (
            "max-age=63072000" in resp.headers["Strict-Transport-Security"]
        )

    def test_server_header_hidden(self):
        """Server banner is replaced with an opaque label."""
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert resp.headers.get("Server") == "mediaman"

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


class TestCSRFOriginMiddleware:
    """The CSRF middleware rejects cross-origin state-changing requests."""

    def test_same_origin_post_allowed(self):
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post(
            "/api/thing", headers={"Origin": "http://testserver"}
        )
        assert resp.status_code == 200

    def test_cross_origin_post_rejected(self):
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post(
            "/api/thing", headers={"Origin": "https://evil.example.com"}
        )
        assert resp.status_code == 403

    def test_non_browser_request_allowed(self):
        """Requests without Origin/Referer (curl, scripts) are allowed."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/api/thing")
        assert resp.status_code == 200

    def test_token_endpoints_exempt(self):
        """/keep/, /download/, /unsubscribe are exempt (they are token-authed)."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/keep/{token}")
        def _keep(token: str):
            return {"ok": True}

        client = TestClient(app)
        resp = client.post(
            "/keep/abc", headers={"Origin": "https://mail.example.com"}
        )
        assert resp.status_code == 200

    def test_get_not_checked(self):
        """GET is never state-changing — not checked."""
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/api/stats")
        def _stats():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get(
            "/api/stats", headers={"Origin": "https://evil.example.com"}
        )
        assert resp.status_code == 200
