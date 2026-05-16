"""Subscriber routes — admin CRUD + newsletter send + public unsubscribe.

Package layout
--------------
``_admin.py``
    Admin-authenticated endpoints: subscriber list/add/remove and
    manual newsletter send.

``_unsubscribe.py``
    Public HMAC-token-authenticated unsubscribe page + confirm
    handler (CSRF-exempt; cross-origin from email clients).

``__init__.py`` (this module)
    Combines the two sub-routers into the single ``router`` mounted by
    the app factory and re-exports the names that tests reach for at the
    ``mediaman.web.routes.subscribers.<name>`` path.
"""

from __future__ import annotations

from fastapi import APIRouter

from mediaman.services.rate_limit.instances import (
    NEWSLETTER_LIMITER as _NEWSLETTER_LIMITER,
)
from mediaman.services.rate_limit.instances import (
    SUBSCRIBER_WRITE_LIMITER as _SUBSCRIBER_WRITE_LIMITER,
)
from mediaman.web.routes.subscribers._admin import (
    _resolve_newsletter_recipients,
    _validate_email,
)
from mediaman.web.routes.subscribers._admin import (
    router as _admin_router,
)
from mediaman.web.routes.subscribers._unsubscribe import (
    _UNSUB_LIMITER,
)
from mediaman.web.routes.subscribers._unsubscribe import (
    router as _unsubscribe_router,
)

__all__ = [
    "_NEWSLETTER_LIMITER",
    "_SUBSCRIBER_WRITE_LIMITER",
    "_UNSUB_LIMITER",
    "_resolve_newsletter_recipients",
    "_validate_email",
    "router",
]

router = APIRouter()
router.include_router(_admin_router)
router.include_router(_unsubscribe_router)
