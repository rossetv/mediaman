"""Delete-root allowlist validation — "is this allowlist safe to delete under?".

This module owns the path-safety exception hierarchy and every check that
runs *before* a deletion touches the filesystem: resolving each configured
root to its canonical form, refusing forbidden top-level paths, and the
atomic ``O_NOFOLLOW`` symlink check that closes the TOCTOU window. The
disk-usage / deletion *operations* that consume these validated roots live
in :mod:`.deletion` and :mod:`.disk_usage`.

Threat model: a buggy or compromised Plex part-path must never be able to
escalate a routine cleanup into a system-wide ``rmtree``. The first line of
defence is refusing to even *start* a deletion when the configured
``delete_allowed_roots`` allowlist is malformed — empty, relative, a symlink,
or pointing at a forbidden system directory.
"""

from __future__ import annotations

import errno as _errno
import os
import stat as _stat
from pathlib import Path


class PathSafetyError(Exception):
    """Base class for any path-safety refusal raised by this module.

    Raised when the allowlist is malformed (e.g. an entry is empty,
    relative, a symlink, or matches a forbidden top-level path), or when
    a deletion target violates the strict-descendant / same-device /
    no-symlink rules. Distinct from :class:`ValueError` so a generic
    ``except ValueError`` cannot catch a security refusal — callers that
    want to surface a refusal in the UI must catch this type explicitly.
    """


class DeletionRefused(PathSafetyError):
    """Raised by :func:`delete_path` (and helpers) when a deletion is refused.

    Used uniformly for every safety check during the deletion path:
    invalid allowlist, target not under any allowed root, target is a
    symlink, cross-device descent, missing-root re-check, etc. Catch this
    in the deletion executor to roll the row back to ``pending`` rather
    than ``deleted``.
    """


#: Paths that must never be configured as a delete root. A misconfigured
#: ``delete_allowed_roots = ["/"]`` (or any system directory) would let a
#: crafted Plex part-path escalate cleanup into a system-wide ``rmtree``,
#: so we refuse to even start a deletion when an allowlist contains any
#: of these. The list deliberately covers the standard FHS top-level
#: directories plus mediaman's own data home — operators should always
#: configure deletion at the *content* mount (e.g. ``/media/movies``)
#: rather than the umbrella mount.
_FORBIDDEN_ROOTS: frozenset[str] = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/data",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/libx32",
        "/media",
        "/mnt",
        "/opt",
        "/private",
        # macOS resolves /tmp, /var, /etc to /private/tmp, /private/var,
        # /private/etc. Without these explicit entries, an operator who
        # mis-configures their delete root to /tmp on macOS would have it
        # resolved to /private/tmp and slip past the bare-name check.
        "/private/etc",
        "/private/tmp",  # nosec B108  # rationale: listed as a *forbidden* delete root; mediaman never writes here
        "/private/var",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",  # nosec B108  # rationale: listed as a *forbidden* delete root; mediaman never writes here
        "/usr",
        "/var",
    }
)


