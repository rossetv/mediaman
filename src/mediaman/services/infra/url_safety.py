"""Back-compat shim — canonical module relocated to mediaman.core.url_safety.

New code should import from ``mediaman.core.url_safety`` directly.
"""

from mediaman.core.url_safety import (  # noqa: F401
    _ALLOWED_SCHEMES,
    _BLOCKED_HOST_SUFFIXES,
    _BLOCKED_V4_NETS,
    _BLOCKED_V6_NETS,
    _METADATA_HOSTNAMES,
    _METADATA_IPS,
    _STRICT_BLOCKED_V4_NETS,
    _STRICT_BLOCKED_V6_NETS,
    _host_is_metadata,
    _ip_is_blocked,
    _normalise_host,
    _resolve_all,
    _strict_egress_enabled,
    is_safe_outbound_url,
    resolve_safe_outbound_url,
)
