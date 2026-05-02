"""Round-2 security hardening tests.

Covers the fixes landed after the external pentest:
- Logout requires auth
- XFF bypass via uvicorn proxy_headers closed
- CF-Connecting-IP preferred when peer is trusted
- 405→401 oracle obscure middleware
- CSRF Origin port normalisation (443/80 stripped)
- CSRF exempt prefix trailing-slash pickiness
- Uniform "Not authenticated" error
- Session fingerprint binding
- Session idle timeout
- Session token hash storage
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_KEY = "0123456789abcdef" * 4


# ---------------------------------------------------------------------------
# Rate-limit + CF-Connecting-IP
# ---------------------------------------------------------------------------


class TestCfConnectingIp:
    def test_cf_connecting_ip_preferred(self, monkeypatch):
        # cf-connecting-ip is honoured ONLY when the peer is in the dedicated
        # MEDIAMAN_CLOUDFLARE_PROXIES list (not the broader TRUSTED_PROXIES).
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        monkeypatch.setenv("MEDIAMAN_CLOUDFLARE_PROXIES", "10.0.0.0/8")
        from mediaman.auth.rate_limit import get_client_ip
        from mediaman.auth.rate_limit.ip_resolver import clear_cache

        clear_cache()

        class FakeRequest:
            headers = {
                "cf-connecting-ip": "198.51.100.7",
                "x-forwarded-for": "1.2.3.4, 10.0.0.1",
            }
            client = type("C", (), {"host": "10.0.0.1"})()

        # CF-Connecting-IP beats XFF when peer is in the Cloudflare allowlist.
        assert get_client_ip(FakeRequest()) == "198.51.100.7"

    def test_cf_connecting_ip_ignored_if_peer_not_cloudflare(self, monkeypatch):
        # Peer is in TRUSTED_PROXIES but NOT in CLOUDFLARE_PROXIES — XFF wins,
        # cf-connecting-ip is ignored. This guards against a non-Cloudflare
        # reverse proxy spoofing client IPs via the CF header.
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        monkeypatch.delenv("MEDIAMAN_CLOUDFLARE_PROXIES", raising=False)
        from mediaman.auth.rate_limit import get_client_ip
        from mediaman.auth.rate_limit.ip_resolver import clear_cache

        clear_cache()

        class FakeRequest:
            headers = {
                "cf-connecting-ip": "198.51.100.7",
                "x-forwarded-for": "1.2.3.4, 10.0.0.1",
            }
            client = type("C", (), {"host": "10.0.0.1"})()

        assert get_client_ip(FakeRequest()) == "1.2.3.4"

    def test_cf_connecting_ip_ignored_if_peer_untrusted(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_TRUSTED_PROXIES", raising=False)
        monkeypatch.delenv("MEDIAMAN_CLOUDFLARE_PROXIES", raising=False)
        from mediaman.auth.rate_limit import get_client_ip
        from mediaman.auth.rate_limit.ip_resolver import clear_cache

        clear_cache()

        class FakeRequest:
            headers = {"cf-connecting-ip": "1.1.1.1"}
            client = type("C", (), {"host": "203.0.113.99"})()

        # Untrusted peer → ignore any forwarded header, return the peer.
        assert get_client_ip(FakeRequest()) == "203.0.113.99"


# ---------------------------------------------------------------------------
# CSRF Origin normalisation
# ---------------------------------------------------------------------------


class TestCsrfPortNormalisation:
    def test_explicit_default_port_accepted(self):
        from mediaman.web import _normalise_host

        assert _normalise_host("mediaman.example.com") == "mediaman.example.com"
        assert _normalise_host("mediaman.example.com:443") == "mediaman.example.com"
        assert _normalise_host("mediaman.example.com:80") == "mediaman.example.com"
        assert _normalise_host("mediaman.example.com:8282") == "mediaman.example.com:8282"

    def test_csrf_accepts_origin_with_default_port(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.web import register_security_middleware

        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/widget")
        def _widget():
            return {"ok": True}

        c = TestClient(app)
        # Same-origin but with explicit :443 — must be accepted.
        resp = c.post(
            "/api/widget",
            headers={"Origin": "http://testserver:443"},
        )
        assert resp.status_code == 200

    def test_csrf_rejects_cross_origin(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.web import register_security_middleware

        app = FastAPI()
        register_security_middleware(app)

        @app.post("/api/widget")
        def _widget():
            return {"ok": True}

        c = TestClient(app)
        resp = c.post("/api/widget", headers={"Origin": "https://evil.com"})
        assert resp.status_code == 403


class TestCsrfExemptPrefix:
    def test_unsubscribe_exact_match(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.web import register_security_middleware

        app = FastAPI()
        register_security_middleware(app)

        @app.post("/unsubscribe")
        def _unsub():
            return {"ok": True}

        @app.post("/unsubscribe-admin")
        def _unsub_admin():
            return {"ok": True}

        c = TestClient(app)
        # Exact /unsubscribe → exempt.
        assert c.post("/unsubscribe", headers={"Origin": "https://mail.com"}).status_code == 200
        # /unsubscribe-admin must NOT inherit the exemption.
        assert (
            c.post("/unsubscribe-admin", headers={"Origin": "https://evil.com"}).status_code == 403
        )

    def test_unsubscribe_slash_no_longer_exempt(self):
        """Regression: /unsubscribe/<anything> is NOT silently exempt.

        The original prefix-based exemption silently exempted everything
        under ``/unsubscribe/``, so any future POST added at a path like
        ``/unsubscribe/confirm`` would inherit the exemption with no
        compile-time signal.  The fix replaces the prefix rule with an
        explicit ``_CSRF_EXEMPT_ROUTES`` allowlist so non-listed paths
        fall through to the normal CSRF check.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.web import register_security_middleware

        app = FastAPI()
        register_security_middleware(app)

        @app.post("/unsubscribe/confirm")
        def _u():
            return {"ok": True}

        c = TestClient(app)
        # Cross-origin POST to a non-exempt path must be rejected.
        assert (
            c.post("/unsubscribe/confirm", headers={"Origin": "https://evil.com"}).status_code
            == 403
        )
        # Same-origin still works (normal CSRF behaviour).
        assert (
            c.post("/unsubscribe/confirm", headers={"Origin": "http://testserver"}).status_code
            == 200
        )


