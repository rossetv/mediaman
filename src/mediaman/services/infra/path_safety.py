"""Safe filesystem path resolution for admin-controlled path inputs.

The disk-usage endpoint accepts a path string from the client.  These
helpers enforce that the resolved path sits within a known-safe root and
that no symlink in the chain points outside of it — preventing symlink
traversal from escaping the allowed tree.
"""

from __future__ import annotations

import os
from pathlib import Path


def disk_usage_allowed_roots() -> list[Path]:
    """Return the filesystem root paths the disk-usage endpoint may stat.

    Roots are sourced from:

    * ``MEDIAMAN_DELETE_ROOTS`` — comma-separated list of paths (same env
      var used by the scanner).
    * ``MEDIAMAN_DATA_DIR`` — the container data directory.
    * ``/media`` and ``/data`` — conventional mount points in Docker
      deployments.
    """
    roots: list[Path] = []

    def _try_add(raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            p = Path(raw).resolve()
        except (OSError, ValueError):
            return
        roots.append(p)

    for token in (os.environ.get("MEDIAMAN_DELETE_ROOTS") or "").split(","):
        _try_add(token)

    _try_add(os.environ.get("MEDIAMAN_DATA_DIR", ""))
    roots.append(Path("/media"))
    roots.append(Path("/data"))
    return roots


def resolve_safe_path(raw: str, roots: list[Path]) -> Path | None:
    """Resolve *raw* and verify it is safe to stat.

    Returns the resolved :class:`~pathlib.Path` if it sits within one of
    *roots*, or ``None`` if:

    * *raw* cannot be parsed as a path.
    * Any component in the chain is a symlink (symlink traversal is
      blocked entirely rather than followed).
    * The resolved path is not a descendant of any allowed root.
    """
    try:
        candidate = Path(raw)
        abs_candidate = Path(os.path.abspath(str(candidate)))
    except (OSError, ValueError):
        return None

    built = Path(abs_candidate.anchor)
    for part in abs_candidate.parts[1:]:
        built = built / part
        try:
            if built.is_symlink():
                return None
        except (OSError, PermissionError):
            return None

    try:
        resolved = abs_candidate.resolve()
    except (OSError, ValueError):
        return None

    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved

    return None
