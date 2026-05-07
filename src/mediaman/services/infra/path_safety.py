"""Safe filesystem path resolution for admin-controlled path inputs.

The disk-usage endpoint accepts a path string from the client.  These
helpers enforce that the resolved path sits within a known-safe root and
that no symlink in the chain points outside of it — preventing symlink
traversal from escaping the allowed tree.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_delete_roots_env(env_val: str) -> list[str]:
    """Parse ``MEDIAMAN_DELETE_ROOTS`` using the canonical colon/comma rules.

    The canonical separator is ``':'`` (PATH-style).  A legacy ``','``
    separator is accepted with a deprecation warning.  Mixed separators are
    accepted but logged as an error because they almost always indicate a
    misconfiguration.

    Returns the list of non-empty stripped path strings.  An empty list means
    the env var was set but yielded no usable paths.

    This function is the single source of truth for separator handling so that
    the disk-usage path and the deletion path both behave identically.
    Callers in ``scanner/repository.py`` should be migrated to use this helper;
    a follow-up commit on ``scanner/repository.py`` is needed by the orchestrator.
    """
    if not env_val:
        return []
    has_colon = ":" in env_val
    has_comma = "," in env_val
    if has_comma:
        logger.warning(
            "MEDIAMAN_DELETE_ROOTS uses ',' separator — this is deprecated. "
            "Use ':' (PATH-style) instead; see .env.example."
        )
    if has_comma and has_colon:
        logger.error(
            "MEDIAMAN_DELETE_ROOTS contains both ':' and ',' separators — "
            "this is almost certainly a mistake.  Pick one (':' preferred) and retry."
        )
    return [r.strip() for r in re.split(r"[:,]", env_val) if r.strip()]


def disk_usage_allowed_roots() -> list[Path]:
    """Return the filesystem root paths the disk-usage endpoint may stat.

    Roots are sourced from:

    * ``MEDIAMAN_DELETE_ROOTS`` — colon-separated list of paths (same env var
      used by the scanner; commas accepted for legacy installs with a warning).
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

    for token in parse_delete_roots_env(os.environ.get("MEDIAMAN_DELETE_ROOTS") or ""):
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

    Note on the per-component symlink walk: there is a small TOCTOU
    window between the per-component ``is_symlink`` check and the
    final ``resolve()`` — an attacker who can rename a directory into
    a symlink between the two syscalls could in theory bypass the
    component check. The risk is bounded by the strict-descendant
    check at the end (the resolved path must still sit inside a
    configured root, so any swap would have to land *inside* that
    root to be useful), and this helper is only called from the
    disk-usage endpoint which is read-only — the worst-case failure
    is reading stat data from an unintended path within a configured
    root, not arbitrary file disclosure. For a destructive operation
    we'd need the fd-based ``O_NOFOLLOW`` pattern used by
    :mod:`mediaman.services.infra.storage`, but the read-only
    nature of this caller makes the upgrade a non-priority.
    """
    try:
        candidate = Path(raw)
        abs_candidate = candidate.absolute()
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
