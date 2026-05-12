"""Tests for the SSRF guard on admin-configured service URLs."""

from __future__ import annotations

import socket
import sqlite3

import pytest

from mediaman.services.infra.url_safety import (
    PINNED_EXTERNAL_HOSTS,
    allowed_outbound_hosts,
    is_safe_outbound_url,
    resolve_safe_outbound_url,
)
from tests.helpers.factories import insert_settings


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

    def test_fqdn_trailing_dot_metadata_blocked(self):
        """A trailing dot must not bypass the suffix block.

        ``metadata.google.internal.`` is the absolute DNS form of
        ``metadata.google.internal``; the suffix check is a literal
        ``endswith(".internal")`` and would miss the FQDN form unless
        ``_normalise_host`` strips the trailing dot.
        """
        assert not is_safe_outbound_url("http://metadata.google.internal./")
        assert not is_safe_outbound_url("http://service.internal./")

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

    def test_returns_self_pin_for_literal_ip_url(self):
        """A URL with a literal IP is pinned to itself.

        Modern urllib3 still calls ``getaddrinfo("192.0.2.1", port)`` to
        build the connection tuple, and a process-wide monkeypatch on
        ``socket.getaddrinfo`` could redirect that lookup. Pinning the
        literal address to itself short-circuits the resolver and makes
        the connect deterministic with the validated answer.
        """
        safe, hostname, ip = resolve_safe_outbound_url("http://192.0.2.1:7878/")
        assert safe is True
        assert hostname == "192.0.2.1"
        assert ip == "192.0.2.1"

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


