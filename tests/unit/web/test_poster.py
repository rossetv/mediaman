"""Tests for poster proxy endpoint security."""

from unittest.mock import patch


class TestRatingKeyValidation:
    def test_rejects_path_traversal(self):
        from mediaman.web.routes.poster import _validate_rating_key
        assert _validate_rating_key("../../admin") is False
        assert _validate_rating_key("..%2F..%2Fidentity") is False
        assert _validate_rating_key("123/../../foo") is False

    def test_accepts_numeric_keys(self):
        from mediaman.web.routes.poster import _validate_rating_key
        assert _validate_rating_key("12345") is True
        assert _validate_rating_key("1") is True

    def test_rejects_non_numeric(self):
        from mediaman.web.routes.poster import _validate_rating_key
        assert _validate_rating_key("abc") is False
        assert _validate_rating_key("") is False
        assert _validate_rating_key("12a") is False


class TestPosterSignature:
    def test_sign_and_verify_roundtrip(self):
        from mediaman.web.routes.poster import (
            _verify_poster_signature,
            sign_poster_url,
        )
        url = sign_poster_url("12345", "test-secret-key-for-unit-tests-only")
        assert url.startswith("/api/poster/12345?sig=")
        sig = url.split("?sig=")[1]
        assert _verify_poster_signature(
            "12345", sig, "test-secret-key-for-unit-tests-only"
        )

    def test_verify_rejects_tampered_rating_key(self):
        from mediaman.web.routes.poster import (
            _verify_poster_signature,
            sign_poster_url,
        )
        url = sign_poster_url("12345", "test-secret-key-for-unit-tests-only")
        sig = url.split("?sig=")[1]
        # Signature was for "12345" — swapping the rating key must fail.
        assert not _verify_poster_signature(
            "99999", sig, "test-secret-key-for-unit-tests-only"
        )

    def test_verify_rejects_tampered_signature(self):
        from mediaman.web.routes.poster import _verify_poster_signature
        assert not _verify_poster_signature("12345", "AAAA", "secret")

    def test_verify_rejects_empty_signature(self):
        from mediaman.web.routes.poster import _verify_poster_signature
        assert not _verify_poster_signature("12345", "", "secret")

    def test_verify_rejects_wrong_secret(self):
        from mediaman.web.routes.poster import (
            _verify_poster_signature,
            sign_poster_url,
        )
        url = sign_poster_url("12345", "correct-secret-key-exactly-32-chars")
        sig = url.split("?sig=")[1]
        assert not _verify_poster_signature(
            "12345", sig, "wrong-secret-key-with-exactly-32-bytes"
        )

    def test_verify_ignores_malformed_base64(self):
        from mediaman.web.routes.poster import _verify_poster_signature
        # A signature containing characters invalid in urlsafe base64 must
        # decline cleanly, not raise.
        assert not _verify_poster_signature("12345", "!!!not-base64!!!", "s")


class TestAllowedPosterHost:
    def test_accepts_tmdb(self):
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert _is_allowed_poster_host("https://image.tmdb.org/t/p/w500/x.jpg")

    def test_accepts_themoviedb(self):
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert _is_allowed_poster_host("https://image.themoviedb.org/x.jpg")

    def test_accepts_imdb(self):
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert _is_allowed_poster_host("https://m.media-amazon.imdb.com/x.jpg")

    def test_rejects_http(self):
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert not _is_allowed_poster_host("http://image.tmdb.org/x.jpg")

    def test_rejects_unknown_host(self):
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert not _is_allowed_poster_host("https://evil.example.com/x.jpg")

    def test_rejects_lookalike_suffix(self):
        """`evil-tmdb.org` must NOT be accepted as a `tmdb.org` subdomain."""
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert not _is_allowed_poster_host("https://eviltmdb.org/x.jpg")
        assert not _is_allowed_poster_host("https://tmdb.org.evil.com/x.jpg")

    def test_rejects_ip_literal(self):
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert not _is_allowed_poster_host("https://127.0.0.1/x.jpg")

    def test_rejects_garbage(self):
        from mediaman.web.routes.poster import _is_allowed_poster_host
        assert not _is_allowed_poster_host("not a url")


class TestPosterEndpointAuth:
    """End-to-end checks that the /api/poster/ route enforces auth."""

    def _build_test_app(self, tmp_path, secret_key, *, stub_cache=True):
        """Create a FastAPI TestClient with mocked DB and config."""
        import os
        os.environ["MEDIAMAN_SECRET_KEY"] = secret_key
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import router

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # Pre-seed the on-disk cache so the handler returns a hit (200)
        # without needing to mock Plex/Radarr.
        if stub_cache:
            import hashlib
            cache_dir = tmp_path / "poster_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            safe_name = hashlib.sha256(b"12345").hexdigest()[:16]
            (cache_dir / f"{safe_name}.jpg").write_bytes(b"fake-jpg")

        # Reset the module-level cache dir so the new tmp env var wins.
        import mediaman.web.routes.poster as poster_mod
        poster_mod._cache_dir = None

        return client, conn

    def test_unauthenticated_request_rejected(self, tmp_path):
        secret = "unit-test-secret-key-long-enough-value"
        client, _ = self._build_test_app(tmp_path, secret)
        r = client.get("/api/poster/12345")
        assert r.status_code == 401

    def test_bad_signature_rejected(self, tmp_path):
        secret = "unit-test-secret-key-long-enough-value"
        client, _ = self._build_test_app(tmp_path, secret)
        r = client.get("/api/poster/12345?sig=AAAAAAAA")
        assert r.status_code == 401

    def test_valid_signature_accepted(self, tmp_path):
        secret = "unit-test-secret-key-long-enough-value"
        from mediaman.web.routes.poster import sign_poster_url
        client, _ = self._build_test_app(tmp_path, secret)

        # Build the correct signed URL for rating_key=12345.
        signed = sign_poster_url("12345", secret)
        r = client.get(signed)
        assert r.status_code == 200
        assert r.content == b"fake-jpg"

    def test_admin_session_bypasses_signature(self, tmp_path):
        """A logged-in admin need not attach ?sig=... — the session is enough."""
        secret = "unit-test-secret-key-long-enough-value"
        client, conn = self._build_test_app(tmp_path, secret)

        # Create a user + session
        from mediaman.auth.session import create_session, create_user
        create_user(conn, "admin", "pw-never-used-here-but-must-be-long")
        token = create_session(conn, "admin")

        client.cookies.set("session_token", token)
        r = client.get("/api/poster/12345")
        assert r.status_code == 200

    def test_invalid_rating_key_404s(self, tmp_path):
        """Non-numeric rating keys must 404 (after auth, if admin)."""
        secret = "unit-test-secret-key-long-enough-value"
        client, conn = self._build_test_app(tmp_path, secret, stub_cache=False)

        from mediaman.auth.session import create_session, create_user
        create_user(conn, "admin", "long-enough-test-password-please")
        token = create_session(conn, "admin")

        client.cookies.set("session_token", token)
        r = client.get("/api/poster/abc")
        assert r.status_code == 404
