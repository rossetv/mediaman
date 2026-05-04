"""Back-compat shim — module relocated to :mod:`mediaman.web.auth.password_hash`.

At runtime this module replaces itself in ``sys.modules`` with the canonical
module so that ``mediaman.auth.password_hash`` and
``mediaman.web.auth.password_hash`` are the identical object — patches and
mutable module state applied via either path are visible on both.
"""

# ruff: noqa: F401 — deliberate re-export shim; imports provide mypy visibility

from __future__ import annotations

import sys

import mediaman.web.auth.password_hash as _real
from mediaman.web.auth.password_hash import (
    _BCRYPT_MAX_INPUT_BYTES,
    _DUMMY_HASH,
    _DUMMY_HASH_LOCK,
    _LOG_FIELD_RE,
    BCRYPT_ROUNDS,
    UserRecord,
    _get_dummy_hash,
    _normalise_password,
    _prepare_bcrypt_input,
    _sanitise_log_field,
    authenticate,
    change_password,
    create_user,
    delete_user,
    list_users,
    set_must_change_password,
    user_must_change_password,
)

sys.modules[__name__] = _real