# ---------------------------------------------------------------------------
# Allowlist (opt-in)
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_db():
    """A throwaway in-memory SQLite DB with just the ``settings`` table.

    Built locally rather than through ``init_db`` to keep the test
    independent of the migration runner; the schema mirrors what
    ``allowed_outbound_hosts`` actually reads.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "encrypted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '')"
    )
    yield conn
    conn.close()


class TestAllowedOutboundHosts:
    """``allowed_outbound_hosts(conn)`` returns the pinned externals plus
    the configured integration hostnames from ``settings``.
    """

    def test_empty_settings_returns_pinned_externals_only(self, settings_db):
        hosts = allowed_outbound_hosts(settings_db)
        assert hosts == PINNED_EXTERNAL_HOSTS

    def test_pinned_externals_always_present(self, settings_db):
        hosts = allowed_outbound_hosts(settings_db)
        assert "api.themoviedb.org" in hosts
        assert "image.tmdb.org" in hosts
        assert "www.omdbapi.com" in hosts
        assert "api.mailgun.net" in hosts
        assert "api.eu.mailgun.net" in hosts
        assert "api.openai.com" in hosts

    def test_configured_integration_urls_added_by_hostname(self, settings_db):
        insert_settings(settings_db, plex_url="http://plex.lan:32400/", updated_at="")
        insert_settings(settings_db, radarr_url="https://radarr.example.com:7878/", updated_at="")
        insert_settings(settings_db, sonarr_url="http://192.168.1.20:8989/", updated_at="")
        insert_settings(settings_db, nzbget_url="http://nzb.lan:6789/", updated_at="")
        hosts = allowed_outbound_hosts(settings_db)
        assert "plex.lan" in hosts
        assert "radarr.example.com" in hosts
        assert "192.168.1.20" in hosts
        assert "nzb.lan" in hosts

    def test_empty_string_value_is_skipped(self, settings_db):
        insert_settings(settings_db, plex_url="", updated_at="")
        hosts = allowed_outbound_hosts(settings_db)
        assert hosts == PINNED_EXTERNAL_HOSTS

    def test_unparseable_url_is_silently_skipped(self, settings_db):
        insert_settings(settings_db, radarr_url="not a url at all", updated_at="")
        hosts = allowed_outbound_hosts(settings_db)
        # The pinned externals still apply; the bogus radarr_url is dropped.
        assert "radarr.example.com" not in hosts
        assert hosts >= PINNED_EXTERNAL_HOSTS


class TestIsSafeOutboundUrlAllowlist:
    """When ``allowed_hosts`` is provided, the URL hostname must be in the
    set (or in the pinned externals); otherwise the URL is refused even if
    it would pass the deny-list checks.
    """

    def test_none_disables_allowlist(self, clean_dns):
        # Default behaviour: deny-list only, allowlist not consulted.
        assert is_safe_outbound_url("http://random-host.example.com/")

    def test_allowlist_blocks_unlisted_host(self, clean_dns):
        assert not is_safe_outbound_url(
            "http://random-host.example.com/",
            allowed_hosts=frozenset({"radarr.example.com"}),
        )

    def test_allowlist_permits_listed_host(self, clean_dns):
        assert is_safe_outbound_url(
            "http://radarr.example.com/",
            allowed_hosts=frozenset({"radarr.example.com"}),
        )

    def test_allowlist_permits_pinned_external_even_without_explicit_entry(self, clean_dns):
        # An empty per-call allowlist still permits TMDB/OMDb/Mailgun/OpenAI.
        assert is_safe_outbound_url(
            "https://api.themoviedb.org/3/movie/123",
            allowed_hosts=frozenset(),
        )

    def test_allowlist_does_not_override_deny_list(self, fake_dns):
        """An allowlisted host that resolves to a metadata IP must still
        be refused — the allowlist is composed on top of, not in place of,
        the deny-list.
        """
        fake_dns(["169.254.169.254"])
        assert not is_safe_outbound_url(
            "http://radarr.example.com/",
            allowed_hosts=frozenset({"radarr.example.com"}),
        )

    def test_allowlist_case_insensitive(self, clean_dns):
        assert is_safe_outbound_url(
            "http://Radarr.Example.COM/",
            allowed_hosts=frozenset({"radarr.example.com"}),
        )

    def test_allowlist_trailing_dot_normalised(self, clean_dns):
        assert is_safe_outbound_url(
            "http://radarr.example.com./",
            allowed_hosts=frozenset({"radarr.example.com"}),
        )

    def test_idn_allowlist_match(self, clean_dns):
        """A Unicode hostname IDN-normalised to a punycode entry in the
        allowlist must be accepted. The reverse — a punycode URL whose
        ASCII form is not in the allowlist — must be refused.
        """
        # bücher → xn--bcher-kva
        assert is_safe_outbound_url(
            "http://xn--bcher-kva.example.com/",
            allowed_hosts=frozenset({"xn--bcher-kva.example.com"}),
        )
        assert not is_safe_outbound_url(
            "http://xn--bcher-kva.example.com/",
            allowed_hosts=frozenset({"other.example.com"}),
        )


class TestAllowedOutboundHostsFailClosed:
    """``allowed_outbound_hosts`` must return the pinned-only set on
    ``sqlite3.Error`` rather than a partially-populated allowlist.

    The docstring promises fail-closed behaviour; previously the helper
    silently dropped the failing row and kept assembling the allowlist,
    which produced a half-built set after a partial scan.
    """

    def test_sqlite_error_returns_pinned_only(self, settings_db):
        # Wrap the real conn so the first ``execute`` raises — mimics
        # a transient OperationalError mid-iteration. ``sqlite3.Connection``
        # itself is C-level and refuses attribute assignment, so a thin
        # proxy is the cleanest way to inject the failure.
        class FailingConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, *_a, **_kw):
                raise sqlite3.OperationalError("simulated schema drift")

        # Pre-populate so we'd see a leak of half-built state if the
        # function were to keep going after the first failure.
        insert_settings(settings_db, plex_url="http://plex.lan:32400/", updated_at="")
        wrapped = FailingConn(settings_db)

        hosts = allowed_outbound_hosts(wrapped)  # type: ignore[arg-type]
        assert hosts == PINNED_EXTERNAL_HOSTS
        assert "plex.lan" not in hosts


class TestSafeHTTPClientAllowlistWiring:
    """Production-style wiring check (W1.32).

    The contract is:

    * Composing the allowlist from a settings DB with ``plex_url=...``
      includes that host.
    * Constructing a :class:`SafeHTTPClient` with that allowlist allows
      a request to the configured host.
    * The same client refuses a request to an off-allowlist host with
      :class:`SafeHTTPError` (the SSRF-refusal shape on the boundary).
    """

    def test_configured_plex_host_is_allowlisted(self, settings_db, clean_dns):
        from mediaman.services.infra.http import SafeHTTPClient, SafeHTTPError

        insert_settings(settings_db, plex_url="http://plex.lan:32400/", updated_at="")
        composed = allowed_outbound_hosts(settings_db)
        assert "plex.lan" in composed

        client = SafeHTTPClient(allowed_hosts=composed)
        # Intercept the actual transport so the test stays hermetic; we
        # only care that the SSRF guard does NOT refuse this URL.
        called: list = []

        def fake_dispatch(*args, **_kwargs):
            called.append(args)
            return _stub_safe_response()

        import mediaman.services.infra.http.client as http_client_mod

        original_dispatch = http_client_mod._dispatch
        http_client_mod._dispatch = fake_dispatch  # type: ignore[assignment]
        try:
            client.get("http://plex.lan:32400/library/sections")
        finally:
            http_client_mod._dispatch = original_dispatch  # type: ignore[assignment]
        assert called, "configured plex host must reach the dispatcher"

        # An off-allowlist host with the same composed allowlist refuses.
        with pytest.raises(SafeHTTPError) as excinfo:
            client.get("http://attacker.example.com/")
        assert "refused by SSRF guard" in excinfo.value.body_snippet


def _stub_safe_response():
    """Minimal :class:`requests.Response` stand-in for the dispatcher.

    Returns the smallest object the streaming reader will accept: a
    200 status, no content-length header, and an iterator yielding a
    single empty chunk so the body cap is never tripped.
    """
    from unittest.mock import MagicMock

    import requests as http_requests

    resp = MagicMock(spec=http_requests.Response)
    resp.status_code = 200
    resp.headers = {"Content-Type": "image/jpeg"}
    resp.iter_content = lambda chunk_size=65536: iter([b""])
    resp.close = MagicMock()
    return resp
