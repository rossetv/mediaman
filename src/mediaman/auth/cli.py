"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.cli`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.cli as _real
from mediaman.web.auth.cli import (
    _prompt_username,
    _read_password_from_stdin,
    create_user_cli,
)

sys.modules[__name__] = _real
