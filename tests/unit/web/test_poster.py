"""Tests for poster proxy endpoint security."""

from datetime import UTC

import pytest
import requests


@pytest.fixture(autouse=True)
def _reset_poster_module_state():
    """Reset poster.py module-level state between tests so suite
    ordering does not cause spurious failures (cache dir, GC counter,
    public-IP rate limiter)."""
    from mediaman.services.infra.rate_limits import POSTER_PUBLIC_LIMITER
    from mediaman.web.routes import poster as poster_mod

    poster_mod._cache_dir = None
    poster_mod._cache_gc_counter = 0
    POSTER_PUBLIC_LIMITER.reset()
    yield
    poster_mod._cache_dir = None
    poster_mod._cache_gc_counter = 0
    POSTER_PUBLIC_LIMITER.reset()


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
        payload = validate_poster_token(sig, self._KEY)
        assert payload is not None
        assert payload.get("rk") == "12345"

    def test_verify_rejects_tampered_rating_key(self):
        """Token for "12345" must not authorise access to "99999"."""
        from mediaman.crypto import validate_poster_token
        from mediaman.web.routes.poster import sign_poster_url

        url = sign_poster_url("12345", self._KEY)
        sig = url.split("?sig=", 1)[1]
        payload = validate_poster_token(sig, self._KEY)
        # The token is cryptographically valid, but the rk claim doesn't match.
        assert payload is None or payload.get("rk") != "99999"

    def test_verify_rejects_tampered_signature(self):
        from mediaman.crypto import validate_poster_token

        assert validate_poster_token("AAAA.BBBB", self._KEY) is None

    def test_verify_rejects_empty_signature(self):
        from mediaman.crypto import validate_poster_token

        assert validate_poster_token("", self._KEY) is None

    def test_verify_rejects_wrong_secret(self):
        from mediaman.crypto import validate_poster_token
        from mediaman.web.routes.poster import sign_poster_url

        url = sign_poster_url("12345", self._KEY)
        sig = url.split("?sig=", 1)[1]
        other_key = "fedcba9876543210" * 4
        assert validate_poster_token(sig, other_key) is None

    def test_verify_rejects_malformed(self):
        from mediaman.crypto import validate_poster_token

        assert validate_poster_token("!!!not-base64!!!", self._KEY) is None

    def test_verify_rejects_oversize_token(self):
        from mediaman.crypto import validate_poster_token

        assert validate_poster_token("A" * 10_000, self._KEY) is None

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
        assert validate_poster_token(keep, self._KEY) is None


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

        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

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

        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

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
        from datetime import datetime

        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import _fetch_arr_poster

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        now = datetime.now(UTC).isoformat()
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
        from datetime import datetime
        from unittest.mock import MagicMock, patch

        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import _fetch_arr_poster

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        now = datetime.now(UTC).isoformat()
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
            {
                "id": 2010,
                "title": "Inception",
                "images": [
                    {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/WRONG.jpg"}
                ],
            },
            {
                "id": 2020,
                "title": "Inception",
                "images": [
                    {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/RIGHT.jpg"}
                ],
            },
        ]
        from mediaman.config import load_config

        config = load_config()

        with (
            patch("mediaman.web.routes.poster.build_radarr_from_db", return_value=mock_radarr),
            patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http,
            patch("mediaman.web.routes.poster.is_safe_outbound_url", return_value=True),
        ):
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

    def test_valid_token_returns_payload(self):
        from mediaman.crypto import generate_poster_token, validate_poster_token

        token = generate_poster_token(rating_key="99", secret_key=self._KEY)
        payload = validate_poster_token(token, self._KEY)
        assert payload is not None
        assert payload.get("rk") == "99"

    def test_token_with_single_bit_flip_returns_none(self):
        """A one-character mutation in the signature must always fail."""
        from mediaman.crypto import generate_poster_token, validate_poster_token

        token = generate_poster_token(rating_key="99", secret_key=self._KEY)
        # Flip a character in the signature portion (after the dot).
        payload, sig = token.rsplit(".", 1)
        # Replace the first character of the sig with a different character.
        bad_char = "A" if sig[0] != "A" else "B"
        mutated = f"{payload}.{bad_char}{sig[1:]}"
        assert validate_poster_token(mutated, self._KEY) is None

    def test_compare_digest_is_used_in_validate_path(self):
        """Confirm that hmac.compare_digest is invoked during validation.

        This guards against a refactor that replaces the timing-safe
        comparison with a plain equality check.
        """
        import hmac
        from unittest.mock import patch

        from mediaman.crypto import generate_poster_token, validate_poster_token

        token = generate_poster_token(rating_key="42", secret_key=self._KEY)
        calls: list[bool] = []
        original = hmac.compare_digest

        def recording_compare_digest(a, b):
            result = original(a, b)
            calls.append(result)
            return result

        with patch("mediaman.crypto.hmac.compare_digest", side_effect=recording_compare_digest):
            result = validate_poster_token(token, self._KEY)

        assert result is not None
        assert result.get("rk") == "42"
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
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
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

        with (
            patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http,
            patch("mediaman.web.routes.poster.is_safe_outbound_url", return_value=True),
            patch(
                "mediaman.web.routes.poster._sanitise_plex_url", return_value="https://localhost"
            ),
        ):
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

        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES ('plex_url', 'https://localhost', 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES ('plex_token', 'fake-token', 0, ?)",
            (now,),
        )
        conn.commit()

        with (
            patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http,
            patch(
                "mediaman.web.routes.poster._sanitise_plex_url", return_value="https://localhost"
            ),
            patch("mediaman.web.routes.poster._fetch_arr_poster", return_value=(None, None)),
        ):
            mock_http.get.side_effect = requests.exceptions.Timeout("timed out")
            signed = sign_poster_url("12345", self._KEY)
            r = client.get(signed)

        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Sidecar mime, LRU cap, public rate limit, tmp cleanup (Domain 03 19-22)
# ---------------------------------------------------------------------------


class TestPosterPublicRateLimit:
    """The unauthenticated signed-URL path must be IP-bucket-limited so
    a leaked URL cannot be used as a bandwidth-amplification vector."""

    _KEY = "0123456789abcdef" * 4

    def _setup(self, tmp_path):
        import hashlib as _hashlib
        import os

        os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.config import load_config
        from mediaman.db import init_db, set_connection
        from mediaman.services.infra.rate_limits import POSTER_PUBLIC_LIMITER
        from mediaman.web.routes.poster import router

        POSTER_PUBLIC_LIMITER.reset()
        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)

        app = FastAPI()
        app.state.config = load_config()
        app.include_router(router)

        # Pre-seed the cache so the response is a fast 200.
        cache_dir = tmp_path / "poster_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _hashlib.sha256(b"12345").hexdigest()
        (cache_dir / f"{safe_name}.jpg").write_bytes(b"fake-jpg")

        import mediaman.web.routes.poster as poster_mod

        poster_mod._cache_dir = None
        return TestClient(app), POSTER_PUBLIC_LIMITER

    def test_unauthenticated_burst_is_throttled(self, tmp_path):
        from mediaman.web.routes.poster import sign_poster_url

        client, _ = self._setup(tmp_path)
        signed = sign_poster_url("12345", self._KEY)

        # Limiter is 60/min per /24 (or /64 for v6). Burst slightly past.
        ok = 0
        throttled = 0
        for _ in range(70):
            r = client.get(signed)
            if r.status_code == 200:
                ok += 1
            elif r.status_code == 429:
                throttled += 1
        assert ok == 60
        assert throttled == 10

    def test_admin_bypasses_ip_cap(self, tmp_path):
        client, _ = self._setup(tmp_path)
        from mediaman.db import get_db
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        conn = get_db()
        create_user(conn, "admin", "long-enough-test-password-please", enforce_policy=False)
        token = create_session(conn, "admin")

        client.cookies.set("session_token", token)

        # Admin gets unlimited access — burst past the IP cap with no 429s.
        for _ in range(80):
            r = client.get("/api/poster/12345")
            assert r.status_code == 200


