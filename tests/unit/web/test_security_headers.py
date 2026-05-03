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
    BodySizeLimitMiddleware,
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

    def test_hsts_off_by_default(self, monkeypatch):
        """HSTS is now opt-in.  Without ``MEDIAMAN_HSTS_ENABLED=true`` the
        header MUST NOT be emitted, even on a plain HTTP request — a
        misconfigured plaintext deploy that ships HSTS would lock real
        users out of the origin for two years."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_HSTS_ENABLED", raising=False)
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_off_on_https_when_not_enabled(self, monkeypatch):
        """Even on HTTPS, HSTS stays off until the operator opts in."""
        monkeypatch.delenv("MEDIAMAN_FORCE_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_HSTS_ENABLED", raising=False)
        client = TestClient(_build_app(), base_url="https://testserver")
        resp = client.get("/ping")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_off_on_http_even_when_enabled(self, monkeypatch):
        """``MEDIAMAN_HSTS_ENABLED=true`` is necessary but not sufficient.
        The request must also be HTTPS — emitting HSTS over plaintext is
        the misconfiguration that the gate exists to prevent."""
        monkeypatch.setenv("MEDIAMAN_HSTS_ENABLED", "true")
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_emitted_when_enabled_and_https(self, monkeypatch):
        """Both conditions met: env var on AND request is HTTPS."""
        monkeypatch.setenv("MEDIAMAN_HSTS_ENABLED", "true")
        client = TestClient(_build_app(), base_url="https://testserver")
        resp = client.get("/ping")
        assert "max-age=63072000" in resp.headers["Strict-Transport-Security"]

    def test_hsts_force_secure_cookies_false_still_disables(self, monkeypatch):
        """Legacy ``MEDIAMAN_FORCE_SECURE_COOKIES=false`` continues to
        suppress HSTS so an operator with the old toggle doesn't get a
        surprise upgrade."""
        monkeypatch.setenv("MEDIAMAN_HSTS_ENABLED", "true")
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "false")
        client = TestClient(_build_app(), base_url="https://testserver")
        resp = client.get("/ping")
        assert "Strict-Transport-Security" not in resp.headers

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
    """Per-request CSP nonce — script-src, style-src, and style-src-attr.

    A 16-byte base64url nonce is minted for every request, exposed on
    ``request.state.csp_nonce`` for templates and route handlers, and
    woven into both ``script-src`` and ``style-src`` (for ``<script>``
    and ``<style>`` blocks respectively).

    Inline ``style="..."`` ATTRIBUTES are gated by a separate
    ``style-src-attr`` directive that uses ``'unsafe-inline'`` without a
    nonce. Chromium blocks inline-style attributes when ``style-src``
    contains a nonce, even with ``'unsafe-inline'`` listed alongside;
    splitting the directive avoids that pitfall while keeping the
    block-level lockdown.
    """

    def test_csp_header_contains_script_nonce(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        csp = resp.headers["Content-Security-Policy"]
        assert "script-src 'self' 'nonce-" in csp
        # 'unsafe-inline' was dropped from script-src after Wave 7 — a
        # stored XSS in a Jinja |safe interpolation can no longer execute
        # script content.
        assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]

    def test_csp_header_contains_style_nonce(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        csp = resp.headers["Content-Security-Policy"]
        # The ``<style>`` block source list is locked down to nonce only
        # — the previous ``'unsafe-inline'`` fallback was redundant
        # under CSP3 (the nonce overrides it for blocks anyway).
        # ``style-src`` directive comes first; ``style-src-attr`` adds
        # the attribute escape hatch separately.
        assert "style-src 'self' 'nonce-" in csp
        style_src_segment = csp.split("style-src ")[1].split(";")[0]
        assert "'unsafe-inline'" not in style_src_segment

    def test_csp_header_allows_inline_style_attributes_separately(self):
        """``style-src-attr 'unsafe-inline'`` keeps inline ``style=""``
        attributes working without re-opening ``<style>`` blocks.

        Reported: every modal that hides via ``style="display:none"``
        rendered visible-by-default after the directive split because
        Chromium honoured the nonce-overrides-unsafe-inline rule for
        attributes too.
        """
        client = TestClient(_build_app())
        resp = client.get("/ping")
        csp = resp.headers["Content-Security-Policy"]
        assert "style-src-attr 'unsafe-inline'" in csp

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
        assert isinstance(body_nonce, str)
        assert len(body_nonce) >= 16
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


class TestTrustedHostMiddleware:
    """``MEDIAMAN_ALLOWED_HOSTS`` pins the set of acceptable Host headers.

    An attacker who can set arbitrary ``Host:`` (e.g. by getting the
    target to visit ``http://attacker.example`` that proxies to the
    real origin) can poison anything we derive from ``request.url`` —
    CSRF host comparisons, cookie domains, mailgun-templated absolute
    URLs.  ``TrustedHostMiddleware`` rejects unknown hosts at the door
    so the request never reaches the rest of the middleware stack.
    """

    def test_default_wildcard_accepts_anything(self, monkeypatch):
        """Backward-compat: when the env var is unset the app keeps
        accepting any Host (with a startup warning logged)."""
        monkeypatch.delenv("MEDIAMAN_ALLOWED_HOSTS", raising=False)
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/ping")
        def _ping():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/ping", headers={"Host": "anything.example"})
        assert resp.status_code == 200

    def test_pinned_host_accepts_match(self, monkeypatch):
        """A request whose Host matches the allowlist is accepted."""
        monkeypatch.setenv("MEDIAMAN_ALLOWED_HOSTS", "mediaman.example.com")
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/ping")
        def _ping():
            return {"ok": True}

        client = TestClient(app, base_url="http://mediaman.example.com")
        resp = client.get("/ping")
        assert resp.status_code == 200

    def test_pinned_host_rejects_mismatch(self, monkeypatch):
        """A request whose Host is outside the allowlist is rejected."""
        monkeypatch.setenv("MEDIAMAN_ALLOWED_HOSTS", "mediaman.example.com")
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/ping")
        def _ping():
            return {"ok": True}

        client = TestClient(app, base_url="http://attacker.example")
        resp = client.get("/ping")
        assert resp.status_code == 400

    def test_multiple_hosts_accepted(self, monkeypatch):
        """Comma-separated entries are all accepted."""
        monkeypatch.setenv("MEDIAMAN_ALLOWED_HOSTS", "mediaman.example.com, alt.example.com ")
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/ping")
        def _ping():
            return {"ok": True}

        for host in ("mediaman.example.com", "alt.example.com"):
            client = TestClient(app, base_url=f"http://{host}")
            resp = client.get("/ping")
            assert resp.status_code == 200, host

    def test_startup_warning_logged_when_unconfigured(self, monkeypatch, caplog):
        """Operators who forget to pin a hostname get a logged warning
        on app build — silent wildcard acceptance is the bug we're
        avoiding."""
        import logging

        monkeypatch.delenv("MEDIAMAN_ALLOWED_HOSTS", raising=False)
        with caplog.at_level(logging.WARNING, logger="mediaman.web"):
            app = FastAPI()
            register_security_middleware(app)
        # ``register_security_middleware`` should have logged a warning.
        warned = [r for r in caplog.records if "MEDIAMAN_ALLOWED_HOSTS" in r.getMessage()]
        assert warned, "expected an MEDIAMAN_ALLOWED_HOSTS startup warning"


class TestBodySizeLimitMiddleware:
    """Cap total request body bytes to prevent OOM on a giant POST.

    The middleware is wired in by ``register_security_middleware`` and
    enforces a default of 8 MiB; operators can override the cap with
    ``MEDIAMAN_MAX_REQUEST_BYTES``.
    """

    def test_small_body_accepted(self):
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=1024)

        # Use a Starlette route directly to avoid FastAPI's parameter
        # introspection from intercepting the ``request`` argument.
        async def handler(req):
            from starlette.responses import JSONResponse

            data = await req.body()
            return JSONResponse({"len": len(data)})

        app.router.add_route("/echo", handler, methods=["POST"])

        client = TestClient(app)
        resp = client.post("/echo", content=b"x" * 100)
        assert resp.status_code == 200
        assert resp.json() == {"len": 100}

    def test_oversize_body_returns_413(self):
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=128)

        async def handler(req):
            from starlette.responses import JSONResponse

            data = await req.body()
            return JSONResponse({"len": len(data)})

        app.router.add_route("/echo", handler, methods=["POST"])

        client = TestClient(app)
        resp = client.post("/echo", content=b"x" * 1024)
        assert resp.status_code == 413
        assert b"Payload too large" in resp.content

    def test_content_length_over_cap_short_circuits(self):
        """An oversize Content-Length header is rejected before any
        body bytes are read.  We don't have an easy way to assert the
        handler was never called from the test client, but we can at
        least confirm the response is 413."""
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=64)

        async def handler(req):  # pragma: no cover — should not run
            from starlette.responses import JSONResponse

            data = await req.body()
            return JSONResponse({"len": len(data)})

        app.router.add_route("/echo", handler, methods=["POST"])

        client = TestClient(app)
        resp = client.post(
            "/echo",
            content=b"x" * 256,
            headers={"Content-Length": "256"},
        )
        assert resp.status_code == 413

    def test_env_var_override(self, monkeypatch):
        """``MEDIAMAN_MAX_REQUEST_BYTES`` overrides the default."""
        monkeypatch.setenv("MEDIAMAN_MAX_REQUEST_BYTES", "32")
        app = FastAPI()
        # ``max_bytes=None`` means "read env on first request".
        app.add_middleware(BodySizeLimitMiddleware)

        async def handler(req):
            from starlette.responses import JSONResponse

            data = await req.body()
            return JSONResponse({"len": len(data)})

        app.router.add_route("/echo", handler, methods=["POST"])

        client = TestClient(app)
        resp = client.post("/echo", content=b"x" * 64)
        assert resp.status_code == 413

    def test_default_via_register_security_middleware(self):
        """The helper wires the body-size middleware in with the default
        cap (8 MiB), so a 9 MiB POST is rejected against a freshly built
        app."""
        app = FastAPI()
        register_security_middleware(app)

        async def handler(req):  # pragma: no cover — should not run
            from starlette.responses import JSONResponse

            data = await req.body()
            return JSONResponse({"len": len(data)})

        app.router.add_route("/echo", handler, methods=["POST"])

        client = TestClient(app)
        big = b"x" * (9 * 1024 * 1024)
        resp = client.post("/echo", content=big)
        assert resp.status_code == 413

    def test_zero_or_negative_cap_disables_limit(self, monkeypatch):
        """``MEDIAMAN_MAX_REQUEST_BYTES=0`` is treated as 'unlimited'.
        A negative value falls back to the default (with a warning)."""
        monkeypatch.setenv("MEDIAMAN_MAX_REQUEST_BYTES", "0")
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware)

        async def handler(req):
            from starlette.responses import JSONResponse

            data = await req.body()
            return JSONResponse({"len": len(data)})

        app.router.add_route("/echo", handler, methods=["POST"])

        client = TestClient(app)
        resp = client.post("/echo", content=b"x" * 65536)
        assert resp.status_code == 200

    def test_unparseable_env_falls_back_to_default(self, monkeypatch):
        """An unparseable ``MEDIAMAN_MAX_REQUEST_BYTES`` reverts to the
        8 MiB default — it must NOT silently disable the cap."""
        monkeypatch.setenv("MEDIAMAN_MAX_REQUEST_BYTES", "not-a-number")
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware)

        async def handler(req):  # pragma: no cover — should not run
            from starlette.responses import JSONResponse

            data = await req.body()
            return JSONResponse({"len": len(data)})

        app.router.add_route("/echo", handler, methods=["POST"])

        client = TestClient(app)
        big = b"x" * (9 * 1024 * 1024)
        resp = client.post("/echo", content=big)
        assert resp.status_code == 413


class TestNormaliseOrigin:
    """``_normalise_origin`` parses Origin/Referer/netloc strings into
    ``(scheme, host[:port])`` tuples for direct equality comparison.

    Two correctness fixes vs. the old prefix-strip logic:

    1. IPv6 hosts must round-trip cleanly — ``[2001:db8::1]:443`` →
       ``("https", "2001:db8::1")``, not ``"[2001:db8"``.
    2. Non-default ports must survive — ``example.com:8443`` stays
       ``"example.com:8443"`` so an Origin on a different port is
       not silently treated as same-origin.
    """

    def test_ipv6_origin_with_default_port(self):
        from mediaman.web import _normalise_origin

        scheme, host = _normalise_origin("https://[2001:db8::1]:443")
        assert scheme == "https"
        assert host == "2001:db8::1"

    def test_ipv6_origin_with_custom_port(self):
        from mediaman.web import _normalise_origin

        scheme, host = _normalise_origin("http://[::1]:8080")
        assert scheme == "http"
        # Bracketed re-stitch keeps host:port unambiguous.
        assert host == "[::1]:8080"

    def test_https_default_port_stripped(self):
        from mediaman.web import _normalise_origin

        scheme, host = _normalise_origin("https://example.com:443")
        assert (scheme, host) == ("https", "example.com")

    def test_http_default_port_stripped(self):
        from mediaman.web import _normalise_origin

        scheme, host = _normalise_origin("http://example.com:80")
        assert (scheme, host) == ("http", "example.com")

    def test_non_default_port_preserved(self):
        from mediaman.web import _normalise_origin

        # Previously ``endswith(":443")`` failed and the host survived
        # as ``"example.com:8443"`` — that's correct here, AND it must
        # be different from ``"example.com"`` so a request on :8443
        # rejects an Origin on the bare host.
        scheme, host = _normalise_origin("https://example.com:8443")
        assert (scheme, host) == ("https", "example.com:8443")

    def test_lowercases_scheme_and_host(self):
        from mediaman.web import _normalise_origin

        scheme, host = _normalise_origin("HTTPS://EXAMPLE.COM")
        assert (scheme, host) == ("https", "example.com")

    def test_bare_netloc_uses_default_scheme(self):
        from mediaman.web import _normalise_origin

        scheme, host = _normalise_origin("example.com:443", default_scheme="https")
        assert (scheme, host) == ("https", "example.com")

    def test_normalise_host_drops_default_port(self):
        """Backward-compat shim: ``_normalise_host`` returns just the
        host portion (still used by tests / older code paths)."""
        from mediaman.web import _normalise_host

        assert _normalise_host("example.com:443") == "example.com"
        assert _normalise_host("example.com") == "example.com"
        # IPv6 round-trip: previously this returned ``"[2001:db8"``.
        assert _normalise_host("[2001:db8::1]:443") == "2001:db8::1"


class TestCSRFHostOnly:
    """CSRF compares hosts only — see ``CSRFOriginMiddleware`` docstring.

    The Wave 5-1 attempt to compare ``(scheme, host)`` (finding 11)
    broke real reverse-proxy deployments where uvicorn sees
    ``request.url.scheme == "http"`` but the browser is on ``https://``
    and sets ``Origin: https://...``.  The cross-scheme attack the
    harden was guarding against is already closed by ``Secure`` cookie
    flag (browser refuses to send the cookie over HTTP) and
    ``SameSite=Strict`` on the session cookie.
    """

    def test_cross_scheme_same_host_accepted(self):
        """A reverse-proxy deployment where uvicorn sees ``http`` but
        the browser submits ``Origin: https://example.com`` is
        accepted as long as the host matches.  This is exactly the
        scenario where the previous strict-scheme check produced a
        spurious 403 on every login."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app, base_url="http://example.com")
        resp = client.post(
            "/api/thing",
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200

    def test_same_scheme_same_host_accepted(self):
        """The trivial case continues to work."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app, base_url="https://example.com")
        resp = client.post(
            "/api/thing",
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200

    def test_cross_host_rejected(self):
        """Different host is still rejected — that's the substantive
        CSRF check that wasn't reverted."""
        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/thing")
        def _endpoint():
            return {"ok": True}

        client = TestClient(app, base_url="https://example.com")
        resp = client.post(
            "/api/thing",
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403


class TestForcePasswordChangeHealthExempt:
    """``/healthz`` and ``/readyz`` are exempt from the
    ForcePasswordChangeMiddleware.  Probes never carry sessions, but
    a stale browser cookie sharing the same origin used to redirect
    the probe away from a 200 response.  Adding the probe paths to
    the allowlist closes that gap."""

    def test_healthz_skipped_with_session(self):
        """Even with a flagged session_token cookie present, /healthz
        passes straight through to the handler."""
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/healthz")
        def _healthz():
            return {"status": "ok"}

        client = TestClient(app)
        client.cookies.set("session_token", "any-stale-cookie")
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_readyz_skipped_with_session(self):
        app = FastAPI()
        register_security_middleware(app)

        @app.get("/readyz")
        def _readyz():
            return {"status": "ready"}

        client = TestClient(app)
        client.cookies.set("session_token", "any-stale-cookie")
        resp = client.get("/readyz")
        assert resp.status_code == 200
