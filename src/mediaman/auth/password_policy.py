"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.password_policy`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.password_policy as _real
from mediaman.web.auth.password_policy import (
    MAX_BYTES,
    MIN_LENGTH,
    MIN_UNIQUE,
    PASSPHRASE_MIN_LENGTH,
    PASSPHRASE_MIN_UNIQUE,
    _char_classes,
    _is_sequential,
    _load_common_passwords,
    _looks_like_passphrase,
    is_strong,
    password_issues,
    policy_summary,
)

sys.modules[__name__] = _real