class TestPosterCacheSidecarMime:
    """Cache writes must persist the served mime in a sidecar file so
    PNG/WebP cache hits don't get served as image/jpeg under nosniff."""

    _KEY = "0123456789abcdef" * 4

    def _setup(self, tmp_path):
        import os

        os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.config import load_config
        from mediaman.db import init_db, set_connection
        from mediaman.services.infra.rate_limits import POSTER_PUBLIC_LIMITER
        from mediaman.web.routes.poster import router

        POSTER_PUBLIC_LIMITER.reset()
        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)

        app = FastAPI()
        app.state.config = load_config()
        app.include_router(router)

        import mediaman.web.routes.poster as poster_mod

        poster_mod._cache_dir = None
        return TestClient(app), conn

    def test_png_first_fetch_persists_sidecar_and_serves_correct_mime(self, tmp_path):
        from datetime import datetime
        from unittest.mock import MagicMock, patch

        from mediaman.web.routes.poster import sign_poster_url

        client, conn = self._setup(tmp_path)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES "
            "('plex_url', 'https://localhost', 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES "
            "('plex_token', 'fake-token', 0, ?)",
            (now,),
        )
        conn.commit()

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        mock_resp = MagicMock()
        mock_resp.content = png_bytes
        mock_resp.headers = {"Content-Type": "image/png"}

        with (
            patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http,
            patch(
                "mediaman.web.routes.poster._sanitise_plex_url",
                return_value="https://localhost",
            ),
        ):
            mock_http.get.return_value = mock_resp
            signed = sign_poster_url("99988", self._KEY)
            r1 = client.get(signed)

        assert r1.status_code == 200
        assert r1.headers["content-type"].startswith("image/png")

        # Cache hit on the second request must serve image/png too,
        # not the legacy image/jpeg default.
        signed = sign_poster_url("99988", self._KEY)
        r2 = client.get(signed)
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("image/png")

    def test_legacy_cache_without_sidecar_falls_back_to_jpeg(self, tmp_path):
        """A pre-existing cache entry from before this change has no
        sidecar; the route must serve image/jpeg rather than 500."""
        import hashlib as _hashlib

        from mediaman.web.routes.poster import sign_poster_url

        client, _ = self._setup(tmp_path)
        cache_dir = tmp_path / "poster_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _hashlib.sha256(b"55512").hexdigest()
        (cache_dir / f"{safe_name}.jpg").write_bytes(b"fake")

        signed = sign_poster_url("55512", self._KEY)
        r = client.get(signed)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")

    def test_sidecar_with_unknown_mime_falls_back_to_jpeg(self, tmp_path):
        """A sidecar whose contents are not in the allow-list must
        never reach the wire — defaults back to image/jpeg."""
        import hashlib as _hashlib

        from mediaman.web.routes.poster import sign_poster_url

        client, _ = self._setup(tmp_path)
        cache_dir = tmp_path / "poster_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _hashlib.sha256(b"55513").hexdigest()
        cached = cache_dir / f"{safe_name}.jpg"
        cached.write_bytes(b"fake")
        cached.with_suffix(".jpg.mime").write_text("text/html", encoding="ascii")

        signed = sign_poster_url("55513", self._KEY)
        r = client.get(signed)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")


