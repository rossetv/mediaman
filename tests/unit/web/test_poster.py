"""Tests for poster proxy endpoint security."""


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

    def test_rejects_oversized_rating_key(self):
        from mediaman.web.routes.poster import _validate_rating_key
        assert _validate_rating_key("1" * 13) is False


class TestPosterSignature:
    """Poster URLs are signed with an expiry-bearing HMAC token.

    The ``sign_poster_url`` → ``validate_poster_token`` pair now uses
    the domain-separated token API in :mod:`mediaman.crypto`, so the
    signature cannot be confused with keep/download/unsubscribe
    tokens.
    """

    _KEY = "0123456789abcdef" * 4  # 64 hex chars, 16 unique — passes entropy check

    def test_sign_and_verify_roundtrip(self):
        from mediaman.crypto import validate_poster_token
        from mediaman.web.routes.poster import sign_poster_url

        url = sign_poster_url("12345", self._KEY)
        assert url.startswith("/api/poster/12345?sig=")
        sig = url.split("?sig=", 1)[1]
        assert validate_poster_token(sig, self._KEY, "12345")

    def test_verify_rejects_tampered_rating_key(self):
        from mediaman.crypto import validate_poster_token
        from mediaman.web.routes.poster import sign_poster_url

        url = sign_poster_url("12345", self._KEY)
        sig = url.split("?sig=", 1)[1]
        assert not validate_poster_token(sig, self._KEY, "99999")

    def test_verify_rejects_tampered_signature(self):
        from mediaman.crypto import validate_poster_token
        assert not validate_poster_token("AAAA.BBBB", self._KEY, "12345")

    def test_verify_rejects_empty_signature(self):
        from mediaman.crypto import validate_poster_token
        assert not validate_poster_token("", self._KEY, "12345")

    def test_verify_rejects_wrong_secret(self):
        from mediaman.crypto import validate_poster_token
        from mediaman.web.routes.poster import sign_poster_url

        url = sign_poster_url("12345", self._KEY)
        sig = url.split("?sig=", 1)[1]
        other_key = "fedcba9876543210" * 4
        assert not validate_poster_token(sig, other_key, "12345")

    def test_verify_rejects_malformed(self):
        from mediaman.crypto import validate_poster_token
        assert not validate_poster_token("!!!not-base64!!!", self._KEY, "12345")

    def test_verify_rejects_oversize_token(self):
        from mediaman.crypto import validate_poster_token
        assert not validate_poster_token("A" * 10_000, self._KEY, "12345")

    def test_cross_purpose_tokens_are_rejected(self):
        """A keep-token must not validate as a poster-token even if the HMAC looks right."""
        import time
        from mediaman.crypto import generate_keep_token, validate_poster_token

        keep = generate_keep_token(
            media_item_id="12345",
            action_id=1,
            expires_at=int(time.time()) + 3600,
            secret_key=self._KEY,
        )
        assert not validate_poster_token(keep, self._KEY, "12345")


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

    _KEY = "0123456789abcdef" * 4  # 64 hex chars, 16 unique

    def _build_test_app(self, tmp_path, secret_key, *, stub_cache=True):
        """Create a FastAPI TestClient with mocked DB and config."""
        import hashlib
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
            cache_dir = tmp_path / "poster_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            safe_name = hashlib.sha256(b"12345").hexdigest()
            (cache_dir / f"{safe_name}.jpg").write_bytes(b"fake-jpg")

        # Reset the module-level cache dir so the new tmp env var wins.
        import mediaman.web.routes.poster as poster_mod
        poster_mod._cache_dir = None

        return client, conn

    def test_unauthenticated_request_rejected(self, tmp_path):
        client, _ = self._build_test_app(tmp_path, self._KEY)
        r = client.get("/api/poster/12345")
        assert r.status_code == 401

    def test_bad_signature_rejected(self, tmp_path):
        client, _ = self._build_test_app(tmp_path, self._KEY)
        r = client.get("/api/poster/12345?sig=AAAAAAAA")
        assert r.status_code == 401

    def test_valid_signature_accepted(self, tmp_path):
        from mediaman.web.routes.poster import sign_poster_url

        client, _ = self._build_test_app(tmp_path, self._KEY)

        signed = sign_poster_url("12345", self._KEY)
        r = client.get(signed)
        assert r.status_code == 200
        assert r.content == b"fake-jpg"

    def test_admin_session_bypasses_signature(self, tmp_path):
        """A logged-in admin need not attach ?sig=... — the session is enough."""
        client, conn = self._build_test_app(tmp_path, self._KEY)

        from mediaman.auth.session import create_session, create_user
        create_user(conn, "admin", "long-enough-test-password-please")
        token = create_session(conn, "admin")

        client.cookies.set("session_token", token)
        r = client.get("/api/poster/12345")
        assert r.status_code == 200

    def test_invalid_rating_key_unauth_returns_401_not_404(self, tmp_path):
        """Unauth + bad rating key returns 401, not 404 — prevents existence oracle."""
        client, _ = self._build_test_app(tmp_path, self._KEY, stub_cache=False)
        r = client.get("/api/poster/abc")
        assert r.status_code == 401

    def test_invalid_rating_key_admin_returns_404(self, tmp_path):
        """Admin + bad rating key returns 404."""
        client, conn = self._build_test_app(tmp_path, self._KEY, stub_cache=False)

        from mediaman.auth.session import create_session, create_user
        create_user(conn, "admin", "long-enough-test-password-please")
        token = create_session(conn, "admin")

        client.cookies.set("session_token", token)
        r = client.get("/api/poster/abc")
        assert r.status_code == 404
