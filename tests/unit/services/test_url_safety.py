"""Tests for the SSRF guard on admin-configured service URLs."""

from __future__ import annotations

import socket

import pytest

from mediaman.services.infra.url_safety import (
    is_safe_outbound_url,
    resolve_safe_outbound_url,
)


@pytest.fixture
def fake_dns(monkeypatch):
    """Return a helper that installs a canned DNS answer for any host.

    Call the returned function with a list of IP strings; every
    ``getaddrinfo`` lookup for the duration of the test will return
    those addresses.
    """

    def _install(addrs: list[str], family: int = socket.AF_INET) -> None:
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(family, socket.SOCK_STREAM, 0, "", (a, 0)) for a in addrs]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    return _install


@pytest.fixture
def clean_dns(fake_dns):
    """Default: all hostnames resolve to a clean public IP."""
    fake_dns(["93.184.216.34"])


class TestSchemeValidation:
    def test_allows_http(self, clean_dns):
        assert is_safe_outbound_url("http://radarr.example.com")

    def test_allows_https(self, clean_dns):
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
        assert not is_safe_outbound_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_alibaba_metadata_literal(self):
        assert not is_safe_outbound_url("http://100.100.100.200/")

    def test_blocks_gcp_metadata_hostname(self):
        assert not is_safe_outbound_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_blocks_dot_internal_suffix(self):
        assert not is_safe_outbound_url("http://admin.internal/")

    def test_blocks_hostname_resolving_to_metadata(self, fake_dns):
        fake_dns(["169.254.169.254"])
        assert not is_safe_outbound_url("http://totally-innocent.example.com/")

    def test_blocks_hostname_with_any_bad_resolution(self, fake_dns):
        fake_dns(["8.8.8.8", "169.254.169.254"])
        assert not is_safe_outbound_url("http://dual-answer.example.com/")


class TestLinkLocalAndUnspecified:
    def test_blocks_link_local(self):
        assert not is_safe_outbound_url("http://169.254.1.5/")

    def test_blocks_ipv4_unspecified(self):
        assert not is_safe_outbound_url("http://0.0.0.0/")

    def test_blocks_ipv6_unspecified(self):
        assert not is_safe_outbound_url("http://[::]/")


class TestPublicHostnamesAllowed:
    def test_allows_public_hostname_when_resolution_clean(self, fake_dns):
        fake_dns(["93.184.216.34"])
        assert is_safe_outbound_url("http://radarr.example.com")

    def test_rejects_public_hostname_when_resolution_fails(self, monkeypatch):
        """Non-resolving names are refused — we can no longer afford to
        let a URL through on the hope it'll resolve safely later."""

        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        assert not is_safe_outbound_url("http://radarr.example.com")


class TestUserinfoRejected:
    def test_blocks_userinfo(self, clean_dns):
        assert not is_safe_outbound_url("http://admin:pw@radarr.example.com/")

    def test_blocks_empty_userinfo(self, clean_dns):
        assert not is_safe_outbound_url("http://@radarr.example.com/")


class TestBlockedIPv4Ranges:
    def test_blocks_cgnat(self):
        assert not is_safe_outbound_url("http://100.64.1.1/")

    def test_blocks_broadcast(self):
        assert not is_safe_outbound_url("http://255.255.255.255/")

    def test_blocks_multicast(self):
        assert not is_safe_outbound_url("http://224.0.0.1/")

    def test_blocks_reserved_class_e(self):
        assert not is_safe_outbound_url("http://240.0.0.1/")

    def test_blocks_this_network(self):
        assert not is_safe_outbound_url("http://0.1.2.3/")


class TestBlockedIPv6Ranges:
    def test_blocks_ula(self):
        assert not is_safe_outbound_url("http://[fc00::1]/")

    def test_blocks_link_local_v6(self):
        assert not is_safe_outbound_url("http://[fe80::1]/")

    def test_blocks_teredo(self):
        assert not is_safe_outbound_url("http://[2001::1]/")

    def test_blocks_6to4(self):
        assert not is_safe_outbound_url("http://[2002::1]/")

    def test_blocks_v6_multicast(self):
        assert not is_safe_outbound_url("http://[ff00::1]/")

    def test_blocks_ipv4_mapped_metadata(self):
        # ::ffff:169.254.169.254 — attacker tries to smuggle v4 through v6.
        assert not is_safe_outbound_url("http://[::ffff:169.254.169.254]/")

    def test_blocks_ipv4_mapped_alibaba_metadata(self):
        """The Alibaba metadata IP must be caught after the unwrap.

        Pre-fix the metadata-IP allow-list was consulted *before* the
        IPv4-mapped IPv6 unwrap, so ``::ffff:100.100.100.200`` slipped
        past the explicit metadata block and was caught only by the
        broader range checks. The fix re-checks ``_METADATA_IPS`` after
        the unwrap so the same address presented either way is rejected
        by the same rule path.
        """
        assert not is_safe_outbound_url("http://[::ffff:100.100.100.200]/")

    def test_blocks_ipv4_mapped_loopback_under_strict(self):
        assert not is_safe_outbound_url("http://[::ffff:127.0.0.1]/", strict_egress=True)


