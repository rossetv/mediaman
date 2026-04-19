"""Tests for the SSRF guard on admin-configured service URLs."""

from __future__ import annotations

import socket
from unittest.mock import patch

from mediaman.services.url_safety import is_safe_outbound_url


class TestSchemeValidation:
    def test_allows_http(self):
        assert is_safe_outbound_url("http://radarr.example.com")

    def test_allows_https(self):
        assert is_safe_outbound_url("https://radarr.example.com")

    def test_blocks_file_scheme(self):
        assert not is_safe_outbound_url("file:///etc/passwd")

    def test_blocks_gopher(self):
        assert not is_safe_outbound_url("gopher://localhost:70/")

    def test_blocks_ldap(self):
        assert not is_safe_outbound_url("ldap://directory.example.com")

    def test_blocks_ftp(self):
        assert not is_safe_outbound_url("ftp://ftp.example.com")

    def test_blocks_empty(self):
        assert not is_safe_outbound_url("")

    def test_blocks_garbage(self):
        assert not is_safe_outbound_url("not a url at all")


class TestLanAddressesAllowed:
    """RFC1918 addresses are the common case — do not block them."""

    def test_allows_192_168(self):
        assert is_safe_outbound_url("http://192.168.1.10:7878")

    def test_allows_10_x(self):
        assert is_safe_outbound_url("http://10.0.0.5:32400")

    def test_allows_172_16(self):
        assert is_safe_outbound_url("http://172.16.5.5:8989")

    def test_allows_loopback_literal(self):
        # localhost / 127.0.0.1 is fine too — self-hosted mediaman on
        # the same box as its services is a supported deployment.
        assert is_safe_outbound_url("http://127.0.0.1:7878")


class TestMetadataEndpointsBlocked:
    def test_blocks_aws_imds_literal(self):
        assert not is_safe_outbound_url(
            "http://169.254.169.254/latest/meta-data/"
        )

    def test_blocks_alibaba_metadata_literal(self):
        assert not is_safe_outbound_url("http://100.100.100.200/")

    def test_blocks_gcp_metadata_hostname(self):
        # Host-literal match — no DNS required.
        assert not is_safe_outbound_url(
            "http://metadata.google.internal/computeMetadata/v1/"
        )

    def test_blocks_dot_internal_suffix(self):
        assert not is_safe_outbound_url("http://admin.internal/")

    def test_blocks_hostname_resolving_to_metadata(self, monkeypatch):
        """An attacker could register a public DNS name that resolves to
        169.254.169.254. The resolver must catch it."""
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))
            ]
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        assert not is_safe_outbound_url("http://totally-innocent.example.com/")

    def test_blocks_hostname_with_any_bad_resolution(self, monkeypatch):
        """If *any* returned address is bad, the whole URL is refused."""
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0)),
            ]
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        assert not is_safe_outbound_url("http://dual-answer.example.com/")


class TestLinkLocalAndUnspecified:
    def test_blocks_link_local(self):
        assert not is_safe_outbound_url("http://169.254.1.5/")

    def test_blocks_ipv4_unspecified(self):
        assert not is_safe_outbound_url("http://0.0.0.0/")

    def test_blocks_ipv6_unspecified(self):
        assert not is_safe_outbound_url("http://[::]/")


class TestPublicHostnamesAllowed:
    def test_allows_public_hostname_when_resolution_clean(self, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        assert is_safe_outbound_url("http://radarr.example.com")

    def test_allows_public_hostname_when_resolution_fails(self, monkeypatch):
        """Resolution failure (DNS down, typo) should not block config —
        the admin is allowed to save a URL that will resolve later."""
        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror("Name or service not known")
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        assert is_safe_outbound_url("http://radarr.example.com")