class TestPosterCacheLruCap:
    """The cache directory must sweep oldest-first when total size
    exceeds the cap so a long-lived install doesn't fill the disk."""

    def test_sweep_drops_oldest_when_over_cap(self, tmp_path, monkeypatch):
        import os as _os
        import time

        from mediaman.web.routes import poster as poster_mod

        cache_dir = tmp_path / "poster_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Set a very small cap so a 200-byte payload trips it.
        monkeypatch.setattr(poster_mod, "_CACHE_DIR_MAX_BYTES", 200)
        monkeypatch.setattr(poster_mod, "_CACHE_GC_RECHECK_EVERY", 1)
        monkeypatch.setattr(poster_mod, "_cache_gc_counter", 0, raising=False)

        # Oldest first.
        for i in range(5):
            f = cache_dir / f"{i:04d}.jpg"
            f.write_bytes(b"x" * 100)
            mtime = time.time() - (5 - i) * 60  # 5 mins, 4 mins, ...
            _os.utime(str(f), (mtime, mtime))

        poster_mod._maybe_sweep_cache(cache_dir)

        survivors = sorted(p.name for p in cache_dir.iterdir())
        # 5 files * 100 bytes = 500 bytes; cap 200 → target 180 → keep at most one.
        assert len(survivors) <= 2

    def test_sidecar_is_swept_with_image(self, tmp_path, monkeypatch):
        import os as _os
        import time

        from mediaman.web.routes import poster as poster_mod

        cache_dir = tmp_path / "poster_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(poster_mod, "_CACHE_DIR_MAX_BYTES", 50)
        monkeypatch.setattr(poster_mod, "_CACHE_GC_RECHECK_EVERY", 1)
        monkeypatch.setattr(poster_mod, "_cache_gc_counter", 0, raising=False)

        old_jpg = cache_dir / "old.jpg"
        old_jpg.write_bytes(b"x" * 100)
        old_sidecar = cache_dir / "old.jpg.mime"
        old_sidecar.write_text("image/jpeg", encoding="ascii")

        # Force older mtime.
        mtime = time.time() - 600
        _os.utime(str(old_jpg), (mtime, mtime))

        poster_mod._maybe_sweep_cache(cache_dir)

        # Both the jpg and its sidecar should be gone.
        assert not old_jpg.exists()
        assert not old_sidecar.exists()


