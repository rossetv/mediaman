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

        from mediaman.config import load_config

        app = FastAPI()
        app.state.config = load_config()
        app.include_router(router)
        client = TestClient(app)

        # Pre-seed the on-disk cache so the handler returns a hit (200)
        # without needing to mock Plex/Radarr.
        if stub_cache:
            cache_dir = tmp_path / "poster_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            safe_name = hashlib.sha256(b"12345").hexdigest()
            (cache_dir / f"{safe_name}.jpg").write_bytes(b"fake-jpg")

        # Reset the module-level cache dir so the new data_dir wins.
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
        create_user(conn, "admin", "long-enough-test-password-please", enforce_policy=False)
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
        create_user(conn, "admin", "long-enough-test-password-please", enforce_policy=False)
        token = create_session(conn, "admin")

        client.cookies.set("session_token", token)
        r = client.get("/api/poster/abc")
        assert r.status_code == 404


class TestArrPosterByStoredId:
    """C16 — Radarr/Sonarr poster lookup must use the stored radarr_id /
    sonarr_id rather than a title match, otherwise a request for an
    unrelated row with the same title poisons the cache."""

    _KEY = "0123456789abcdef" * 4

    def test_no_radarr_id_returns_none(self, tmp_path):
        """If the media_items row has a NULL radarr_id, the fallback returns None."""
        from datetime import datetime, timezone
        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import _fetch_arr_poster

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('r1','Inception','movie',1,'r1',?,'/p',1)",
            (now,),
        )
        conn.commit()

        import os
        os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        bytes_, ctype = _fetch_arr_poster(conn, "r1", None)
        assert bytes_ is None
        assert ctype is None

    def test_radarr_id_match_uses_id_not_title(self, tmp_path):
        """When radarr_id is stored, the Arr lookup matches by id, not title."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock, patch

        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import _fetch_arr_poster

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        now = datetime.now(timezone.utc).isoformat()
        # Stored row: title "Inception", radarr_id 2020.
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes, radarr_id) "
            "VALUES ('r1','Inception','movie',1,'r1',?,'/p',1,2020)",
            (now,),
        )
        conn.commit()

        import os
        os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        # Radarr returns two movies sharing the title — the matcher must
        # pick the one with id 2020, not the other.
        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [
            {"id": 2010, "title": "Inception", "images": [
                {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/WRONG.jpg"}
            ]},
            {"id": 2020, "title": "Inception", "images": [
                {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/RIGHT.jpg"}
            ]},
        ]
        with patch("mediaman.web.routes.poster.build_radarr_from_db", return_value=mock_radarr), \
             patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http:
            mock_resp = MagicMock()
            mock_resp.content = b"right"
            mock_resp.headers = {"Content-Type": "image/jpeg"}
            mock_http.get.return_value = mock_resp

            bytes_, ctype = _fetch_arr_poster(conn, "r1", None)

            # The fetched URL must be the RIGHT one (matching stored id 2020).
            assert mock_http.get.call_args[0][0].endswith("RIGHT.jpg")
            assert bytes_ == b"right"
            assert ctype == "image/jpeg"
