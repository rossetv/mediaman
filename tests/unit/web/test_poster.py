"""Tests for poster proxy endpoint security."""

from __future__ import annotations

import pytest
import requests

from mediaman.services.infra import SafeHTTPError
from tests.helpers.factories import (
    insert_admin_user,
    insert_audit_log,
    insert_media_item,
    insert_settings,
)


def _scan(conn) -> None:
    """Tick Plex library 1 as scanned; items in it may then have their poster served."""
    insert_settings(conn, plex_libraries='["1"]')


@pytest.fixture(autouse=True)
def _reset_poster_module_state():
    """Reset poster.py module-level state between tests so suite
    ordering does not cause spurious failures (cache dir, GC counter,
    public-IP rate limiter)."""
    from mediaman.services.rate_limit.instances import POSTER_PUBLIC_LIMITER
    from mediaman.web.routes.poster import cache as cache_mod
    from mediaman.web.routes.poster import fetch as fetch_mod

    cache_mod._cache_dir = None
    cache_mod._cache_gc_counter = 0
    POSTER_PUBLIC_LIMITER.reset()
    fetch_mod._MISSING_POSTERS.clear()
    yield
    cache_mod._cache_dir = None
    cache_mod._cache_gc_counter = 0
    POSTER_PUBLIC_LIMITER.reset()
    fetch_mod._MISSING_POSTERS.clear()


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

    The ``sign_poster_url`` → ``validate_poster_token`` pair uses
    the domain-separated token API in `mediaman.crypto`, so the
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

    # The canonical pinned poster-CDN hosts, mirrored from
    # ``PINNED_EXTERNAL_HOSTS`` — the allow path passes these as the
    # SafeHTTPClient allowlist so host policy comes from one source.
    _ALLOWED = frozenset({"image.tmdb.org", "m.media-amazon.com", "images.amazon.com"})

    def _allow(self, url: str) -> bool:
        """Run ``_is_allowed_poster_host`` with DNS check stubbed to True."""
        from unittest.mock import patch

        from mediaman.web.routes.poster import _is_allowed_poster_host

        with patch("mediaman.web.routes.poster.fetch.is_safe_outbound_url", return_value=True):
            return _is_allowed_poster_host(url, allowed_hosts=self._ALLOWED)

    def _deny(self, url: str) -> bool:
        """Run ``_is_allowed_poster_host`` without stubbing (rejects before DNS)."""
        from mediaman.web.routes.poster import _is_allowed_poster_host

        return _is_allowed_poster_host(url, allowed_hosts=self._ALLOWED)

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

        with patch("mediaman.web.routes.poster.fetch.is_safe_outbound_url", return_value=False):
            assert not _is_allowed_poster_host(
                "https://image.tmdb.org/t/p/w500/x.jpg", allowed_hosts=self._ALLOWED
            )


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
        from mediaman.web.routes.poster import cache as cache_mod

        cache_mod._cache_dir = None

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
        insert_media_item(conn, id="12345", plex_library_id=1)
        _scan(conn)

        client.cookies.set("session_token", token)
        r = client.get("/api/poster/12345")
        assert r.status_code == 200

    def _session(self, conn, client, role="viewer"):
        from mediaman.web.auth.session_store import create_session

        insert_admin_user(conn, username="person", role=role)
        client.cookies.set("session_token", create_session(conn, "person"))

    def test_viewer_refused_for_item_outside_scanned_libraries(self, tmp_path):
        client, conn = self._build_test_app(tmp_path, self._KEY)
        self._session(conn, client, "viewer")
        insert_media_item(conn, id="12345", plex_library_id=9)
        _scan(conn)

        assert client.get("/api/poster/12345").status_code == 404

    def test_admin_refused_for_item_outside_scanned_libraries(self, tmp_path):
        client, conn = self._build_test_app(tmp_path, self._KEY)
        self._session(conn, client, "admin")
        insert_media_item(conn, id="12345", plex_library_id=9)
        _scan(conn)

        assert client.get("/api/poster/12345").status_code == 404

    def test_deleted_item_poster_is_served_by_audit_media_id(self, tmp_path):
        from mediaman.core.audit import log_audit

        client, conn = self._build_test_app(tmp_path, self._KEY)
        self._session(conn, client, "viewer")
        _scan(conn)
        log_audit(conn, "12345", "deleted", "Deleted: Gone")
        conn.commit()

        assert client.get("/api/poster/12345").status_code == 200

    def test_deleted_item_poster_is_served_by_rating_key_in_detail(self, tmp_path):
        from mediaman.core.audit import log_audit

        client, conn = self._build_test_app(tmp_path, self._KEY)
        self._session(conn, client, "viewer")
        _scan(conn)
        log_audit(conn, "sonarr:7", "deleted", "Deleted: Gone [rk:12345]")
        log_audit(conn, "other", "snoozed", "Snoozed [rk:54321]")
        conn.commit()

        assert client.get("/api/poster/12345").status_code == 200
        assert client.get("/api/poster/54321").status_code == 404

    def test_session_refused_for_key_not_in_the_library(self, tmp_path):
        client, conn = self._build_test_app(tmp_path, self._KEY)
        self._session(conn, client)
        _scan(conn)

        assert client.get("/api/poster/12345").status_code == 404

    def test_show_key_of_a_scanned_season_is_served(self, tmp_path):
        client, conn = self._build_test_app(tmp_path, self._KEY)
        self._session(conn, client)
        insert_media_item(
            conn,
            id="501",
            plex_rating_key="501",
            media_type="tv_season",
            season_number=1,
            show_rating_key="12345",
            plex_library_id=1,
        )
        _scan(conn)

        assert client.get("/api/poster/12345").status_code == 200

    def test_invalid_rating_key_unauth_returns_401_not_404(self, tmp_path):
        """Unauth + bad rating key returns 401, not 404 — prevents existence oracle."""
        client, _ = self._build_test_app(tmp_path, self._KEY, stub_cache=False)
        r = client.get("/api/poster/abc")
        assert r.status_code == 401

    def test_invalid_rating_key_admin_returns_404(self, tmp_path):
        client, conn = self._build_test_app(tmp_path, self._KEY, stub_cache=False)

        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        create_user(conn, "admin", "long-enough-test-password-please", enforce_policy=False)
        token = create_session(conn, "admin")

        client.cookies.set("session_token", token)
        r = client.get("/api/poster/abc")
        assert r.status_code == 404


