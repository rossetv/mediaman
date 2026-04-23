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
    """The allow-list uses exact hostname matching (no subdomain wildcards).

    Positive cases mock ``is_safe_outbound_url`` to avoid real DNS in unit
    tests — the DNS-resolution behaviour is exercised by the url_safety
    unit tests separately.
    """

    def _allow(self, url: str) -> bool:
        """Run ``_is_allowed_poster_host`` with DNS check stubbed to True."""
        from unittest.mock import patch

        from mediaman.web.routes.poster import _is_allowed_poster_host

        with patch("mediaman.web.routes.poster.is_safe_outbound_url", return_value=True):
            return _is_allowed_poster_host(url)

    def _deny(self, url: str) -> bool:
        """Run ``_is_allowed_poster_host`` without stubbing (rejects before DNS)."""
        from mediaman.web.routes.poster import _is_allowed_poster_host
        return _is_allowed_poster_host(url)

    def test_accepts_image_tmdb_org(self):
        assert self._allow("https://image.tmdb.org/t/p/w500/x.jpg")

    def test_accepts_m_media_amazon_com(self):
        assert self._allow("https://m.media-amazon.com/images/M/poster.jpg")

    def test_accepts_images_amazon_com(self):
        assert self._allow("https://images.amazon.com/images/poster.jpg")

    def test_rejects_subdomain_of_allowed(self):
        """Subdomain of an allowed host must NOT pass — no wildcard matching."""
        assert not self._deny("https://evil.image.tmdb.org/x.jpg")

    def test_rejects_themoviedb_no_longer_in_list(self):
        """image.themoviedb.org was in the old suffix list but is not in the new exact list."""
        assert not self._deny("https://image.themoviedb.org/x.jpg")

    def test_rejects_imdb_not_in_list(self):
        """m.media-amazon.imdb.com is not in the exact allow-list."""
        assert not self._deny("https://m.media-amazon.imdb.com/x.jpg")

    def test_rejects_http(self):
        assert not self._deny("http://image.tmdb.org/x.jpg")

    def test_rejects_unknown_host(self):
        assert not self._deny("https://evil.example.com/x.jpg")

    def test_rejects_lookalike_suffix(self):
        assert not self._deny("https://eviltmdb.org/x.jpg")
        assert not self._deny("https://tmdb.org.evil.com/x.jpg")

    def test_rejects_non_443_port(self):
        """Non-443 port on an otherwise-allowed host must be rejected."""
        assert not self._deny("https://image.tmdb.org:8080/x.jpg")

    def test_rejects_ip_literal(self):
        assert not self._deny("https://127.0.0.1/x.jpg")

    def test_rejects_garbage(self):
        assert not self._deny("not a url")

    def test_dns_rebind_private_ip_rejected(self):
        """If is_safe_outbound_url returns False (e.g. DNS resolves private IP), reject."""
        from unittest.mock import patch

        from mediaman.web.routes.poster import _is_allowed_poster_host

        with patch("mediaman.web.routes.poster.is_safe_outbound_url", return_value=False):
            assert not _is_allowed_poster_host("https://image.tmdb.org/t/p/w500/x.jpg")


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

        from mediaman.config import load_config
        config = load_config()

        bytes_, ctype = _fetch_arr_poster(conn, "r1", None, config)
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
        from mediaman.config import load_config
        config = load_config()

        with patch("mediaman.web.routes.poster.build_radarr_from_db", return_value=mock_radarr), \
             patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http, \
             patch("mediaman.web.routes.poster.is_safe_outbound_url", return_value=True):
            mock_resp = MagicMock()
            mock_resp.content = b"right"
            mock_resp.headers = {"Content-Type": "image/jpeg"}
            mock_http.get.return_value = mock_resp

            bytes_, ctype = _fetch_arr_poster(conn, "r1", None, config)

            # The fetched URL must be the RIGHT one (matching stored id 2020).
            assert mock_http.get.call_args[0][0].endswith("RIGHT.jpg")
            assert bytes_ == b"right"
            assert ctype == "image/jpeg"


