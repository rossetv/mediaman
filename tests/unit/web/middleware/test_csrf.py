"""Comprehensive tests for :mod:`mediaman.web.middleware.csrf`.

Covers the full CSRF defence surface of :class:`CSRFOriginMiddleware`:

- Exempt-routes allowlist (which paths bypass the Origin/Referer check).
- Origin header checks: matches, mismatch, missing, malformed.
- Referer header fallback: used when Origin is absent.
- HTTP method handling: only mutating methods are enforced; GET / HEAD /
  OPTIONS bypass the check entirely.
- Edge cases: session cookie present without Origin, no cookie without
  Origin, empty Origin value.

:mod:`tests.unit.web.test_security_headers` already has brief smoke
tests wired through ``register_security_middleware``.  The tests here go
deeper — they target the middleware class directly so they are insulated
from the middleware stack ordering and can exercise the internal logic
precisely without the ambient security headers or body-size caps.

Tests that need a real DB (e.g. authed-client session creation) use the
``conn`` fixture; pure middleware logic tests do not need DB access.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.web.middleware.csrf import CSRFOriginMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(*, post_path: str = "/api/thing", extra_routes: dict | None = None) -> FastAPI:
    """Return a minimal FastAPI app with *only* the CSRF middleware attached.

    Using the middleware directly (rather than ``register_security_middleware``)
    keeps each test focused on the CSRF logic and prevents the SecurityHeaders
    middleware from rewriting response bodies or injecting nonces that would
    obscure the test intent.
    """
    app = FastAPI()
    app.add_middleware(CSRFOriginMiddleware)

    @app.post(post_path)
    def _post():
        return {"ok": True}

    @app.get(post_path)
    def _get():
        return {"ok": True}

    @app.head(post_path)
    def _head():
        return {"ok": True}

    @app.put(post_path)
    def _put():
        return {"ok": True}

    @app.patch(post_path)
    def _patch():
        return {"ok": True}

    @app.delete(post_path)
    def _delete():
        return {"ok": True}

    if extra_routes:
        for path, fn in extra_routes.items():
            app.add_api_route(path, fn, methods=["POST"])

    return app


# ---------------------------------------------------------------------------
# HTTP method gating: safe methods bypass the CSRF check entirely
# ---------------------------------------------------------------------------


class TestSafeMethodsBypass:
    """GET, HEAD, and OPTIONS are never state-changing; the middleware must
    pass them through without examining Origin or Referer."""

    def test_get_with_cross_origin_header_allowed(self):
        """GET is safe — cross-origin Origin header must not produce a 403."""
        client = TestClient(_make_app())
        resp = client.get("/api/thing", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 200

    def test_head_with_cross_origin_header_allowed(self):
        """HEAD is safe — cross-origin Origin header must not produce a 403."""
        client = TestClient(_make_app())
        resp = client.head("/api/thing", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 200

    def test_get_without_any_header_allowed(self):
        """GET without Origin or Referer is always safe."""
        client = TestClient(_make_app())
        resp = client.get("/api/thing")
        assert resp.status_code == 200

    def test_get_with_session_cookie_and_cross_origin_allowed(self):
        """The session-cookie guard only applies to mutating methods; GET
        with a session cookie and a cross-origin Origin must still pass."""
        client = TestClient(_make_app())
        client.cookies.set("session_token", "some-token")
        resp = client.get("/api/thing", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Mutating methods: POST / PUT / PATCH / DELETE all enforced
# ---------------------------------------------------------------------------


class TestMutatingMethodsEnforced:
    """All four mutating methods must be subject to the Origin/Referer check.

    The existing smoke tests focus on POST; this class pins PUT, PATCH,
    and DELETE so a future refactor that accidentally drops one of them
    from ``_CSRF_PROTECTED_METHODS`` is caught.
    """

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_cross_origin_rejected_for_mutating_method(self, method: str):
        """A cross-origin Origin header on any mutating method must return 403."""
        client = TestClient(_make_app())
        resp = client.request(
            method,
            "/api/thing",
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_same_origin_accepted_for_mutating_method(self, method: str):
        """A same-origin Origin header on any mutating method must pass."""
        client = TestClient(_make_app(), base_url="http://testserver")
        resp = client.request(
            method,
            "/api/thing",
            headers={"Origin": "http://testserver"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Origin header checks
# ---------------------------------------------------------------------------


class TestOriginHeaderChecks:
    """Direct tests of the Origin header comparison logic."""

    def test_matching_origin_allowed(self):
        """``Origin: http://testserver`` on a request to ``testserver`` passes."""
        client = TestClient(_make_app())
        resp = client.post("/api/thing", headers={"Origin": "http://testserver"})
        assert resp.status_code == 200

    def test_mismatched_origin_rejected(self):
        """``Origin`` from a different host must return 403."""
        client = TestClient(_make_app())
        resp = client.post("/api/thing", headers={"Origin": "https://attacker.example.com"})
        assert resp.status_code == 403

    def test_origin_subdomain_of_server_rejected(self):
        """A subdomain of the target host is not same-origin and must be rejected."""
        client = TestClient(_make_app(), base_url="http://example.com")
        resp = client.post(
            "/api/thing",
            headers={"Origin": "http://sub.example.com"},
        )
        assert resp.status_code == 403

    def test_origin_with_default_http_port_matches_bare_host(self):
        """``Origin: http://example.com:80`` is normalised to ``example.com``
        — identical to ``http://example.com`` — and therefore accepted."""
        client = TestClient(_make_app(), base_url="http://example.com")
        resp = client.post(
            "/api/thing",
            headers={"Origin": "http://example.com:80"},
        )
        assert resp.status_code == 200

    def test_origin_with_non_default_port_preserved_and_checked(self):
        """``Origin: http://example.com:8080`` is a different host than
        ``example.com`` and must be rejected when the server is on
        the bare hostname (port 80)."""
        client = TestClient(_make_app(), base_url="http://example.com")
        resp = client.post(
            "/api/thing",
            headers={"Origin": "http://example.com:8080"},
        )
        assert resp.status_code == 403

    def test_malformed_origin_rejected(self):
        """A syntactically malformed Origin header cannot match any host and
        must be treated as a mismatch (fail-closed)."""
        client = TestClient(_make_app())
        # An Origin that looks like a URL but has an invalid port component
        # will cause urlsplit to raise ValueError inside ``_host_of``; the
        # middleware catches that and returns ``""`` (no match → 403).
        resp = client.post(
            "/api/thing",
            headers={"Origin": "http://[badipv6"},
        )
        assert resp.status_code == 403

    def test_empty_origin_treated_as_missing(self):
        """An empty Origin header value is treated the same as a missing
        Origin: no cookie → non-browser client → allowed through."""
        client = TestClient(_make_app())
        resp = client.post("/api/thing", headers={"Origin": ""})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Referer header fallback (used when Origin is absent)
