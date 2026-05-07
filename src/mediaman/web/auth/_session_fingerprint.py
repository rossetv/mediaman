"""Session-fingerprint binding (mode + IP/UA hash) for the session-store cookie."""

from __future__ import annotations

import hashlib
import ipaddress
import os

_FINGERPRINT_MODE_ENV = "MEDIAMAN_FINGERPRINT_MODE"
#: Supported fingerprint modes.  Each one trades resilience against
#: legitimate client churn for binding strength:
#:
#: ``off``    — no binding at all.  ``fingerprint`` is stored empty and
#:              the validate-side comparison is skipped.  Useful for
#:              deployments behind reverse-proxy farms that rewrite
#:              client IPs unpredictably or where every legitimate
#:              client is on a churn-heavy CGNAT.
#:
#: ``loose``  — IPv4 bucketed at ``/24`` and IPv6 at ``/64``; UA hash
#:              truncated to 16 hex chars.  This is the default.  It
#:              tolerates an end user roaming inside a single carrier
#:              CGNAT pool and minor UA churn (Chrome version bumps mid
#:              session) without invalidating the cookie, while still
#:              shutting down a stolen-cookie replay from a different
#:              network or a different browser family.
#:
#: ``strict`` — full client IP (no bucketing) and full SHA-256 UA hash
#:              (no truncation).  Maximum binding strength but
#:              intolerant of CGNAT IP rotation and any UA churn at
#:              all (User-Agent string changes, Chrome version bumps,
#:              switching from desktop to mobile UA on the same
#:              network).  Choose this when every legitimate client
#:              has a stable public IP and a stable UA.
_VALID_FINGERPRINT_MODES = {"strict", "loose", "off"}

#: Per-mode bucket configuration consumed by :func:`_client_fingerprint`.
#: ``ipv4_prefix`` / ``ipv6_prefix`` — CIDR length to bucket the client
#: IP at; ``None`` means "use the full address with no bucketing".
#: ``ua_hash_chars`` — number of leading hex chars of the SHA-256 UA
#: hash to keep; ``None`` means "use the full 64-char digest".
_FINGERPRINT_BUCKETS: dict[str, dict[str, int | None]] = {
    "loose": {"ipv4_prefix": 24, "ipv6_prefix": 64, "ua_hash_chars": 16},
    "strict": {"ipv4_prefix": None, "ipv6_prefix": None, "ua_hash_chars": None},
}


def _fingerprint_mode() -> str:
    """Return the current fingerprint mode from the environment."""
    mode = (os.environ.get(_FINGERPRINT_MODE_ENV) or "loose").lower()
    if mode not in _VALID_FINGERPRINT_MODES:
        return "loose"
    return mode


def _client_fingerprint(
    user_agent: str | None,
    client_ip: str | None,
    *,
    mode: str | None = None,
) -> str:
    """Compute a stable fingerprint for session-to-client binding.

    Dispatches on *mode* — defaults to the value of
    :func:`_fingerprint_mode` when the caller does not pin it.  ``off``
    is intentionally not handled here; create-/validate-side code
    branches on ``mode == 'off'`` before calling this helper.

    See :data:`_VALID_FINGERPRINT_MODES` for the documented trade-offs
    of each mode.
    """
    if mode is None:
        mode = _fingerprint_mode()
    bucket_cfg = _FINGERPRINT_BUCKETS.get(mode, _FINGERPRINT_BUCKETS["loose"])
    ua_hash_chars = bucket_cfg["ua_hash_chars"]
    full_ua_hash = hashlib.sha256((user_agent or "").encode()).hexdigest()
    ua_hash = full_ua_hash if ua_hash_chars is None else full_ua_hash[:ua_hash_chars]

    if not client_ip:
        prefix = "unknown"
    else:
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            prefix = "unknown"
        else:
            if isinstance(addr, ipaddress.IPv6Address):
                ipv6_prefix = bucket_cfg["ipv6_prefix"]
                if ipv6_prefix is None:
                    prefix = str(addr)
                else:
                    prefix = str(
                        ipaddress.ip_network(
                            f"{client_ip}/{ipv6_prefix}", strict=False
                        ).network_address
                    )
            else:
                ipv4_prefix = bucket_cfg["ipv4_prefix"]
                if ipv4_prefix is None:
                    prefix = str(addr)
                else:
                    prefix = str(
                        ipaddress.ip_network(
                            f"{client_ip}/{ipv4_prefix}", strict=False
                        ).network_address
                    )
    return f"{ua_hash}:{prefix}"
