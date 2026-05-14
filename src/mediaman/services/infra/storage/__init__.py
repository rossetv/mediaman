"""Filesystem operations — disk usage, deletion, and delete-root validation.

This package is the shared filesystem plumbing below every mediaman service.
It owns the most dangerous operation in the codebase — recursive deletion —
and the validation that gates it.

Threat model
------------
A buggy or compromised Plex part-path (``part.file``) could escalate a routine
library-cleanup job into a system-wide ``rmtree``. Two layers of defence
guard against that:

* **Forbidden-root allowlist refusal** — a deletion never starts unless the
  configured ``delete_allowed_roots`` allowlist is well-formed: absolute,
  non-symlink, and not pointing at a system / mount-root directory
  (:mod:`._delete_roots`).
* **Strict-descendant + same-device + no-symlink rules at delete time** — the
  target is re-resolved and re-checked, pinned to the root's filesystem
  device, and walked with fd-based ``os.fwalk(follow_symlinks=False)`` so a
  TOCTOU symlink swap cannot redirect the deletion (:mod:`.deletion`).

Package layout
--------------
The former single ``storage.py`` decomposes along the seam the
``services-infra`` audit named:

* :mod:`._delete_roots` — the path-safety exception hierarchy
  (:class:`PathSafetyError`, :class:`DeletionRefused`), :data:`_FORBIDDEN_ROOTS`,
  and the allowlist *validation* helpers.
* :mod:`.deletion` — the destructive operations :func:`delete_path` and
  :func:`_safe_rmtree`.
* :mod:`.disk_usage` — the read-only queries :func:`get_aggregate_disk_usage`
  and :func:`get_directory_size`.

This module is the public barrel: every name previously importable from
``mediaman.services.infra.storage`` (including the private helpers and
constants the tests reach for) stays importable from that exact path.

Lives under ``services/infra/`` because it is shared plumbing that every
service package depends on; it must not grow business logic.
"""

from __future__ import annotations

import logging

from mediaman.services.infra.storage._delete_roots import (
    _FORBIDDEN_ROOTS,
    DeletionRefused,
    PathSafetyError,
    _check_symlink_via_nofollow,
    _resolve_root_candidate,
    _validate_delete_roots,
    _validate_single_root,
)
from mediaman.services.infra.storage.deletion import (
    _safe_rmtree,
    delete_path,
)
from mediaman.services.infra.storage.disk_usage import (
    get_aggregate_disk_usage,
    get_directory_size,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_FORBIDDEN_ROOTS",
    "DeletionRefused",
    "PathSafetyError",
    "_check_symlink_via_nofollow",
    "_resolve_root_candidate",
    "_safe_rmtree",
    "_validate_delete_roots",
    "_validate_single_root",
    "delete_path",
    "get_aggregate_disk_usage",
    "get_directory_size",
]
