"""Back-compat shim — the HTTP client has moved to ``services/infra/http/``.

All public and private names previously defined here are re-exported so
existing import paths continue to work without modification:

    from mediaman.services.infra.http_client import SafeHTTPClient  # still works
    from mediaman.services.infra.http_client import pin_dns_for_request  # still works

For new code, import from the canonical location instead:

    from mediaman.services.infra.http import SafeHTTPClient

Patchability note
-----------------
Tests use ``monkeypatch.setattr(http_client, "_dispatch", ...)`` and
``monkeypatch.setattr(http_client, "_ORIG_GETADDRINFO", ...)``.
``client._request`` and ``dns_pinning._patched_getaddrinfo`` resolve these
names at call time via ``sys.modules["mediaman.services.infra.http_client"]``
so that monkeypatching this module's namespace takes effect correctly.
"""

import time  # noqa: F401 — tests patch http_client.time.sleep

from mediaman.core.url_safety import resolve_safe_outbound_url  # noqa: F401
from mediaman.services.infra.http import *  # noqa: F403
from mediaman.services.infra.http.client import (  # noqa: F401
    _DEFAULT_MAX_BYTES,
    _DEFAULT_TIMEOUT,
    _USER_AGENT,
    SafeHTTPClient,
    SafeHTTPError,
    _dispatch,
)
from mediaman.services.infra.http.dns_pinning import (  # noqa: F401
    _DNS_PIN_LOCAL,
    _ORIG_GETADDRINFO,
    _patched_getaddrinfo,
    ensure_hook_installed,
    pin,
    pin_dns_for_request,
)
from mediaman.services.infra.http.retry import (  # noqa: F401
    _BODY_SNIPPET_BYTES,
    _RETRY_AFTER_MAX_SECONDS,
    _RETRY_AFTER_STATUSES,
    _RETRY_BACKOFFS,
    _RETRYABLE_EXCEPTIONS,
    _RETRYABLE_STATUSES,
    _retry_after_seconds,
)
from mediaman.services.infra.http.streaming import (  # noqa: F401
    _ContentTypeMismatch,
    _read_capped,
    _SizeCapExceeded,
)
