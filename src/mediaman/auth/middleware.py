"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.middleware`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.middleware as _real
from mediaman.web.auth.middleware import (
    PageSession,
    get_current_admin,
    get_optional_admin,
    get_optional_admin_from_token,
    resolve_page_session,
)

sys.modules[__name__] = _real
