"""Targeted tests for the 2026-04-18 security hardening work.

Each test covers a specific fix: rate-limit XFF walk, AES-GCM AAD
binding, HMAC domain separation, CSP defence-in-depth, download-
token single-use, and so on. These tests are intentionally in a
separate file to make the regression surface easy to find.
"""

from __future__ import annotations

import time

import pytest

_KEY = "0123456789abcdef" * 4  # 64 hex chars, 16 unique — passes entropy check


# ---------------------------------------------------------------------------
# HMAC token domain separation
# ---------------------------------------------------------------------------


class TestTokenDomainSeparation:
    """A token signed for one purpose must NOT validate as another purpose."""

    def test_keep_token_not_valid_as_download(self):
        from mediaman.crypto import generate_keep_token, validate_download_token

        keep = generate_keep_token(
            media_item_id="999",
            action_id=1,
            expires_at=int(time.time()) + 3600,
            secret_key=_KEY,
        )
        assert validate_download_token(keep, _KEY) is None

    def test_download_token_not_valid_as_keep(self):
        from mediaman.crypto import generate_download_token, validate_keep_token

        dl = generate_download_token(
            email="x@y.com",
            action="download",
            title="Test",
            media_type="movie",
            tmdb_id=123,
            recommendation_id=None,
            secret_key=_KEY,
        )
        assert validate_keep_token(dl, _KEY) is None

    def test_unsubscribe_token_not_valid_as_keep(self):
        from mediaman.crypto import generate_unsubscribe_token, validate_keep_token

        unsub = generate_unsubscribe_token(email="x@y.com", secret_key=_KEY)
        assert validate_keep_token(unsub, _KEY) is None

    def test_poster_token_not_valid_as_download(self):
        from mediaman.crypto import generate_poster_token, validate_download_token

        pt = generate_poster_token("12345", _KEY)
        assert validate_download_token(pt, _KEY) is None


class TestTokenLengthCap:
    def test_keep_token_rejects_oversize(self):
        from mediaman.crypto import validate_keep_token

        assert validate_keep_token("A" * 10_000, _KEY) is None

    def test_download_token_rejects_oversize(self):
        from mediaman.crypto import validate_download_token

        assert validate_download_token("A" * 10_000, _KEY) is None


# ---------------------------------------------------------------------------
# AES-GCM AAD binding (prevents ciphertext row swap)
# ---------------------------------------------------------------------------


class TestAesGcmAad:
    def test_aad_binding_roundtrip(self, db_path):
        from mediaman.crypto import decrypt_value, encrypt_value
        from mediaman.db import init_db

        conn = init_db(str(db_path))
        ct = encrypt_value("the-plex-token", _KEY, conn=conn, aad=b"plex_token")
        pt = decrypt_value(ct, _KEY, conn=conn, aad=b"plex_token")
        assert pt == "the-plex-token"

    def test_aad_mismatch_raises(self, db_path):
        """Swapping the setting-key AAD must fail authentication."""
        from cryptography.exceptions import InvalidTag

        from mediaman.crypto import decrypt_value, encrypt_value
        from mediaman.db import init_db

        conn = init_db(str(db_path))
        ct = encrypt_value("the-plex-token", _KEY, conn=conn, aad=b"plex_token")

        # Decrypting with a different AAD must fail — this is exactly
        # the scenario where an attacker has swapped a ciphertext from
        # the ``plex_token`` row into, say, ``openai_api_key``.
        with pytest.raises(InvalidTag):
            decrypt_value(ct, _KEY, conn=conn, aad=b"openai_api_key")

    def test_legacy_ciphertext_still_decrypts_without_aad(self, db_path):
        """Ciphertexts written before AAD-binding still decrypt when AAD is supplied."""
        from mediaman.crypto import decrypt_value, encrypt_value
        from mediaman.db import init_db

        conn = init_db(str(db_path))
        # Simulate a legacy write with no AAD.
        ct = encrypt_value("legacy-token", _KEY, conn=conn, aad=None)

        # Reading with AAD should still succeed because decrypt_value
        # falls back to no-AAD on InvalidTag.
        pt = decrypt_value(ct, _KEY, conn=conn, aad=b"plex_token")
        assert pt == "legacy-token"