# ---------------------------------------------------------------------------


class TestRefererHeaderFallback:
    """When no ``Origin`` header is present the middleware falls back to
    ``Referer``.  The Referer is a full URL; the middleware extracts the
    host component and compares it the same way it compares Origin."""

    def test_matching_referer_allowed_when_no_origin(self):
        """A same-host Referer URL must be accepted as the CSRF proof."""
        client = TestClient(_make_app())
        resp = client.post(
            "/api/thing",
            headers={"Referer": "http://testserver/some/page"},
        )
        assert resp.status_code == 200

    def test_cross_origin_referer_rejected_when_no_origin(self):
        """A Referer pointing to a different host must be rejected."""
        client = TestClient(_make_app())
        resp = client.post(
            "/api/thing",
            headers={"Referer": "https://attacker.example.com/page"},
        )
        assert resp.status_code == 403

    def test_origin_takes_precedence_over_referer(self):
        """When both Origin and Referer are present, Origin is authoritative.
        A mismatched Origin must produce 403 even when Referer would pass."""
        client = TestClient(_make_app())
        resp = client.post(
            "/api/thing",
            headers={
                "Origin": "https://attacker.example.com",
                "Referer": "http://testserver/page",
            },
        )
        assert resp.status_code == 403

    def test_matching_origin_wins_even_if_referer_mismatches(self):
        """Origin passes and Referer is irrelevant — must be accepted."""
        client = TestClient(_make_app())
        resp = client.post(
            "/api/thing",
            headers={
                "Origin": "http://testserver",
                "Referer": "https://evil.example.com/page",
            },
        )
        assert resp.status_code == 200

    def test_malformed_referer_rejected(self):
        """A syntactically invalid Referer falls back to an empty host
        (fail-closed) and must produce a 403."""
        client = TestClient(_make_app())
        resp = client.post(
            "/api/thing",
            headers={"Referer": "http://[badipv6/page"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Missing Origin / Referer — non-browser vs. cookie-carrying
# ---------------------------------------------------------------------------


class TestMissingOriginBehaviour:
    """The middleware distinguishes between two missing-header cases:

    * No Origin / Referer AND no session cookie → non-browser API client
      (curl, scripts). These callers have no CSRF exposure. Allow.
    * No Origin / Referer BUT session cookie present → ambiguous browser
      request. Reject to prevent a browser that drops both headers from
      carrying a victim cookie across a CSRF form.
    """

    def test_no_origin_no_cookie_allowed(self):
        """API client without a session cookie is allowed through."""
        client = TestClient(_make_app())
        resp = client.post("/api/thing")
        assert resp.status_code == 200

    def test_no_origin_with_session_cookie_rejected(self):
        """Session cookie present but no Origin/Referer must return 403."""
        client = TestClient(_make_app())
        client.cookies.set("session_token", "some-session-token")
        resp = client.post("/api/thing")
        assert resp.status_code == 403

    def test_session_cookie_with_matching_origin_allowed(self):
        """Session cookie + correct Origin header → passes the CSRF check."""
        client = TestClient(_make_app())
        client.cookies.set("session_token", "some-session-token")
        resp = client.post("/api/thing", headers={"Origin": "http://testserver"})
        assert resp.status_code == 200

    def test_session_cookie_with_mismatching_origin_rejected(self):
        """Session cookie + wrong Origin → must be rejected."""
        client = TestClient(_make_app())
        client.cookies.set("session_token", "some-session-token")
        resp = client.post(
            "/api/thing",
            headers={"Origin": "https://attacker.example.com"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Exempt-routes allowlist
# ---------------------------------------------------------------------------


class TestExemptRoutesAllowlist:
    """The explicit (method, path-pattern) allowlist bypasses the Origin check.

    These routes are HMAC-token-authenticated; they legitimately arrive
    from webmail clients whose Origin is the user's webmail host.

    The exact patterns are:
      POST /download/{token}   — single-segment token only
      POST /keep/{token}       — single-segment token only
      POST /unsubscribe        — exact path match
    """

    def _app_with_exempt_routes(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(CSRFOriginMiddleware)

        @app.post("/download/{token}")
        def _download(token: str):
            return {"ok": True}

        @app.post("/keep/{token}")
        def _keep(token: str):
            return {"ok": True}

        @app.post("/unsubscribe")
        def _unsubscribe():
            return {"ok": True}

        @app.post("/download/{token}/extra")
        def _download_extra(token: str):
            return {"ok": True}

        @app.post("/keep/{token}/forever")
        def _keep_forever(token: str):
            return {"ok": True}

        @app.post("/unsubscribe/all")
        def _unsubscribe_all():
            return {"ok": True}

        @app.post("/api/thing")
        def _api():
            return {"ok": True}

        return app

    def test_post_download_token_exempt_from_cross_origin(self):
        """POST /download/{token} is on the allowlist — cross-origin Origin accepted."""
        client = TestClient(self._app_with_exempt_routes())
        resp = client.post("/download/abc123", headers={"Origin": "https://mail.example.com"})
        assert resp.status_code == 200

    def test_post_keep_token_exempt_from_cross_origin(self):
        """POST /keep/{token} is on the allowlist — cross-origin Origin accepted."""
        client = TestClient(self._app_with_exempt_routes())
        resp = client.post("/keep/abc123", headers={"Origin": "https://mail.example.com"})
        assert resp.status_code == 200

    def test_post_unsubscribe_exact_path_exempt(self):
        """POST /unsubscribe (exact) is on the allowlist — cross-origin accepted."""
        client = TestClient(self._app_with_exempt_routes())
        resp = client.post("/unsubscribe", headers={"Origin": "https://mail.example.com"})
        assert resp.status_code == 200

    def test_download_nested_path_not_exempt(self):
        """POST /download/{token}/extra has two segments after the prefix —
        it does NOT inherit the exemption and must be CSRF-checked."""
        client = TestClient(self._app_with_exempt_routes())
        resp = client.post("/download/abc/extra", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403

    def test_keep_nested_path_not_exempt(self):
        """POST /keep/{token}/forever has two segments — not exempt."""
        client = TestClient(self._app_with_exempt_routes())
        resp = client.post("/keep/abc/forever", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403

    def test_unsubscribe_sub_path_not_exempt(self):
        """POST /unsubscribe/all is not the exact /unsubscribe path — not exempt."""
        client = TestClient(self._app_with_exempt_routes())
        resp = client.post("/unsubscribe/all", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403

    def test_exempt_route_with_session_cookie_and_no_origin_allowed(self):
        """Exempt routes bypass the entire CSRF check, including the
        session-cookie + no-Origin guard."""
        client = TestClient(self._app_with_exempt_routes())
        client.cookies.set("session_token", "some-token")
        resp = client.post("/keep/mytoken")
        assert resp.status_code == 200

    def test_non_exempt_api_route_checked_normally(self):
        """An ordinary API route that is not on the allowlist must still be
        checked — ensures the allowlist is not inadvertently too broad."""
        client = TestClient(self._app_with_exempt_routes())
        resp = client.post("/api/thing", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 403

    def test_get_method_on_exempt_path_is_also_free(self):
        """Even without the exemption, GET would be safe — adding an exempt
        path does not accidentally break GET routes on the same prefix."""
        app = FastAPI()
        app.add_middleware(CSRFOriginMiddleware)

        @app.get("/keep/{token}")
        def _keep_get(token: str):
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/keep/abc", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Response content — error bodies carry the CSRF sentinel
# ---------------------------------------------------------------------------


class TestCSRFErrorResponseContent:
    """403 responses from the CSRF middleware must include the ``CSRF:`` prefix
    in the body so callers (and monitoring) can distinguish CSRF rejections
    from other 403 sources."""

    def test_origin_mismatch_body_contains_csrf_sentinel(self):
        client = TestClient(_make_app())
        resp = client.post("/api/thing", headers={"Origin": "https://attacker.example.com"})
        assert resp.status_code == 403
        assert b"CSRF" in resp.content

    def test_referer_mismatch_body_contains_csrf_sentinel(self):
        client = TestClient(_make_app())
        resp = client.post(
            "/api/thing",
            headers={"Referer": "https://attacker.example.com/page"},
        )
        assert resp.status_code == 403
        assert b"CSRF" in resp.content

    def test_missing_origin_with_cookie_body_contains_csrf_sentinel(self):
        client = TestClient(_make_app())
        client.cookies.set("session_token", "some-token")
        resp = client.post("/api/thing")
        assert resp.status_code == 403
        assert b"CSRF" in resp.content
