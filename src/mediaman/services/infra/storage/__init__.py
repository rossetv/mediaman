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
  allowlist of delete folders (``MEDIAMAN_DELETE_ROOTS``) is well-formed: absolute,
  non-symlink, and not pointing at a system / mount-root directory
  (``_delete_roots``).
* **Strict-descendant + same-device + no-symlink rules at delete time** — the
  target is re-resolved and re-checked, pinned to the root's filesystem
  device, and walked with fd-based ``os.fwalk(follow_symlinks=False)`` so a
  TOCTOU symlink swap cannot redirect the deletion (``deletion``).

Package layout
--------------
The package is split into:

* ``_delete_roots`` — the path-safety exception hierarchy
  (``PathSafetyError``, ``DeletionRefused``), ``_FORBIDDEN_ROOTS``,
  and the allowlist *validation* helpers.
* ``deletion`` — the destructive operations: ``delete_path`` (its
  private recursive remover ``_safe_rmtree`` is intentionally not exported),
  ``delete_tracked_files`` (an item's own files and their sidecars only) and
  ``remove_empty_folders`` (folders those removals left bare).
* ``disk_usage`` — the read-only queries ``get_aggregate_disk_usage``
  and ``get_directory_size``.
* ``disk_reads`` — the deadline a request thread puts on such a read.

This module is the public barrel: every name, including the private helpers
and constants the tests reach for, is importable from
``mediaman.services.infra.storage`` itself.

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
    TrackedFilesResult,
    delete_path,
    delete_tracked_files,
    path_within_delete_roots,
    remove_empty_folders,
    unavailable_folder,
)
from mediaman.services.infra.storage.disk_usage import (
    get_aggregate_disk_usage,
    get_directory_size,
    get_disk_usage_for_paths,
    is_same_disk,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_FORBIDDEN_ROOTS",
    "DeletionRefused",
    "PathSafetyError",
    "TrackedFilesResult",
    "_check_symlink_via_nofollow",
    "_resolve_root_candidate",
    "_validate_delete_roots",
    "_validate_single_root",
    "delete_path",
    "delete_tracked_files",
    "get_aggregate_disk_usage",
    "get_directory_size",
    "get_disk_usage_for_paths",
    "is_same_disk",
    "path_within_delete_roots",
    "remove_empty_folders",
    "unavailable_folder",
]
