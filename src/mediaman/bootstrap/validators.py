"""Pure validation helpers used at startup.

These functions validate operator-supplied configuration values (scan
schedule settings, worker count, trusted-proxy CIDRs) and raise
:class:`ValueError` or :class:`RuntimeError` on bad input so the caller
sees a clear, actionable message instead of an opaque downstream failure.

They have no side effects and no imports beyond the standard library, so
they are easy to unit-test and safe to call from any layer.

Exported validators
-------------------
- :func:`validate_scan_time` — parse ``HH:MM`` into ``(hour, minute)``
- :func:`validate_scan_day` — validate weekday token(s) for APScheduler
- :func:`validate_scan_timezone` — confirm an IANA timezone string
- :func:`validate_sync_interval` — parse and bound a sync interval in minutes
- :func:`enforce_single_worker` — refuse multi-worker uvicorn deployments
- :func:`sanitise_trusted_proxies` — strip wildcard/invalid proxy CIDRs
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_SCAN_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

# APScheduler accepts either a single weekday token or a comma-separated
# list. We deliberately allow only canonical short names so a typo like
# "moon" trips early instead of silently producing a never-firing trigger.
_VALID_DAY_TOKENS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})

# Wildcard tokens uvicorn happily accepts but mediaman refuses: both
# expand "trust this proxy" to "trust every peer", and uvicorn's
# proxy_headers handler rewrites ``request.client.host`` from the
# X-Forwarded-For header before our rate-limiter sees it.
_FORBIDDEN_TRUSTED_PROXY_TOKENS = frozenset({"*", "0.0.0.0/0", "::/0"})


def validate_scan_time(s: str) -> tuple[int, int]:
    """Parse and validate a scan time string in ``HH:MM`` 24-hour format.

    Returns ``(hour, minute)`` on success. Raises :class:`ValueError`
    with a descriptive message on any invalid input so the operator sees
    a clear startup error rather than a silent misconfiguration.

    Validation is two-stage: a regex confirms the shape, then
    :func:`datetime.strptime` confirms the value is a real time (e.g.
    ``"25:00"`` would pass the regex but fail strptime).
    """
    if not _SCAN_TIME_RE.match(s):
        raise ValueError(
            f"scan_time {s!r} is invalid — expected HH:MM in 24-hour format (e.g. '09:00')"
        )
    try:
        dt = datetime.strptime(s, "%H:%M")
    except ValueError as exc:
        raise ValueError(
            f"scan_time {s!r} is not a valid time — expected HH:MM in 24-hour format"
        ) from exc
    return dt.hour, dt.minute


def validate_scan_day(s: str) -> str:
    """Reject scan_day values APScheduler can't parse.

    Accepts either a single token (``"mon"``) or a comma-separated list
    (``"mon,wed,fri"``). Tokens are normalised to lowercase short
    weekday names; anything else raises :class:`ValueError`.
    """
    raw = (s or "").strip().lower()
    if not raw:
        raise ValueError("scan_day must not be empty")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"scan_day {s!r} is invalid — expected one of {sorted(_VALID_DAY_TOKENS)}")
    bad = [p for p in parts if p not in _VALID_DAY_TOKENS]
    if bad:
        raise ValueError(
            f"scan_day {s!r} contains unknown weekday token(s): {bad!r} — "
            f"expected one or more of {sorted(_VALID_DAY_TOKENS)}"
        )
    return ",".join(parts)


def validate_scan_timezone(s: str) -> str:
    """Reject scan_timezone values that aren't IANA timezones.

    Uses :class:`zoneinfo.ZoneInfo` to confirm the string resolves;
    raises :class:`ValueError` with a clear message otherwise so the
    operator sees the startup failure instead of an opaque
    APScheduler exception once the cron trigger fires.
    """
    raw = (s or "").strip()
    if not raw:
        raise ValueError("scan_timezone must not be empty")
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        # Standard library should always provide it; defer if unavailable.
        return raw
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"scan_timezone {raw!r} is not a known IANA timezone") from exc
    except (KeyError, ValueError) as exc:
        raise ValueError(f"scan_timezone {raw!r} is invalid: {exc}") from exc
    return raw


def validate_sync_interval(s: str) -> int:
    """Parse and bound ``library_sync_interval`` (minutes).

    Returns the integer minute count. Refuses zero or negative values
    so the scheduler doesn't degenerate into a tight loop.
    """
    try:
        value = int(s)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"library_sync_interval {s!r} is invalid — expected a positive integer (minutes)"
        ) from exc
    if value <= 0:
        raise ValueError(f"library_sync_interval must be a positive integer (got {value})")
    if value > 24 * 60:
        raise ValueError(f"library_sync_interval must be at most 1440 minutes (got {value})")
    return value


def enforce_single_worker() -> None:
    """Refuse to start under multi-worker uvicorn.

    Several invariants in mediaman assume a single process holds the
    SQLite connection: the APScheduler instance, the in-memory rate
    limits, and the search-trigger throttle would all duplicate or race
    if a second worker booted up. Token replay is now backed by SQLite
    and would survive, but the rest is not yet ready for horizontal scale,
    so we fail loudly instead of degrading silently.

    Reads ``MEDIAMAN_WORKERS``, ``UVICORN_WORKERS`` (uvicorn respects it),
    and ``WORKERS`` so an operator who exports any of them by accident
    sees an immediate error rather than a half-broken deployment.
    Unparseable values (``WORKERS=auto``, ``WORKERS=$()``, a stray
    comment) log a WARNING so a typo cannot land silently as "unset".
    """
    candidates = ("MEDIAMAN_WORKERS", "UVICORN_WORKERS", "WORKERS")
    for name in candidates:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring %s=%r — value is not an integer. Set %s to 1 (or "
                "unset it) to silence this warning; mediaman requires a "
                "single worker.",
                name,
                raw,
                name,
            )
            continue
        if value > 1:
            logger.error(
                "Refusing to start: %s=%d but mediaman requires WORKERS=1. "
                "Several invariants (scheduler, rate-limits, in-process "
                "throttles) assume a single process — multi-worker support "
                "would silently corrupt them. Run multiple replicas behind "
                "your reverse proxy instead, or unset %s.",
                name,
                value,
                name,
            )
            raise RuntimeError(
                f"mediaman requires a single worker; {name}={value} is not supported"
            )


def sanitise_trusted_proxies(raw: str) -> str:
    """Return a sanitised ``forwarded_allow_ips`` value or empty string.

    Uvicorn accepts ``"*"`` and ``"0.0.0.0/0"`` as "trust every peer".
    The internal IP-resolver tries to parse ``"*"``, fails, and returns
    an empty list — but uvicorn has ALREADY mutated
    ``request.client.host`` from the ``X-Forwarded-For`` header by then.
    The result is a per-IP rate-limit that buckets every request on an
    attacker-supplied address.

    Sanitisation rules:

    * Reject the literal wildcards in :data:`_FORBIDDEN_TRUSTED_PROXY_TOKENS`
      with a ``CRITICAL`` log line; return the empty string so
      the caller falls through to the proxy-headers-OFF branch.
    * Drop any entry that fails :class:`ipaddress.ip_network` parsing
      (single IPs are accepted because ``ip_network('10.0.0.1')`` is a
      valid /32 network); log a WARNING per dropped entry so a typo is
      visible.
    * Return the surviving entries comma-joined; uvicorn accepts that
      shape unchanged.

    Empty/whitespace input returns the empty string — caller treats that
    as "no proxy trust configured".
    """
    if not raw or not raw.strip():
        return ""

    cleaned: list[str] = []
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        if token in _FORBIDDEN_TRUSTED_PROXY_TOKENS:
            logger.critical(
                "Refusing wildcard MEDIAMAN_TRUSTED_PROXIES entry %r — "
                "this would let any peer set X-Forwarded-For and bypass "
                "per-IP rate limits. Drop it from the env var; only "
                "specific reverse-proxy IPs/CIDRs are accepted.",
                token,
            )
            continue
        try:
            ipaddress.ip_network(token, strict=False)
        except ValueError:
            logger.warning(
                "Ignoring invalid MEDIAMAN_TRUSTED_PROXIES entry %r — "
                "not a valid IP address or CIDR.",
                token,
            )
            continue
        cleaned.append(token)
    return ",".join(cleaned)
