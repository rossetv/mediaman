"""Pydantic models for web API request/response validation.

Historically this lived in a single ``models.py`` module.  It was split
into a package as the surface grew (Domain-04 hardening pushed
``SettingsUpdate`` past 200 LOC on its own).  All public symbols are
re-exported from this package's ``__init__`` so that
``from mediaman.web.models import X`` continues to work for every
caller — see ``tests/unit/web/test_models_hardening.py`` and the
``mediaman.web.routes.*`` modules for the existing import surface.
"""

from __future__ import annotations

# Private re-exports — these are imported by callers outside the
# package (``_API_KEY_RE`` by ``mediaman.web.routes.settings`` and
# ``_reject_crlf`` by ``tests/unit/web/test_models_hardening.py``).
# Keeping them here preserves the historical import surface.
from ._common import _API_KEY_RE as _API_KEY_RE
from ._common import ACTION_PROTECTED_FOREVER as ACTION_PROTECTED_FOREVER
from ._common import ACTION_SCHEDULED_DELETION as ACTION_SCHEDULED_DELETION
from ._common import ACTION_SNOOZED as ACTION_SNOOZED
from ._common import VALID_KEEP_DURATIONS as VALID_KEEP_DURATIONS
from ._common import _reject_crlf as _reject_crlf
from .auth import KeepRequest as KeepRequest
from .auth import LoginRequest as LoginRequest
from .settings import DiskThresholds as DiskThresholds
from .settings import SettingsUpdate as SettingsUpdate
from .subscribers import SubscriberCreate as SubscriberCreate

__all__ = [
    # Action constants
    "ACTION_PROTECTED_FOREVER",
    "ACTION_SCHEDULED_DELETION",
    "ACTION_SNOOZED",
    # Vocabulary
    "VALID_KEEP_DURATIONS",
    # Public models
    "DiskThresholds",
    "KeepRequest",
    "LoginRequest",
    "SettingsUpdate",
    "SubscriberCreate",
]