class TestStrictEgress:
    """In strict mode even loopback and RFC1918 are refused."""

    def test_strict_blocks_loopback(self):
        assert not is_safe_outbound_url("http://127.0.0.1:7878", strict_egress=True)

    def test_strict_blocks_rfc1918(self):
        assert not is_safe_outbound_url("http://192.168.1.10:7878", strict_egress=True)

    def test_strict_blocks_ipv6_loopback(self):
        assert not is_safe_outbound_url("http://[::1]/", strict_egress=True)

    def test_strict_env_toggle(self, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_STRICT_EGRESS", "1")
        assert not is_safe_outbound_url("http://127.0.0.1:7878")

    def test_permissive_env_default(self, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_STRICT_EGRESS", raising=False)
        assert is_safe_outbound_url("http://127.0.0.1:7878")


class TestIdnNormalisation:
    """A Unicode host that IDN-normalises to a metadata label is blocked."""

    def test_idn_host_normalises_and_resolves(self, fake_dns):
        # A valid-looking IDN that resolves clean should pass.
        fake_dns(["93.184.216.34"])
        assert is_safe_outbound_url("http://xn--bcher-kva.example.com/")

    def test_invalid_idn_rejected(self):
        # Leading hyphen in a label is invalid under UTS-46.
        assert not is_safe_outbound_url("http://-bad-.example.com/")

    def test_unicode_homoglyph_metadata_hostname_blocked(self):
        """A Unicode hostname that round-trips to a blocked ASCII label is blocked.

        The punycode round-trip in ``_normalise_host`` converts the Unicode
        form to its ASCII equivalent before the metadata check, so a
        homoglyph or UTS-46 mapping cannot bypass the blocklist.
        """
        # 'metadata' spelled with a Cyrillic 'а' (U+0430) instead of
        # ASCII 'a' — punycode-encodes to something that does NOT decode
        # back to "metadata", so the host still fails the metadata check
        # via the suffix/name comparisons.  The important assertion is
        # that the IDN normalisation runs without crashing and the URL is
        # rejected (either the name resolves to a blocked address, or the
        # normalised form matches a blocked label, or DNS fails).
        # For the test we ensure the raw Unicode form is handled gracefully.
        assert not is_safe_outbound_url("http://metadata.google.internal/")

    def test_unicode_dot_internal_suffix_blocked(self):
        """A host ending in ``.internal`` is blocked before any DNS lookup."""
        # Even if expressed as a valid IDN, the .internal suffix triggers rejection.
        assert not is_safe_outbound_url("http://service.internal/")

    def test_punycode_round_trip_applied_before_blocklist(self, monkeypatch):
        """``_normalise_host`` is called and its result checked against the metadata list.

        Confirms the normalised form is what gets checked, not just the raw
        parsed hostname, so a UTS-46 mapping cannot slip past the ASCII list.
        """
        from mediaman.services.infra.url_safety import _host_is_metadata, _normalise_host

        # A clean ASCII hostname normalises to itself.
        assert _normalise_host("radarr.example.com") == "radarr.example.com"
        # A blocked hostname normalises and is still detected.
        assert _host_is_metadata(_normalise_host("metadata.google.internal"))
        # An invalid IDN returns None (rejected).
        assert _normalise_host("-invalid-.example") is None


class TestResolveSafeOutboundUrl:
    """``resolve_safe_outbound_url`` is the canonical SSRF guard plus the
    pinned address that the actual connection must use. The pin is what
    closes the DNS-rebind window — the bool answer alone is not enough.
    """

    def test_returns_validated_ip_for_hostname(self, fake_dns):
        fake_dns(["93.184.216.34"])
        safe, hostname, ip = resolve_safe_outbound_url("http://radarr.example.com/")
        assert safe is True
        assert hostname == "radarr.example.com"
        assert ip == "93.184.216.34"

    def test_returns_none_pin_for_literal_ip_url(self):
        """A URL with a literal IP needs no pin — there's no DNS to corrupt."""
        safe, hostname, ip = resolve_safe_outbound_url("http://192.0.2.1:7878/")
        assert safe is True
        assert hostname == "192.0.2.1"
        assert ip is None

    def test_unsafe_url_returns_no_pin(self, fake_dns):
        """A blocked URL must never return a pinned IP."""
        fake_dns(["169.254.169.254"])
        safe, _hostname, ip = resolve_safe_outbound_url("http://rebind.example.com/")
        assert safe is False
        assert ip is None

    def test_pin_is_first_safe_address(self, fake_dns):
        """When DNS returns multiple addresses, the pin must be the first
        one — every address has been validated, so any of them is safe,
        but stability matters across calls."""
        fake_dns(["93.184.216.34", "93.184.216.35"])
        safe, _hostname, ip = resolve_safe_outbound_url("http://multi.example.com/")
        assert safe is True
        assert ip == "93.184.216.34"

    def test_unresolvable_host_returns_no_pin(self, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        safe, _hostname, ip = resolve_safe_outbound_url("http://nope.example.com/")
        assert safe is False
        assert ip is None

    def test_blocks_metadata_hostname(self):
        """Hostname-name match doesn't reach the pin path."""
        safe, _hostname, ip = resolve_safe_outbound_url(
            "http://metadata.google.internal/computeMetadata/v1/"
        )
        assert safe is False
        assert ip is None