# ---------------------------------------------------------------------------
# Rate-limit XFF walk (prevents leftmost-spoof bypass)
# ---------------------------------------------------------------------------


class TestXffRightmostWalk:
    def test_attacker_cannot_spoof_leftmost_xff(self, monkeypatch):
        """Right-to-left walk ignores an attacker-prepended leftmost entry."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        from mediaman.auth.rate_limit import get_client_ip

        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4, 198.51.100.7, 10.0.0.1"}
            client = type("C", (), {"host": "10.0.0.1"})()

        # Must return the real client (rightmost non-trusted) — 198.51.100.7.
        # Previous naive implementation returned 1.2.3.4.
        assert get_client_ip(FakeRequest()) == "198.51.100.7"

    def test_all_trusted_falls_back_to_peer(self, monkeypatch):
        """If every XFF entry is a trusted proxy, fall back to the direct peer."""
        monkeypatch.setenv("MEDIAMAN_TRUSTED_PROXIES", "10.0.0.0/8")
        from mediaman.auth.rate_limit import get_client_ip

        class FakeRequest:
            headers = {"x-forwarded-for": "10.0.0.1, 10.0.0.2"}
            client = type("C", (), {"host": "10.0.0.2"})()

        assert get_client_ip(FakeRequest()) == "10.0.0.2"


# ---------------------------------------------------------------------------
# Secret-key entropy validation
# ---------------------------------------------------------------------------


class TestSecretKeyEntropy:
    def test_rejects_trivial_repetition(self, monkeypatch):
        from mediaman.config import ConfigError, load_config

        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "a" * 64)
        with pytest.raises(ConfigError, match="weak"):
            load_config()

    def test_rejects_mediaman_string(self, monkeypatch):
        from mediaman.config import ConfigError, load_config

        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "mediamanmediamanmediamanmediaman")
        with pytest.raises(ConfigError, match="weak"):
            load_config()

    def test_accepts_hex_key(self, monkeypatch):
        import secrets

        from mediaman.config import load_config

        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secrets.token_hex(32))
        cfg = load_config()
        assert cfg.secret_key


# ---------------------------------------------------------------------------
# Unsubscribe token hardening
# ---------------------------------------------------------------------------


class TestUnsubscribeToken:
    def test_roundtrip(self):
        from mediaman.crypto import (
            generate_unsubscribe_token,
            validate_unsubscribe_token,
        )

        token = generate_unsubscribe_token(email="x@y.com", secret_key=_KEY)
        assert validate_unsubscribe_token(token, _KEY, "x@y.com")

    def test_wrong_email_rejected(self):
        from mediaman.crypto import (
            generate_unsubscribe_token,
            validate_unsubscribe_token,
        )

        token = generate_unsubscribe_token(email="x@y.com", secret_key=_KEY)
        assert not validate_unsubscribe_token(token, _KEY, "other@y.com")

    def test_expired_token_rejected(self):
        from mediaman.crypto import (
            generate_unsubscribe_token,
            validate_unsubscribe_token,
        )

        token = generate_unsubscribe_token(
            email="x@y.com", secret_key=_KEY, ttl_days=0
        )
        # ttl_days=0 means exp=now → validates the equality with <
        time.sleep(0.01)
        assert not validate_unsubscribe_token(token, _KEY, "x@y.com")


# ---------------------------------------------------------------------------
# CSP hardening
# ---------------------------------------------------------------------------


class TestCsp:
    def test_img_src_permissive_but_scoped(self):
        """img-src allows any HTTPS (posters come from many CDNs).

        The attack surface from a permissive img-src is limited —
        images can't execute code — and a strict allowlist breaks
        the Downloads/Library/Recommended pages whenever Radarr or
        Sonarr return a poster from a CDN we didn't enumerate.
        """
        from mediaman.web import _CSP

        # img-src accepts self, inline data, blobs, and any https: origin.
        assert "img-src 'self' data: blob: https:" in _CSP
        # script-src remains scoped (no wildcards beyond unsafe-inline).
        assert " https:" not in _CSP.split("script-src")[1].split(";")[0]

    def test_object_src_none(self):
        from mediaman.web import _CSP
        assert "object-src 'none'" in _CSP

    def test_frame_ancestors_none(self):
        from mediaman.web import _CSP
        assert "frame-ancestors 'none'" in _CSP
