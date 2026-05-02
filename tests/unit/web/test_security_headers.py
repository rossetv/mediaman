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
        assert "max-age=63072000" in resp.headers["Strict-Transport-Security"]

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
        resp = client.post("/api/thing", headers={"Origin": "http://testserver"})
        assert resp.status_code == 200

    def test_cross_origin_post_rejected(self):
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/api/thing", headers={"Origin": "https://evil.example.com"})
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
        resp = client.post("/keep/abc", headers={"Origin": "https://mail.example.com"})
        assert resp.status_code == 200

    def test_get_not_checked(self):
        """GET is never state-changing — not checked."""
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/api/stats")
        def _stats():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/api/stats", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 200


class TestCSRFExemptAllowlist:
    """Per-route CSRF exemption — allowlisted paths only.

    Previously the exemption was prefix-based: any new POST under
    ``/download/...``, ``/keep/...``, or ``/unsubscribe/...`` would
    inherit the exemption silently.  The fix is to enumerate the
    exact (method, path-pattern) pairs that bypass the Origin check,
    so a future POST under one of those prefixes that isn't on the
    list is still CSRF-checked.
    """

    def test_post_download_token_exempt(self):
        """POST /download/{token} — exempt (token-authenticated)."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/download/{token}")
        def _download(token: str):
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/download/abc", headers={"Origin": "https://mail.example.com"})
        assert resp.status_code == 200

    def test_post_keep_token_exempt(self):
        """POST /keep/{token} — exempt (token-authenticated)."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/keep/{token}")
        def _keep(token: str):
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/keep/abc", headers={"Origin": "https://mail.example.com"})
        assert resp.status_code == 200

    def test_post_unsubscribe_exact_exempt(self):
        """POST /unsubscribe — exempt (token-authenticated via form)."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/unsubscribe")
        def _unsubscribe():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/unsubscribe", headers={"Origin": "https://mail.example.com"})
        assert resp.status_code == 200

    def test_new_post_under_download_prefix_is_checked(self):
        """A NEW POST under /download/ that isn't on the exempt list
        does NOT silently inherit exemption — regression for the
        prefix-based design that had this sharp edge."""
        app = FastAPI()
        register_security_middleware(app)

        # Hypothetical future endpoint, not on the exempt list because
        # the exempt regex requires a single token segment.
        @app.post("/download/{token}/extra")
        def _new_download(token: str):
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/download/abc/extra", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403, (
            "New POST under /download/ silently inherited CSRF exemption — "
            "this is exactly the regression we are guarding against."
        )

    def test_new_post_under_keep_prefix_is_checked(self):
        """Mirror of the above for /keep/."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/keep/{token}/forever")
        def _new_keep(token: str):
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/keep/abc/forever", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403

    def test_new_post_under_unsubscribe_prefix_is_checked(self):
        """A POST to /unsubscribe/<anything> is no longer exempt — only
        the exact path /unsubscribe is on the allowlist."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/unsubscribe/all")
        def _unsubscribe_all():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/unsubscribe/all", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403


class TestCSPNonce:
    """Per-request CSP nonce — script-src and style-src.

    A 16-byte base64url nonce is minted for every request, exposed on
    ``request.state.csp_nonce`` for templates and route handlers, and
    woven into both ``script-src`` and ``style-src``.  The existing
    ``'unsafe-inline'`` is retained as a CSP2 fallback so untouched
    inline blocks keep rendering until they are migrated.
    """

    def test_csp_header_contains_script_nonce(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        csp = resp.headers["Content-Security-Policy"]
        assert "script-src 'self' 'nonce-" in csp
        # 'unsafe-inline' must still be present as the CSP2 fallback.
        assert "'unsafe-inline'" in csp.split("script-src")[1].split(";")[0]

    def test_csp_header_contains_style_nonce(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        csp = resp.headers["Content-Security-Policy"]
        assert "style-src 'self' 'nonce-" in csp
        assert "'unsafe-inline'" in csp.split("style-src")[1].split(";")[0]

    def test_consecutive_requests_have_different_nonces(self):
        """Two consecutive requests must mint different nonces — a
        single fixed nonce would be no better than 'unsafe-inline'."""
        import re

        client = TestClient(_build_app())
        nonces: set[str] = set()
        for _ in range(3):
            resp = client.get("/ping")
            csp = resp.headers["Content-Security-Policy"]
            match = re.search(r"script-src [^;]*'nonce-([^']+)'", csp)
            assert match is not None, f"no nonce found in CSP: {csp!r}"
            nonces.add(match.group(1))
        assert len(nonces) == 3, f"nonce was reused across requests: {nonces!r}"

    def test_request_state_csp_nonce_accessible_in_handler(self):
        """A route handler can read the per-request nonce via
        ``request.state.csp_nonce`` and emit it in the body — this is
        the path Jinja templates use to attach ``nonce="..."`` to
        inline blocks.

        The handler is registered as a plain Starlette route so that
        FastAPI's parameter introspection (which sees stringified
        annotations under ``from __future__ import annotations`` and
        falls back to query-binding) doesn't intercept the ``request``
        argument.
        """
        from starlette.responses import JSONResponse

        app = FastAPI()
        register_security_middleware(app)

        async def _echo(req):
            return JSONResponse({"nonce": req.state.csp_nonce})

        app.router.add_route("/echo-nonce", _echo, methods=["GET"])

        client = TestClient(app)
        resp = client.get("/echo-nonce")
        body = resp.json()
        assert resp.status_code == 200, f"handler errored: {body!r}"
        body_nonce = body["nonce"]
        assert isinstance(body_nonce, str) and len(body_nonce) >= 16
        # The nonce surfaced to the handler must match the nonce in the
        # outbound CSP header — otherwise templates couldn't reliably
        # mark inline blocks for the browser to accept them.
        csp = resp.headers["Content-Security-Policy"]
        assert f"'nonce-{body_nonce}'" in csp

    def test_nonce_format_is_base64url(self):
        """``secrets.token_urlsafe(16)`` produces a 22-character
        base64url-encoded value — no padding, only A-Z / a-z / 0-9 /
        - / _.  Anything else would be a CSP-injection liability."""
        import re

        client = TestClient(_build_app())
        resp = client.get("/ping")
        csp = resp.headers["Content-Security-Policy"]
        match = re.search(r"'nonce-([^']+)'", csp)
        assert match is not None
        nonce = match.group(1)
        assert re.fullmatch(r"[A-Za-z0-9_-]+", nonce), (
            f"nonce contains characters that could break out of the CSP directive: {nonce!r}"
        )
        # 16 bytes of base64url is 22 characters with no padding.
        assert len(nonce) == 22
