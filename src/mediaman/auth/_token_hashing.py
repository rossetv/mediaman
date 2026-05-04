"""Back-compat shim — module relocated to :mod:`mediaman.web.auth._token_hashing`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so both import paths refer to the identical object.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth._token_hashing as _real
from mediaman.web.auth._token_hashing import _hash_token, hash_token

sys.modules[__name__] = _real
