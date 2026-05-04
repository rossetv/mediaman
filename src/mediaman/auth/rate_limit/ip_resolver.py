"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.rate_limit.ip_resolver`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.rate_limit.ip_resolver as _real
from mediaman.web.auth.rate_limit.ip_resolver import (
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

sys.modules[__name__] = _real