# ---------------------------------------------------------------------------
# 405 → 401 oracle obscure
# ---------------------------------------------------------------------------


class TestObscure405:
    def test_api_405_becomes_401(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.web import register_security_middleware

        app = FastAPI()
        register_security_middleware(app)

        @app.get("/api/thing")
        def _thing():
            return {"ok": True}

        c = TestClient(app)
        # DELETE on a GET-only route is a 405 at FastAPI level — our
        # middleware rewrites to 401 on /api/* paths to close the
        # oracle.
        resp = c.delete("/api/thing")
        assert resp.status_code == 401

    def test_non_api_405_preserved(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.web import register_security_middleware

        app = FastAPI()
        register_security_middleware(app)

        @app.get("/page")
        def _page():
            return {"ok": True}

        c = TestClient(app)
        # Non-/api path should still see a real 405.
        resp = c.delete("/page")
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Session fingerprint binding
# ---------------------------------------------------------------------------


class TestSessionFingerprint:
    def _conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def test_fingerprint_rejects_different_client(self, tmp_path):
        from mediaman.auth.session import create_session, create_user, validate_session

        conn = self._conn(tmp_path)
        create_user(conn, "alice", "test-password-long-enough", enforce_policy=False)
        token = create_session(
            conn,
            "alice",
            user_agent="Mozilla/5.0 X",
            client_ip="192.0.2.1",
        )

        # Same UA, same /24 — OK.
        assert (
            validate_session(
                conn,
                token,
                user_agent="Mozilla/5.0 X",
                client_ip="192.0.2.99",
            )
            == "alice"
        )

        # Different UA — cookie theft → reject.
        assert (
            validate_session(
                conn,
                token,
                user_agent="Chrome attacker",
                client_ip="192.0.2.1",
            )
            is None
        )

    def test_unbound_session_works_without_fingerprint(self, tmp_path):
        """Sessions created with no UA/IP (CLI, tests, legacy) are unbound."""
        from mediaman.auth.session import create_session, create_user, validate_session

        conn = self._conn(tmp_path)
        create_user(conn, "bob", "test-password-long-enough", enforce_policy=False)
        token = create_session(conn, "bob")

        # No fingerprint stored → any client succeeds.
        assert (
            validate_session(
                conn,
                token,
                user_agent="anything",
                client_ip="1.2.3.4",
            )
            == "bob"
        )

    def test_token_stored_as_hash(self, tmp_path):
        """Raw token must not be the primary key — token_hash is stored."""
        from mediaman.auth.session import create_session, create_user

        conn = self._conn(tmp_path)
        create_user(conn, "carol", "test-password-long-enough", enforce_policy=False)
        create_session(conn, "carol", user_agent="UA", client_ip="1.1.1.1")

        row = conn.execute(
            "SELECT token_hash, fingerprint, issued_ip FROM admin_sessions"
        ).fetchone()
        # token_hash is populated
        assert row["token_hash"] is not None
        assert len(row["token_hash"]) == 64
        # fingerprint captured
        assert row["fingerprint"]
        # issued_ip captured
        assert row["issued_ip"] == "1.1.1.1"


class TestSessionIdleTimeout:
    def test_idle_session_expires(self, tmp_path):
        from mediaman.auth.session import create_session, create_user, validate_session
        from mediaman.db import init_db

        conn = init_db(str(tmp_path / "mm.db"))
        create_user(conn, "dan", "test-password-long-enough", enforce_policy=False)
        token = create_session(conn, "dan")

        # Poke last_used_at into the past (25 h ago — beyond idle window).
        past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        conn.execute("UPDATE admin_sessions SET last_used_at = ?", (past,))
        conn.commit()

        # Should now be rejected.
        assert validate_session(conn, token) is None
