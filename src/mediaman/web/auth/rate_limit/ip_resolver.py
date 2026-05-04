"""Back-compat shim — IP resolver moved to :mod:`mediaman.services.rate_limit.ip_resolver`.

Re-exports everything from the canonical location so existing imports
(e.g. ``from mediaman.auth.rate_limit import ip_resolver as ip_resolver_module``)
continue to resolve the same module-level LRU cache as the new path.
"""

# ruff: noqa: F401 — deliberate re-export facade.

from mediaman.services.rate_limit.ip_resolver import (
    _UNKNOWN_PEER,
    _cloudflare_proxies_cached,
    _ip_in_networks,
    _parse_proxy_env,
    _trusted_proxies_cached,
    clear_cache,
    cloudflare_proxies,
    get_client_ip,
    peer_is_trusted,
    trusted_proxies,
)

__all__ = [
    "clear_cache",
    "cloudflare_proxies",
    "get_client_ip",
    "peer_is_trusted",
    "trusted_proxies",
]