def _resolve_root_candidate(raw: str) -> Path:
    """Resolve ``raw`` to its canonical form, raising :class:`DeletionRefused` on error.

    The resolve runs before any forbidden-root check so paths like
    ``/data/..`` (which resolves to ``/``) are caught regardless of
    whether the literal path exists. A missing path is tolerated here —
    operators sometimes configure roots that don't exist yet (e.g. a
    mount brought up later); the forbidden-root list still shields the
    dangerous cases.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise DeletionRefused(
            f"Refusing to delete: allowed root '{raw}' is not an "
            "absolute path. Configure delete_allowed_roots with "
            "absolute directory paths only."
        )
    try:
        return candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise DeletionRefused(
            f"Refusing to delete: allowed root '{raw}' could not be resolved: {exc}"
        ) from exc


def _check_symlink_via_nofollow(raw: str, candidate: Path) -> None:
    """Atomic symlink check: open *candidate* with ``O_NOFOLLOW | O_DIRECTORY``.

    The earlier two-step ``is_symlink()`` then ``resolve()`` had a TOCTOU
    window in which an attacker could swap the directory for a symlink
    between syscalls; the fd-based form rejects that. Skip the check if
    the path is missing — that matches previous behaviour for
    unconfigured-yet mounts and the forbidden-root check has already
    shielded the dangerous cases.
    """
    if not candidate.exists():
        return
    try:
        fd = os.open(
            str(candidate),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
    except OSError as exc:
        # ``O_NOFOLLOW`` on a symlink yields ``ELOOP`` on Linux but
        # ``ENOTDIR`` (and sometimes ``ELOOP``) on macOS, depending on
        # whether ``O_DIRECTORY`` or the symlink rule fires first.
        # Re-check with ``lstat`` to give a precise error in either case.
        try:
            lst = os.lstat(str(candidate))
        except OSError:
            lst = None

        if lst is not None and _stat.S_ISLNK(lst.st_mode):
            raise DeletionRefused(
                f"Refusing to delete: allowed root '{raw}' is a symlink. "
                "Configure delete_allowed_roots with real directories only."
            ) from None
        if exc.errno == _errno.ELOOP:
            raise DeletionRefused(
                f"Refusing to delete: allowed root '{raw}' is a symlink. "
                "Configure delete_allowed_roots with real directories only."
            ) from None
        if exc.errno == _errno.ENOTDIR:
            raise DeletionRefused(
                f"Refusing to delete: allowed root '{raw}' is not a directory."
            ) from None
        # Anything else (EACCES, etc.) — operator config error.
        raise DeletionRefused(
            f"Refusing to delete: allowed root '{raw}' cannot be opened: {exc}"
        ) from exc
    else:
        os.close(fd)


def _validate_single_root(raw: str) -> Path:
    """Resolve and sanity-check a single delete-root entry.

    Returns the resolved :class:`Path`, or raises :class:`DeletionRefused`
    if the entry is empty / not a string, relative, a forbidden
    system-root path, or a symlink.
    """
    if not raw or not isinstance(raw, str):
        raise DeletionRefused(
            "Refusing to delete: an entry in delete_allowed_roots is "
            "empty or not a string. Configure delete_allowed_roots "
            "with absolute directory paths only."
        )
    real = _resolve_root_candidate(raw)
    normalised = str(real)
    if normalised in _FORBIDDEN_ROOTS:
        raise DeletionRefused(
            f"Refusing to delete: allowed root '{raw}' resolves to "
            f"'{normalised}', a system / mount-root path that must not "
            "be a delete root. Configure delete_allowed_roots with "
            "specific content directories (e.g. '/media/movies'), "
            "never bare top-level mounts."
        )
    _check_symlink_via_nofollow(raw, Path(raw))
    return real


def _validate_delete_roots(roots: list[str]) -> list[Path]:
    """Resolve and sanity-check the configured delete-root allowlist.

    Returns the list of resolved root paths, or raises :class:`DeletionRefused`
    if any root is empty, relative, a symlink, or matches one of the
    well-known top-level system directories in :data:`_FORBIDDEN_ROOTS`.

    The resolved-path check runs against the **resolved** form so an
    attacker who manages to set ``delete_allowed_roots = ["/data/.."]``
    (which resolves to ``/``) is still refused.

    The symlink check is done by opening the candidate path with
    ``O_NOFOLLOW | O_DIRECTORY`` and lstat'ing the resulting fd in a
    single atomic step. The earlier two-step ``is_symlink()`` then
    ``resolve()`` had a TOCTOU window: an attacker who could swap the
    directory for a symlink between the two syscalls could slip past
    the symlink check. The fd-based form holds the inode reference so
    nothing can swap it after the open.
    """
    if not roots:
        raise DeletionRefused(
            "delete_allowed_roots not configured; refusing deletion. "
            "Set the delete_allowed_roots setting (JSON list) or the "
            "MEDIAMAN_DELETE_ROOTS env var (colon-separated)."
        )
    return [_validate_single_root(raw) for raw in roots]
