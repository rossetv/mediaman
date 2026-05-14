"""Settings routes package.

Thin aggregator over focused submodules:

* :mod:`.api`     — JSON API handlers: GET /api/settings, PUT /api/settings,
  POST /api/settings/test/{service}, GET /api/plex/libraries,
  GET /api/settings/disk-usage, plus their handler-private helpers.
* :mod:`.secrets` — field sets (SECRET_FIELDS, SENSITIVE_KEYS, etc.),
  masking helpers, and sentinel constants for unchanged/clear writes.
* :mod:`.testers` — per-service tester functions, the TESTERS registry,
  per-tester key allow-lists, and the in-memory result cache.
* :mod:`.core`    — URL-field validation helpers (_URL_FIELDS,
  validate_url_fields).

This barrel owns the ``GET /settings`` page handler and ``_load_settings``
(a documented monkeypatch target in the test suite), and aggregates the
``.api`` router so the registered route set is unchanged.

Callers should keep importing ``router`` from this package. The
``... import X as X`` block below re-exports the public constants and
helpers from the submodules so the flat path
``from mediaman.web.routes.settings import X`` keeps working for
everything that was accessible on the old single-file module — the test
suite patches several of these names on this module object.
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.services.arr.build import (
    build_plex_from_db as build_plex_from_db,  # re-exported for flat-import back-compat
)
from mediaman.services.infra import ConfigDecryptError
from mediaman.services.infra import (
    is_safe_outbound_url as is_safe_outbound_url,  # re-exported for patch targets
)
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import get_user_email as _get_user_email
from mediaman.web.repository.settings import (
    fetch_encrypted_key_set as _encrypted_keys,
)
from mediaman.web.repository.settings import (
    load_settings as _load_settings,
)

# ---------------------------------------------------------------------------
# Submodule imports — re-exported so the old flat module path still works.
# Tests monkeypatch attributes on this module object; everything they touch
# must live or be re-imported here.
# ---------------------------------------------------------------------------
from mediaman.web.routes.settings.api import (
    router as _api_router,
)
from mediaman.web.routes.settings.core import (
    _URL_FIELDS as _URL_FIELDS,
)
from mediaman.web.routes.settings.core import (
    _scrub_url_for_log as _scrub_url_for_log,
)
from mediaman.web.routes.settings.core import (
    validate_url_fields as _validate_url_fields,
)
from mediaman.web.routes.settings.secrets import (
    ALL_KEYS as _ALL_KEYS,
)
from mediaman.web.routes.settings.secrets import (
    SECRET_FIELDS as SECRET_FIELDS,  # re-exported for test imports
)
from mediaman.web.routes.settings.secrets import (
    SENSITIVE_KEYS as SENSITIVE_KEYS,
)
from mediaman.web.routes.settings.secrets import (
    has_sensitive_key_changes as _touches_sensitive_keys,
)
from mediaman.web.routes.settings.secrets import (
    mask_encrypted_keys as _mask_encrypted_keys,
)
from mediaman.web.routes.settings.testers import (
    SERVICE_TESTER_KEYS as _SERVICE_TESTER_KEYS,
)
from mediaman.web.routes.settings.testers import (
    TEST_CACHE as _TEST_CACHE,
)
from mediaman.web.routes.settings.testers import (
    TESTER_TIMEOUT_SECONDS as _TESTER_TIMEOUT_SECONDS,
)
from mediaman.web.routes.settings.testers import (
    TESTERS as _SERVICE_TESTERS,
)
from mediaman.web.routes.settings.testers import (
    cache_get as _cache_get,
)
from mediaman.web.routes.settings.testers import (
    cache_put as _cache_put,
)
from mediaman.web.routes.settings.testers import (
    invalidate_test_cache_for_keys as _invalidate_test_cache_for_keys,
)

# Re-export the cache dict under the name tests import directly.
TEST_CACHE = _TEST_CACHE

# Explicit public surface (§3.5): ``router`` is the only name outside callers
# need, but the leading-underscore and submodule re-exports below must stay
# importable from this package root for the test suite's flat-import and
# monkeypatch contract — listing them here marks them as intentional exports.
__all__ = [
    "SECRET_FIELDS",
    "SENSITIVE_KEYS",
    "TEST_CACHE",
    "_ALL_KEYS",
    "_SERVICE_TESTERS",
    "_SERVICE_TESTER_KEYS",
    "_TESTER_TIMEOUT_SECONDS",
    "_TEST_CACHE",
    "_URL_FIELDS",
    "_cache_get",
    "_cache_put",
    "_encrypted_keys",
    "_invalidate_test_cache_for_keys",
    "_load_settings",
    "_mask_encrypted_keys",
    "_scrub_url_for_log",
    "_touches_sensitive_keys",
    "_validate_url_fields",
    "build_plex_from_db",
    "is_safe_outbound_url",
    "router",
    "settings_page",
]

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    """Render the settings page. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Skip every encrypted secret — the page only ever shows '****' for
    # them, so decrypting just to throw the plaintext away is wasted
    # work and an unnecessary exposure window.
    enc_keys = _encrypted_keys(conn)
    config = request.app.state.config
    try:
        plain = _load_settings(
            conn,
            config.secret_key,
            keys=set(_ALL_KEYS) - enc_keys,
        )
    except ConfigDecryptError:
        plain = {}
    settings = _mask_encrypted_keys(plain, enc_keys)

    _libs_raw = settings.get("plex_libraries") or []
    plex_libraries_selected: list[str] = list(_libs_raw) if isinstance(_libs_raw, list) else []

    self_email = _get_user_email(conn, username)

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "username": username,
            "email": self_email,
            "nav_active": "settings",
            "settings": settings,
            "plex_libraries_selected": plex_libraries_selected,
        },
    )


# ---------------------------------------------------------------------------
# API routes — owned by ``.api``, aggregated here so the route set is unchanged.
# ---------------------------------------------------------------------------

router.include_router(_api_router)