class TestPosterTokenTimingSafety:
    """H19 regression — validate_poster_token must use hmac.compare_digest.

    We cannot test the C implementation's timing properties in a unit test,
    but we can assert that the comparison path goes through
    ``hmac.compare_digest`` and that equal/unequal tokens behave correctly,
    catching any accidental revert to a plain ``==`` comparison.
    """

    _KEY = "0123456789abcdef" * 4

    def test_valid_token_returns_true(self):
        from mediaman.crypto import generate_poster_token, validate_poster_token
        token = generate_poster_token("99", self._KEY)
        assert validate_poster_token(token, self._KEY, "99") is True

    def test_token_with_single_bit_flip_returns_false(self):
        """A one-character mutation in the signature must always fail."""
        from mediaman.crypto import generate_poster_token, validate_poster_token
        token = generate_poster_token("99", self._KEY)
        # Flip a character in the signature portion (after the dot).
        payload, sig = token.rsplit(".", 1)
        # Replace the first character of the sig with a different character.
        bad_char = "A" if sig[0] != "A" else "B"
        mutated = f"{payload}.{bad_char}{sig[1:]}"
        assert validate_poster_token(mutated, self._KEY, "99") is False

    def test_compare_digest_is_used_in_validate_path(self):
        """Confirm that hmac.compare_digest is invoked during validation.

        This guards against a refactor that replaces the timing-safe
        comparison with a plain equality check.
        """
        import hmac
        from unittest.mock import patch

        from mediaman.crypto import generate_poster_token, validate_poster_token

        token = generate_poster_token("42", self._KEY)
        calls: list[bool] = []
        original = hmac.compare_digest

        def recording_compare_digest(a, b):
            result = original(a, b)
            calls.append(result)
            return result

        with patch("mediaman.crypto.hmac.compare_digest", side_effect=recording_compare_digest):
            result = validate_poster_token(token, self._KEY, "42")

        assert result is True
        assert len(calls) >= 1, "hmac.compare_digest was not called"
        assert calls[-1] is True


class TestPosterCacheAtomicWrite:
    """H18 — cache writes must be atomic (temp file + os.replace)."""

    _KEY = "0123456789abcdef" * 4

    def _build_test_app(self, tmp_path, secret_key, *, stub_cache=False):
        import os as _os
        _os.environ["MEDIAMAN_SECRET_KEY"] = secret_key
        _os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.config import load_config
        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import router

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)

        app = FastAPI()
        app.state.config = load_config()
        app.include_router(router)

        import mediaman.web.routes.poster as poster_mod
        poster_mod._cache_dir = None

        return TestClient(app), conn

    def test_no_tmp_file_left_after_successful_cache_write(self, tmp_path):
        """After a successful poster fetch, no .tmp file should remain."""
        from unittest.mock import MagicMock, patch

        from mediaman.web.routes.poster import sign_poster_url

        client, conn = self._build_test_app(tmp_path, self._KEY)

        # Seed DB with Plex URL/token rows.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES ('plex_url', 'https://localhost', 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES ('plex_token', 'fake-token', 0, ?)",
            (now,),
        )
        conn.commit()

        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8\xff"  # JPEG magic bytes
        mock_resp.headers = {"Content-Type": "image/jpeg"}

        with patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http, \
             patch("mediaman.web.routes.poster.is_safe_outbound_url", return_value=True), \
             patch("mediaman.web.routes.poster._sanitise_plex_url", return_value="https://localhost"):
            mock_http.get.return_value = mock_resp
            signed = sign_poster_url("12345", self._KEY)
            r = client.get(signed)

        assert r.status_code == 200

        cache_dir = tmp_path / "poster_cache"
        tmp_files = list(cache_dir.glob("*.tmp"))
        assert tmp_files == [], f"Stale .tmp files found: {tmp_files}"


class TestPosterTimeoutFallback:
    """H17 — a slow or failing poster fetch returns 404, not an exception."""

    _KEY = "0123456789abcdef" * 4

    def _build_test_app(self, tmp_path, secret_key):
        import os as _os
        _os.environ["MEDIAMAN_SECRET_KEY"] = secret_key
        _os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.config import load_config
        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import router

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)

        app = FastAPI()
        app.state.config = load_config()
        app.include_router(router)

        import mediaman.web.routes.poster as poster_mod
        poster_mod._cache_dir = None

        return TestClient(app), conn

    def test_plex_timeout_returns_404_not_500(self, tmp_path):
        """If the Plex fetch times out and there is no Arr fallback, return 404."""
        from unittest.mock import patch

        from mediaman.web.routes.poster import sign_poster_url

        client, conn = self._build_test_app(tmp_path, self._KEY)

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES ('plex_url', 'https://localhost', 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES ('plex_token', 'fake-token', 0, ?)",
            (now,),
        )
        conn.commit()

        with patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http, \
             patch("mediaman.web.routes.poster._sanitise_plex_url", return_value="https://localhost"), \
             patch("mediaman.web.routes.poster._fetch_arr_poster", return_value=(None, None)):
            mock_http.get.side_effect = Exception("timed out")
            signed = sign_poster_url("12345", self._KEY)
            r = client.get(signed)

        assert r.status_code == 404