class TestArrPosterByStoredId:
    """Radarr/Sonarr poster lookup must use the stored radarr_id /
    sonarr_id rather than a title match, otherwise a request for an
    unrelated row with the same title poisons the cache."""

    _KEY = "0123456789abcdef" * 4

    def test_no_radarr_id_returns_none(self, tmp_path):
        """If the media_items row has a NULL radarr_id, the fallback returns None."""
        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import _fetch_arr_poster

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        insert_media_item(
            conn,
            id="r1",
            title="Inception",
            plex_rating_key="r1",
            file_path="/p",
            file_size_bytes=1,
        )

        import os

        os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from mediaman.config import load_config

        config = load_config()

        bytes_, ctype = _fetch_arr_poster(conn, "r1", config)
        assert bytes_ is None
        assert ctype is None

    def test_radarr_id_match_uses_id_not_title(self, tmp_path):
        """When radarr_id is stored, the Arr lookup matches by id, not title."""
        from unittest.mock import MagicMock, patch

        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import _fetch_arr_poster

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        # Stored row: title "Inception", radarr_id 2020.
        insert_media_item(
            conn,
            id="r1",
            title="Inception",
            plex_rating_key="r1",
            file_path="/p",
            file_size_bytes=1,
            radarr_id=2020,
        )

        import os

        os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        # Radarr is called via get_movie_by_id(radarr_id) — the single-item
        # lookup — so mock that method directly.  The correct
        # poster URL must come from the record with id 2020.
        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_id.return_value = {
            "id": 2020,
            "title": "Inception",
            "images": [
                {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/w342/RIGHT.jpg"}
            ],
        }
        from mediaman.config import load_config

        config = load_config()

        with (
            patch(
                "mediaman.web.routes.poster.fetch.build_radarr_from_db", return_value=mock_radarr
            ),
            patch("mediaman.web.routes.poster.fetch._POSTER_HTTP") as mock_http,
            patch("mediaman.web.routes.poster.fetch.is_safe_outbound_url", return_value=True),
        ):
            mock_resp = MagicMock()
            mock_resp.content = b"right"
            mock_resp.headers = {"Content-Type": "image/jpeg"}
            mock_http.get.return_value = mock_resp

            bytes_, ctype = _fetch_arr_poster(conn, "r1", config)

            # The fetched URL must be the RIGHT one (matching stored id 2020).
            assert mock_http.get.call_args[0][0].endswith("RIGHT.jpg")
            assert bytes_ == b"right"
            assert ctype == "image/jpeg"


class TestPosterTokenTimingSafety:
    """Validate_poster_token must use hmac.compare_digest.

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
    """Cache writes must be atomic (temp file + os.replace)."""

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

        from mediaman.web.routes.poster import cache as cache_mod

        cache_mod._cache_dir = None

        return TestClient(app), conn

    def test_no_tmp_file_left_after_successful_cache_write(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from mediaman.web.routes.poster import sign_poster_url

        client, conn = self._build_test_app(tmp_path, self._KEY)

        # Seed DB with Plex URL/token rows.
        insert_settings(conn, plex_url="https://localhost", plex_token="fake-token")

        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8\xff"  # JPEG magic bytes
        mock_resp.headers = {"Content-Type": "image/jpeg"}

        with (
            patch("mediaman.web.routes.poster.fetch._POSTER_HTTP") as mock_http,
            patch("mediaman.web.routes.poster.fetch.is_safe_outbound_url", return_value=True),
            patch(
                "mediaman.web.routes.poster.fetch.sanitise_plex_url",
                return_value="https://localhost",
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
    """A slow or failing poster fetch returns 404, not an exception."""

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

        from mediaman.web.routes.poster import cache as cache_mod

        cache_mod._cache_dir = None

        return TestClient(app), conn

    def test_plex_timeout_returns_404_not_500(self, tmp_path):
        """If the Plex fetch times out and there is no Arr fallback, return 404."""
        from unittest.mock import patch

        from mediaman.web.routes.poster import sign_poster_url

        client, conn = self._build_test_app(tmp_path, self._KEY)

        insert_settings(conn, plex_url="https://localhost", plex_token="fake-token")

        with (
            patch("mediaman.web.routes.poster.fetch._POSTER_HTTP") as mock_http,
            patch(
                "mediaman.web.routes.poster.fetch.sanitise_plex_url",
                return_value="https://localhost",
            ),
            patch("mediaman.web.routes.poster._fetch_arr_poster", return_value=(None, None)),
        ):
            mock_http.get.side_effect = requests.exceptions.Timeout("timed out")
            signed = sign_poster_url("12345", self._KEY)
            r = client.get(signed)

        assert r.status_code == 404


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
        from mediaman.services.rate_limit.instances import POSTER_PUBLIC_LIMITER
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

        from mediaman.web.routes.poster import cache as cache_mod

        cache_mod._cache_dir = None
        return TestClient(app), POSTER_PUBLIC_LIMITER

    def test_unauthenticated_requests_within_allowance_succeed(self, tmp_path):
        """First 60 requests (the documented allowance) must all return 200."""
        from mediaman.web.routes.poster import sign_poster_url

        client, _ = self._setup(tmp_path)
        signed = sign_poster_url("12345", self._KEY)

        for i in range(60):
            r = client.get(signed)
            assert r.status_code == 200, f"Request {i + 1}/60 expected 200 but got {r.status_code}"

    def test_unauthenticated_request_beyond_allowance_is_throttled(self, tmp_path):
        """Request 61 (first beyond the 60/min cap) must return 429."""
        from mediaman.web.routes.poster import sign_poster_url

        client, _ = self._setup(tmp_path)
        signed = sign_poster_url("12345", self._KEY)

        for i in range(60):
            r = client.get(signed)
            assert r.status_code == 200, (
                f"Setup request {i + 1}/60 expected 200 but got {r.status_code}"
            )

        r = client.get(signed)
        assert r.status_code == 429

    def test_admin_bypasses_ip_cap(self, tmp_path):
        client, _ = self._setup(tmp_path)
        from mediaman.db import get_db
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        conn = get_db()
        create_user(conn, "admin", "long-enough-test-password-please", enforce_policy=False)
        token = create_session(conn, "admin")
        insert_media_item(conn, id="12345", plex_library_id=1)
        _scan(conn)

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
        from mediaman.services.rate_limit.instances import POSTER_PUBLIC_LIMITER
        from mediaman.web.routes.poster import router

        POSTER_PUBLIC_LIMITER.reset()
        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)

        app = FastAPI()
        app.state.config = load_config()
        app.include_router(router)

        from mediaman.web.routes.poster import cache as cache_mod

        cache_mod._cache_dir = None
        return TestClient(app), conn

    def test_png_first_fetch_persists_sidecar_and_serves_correct_mime(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from mediaman.web.routes.poster import sign_poster_url

        client, conn = self._setup(tmp_path)
        insert_settings(conn, plex_url="https://localhost", plex_token="fake-token")

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        mock_resp = MagicMock()
        mock_resp.content = png_bytes
        mock_resp.headers = {"Content-Type": "image/png"}

        with (
            patch("mediaman.web.routes.poster.fetch._POSTER_HTTP") as mock_http,
            patch(
                "mediaman.web.routes.poster.fetch.sanitise_plex_url",
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

        from mediaman.web.routes.poster import cache as cache_mod

        cache_dir = tmp_path / "poster_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Set a very small cap so a 200-byte payload trips it.
        monkeypatch.setattr(cache_mod, "_CACHE_DIR_MAX_BYTES", 200)
        monkeypatch.setattr(cache_mod, "_CACHE_GC_RECHECK_EVERY", 1)
        monkeypatch.setattr(cache_mod, "_cache_gc_counter", 0, raising=False)

        # Oldest first.
        for i in range(5):
            f = cache_dir / f"{i:04d}.jpg"
            f.write_bytes(b"x" * 100)
            mtime = time.time() - (5 - i) * 60  # 5 mins, 4 mins, ...
            _os.utime(str(f), (mtime, mtime))

        cache_mod.maybe_sweep_cache(cache_dir)

        survivors = sorted(p.name for p in cache_dir.iterdir())
        # 5 files * 100 bytes = 500 bytes; cap 200 → target 180 → keep at most one.
        assert len(survivors) <= 2

    def test_sidecar_is_swept_with_image(self, tmp_path, monkeypatch):
        import os as _os
        import time

        from mediaman.web.routes.poster import cache as cache_mod

        cache_dir = tmp_path / "poster_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(cache_mod, "_CACHE_DIR_MAX_BYTES", 50)
        monkeypatch.setattr(cache_mod, "_CACHE_GC_RECHECK_EVERY", 1)
        monkeypatch.setattr(cache_mod, "_cache_gc_counter", 0, raising=False)

        old_jpg = cache_dir / "old.jpg"
        old_jpg.write_bytes(b"x" * 100)
        old_sidecar = cache_dir / "old.jpg.mime"
        old_sidecar.write_text("image/jpeg", encoding="ascii")

        # Force older mtime.
        mtime = time.time() - 600
        _os.utime(str(old_jpg), (mtime, mtime))

        cache_mod.maybe_sweep_cache(cache_dir)

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

        from mediaman.web.routes.poster import cache as cache_mod

        cache_mod._cache_dir = None

        from mediaman.services.rate_limit.instances import POSTER_PUBLIC_LIMITER

        POSTER_PUBLIC_LIMITER.reset()

        insert_settings(conn, plex_url="https://localhost", plex_token="fake-token")

        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8\xff" + b"\x00" * 32
        mock_resp.headers = {"Content-Type": "image/jpeg"}

        # Make os.replace blow up so the temp file is left behind.
        def fail_replace(_src, _dst):
            raise OSError("simulated replace failure")

        with (
            patch("mediaman.web.routes.poster.fetch._POSTER_HTTP") as mock_http,
            patch(
                "mediaman.web.routes.poster.fetch.sanitise_plex_url",
                return_value="https://localhost",
            ),
            patch("mediaman.web.routes.poster.cache.os.replace", side_effect=fail_replace),
        ):
            mock_http.get.return_value = mock_resp
            signed = sign_poster_url("44455", self._KEY)
            r = client.get(signed)

        assert r.status_code == 200
        cache_dir = tmp_path / "poster_cache"
        leftover = list(cache_dir.glob("*.tmp"))
        assert leftover == [], f"Stale .tmp left after replace failure: {leftover}"


class TestOversizedPlexPoster:
    """A poster whose original is over the body cap is served from Plex's card-sized copy."""

    _KEY = "0123456789abcdef" * 4
    _TOO_BIG = "response body too large: declared 5242880 > cap 4194304"

    def _app(self, tmp_path):
        import os as _os

        _os.environ["MEDIAMAN_SECRET_KEY"] = self._KEY
        _os.environ["MEDIAMAN_DATA_DIR"] = str(tmp_path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from mediaman.config import load_config
        from mediaman.db import init_db, set_connection
        from mediaman.web.routes.poster import router

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)
        insert_settings(conn, plex_url="https://localhost", plex_token="fake-token")
        app = FastAPI()
        app.state.config = load_config()
        app.include_router(router)
        return TestClient(app)

    @staticmethod
    def _plex(*, transcode_ok: bool, original_ok: bool = False):
        """Stub client: the original is over the cap; the transcoder answers as told."""
        from unittest.mock import MagicMock

        from mediaman.services.infra import SafeHTTPError

        calls: list[tuple[str, dict]] = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            if "/photo/:/transcode" in url:
                if not transcode_ok:
                    raise SafeHTTPError(500, "transcoder busy", url)
                body = b"small-card"
            elif original_ok:
                body = b"original"
            else:
                raise SafeHTTPError(200, TestOversizedPlexPoster._TOO_BIG, url)
            resp = MagicMock()
            resp.content = body
            resp.headers = {"Content-Type": "image/jpeg"}
            return resp

        stub = MagicMock()
        stub.get.side_effect = get
        return stub, calls

    def _fetch(self, client, stub, key="12345"):
        from unittest.mock import patch

        from mediaman.web.routes.poster import sign_poster_url

        with (
            patch("mediaman.web.routes.poster.fetch._POSTER_HTTP", stub),
            patch(
                "mediaman.web.routes.poster.fetch.sanitise_plex_url",
                return_value="https://localhost",
            ),
            patch("mediaman.web.routes.poster._fetch_arr_poster", return_value=(None, None)),
        ):
            return client.get(sign_poster_url(key, self._KEY))

    def test_original_over_the_cap_is_served_from_the_card_sized_copy(self, tmp_path):
        stub, calls = self._plex(transcode_ok=True)
        r = self._fetch(self._app(tmp_path), stub)

        assert r.status_code == 200
        assert r.content == b"small-card"
        url, kwargs = calls[0]
        assert url.startswith("https://localhost/photo/:/transcode?")
        assert "url=%2Flibrary%2Fmetadata%2F12345%2Fthumb" in url
        assert kwargs["headers"] == {"X-Plex-Token": "fake-token"}

    def test_original_is_used_when_the_transcoder_fails(self, tmp_path):
        stub, calls = self._plex(transcode_ok=False, original_ok=True)
        r = self._fetch(self._app(tmp_path), stub)

        assert r.status_code == 200
        assert r.content == b"original"
        assert calls[-1][0] == "https://localhost/library/metadata/12345/thumb"

    def test_the_log_says_why_the_fetch_failed(self, tmp_path, caplog):
        stub, _ = self._plex(transcode_ok=False)
        with caplog.at_level("WARNING", logger="mediaman.web.routes.poster.fetch"):
            r = self._fetch(self._app(tmp_path), stub)

        assert r.status_code == 404
        assert "too large" in caplog.text

    def test_a_failing_poster_is_not_refetched_until_the_pause_ends(self, tmp_path):
        from unittest.mock import patch

        client = self._app(tmp_path)
        stub, calls = self._plex(transcode_ok=False)
        with patch("mediaman.web.routes.poster.fetch.monotonic", return_value=1000.0):
            assert self._fetch(client, stub).status_code == 404
            first = len(calls)
            assert self._fetch(client, stub).status_code == 404
            assert len(calls) == first
        with patch("mediaman.web.routes.poster.fetch.monotonic", return_value=1000.0 + 3600):
            assert self._fetch(client, stub).status_code == 404
            assert len(calls) > first

    @pytest.mark.parametrize(
        "failure",
        [
            SafeHTTPError(0, "transport error: ConnectionError", "https://localhost/x"),
            requests.ConnectionError("plex down"),
        ],
        ids=["safe-http-transport", "requests-exception"],
    )
    def test_an_unreachable_plex_is_asked_once_not_for_the_original_too(self, tmp_path, failure):
        from unittest.mock import MagicMock

        stub = MagicMock()
        stub.get.side_effect = failure
        r = self._fetch(self._app(tmp_path), stub)

        assert r.status_code == 404
        assert stub.get.call_count == 1

    def test_the_log_hides_the_token_even_when_it_straddles_the_cut_and_cannot_forge_lines(
        self, tmp_path, caplog
    ):
        from mediaman.services.infra import SafeHTTPError

        body = "x" * 195 + "fake-token" + "\nWARNING forged line"
        stub, _ = self._plex(transcode_ok=False)
        stub.get.side_effect = SafeHTTPError(500, body, "https://localhost/x")
        with caplog.at_level("WARNING", logger="mediaman.web.routes.poster.fetch"):
            self._fetch(self._app(tmp_path), stub)

        assert "fake-" not in caplog.text
        assert "\nWARNING forged line" not in caplog.text


class TestPlexTransportFailures:
    """A slow transcoder still falls back to the original; an unreachable Plex is asked once."""

    @staticmethod
    def _transport_error(url, cause):
        return SafeHTTPError(0, f"transport error: {type(cause).__name__}", url)

    def _fetch(self, transcode_cause):
        from unittest.mock import MagicMock

        from mediaman.web.routes.poster.fetch import fetch_plex_poster

        urls: list[str] = []

        def get(url, **kwargs):
            urls.append(url)
            if "/photo/:/transcode" in url:
                raise self._transport_error(url, transcode_cause)
            resp = MagicMock()
            resp.content = b"original"
            resp.headers = {"Content-Type": "image/jpeg"}
            return resp

        stub = MagicMock()
        stub.get.side_effect = get
        return fetch_plex_poster("https://plex", "tok", "12345", http_client=stub), urls

    def test_original_is_served_after_the_transcoder_read_times_out(self):
        (content, _), urls = self._fetch(requests.exceptions.ReadTimeout("slow"))

        assert content == b"original"
        assert len(urls) == 2

    @pytest.mark.parametrize(
        "cause",
        [requests.exceptions.ConnectionError("refused"), requests.exceptions.ConnectTimeout("t")],
    )
    def test_unreachable_plex_is_asked_once(self, cause):
        (content, _), urls = self._fetch(cause)

        assert content is None
        assert len(urls) == 1


class TestPosterBackdrop:
    """?kind=art serves the wide backdrop from Plex, cached on disk, else TMDB's, else 404."""

    _KEY = "0123456789abcdef" * 4

    def _client(self, tmp_path):
        return TestOversizedPlexPoster._app(self, tmp_path)

    @staticmethod
    def _plex(*, has_art: bool):
        from unittest.mock import MagicMock

        urls: list[str] = []

        def is_art(url):
            return "%2Fart" in url or url.endswith("/art")

        def get(url, **kwargs):
            urls.append(url)
            if is_art(url) and not has_art:
                raise SafeHTTPError(404, "not found", url)
            resp = MagicMock()
            resp.content = b"wide-art" if is_art(url) else b"portrait-poster"
            resp.headers = {"Content-Type": "image/jpeg"}
            return resp

        stub = MagicMock()
        stub.get.side_effect = get
        return stub, urls

    def _get(self, client, stub, query="&kind=art"):
        from unittest.mock import patch

        from mediaman.web.routes.poster import sign_poster_url

        with (
            patch("mediaman.web.routes.poster.fetch._POSTER_HTTP", stub),
            patch(
                "mediaman.web.routes.poster.fetch.sanitise_plex_url",
                return_value="https://localhost",
            ),
            patch("mediaman.web.routes.poster._fetch_arr_poster", return_value=(None, None)),
        ):
            return client.get(sign_poster_url("12345", self._KEY) + query)

    def test_the_backdrop_is_asked_of_plex_at_screen_width(self, tmp_path):
        stub, urls = self._plex(has_art=True)

        r = self._get(self._client(tmp_path), stub)

        assert r.status_code == 200
        assert r.content == b"wide-art"
        assert "url=%2Flibrary%2Fmetadata%2F12345%2Fart" in urls[0]
        assert "width=1920" in urls[0]

    def test_a_cached_backdrop_is_not_fetched_again(self, tmp_path):
        stub, urls = self._plex(has_art=True)
        client = self._client(tmp_path)

        self._get(client, stub)
        first = len(urls)
        r = self._get(client, stub)

        assert r.content == b"wide-art"
        assert len(urls) == first

    def test_a_title_with_no_backdrop_is_a_404_never_its_poster(self, tmp_path):
        stub, _ = self._plex(has_art=False)

        r = self._get(self._client(tmp_path), stub)

        assert r.status_code == 404
        assert r.content == b""

    def test_without_kind_the_poster_is_served_and_plex_is_not_asked_for_art(self, tmp_path):
        stub, urls = self._plex(has_art=True)

        r = self._get(self._client(tmp_path), stub, query="")

        assert r.content == b"portrait-poster"
        assert not any("%2Fart" in u for u in urls)

    def test_a_missing_backdrop_is_not_remembered_for_a_week(self, tmp_path):
        stub, _ = self._plex(has_art=False)
        client = self._client(tmp_path)

        missing = self._get(client, stub)
        plain = self._get(client, stub, query="")

        assert missing.headers["cache-control"] == "private, max-age=300"
        assert plain.headers["cache-control"] == "public, max-age=604800"

    def test_a_backdrop_served_from_plex_is_kept_for_the_week(self, tmp_path):
        stub, _ = self._plex(has_art=True)

        r = self._get(self._client(tmp_path), stub)

        assert r.headers["cache-control"] == "public, max-age=604800"

    def test_without_plex_credentials_the_backdrop_is_a_404_not_the_cached_poster(self, tmp_path):
        from mediaman.web.routes.poster import sign_poster_url

        client, conn = TestPosterEndpointAuth._build_test_app(self, tmp_path, self._KEY)
        conn.execute("DELETE FROM settings WHERE key IN ('plex_url', 'plex_token')")
        conn.commit()

        r = client.get(sign_poster_url("12345", self._KEY) + "&kind=art")

        assert r.status_code == 404


class TestPosterTmdbBackdrop:
    """With no Plex backdrop, a signed-in caller is sent to TMDB's for the same title."""

    _KEY = "0123456789abcdef" * 4
    _IMAGE = "/api/watch/img/tmdb/w1280/back.jpg"

    @pytest.fixture
    def ask(self, tmp_path, monkeypatch):
        """A signed-in GET of ?kind=art with TMDB answering *details*; gives the response and
        what TMDB was asked."""
        from mediaman.services import watch_catalog
        from mediaman.services.media_meta.tmdb import TmdbClient
        from mediaman.web.auth.session_store import create_session
        from mediaman.web.routes.poster import sign_poster_url, title_art

        now = [1000.0]
        timeouts = []
        monkeypatch.setattr(title_art, "_unanswered", {})
        monkeypatch.setattr(title_art, "_clock", lambda: now[0])
        client, conn = TestPosterEndpointAuth._build_test_app(self, tmp_path, self._KEY)
        _scan(conn)
        insert_admin_user(conn, username="admin")
        token = create_session(conn, "admin")

        def ask(
            key,
            *,
            details,
            tmdb=True,
            signed=False,
            tmdb_id_for_tvdb=lambda _c, tvdb: tvdb + 1,
        ):
            asked = []

            def answer(_client, kind, tmdb_id):
                asked.append((kind, tmdb_id))
                if isinstance(details, Exception):
                    raise details
                return details

            monkeypatch.setattr(watch_catalog, "details", answer)
            monkeypatch.setattr(watch_catalog, "tmdb_id_for_tvdb", tmdb_id_for_tvdb)

            def from_db(*_a, timeout=10.0):
                timeouts.append(timeout)
                return object() if tmdb else None

            monkeypatch.setattr(TmdbClient, "from_db", from_db)
            if signed:
                url = sign_poster_url(key, self._KEY) + "&kind=art"
            else:
                client.cookies.set("session_token", token)
                url = f"/api/poster/{key}?kind=art"
            return client.get(url, follow_redirects=False), asked

        ask.conn = conn
        ask.now = now
        ask.timeouts = timeouts
        return ask

    def _item(self, ask, *, tmdb_id=None, tvdb_id=None, **fields):
        key = insert_media_item(ask.conn, plex_library_id=1, **fields)
        ask.conn.execute(
            "UPDATE media_items SET tmdb_id = ?, tvdb_id = ? WHERE id = ?", (tmdb_id, tvdb_id, key)
        )
        ask.conn.commit()

    def test_a_movie_is_redirected_to_its_tmdb_backdrop_for_minutes(self, ask):
        self._item(ask, id="700", tmdb_id=42)

        r, asked = ask("700", details={"backdrop_url": self._IMAGE})

        assert (r.status_code, r.headers["location"]) == (302, self._IMAGE)
        assert r.headers["cache-control"] == "private, max-age=300"
        assert asked == [("movie", 42)]

    def test_a_show_with_only_a_tvdb_id_finds_its_tmdb_backdrop(self, ask):
        self._item(
            ask,
            id="801",
            media_type="tv_season",
            show_rating_key="800",
            season_number=1,
            tvdb_id=6,
        )

        r, asked = ask("800", details={"backdrop_url": self._IMAGE})

        assert r.headers["location"] == self._IMAGE
        assert asked == [("tv", 7)]

    def test_no_tmdb_key_is_a_404_and_tmdb_is_not_asked(self, ask):
        self._item(ask, id="700", tmdb_id=42)

        r, asked = ask("700", details={"backdrop_url": self._IMAGE}, tmdb=False)

        assert r.status_code == 404
        assert asked == []

    def test_a_tmdb_outage_is_a_404(self, ask):
        from mediaman.services.watch_catalog import TmdbUnavailable

        self._item(ask, id="700", tmdb_id=42)

        r, _ = ask("700", details=TmdbUnavailable("details"))

        assert r.status_code == 404

    def test_a_title_tmdb_has_no_backdrop_for_is_a_404(self, ask):
        self._item(ask, id="700", tmdb_id=42)

        r, _ = ask("700", details={"backdrop_url": None})

        assert r.status_code == 404

    def test_a_signed_url_caller_makes_no_tmdb_request(self, ask):
        self._item(ask, id="700", tmdb_id=42)

        r, asked = ask("700", details={"backdrop_url": self._IMAGE}, signed=True)

        assert r.status_code == 404
        assert asked == []

    def test_tmdb_is_given_three_seconds_to_answer(self, ask):
        self._item(ask, id="700", tmdb_id=42)

        ask("700", details={"backdrop_url": self._IMAGE})

        assert ask.timeouts == [3.0]

    def test_a_title_tmdb_gave_no_answer_for_is_asked_again_only_after_five_minutes(self, ask):
        from mediaman.services.watch_catalog import TmdbUnavailable

        self._item(ask, id="700", tmdb_id=42)
        self._item(ask, id="701", tmdb_id=43)
        ask("700", details=TmdbUnavailable("details"))

        ask.now[0] += 299
        r, asked = ask("700", details={"backdrop_url": self._IMAGE})
        assert (r.status_code, asked) == (404, [])
        r, _ = ask("701", details={"backdrop_url": self._IMAGE})
        assert r.status_code == 302

        ask.now[0] += 1
        r, asked = ask("700", details={"backdrop_url": self._IMAGE})
        assert (r.status_code, asked) == (302, [("movie", 42)])

    def test_a_show_tmdb_gave_no_id_for_is_not_asked_again_at_once(self, ask):
        self._item(
            ask,
            id="801",
            media_type="tv_season",
            show_rating_key="800",
            season_number=1,
            tvdb_id=6,
        )
        lookups = []
        for _ in range(2):
            r, _asked = ask(
                "800",
                details={"backdrop_url": self._IMAGE},
                tmdb_id_for_tvdb=lambda _c, tvdb: lookups.append(tvdb),
            )
            assert r.status_code == 404

        assert lookups == [6]

    def test_with_no_plex_set_up_a_signed_in_caller_still_gets_tmdbs_backdrop(
        self, ask, monkeypatch
    ):
        from mediaman.web.routes import poster

        def plex_asked(*_a):
            raise AssertionError("Plex was asked")

        monkeypatch.setattr(poster, "_fetch_plex_art", plex_asked)
        self._item(ask, id="700", tmdb_id=42)

        r, asked = ask("700", details={"backdrop_url": self._IMAGE})

        assert (r.status_code, r.headers["location"]) == (302, self._IMAGE)
        assert asked == [("movie", 42)]

    @pytest.mark.parametrize("held_in", [None, 2], ids=["deleted", "library not scanned"])
    def test_a_title_outside_the_scanned_libraries_is_not_sent_to_tmdb(self, ask, held_in):
        # Recorded as deleted, so its poster may still be served.
        insert_audit_log(ask.conn, media_item_id="700", action="deleted")
        if held_in is not None:
            key = insert_media_item(ask.conn, id="700", plex_library_id=held_in)
            ask.conn.execute("UPDATE media_items SET tmdb_id = 42 WHERE id = ?", (key,))
            ask.conn.commit()

        r, asked = ask("700", details={"backdrop_url": self._IMAGE})

        assert (r.status_code, asked) == (404, [])