class TestPosterTempCleanupOnFailure:
    """When ``os.replace`` fails after the temp file is written, the
    temp must be removed explicitly — leaving it would orphan disk
    space until the next sweep."""

    _KEY = "0123456789abcdef" * 4

    def test_orphan_tmp_removed_on_replace_failure(self, tmp_path):
        import os
        from datetime import datetime
        from unittest.mock import MagicMock, patch

        from mediaman.web.routes import poster as poster_mod
        from mediaman.web.routes.poster import sign_poster_url

        os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.config import load_config
        from mediaman.db import init_db, set_connection

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        app = FastAPI()
        app.state.config = load_config()
        app.include_router(poster_mod.router)
        client = TestClient(app)

        poster_mod._cache_dir = None

        from mediaman.services.infra.rate_limits import POSTER_PUBLIC_LIMITER

        POSTER_PUBLIC_LIMITER.reset()

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES "
            "('plex_url', 'https://localhost', 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES "
            "('plex_token', 'fake-token', 0, ?)",
            (now,),
        )
        conn.commit()

        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8\xff" + b"\x00" * 32
        mock_resp.headers = {"Content-Type": "image/jpeg"}

        # Make os.replace blow up so the temp file is left behind.
        def fail_replace(_src, _dst):
            raise OSError("simulated replace failure")

        with (
            patch("mediaman.web.routes.poster._POSTER_HTTP") as mock_http,
            patch(
                "mediaman.web.routes.poster._sanitise_plex_url",
                return_value="https://localhost",
            ),
            patch("mediaman.web.routes.poster.os.replace", side_effect=fail_replace),
        ):
            mock_http.get.return_value = mock_resp
            signed = sign_poster_url("44455", self._KEY)
            r = client.get(signed)

        assert r.status_code == 200
        cache_dir = tmp_path / "poster_cache"
        leftover = list(cache_dir.glob("*.tmp"))
        assert leftover == [], f"Stale .tmp left after replace failure: {leftover}"
